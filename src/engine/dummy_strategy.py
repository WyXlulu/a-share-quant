from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import pandas as pd

from src.domain import TradeStatus
from src.engine.event_clock import ClockContext


OrderSide = Literal["buy", "sell"]


@dataclass(frozen=True)
class OrderIntent:
    security_id: str
    side: OrderSide
    quantity: int
    decision_date: date
    reason: str
    tag: str = "dummy_rebalance"

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        object.__setattr__(self, "security_id", str(self.security_id).zfill(6))


@dataclass
class DummyStrategy:
    rebalance_every_n_days: int = 5
    target_count: int = 10
    order_quantity: int = 100
    _bar_index: int = 0

    def __post_init__(self) -> None:
        if self.rebalance_every_n_days <= 0:
            raise ValueError("rebalance_every_n_days must be positive")
        if self.target_count <= 0:
            raise ValueError("target_count must be positive")
        if self.order_quantity <= 0 or self.order_quantity % 100 != 0:
            raise ValueError("order_quantity must be a positive 100-share lot multiple")

    def on_bar(self, ctx: ClockContext) -> list[OrderIntent]:
        is_rebalance_day = self._bar_index % self.rebalance_every_n_days == 0
        self._bar_index += 1
        if not is_rebalance_day:
            return []

        candidates = self._tradable_universe(ctx)
        selected = candidates[: self.target_count]
        return [
            OrderIntent(
                security_id=security_id,
                side="buy",
                quantity=self.order_quantity,
                decision_date=ctx.trade_date,
                reason="fixed_id_order_dummy_strategy",
            )
            for security_id in selected
        ]

    def _tradable_universe(self, ctx: ClockContext) -> list[str]:
        rows = ctx.portal.query(
            "daily_bar_raw",
            columns=["security_id", "trade_date", "trade_status"],
        )
        trade_dates = pd.to_datetime(rows["trade_date"], errors="raise").dt.date

        # Tradability deliberately uses only same-day fields visible through ctx.portal:
        # trade_date == T and trade_status == NORMAL. It does not read security_master.is_st
        # because that field is currently CURRENT_SNAPSHOT_ONLY and would contaminate PIT selection.
        tradable = rows.loc[
            (trade_dates == ctx.trade_date)
            & (rows["trade_status"].astype(str) == TradeStatus.NORMAL.value)
        ].copy()
        security_ids = tradable["security_id"].astype(str).str.zfill(6).drop_duplicates()
        return sorted(security_ids.tolist())


@dataclass
class DummyRebalanceStrategy(DummyStrategy):
    portfolio_ledger: Any = None

    def on_bar(self, ctx: ClockContext) -> list[OrderIntent]:
        is_rebalance_day = self._bar_index % self.rebalance_every_n_days == 0
        self._bar_index += 1
        if not is_rebalance_day:
            return []

        candidates = self._tradable_universe(ctx)
        targets = set(candidates[: self.target_count])
        position_view = (
            self.portfolio_ledger.position_view()
            if self.portfolio_ledger is not None
            else {}
        )

        intents: list[OrderIntent] = []
        for security_id in sorted(position_view):
            position = position_view[security_id]
            if security_id not in targets and position.sellable_quantity > 0:
                intents.append(
                    OrderIntent(
                        security_id=security_id,
                        side="sell",
                        quantity=position.sellable_quantity,
                        decision_date=ctx.trade_date,
                        reason="fixed_id_order_dummy_rebalance_sell",
                    )
                )

        for security_id in sorted(targets):
            position = position_view.get(security_id)
            if position is None or position.total_quantity == 0:
                intents.append(
                    OrderIntent(
                        security_id=security_id,
                        side="buy",
                        quantity=self.order_quantity,
                        decision_date=ctx.trade_date,
                        reason="fixed_id_order_dummy_rebalance_buy",
                    )
                )
        return intents
