from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.calendar import trading_calendar_from_dates
from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import FeeSchedule, FillLedgerEntry, OrderIntent, T1OpenExecutor
from src.engine.execution import FeeScheduleError


class T1OpenExecutorTest(unittest.TestCase):
    def test_buy_fee_has_commission_and_transfer_fee_without_stamp_duty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(tmpdir, _fee_rows())
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(
                _intent("000001", date(2026, 6, 29), side="buy", quantity=100)
            )

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.gross_amount, Decimal("1000.00"))
            self.assertEqual(fill.commission, Decimal("5.00"))
            self.assertEqual(fill.stamp_duty, Decimal("0.00"))
            self.assertEqual(fill.transfer_fee, Decimal("0.01"))
            self.assertEqual(fill.total_fee, Decimal("5.01"))
            self.assertEqual(fill.net_amount, Decimal("1005.01"))

    def test_sell_fee_includes_stamp_duty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(tmpdir, _fee_rows())
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(
                _intent("000001", date(2026, 6, 29), side="sell", quantity=1000)
            )

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.gross_amount, Decimal("10000.00"))
            self.assertEqual(fill.commission, Decimal("5.00"))
            self.assertEqual(fill.transfer_fee, Decimal("0.10"))
            self.assertEqual(fill.stamp_duty, Decimal("5.00"))
            self.assertEqual(fill.total_fee, Decimal("10.10"))
            self.assertEqual(fill.net_amount, Decimal("9989.90"))

    def test_minimum_commission_is_five_yuan_for_small_fill(self) -> None:
        fee = FeeSchedule().calculate(
            side="buy",
            execution_ts=date(2026, 6, 30),
            execution_price=10.0,
            filled_quantity=100,
        )

        self.assertEqual(fee.gross_amount, Decimal("1000.00"))
        self.assertEqual(fee.commission, Decimal("5.00"))

    def test_large_fill_commission_uses_rate_when_above_minimum(self) -> None:
        fee = FeeSchedule().calculate(
            side="buy",
            execution_ts=date(2026, 6, 30),
            execution_price=100.0,
            filled_quantity=100000,
        )

        self.assertEqual(fee.gross_amount, Decimal("10000000.00"))
        self.assertEqual(fee.commission, Decimal("2500.00"))
        self.assertEqual(fee.transfer_fee, Decimal("100.00"))
        self.assertEqual(fee.total_fee, Decimal("2600.00"))

    def test_fee_decimal_rounding_to_cent_without_float_drift(self) -> None:
        fee = FeeSchedule().calculate(
            side="sell",
            execution_ts=date(2026, 6, 30),
            execution_price=3333.33,
            filled_quantity=100,
        )

        self.assertEqual(fee.gross_amount, Decimal("333333.00"))
        self.assertEqual(fee.commission, Decimal("83.33"))
        self.assertEqual(fee.transfer_fee, Decimal("3.33"))
        self.assertEqual(fee.stamp_duty, Decimal("166.67"))
        self.assertEqual(fee.total_fee, Decimal("253.33"))
        self.assertEqual(fee.net_amount, Decimal("333079.67"))

    def test_fee_schedule_resolves_rates_by_effective_date(self) -> None:
        schedule = FeeSchedule()

        before_stamp = schedule.resolve(date(2023, 8, 27))
        before_transfer_cut = schedule.resolve(date(2025, 4, 28))
        after_transfer_cut = schedule.resolve(date(2025, 4, 29))

        self.assertEqual(before_stamp.stamp_duty_rate, Decimal("0.001"))
        self.assertEqual(before_transfer_cut.stamp_duty_rate, Decimal("0.0005"))
        self.assertEqual(before_transfer_cut.transfer_fee_rate, Decimal("0.00002"))
        self.assertEqual(after_transfer_cut.transfer_fee_rate, Decimal("0.00001"))

    def test_fee_schedule_fails_closed_before_known_stamp_duty_rate(self) -> None:
        with self.assertRaisesRegex(FeeScheduleError, "无该日期的已知费率"):
            FeeSchedule().resolve(date(2008, 9, 18))

    def test_t1_suspended_order_is_explicitly_suspended_without_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=10.0, high=10.5, low=9.5, close=10.0),
                    _bar_row(
                        "000001",
                        "2026-06-30",
                        open_price=None,
                        high=None,
                        low=None,
                        close=None,
                        trade_status=TradeStatus.SUSPENDED.value,
                    ),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(_intent("000001", date(2026, 6, 29), side="buy"))

            self.assertEqual(fill.status, "SUSPENDED")
            self.assertEqual(fill.reason, "NO_TRADE_SUSPENDED")
            self.assertEqual(fill.execution_date, date(2026, 6, 30))
            self.assertIsNone(fill.execution_price)
            self.assertEqual(fill.filled_quantity, 0)

    def test_suspended_and_missing_open_have_distinct_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=10.0, high=10.5, low=9.5, close=10.0),
                    _bar_row("000002", "2026-06-29", open_price=10.0, high=10.5, low=9.5, close=10.0),
                    _bar_row(
                        "000001",
                        "2026-06-30",
                        open_price=None,
                        high=None,
                        low=None,
                        close=None,
                        trade_status=TradeStatus.SUSPENDED.value,
                    ),
                    _bar_row(
                        "000002",
                        "2026-06-30",
                        open_price=None,
                        high=10.5,
                        low=9.5,
                        close=10.0,
                        trade_status=TradeStatus.NORMAL.value,
                    ),
                ],
                security_master_rows=[
                    _security_master_row("000001", board="主板", list_date="2020-01-01"),
                    _security_master_row("000002", board="主板", list_date="2020-01-01"),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            suspended_fill, missing_fill = executor.execute(
                [
                    _intent("000001", date(2026, 6, 29), side="buy"),
                    _intent("000002", date(2026, 6, 29), side="buy"),
                ]
            )

            self.assertEqual(suspended_fill.status, "SUSPENDED")
            self.assertEqual(suspended_fill.reason, "NO_TRADE_SUSPENDED")
            self.assertEqual(missing_fill.status, "UNFILLED")
            self.assertEqual(missing_fill.reason, "NO_OPEN_PRICE")

    def test_main_board_limit_up_rejects_buy_without_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=9.8, high=10.5, low=9.5, close=10.0),
                    _bar_row("000001", "2026-06-30", open_price=11.0, high=11.0, low=11.0, close=11.0),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(_intent("000001", date(2026, 6, 29), side="buy"))

            self.assertEqual(fill.status, "REJECTED")
            self.assertEqual(fill.reason, "LIMIT_UP_NO_BUY")
            self.assertEqual(fill.execution_date, date(2026, 6, 30))
            self.assertEqual(fill.execution_price, 11.0)
            self.assertEqual(fill.filled_quantity, 0)

    def test_main_board_limit_down_rejects_sell_without_fill(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=10.2, high=10.5, low=9.8, close=10.0),
                    _bar_row("000001", "2026-06-30", open_price=9.0, high=9.0, low=9.0, close=9.0),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(_intent("000001", date(2026, 6, 29), side="sell"))

            self.assertEqual(fill.status, "REJECTED")
            self.assertEqual(fill.reason, "LIMIT_DOWN_NO_SELL")
            self.assertEqual(fill.execution_date, date(2026, 6, 30))
            self.assertEqual(fill.execution_price, 9.0)
            self.assertEqual(fill.filled_quantity, 0)

    def test_fills_at_t1_open_not_t_close_or_other_t1_prices(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(tmpdir, _price_rows())
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))
            intent = _intent("000001", date(2026, 6, 29))

            fill = executor.execute([intent])[0]

            self.assertIsInstance(fill, FillLedgerEntry)
            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.intent_date, date(2026, 6, 29))
            self.assertEqual(fill.execution_date, date(2026, 6, 30))
            self.assertEqual(fill.execution_price, 10.0)
            self.assertNotEqual(fill.execution_price, 99.0)
            self.assertNotEqual(fill.execution_price, 20.0)
            self.assertNotEqual(fill.execution_price, 15.0)
            self.assertEqual(fill.filled_quantity, 100)

    def test_science_tech_board_uses_20pct_while_main_board_uses_10pct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("688001", "2026-06-29", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("000001", "2026-06-30", open_price=11.5, high=11.5, low=11.5, close=11.5),
                    _bar_row("688001", "2026-06-30", open_price=11.5, high=11.5, low=11.5, close=11.5),
                ],
                security_master_rows=[
                    _security_master_row("000001", board="主板", list_date="2020-01-01"),
                    _security_master_row("688001", board="科创板", list_date="2020-01-01"),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            main_fill, star_fill = executor.execute(
                [
                    _intent("000001", date(2026, 6, 29), side="buy"),
                    _intent("688001", date(2026, 6, 29), side="buy"),
                ]
            )

            self.assertEqual(main_fill.status, "REJECTED")
            self.assertEqual(main_fill.reason, "LIMIT_UP_NO_BUY")
            self.assertEqual(star_fill.status, "FILLED")
            self.assertEqual(star_fill.execution_price, 11.5)

    def test_chinext_uses_10pct_limit_before_2020_registration_reform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("300001", "2019-01-02", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("300001", "2019-01-03", open_price=11.0, high=11.0, low=11.0, close=11.0),
                ],
                security_master_rows=[
                    _security_master_row("300001", board="创业板", list_date="2018-01-01"),
                ],
            )
            calendar = trading_calendar_from_dates([date(2019, 1, 2), date(2019, 1, 3)])
            executor = T1OpenExecutor(calendar, portal, end_date=date(2019, 1, 3))

            fill = executor.execute_one(_intent("300001", date(2019, 1, 2), side="buy"))

            self.assertEqual(fill.status, "REJECTED")
            self.assertEqual(fill.reason, "LIMIT_UP_NO_BUY")
            self.assertEqual(fill.execution_price, 11.0)

    def test_chinext_uses_20pct_limit_after_2020_registration_reform(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("300001", "2021-01-04", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("300001", "2021-01-05", open_price=11.5, high=11.5, low=11.5, close=11.5),
                ],
                security_master_rows=[
                    _security_master_row("300001", board="创业板", list_date="2018-01-01"),
                ],
            )
            calendar = trading_calendar_from_dates([date(2021, 1, 4), date(2021, 1, 5)])
            executor = T1OpenExecutor(calendar, portal, end_date=date(2021, 1, 5))

            fill = executor.execute_one(_intent("300001", date(2021, 1, 4), side="buy"))

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.execution_price, 11.5)

    def test_2022_main_board_new_listing_uses_first_day_44_36_then_10pct(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2021-12-31", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("000002", "2021-12-31", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("000001", "2022-01-03", open_price=14.4, high=14.4, low=14.4, close=10.0),
                    _bar_row("000002", "2022-01-03", open_price=6.4, high=6.4, low=6.4, close=10.0),
                    _bar_row("000001", "2022-01-04", open_price=11.0, high=11.0, low=11.0, close=11.0),
                ],
                security_master_rows=[
                    _security_master_row("000001", board="主板", list_date="2022-01-03"),
                    _security_master_row("000002", board="主板", list_date="2022-01-03"),
                ],
            )
            calendar = trading_calendar_from_dates(
                [date(2021, 12, 31), date(2022, 1, 3), date(2022, 1, 4)]
            )
            executor = T1OpenExecutor(calendar, portal, end_date=date(2022, 1, 4))

            first_day_up = executor.execute_one(_intent("000001", date(2021, 12, 31), side="buy"))
            first_day_down = executor.execute_one(_intent("000002", date(2021, 12, 31), side="sell"))
            second_day_up = executor.execute_one(_intent("000001", date(2022, 1, 3), side="buy"))

            self.assertEqual(first_day_up.status, "REJECTED")
            self.assertEqual(first_day_up.reason, "LIMIT_UP_NO_BUY")
            self.assertEqual(first_day_up.execution_price, 14.4)
            self.assertEqual(first_day_down.status, "REJECTED")
            self.assertEqual(first_day_down.reason, "LIMIT_DOWN_NO_SELL")
            self.assertEqual(first_day_down.execution_price, 6.4)
            self.assertEqual(second_day_up.status, "REJECTED")
            self.assertEqual(second_day_up.reason, "LIMIT_UP_NO_BUY")
            self.assertEqual(second_day_up.execution_price, 11.0)

    def test_2024_main_board_first_five_listing_sessions_have_no_price_limit_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2024-02-29", open_price=10.0, high=10.0, low=10.0, close=10.0),
                    _bar_row("000001", "2024-03-01", open_price=20.0, high=20.0, low=20.0, close=20.0),
                ],
                security_master_rows=[
                    _security_master_row("000001", board="主板", list_date="2024-03-01"),
                ],
            )
            calendar = trading_calendar_from_dates([date(2024, 2, 29), date(2024, 3, 1)])
            executor = T1OpenExecutor(calendar, portal, end_date=date(2024, 3, 1))

            fill = executor.execute_one(_intent("000001", date(2024, 2, 29), side="buy"))

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.reason, "T1_OPEN_FILLED")
            self.assertEqual(fill.execution_price, 20.0)

    def test_first_five_listing_sessions_have_no_price_limit_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(
                tmpdir,
                [
                    _bar_row("000001", "2026-06-29", open_price=9.8, high=10.5, low=9.5, close=10.0),
                    _bar_row("000001", "2026-06-30", open_price=11.0, high=11.0, low=11.0, close=11.0),
                ],
                security_master_rows=[
                    _security_master_row("000001", board="主板", list_date="2026-06-29"),
                ],
            )
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))

            fill = executor.execute_one(_intent("000001", date(2026, 6, 29), side="buy"))

            self.assertEqual(fill.status, "FILLED")
            self.assertEqual(fill.reason, "T1_OPEN_FILLED")
            self.assertEqual(fill.execution_price, 11.0)

    def test_never_fills_on_intent_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(tmpdir, _price_rows())
            executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))
            intent = _intent("000001", date(2026, 6, 29))

            fill = executor.execute_one(intent)

            self.assertNotEqual(fill.execution_date, intent.decision_date)
            self.assertEqual(fill.execution_date, date(2026, 6, 30))

    def test_missing_t1_open_does_not_steal_later_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _portal(tmpdir, _missing_t1_with_later_price_rows())
            calendar = trading_calendar_from_dates(
                [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
            )
            executor = T1OpenExecutor(calendar, portal, end_date=date(2026, 7, 1))
            intent = _intent("000001", date(2026, 6, 29))

            fill = executor.execute_one(intent)

            self.assertEqual(fill.status, "UNFILLED")
            self.assertEqual(fill.reason, "NO_OPEN_PRICE")
            self.assertEqual(fill.execution_date, date(2026, 6, 30))
            self.assertIsNone(fill.execution_price)
            self.assertEqual(fill.filled_quantity, 0)

    def test_reproducible_fill_ledger_for_same_inputs(self) -> None:
        first = _run_executor()
        second = _run_executor()

        self.assertEqual(first, second)


def _run_executor() -> list[FillLedgerEntry]:
    with tempfile.TemporaryDirectory() as tmpdir:
        portal = _portal(tmpdir, _price_rows())
        executor = T1OpenExecutor(_calendar(), portal, end_date=date(2026, 6, 30))
        return executor.execute([_intent("000001", date(2026, 6, 29))])


def _calendar():
    return trading_calendar_from_dates([date(2026, 6, 29), date(2026, 6, 30)])


def _portal(
    tmpdir: str,
    rows: list[dict[str, object]],
    security_master_rows: list[dict[str, object]] | None = None,
) -> PITDataPortal:
    daily_path = Path(tmpdir) / "daily_bar_raw.parquet"
    security_master_path = Path(tmpdir) / "security_master.parquet"
    pd.DataFrame(rows).to_parquet(daily_path, index=False)
    pd.DataFrame(security_master_rows or _default_security_master_rows()).to_parquet(
        security_master_path,
        index=False,
    )
    return PITDataPortal({"daily_bar_raw": daily_path, "security_master": security_master_path})


def _intent(
    security_id: str,
    decision_date: date,
    side: str = "buy",
    quantity: int = 100,
) -> OrderIntent:
    return OrderIntent(
        security_id=security_id,
        side=side,
        quantity=quantity,
        decision_date=decision_date,
        reason="test_intent",
    )


def _price_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=50.0, high=120.0, low=40.0, close=99.0),
        _bar_row("000001", "2026-06-30", open_price=10.0, high=20.0, low=5.0, close=15.0),
    ]


def _fee_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=10.0, high=10.0, low=10.0, close=10.0),
        _bar_row("000001", "2026-06-30", open_price=10.0, high=10.0, low=10.0, close=10.0),
    ]


def _missing_t1_with_later_price_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=50.0, high=120.0, low=40.0, close=99.0),
        _bar_row("000001", "2026-07-01", open_price=123.0, high=130.0, low=120.0, close=125.0),
    ]


def _bar_row(
    security_id: str,
    trade_date: str,
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
    trade_status: str = TradeStatus.NORMAL.value,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 999999,
        "trade_status": trade_status,
        "event_ts": f"{trade_date}T15:00:00+08:00",
        "available_at": f"{trade_date}T15:00:00+08:00",
        "snapshot_id": "fixture",
    }


def _default_security_master_rows() -> list[dict[str, object]]:
    return [_security_master_row("000001", board="主板", list_date="2020-01-01")]


def _security_master_row(security_id: str, board: str, list_date: str) -> dict[str, object]:
    return {
        "security_id": security_id,
        "board": board,
        "list_date": list_date,
        "available_at": f"{list_date}T15:00:00+08:00",
        "snapshot_id": "fixture",
    }


if __name__ == "__main__":
    unittest.main()
