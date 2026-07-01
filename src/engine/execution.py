from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from src.calendar import TradingCalendar
from src.data import PITDataPortal
from src.engine.dummy_strategy import OrderIntent


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_BAR_ASOF_TIME = time(15, 0, 0)
PRICE_TICK = Decimal("0.01")
PRICE_TOLERANCE = 1e-9
BOARD_LIMIT_RATES = {
    "主板": Decimal("0.10"),
    "创业板": Decimal("0.20"),
    "科创板": Decimal("0.20"),
    "北交所": Decimal("0.30"),
}

FillStatus = Literal["FILLED", "UNFILLED", "REJECTED"]


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

        rejection_reason = self._limit_rejection_reason(order_intent, next_session, open_price)
        if rejection_reason is not None:
            return _rejected(order_intent, next_session, open_price, rejection_reason)

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

    def _limit_rejection_reason(
        self,
        order_intent: OrderIntent,
        execution_date: date,
        open_price: float,
    ) -> str | None:
        limit_context = self._limit_context(order_intent.security_id, order_intent.decision_date, execution_date)
        if limit_context is None or _is_new_listing_window(
            self.calendar,
            limit_context.list_date,
            execution_date,
        ):
            return None

        limit_up, limit_down = _limit_prices(limit_context.previous_close, limit_context.limit_rate)
        if order_intent.side == "buy" and open_price >= limit_up - PRICE_TOLERANCE:
            return "LIMIT_UP_NO_BUY"
        if order_intent.side == "sell" and open_price <= limit_down + PRICE_TOLERANCE:
            return "LIMIT_DOWN_NO_SELL"
        return None

    def _limit_context(
        self,
        security_id: str,
        intent_date: date,
        execution_date: date,
    ) -> "_LimitContext | None":
        previous_close = self._previous_close(security_id, intent_date)
        if previous_close is None:
            return None

        master = self._security_master(security_id, execution_date)
        if master is None:
            return None

        limit_rate = BOARD_LIMIT_RATES.get(master.board)
        if limit_rate is None:
            return None

        return _LimitContext(
            previous_close=previous_close,
            limit_rate=limit_rate,
            list_date=master.list_date,
        )

    def _previous_close(self, security_id: str, intent_date: date) -> float | None:
        rows = self.portal.query(
            "daily_bar_raw",
            _daily_bar_asof(intent_date),
            security_ids=[security_id],
            columns=["security_id", "trade_date", "close"],
        )
        if rows.empty:
            return None

        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        close_rows = rows.loc[trade_dates == intent_date].copy()
        if close_rows.empty:
            return None

        close_value = close_rows.sort_values("security_id").iloc[0]["close"]
        if pd.isna(close_value):
            return None
        return float(close_value)

    def _security_master(self, security_id: str, execution_date: date) -> "_SecurityMasterView | None":
        rows = self.portal.query(
            "security_master",
            _daily_bar_asof(execution_date),
            security_ids=[security_id],
            columns=["security_id", "board", "list_date"],
        )
        if rows.empty:
            return None

        row = rows.sort_values("security_id").iloc[0]
        list_date = pd.to_datetime(row["list_date"], errors="coerce")
        if pd.isna(row["board"]) or pd.isna(list_date):
            return None
        return _SecurityMasterView(board=str(row["board"]), list_date=list_date.date())


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


def _rejected(
    order_intent: OrderIntent,
    execution_date: date,
    execution_price: float,
    reason: str,
) -> FillLedgerEntry:
    return FillLedgerEntry(
        order_intent=order_intent,
        intent_date=order_intent.decision_date,
        execution_date=execution_date,
        execution_price=execution_price,
        filled_quantity=0,
        status="REJECTED",
        reason=reason,
    )


@dataclass(frozen=True)
class _LimitContext:
    previous_close: float
    limit_rate: Decimal
    list_date: date


@dataclass(frozen=True)
class _SecurityMasterView:
    board: str
    list_date: date


def _limit_prices(previous_close: float, limit_rate: Decimal) -> tuple[float, float]:
    close = Decimal(str(previous_close))
    limit_up = (close * (Decimal("1") + limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    limit_down = (close * (Decimal("1") - limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    return float(limit_up), float(limit_down)


def _is_new_listing_window(
    calendar: TradingCalendar,
    list_date: date,
    execution_date: date,
) -> bool:
    if execution_date < list_date:
        return False
    if not calendar.trade_dates or list_date < calendar.trade_dates[0]:
        return False
    return calendar.trading_days_between(list_date, execution_date) <= 5
