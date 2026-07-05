from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable

import pandas as pd

from src.domain import DataContractError, TradeStatus
from src.engine.dummy_strategy import OrderIntent
from src.engine.event_clock import ClockContext
from src.features.cross_sectional_momentum import (
    CrossSectionalMomentumSignal,
    calculate_cross_sectional_momentum_signal,
)
from src.features.pit_adjustment_service import AdjustedReturnStatus, EVIDENCE_STATUS, PITAdjustmentService


@dataclass(frozen=True)
class MomentumOrderIntent(OrderIntent):
    evidence_status: str = EVIDENCE_STATUS
    signal_manifest_hash: str = ""
    target_weight: Decimal = Decimal("0")


@dataclass(frozen=True)
class MomentumStrategyConfig:
    top_n: int
    max_single_name_weight: Decimal = Decimal("1")
    min_cash_buffer: Decimal = Decimal("0")
    lot_size: int = 100
    rebalance_every_n_days: int = 1

    def __post_init__(self) -> None:
        if self.top_n <= 0:
            raise ValueError("top_n must be positive")
        if self.max_single_name_weight <= Decimal("0") or self.max_single_name_weight > Decimal("1"):
            raise ValueError("max_single_name_weight must be in (0, 1]")
        if self.min_cash_buffer < Decimal("0") or self.min_cash_buffer >= Decimal("1"):
            raise ValueError("min_cash_buffer must be in [0, 1)")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be positive")
        if self.rebalance_every_n_days <= 0:
            raise ValueError("rebalance_every_n_days must be positive")


@dataclass(frozen=True)
class MomentumTargetWeight:
    security_id: str
    weight: Decimal
    rank: int
    momentum_score: Decimal


@dataclass
class SignalDrivenMomentumStrategy:
    security_ids: tuple[str, ...]
    adjustment_service: PITAdjustmentService
    portfolio_ledger: Any
    config: MomentumStrategyConfig
    _bar_index: int = 0
    latest_signal: CrossSectionalMomentumSignal | None = field(default=None, init=False)
    latest_target_weights: tuple[MomentumTargetWeight, ...] = field(default=tuple(), init=False)

    def __post_init__(self) -> None:
        self.security_ids = tuple(dict.fromkeys(str(security_id).zfill(6) for security_id in self.security_ids))
        if not self.security_ids:
            raise ValueError("security_ids must not be empty")

    def on_bar(self, ctx: ClockContext) -> list[OrderIntent]:
        is_rebalance_day = self._bar_index % self.config.rebalance_every_n_days == 0
        self._bar_index += 1
        if not is_rebalance_day:
            return []

        tradable = set(self._tradable_universe(ctx))
        signal_universe = tuple(security_id for security_id in self.security_ids if security_id in tradable)
        signal = calculate_cross_sectional_momentum_signal(
            signal_universe,
            ctx.asof_ts,
            ctx.asof_ts,
            self.adjustment_service,
        )
        targets = build_equal_weight_targets(
            signal,
            top_n=self.config.top_n,
            max_single_name_weight=self.config.max_single_name_weight,
        )
        self.latest_signal = signal
        self.latest_target_weights = targets
        return self._diff_to_order_intents(ctx, signal, targets)

    def _diff_to_order_intents(
        self,
        ctx: ClockContext,
        signal: CrossSectionalMomentumSignal,
        targets: tuple[MomentumTargetWeight, ...],
    ) -> list[OrderIntent]:
        position_view = self.portfolio_ledger.position_view()
        target_weights = {target.security_id: target.weight for target in targets}
        relevant_ids = tuple(sorted(set(target_weights) | set(position_view)))
        prices = _visible_close_map(ctx, relevant_ids)
        nav = _portfolio_nav(self.portfolio_ledger, position_view, prices)
        investable_nav = nav * (Decimal("1") - self.config.min_cash_buffer)

        target_quantities: dict[str, int] = {}
        for security_id, weight in target_weights.items():
            price = prices.get(security_id)
            if price is None or price <= Decimal("0"):
                continue
            target_value = investable_nav * weight
            target_quantities[security_id] = _round_down_lot(
                int(target_value / price),
                self.config.lot_size,
            )

        intents: list[OrderIntent] = []
        for security_id in relevant_ids:
            position = position_view.get(security_id)
            current_quantity = 0 if position is None else position.total_quantity
            sellable_quantity = 0 if position is None else position.sellable_quantity
            target_quantity = target_quantities.get(security_id, 0)
            if current_quantity <= target_quantity:
                continue
            desired_sell = current_quantity - target_quantity
            sell_quantity = min(desired_sell, sellable_quantity)
            if target_quantity > 0:
                sell_quantity = _round_down_lot(sell_quantity, self.config.lot_size)
            if sell_quantity > 0:
                intents.append(
                    _intent(
                        security_id,
                        "sell",
                        sell_quantity,
                        ctx.trade_date,
                        "momentum_signal_sell_to_target",
                        signal.signal_manifest_hash,
                        target_weights.get(security_id, Decimal("0")),
                    )
                )

        for security_id in relevant_ids:
            current_quantity = position_view.get(security_id).total_quantity if security_id in position_view else 0
            target_quantity = target_quantities.get(security_id, 0)
            buy_quantity = _round_down_lot(target_quantity - current_quantity, self.config.lot_size)
            if buy_quantity > 0:
                intents.append(
                    _intent(
                        security_id,
                        "buy",
                        buy_quantity,
                        ctx.trade_date,
                        "momentum_signal_buy_to_target",
                        signal.signal_manifest_hash,
                        target_weights.get(security_id, Decimal("0")),
                    )
                )
        return intents

    def _tradable_universe(self, ctx: ClockContext) -> list[str]:
        rows = ctx.portal.query(
            "daily_bar_raw",
            columns=["security_id", "trade_date", "trade_status"],
        )
        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        tradable = rows.loc[
            (trade_dates == ctx.trade_date)
            & (rows["trade_status"].astype(str) == TradeStatus.NORMAL.value)
        ].copy()
        return sorted(tradable["security_id"].astype(str).str.zfill(6).drop_duplicates().tolist())


def build_equal_weight_targets(
    signal: CrossSectionalMomentumSignal,
    *,
    top_n: int,
    max_single_name_weight: Decimal,
) -> tuple[MomentumTargetWeight, ...]:
    if top_n <= 0:
        raise ValueError("top_n must be positive")
    eligible = sorted(
        (
            point
            for point in signal.points
            if point.status == AdjustedReturnStatus.OK
            and point.momentum_score is not None
            and point.cross_sectional_rank is not None
        ),
        key=lambda point: (point.cross_sectional_rank, point.security_id),
    )[:top_n]
    if not eligible:
        return tuple()
    equal_weight = Decimal("1") / Decimal(len(eligible))
    weight = min(equal_weight, max_single_name_weight)
    return tuple(
        MomentumTargetWeight(
            point.security_id,
            weight,
            point.cross_sectional_rank or 0,
            point.momentum_score or Decimal("0"),
        )
        for point in eligible
    )


def _intent(
    security_id: str,
    side: str,
    quantity: int,
    decision_date: date,
    reason: str,
    signal_manifest_hash: str,
    target_weight: Decimal,
) -> MomentumOrderIntent:
    return MomentumOrderIntent(
        security_id=security_id,
        side=side,
        quantity=quantity,
        decision_date=decision_date,
        reason=reason,
        tag="momentum_rebalance",
        evidence_status=EVIDENCE_STATUS,
        signal_manifest_hash=signal_manifest_hash,
        target_weight=target_weight,
    )


def _portfolio_nav(
    portfolio_ledger: Any,
    position_view: dict[str, Any],
    prices: dict[str, Decimal],
) -> Decimal:
    cash = (
        portfolio_ledger.cash.available_cash
        + portfolio_ledger.cash.frozen_cash
        + portfolio_ledger.cash.receivable_cash
    )
    market_value = Decimal("0")
    for security_id, position in position_view.items():
        price = prices.get(security_id)
        if price is None:
            raise DataContractError(f"missing visible close for held security {security_id}")
        market_value += price * Decimal(position.total_quantity)
    return cash + market_value


def _visible_close_map(ctx: ClockContext, security_ids: Iterable[str]) -> dict[str, Decimal]:
    requested = tuple(sorted(dict.fromkeys(str(security_id).zfill(6) for security_id in security_ids)))
    if not requested:
        return {}
    rows = ctx.portal.query(
        "daily_bar_raw",
        security_ids=requested,
        columns=["security_id", "trade_date", "close"],
    )
    if rows.empty:
        return {}
    rows = rows.copy()
    rows["_trade_date"] = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
    rows = rows.loc[rows["_trade_date"] <= ctx.trade_date].copy()
    rows = rows.loc[rows["close"].notna()].copy()
    prices: dict[str, Decimal] = {}
    for row in rows.sort_values(["security_id", "_trade_date"]).itertuples(index=False):
        prices[str(getattr(row, "security_id")).zfill(6)] = Decimal(str(getattr(row, "close")))
    return prices


def _round_down_lot(quantity: int, lot_size: int) -> int:
    if quantity <= 0:
        return 0
    return int((Decimal(quantity) / Decimal(lot_size)).to_integral_value(rounding=ROUND_DOWN)) * lot_size
