from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError
from src.engine.backtest_runner import (
    BacktestConfig,
    BacktestRunner,
    BacktestValidationError,
    CachedPITDataPortal,
    DailyNavRow,
    _assert_accounting_identity,
    _assert_non_negative_state,
    _money,
    _pending_dividend_total,
)
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.event_clock import EventDrivenClock
from src.engine.execution import FillLedgerEntry, LockedOrder, T1OpenExecutor
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PortfolioLedgerEntry
from src.golden_slice.deterministic_ledger_audit import (
    DeterministicLedgerAuditResult,
    run_deterministic_ledger_audit,
)
from src.golden_slice.execution_snapshot import (
    GoldenSliceExecutionSnapshot,
    build_execution_snapshot,
    cash_field_split_diagnostics,
    load_execution_snapshot,
)
from src.golden_slice.manifest import (
    GOLDEN_SLICE_SECURITY_IDS,
    assert_frozen_and_consistent,
)
from src.golden_slice.precomputed_signals import (
    OrderedSignalBinding,
    PrecomputedMomentumStrategy,
    load_ordered_signal_binding,
    monthly_first_trading_days,
)
from src.market_calendar import TradingCalendar, trading_calendar_from_dates
from src.portfolio.momentum_strategy import MomentumStrategyConfig


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
EXPECTED_MANIFEST_HASH = (
    "94e025b6a0b259c56751c6d3f3953c4a804aeea1c85b3b73dac9f9b2f468d4ae"
)
EVIDENCE_STATUS = "EXPLORATORY_TAINTED"
AUDIT_STATUS = "PENDING_AUDIT"
VALIDATION_SCOPE = "GOLDEN_SLICE_PIPELINE"
SIGNAL_START_DATE = date(2020, 1, 2)
SIGNAL_END_DATE = date(2023, 12, 29)
INITIAL_CASH = Decimal("1000000.00")
TOP_N = 3

DEFAULT_MANIFEST_PATH = Path("src/golden_slice/golden_slice_manifest.json")
DEFAULT_L1_PATH = Path("data/l1_raw/daily_bar_raw.parquet")
DEFAULT_SECURITY_MASTER_PATH = Path("data/l1_raw/security_master.parquet")
DEFAULT_CALENDAR_PATH = Path("data/l1_raw/trading_calendar.parquet")
DEFAULT_4A_ARTIFACT_DIR = Path(
    "artifacts/golden_slice_momentum_ic_20260728_94e025b6"
)
DEFAULT_ARTIFACTS_ROOT = Path("artifacts")


@dataclass(frozen=True)
class GoldenSliceExecutionRunResult:
    experiment_id: str
    artifact_dir: Path
    snapshot: GoldenSliceExecutionSnapshot
    signal_binding: OrderedSignalBinding
    rebalance_dates: tuple[date, ...]
    order_count: int
    fill_count: int
    rejection_reason_counts: tuple[tuple[str, int], ...]
    ca_event_counts: tuple[tuple[str, int], ...]
    unprocessed_ca_count: int
    total_fees: Decimal
    maximum_accounting_identity_deviation: Decimal
    deterministic_ledger_audit: DeterministicLedgerAuditResult


def run_golden_slice_execution_pipeline(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    l1_path: Path = DEFAULT_L1_PATH,
    security_master_path: Path = DEFAULT_SECURITY_MASTER_PATH,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
    four_a_artifact_dir: Path = DEFAULT_4A_ARTIFACT_DIR,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    run_date: date | None = None,
) -> GoldenSliceExecutionRunResult:
    # Governance gate is the first operation. No 4b output or input adaptation
    # is permitted before the frozen selection has passed.
    manifest = _load_frozen_manifest(manifest_path)
    assert_frozen_and_consistent(manifest)
    manifest_hash = str(manifest.get("manifest_hash", ""))
    if manifest_hash != EXPECTED_MANIFEST_HASH:
        raise DataContractError(
            "golden slice manifest hash is not the approved freeze: "
            f"expected={EXPECTED_MANIFEST_HASH}, actual={manifest_hash}"
        )

    effective_date = run_date or datetime.now(ASIA_SHANGHAI).date()
    snapshot_id = f"golden_slice_{effective_date.isoformat()}_EXECUTION"
    experiment_id = (
        "golden_slice_execution_"
        f"{effective_date.strftime('%Y%m%d')}_{manifest_hash[:8]}"
    )
    artifact_dir = artifacts_root / f"{experiment_id}_4b"
    snapshot_dir = artifact_dir / "snapshot"
    calendar = _load_trading_calendar(calendar_path)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    progress_path = artifact_dir / "progress.log"
    started_at = perf_counter()
    _progress(progress_path, "frozen manifest gate PASS; starting EXECUTION snapshot")

    if all(
        (snapshot_dir / name).exists()
        for name in (
            "daily_bar_raw.parquet",
            "corporate_actions.parquet",
            "security_master.parquet",
        )
    ):
        snapshot = load_execution_snapshot(
            output_dir=snapshot_dir,
            snapshot_id=snapshot_id,
        )
        snapshot_reused = True
    else:
        snapshot = build_execution_snapshot(
            manifest,
            l1_path=l1_path,
            security_master_path=security_master_path,
            output_dir=snapshot_dir,
            snapshot_id=snapshot_id,
        )
        snapshot_reused = False

    actions = pd.read_parquet(snapshot.corporate_action_path)
    split_diagnostics = cash_field_split_diagnostics(actions)
    _progress(
        progress_path,
        "EXECUTION snapshot "
        f"{'reused' if snapshot_reused else 'built'}; cash split 22/54 PASS",
    )

    predictions_path = four_a_artifact_dir / "predictions.parquet"
    feature_manifest_path = four_a_artifact_dir / "feature_manifest.json"
    binding = load_ordered_signal_binding(
        predictions_path=predictions_path,
        feature_manifest_path=feature_manifest_path,
        calendar=calendar,
        start_date=SIGNAL_START_DATE,
        end_date=SIGNAL_END_DATE,
        expected_security_ids=GOLDEN_SLICE_SECURITY_IDS,
    )
    rebalance_dates = monthly_first_trading_days(
        calendar,
        SIGNAL_START_DATE,
        SIGNAL_END_DATE,
    )
    _progress(
        progress_path,
        "signal projection PASS; ordered hash binding PASS; "
        f"monthly rebalance dates={len(rebalance_dates)}",
    )

    table_paths = {
        "daily_bar_raw": snapshot.daily_bar_path,
        "corporate_actions": snapshot.corporate_action_path,
        "security_master": snapshot.security_master_path,
    }
    portal = CachedPITDataPortal(table_paths, calendar)
    config = BacktestConfig(
        start_date=SIGNAL_START_DATE,
        end_date=SIGNAL_END_DATE,
        initial_cash=INITIAL_CASH,
        table_paths=table_paths,
        calendar_path=calendar_path,
        output_dir=artifact_dir,
        universe="frozen_golden_slice_12",
        exploratory_tainted=True,
    )
    phase1_runner = BacktestRunner(config, calendar=calendar, portal=portal)
    ledger = PortfolioLedger(
        CashState(settled_cash=INITIAL_CASH, available_cash=INITIAL_CASH),
        calendar=calendar,
    )
    strategy = PrecomputedMomentumStrategy(
        binding=binding,
        portfolio_ledger=ledger,
        config=MomentumStrategyConfig(top_n=TOP_N),
        rebalance_dates=frozenset(rebalance_dates),
    )
    executor = T1OpenExecutor(calendar, portal, end_date=SIGNAL_END_DATE)
    ca_handler = CorporateActionHandler(calendar, portal)
    clock = EventDrivenClock(
        SIGNAL_START_DATE,
        SIGNAL_END_DATE,
        calendar,
        portal,
    )

    locked_orders: list[LockedOrder] = []
    order_records: list[dict[str, Any]] = []
    fills: list[FillLedgerEntry] = []
    nav_rows: list[DailyNavRow] = []
    ca_observations: list[dict[str, Any]] = []
    pending_by_execution_date: dict[date, list[LockedOrder]] = {}
    last_visible_close: dict[str, Decimal] = {}
    previous_nav = INITIAL_CASH
    previous_unrealized = Decimal("0.00")
    maximum_identity_deviation = Decimal("0.00")
    trade_dates = tuple(calendar.between(SIGNAL_START_DATE, SIGNAL_END_DATE))
    processed_days = 0

    def on_bar(ctx) -> None:
        nonlocal previous_nav, previous_unrealized
        nonlocal maximum_identity_deviation, processed_days
        event_count_before = len(ledger.ledger_entries)
        pending_dividends_before = _pending_dividend_total(ledger)
        position_before = _position_state_snapshot(ledger)
        pending_by_key_before = dict(ledger.pending_cash_dividends)

        ledger.unlock_positions(ctx.trade_date)
        ca_entries = ca_handler.process_day(ledger, ctx.trade_date)
        ca_observations.extend(
            _observe_ca_entries(
                ca_entries,
                ledger,
                position_before,
                pending_by_key_before,
            )
        )
        unsupported = [
            entry
            for entry in ca_entries
            if entry.event_type
            in {"UNSUPPORTED_CORPORATE_EVENT", "UNPROCESSED_CA"}
        ]
        if unsupported:
            raise BacktestValidationError(
                "golden slice execution hit fail-closed CA event: "
                f"{[asdict(entry) for entry in unsupported]}"
            )

        due_orders = pending_by_execution_date.pop(ctx.trade_date, [])
        if due_orders:
            fills.extend(executor.execute_open_round(due_orders, ledger))

        intents = strategy.on_bar(ctx)
        for intent in intents:
            locked_or_fill = phase1_runner._lock_intent(  # noqa: SLF001
                intent,
                ledger,
                executor,
            )
            if isinstance(locked_or_fill, LockedOrder):
                locked_orders.append(locked_or_fill)
                order_records.append(_locked_order_record(locked_or_fill))
                execution_date = phase1_runner._execution_date(  # noqa: SLF001
                    locked_or_fill
                )
                pending_by_execution_date.setdefault(execution_date, []).append(
                    locked_or_fill
                )
            else:
                fills.append(locked_or_fill)
                order_records.append(_immediate_order_record(locked_or_fill))
                ledger.apply_execution_result(locked_or_fill)

        ledger.assert_invariants()
        nav_row = phase1_runner._nav_row(  # noqa: SLF001
            trade_date=ctx.trade_date,
            ledger=ledger,
            fills=fills,
            event_count=len(ledger.ledger_entries) - event_count_before,
            last_visible_close=last_visible_close,
            pending_dividends_before=pending_dividends_before,
            previous_nav=previous_nav,
            previous_unrealized=previous_unrealized,
        )
        expected_nav = _money(
            previous_nav
            + nav_row.realized_pnl
            + (nav_row.unrealized_pnl - previous_unrealized)
            + nav_row.dividend_accrued
        )
        deviation = abs(nav_row.nav - expected_nav)
        maximum_identity_deviation = max(
            maximum_identity_deviation,
            deviation,
        )
        _assert_accounting_identity(previous_nav, previous_unrealized, nav_row)
        _assert_non_negative_state(ledger)
        previous_nav = nav_row.nav
        previous_unrealized = nav_row.unrealized_pnl
        nav_rows.append(nav_row)

        processed_days += 1
        if processed_days % 100 == 0 or processed_days == len(trade_dates):
            elapsed = perf_counter() - started_at
            remaining = (
                (elapsed / processed_days) * (len(trade_dates) - processed_days)
            )
            _progress(
                progress_path,
                "execution progress: "
                f"{processed_days}/{len(trade_dates)}; "
                f"elapsed={elapsed:.1f}s; eta={remaining:.1f}s",
            )

    clock.run(on_bar)
    if pending_by_execution_date:
        raise BacktestValidationError(
            "pending orders remain after golden slice end_date"
        )

    unprocessed_count = sum(
        entry.event_type == "UNPROCESSED_CA" for entry in ledger.ledger_entries
    )
    if unprocessed_count:
        raise BacktestValidationError(
            f"golden slice UNPROCESSED_CA_count={unprocessed_count}"
        )
    deterministic_audit = run_deterministic_ledger_audit(
        corporate_action_path=snapshot.corporate_action_path,
        calendar=calendar,
    )
    _progress(
        progress_path,
        "main execution and deterministic CA ledger audit PASS; writing artifacts",
    )

    fills_frame = pd.DataFrame([_fill_record(fill) for fill in fills])
    ca_ledger_entries = [
        entry
        for entry in ledger.ledger_entries
        if entry.event_type.startswith("CA_")
        or entry.event_type
        in {"UNSUPPORTED_CORPORATE_EVENT", "UNPROCESSED_CA"}
    ]
    ledger_frame = pd.DataFrame(
        [_ledger_entry_record(entry) for entry in ca_ledger_entries]
    )
    nav_frame = pd.DataFrame([_nav_record(row) for row in nav_rows])
    pd.DataFrame(order_records).to_parquet(
        artifact_dir / "orders.parquet",
        index=False,
    )
    fills_frame.to_parquet(artifact_dir / "fills.parquet", index=False)
    ledger_frame.to_parquet(
        artifact_dir / "corporate_action_ledger.parquet",
        index=False,
    )
    nav_frame.to_parquet(
        artifact_dir / "portfolio_ledger.parquet",
        index=False,
    )

    common_status = {
        "evidence_status": EVIDENCE_STATUS,
        "audit_status": AUDIT_STATUS,
        "validation_scope": VALIDATION_SCOPE,
        "validation_scope_manifest_hash": manifest_hash,
    }
    shutil.copyfile(
        manifest_path,
        artifact_dir / "golden_slice_manifest.json",
    )
    ca_event_counts = Counter(
        entry.event_type
        for entry in ledger.ledger_entries
        if entry.event_type.startswith("CA_")
        or entry.event_type
        in {"UNSUPPORTED_CORPORATE_EVENT", "UNPROCESSED_CA"}
    )
    rejection_reason_counts = Counter(
        fill.reason for fill in fills if fill.status != "FILLED"
    )
    fill_status_counts = Counter(fill.status for fill in fills)
    execution_outcome_categories = {
        "filled": fill_status_counts.get("FILLED", 0),
        "limit_up_or_down_rejected": sum(
            rejection_reason_counts.get(reason, 0)
            for reason in ("LIMIT_UP_NO_BUY", "LIMIT_DOWN_NO_SELL")
        ),
        "suspended": fill_status_counts.get("SUSPENDED", 0),
        "cash_insufficient": rejection_reason_counts.get(
            "CASH_INSUFFICIENT",
            0,
        ),
        "capacity_rejected": sum(
            rejection_reason_counts.get(reason, 0)
            for reason in ("CAPACITY_NO_ADV_DATA", "CAPACITY_REJECTED")
        ),
        "no_open_price": rejection_reason_counts.get("NO_OPEN_PRICE", 0),
    }
    capacity_capped_count = sum(
        order.capacity_reason == "CAPACITY_CAPPED" for order in locked_orders
    )
    lot_size_adjustment_count = sum(
        order.original_quantity != order.locked_quantity
        and order.capacity_reason != "CAPACITY_CAPPED"
        for order in locked_orders
    )
    total_fees = _money(
        sum((fill.total_fee for fill in fills), Decimal("0.00"))
    )
    diagnostics = {
        **common_status,
        "experiment_id": experiment_id,
        "manifest_gate": "PASS",
        "snapshot": _snapshot_payload(snapshot, split_diagnostics),
        "signal_input": {
            "predictions_path": predictions_path.as_posix(),
            "predictions_sha256": binding.predictions_sha256,
            "predictions_sha256_has_4a_trusted_baseline": False,
            "projected_columns": list(binding.projected_columns),
            "forbidden_columns_not_loaded": [
                "future_return",
                "label_status",
                "label_end_ts",
                "label_observed_at",
                "rank_ic",
            ],
            "ordered_hash_binding": {
                "description": (
                    "The Nth real trading day is bound to the Nth 4a feature "
                    "manifest hash. This is not a content-recomputed hash check."
                ),
                "calendar_day_count": len(binding.signal_dates),
                "unique_signal_dates_match_calendar": True,
                "unique_signal_security_keys": True,
                "maximum_securities_per_day": max(
                    len(signal.points)
                    for signal in binding.signals_by_date.values()
                ),
                "feature_manifest_hash_count": len(
                    binding.signal_manifest_hashes
                ),
            },
        },
        "strategy": {
            "target_builder": "build_equal_weight_targets",
            "order_diff_path": (
                "SignalDrivenMomentumStrategy._diff_to_order_intents"
            ),
            "top_n": TOP_N,
            "initial_cash": str(INITIAL_CASH),
            "rebalance_policy": "first real trading day of each month",
            "rebalance_dates": [value.isoformat() for value in rebalance_dates],
            "fee_schedule": "Phase 1 default FeeSchedule test tier",
        },
        "execution": {
            "order_count": len(order_records),
            "locked_order_count": len(locked_orders),
            "fill_count": len(fills),
            "fill_status_counts": dict(sorted(fill_status_counts.items())),
            "rejection_reason_counts": dict(
                sorted(rejection_reason_counts.items())
            ),
            "outcome_categories": execution_outcome_categories,
            "capacity_capped_count": capacity_capped_count,
            "lot_size_adjustment_count": lot_size_adjustment_count,
            "total_fees": str(total_fees),
            "maximum_accounting_identity_deviation": str(
                maximum_identity_deviation
            ),
        },
        "main_backtest_corporate_actions": {
            "event_count": len(ca_observations),
            "event_type_counts": dict(sorted(ca_event_counts.items())),
            "UNPROCESSED_CA_count": unprocessed_count,
            "events": ca_observations,
        },
        "deterministic_ledger_audit": _jsonable(
            asdict(deterministic_audit)
        ),
        "limitations": {
            "all_input_data_clean": False,
            "l1_source": "akshare_raw",
            "broker_profile_available": False,
            "backtest_design_12_3_condition_6_fully_satisfied": False,
            "cash_payment_timing": (
                "Existing simplification: first trading day after ex-date; "
                "official payment-date timing was not validated."
            ),
            "performance_analysis_prohibited": True,
        },
    }
    _write_json(artifact_dir / "diagnostics.json", diagnostics)
    _write_json(
        artifact_dir / "execution_manifest.json",
        {
            **common_status,
            "snapshot_id": snapshot.snapshot_id,
            "snapshot_hashes": {
                "daily_bar_raw_sha256": snapshot.daily_bar_sha256,
                "corporate_actions_sha256": snapshot.corporate_action_sha256,
                "security_master_sha256": snapshot.security_master_sha256,
            },
            "source_snapshot_ids": list(snapshot.source_snapshot_ids),
            "source_security_master_snapshot_ids": list(
                snapshot.source_security_master_snapshot_ids
            ),
            "source_predictions_sha256": binding.predictions_sha256,
            "source_predictions_sha256_has_4a_trusted_baseline": False,
            "created_at": datetime.now(ASIA_SHANGHAI).isoformat(
                timespec="seconds"
            ),
        },
    )
    (artifact_dir / "report.md").write_text(
        _report_markdown(diagnostics),
        encoding="utf-8",
    )
    _progress(
        progress_path,
        f"4b complete; elapsed={perf_counter() - started_at:.1f}s",
    )

    return GoldenSliceExecutionRunResult(
        experiment_id=experiment_id,
        artifact_dir=artifact_dir,
        snapshot=snapshot,
        signal_binding=binding,
        rebalance_dates=rebalance_dates,
        order_count=len(order_records),
        fill_count=len(fills),
        rejection_reason_counts=tuple(
            sorted(rejection_reason_counts.items())
        ),
        ca_event_counts=tuple(sorted(ca_event_counts.items())),
        unprocessed_ca_count=unprocessed_count,
        total_fees=total_fees,
        maximum_accounting_identity_deviation=maximum_identity_deviation,
        deterministic_ledger_audit=deterministic_audit,
    )


def _load_frozen_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DataContractError(f"golden slice manifest does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_trading_calendar(path: Path) -> TradingCalendar:
    frame = pd.read_parquet(path)
    if "trade_date" not in frame.columns:
        raise DataContractError("trading calendar parquet missing trade_date")
    return trading_calendar_from_dates(frame["trade_date"])


def _position_state_snapshot(ledger: PortfolioLedger) -> dict[str, dict[str, Any]]:
    return {
        security_id: {
            "quantity": position.total_quantity,
            "cost_basis": position.cost_basis,
        }
        for security_id, position in ledger.positions.items()
    }


def _observe_ca_entries(
    entries: list[PortfolioLedgerEntry],
    ledger: PortfolioLedger,
    position_before: dict[str, dict[str, Any]],
    pending_before: dict[tuple[str, date], Decimal],
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for entry in entries:
        prior = position_before.get(
            entry.security_id,
            {"quantity": 0, "cost_basis": Decimal("0.00")},
        )
        position = ledger.positions.get(entry.security_id)
        pending_key = (entry.security_id, entry.trade_date)
        receivable_delta = Decimal("0.00")
        if entry.event_type == "CA_DIVIDEND_ACCRUED":
            receivable_delta = (
                ledger.pending_cash_dividends.get(
                    pending_key,
                    Decimal("0.00"),
                )
                - pending_before.get(pending_key, Decimal("0.00"))
            )
        stock_lots = []
        if position is not None and entry.event_type == "CA_SHARES_ADJUSTED":
            stock_lots = [
                lot
                for lot in position.lots
                if lot.source == "STOCK_DIVIDEND"
                and lot.trade_date == entry.trade_date
            ]
        observations.append(
            {
                "event_type": entry.event_type,
                "action_type": _action_type_for_entry(entry.event_type),
                "security_id": entry.security_id,
                "trade_date": entry.trade_date.isoformat(),
                "prior_position": prior["quantity"],
                "share_delta": entry.quantity_delta,
                "receivable_cash_delta": str(receivable_delta),
                "cost_basis_delta": str(entry.cost_basis_delta),
                "total_cost_basis_before": str(prior["cost_basis"]),
                "total_cost_basis_after": (
                    "0.00" if position is None else str(position.cost_basis)
                ),
                "sellable_date": (
                    stock_lots[-1].sellable_from.isoformat()
                    if stock_lots
                    else None
                ),
                "fill_reason": entry.fill_reason,
            }
        )
    return observations


def _action_type_for_entry(event_type: str) -> str:
    return {
        "CA_DIVIDEND_ACCRUED": "CASH_DIVIDEND",
        "CA_DIVIDEND_PAID": "CASH_DIVIDEND_PAYMENT",
        "CA_SHARES_ADJUSTED": "STOCK_DIVIDEND",
        "UNSUPPORTED_CORPORATE_EVENT": "UNSUPPORTED",
        "UNPROCESSED_CA": "UNPROCESSED",
    }.get(event_type, event_type)


def _locked_order_record(order: LockedOrder) -> dict[str, Any]:
    intent = order.order_intent
    return {
        "security_id": intent.security_id,
        "side": intent.side,
        "decision_date": intent.decision_date.isoformat(),
        "requested_quantity": intent.quantity,
        "locked_quantity": order.locked_quantity,
        "lock_status": "LOCKED",
        "reason": intent.reason,
        "signal_manifest_hash": getattr(intent, "signal_manifest_hash", ""),
        "target_weight": str(getattr(intent, "target_weight", "")),
        "reference_price": (
            None if order.reference_price is None else str(order.reference_price)
        ),
        "price_cap": None if order.price_cap is None else str(order.price_cap),
        "price_floor": (
            None if order.price_floor is None else str(order.price_floor)
        ),
        "reserved_cash": str(order.reserved_cash),
        "limit_check": order.limit_check,
        "limit_reference_status": order.limit_reference_status,
        "capacity_reason": order.capacity_reason,
        "adv_window_status": order.adv_window_status,
    }


def _immediate_order_record(fill: FillLedgerEntry) -> dict[str, Any]:
    intent = fill.order_intent
    return {
        "security_id": intent.security_id,
        "side": intent.side,
        "decision_date": intent.decision_date.isoformat(),
        "requested_quantity": intent.quantity,
        "locked_quantity": 0,
        "lock_status": fill.status,
        "reason": fill.reason,
        "signal_manifest_hash": getattr(intent, "signal_manifest_hash", ""),
        "target_weight": str(getattr(intent, "target_weight", "")),
        "reference_price": None,
        "price_cap": None,
        "price_floor": None,
        "reserved_cash": str(fill.reserved_cash),
        "limit_check": fill.limit_check,
        "limit_reference_status": fill.limit_reference_status,
        "capacity_reason": fill.capacity_reason,
        "adv_window_status": fill.adv_window_status,
    }


def _fill_record(fill: FillLedgerEntry) -> dict[str, Any]:
    return {
        "security_id": fill.order_intent.security_id,
        "side": fill.order_intent.side,
        "intent_date": fill.intent_date.isoformat(),
        "execution_date": (
            None
            if fill.execution_date is None
            else fill.execution_date.isoformat()
        ),
        "execution_price": fill.execution_price,
        "filled_quantity": fill.filled_quantity,
        "requested_quantity": fill.requested_quantity,
        "status": fill.status,
        "reason": fill.reason,
        "gross_amount": str(fill.gross_amount),
        "commission": str(fill.commission),
        "stamp_duty": str(fill.stamp_duty),
        "transfer_fee": str(fill.transfer_fee),
        "total_fee": str(fill.total_fee),
        "net_amount": str(fill.net_amount),
        "capacity_reason": fill.capacity_reason,
        "adv_window_status": fill.adv_window_status,
        "limit_reference_status": fill.limit_reference_status,
    }


def _ledger_entry_record(entry: PortfolioLedgerEntry) -> dict[str, Any]:
    payload = asdict(entry)
    payload["trade_date"] = entry.trade_date.isoformat()
    for field in (
        "cash_delta",
        "cost_basis_delta",
        "realized_pnl",
        "available_cash_after",
    ):
        payload[field] = str(payload[field])
    return payload


def _nav_record(row: DailyNavRow) -> dict[str, Any]:
    return {
        "trade_date": row.trade_date.isoformat(),
        "nav": str(row.nav),
        "cash": str(row.cash),
        "holdings_market_value": str(row.holdings_market_value),
        "event_count": row.event_count,
        "realized_pnl": str(row.realized_pnl),
        "unrealized_pnl": str(row.unrealized_pnl),
        "dividend_accrued": str(row.dividend_accrued),
        "fees": str(row.fees),
    }


def _snapshot_payload(
    snapshot: GoldenSliceExecutionSnapshot,
    split_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "purpose": "EXECUTION",
        "adjustment_only_snapshot_reuse_prohibited": True,
        "l1_row_count": snapshot.l1_row_count,
        "l1_rows_by_security": dict(snapshot.l1_rows_by_security),
        "ca_row_count": snapshot.ca_row_count,
        "security_master_row_count": snapshot.security_master_row_count,
        "source_snapshot_ids": list(snapshot.source_snapshot_ids),
        "source_security_master_snapshot_ids": list(
            snapshot.source_security_master_snapshot_ids
        ),
        "hashes": {
            "daily_bar_raw_sha256": snapshot.daily_bar_sha256,
            "corporate_actions_sha256": snapshot.corporate_action_sha256,
            "security_master_sha256": snapshot.security_master_sha256,
        },
        "cash_field_split": split_diagnostics,
        "security_master_current_snapshot_fields": (
            "PITDataPortal masks CURRENT_SNAPSHOT_ONLY fields at historical "
            "as-of; execution consumes board/list_date only. The selected 12 "
            "were non-ST by slice construction, but current is_st is not used."
        ),
    }


def _report_markdown(diagnostics: dict[str, Any]) -> str:
    snapshot = diagnostics["snapshot"]
    signal = diagnostics["signal_input"]
    execution = diagnostics["execution"]
    ca = diagnostics["main_backtest_corporate_actions"]
    audit = diagnostics["deterministic_ledger_audit"]
    ca_rows = "\n".join(
        "| {trade_date} | {security_id} | {action_type} | {prior_position} | "
        "{share_delta} | {receivable_cash_delta} | {cost_basis_delta} | "
        "{sellable_date} |".format(**event)
        for event in ca["events"]
    )
    return f"""# Golden Slice Block 4b Execution Audit

> This run validates the execution boundary and corporate-action ledger on the
> manually verified CA ledger. L1 prices still come from `akshare_raw`; it is
> incorrect to describe all inputs as clean. This report contains no strategy
> performance conclusion and deliberately omits annualized return, Sharpe,
> drawdown, win rate, and NAV charts.
>
> BACKTEST_DESIGN §12.3 condition 6 is **not fully satisfied** because this
> repository has no real broker profile. The run uses the Phase 1 default
> `FeeSchedule` test tier. Official cash payment dates are also not validated:
> the existing handler pays on the first trading day after ex-date.
>
> Whether the project reaches `BACKTEST_VALIDATED` remains subject to the ten
> post-run audit conditions in §12.3. This run does not self-certify.

## Status

- evidence_status: `{diagnostics['evidence_status']}`
- audit_status: `{diagnostics['audit_status']}`
- validation_scope: `{diagnostics['validation_scope']}`
- validation_scope_manifest_hash: `{diagnostics['validation_scope_manifest_hash']}`
- frozen manifest gate: `{diagnostics['manifest_gate']}`

## Snapshot

- snapshot_id: `{snapshot['snapshot_id']}`
- L1 rows: `{snapshot['l1_row_count']}`
- CA rows: `{snapshot['ca_row_count']}`
- security_master rows: `{snapshot['security_master_row_count']}`
- cash fields: `{snapshot['cash_field_split']['different_count']}` different / `{snapshot['cash_field_split']['same_count']}` equal
- maximum split: `{json.dumps(snapshot['cash_field_split']['maximum_difference'], ensure_ascii=False)}`

The EXECUTION snapshot carries actual cash entitlement in
`cash_dividend_per_share` and the ex-right deduction in
`ex_right_cash_deduction_per_share`. It does not reuse the 4a
`ADJUSTMENT_ONLY` snapshot.

## Signal Input

- physically projected columns: `{', '.join(signal['projected_columns'])}`
- predictions SHA-256: `{signal['predictions_sha256']}`
- 4a trusted baseline for this file hash: `False`
- ordered hash binding days: `{signal['ordered_hash_binding']['calendar_day_count']}`

“Ordered hash binding” means the Nth real trading day is paired with the Nth
hash in 4a's feature manifest. It is **not** a hash recomputed from parquet
contents; Decimal-to-float loss and the lack of date keys prevent that stronger
claim without regenerating 4a.

## Execution Diagnostics

- orders: `{execution['order_count']}`
- locked orders: `{execution['locked_order_count']}`
- fills/outcomes: `{execution['fill_count']}`
- fill status counts: `{json.dumps(execution['fill_status_counts'], ensure_ascii=False)}`
- rejection reasons: `{json.dumps(execution['rejection_reason_counts'], ensure_ascii=False)}`
- requested outcome categories: `{json.dumps(execution['outcome_categories'], ensure_ascii=False)}`
- capacity capped: `{execution['capacity_capped_count']}`
- lot-size adjustments: `{execution['lot_size_adjustment_count']}`
- total fees under Phase 1 test tier: `{execution['total_fees']}`
- maximum accounting identity deviation: `{execution['maximum_accounting_identity_deviation']}`

## Corporate Actions

- naturally triggered ledger observations: `{ca['event_count']}`
- event counts: `{json.dumps(ca['event_type_counts'], ensure_ascii=False)}`
- UNPROCESSED_CA_count: `{ca['UNPROCESSED_CA_count']}`

| date | security | action_type | prior position | share delta | receivable cash delta | cost basis delta | sellable date |
|---|---|---:|---:|---:|---:|---:|---|
{ca_rows}

The deterministic audit does not depend on the strategy happening to hold the
required names:

- cash audits: `{json.dumps(audit['cash_dividends'], ensure_ascii=False)}`
- stock audits: `{json.dumps(audit['stock_dividends'], ensure_ascii=False)}`

For stock dividends the asserted semantics are: total cost basis unchanged,
`cost_basis_delta=0`, per-share cost diluted, and the new lot has
`sellable_from=ex_date` with immediate unlock.

## Explicit Limitations

- No performance analysis is produced.
- Missing real broker profile means §12.3 condition 6 remains incomplete.
- Cash payment timing is the existing ex-date-plus-one-trading-day
  simplification, not an official payment-date validation.
- L1 remains `akshare_raw`; only the frozen CA ledger is manually verified from
  official PDFs.
"""


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            _jsonable(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _progress(path: Path, message: str) -> None:
    timestamp = datetime.now(ASIA_SHANGHAI).isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run golden-slice block 4b execution audit"
    )
    parser.add_argument("--manifest-path", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--l1-path", type=Path, default=DEFAULT_L1_PATH)
    parser.add_argument(
        "--security-master-path",
        type=Path,
        default=DEFAULT_SECURITY_MASTER_PATH,
    )
    parser.add_argument("--calendar-path", type=Path, default=DEFAULT_CALENDAR_PATH)
    parser.add_argument(
        "--four-a-artifact-dir",
        type=Path,
        default=DEFAULT_4A_ARTIFACT_DIR,
    )
    parser.add_argument(
        "--artifacts-root",
        type=Path,
        default=DEFAULT_ARTIFACTS_ROOT,
    )
    parser.add_argument("--run-date", type=date.fromisoformat)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = run_golden_slice_execution_pipeline(
        manifest_path=args.manifest_path,
        l1_path=args.l1_path,
        security_master_path=args.security_master_path,
        calendar_path=args.calendar_path,
        four_a_artifact_dir=args.four_a_artifact_dir,
        artifacts_root=args.artifacts_root,
        run_date=args.run_date,
    )
    print(
        json.dumps(
            {
                "experiment_id": result.experiment_id,
                "artifact_dir": result.artifact_dir.as_posix(),
                "orders": result.order_count,
                "fills": result.fill_count,
                "fees": str(result.total_fees),
                "maximum_accounting_identity_deviation": str(
                    result.maximum_accounting_identity_deviation
                ),
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
