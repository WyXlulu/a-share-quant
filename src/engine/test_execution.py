from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from src.calendar import trading_calendar_from_dates
from src.data import PITDataPortal
from src.engine import FillLedgerEntry, OrderIntent, T1OpenExecutor


class T1OpenExecutorTest(unittest.TestCase):
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


def _intent(security_id: str, decision_date: date, side: str = "buy") -> OrderIntent:
    return OrderIntent(
        security_id=security_id,
        side=side,
        quantity=100,
        decision_date=decision_date,
        reason="test_intent",
    )


def _price_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=50.0, high=120.0, low=40.0, close=99.0),
        _bar_row("000001", "2026-06-30", open_price=10.0, high=20.0, low=5.0, close=15.0),
    ]


def _missing_t1_with_later_price_rows() -> list[dict[str, object]]:
    return [
        _bar_row("000001", "2026-06-29", open_price=50.0, high=120.0, low=40.0, close=99.0),
        _bar_row("000001", "2026-07-01", open_price=123.0, high=130.0, low=120.0, close=125.0),
    ]


def _bar_row(
    security_id: str,
    trade_date: str,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 999999,
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
