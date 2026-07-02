from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import BROKER_ADAPTER_RULE, LockedOrder, OrderIntent, T1OpenExecutor
from src.engine.execution import LimitRuleTable
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState
from src.market_calendar import trading_calendar_from_dates


class OpenRoundExecutionTest(unittest.TestCase):
    def test_ex010_same_open_round_forces_sell_batch_before_buy_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _ledger_with_sellable_position(initial_cash=Decimal("0.00"))
            executor = _executor(tmpdir, _round_rows())
            buy_locked, sell_locked = _lock_buy_then_sell(executor, ledger)

            fills = executor.execute_open_round([buy_locked, sell_locked], ledger)

            self.assertEqual([fill.order_intent.side for fill in fills], ["sell", "buy"])
            self.assertEqual([fill.status for fill in fills], ["FILLED", "FILLED"])
            self.assertEqual(
                [entry.event_type for entry in ledger.ledger_entries],
                ["SELL_LOCK", "SELL_FILL", "BUY_FILL"],
            )

    def test_ex010_sell_net_cash_is_available_to_same_round_buy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _ledger_with_sellable_position(initial_cash=Decimal("0.00"))
            executor = _executor(tmpdir, _round_rows())
            buy_locked, sell_locked = _lock_buy_then_sell(executor, ledger)

            fills = executor.execute_open_round([buy_locked, sell_locked], ledger)
            buy_entry = ledger.ledger_entries[-1]

            self.assertEqual(fills[0].net_amount, Decimal("994.49"))
            self.assertEqual(fills[1].net_amount, Decimal("905.01"))
            self.assertEqual(buy_entry.event_type, "BUY_FILL")
            self.assertEqual(buy_entry.available_cash_after, Decimal("89.48"))
            self.assertEqual(ledger.cash.available_cash, Decimal("89.48"))

    def test_ex010_t_day_locking_does_not_preborrow_future_sell_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = _ledger_with_sellable_position(initial_cash=Decimal("100.00"))
            executor = _executor(tmpdir, _round_rows())
            sell_lock = executor.lock_order(_intent("000001", "sell"))
            buy_lock = executor.lock_order(
                _intent("000003", "buy"),
                available_cash=ledger.cash.available_cash,
            )

            self.assertIsInstance(sell_lock, LockedOrder)
            self.assertNotIsInstance(buy_lock, LockedOrder)
            self.assertEqual(buy_lock.status, "REJECTED")
            self.assertEqual(buy_lock.reason, "CASH_INSUFFICIENT")
            self.assertIsNone(buy_lock.execution_date)

    def test_ex010_broker_adapter_rule_is_declared_for_live_reconciliation(self) -> None:
        self.assertIn("先处理全部卖单、再处理全部买单", BROKER_ADAPTER_RULE)
        self.assertIn("卖出FILLED所释放的净额现金", BROKER_ADAPTER_RULE)
        self.assertIn("实盘对接时须逐字核对券商真实资金可用规则", BROKER_ADAPTER_RULE)


def _lock_buy_then_sell(
    executor: T1OpenExecutor,
    ledger: PortfolioLedger,
) -> tuple[LockedOrder, LockedOrder]:
    ledger.lock_for_sell("000001", 100, trade_date=date(2026, 6, 29))
    sell_locked = executor.lock_order(_intent("000001", "sell"))
    buy_locked = executor.lock_order(
        _intent("000002", "buy"),
        available_cash=ledger.cash.available_cash,
    )
    if not isinstance(sell_locked, LockedOrder):
        raise AssertionError(f"expected sell LockedOrder, got {sell_locked}")
    if not isinstance(buy_locked, LockedOrder):
        raise AssertionError(f"expected buy LockedOrder, got {buy_locked}")
    if buy_locked.reserved_cash != Decimal("0.00"):
        raise AssertionError(
            f"expected no T-day buy reservation, got {buy_locked.reserved_cash}"
        )
    return buy_locked, sell_locked


def _ledger_with_sellable_position(initial_cash: Decimal) -> PortfolioLedger:
    ledger = PortfolioLedger(
        CashState(settled_cash=initial_cash, available_cash=initial_cash),
        calendar=_calendar(),
    )
    ledger.positions["000001"] = PositionState(
        "000001",
        [
            PositionLot(
                quantity=100,
                cost_basis=Decimal("500.00"),
                trade_date=date(2026, 6, 26),
                sellable_from=date(2026, 6, 29),
                is_unlocked=True,
            )
        ],
    )
    return ledger


def _executor(tmpdir: str, rows: list[dict[str, object]]) -> T1OpenExecutor:
    daily_path = Path(tmpdir) / "daily_bar_raw.parquet"
    security_master_path = Path(tmpdir) / "security_master.parquet"
    pd.DataFrame(rows).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(security_master_path, index=False)
    portal = PITDataPortal({"daily_bar_raw": daily_path, "security_master": security_master_path})
    return T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))


def _round_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=10.0, close=10.0),
        _bar_row("000001", "2026-06-30", open_price=10.0, close=10.0),
        _bar_row("000002", "2026-06-30", open_price=9.0, close=9.0),
        _bar_row("000003", "2026-06-29", open_price=10.0, close=10.0),
        _bar_row("000003", "2026-06-30", open_price=10.0, close=10.0),
    ]


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


def _intent(security_id: str, side: str) -> OrderIntent:
    return OrderIntent(
        security_id=security_id,
        side=side,
        quantity=100,
        decision_date=date(2026, 6, 29),
        reason="ex010_same_open_round",
    )


def _calendar():
    return trading_calendar_from_dates(
        [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
    )


if __name__ == "__main__":
    unittest.main()
