from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from src.calendar import trading_calendar_from_dates
from src.data import PITDataPortal
from src.engine import EventDrivenClock


class EventDrivenClockTest(unittest.TestCase):
    def test_clock_advances_once_per_trading_day_from_calendar(self) -> None:
        calendar = _calendar()
        visited: list[date] = []

        clock = EventDrivenClock(
            start_date="2026-06-27",
            end_date="2026-07-01",
            calendar=calendar,
            portal=_portal_with_fixture(),
        )

        clock.run(lambda ctx: visited.append(ctx.trade_date))

        self.assertEqual(visited, calendar.between("2026-06-27", "2026-07-01"))
        self.assertEqual(visited, [date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)])

    def test_callback_portal_cannot_see_future_daily_bar_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "daily_bar_raw.parquet"
            pd.DataFrame(_daily_bar_rows()).to_parquet(table_path, index=False)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})
            calendar = trading_calendar_from_dates([date(2026, 6, 30)])
            clock = EventDrivenClock("2026-06-30", "2026-06-30", calendar, portal)

            def assert_visible_only_through_t(ctx) -> None:
                rows = ctx.portal.query("daily_bar_raw")
                trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
                available_at = pd.to_datetime(rows["available_at"], errors="raise")

                self.assertFalse((trade_dates > ctx.trade_date).any())
                self.assertFalse((available_at > ctx.asof_ts).any())
                self.assertEqual(rows["security_id"].tolist(), ["600519"])
                self.assertFalse(rows["security_id"].eq("000001").any())
                self.assertFalse(rows["security_id"].eq("000002").any())

            clock.run(assert_visible_only_through_t)

    def test_callback_is_called_exactly_once_for_each_trading_day(self) -> None:
        calendar = _calendar()
        calls: dict[date, int] = {}
        clock = EventDrivenClock(
            start_date="2026-06-26",
            end_date="2026-07-01",
            calendar=calendar,
            portal=_portal_with_fixture(),
        )

        def record_call(ctx) -> None:
            calls[ctx.trade_date] = calls.get(ctx.trade_date, 0) + 1

        clock.run(record_call)

        expected_days = calendar.between("2026-06-26", "2026-07-01")
        self.assertEqual(set(calls), set(expected_days))
        self.assertTrue(all(count == 1 for count in calls.values()))
        self.assertEqual(sum(calls.values()), len(expected_days))


def _calendar():
    return trading_calendar_from_dates(
        [
            date(2026, 6, 26),
            date(2026, 6, 29),
            date(2026, 6, 30),
            date(2026, 7, 1),
        ]
    )


def _portal_with_fixture() -> PITDataPortal:
    table_path = Path("data/l1_raw/daily_bar_raw.parquet")
    return PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})


def _daily_bar_rows() -> list[dict[str, object]]:
    return [
        {
            "security_id": "600519",
            "trade_date": "2026-06-30",
            "close": 100.0,
            "event_ts": "2026-06-30T15:00:00+08:00",
            "available_at": "2026-06-30T15:00:00+08:00",
            "snapshot_id": "fixture",
        },
        {
            "security_id": "000001",
            "trade_date": "2026-07-01",
            "close": 999.0,
            "event_ts": "2026-06-30T15:00:00+08:00",
            "available_at": "2026-06-30T15:00:00+08:00",
            "snapshot_id": "fixture",
        },
        {
            "security_id": "000002",
            "trade_date": "2026-06-30",
            "close": 888.0,
            "event_ts": "2026-07-01T15:00:00+08:00",
            "available_at": "2026-07-01T15:00:00+08:00",
            "snapshot_id": "fixture",
        },
    ]


if __name__ == "__main__":
    unittest.main()
