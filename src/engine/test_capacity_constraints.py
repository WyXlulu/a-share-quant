from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import LockedOrder, OrderIntent, T1OpenExecutor
from src.engine.execution import LimitRuleTable
from src.market_calendar import trading_calendar_from_dates


class CapacityConstraintTest(unittest.TestCase):
    def test_ex008_oversized_order_is_capped_at_t_day_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(amounts=[20000.0] * 20))

            locked = executor.lock_order(_intent(quantity=1000), available_cash=Decimal("20000.00"))

            self.assertIsInstance(locked, LockedOrder)
            self.assertEqual(locked.original_quantity, 1000)
            self.assertEqual(locked.locked_quantity, 100)
            self.assertEqual(locked.capacity_reason, "CAPACITY_CAPPED")
            self.assertEqual(locked.adv_window_status, "ADV_FULL_WINDOW")
            self.assertEqual(locked.trailing_adv_notional, Decimal("20000.00"))
            self.assertEqual(locked.max_order_notional, Decimal("1000.00"))

            fill = executor.execute_one(locked)
            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.requested_quantity, 1000)
            self.assertEqual(fill.filled_quantity, 100)
            self.assertEqual(fill.capacity_reason, "CAPACITY_CAPPED")

    def test_ex008_t1_volume_and_amount_changes_do_not_change_locked_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as low_tmpdir:
            low_t1 = _executor(
                low_tmpdir,
                _rows(amounts=[20000.0] * 20, t1_volume=1, t1_amount=1.0),
            ).lock_order(_intent(quantity=1000), available_cash=Decimal("20000.00"))
        with tempfile.TemporaryDirectory() as high_tmpdir:
            high_t1 = _executor(
                high_tmpdir,
                _rows(amounts=[20000.0] * 20, t1_volume=999999999, t1_amount=999999999.0),
            ).lock_order(_intent(quantity=1000), available_cash=Decimal("20000.00"))

        self.assertIsInstance(low_t1, LockedOrder)
        self.assertIsInstance(high_t1, LockedOrder)
        self.assertEqual(low_t1.locked_quantity, high_t1.locked_quantity)
        self.assertEqual(low_t1.capacity_reason, high_t1.capacity_reason)
        self.assertEqual(low_t1.trailing_adv_notional, high_t1.trailing_adv_notional)
        self.assertEqual(low_t1.max_order_notional, high_t1.max_order_notional)

    def test_capacity_sufficient_order_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(amounts=[1000000.0] * 20))

            locked = executor.lock_order(_intent(quantity=300), available_cash=Decimal("10000.00"))

            self.assertIsInstance(locked, LockedOrder)
            self.assertEqual(locked.original_quantity, 300)
            self.assertEqual(locked.locked_quantity, 300)
            self.assertEqual(locked.capacity_reason, "NONE")
            self.assertEqual(locked.adv_window_status, "ADV_FULL_WINDOW")

    def test_partial_adv_window_is_marked_and_uses_available_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(amounts=[20000.0] * 5))

            locked = executor.lock_order(_intent(quantity=100), available_cash=Decimal("2000.00"))

            self.assertIsInstance(locked, LockedOrder)
            self.assertEqual(locked.locked_quantity, 100)
            self.assertEqual(locked.capacity_reason, "NONE")
            self.assertEqual(locked.adv_window_status, "ADV_PARTIAL_WINDOW")
            self.assertEqual(locked.trailing_adv_notional, Decimal("20000.00"))
            self.assertEqual(locked.max_order_notional, Decimal("1000.00"))

    def test_no_adv_data_rejects_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            executor = _executor(tmpdir, _rows(amounts=[None] * 20))

            result = executor.lock_order(_intent(quantity=100), available_cash=Decimal("2000.00"))

            self.assertNotIsInstance(result, LockedOrder)
            self.assertEqual(result.status, "REJECTED")
            self.assertEqual(result.reason, "CAPACITY_NO_ADV_DATA")
            self.assertEqual(result.requested_quantity, 100)
            self.assertIsNone(result.execution_date)


def _executor(tmpdir: str, rows: list[dict[str, object]]) -> T1OpenExecutor:
    daily_path = Path(tmpdir) / "daily_bar_raw.parquet"
    security_master_path = Path(tmpdir) / "security_master.parquet"
    pd.DataFrame(rows).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(security_master_path, index=False)
    portal = PITDataPortal({"daily_bar_raw": daily_path, "security_master": security_master_path})
    return T1OpenExecutor(_calendar(), portal, end_date=_T1)


def _rows(
    *,
    amounts: list[float | None],
    t1_volume: int = 999999,
    t1_amount: float = 50000.0,
) -> list[dict[str, object]]:
    start = _T - timedelta(days=len(amounts) - 1)
    rows = [
        _bar_row(
            _SECURITY_ID,
            start + timedelta(days=offset),
            open_price=10.0,
            close=10.0,
            volume=999999,
            amount=amount,
        )
        for offset, amount in enumerate(amounts)
    ]
    rows.append(
        _bar_row(
            _SECURITY_ID,
            _T1,
            open_price=10.0,
            close=10.0,
            volume=t1_volume,
            amount=t1_amount,
        )
    )
    return rows


def _bar_row(
    security_id: str,
    trade_date: date,
    *,
    open_price: float,
    close: float,
    volume: int,
    amount: float | None,
) -> dict[str, object]:
    trade_date_str = trade_date.isoformat()
    return {
        "security_id": security_id,
        "trade_date": trade_date_str,
        "open": open_price,
        "high": open_price,
        "low": open_price,
        "close": close,
        "volume": volume,
        "amount": amount,
        "trade_status": TradeStatus.NORMAL.value,
        "event_ts": f"{trade_date_str}T15:00:00+08:00",
        "available_at": f"{trade_date_str}T15:00:00+08:00",
        "snapshot_id": "fixture",
    }


def _security_master_rows() -> list[dict[str, object]]:
    board = next(iter(LimitRuleTable().rules_by_board))
    return [
        {
            "security_id": _SECURITY_ID,
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "fixture",
        }
    ]


def _intent(quantity: int) -> OrderIntent:
    return OrderIntent(
        security_id=_SECURITY_ID,
        side="buy",
        quantity=quantity,
        decision_date=_T,
        reason="ex008_capacity_constraint",
    )


def _calendar():
    return trading_calendar_from_dates([_T - timedelta(days=offset) for offset in range(19, -1, -1)] + [_T1])


_SECURITY_ID = "000001"
_T = date(2026, 1, 20)
_T1 = date(2026, 1, 21)


if __name__ == "__main__":
    unittest.main()
