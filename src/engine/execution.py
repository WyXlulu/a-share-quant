from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from src.calendar import TradingCalendar
from src.data import PITDataPortal
from src.engine.dummy_strategy import OrderIntent


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_BAR_ASOF_TIME = time(15, 0, 0)

FillStatus = Literal["FILLED", "UNFILLED"]


@dataclass(frozen=True)
class FillLedgerEntry:
    order_intent: OrderIntent
    intent_date: date
    execution_date: date | None
    execution_price: float | None
    filled_quantity: int
    status: FillStatus
    reason: str


@dataclass(frozen=True)
class T1OpenExecutor:
    calendar: TradingCalendar
    portal: PITDataPortal
    end_date: date

    def execute(self, order_intents: list[OrderIntent]) -> list[FillLedgerEntry]:
        return [self.execute_one(order_intent) for order_intent in order_intents]

    def execute_one(self, order_intent: OrderIntent) -> FillLedgerEntry:
        next_session = self._next_session(order_intent.decision_date)
        if next_session is None:
            return _unfilled(order_intent, None, "NO_NEXT_SESSION")

        open_price = self._open_price(order_intent.security_id, next_session)
        if open_price is None:
            return _unfilled(order_intent, next_session, "NO_OPEN_PRICE")

        return FillLedgerEntry(
            order_intent=order_intent,
            intent_date=order_intent.decision_date,
            execution_date=next_session,
            execution_price=open_price,
            filled_quantity=order_intent.quantity,
            status="FILLED",
            reason="T1_OPEN_FILLED",
        )

    def _next_session(self, decision_date: date) -> date | None:
        try:
            next_session = self.calendar.next_trading_day(decision_date)
        except IndexError:
            return None
        if next_session > self.end_date:
            return None
        return next_session

    def _open_price(self, security_id: str, execution_date: date) -> float | None:
        rows = self.portal.query(
            "daily_bar_raw",
            _daily_bar_asof(execution_date),
            security_ids=[security_id],
            columns=["security_id", "trade_date", "open"],
        )
        if rows.empty:
            return None

        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        execution_rows = rows.loc[trade_dates == execution_date].copy()
        if execution_rows.empty:
            return None

        open_value = execution_rows.sort_values("security_id").iloc[0]["open"]
        if pd.isna(open_value):
            return None
        return float(open_value)


def _daily_bar_asof(trade_date: date) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(trade_date, DAILY_BAR_ASOF_TIME, tzinfo=ASIA_SHANGHAI))


def _unfilled(
    order_intent: OrderIntent,
    execution_date: date | None,
    reason: str,
) -> FillLedgerEntry:
    return FillLedgerEntry(
        order_intent=order_intent,
        intent_date=order_intent.decision_date,
        execution_date=execution_date,
        execution_price=None,
        filled_quantity=0,
        status="UNFILLED",
        reason=reason,
    )
