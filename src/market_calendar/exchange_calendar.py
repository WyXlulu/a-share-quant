from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd


def _as_date(value: date | datetime | str | pd.Timestamp) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


@dataclass(frozen=True)
class TradingCalendar:
    trade_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        unique_sorted = tuple(sorted(set(self.trade_dates)))
        object.__setattr__(self, "trade_dates", unique_sorted)

    def is_trading_day(self, day: date | datetime | str | pd.Timestamp) -> bool:
        target = _as_date(day)
        index = bisect_left(self.trade_dates, target)
        return index < len(self.trade_dates) and self.trade_dates[index] == target

    def previous_trading_day(
        self, day: date | datetime | str | pd.Timestamp, n: int = 1
    ) -> date:
        if n < 1:
            raise ValueError("n must be >= 1")
        target = _as_date(day)
        index = bisect_left(self.trade_dates, target) - n
        if index < 0:
            raise IndexError(f"No previous trading day for {target} with n={n}")
        return self.trade_dates[index]

    def next_trading_day(self, day: date | datetime | str | pd.Timestamp, n: int = 1) -> date:
        if n < 1:
            raise ValueError("n must be >= 1")
        target = _as_date(day)
        index = bisect_right(self.trade_dates, target) + n - 1
        if index >= len(self.trade_dates):
            raise IndexError(f"No next trading day for {target} with n={n}")
        return self.trade_dates[index]

    def trading_days_between(
        self,
        start: date | datetime | str | pd.Timestamp,
        end: date | datetime | str | pd.Timestamp,
        inclusive: bool = True,
    ) -> int:
        start_date = _as_date(start)
        end_date = _as_date(end)
        if end_date < start_date:
            raise ValueError("end must be >= start")

        left = bisect_left(self.trade_dates, start_date)
        right = bisect_right(self.trade_dates, end_date)
        count = right - left
        if not inclusive:
            count -= int(self.is_trading_day(start_date))
            count -= int(self.is_trading_day(end_date))
        return max(count, 0)

    def between(
        self,
        start: date | datetime | str | pd.Timestamp,
        end: date | datetime | str | pd.Timestamp,
    ) -> list[date]:
        start_date = _as_date(start)
        end_date = _as_date(end)
        left = bisect_left(self.trade_dates, start_date)
        right = bisect_right(self.trade_dates, end_date)
        return list(self.trade_dates[left:right])


def trading_calendar_from_dates(
    trade_dates: list[date] | tuple[date, ...] | pd.Series,
) -> TradingCalendar:
    trade_dates = pd.to_datetime(pd.Series(trade_dates), errors="raise").dt.date
    return TradingCalendar(tuple(trade_dates))
