from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import EventDrivenClock, LockedOrder, T1OpenExecutor
from src.engine.dummy_strategy import DummyRebalanceStrategy, OrderIntent
from src.engine.execution import FillLedgerEntry, LimitRuleTable
from src.engine.portfolio_ledger import (
    CashState,
    PortfolioLedger,
    PortfolioLedgerEntry,
    PositionLot,
    PositionState,
)
from src.market_calendar import trading_calendar_from_dates


class RebalanceSellPathTest(unittest.TestCase):
    def test_rebalance_sell_intent_fills_t1_and_updates_ledger(self) -> None:
        result = _run_sell_path(t1_open=12.0)

        fill = result.fills[0]
        position = result.ledger.positions["000003"]
        self.assertEqual(result.first_day_intents[0].side, "sell")
        self.assertEqual(result.first_day_intents[0].security_id, "000003")
        self.assertEqual(fill.status, "FILLED")
        self.assertEqual(fill.execution_date, date(2026, 6, 30))
        self.assertEqual(fill.reason, "T1_OPEN_FILLED")
        self.assertEqual(position.total_quantity, 0)
        self.assertEqual(result.ledger.cash.available_cash, Decimal("1194.39"))
        self.assertEqual(result.ledger.ledger_entries[-1].realized_pnl, Decimal("194.39"))
        self.assertEqual(_event_types(result.ledger.ledger_entries), ["SELL_LOCK", "SELL_FILL"])

    def test_limit_down_sell_reject_releases_locked_inventory(self) -> None:
        result = _run_sell_path(t1_open=9.0)

        fill = result.fills[0]
        position = result.ledger.positions["000003"]
        self.assertEqual(fill.status, "REJECTED")
        self.assertEqual(fill.reason, "LIMIT_DOWN_NO_SELL")
        self.assertEqual(position.total_quantity, 100)
        self.assertEqual(position.locked_quantity, 0)
        self.assertEqual(position.sellable_quantity, 100)
        self.assertEqual(result.ledger.cash.available_cash, Decimal("0.00"))
        self.assertEqual(_event_types(result.ledger.ledger_entries), ["SELL_LOCK", "SELL_LOCK_RELEASE"])

    def test_suspended_sell_releases_locked_inventory(self) -> None:
        result = _run_sell_path(t1_open=None, t1_trade_status=TradeStatus.SUSPENDED.value)

        fill = result.fills[0]
        position = result.ledger.positions["000003"]
        self.assertEqual(fill.status, "SUSPENDED")
        self.assertEqual(fill.reason, "NO_TRADE_SUSPENDED")
        self.assertEqual(position.total_quantity, 100)
        self.assertEqual(position.locked_quantity, 0)
        self.assertEqual(position.sellable_quantity, 100)
        self.assertEqual(result.ledger.cash.available_cash, Decimal("0.00"))
        self.assertEqual(_event_types(result.ledger.ledger_entries), ["SELL_LOCK", "SELL_LOCK_RELEASE"])

    def test_rebalance_sell_path_is_reproducible_for_same_inputs(self) -> None:
        first = _run_sell_path(t1_open=12.0)
        second = _run_sell_path(t1_open=12.0)

        self.assertEqual(first.first_day_intents, second.first_day_intents)
        self.assertEqual(first.fills, second.fills)
        self.assertEqual(first.ledger.cash, second.ledger.cash)
        self.assertEqual(first.ledger.positions, second.ledger.positions)
        self.assertEqual(first.ledger.ledger_entries, second.ledger.ledger_entries)


@dataclass(frozen=True)
class SellPathResult:
    ledger: PortfolioLedger
    first_day_intents: list[OrderIntent]
    fills: list[FillLedgerEntry]


def _run_sell_path(
    *,
    t1_open: float | None,
    t1_trade_status: str = TradeStatus.NORMAL.value,
) -> SellPathResult:
    with tempfile.TemporaryDirectory() as tmpdir:
        calendar = _calendar()
        portal = _portal(tmpdir, t1_open=t1_open, t1_trade_status=t1_trade_status)
        ledger = _seeded_ledger(calendar)
        strategy = DummyRebalanceStrategy(
            rebalance_every_n_days=1,
            target_count=2,
            order_quantity=100,
            portfolio_ledger=ledger,
        )
        executor = T1OpenExecutor(calendar, portal, end_date=date(2026, 6, 30))
        clock = EventDrivenClock("2026-06-29", "2026-06-30", calendar, portal)
        pending_sells: list[LockedOrder] = []
        first_day_intents: list[OrderIntent] = []
        fills: list[FillLedgerEntry] = []

        def on_bar(ctx) -> None:
            ledger.unlock_positions(ctx.trade_date)
            if pending_sells:
                for intent in list(pending_sells):
                    fill = executor.execute_one(intent)
                    fills.append(fill)
                    ledger.apply_execution_result(fill)
                pending_sells.clear()
                return

            intents = strategy.on_bar(ctx)
            if ctx.trade_date == date(2026, 6, 29):
                first_day_intents.extend(intents)
            for intent in intents:
                if intent.side == "sell":
                    ledger.lock_for_sell(
                        intent.security_id,
                        intent.quantity,
                        trade_date=ctx.trade_date,
                    )
                    locked_order = executor.lock_order(intent)
                    if not isinstance(locked_order, LockedOrder):
                        raise AssertionError(f"expected LockedOrder, got {locked_order}")
                    pending_sells.append(locked_order)

        clock.run(on_bar)
        return SellPathResult(ledger=ledger, first_day_intents=first_day_intents, fills=fills)


def _seeded_ledger(calendar) -> PortfolioLedger:
    ledger = PortfolioLedger(CashState(), calendar=calendar)
    ledger.positions["000003"] = PositionState(
        "000003",
        lots=[
            PositionLot(
                quantity=100,
                cost_basis=Decimal("1000.00"),
                trade_date=date(2026, 6, 26),
                sellable_from=date(2026, 6, 29),
            )
        ],
    )
    return ledger


def _calendar():
    return trading_calendar_from_dates([date(2026, 6, 29), date(2026, 6, 30)])


def _portal(
    tmpdir: str,
    *,
    t1_open: float | None,
    t1_trade_status: str,
) -> PITDataPortal:
    daily_path = Path(tmpdir) / "daily_bar_raw.parquet"
    security_master_path = Path(tmpdir) / "security_master.parquet"
    pd.DataFrame(_daily_rows(t1_open, t1_trade_status)).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(security_master_path, index=False)
    return PITDataPortal({"daily_bar_raw": daily_path, "security_master": security_master_path})


def _daily_rows(t1_open: float | None, t1_trade_status: str) -> list[dict[str, object]]:
    rows = [
        _bar_row("000001", "2026-06-29", open_price=10.0, close=10.0),
        _bar_row("000002", "2026-06-29", open_price=10.0, close=10.0),
        _bar_row("000003", "2026-06-29", open_price=10.0, close=10.0),
        _bar_row("000001", "2026-06-30", open_price=10.0, close=10.0),
        _bar_row("000002", "2026-06-30", open_price=10.0, close=10.0),
    ]
    rows.append(
        _bar_row(
            "000003",
            "2026-06-30",
            open_price=t1_open,
            close=t1_open,
            trade_status=t1_trade_status,
        )
    )
    return rows


def _bar_row(
    security_id: str,
    trade_date: str,
    *,
    open_price: float | None,
    close: float | None,
    trade_status: str = TradeStatus.NORMAL.value,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date,
        "open": open_price,
        "high": open_price,
        "low": open_price,
        "close": close,
        "volume": 999999,
        "trade_status": trade_status,
        "event_ts": f"{trade_date}T15:00:00+08:00",
        "available_at": f"{trade_date}T15:00:00+08:00",
        "snapshot_id": "fixture",
    }


def _security_master_rows() -> list[dict[str, object]]:
    board = next(iter(LimitRuleTable().rules_by_board))
    return [
        {
            "security_id": security_id,
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "fixture",
        }
        for security_id in ("000001", "000002", "000003")
    ]


def _event_types(entries: list[PortfolioLedgerEntry]) -> list[str]:
    return [entry.event_type for entry in entries]


if __name__ == "__main__":
    unittest.main()
