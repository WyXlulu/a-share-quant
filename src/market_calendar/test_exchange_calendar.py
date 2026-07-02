from __future__ import annotations

import unittest

from src.market_calendar import trading_calendar_from_dates

try:
    from src.data.akshare_adapter import fetch_exchange_trade_dates
except ModuleNotFoundError as exc:
    if exc.name != "akshare":
        raise
    fetch_exchange_trade_dates = None


class TradingCalendarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if fetch_exchange_trade_dates is None:
            raise unittest.SkipTest("akshare未安装")
        cls.calendar = trading_calendar_from_dates(fetch_exchange_trade_dates())

    def test_known_holidays_are_not_trading_days(self) -> None:
        self.assertFalse(self.calendar.is_trading_day("2024-10-01"))
        self.assertFalse(self.calendar.is_trading_day("2024-10-04"))
        self.assertFalse(self.calendar.is_trading_day("2024-02-09"))

    def test_known_sessions_are_trading_days(self) -> None:
        self.assertTrue(self.calendar.is_trading_day("2024-09-30"))
        self.assertTrue(self.calendar.is_trading_day("2024-10-08"))
        self.assertTrue(self.calendar.is_trading_day("2024-02-08"))
        self.assertTrue(self.calendar.is_trading_day("2024-02-19"))

    def test_neighbor_and_count_helpers(self) -> None:
        self.assertEqual(self.calendar.previous_trading_day("2024-10-08").isoformat(), "2024-09-30")
        self.assertEqual(self.calendar.next_trading_day("2024-09-30").isoformat(), "2024-10-08")
        self.assertEqual(self.calendar.trading_days_between("2024-09-30", "2024-10-08"), 2)


if __name__ == "__main__":
    unittest.main()
