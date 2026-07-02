from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import LockedOrder, OrderIntent, T1OpenExecutor
from src.engine.execution import FeeSchedule, FillLedgerEntry, LimitRuleTable
from src.engine.portfolio_ledger import CashState, PortfolioLedger
from src.market_calendar import trading_calendar_from_dates


class OrderLockingTest(unittest.TestCase):
    def test_lt011_locked_order_fields_are_unchanged_when_t1_open_changes(self) -> None:
        low_open = _lock_and_execute(t1_open=10.5)
        high_open = _lock_and_execute(t1_open=11.0)

        self.assertEqual(_locked_snapshot(low_open.locked_orders), _locked_snapshot(high_open.locked_orders))
        self.assertEqual([order.order_intent.security_id for order in low_open.locked_orders], ["000001"])
        self.assertEqual(low_open.fills[0].status, "FILLED")
        self.assertEqual(low_open.fills[0].execution_price, 10.5)
        self.assertEqual(high_open.fills[0].status, "REJECTED")
        self.assertEqual(high_open.fills[0].reason, "LIMIT_UP_NO_BUY")
        self.assertEqual(high_open.fills[0].execution_price, 11.0)

    def test_reserved_cash_covers_theoretical_limit_up_worst_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(t1_open=11.0))
            locked = executor.lock_order(_buy_intent(), available_cash=Decimal("2000.00"))

            self.assertIsInstance(locked, LockedOrder)
            worst_case = FeeSchedule().calculate("buy", date(2026, 6, 30), 11.0, 100)
            self.assertEqual(locked.price_cap, Decimal("11.00"))
            self.assertEqual(locked.reserved_cash, worst_case.net_amount)
            self.assertGreaterEqual(locked.reserved_cash, worst_case.net_amount)

    def test_filled_buy_releases_reservation_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(t1_open=10.0))
            locked = executor.lock_order(_buy_intent(), available_cash=Decimal("2000.00"))
            self.assertIsInstance(locked, LockedOrder)
            ledger = PortfolioLedger(
                CashState(settled_cash=Decimal("2000.00"), available_cash=Decimal("2000.00")),
                calendar=_calendar(),
            )

            ledger.reserve_cash_for_buy(locked)
            fill = executor.execute_one(locked)
            ledger.apply_execution_result(fill)

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(ledger.cash.frozen_cash, Decimal("0.00"))
            self.assertEqual(ledger.cash.available_cash, Decimal("994.99"))
            self.assertEqual(ledger.positions["000001"].cost_basis, Decimal("1005.01"))

    def test_non_filled_buy_outcomes_release_full_reservation(self) -> None:
        cases = [
            ("REJECTED", _rows(t1_open=11.0), "LIMIT_UP_NO_BUY"),
            (
                "SUSPENDED",
                _rows(t1_open=None, t1_trade_status=TradeStatus.SUSPENDED.value),
                "NO_TRADE_SUSPENDED",
            ),
            ("UNFILLED", _rows(t1_open=10.0, include_t1=False), "NO_OPEN_PRICE"),
        ]
        for expected_status, rows, expected_reason in cases:
            with self.subTest(expected_status=expected_status):
                with tempfile.TemporaryDirectory() as tmpdir:
                    executor = _executor(tmpdir, rows)
                    locked = executor.lock_order(_buy_intent(), available_cash=Decimal("2000.00"))
                    self.assertIsInstance(locked, LockedOrder)
                    ledger = PortfolioLedger(
                        CashState(
                            settled_cash=Decimal("2000.00"),
                            available_cash=Decimal("2000.00"),
                        ),
                        calendar=_calendar(),
                    )

                    ledger.reserve_cash_for_buy(locked)
                    fill = executor.execute_one(locked)
                    ledger.apply_execution_result(fill)

                    self.assertEqual(fill.status, expected_status)
                    self.assertEqual(fill.reason, expected_reason)
                    self.assertEqual(ledger.cash.frozen_cash, Decimal("0.00"))
                    self.assertEqual(ledger.cash.available_cash, Decimal("2000.00"))
                    self.assertEqual(ledger.positions, {})

    def test_cash_insufficient_rejects_at_t_day_locking_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(t1_open=10.0))

            result = executor.lock_order(_buy_intent(), available_cash=Decimal("100.00"))

            self.assertIsInstance(result, FillLedgerEntry)
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(result.reason, "CASH_INSUFFICIENT")
            self.assertIsNone(result.execution_date)
            self.assertEqual(result.filled_quantity, 0)


class _RunResult:
    def __init__(self, locked_orders: list[LockedOrder], fills: list[FillLedgerEntry]) -> None:
        self.locked_orders = locked_orders
        self.fills = fills


def _lock_and_execute(t1_open: float) -> _RunResult:
    with tempfile.TemporaryDirectory() as tmpdir:
        executor = _executor(tmpdir, _rows(t1_open=t1_open))
        lock_result = executor.lock_order(_buy_intent(), available_cash=Decimal("2000.00"))
        if not isinstance(lock_result, LockedOrder):
            raise AssertionError(f"expected LockedOrder, got {lock_result}")
        fill = executor.execute_one(lock_result)
        return _RunResult([lock_result], [fill])


def _locked_snapshot(locked_orders: list[LockedOrder]) -> list[tuple[object, ...]]:
    return [
        (
            order.order_intent.security_id,
            order.locked_quantity,
            order.reference_price,
            order.price_cap,
            order.price_floor,
            order.reserved_cash,
            order.reference_price_ts,
            order.ruleset_version,
            order.ttl,
        )
        for order in locked_orders
    ]


def _buy_intent() -> OrderIntent:
    return OrderIntent(
        security_id="000001",
        side="buy",
        quantity=100,
        decision_date=date(2026, 6, 29),
        reason="lt011_fixture",
    )


def _executor(tmpdir: str, rows: list[dict[str, object]]) -> T1OpenExecutor:
    daily_path = Path(tmpdir) / "daily_bar_raw.parquet"
    security_master_path = Path(tmpdir) / "security_master.parquet"
    pd.DataFrame(rows).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(security_master_path, index=False)
    portal = PITDataPortal({"daily_bar_raw": daily_path, "security_master": security_master_path})
    return T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))


def _rows(
    *,
    t1_open: float | None,
    t1_trade_status: str = TradeStatus.NORMAL.value,
    include_t1: bool = True,
) -> list[dict[str, object]]:
    rows = [_bar_row("2026-06-29", open_price=10.0, close=10.0)]
    if include_t1:
        rows.append(
            _bar_row(
                "2026-06-30",
                open_price=t1_open,
                close=t1_open,
                trade_status=t1_trade_status,
            )
        )
    return rows


def _bar_row(
    trade_date: str,
    *,
    open_price: float | None,
    close: float | None,
    trade_status: str = TradeStatus.NORMAL.value,
) -> dict[str, object]:
    return {
        "security_id": "000001",
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
            "security_id": "000001",
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "fixture",
        }
    ]


def _calendar():
    return trading_calendar_from_dates(
        [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
    )


if __name__ == "__main__":
    unittest.main()
