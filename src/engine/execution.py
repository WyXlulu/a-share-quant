from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_calendar import TradingCalendar
from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine.dummy_strategy import OrderIntent


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_BAR_ASOF_TIME = time(15, 0, 0)
PRICE_TICK = Decimal("0.01")
PRICE_TOLERANCE = 1e-9
MONEY_QUANT = Decimal("0.01")
FillStatus = Literal["FILLED", "UNFILLED", "REJECTED", "SUSPENDED"]
LimitCheck = Literal[
    "NOT_EVALUATED",
    "APPLIED",
    "SKIPPED_NO_PREV_CLOSE",
    "SKIPPED_NO_MASTER",
    "SKIPPED_NO_RULE",
    "EXEMPT_NEW_LISTING",
]
LotSizePolicy = Literal["ROUND_DOWN", "REJECT"]
NewListingLimitPolicy = Literal[
    "DAILY_LIMIT_FROM_LISTING",
    "FIRST_DAY_ASYMMETRIC",
    "FIRST_DAY_NO_LIMIT",
    "FIRST_FIVE_NO_LIMIT",
]


class FeeScheduleError(ValueError):
    """Raised when a requested fee date is outside known fee rules."""


@dataclass(frozen=True)
class FillLedgerEntry:
    order_intent: OrderIntent
    intent_date: date
    execution_date: date | None
    execution_price: float | None
    filled_quantity: int
    status: FillStatus
    reason: str
    requested_quantity: int = 0
    limit_check: LimitCheck = "NOT_EVALUATED"
    gross_amount: Decimal = Decimal("0.00")
    commission: Decimal = Decimal("0.00")
    stamp_duty: Decimal = Decimal("0.00")
    transfer_fee: Decimal = Decimal("0.00")
    total_fee: Decimal = Decimal("0.00")
    net_amount: Decimal = Decimal("0.00")


@dataclass(frozen=True)
class EffectiveRate:
    effective_date: date
    rate: Decimal


@dataclass(frozen=True)
class ResolvedFeeRates:
    commission_rate: Decimal
    commission_minimum: Decimal
    stamp_duty_rate: Decimal
    transfer_fee_rate: Decimal


@dataclass(frozen=True)
class FeeBreakdown:
    gross_amount: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    total_fee: Decimal
    net_amount: Decimal


@dataclass(frozen=True)
class PriceLimitRule:
    effective_date: date
    daily_limit_rate: Decimal
    new_listing_policy: NewListingLimitPolicy = "DAILY_LIMIT_FROM_LISTING"
    first_day_limit_up_rate: Decimal | None = None
    first_day_limit_down_rate: Decimal | None = None


@dataclass(frozen=True)
class LimitRuleTable:
    rules_by_board: dict[str, tuple[PriceLimitRule, ...]] = field(
        default_factory=lambda: {
            "主板": (
                PriceLimitRule(
                    effective_date=date(2015, 1, 1),
                    daily_limit_rate=Decimal("0.10"),
                    new_listing_policy="FIRST_DAY_ASYMMETRIC",
                    first_day_limit_up_rate=Decimal("0.44"),
                    first_day_limit_down_rate=Decimal("0.36"),
                ),
                PriceLimitRule(
                    effective_date=date(2023, 2, 17),
                    daily_limit_rate=Decimal("0.10"),
                    new_listing_policy="FIRST_FIVE_NO_LIMIT",
                ),
            ),
            "创业板": (
                PriceLimitRule(
                    effective_date=date(2015, 1, 1),
                    daily_limit_rate=Decimal("0.10"),
                ),
                PriceLimitRule(
                    effective_date=date(2020, 8, 24),
                    daily_limit_rate=Decimal("0.20"),
                    new_listing_policy="FIRST_FIVE_NO_LIMIT",
                ),
            ),
            "科创板": (
                PriceLimitRule(
                    effective_date=date(2019, 7, 22),
                    daily_limit_rate=Decimal("0.20"),
                    new_listing_policy="FIRST_FIVE_NO_LIMIT",
                ),
            ),
            "北交所": (
                PriceLimitRule(
                    effective_date=date(2021, 11, 15),
                    daily_limit_rate=Decimal("0.30"),
                    new_listing_policy="FIRST_DAY_NO_LIMIT",
                ),
            ),
        }
    )

    def resolve(self, board: str, execution_date: date) -> PriceLimitRule | None:
        rules = self.rules_by_board.get(board)
        if not rules:
            return None
        active_rules = [rule for rule in rules if rule.effective_date <= execution_date]
        if not active_rules:
            return None
        return sorted(active_rules, key=lambda rule: rule.effective_date)[-1]


@dataclass(frozen=True)
class FeeSchedule:
    # Broker commission is configurable; default is a common retail tier, not an exchange rule.
    commission_rate: Decimal = Decimal("0.00025")
    commission_minimum: Decimal = Decimal("5.00")
    # Stamp duty is sell-side only since 2008-09-19: 0.1%, cut to 0.05% on 2023-08-28.
    stamp_duty_rates: tuple[EffectiveRate, ...] = (
        EffectiveRate(date(2008, 9, 19), Decimal("0.001")),
        EffectiveRate(date(2023, 8, 28), Decimal("0.0005")),
    )
    # Transfer fee: 0.002% before 2025-04-29, 0.001% from 2025-04-29.
    transfer_fee_rates: tuple[EffectiveRate, ...] = (
        EffectiveRate(date(1900, 1, 1), Decimal("0.00002")),
        EffectiveRate(date(2025, 4, 29), Decimal("0.00001")),
    )

    def resolve(self, execution_ts: date | datetime | pd.Timestamp) -> ResolvedFeeRates:
        execution_date = _as_date(execution_ts)
        if self.commission_rate <= Decimal("0"):
            raise FeeScheduleError("佣金费率必须为正，不能静默返回0")
        if self.commission_minimum < Decimal("0"):
            raise FeeScheduleError("佣金最低收费不能为负")
        return ResolvedFeeRates(
            commission_rate=self.commission_rate,
            commission_minimum=_money(self.commission_minimum),
            stamp_duty_rate=_resolve_rate(self.stamp_duty_rates, execution_date, "印花税"),
            transfer_fee_rate=_resolve_rate(self.transfer_fee_rates, execution_date, "过户费"),
        )

    def calculate(
        self,
        side: str,
        execution_ts: date | datetime | pd.Timestamp,
        execution_price: float,
        filled_quantity: int,
    ) -> FeeBreakdown:
        rates = self.resolve(execution_ts)
        gross_amount = _money(Decimal(str(execution_price)) * Decimal(filled_quantity))
        commission = _money(
            max(gross_amount * rates.commission_rate, rates.commission_minimum)
        )
        transfer_fee = _money(gross_amount * rates.transfer_fee_rate)
        stamp_duty = (
            _money(gross_amount * rates.stamp_duty_rate)
            if side == "sell"
            else Decimal("0.00")
        )
        total_fee = _money(commission + transfer_fee + stamp_duty)
        net_amount = (
            _money(gross_amount - total_fee)
            if side == "sell"
            else _money(gross_amount + total_fee)
        )
        return FeeBreakdown(
            gross_amount=gross_amount,
            commission=commission,
            stamp_duty=stamp_duty,
            transfer_fee=transfer_fee,
            total_fee=total_fee,
            net_amount=net_amount,
        )


@dataclass(frozen=True)
class T1OpenExecutor:
    calendar: TradingCalendar
    portal: PITDataPortal
    end_date: date
    fee_schedule: FeeSchedule = field(default_factory=FeeSchedule)
    limit_rule_table: LimitRuleTable = field(default_factory=LimitRuleTable)
    lot_size_policy: LotSizePolicy = "ROUND_DOWN"

    def execute(self, order_intents: list[OrderIntent]) -> list[FillLedgerEntry]:
        return [self.execute_one(order_intent) for order_intent in order_intents]

    def execute_one(self, order_intent: OrderIntent) -> FillLedgerEntry:
        next_session = self._next_session(order_intent.decision_date)
        if next_session is None:
            return _unfilled(order_intent, None, "NO_NEXT_SESSION")

        adjusted_quantity = self._buy_lot_quantity(order_intent)
        if adjusted_quantity is None:
            return _rejected(
                order_intent,
                next_session,
                None,
                "ODD_LOT_REJECTED",
                requested_quantity=order_intent.quantity,
            )

        execution_bar = self._execution_bar(order_intent.security_id, next_session)
        if execution_bar is None:
            return _unfilled(order_intent, next_session, "NO_OPEN_PRICE")
        if execution_bar.trade_status == TradeStatus.SUSPENDED.value:
            return _suspended(order_intent, next_session)
        if execution_bar.open_price is None:
            return _unfilled(order_intent, next_session, "NO_OPEN_PRICE")

        limit_evaluation = self._limit_evaluation(order_intent, next_session, execution_bar.open_price)
        if limit_evaluation.rejection_reason is not None:
            return _rejected(
                order_intent,
                next_session,
                execution_bar.open_price,
                limit_evaluation.rejection_reason,
                requested_quantity=order_intent.quantity,
                limit_check=limit_evaluation.limit_check,
            )

        fees = self.fee_schedule.calculate(
            order_intent.side,
            next_session,
            execution_bar.open_price,
            adjusted_quantity,
        )
        return FillLedgerEntry(
            order_intent=order_intent,
            intent_date=order_intent.decision_date,
            execution_date=next_session,
            execution_price=execution_bar.open_price,
            filled_quantity=adjusted_quantity,
            status="FILLED",
            reason="T1_OPEN_FILLED",
            requested_quantity=order_intent.quantity,
            limit_check=limit_evaluation.limit_check,
            gross_amount=fees.gross_amount,
            commission=fees.commission,
            stamp_duty=fees.stamp_duty,
            transfer_fee=fees.transfer_fee,
            total_fee=fees.total_fee,
            net_amount=fees.net_amount,
        )

    def _next_session(self, decision_date: date) -> date | None:
        try:
            next_session = self.calendar.next_trading_day(decision_date)
        except IndexError:
            return None
        if next_session > self.end_date:
            return None
        return next_session

    def _buy_lot_quantity(self, order_intent: OrderIntent) -> int | None:
        if order_intent.side != "buy":
            # A-share odd-lot sell rules depend on current holdings; enforce them in the ledger step.
            return order_intent.quantity
        if order_intent.quantity % 100 == 0:
            return order_intent.quantity
        if self.lot_size_policy == "REJECT":
            return None
        rounded_quantity = (order_intent.quantity // 100) * 100
        return rounded_quantity if rounded_quantity > 0 else None

    def _execution_bar(self, security_id: str, execution_date: date) -> "_ExecutionBar | None":
        rows = self.portal.query(
            "daily_bar_raw",
            _daily_bar_asof(execution_date),
            security_ids=[security_id],
            columns=["security_id", "trade_date", "open", "trade_status"],
        )
        if rows.empty:
            return None

        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        execution_rows = rows.loc[trade_dates == execution_date].copy()
        if execution_rows.empty:
            return None

        open_value = execution_rows.sort_values("security_id").iloc[0]["open"]
        trade_status = str(execution_rows.sort_values("security_id").iloc[0]["trade_status"])
        # A present T+1 row with trade_status=停牌 is a market-rule no-trade case, even when
        # open is missing. A present non-suspended row with open missing is data absence.
        open_price = None if pd.isna(open_value) else float(open_value)
        return _ExecutionBar(open_price=open_price, trade_status=trade_status)

    def _limit_evaluation(
        self,
        order_intent: OrderIntent,
        execution_date: date,
        open_price: float,
    ) -> "_LimitEvaluation":
        previous_close = self._previous_close(order_intent.security_id, order_intent.decision_date)
        if previous_close is None:
            return _LimitEvaluation(rejection_reason=None, limit_check="SKIPPED_NO_PREV_CLOSE")

        master = self._security_master(order_intent.security_id, execution_date)
        if master is None:
            return _LimitEvaluation(rejection_reason=None, limit_check="SKIPPED_NO_MASTER")

        limit_rule = self.limit_rule_table.resolve(master.board, execution_date)
        if limit_rule is None:
            return _LimitEvaluation(rejection_reason=None, limit_check="SKIPPED_NO_RULE")

        # TODO(DECISIONS.md): ex-date limit reference prices need corporate-action adjusted
        # reference-price handling; current implementation still uses previous raw close.
        limit_prices = _limit_prices_for_rule(
            self.calendar,
            previous_close,
            limit_rule,
            master.list_date,
            execution_date,
        )
        if limit_prices is None:
            return _LimitEvaluation(rejection_reason=None, limit_check="EXEMPT_NEW_LISTING")

        limit_up, limit_down = limit_prices
        if order_intent.side == "buy" and open_price >= limit_up - PRICE_TOLERANCE:
            return _LimitEvaluation(rejection_reason="LIMIT_UP_NO_BUY", limit_check="APPLIED")
        if order_intent.side == "sell" and open_price <= limit_down + PRICE_TOLERANCE:
            return _LimitEvaluation(rejection_reason="LIMIT_DOWN_NO_SELL", limit_check="APPLIED")
        return _LimitEvaluation(rejection_reason=None, limit_check="APPLIED")

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
        lookback_dates = [trade_date for trade_date in self.calendar.trade_dates if trade_date <= intent_date][-60:]
        close_rows = rows.loc[trade_dates.isin(lookback_dates)].copy()
        close_rows = close_rows.loc[close_rows["close"].notna()].copy()
        if close_rows.empty:
            return None

        close_rows["_trade_date"] = pd.to_datetime(close_rows["trade_date"], errors="raise")
        close_value = close_rows.sort_values("_trade_date", ascending=False).iloc[0]["close"]
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


def _as_date(value: date | datetime | pd.Timestamp) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _resolve_rate(rates: tuple[EffectiveRate, ...], execution_date: date, rate_name: str) -> Decimal:
    active_rates = [rate for rate in rates if rate.effective_date <= execution_date]
    if not active_rates:
        earliest = min((rate.effective_date for rate in rates), default=None)
        raise FeeScheduleError(
            f"{rate_name}无该日期的已知费率: execution_date={execution_date}, "
            f"earliest_known_effective_date={earliest}"
        )
    return sorted(active_rates, key=lambda rate: rate.effective_date)[-1].rate


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


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
        requested_quantity=order_intent.quantity,
    )


def _rejected(
    order_intent: OrderIntent,
    execution_date: date,
    execution_price: float | None,
    reason: str,
    requested_quantity: int | None = None,
    limit_check: LimitCheck = "NOT_EVALUATED",
) -> FillLedgerEntry:
    return FillLedgerEntry(
        order_intent=order_intent,
        intent_date=order_intent.decision_date,
        execution_date=execution_date,
        execution_price=execution_price,
        filled_quantity=0,
        status="REJECTED",
        reason=reason,
        requested_quantity=order_intent.quantity if requested_quantity is None else requested_quantity,
        limit_check=limit_check,
    )


def _suspended(
    order_intent: OrderIntent,
    execution_date: date,
) -> FillLedgerEntry:
    return FillLedgerEntry(
        order_intent=order_intent,
        intent_date=order_intent.decision_date,
        execution_date=execution_date,
        execution_price=None,
        filled_quantity=0,
        status="SUSPENDED",
        reason="NO_TRADE_SUSPENDED",
        requested_quantity=order_intent.quantity,
    )


@dataclass(frozen=True)
class _ExecutionBar:
    open_price: float | None
    trade_status: str


@dataclass(frozen=True)
class _LimitEvaluation:
    rejection_reason: str | None
    limit_check: LimitCheck


@dataclass(frozen=True)
class _SecurityMasterView:
    board: str
    list_date: date


def _limit_prices(previous_close: float, limit_rate: Decimal) -> tuple[float, float]:
    close = Decimal(str(previous_close))
    limit_up = (close * (Decimal("1") + limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    limit_down = (close * (Decimal("1") - limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    return float(limit_up), float(limit_down)


def _asymmetric_limit_prices(
    previous_close: float,
    limit_up_rate: Decimal,
    limit_down_rate: Decimal,
) -> tuple[float, float]:
    close = Decimal(str(previous_close))
    limit_up = (close * (Decimal("1") + limit_up_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    limit_down = (close * (Decimal("1") - limit_down_rate)).quantize(
        PRICE_TICK,
        rounding=ROUND_HALF_UP,
    )
    return float(limit_up), float(limit_down)


def _limit_prices_for_rule(
    calendar: TradingCalendar,
    previous_close: float,
    rule: PriceLimitRule,
    list_date: date,
    execution_date: date,
) -> tuple[float, float] | None:
    trading_day_number = _listing_trading_day_number(calendar, list_date, execution_date)
    if trading_day_number is not None:
        if rule.new_listing_policy == "FIRST_FIVE_NO_LIMIT" and trading_day_number <= 5:
            return None
        if rule.new_listing_policy == "FIRST_DAY_NO_LIMIT" and trading_day_number == 1:
            return None
        if rule.new_listing_policy == "FIRST_DAY_ASYMMETRIC" and trading_day_number == 1:
            if rule.first_day_limit_up_rate is None or rule.first_day_limit_down_rate is None:
                return _limit_prices(previous_close, rule.daily_limit_rate)
            return _asymmetric_limit_prices(
                previous_close,
                rule.first_day_limit_up_rate,
                rule.first_day_limit_down_rate,
            )

    return _limit_prices(previous_close, rule.daily_limit_rate)


def _listing_trading_day_number(
    calendar: TradingCalendar,
    list_date: date,
    execution_date: date,
) -> int | None:
    if execution_date < list_date:
        return None
    if not calendar.trade_dates or list_date < calendar.trade_dates[0]:
        return None
    return calendar.trading_days_between(list_date, execution_date)
