from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_calendar import TradingCalendar
from src.data import PITDataPortal
from src.domain import DataContractError


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE_TIME = time(15, 0, 0)


def _as_date(value: date | datetime | str | pd.Timestamp) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _market_close_asof(trade_date: date) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(trade_date, MARKET_CLOSE_TIME, tzinfo=ASIA_SHANGHAI))


@dataclass(frozen=True)
class AsofDataPortal:
    """Per-bar PIT portal view with asof fixed by the clock."""

    _portal: PITDataPortal
    trade_date: date
    asof_ts: pd.Timestamp

    def query(
        self,
        table: str,
        security_ids: Iterable[str] | None = None,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        requested_columns = None if columns is None else list(dict.fromkeys(columns))
        query_columns = requested_columns
        needs_trade_date_for_filter = table == "daily_bar_raw" and requested_columns is not None
        if needs_trade_date_for_filter and "trade_date" not in requested_columns:
            query_columns = [*requested_columns, "trade_date"]

        rows = self._portal.query(
            table,
            self.asof_ts,
            security_ids=security_ids,
            columns=query_columns,
        )

        if table != "daily_bar_raw":
            return rows

        if "trade_date" not in rows.columns:
            raise DataContractError("daily_bar_raw is missing trade_date; fail-closed")

        visible_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        filtered = rows.loc[visible_dates <= self.trade_date].copy()

        if requested_columns is not None:
            filtered = filtered.loc[:, requested_columns].copy()
        filtered.attrs.update(rows.attrs)
        filtered.attrs["trade_date_predicate"] = f"trade_date <= {self.trade_date.isoformat()}"
        return filtered


@dataclass(frozen=True)
class ClockContext:
    trade_date: date
    asof_ts: pd.Timestamp
    portal: AsofDataPortal


OnBarCallback = Callable[[ClockContext], None]


@dataclass(frozen=True)
class EventDrivenClock:
    start_date: date | datetime | str | pd.Timestamp
    end_date: date | datetime | str | pd.Timestamp
    calendar: TradingCalendar
    portal: PITDataPortal

    def __post_init__(self) -> None:
        start = _as_date(self.start_date)
        end = _as_date(self.end_date)
        if end < start:
            raise ValueError("end_date must be >= start_date")
        object.__setattr__(self, "start_date", start)
        object.__setattr__(self, "end_date", end)

    def run(self, on_bar: OnBarCallback | None = None) -> None:
        callback = on_bar or _noop_on_bar
        for trade_date in self.calendar.between(self.start_date, self.end_date):
            asof_ts = _market_close_asof(trade_date)
            asof_portal = AsofDataPortal(self.portal, trade_date, asof_ts)
            callback(ClockContext(trade_date=trade_date, asof_ts=asof_ts, portal=asof_portal))


def _noop_on_bar(ctx: ClockContext) -> None:
    return None
