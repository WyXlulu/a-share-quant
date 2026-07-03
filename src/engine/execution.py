from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_calendar import TradingCalendar
from src.data import PITDataPortal
from src.domain import DataContractError, TradeStatus, calculate_ex_right_reference_price
from src.engine.dummy_strategy import OrderIntent

if TYPE_CHECKING:
    from src.engine.portfolio_ledger import PortfolioLedger


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
DAILY_BAR_ASOF_TIME = time(15, 0, 0)
PRICE_TICK = Decimal("0.01")
PRICE_TOLERANCE = 1e-9
MONEY_QUANT = Decimal("0.01")
BROKER_ADAPTER_RULE = (
    "同一T+1开盘轮次，执行顺序为先处理全部卖单、再处理全部买单；"
    "卖出FILLED所释放的净额现金，计入本轮买单的可用现金。"
    "实盘对接时须逐字核对券商真实资金可用规则。"
)
FillStatus = Literal["FILLED", "UNFILLED", "REJECTED", "SUSPENDED"]
OrderTTL = Literal["NEXT_OPEN_ONLY"]
LimitCheck = Literal[
    "NOT_EVALUATED",
    "APPLIED",
    "SKIPPED_NO_PREV_CLOSE",
    "SKIPPED_NO_MASTER",
    "SKIPPED_NO_RULE",
    "EXEMPT_NEW_LISTING",
]
LotSizePolicy = Literal["ROUND_DOWN", "REJECT"]
CapacityReason = Literal["NONE", "CAPACITY_CAPPED"]
ADVWindowStatus = Literal["NOT_EVALUATED", "ADV_FULL_WINDOW", "ADV_PARTIAL_WINDOW"]
LimitReferenceStatus = Literal[
    "NONE",
    "LIMIT_REF_ADJUSTED_FOR_CA",
    "LIMIT_REF_UNADJUSTED_CA_INVISIBLE",
]
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
    reserved_cash: Decimal = Decimal("0.00")
    original_quantity: int = 0
    capacity_reason: CapacityReason = "NONE"
    adv_window_status: ADVWindowStatus = "NOT_EVALUATED"
    limit_reference_status: LimitReferenceStatus = "NONE"


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
    version: str = "limit_rule_table_v1"
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
class LockedOrder:
    order_intent: OrderIntent
    locked_quantity: int
    original_quantity: int
    reference_price: Decimal | None
    price_cap: Decimal | None
    price_floor: Decimal | None
    reserved_cash: Decimal
    reference_price_ts: pd.Timestamp
    ruleset_version: str
    ttl: OrderTTL = "NEXT_OPEN_ONLY"
    limit_check: LimitCheck = "NOT_EVALUATED"
    capacity_reason: CapacityReason = "NONE"
    adv_window_status: ADVWindowStatus = "NOT_EVALUATED"
    limit_reference_status: LimitReferenceStatus = "NONE"
    trailing_adv_notional: Decimal | None = None
    max_order_notional: Decimal | None = None

    def __post_init__(self) -> None:
        if self.locked_quantity < 0:
            raise ValueError("locked_quantity cannot be negative")
        if self.original_quantity <= 0:
            raise ValueError("original_quantity must be positive")
        object.__setattr__(
            self,
            "reference_price",
            None if self.reference_price is None else _money(self.reference_price),
        )
        object.__setattr__(
            self,
            "price_cap",
            None if self.price_cap is None else _money(self.price_cap),
        )
        object.__setattr__(
            self,
            "price_floor",
            None if self.price_floor is None else _money(self.price_floor),
        )
        object.__setattr__(self, "reserved_cash", _money(self.reserved_cash))
        object.__setattr__(
            self,
            "trailing_adv_notional",
            None
            if self.trailing_adv_notional is None
            else _money(self.trailing_adv_notional),
        )
        object.__setattr__(
            self,
            "max_order_notional",
            None if self.max_order_notional is None else _money(self.max_order_notional),
        )


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
    # 来自规范V3订正第5条: 开盘成交不得用全日ADV, 须显著折扣;
    # 该系数须进入敏感性分析。
    opening_liquidity_fraction: Decimal = Decimal("0.05")
    # 来自规范V3订正第5条: 开盘成交不得用全日ADV, 须显著折扣;
    # 该窗口须与开盘流动性折扣一起进入敏感性分析。
    trailing_adv_days: int = 20

    def __post_init__(self) -> None:
        if self.opening_liquidity_fraction <= Decimal("0"):
            raise ValueError("opening_liquidity_fraction must be positive")
        if self.trailing_adv_days <= 0:
            raise ValueError("trailing_adv_days must be positive")

    def lock_order(
        self,
        order_intent: OrderIntent,
        available_cash: Decimal | None = None,
        *,
        strict_pit: bool = True,
    ) -> LockedOrder | FillLedgerEntry:
        next_session = self._next_session(order_intent.decision_date)
        if next_session is None:
            return _unfilled(order_intent, None, "NO_NEXT_SESSION")

        locked_quantity = self._buy_lot_quantity(order_intent)
        if locked_quantity is None:
            return _rejected(
                order_intent,
                None,
                None,
                "ODD_LOT_REJECTED",
                requested_quantity=order_intent.quantity,
            )

        reference = self._reference_price(order_intent.security_id, order_intent.decision_date)
        limit_reference_status: LimitReferenceStatus = "NONE"
        if reference is not None:
            reference_context = self._limit_reference_context(
                order_intent.security_id,
                order_intent.decision_date,
                next_session,
                reference,
            )
            reference = reference_context.reference_price
            limit_reference_status = reference_context.status
        limit_check = "NOT_EVALUATED"
        price_cap: Decimal | None = None
        price_floor: Decimal | None = None
        if reference is None:
            limit_check = "SKIPPED_NO_PREV_CLOSE"
        else:
            master_asof = order_intent.decision_date if strict_pit else next_session
            master = self._security_master(order_intent.security_id, master_asof)
            if master is None:
                limit_check = "SKIPPED_NO_MASTER"
            else:
                limit_rule = self.limit_rule_table.resolve(master.board, next_session)
                if limit_rule is None:
                    limit_check = "SKIPPED_NO_RULE"
                else:
                    limit_prices = _limit_prices_for_rule(
                        self.calendar,
                        float(reference),
                        limit_rule,
                        master.list_date,
                        next_session,
                    )
                    if limit_prices is None:
                        limit_check = "EXEMPT_NEW_LISTING"
                    else:
                        limit_up, limit_down = limit_prices
                        price_cap = _money(Decimal(str(limit_up)))
                        price_floor = _money(Decimal(str(limit_down)))
                        limit_check = "APPLIED"

        capacity_reason: CapacityReason = "NONE"
        adv_window_status: ADVWindowStatus = "NOT_EVALUATED"
        trailing_adv_notional: Decimal | None = None
        max_order_notional: Decimal | None = None
        if locked_quantity > 0 and reference is not None:
            capacity = self._capacity_limit(
                order_intent.security_id,
                order_intent.decision_date,
            )
            if capacity is None:
                return _rejected(
                    order_intent,
                    None,
                    None,
                    "CAPACITY_NO_ADV_DATA",
                    requested_quantity=order_intent.quantity,
                )
            trailing_adv_notional = capacity.trailing_adv_notional
            max_order_notional = capacity.max_order_notional
            adv_window_status = capacity.adv_window_status
            order_notional = _money(reference * Decimal(locked_quantity))
            if order_notional > max_order_notional:
                capped_quantity = _round_quantity_for_capacity(
                    int(max_order_notional / reference)
                )
                if capped_quantity <= 0:
                    return _rejected(
                        order_intent,
                        None,
                        None,
                        "CAPACITY_REJECTED",
                        requested_quantity=order_intent.quantity,
                        adv_window_status=adv_window_status,
                    )
                locked_quantity = min(locked_quantity, capped_quantity)
                capacity_reason = "CAPACITY_CAPPED"

        reserved_cash = Decimal("0.00")
        if order_intent.side == "buy" and locked_quantity > 0:
            reservation_price = price_cap or reference
            if reservation_price is not None:
                reserved_cash = self.fee_schedule.calculate(
                    "buy",
                    next_session,
                    float(reservation_price),
                    locked_quantity,
                ).net_amount
        if (
            order_intent.side == "buy"
            and available_cash is not None
            and reserved_cash > _money(available_cash)
        ):
            return _rejected(
                order_intent,
                None,
                None,
                "CASH_INSUFFICIENT",
                requested_quantity=order_intent.quantity,
            )

        return LockedOrder(
            order_intent=order_intent,
            locked_quantity=locked_quantity,
            original_quantity=order_intent.quantity,
            reference_price=reference,
            price_cap=price_cap,
            price_floor=price_floor,
            reserved_cash=reserved_cash,
            reference_price_ts=_daily_bar_asof(order_intent.decision_date),
            ruleset_version=self.limit_rule_table.version,
            ttl="NEXT_OPEN_ONLY",
            limit_check=limit_check,
            capacity_reason=capacity_reason,
            adv_window_status=adv_window_status,
            limit_reference_status=limit_reference_status,
            trailing_adv_notional=trailing_adv_notional,
            max_order_notional=max_order_notional,
        )

    def execute(self, orders: list[LockedOrder | OrderIntent]) -> list[FillLedgerEntry]:
        return [self.execute_one(order) for order in orders]

    def execute_open_round(
        self,
        locked_orders: list[LockedOrder],
        portfolio_ledger: "PortfolioLedger",
    ) -> list[FillLedgerEntry]:
        """Execute one same-decision-date T+1 open round using BROKER_ADAPTER_RULE.

        This is only a broker-adapter execution rule: T-day buy locking remains
        conservative and must not pre-borrow cash from future sell fills, because
        a T+1 sell can still be rejected, suspended, or missing an open price.
        """
        if not locked_orders:
            return []

        decision_dates = {
            locked_order.order_intent.decision_date for locked_order in locked_orders
        }
        if len(decision_dates) != 1:
            raise ValueError("execute_open_round requires one decision_date")

        sell_orders = [
            locked_order
            for locked_order in locked_orders
            if locked_order.order_intent.side == "sell"
        ]
        buy_orders = [
            locked_order
            for locked_order in locked_orders
            if locked_order.order_intent.side == "buy"
        ]

        fills: list[FillLedgerEntry] = []
        sell_fills = [self.execute_one(locked_order) for locked_order in sell_orders]
        for fill in sell_fills:
            portfolio_ledger.apply_execution_result(fill)
        fills.extend(sell_fills)

        for locked_order in buy_orders:
            fill = self.execute_one(locked_order)
            portfolio_ledger.apply_execution_result(fill)
            fills.append(fill)

        return fills

    def execute_one(self, order: LockedOrder | OrderIntent) -> FillLedgerEntry:
        locked_order = self._ensure_locked_order(order)
        if isinstance(locked_order, FillLedgerEntry):
            return locked_order

        order_intent = locked_order.order_intent
        next_session = self._next_session(order_intent.decision_date)
        if next_session is None:
            return _unfilled(order_intent, None, "NO_NEXT_SESSION")

        execution_bar = self._execution_bar(order_intent.security_id, next_session)
        if execution_bar is None:
            return _unfilled(
                order_intent,
                next_session,
                "NO_OPEN_PRICE",
                locked_order.reserved_cash,
                capacity_reason=locked_order.capacity_reason,
                adv_window_status=locked_order.adv_window_status,
                limit_reference_status=locked_order.limit_reference_status,
            )
        if execution_bar.trade_status == TradeStatus.SUSPENDED.value:
            return _suspended(
                order_intent,
                next_session,
                locked_order.reserved_cash,
                capacity_reason=locked_order.capacity_reason,
                adv_window_status=locked_order.adv_window_status,
                limit_reference_status=locked_order.limit_reference_status,
            )
        if execution_bar.open_price is None:
            return _unfilled(
                order_intent,
                next_session,
                "NO_OPEN_PRICE",
                locked_order.reserved_cash,
                capacity_reason=locked_order.capacity_reason,
                adv_window_status=locked_order.adv_window_status,
                limit_reference_status=locked_order.limit_reference_status,
            )

        limit_evaluation = self._limit_evaluation(locked_order, execution_bar.open_price)
        if limit_evaluation.rejection_reason is not None:
            return _rejected(
                order_intent,
                next_session,
                execution_bar.open_price,
                limit_evaluation.rejection_reason,
                requested_quantity=order_intent.quantity,
                limit_check=limit_evaluation.limit_check,
                reserved_cash=locked_order.reserved_cash,
                capacity_reason=locked_order.capacity_reason,
                adv_window_status=locked_order.adv_window_status,
                limit_reference_status=locked_order.limit_reference_status,
            )

        fees = self.fee_schedule.calculate(
            order_intent.side,
            next_session,
            execution_bar.open_price,
            locked_order.locked_quantity,
        )
        return FillLedgerEntry(
            order_intent=order_intent,
            intent_date=order_intent.decision_date,
            execution_date=next_session,
            execution_price=execution_bar.open_price,
            filled_quantity=locked_order.locked_quantity,
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
            reserved_cash=locked_order.reserved_cash,
            original_quantity=locked_order.original_quantity,
            capacity_reason=locked_order.capacity_reason,
            adv_window_status=locked_order.adv_window_status,
            limit_reference_status=locked_order.limit_reference_status,
        )

    def _ensure_locked_order(self, order: LockedOrder | OrderIntent) -> LockedOrder | FillLedgerEntry:
        if isinstance(order, LockedOrder):
            return order
        return self.lock_order(order, strict_pit=False)

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

    def _limit_evaluation(self, locked_order: LockedOrder, open_price: float) -> "_LimitEvaluation":
        order_intent = locked_order.order_intent
        if locked_order.limit_check != "APPLIED":
            return _LimitEvaluation(rejection_reason=None, limit_check=locked_order.limit_check)
        if (
            order_intent.side == "buy"
            and locked_order.price_cap is not None
            and open_price >= float(locked_order.price_cap) - PRICE_TOLERANCE
        ):
            return _LimitEvaluation(rejection_reason="LIMIT_UP_NO_BUY", limit_check="APPLIED")
        if (
            order_intent.side == "sell"
            and locked_order.price_floor is not None
            and open_price <= float(locked_order.price_floor) + PRICE_TOLERANCE
        ):
            return _LimitEvaluation(rejection_reason="LIMIT_DOWN_NO_SELL", limit_check="APPLIED")
        return _LimitEvaluation(rejection_reason=None, limit_check="APPLIED")

    def _reference_price(self, security_id: str, intent_date: date) -> Decimal | None:
        previous_close = self._previous_close(security_id, intent_date)
        if previous_close is None:
            return None
        return _money(Decimal(str(previous_close)))

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

    def _limit_reference_context(
        self,
        security_id: str,
        intent_date: date,
        execution_date: date,
        previous_close: Decimal,
    ) -> "_LimitReferenceContext":
        visible_actions = self._corporate_actions_on_ex_date(
            security_id,
            intent_date,
            execution_date,
        )
        if not visible_actions.empty:
            cash_dividend, share_ratio, rights_ratio, rights_price = _corporate_action_adjustments(
                visible_actions
            )
            adjusted_reference = _money(
                calculate_ex_right_reference_price(
                    execution_date,
                    previous_close,
                    cash_dividend,
                    share_ratio,
                    rights_ratio,
                    rights_price,
                )
            )
            if adjusted_reference != previous_close:
                return _LimitReferenceContext(
                    reference_price=adjusted_reference,
                    status="LIMIT_REF_ADJUSTED_FOR_CA",
                )
            return _LimitReferenceContext(reference_price=previous_close, status="NONE")

        close_visible_actions = self._corporate_actions_on_ex_date(
            security_id,
            execution_date,
            execution_date,
        )
        if not close_visible_actions.empty:
            return _LimitReferenceContext(
                reference_price=previous_close,
                status="LIMIT_REF_UNADJUSTED_CA_INVISIBLE",
            )
        return _LimitReferenceContext(reference_price=previous_close, status="NONE")

    def _corporate_actions_on_ex_date(
        self,
        security_id: str,
        asof_date: date,
        ex_date: date,
    ) -> pd.DataFrame:
        try:
            rows = self.portal.query(
                "corporate_actions",
                _daily_bar_asof(asof_date),
                security_ids=[security_id],
            )
        except DataContractError:
            return pd.DataFrame()
        if rows.empty:
            return rows

        ex_dates = pd.to_datetime(rows["ex_date"], errors="raise").dt.date
        actions = rows.loc[ex_dates == ex_date].copy()
        if actions.empty:
            return actions
        return _latest_corporate_action_rows(actions)

    def _capacity_limit(
        self,
        security_id: str,
        intent_date: date,
    ) -> "_CapacityLimit | None":
        try:
            rows = self.portal.query(
                "daily_bar_raw",
                _daily_bar_asof(intent_date),
                security_ids=[security_id],
                columns=["security_id", "trade_date", "amount"],
            )
        except DataContractError:
            return None
        if rows.empty:
            return None

        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        lookback_dates = [
            trade_date
            for trade_date in self.calendar.trade_dates
            if trade_date <= intent_date
        ][-self.trailing_adv_days :]
        amount_rows = rows.loc[trade_dates.isin(lookback_dates)].copy()
        amount_rows = amount_rows.loc[amount_rows["amount"].notna()].copy()
        if amount_rows.empty:
            return None

        amount_rows["_trade_date"] = pd.to_datetime(amount_rows["trade_date"], errors="raise")
        trailing_rows = amount_rows.sort_values("_trade_date", ascending=False).head(
            self.trailing_adv_days
        )
        amounts = [
            Decimal(str(amount))
            for amount in trailing_rows["amount"].tolist()
            if Decimal(str(amount)) >= Decimal("0")
        ]
        if not amounts:
            return None

        trailing_adv_notional = _money(
            sum(amounts, Decimal("0.00")) / Decimal(len(amounts))
        )
        max_order_notional = _money(
            trailing_adv_notional * self.opening_liquidity_fraction
        )
        adv_window_status: ADVWindowStatus = (
            "ADV_FULL_WINDOW"
            if len(amounts) >= self.trailing_adv_days
            else "ADV_PARTIAL_WINDOW"
        )
        return _CapacityLimit(
            trailing_adv_notional=trailing_adv_notional,
            max_order_notional=max_order_notional,
            adv_window_status=adv_window_status,
        )

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


def _round_quantity_for_capacity(quantity: int) -> int:
    return (quantity // 100) * 100


def _unfilled(
    order_intent: OrderIntent,
    execution_date: date | None,
    reason: str,
    reserved_cash: Decimal = Decimal("0.00"),
    capacity_reason: CapacityReason = "NONE",
    adv_window_status: ADVWindowStatus = "NOT_EVALUATED",
    limit_reference_status: LimitReferenceStatus = "NONE",
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
        reserved_cash=reserved_cash,
        original_quantity=order_intent.quantity,
        capacity_reason=capacity_reason,
        adv_window_status=adv_window_status,
        limit_reference_status=limit_reference_status,
    )


def _rejected(
    order_intent: OrderIntent,
    execution_date: date,
    execution_price: float | None,
    reason: str,
    requested_quantity: int | None = None,
    limit_check: LimitCheck = "NOT_EVALUATED",
    reserved_cash: Decimal = Decimal("0.00"),
    capacity_reason: CapacityReason = "NONE",
    adv_window_status: ADVWindowStatus = "NOT_EVALUATED",
    limit_reference_status: LimitReferenceStatus = "NONE",
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
        reserved_cash=reserved_cash,
        original_quantity=order_intent.quantity,
        capacity_reason=capacity_reason,
        adv_window_status=adv_window_status,
        limit_reference_status=limit_reference_status,
    )


def _suspended(
    order_intent: OrderIntent,
    execution_date: date,
    reserved_cash: Decimal = Decimal("0.00"),
    capacity_reason: CapacityReason = "NONE",
    adv_window_status: ADVWindowStatus = "NOT_EVALUATED",
    limit_reference_status: LimitReferenceStatus = "NONE",
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
        reserved_cash=reserved_cash,
        original_quantity=order_intent.quantity,
        capacity_reason=capacity_reason,
        adv_window_status=adv_window_status,
        limit_reference_status=limit_reference_status,
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


@dataclass(frozen=True)
class _CapacityLimit:
    trailing_adv_notional: Decimal
    max_order_notional: Decimal
    adv_window_status: ADVWindowStatus


@dataclass(frozen=True)
class _LimitReferenceContext:
    reference_price: Decimal
    status: LimitReferenceStatus


def _limit_prices(previous_close: float, limit_rate: Decimal) -> tuple[float, float]:
    close = Decimal(str(previous_close))
    limit_up = (close * (Decimal("1") + limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    limit_down = (close * (Decimal("1") - limit_rate)).quantize(PRICE_TICK, rounding=ROUND_HALF_UP)
    return float(limit_up), float(limit_down)


def _corporate_action_adjustments(actions: pd.DataFrame) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    cash_dividend = Decimal("0.00")
    share_ratio = Decimal("0.00")
    rights_ratio = Decimal("0.00")
    rights_consideration = Decimal("0.00")
    for row in actions.itertuples(index=False):
        action_type = str(getattr(row, "action_type"))
        if action_type == "RIGHTS_ISSUE":
            row_rights_ratio = _decimal_or_zero(getattr(row, "rights_ratio", Decimal("0.00")))
            row_rights_price = _decimal_or_zero(getattr(row, "rights_price", Decimal("0.00")))
            rights_ratio += row_rights_ratio
            rights_consideration += row_rights_ratio * row_rights_price
            continue
        if action_type not in ("CASH_DIVIDEND", "STOCK_DIVIDEND"):
            continue
        cash_dividend += _decimal_or_zero(getattr(row, "cash_dividend_per_share"))
        share_ratio += _decimal_or_zero(getattr(row, "share_ratio"))
    rights_price = Decimal("0.00")
    if rights_ratio > Decimal("0"):
        rights_price = rights_consideration / rights_ratio
    return _money(cash_dividend), share_ratio, rights_ratio, rights_price


def _latest_corporate_action_rows(actions: pd.DataFrame) -> pd.DataFrame:
    actions = actions.copy()
    actions["_available_at_sort"] = pd.to_datetime(actions["available_at"], errors="raise")
    return (
        actions.sort_values(["security_id", "ex_date", "action_type", "_available_at_sort"])
        .drop_duplicates(["security_id", "ex_date", "action_type"], keep="last")
        .drop(columns=["_available_at_sort"])
        .reset_index(drop=True)
    )


def _decimal_or_zero(value: object) -> Decimal:
    if pd.isna(value):
        return Decimal("0")
    return Decimal(str(value))


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
