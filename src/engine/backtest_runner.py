from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import PITDataPortal
from src.data.pit_data_portal import (
    REQUIRED_VISIBILITY_COLUMN,
    _mask_security_master_future_fields,
    _parse_asia_shanghai_timestamp,
)
from src.domain import TradeStatus
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.dummy_strategy import DummyRebalanceStrategy
from src.engine.event_clock import EventDrivenClock
from src.engine.execution import FillLedgerEntry, LockedOrder, T1OpenExecutor
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PortfolioLedgerEntry
from src.market_calendar import TradingCalendar, trading_calendar_from_dates


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_TIME = time(15, 0, 0)
MONEY_QUANT = Decimal("0.01")
DEFAULT_START_DATE = date(2015, 7, 1)
DEFAULT_END_DATE = date(2026, 6, 30)
DEFAULT_INITIAL_CASH = Decimal("1000000.00")
DEFAULT_OUTPUT_DIR = Path("data/backtest_output")
DEFAULT_TABLE_PATHS = {
    "daily_bar_raw": Path("data/l1_raw/daily_bar_raw.parquet"),
    "security_master": Path("data/l1_raw/security_master.parquet"),
    "corporate_actions": Path("data/l2_corporate_actions/corporate_actions.parquet"),
}


class BacktestValidationError(AssertionError):
    """Raised when the backtest violates a Phase 1 accounting invariant."""


@dataclass(frozen=True)
class BacktestConfig:
    start_date: date = DEFAULT_START_DATE
    end_date: date = DEFAULT_END_DATE
    initial_cash: Decimal = DEFAULT_INITIAL_CASH
    rebalance_every_n_days: int = 5
    target_count: int = 10
    order_quantity: int = 100
    table_paths: dict[str, Path] = field(default_factory=lambda: DEFAULT_TABLE_PATHS.copy())
    calendar_path: Path = Path("data/l1_raw/trading_calendar.parquet")
    output_dir: Path = DEFAULT_OUTPUT_DIR
    universe: str = "当前HS300非时点成分"
    exploratory_tainted: bool = True


@dataclass(frozen=True)
class DailyNavRow:
    trade_date: date
    nav: Decimal
    cash: Decimal
    holdings_market_value: Decimal
    event_count: int
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    dividend_accrued: Decimal
    fees: Decimal


@dataclass(frozen=True)
class BacktestSummary:
    trading_days: int
    filled_count: int
    rejected_count: int
    suspended_count: int
    unprocessed_ca_count: int
    total_fees: Decimal
    final_nav: Decimal


@dataclass(frozen=True)
class ValidationReport:
    no_exception: bool
    daily_accounting_identity: bool
    no_negative_cash: bool
    no_negative_position: bool
    ledger_invariants_daily: bool
    deterministic_nav_bytes: bool = False


@dataclass
class BacktestResult:
    config: BacktestConfig
    locked_orders: list[LockedOrder]
    fills: list[FillLedgerEntry]
    ledger_entries: list[PortfolioLedgerEntry]
    nav_rows: list[DailyNavRow]
    summary: BacktestSummary
    validation: ValidationReport

    def nav_csv_bytes(self) -> bytes:
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["日期", "净值", "现金", "持仓市值", "当日事件计数"])
        for row in self.nav_rows:
            writer.writerow(
                [
                    row.trade_date.isoformat(),
                    _money_str(row.nav),
                    _money_str(row.cash),
                    _money_str(row.holdings_market_value),
                    row.event_count,
                ]
            )
        return output.getvalue().encode("utf-8")

    def manifest(self) -> dict[str, object]:
        manifests = _read_source_manifests()
        return {
            "snapshot_id": {
                "daily_bar_raw": manifests.get("daily_bar_raw", {}).get("snapshot_id"),
                "security_master": manifests.get("security_master", {}).get("snapshot_id"),
                "corporate_actions": manifests.get("corporate_actions", {}).get("snapshot_id"),
            },
            "code_commit": _git_commit(),
            "strategy_params": {
                "strategy": "DummyRebalanceStrategy",
                "rebalance_every_n_days": self.config.rebalance_every_n_days,
                "target_count": self.config.target_count,
                "order_quantity": self.config.order_quantity,
                "initial_cash": _money_str(self.config.initial_cash),
                "start_date": self.config.start_date.isoformat(),
                "end_date": self.config.end_date.isoformat(),
            },
            "EXPLORATORY_TAINTED": self.config.exploratory_tainted,
            "universe": self.config.universe,
            "validation": {
                "no_exception": self.validation.no_exception,
                "daily_accounting_identity": self.validation.daily_accounting_identity,
                "accounting_identity_note": (
                    "Fees are netted into realized PnL and unrealized PnL through the "
                    "Phase 1 ledger cost-basis policy; total fees are reported separately."
                ),
                "no_negative_cash": self.validation.no_negative_cash,
                "no_negative_position": self.validation.no_negative_position,
                "ledger_invariants_daily": self.validation.ledger_invariants_daily,
                "deterministic_nav_bytes": self.validation.deterministic_nav_bytes,
            },
            "summary": {
                "trading_days": self.summary.trading_days,
                "filled_count": self.summary.filled_count,
                "rejected_count": self.summary.rejected_count,
                "suspended_count": self.summary.suspended_count,
                "UNPROCESSED_CA_count": self.summary.unprocessed_ca_count,
                "total_fees": _money_str(self.summary.total_fees),
                "final_nav": _money_str(self.summary.final_nav),
            },
        }


class BacktestRunner:
    def __init__(
        self,
        config: BacktestConfig | None = None,
        *,
        calendar: TradingCalendar | None = None,
        portal: PITDataPortal | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.calendar = calendar or load_trading_calendar(self.config.calendar_path)
        self.portal = portal or CachedPITDataPortal(self.config.table_paths)

    def run(self) -> BacktestResult:
        ledger = PortfolioLedger(
            CashState(
                settled_cash=self.config.initial_cash,
                available_cash=self.config.initial_cash,
            ),
            calendar=self.calendar,
        )
        strategy = RunnerDummyRebalanceStrategy(
            rebalance_every_n_days=self.config.rebalance_every_n_days,
            target_count=self.config.target_count,
            order_quantity=self.config.order_quantity,
            portfolio_ledger=ledger,
            tradable_by_date=_tradable_universe_by_date(self.portal),
        )
        executor = T1OpenExecutor(self.calendar, self.portal, end_date=self.config.end_date)
        ca_handler = CorporateActionHandler(self.calendar, self.portal)
        clock = EventDrivenClock(
            self.config.start_date,
            self.config.end_date,
            self.calendar,
            self.portal,
        )

        locked_orders: list[LockedOrder] = []
        fills: list[FillLedgerEntry] = []
        nav_rows: list[DailyNavRow] = []
        pending_by_execution_date: dict[date, list[LockedOrder]] = {}
        last_visible_close: dict[str, Decimal] = {}
        previous_nav = self.config.initial_cash
        previous_unrealized = Decimal("0.00")

        def on_bar(ctx) -> None:
            nonlocal previous_nav, previous_unrealized
            event_count_before = len(ledger.ledger_entries)
            pending_dividends_before = _pending_dividend_total(ledger)

            ledger.unlock_positions(ctx.trade_date)
            ca_handler.process_day(ledger, ctx.trade_date)

            due_orders = pending_by_execution_date.pop(ctx.trade_date, [])
            if due_orders:
                fills.extend(executor.execute_open_round(due_orders, ledger))

            intents = strategy.on_bar(ctx)
            for intent in intents:
                locked_or_fill = self._lock_intent(intent, ledger, executor)
                if isinstance(locked_or_fill, LockedOrder):
                    locked_orders.append(locked_or_fill)
                    execution_date = self._execution_date(locked_or_fill)
                    pending_by_execution_date.setdefault(execution_date, []).append(locked_or_fill)
                else:
                    fills.append(locked_or_fill)
                    ledger.apply_execution_result(locked_or_fill)

            ledger.assert_invariants()
            nav_row = self._nav_row(
                trade_date=ctx.trade_date,
                ledger=ledger,
                fills=fills,
                event_count=len(ledger.ledger_entries) - event_count_before,
                last_visible_close=last_visible_close,
                pending_dividends_before=pending_dividends_before,
                previous_nav=previous_nav,
                previous_unrealized=previous_unrealized,
            )
            _assert_accounting_identity(previous_nav, previous_unrealized, nav_row)
            _assert_non_negative_state(ledger)
            previous_nav = nav_row.nav
            previous_unrealized = nav_row.unrealized_pnl
            nav_rows.append(nav_row)

        clock.run(on_bar)

        if pending_by_execution_date:
            raise BacktestValidationError("pending orders remain after configured end_date")

        validation = ValidationReport(
            no_exception=True,
            daily_accounting_identity=True,
            no_negative_cash=True,
            no_negative_position=True,
            ledger_invariants_daily=True,
        )
        summary = _summary(nav_rows, fills, ledger.ledger_entries)
        return BacktestResult(
            config=self.config,
            locked_orders=locked_orders,
            fills=fills,
            ledger_entries=list(ledger.ledger_entries),
            nav_rows=nav_rows,
            summary=summary,
            validation=validation,
        )

    def _lock_intent(
        self,
        intent,
        ledger: PortfolioLedger,
        executor: T1OpenExecutor,
    ) -> LockedOrder | FillLedgerEntry:
        if intent.side == "sell":
            ledger.lock_for_sell(intent.security_id, intent.quantity, trade_date=intent.decision_date)
            return executor.lock_order(intent)

        locked_or_fill = executor.lock_order(intent, available_cash=ledger.cash.available_cash)
        if isinstance(locked_or_fill, LockedOrder):
            ledger.reserve_cash_for_buy(locked_or_fill)
        return locked_or_fill

    def _execution_date(self, locked_order: LockedOrder) -> date:
        try:
            return self.calendar.next_trading_day(locked_order.order_intent.decision_date)
        except IndexError as exc:
            raise BacktestValidationError("locked order has no next trading day") from exc

    def _nav_row(
        self,
        *,
        trade_date: date,
        ledger: PortfolioLedger,
        fills: list[FillLedgerEntry],
        event_count: int,
        last_visible_close: dict[str, Decimal],
        pending_dividends_before: Decimal,
        previous_nav: Decimal,
        previous_unrealized: Decimal,
    ) -> DailyNavRow:
        del previous_nav, previous_unrealized
        market_value = self._market_value(ledger, trade_date, last_visible_close)
        cash = _money(
            ledger.cash.available_cash + ledger.cash.frozen_cash + ledger.cash.receivable_cash
        )
        cost_basis = _money(
            sum(
                (position.cost_basis for position in ledger.positions.values()),
                Decimal("0.00"),
            )
        )
        unrealized = _money(market_value - cost_basis)
        nav = _money(cash + market_value)
        day_entries = [entry for entry in ledger.ledger_entries if entry.trade_date == trade_date]
        day_fills = [fill for fill in fills if fill.execution_date == trade_date]
        realized = _money(
            sum((entry.realized_pnl for entry in day_entries), Decimal("0.00"))
        )
        dividends_paid = _money(
            sum(
                (
                    entry.cash_delta
                    for entry in day_entries
                    if entry.event_type == "CA_DIVIDEND_PAID"
                ),
                Decimal("0.00"),
            )
        )
        dividend_accrued = _money(
            _pending_dividend_total(ledger) - pending_dividends_before + dividends_paid
        )
        fees = _money(sum((fill.total_fee for fill in day_fills), Decimal("0.00")))
        return DailyNavRow(
            trade_date=trade_date,
            nav=nav,
            cash=cash,
            holdings_market_value=market_value,
            event_count=event_count,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            dividend_accrued=dividend_accrued,
            fees=fees,
        )

    def _market_value(
        self,
        ledger: PortfolioLedger,
        trade_date: date,
        last_visible_close: dict[str, Decimal],
    ) -> Decimal:
        held_ids = [
            security_id
            for security_id, position in sorted(ledger.positions.items())
            if position.total_quantity > 0
        ]
        if not held_ids:
            return Decimal("0.00")

        rows = self.portal.query(
            "daily_bar_raw",
            _market_close_asof(trade_date),
            security_ids=held_ids,
            columns=["security_id", "trade_date", "close"],
        )
        if not rows.empty:
            rows = rows.copy()
            rows["_trade_date"] = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
            rows = rows.loc[rows["_trade_date"] <= trade_date].copy()
            rows = rows.loc[rows["close"].notna()].copy()
            rows = rows.sort_values(["security_id", "_trade_date"])
            for row in rows.itertuples(index=False):
                security_id = str(getattr(row, "security_id")).zfill(6)
                last_visible_close[security_id] = _money(Decimal(str(getattr(row, "close"))))

        market_value = Decimal("0.00")
        for security_id in held_ids:
            position = ledger.positions[security_id]
            close = last_visible_close.get(security_id)
            if close is None:
                raise BacktestValidationError(
                    f"no visible close for held security {security_id} on {trade_date}"
                )
            market_value += close * Decimal(position.total_quantity)
        return _money(market_value)


def run_default_backtest(*, deterministic_check: bool = True) -> BacktestResult:
    config = BacktestConfig()
    calendar = load_trading_calendar(config.calendar_path)
    portal = CachedPITDataPortal(config.table_paths)
    first = BacktestRunner(config, calendar=calendar, portal=portal).run()
    if not deterministic_check:
        return first
    second = BacktestRunner(config, calendar=calendar, portal=portal).run()
    deterministic = first.nav_csv_bytes() == second.nav_csv_bytes()
    validation = ValidationReport(
        no_exception=first.validation.no_exception,
        daily_accounting_identity=first.validation.daily_accounting_identity,
        no_negative_cash=first.validation.no_negative_cash,
        no_negative_position=first.validation.no_negative_position,
        ledger_invariants_daily=first.validation.ledger_invariants_daily,
        deterministic_nav_bytes=deterministic,
    )
    if not deterministic:
        raise BacktestValidationError("nav_daily.csv is not byte-identical across reruns")
    first.validation = validation
    return first


def write_backtest_outputs(result: BacktestResult) -> None:
    output_dir = result.config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "nav_daily.csv").write_bytes(result.nav_csv_bytes())
    manifest_text = json.dumps(
        result.manifest(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    (output_dir / "run_manifest.json").write_text(manifest_text + "\n", encoding="utf-8")


class CachedPITDataPortal(PITDataPortal):
    def __init__(self, table_paths: dict[str, Path]) -> None:
        super().__init__(table_paths)
        object.__setattr__(self, "_table_cache", {})
        object.__setattr__(self, "_daily_by_security_cache", None)

    def _read_table(self, table: str) -> pd.DataFrame:
        cache: dict[str, pd.DataFrame] = getattr(self, "_table_cache")
        if table not in cache:
            cache[table] = self._prepare_table(table, super()._read_table(table))
        return cache[table]

    def query(
        self,
        table: str,
        asof_ts: str | pd.Timestamp,
        security_ids: Iterable[str] | None = None,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        if table == "daily_bar_raw" and security_ids is not None:
            rows = self._daily_rows_for_security_ids(security_ids)
        else:
            rows = self._read_table(table)

        asof = _parse_asia_shanghai_timestamp(asof_ts, "asof_ts")
        visible = self._visible_rows(table, rows, asof)

        if security_ids is not None:
            if "security_id" not in visible.columns:
                raise ValueError(f"{table} is missing required security_id column")
            requested_ids = {str(security_id).zfill(6) for security_id in security_ids}
            visible = visible.loc[visible["_security_id_norm"].isin(requested_ids)].copy()

        public = _drop_runner_helper_columns(visible)
        if table == "security_master":
            public = _mask_security_master_future_fields(public, asof)

        output_columns = self._resolve_columns(table, public, columns)
        result = public.loc[:, output_columns].copy()
        result.attrs["asof_ts"] = asof.isoformat()
        result.attrs["table"] = table
        result.attrs["field_capabilities"] = {}
        result.attrs["visibility_predicate"] = (
            f"{REQUIRED_VISIBILITY_COLUMN} <= {asof.isoformat()}"
        )
        return result

    def daily_rows(self) -> pd.DataFrame:
        return self._read_table("daily_bar_raw")

    def _prepare_table(self, table: str, rows: pd.DataFrame) -> pd.DataFrame:
        if REQUIRED_VISIBILITY_COLUMN not in rows.columns:
            raise ValueError(f"{table} is missing required {REQUIRED_VISIBILITY_COLUMN}")
        prepared = rows.copy()
        prepared["_available_at_parsed"] = pd.to_datetime(
            prepared[REQUIRED_VISIBILITY_COLUMN],
            errors="raise",
        )
        if "event_ts" in prepared.columns:
            prepared["_event_ts_parsed"] = pd.to_datetime(prepared["event_ts"], errors="raise")
        if "trade_date" in prepared.columns:
            prepared["_trade_date_date"] = pd.to_datetime(
                prepared["trade_date"],
                errors="raise",
            ).dt.date
        if "security_id" in prepared.columns:
            prepared["_security_id_norm"] = prepared["security_id"].astype(str).str.zfill(6)
        return prepared

    def _visible_rows(
        self,
        table: str,
        rows: pd.DataFrame,
        asof: pd.Timestamp,
    ) -> pd.DataFrame:
        mask = rows["_available_at_parsed"].le(asof)
        if table == "daily_bar_raw" and "_event_ts_parsed" in rows.columns:
            mask = mask & rows["_event_ts_parsed"].le(asof)
        return rows.loc[mask].copy()

    def _daily_rows_for_security_ids(self, security_ids: Iterable[str]) -> pd.DataFrame:
        by_security = self._daily_by_security()
        frames = [
            by_security[security_id]
            for security_id in (str(value).zfill(6) for value in security_ids)
            if security_id in by_security
        ]
        if not frames:
            return self._read_table("daily_bar_raw").iloc[0:0].copy()
        return pd.concat(frames, ignore_index=True)

    def _daily_by_security(self) -> dict[str, pd.DataFrame]:
        cached = getattr(self, "_daily_by_security_cache")
        if cached is not None:
            return cached
        rows = self._read_table("daily_bar_raw")
        by_security = {
            security_id: group.copy()
            for security_id, group in rows.groupby("_security_id_norm", sort=False)
        }
        object.__setattr__(self, "_daily_by_security_cache", by_security)
        return by_security


@dataclass
class RunnerDummyRebalanceStrategy(DummyRebalanceStrategy):
    tradable_by_date: dict[date, list[str]] = field(default_factory=dict)

    def _tradable_universe(self, ctx) -> list[str]:
        return self.tradable_by_date.get(ctx.trade_date, [])


def _tradable_universe_by_date(portal: PITDataPortal) -> dict[date, list[str]]:
    if isinstance(portal, CachedPITDataPortal):
        rows = portal.daily_rows().copy()
    else:
        rows = portal.query(
            "daily_bar_raw",
            _market_close_asof(DEFAULT_END_DATE),
            columns=["security_id", "trade_date", "trade_status", "event_ts", "available_at"],
        )
        rows = rows.copy()
        rows["_trade_date_date"] = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        rows["_security_id_norm"] = rows["security_id"].astype(str).str.zfill(6)

    rows = _rows_visible_at_own_trade_date(rows)
    tradable = rows.loc[rows["trade_status"].astype(str) == TradeStatus.NORMAL.value].copy()
    result: dict[date, list[str]] = {}
    for trade_date, group in tradable.groupby("_trade_date_date"):
        result[trade_date] = sorted(group["_security_id_norm"].drop_duplicates().tolist())
    return result


def _rows_visible_at_own_trade_date(rows: pd.DataFrame) -> pd.DataFrame:
    filtered = rows.copy()
    if "_trade_date_date" not in filtered.columns:
        filtered["_trade_date_date"] = pd.to_datetime(
            filtered["trade_date"],
            errors="raise",
        ).dt.date
    if "_security_id_norm" not in filtered.columns:
        filtered["_security_id_norm"] = filtered["security_id"].astype(str).str.zfill(6)

    if "_available_at_parsed" in filtered.columns:
        available_at = filtered["_available_at_parsed"]
    else:
        available_at = pd.to_datetime(filtered["available_at"], errors="raise")

    trade_date_asof = pd.to_datetime(
        filtered["_trade_date_date"].astype(str) + "T15:00:00+08:00",
        errors="raise",
    )
    visible_mask = available_at.le(trade_date_asof)
    if "event_ts" in filtered.columns or "_event_ts_parsed" in filtered.columns:
        if "_event_ts_parsed" in filtered.columns:
            event_ts = filtered["_event_ts_parsed"]
        else:
            event_ts = pd.to_datetime(filtered["event_ts"], errors="raise")
        visible_mask = visible_mask & event_ts.le(trade_date_asof)
    return filtered.loc[visible_mask].copy()


def _drop_runner_helper_columns(rows: pd.DataFrame) -> pd.DataFrame:
    helper_columns = [
        column
        for column in rows.columns
        if column
        in {
            "_available_at_parsed",
            "_event_ts_parsed",
            "_trade_date_date",
            "_security_id_norm",
        }
    ]
    if not helper_columns:
        return rows
    return rows.drop(columns=helper_columns)


def load_trading_calendar(path: Path) -> TradingCalendar:
    rows = pd.read_parquet(path)
    if "trade_date" in rows.columns:
        values = rows["trade_date"]
    else:
        values = rows.iloc[:, 0]
    return trading_calendar_from_dates(values)


def _summary(
    nav_rows: list[DailyNavRow],
    fills: list[FillLedgerEntry],
    entries: list[PortfolioLedgerEntry],
) -> BacktestSummary:
    final_nav = nav_rows[-1].nav if nav_rows else Decimal("0.00")
    return BacktestSummary(
        trading_days=len(nav_rows),
        filled_count=sum(1 for fill in fills if fill.status == "FILLED"),
        rejected_count=sum(1 for fill in fills if fill.status == "REJECTED"),
        suspended_count=sum(1 for fill in fills if fill.status == "SUSPENDED"),
        unprocessed_ca_count=sum(1 for entry in entries if entry.event_type == "UNPROCESSED_CA"),
        total_fees=_money(sum((fill.total_fee for fill in fills), Decimal("0.00"))),
        final_nav=final_nav,
    )


def _assert_accounting_identity(
    previous_nav: Decimal,
    previous_unrealized: Decimal,
    row: DailyNavRow,
) -> None:
    expected = _money(
        previous_nav
        + row.realized_pnl
        + (row.unrealized_pnl - previous_unrealized)
        + row.dividend_accrued
    )
    if row.nav != expected:
        raise BacktestValidationError(
            f"daily accounting identity failed on {row.trade_date}: "
            f"expected={expected}, actual={row.nav}"
        )


def _assert_non_negative_state(ledger: PortfolioLedger) -> None:
    if ledger.cash.available_cash < Decimal("0.00"):
        raise BacktestValidationError("available cash is negative")
    if ledger.cash.frozen_cash < Decimal("0.00"):
        raise BacktestValidationError("frozen cash is negative")
    if ledger.cash.receivable_cash < Decimal("0.00"):
        raise BacktestValidationError("receivable cash is negative")
    for position in ledger.positions.values():
        if position.total_quantity < 0:
            raise BacktestValidationError(f"negative position for {position.security_id}")


def _pending_dividend_total(ledger: PortfolioLedger) -> Decimal:
    return _money(sum(ledger.pending_cash_dividends.values(), Decimal("0.00")))


def _read_source_manifests() -> dict[str, dict[str, object]]:
    paths = {
        "daily_bar_raw": Path("data/l1_raw/manifest.json"),
        "security_master": Path("data/l1_raw/security_master_manifest.json"),
        "corporate_actions": Path("data/l2_corporate_actions/manifest.json"),
    }
    manifests: dict[str, dict[str, object]] = {}
    for name, path in paths.items():
        if not path.exists():
            manifests[name] = {}
            continue
        manifests[name] = json.loads(path.read_text(encoding="utf-8"))
    return manifests


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return "UNKNOWN"
    return completed.stdout.strip()


def _market_close_asof(trade_date: date) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(trade_date, MARKET_CLOSE_TIME, tzinfo=ASIA_SHANGHAI))


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _money_str(value: Decimal) -> str:
    return format(_money(value), "f")


def _as_date(value: date | str | pd.Timestamp) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def dates(values: Iterable[date | str | pd.Timestamp]) -> list[date]:
    return [_as_date(value) for value in values]
