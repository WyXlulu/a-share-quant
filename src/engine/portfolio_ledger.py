from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from src.engine.execution import FillLedgerEntry
from src.market_calendar import TradingCalendar


MONEY_QUANT = Decimal("0.01")
LotSource = Literal["BUY", "STOCK_DIVIDEND", "SPLIT", "TRANSFER"]
PortfolioEventType = Literal["BUY_FILL", "SELL_FILL"]


class PortfolioLedgerError(ValueError):
    """Base class for portfolio ledger validation failures."""


class InsufficientSellableQuantityError(PortfolioLedgerError):
    """Raised when a sell fill would consume more unlocked inventory than exists."""


@dataclass
class CashState:
    settled_cash: Decimal = Decimal("0.00")
    available_cash: Decimal = Decimal("0.00")
    frozen_cash: Decimal = Decimal("0.00")
    receivable_cash: Decimal = Decimal("0.00")

    def __post_init__(self) -> None:
        self.settled_cash = _money(self.settled_cash)
        self.available_cash = _money(self.available_cash)
        self.frozen_cash = _money(self.frozen_cash)
        self.receivable_cash = _money(self.receivable_cash)


@dataclass(frozen=True)
class PositionLot:
    quantity: int
    cost_basis: Decimal
    trade_date: date
    sellable_from: date
    source: LotSource = "BUY"
    locked_quantity: int = 0

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.locked_quantity < 0 or self.locked_quantity > self.quantity:
            raise ValueError("locked_quantity must be between 0 and quantity")
        if self.cost_basis < Decimal("0"):
            raise ValueError("cost_basis cannot be negative")
        object.__setattr__(self, "cost_basis", _money(self.cost_basis))

    @property
    def unlocked_quantity(self) -> int:
        return self.quantity - self.locked_quantity


@dataclass
class PositionState:
    security_id: str
    lots: list[PositionLot] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.security_id = str(self.security_id).zfill(6)

    @property
    def total_quantity(self) -> int:
        return sum(lot.quantity for lot in self.lots)

    @property
    def locked_quantity(self) -> int:
        return sum(lot.locked_quantity for lot in self.lots)

    @property
    def sellable_quantity(self) -> int:
        # Phase 1 step 1 records sellable_from but does not enforce date-based unlock yet.
        return sum(lot.unlocked_quantity for lot in self.lots)

    @property
    def cost_basis(self) -> Decimal:
        return _money(sum((lot.cost_basis for lot in self.lots), Decimal("0.00")))


@dataclass(frozen=True)
class PortfolioLedgerEntry:
    event_id: int
    event_type: PortfolioEventType
    security_id: str
    trade_date: date
    quantity_delta: int
    cash_delta: Decimal
    cost_basis_delta: Decimal
    realized_pnl: Decimal
    fill_reason: str
    position_quantity_after: int
    available_cash_after: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "security_id", str(self.security_id).zfill(6))
        object.__setattr__(self, "cash_delta", _money(self.cash_delta))
        object.__setattr__(self, "cost_basis_delta", _money(self.cost_basis_delta))
        object.__setattr__(self, "realized_pnl", _money(self.realized_pnl))
        object.__setattr__(
            self, "available_cash_after", _money(self.available_cash_after)
        )


@dataclass
class PortfolioLedger:
    cash: CashState
    calendar: TradingCalendar | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    ledger_entries: list[PortfolioLedgerEntry] = field(default_factory=list)

    def apply_fill(self, fill: FillLedgerEntry) -> PortfolioLedgerEntry | None:
        if fill.status != "FILLED":
            return None
        if fill.execution_date is None:
            raise PortfolioLedgerError("FILLED entry must have execution_date")

        side = fill.order_intent.side
        if side == "buy":
            return self._apply_buy(fill)
        if side == "sell":
            return self._apply_sell(fill)
        raise PortfolioLedgerError(f"unsupported order side: {side}")

    def assert_invariants(self) -> None:
        for security_id, position in self.positions.items():
            if security_id != position.security_id:
                raise AssertionError("position key must match security_id")
            derived_total = sum(lot.quantity for lot in position.lots)
            derived_locked = sum(lot.locked_quantity for lot in position.lots)
            derived_sellable = sum(lot.unlocked_quantity for lot in position.lots)
            derived_cost = _money(
                sum((lot.cost_basis for lot in position.lots), Decimal("0.00"))
            )
            if position.total_quantity != derived_total:
                raise AssertionError("total_quantity invariant failed")
            if position.locked_quantity != derived_locked:
                raise AssertionError("locked_quantity invariant failed")
            if position.sellable_quantity != derived_sellable:
                raise AssertionError("sellable_quantity invariant failed")
            if position.cost_basis != derived_cost:
                raise AssertionError("cost_basis invariant failed")

    def _apply_buy(self, fill: FillLedgerEntry) -> PortfolioLedgerEntry:
        security_id = fill.order_intent.security_id
        trade_date = _require_execution_date(fill)
        net_amount = _money(fill.net_amount)
        position = self._position(security_id)
        lot = PositionLot(
            quantity=fill.filled_quantity,
            cost_basis=net_amount,
            trade_date=trade_date,
            sellable_from=self._next_trading_day(trade_date),
            source="BUY",
        )

        self.cash.available_cash = _money(self.cash.available_cash - net_amount)
        position.lots.append(lot)
        entry = self._append_entry(
            event_type="BUY_FILL",
            security_id=security_id,
            trade_date=trade_date,
            quantity_delta=fill.filled_quantity,
            cash_delta=-net_amount,
            cost_basis_delta=net_amount,
            realized_pnl=Decimal("0.00"),
            fill_reason=fill.reason,
            position=position,
        )
        self.assert_invariants()
        return entry

    def _apply_sell(self, fill: FillLedgerEntry) -> PortfolioLedgerEntry:
        security_id = fill.order_intent.security_id
        trade_date = _require_execution_date(fill)
        position = self._position(security_id)
        quantity = fill.filled_quantity
        if position.sellable_quantity < quantity:
            raise InsufficientSellableQuantityError(
                f"sell quantity {quantity} exceeds sellable quantity "
                f"{position.sellable_quantity} for {security_id}"
            )

        consumed_cost, remaining_lots = _consume_fifo(position.lots, quantity)
        net_amount = _money(fill.net_amount)
        realized_pnl = _money(net_amount - consumed_cost)

        self.cash.available_cash = _money(self.cash.available_cash + net_amount)
        position.lots = remaining_lots
        entry = self._append_entry(
            event_type="SELL_FILL",
            security_id=security_id,
            trade_date=trade_date,
            quantity_delta=-quantity,
            cash_delta=net_amount,
            cost_basis_delta=-consumed_cost,
            realized_pnl=realized_pnl,
            fill_reason=fill.reason,
            position=position,
        )
        self.assert_invariants()
        return entry

    def _position(self, security_id: str) -> PositionState:
        normalized = str(security_id).zfill(6)
        if normalized not in self.positions:
            self.positions[normalized] = PositionState(normalized)
        return self.positions[normalized]

    def _append_entry(
        self,
        *,
        event_type: PortfolioEventType,
        security_id: str,
        trade_date: date,
        quantity_delta: int,
        cash_delta: Decimal,
        cost_basis_delta: Decimal,
        realized_pnl: Decimal,
        fill_reason: str,
        position: PositionState,
    ) -> PortfolioLedgerEntry:
        entry = PortfolioLedgerEntry(
            event_id=len(self.ledger_entries) + 1,
            event_type=event_type,
            security_id=security_id,
            trade_date=trade_date,
            quantity_delta=quantity_delta,
            cash_delta=cash_delta,
            cost_basis_delta=cost_basis_delta,
            realized_pnl=realized_pnl,
            fill_reason=fill_reason,
            position_quantity_after=position.total_quantity,
            available_cash_after=self.cash.available_cash,
        )
        self.ledger_entries.append(entry)
        return entry

    def _next_trading_day(self, trade_date: date) -> date:
        if self.calendar is not None:
            return self.calendar.next_trading_day(trade_date)
        return trade_date + timedelta(days=1)


def _consume_fifo(lots: list[PositionLot], quantity: int) -> tuple[Decimal, list[PositionLot]]:
    remaining_to_sell = quantity
    consumed_cost = Decimal("0.00")
    remaining_lots: list[PositionLot] = []

    for lot in lots:
        if remaining_to_sell <= 0:
            remaining_lots.append(lot)
            continue

        sellable_from_lot = min(lot.unlocked_quantity, remaining_to_sell)
        if sellable_from_lot <= 0:
            remaining_lots.append(lot)
            continue

        if sellable_from_lot == lot.quantity:
            lot_cost = lot.cost_basis
        else:
            lot_cost = _money(
                lot.cost_basis
                * Decimal(sellable_from_lot)
                / Decimal(lot.quantity)
            )
        consumed_cost = _money(consumed_cost + lot_cost)
        remaining_to_sell -= sellable_from_lot

        quantity_left = lot.quantity - sellable_from_lot
        if quantity_left > 0:
            remaining_lots.append(
                replace(
                    lot,
                    quantity=quantity_left,
                    cost_basis=_money(lot.cost_basis - lot_cost),
                )
            )

    if remaining_to_sell != 0:
        raise AssertionError("FIFO consume called without enough sellable inventory")
    return consumed_cost, remaining_lots


def _require_execution_date(fill: FillLedgerEntry) -> date:
    if fill.execution_date is None:
        raise PortfolioLedgerError("execution_date is required")
    return fill.execution_date


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
