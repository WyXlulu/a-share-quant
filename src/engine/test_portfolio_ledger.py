from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date
from decimal import Decimal

from src.engine.dummy_strategy import OrderIntent
from src.engine.execution import FillLedgerEntry
from src.engine.portfolio_ledger import (
    CashState,
    InsufficientSellableQuantityError,
    OddLotSellError,
    PortfolioLedger,
    PositionLot,
    PositionState,
)
from src.market_calendar import trading_calendar_from_dates


class PortfolioLedgerTest(unittest.TestCase):
    def test_buy_debits_cash_adds_lot_and_keeps_aggregates_derived(self) -> None:
        ledger = _ledger(initial_cash=Decimal("10000.00"))

        entry = ledger.apply_fill(
            _fill(
                security_id="000001",
                side="buy",
                quantity=100,
                net_amount=Decimal("1005.01"),
                execution_date=date(2026, 6, 30),
            )
        )

        position = ledger.positions["000001"]
        self.assertEqual(ledger.cash.available_cash, Decimal("8994.99"))
        self.assertEqual(position.total_quantity, 100)
        self.assertEqual(position.sellable_quantity, 0)
        self.assertEqual(position.locked_quantity, 0)
        self.assertEqual(position.unsellable_quantity, 100)
        self.assertEqual(position.cost_basis, Decimal("1005.01"))
        self.assertEqual(len(position.lots), 1)
        self.assertEqual(position.lots[0].quantity, 100)
        self.assertEqual(position.lots[0].cost_basis, Decimal("1005.01"))
        self.assertEqual(position.lots[0].trade_date, date(2026, 6, 30))
        self.assertEqual(position.lots[0].sellable_from, date(2026, 7, 1))
        self.assertFalse(position.lots[0].is_unlocked)
        self.assertEqual(position.lots[0].source, "BUY")
        self.assertEqual(entry.realized_pnl, Decimal("0.00"))
        self.assertEqual(len(ledger.ledger_entries), 1)
        _assert_position_aggregates_are_lot_derived(self, position)

    def test_ex004_same_day_buy_cannot_be_sold_until_next_trading_day_unlock(self) -> None:
        ledger = _ledger(initial_cash=Decimal("10000.00"))
        ledger.unlock_positions(date(2026, 6, 30))
        ledger.apply_fill(
            _fill(
                security_id="000001",
                side="buy",
                quantity=100,
                net_amount=Decimal("1005.01"),
                execution_date=date(2026, 6, 30),
            )
        )

        position = ledger.positions["000001"]
        self.assertEqual(position.sellable_quantity, 0)
        with self.assertRaises(InsufficientSellableQuantityError):
            ledger.apply_fill(
                _fill(
                    security_id="000001",
                    side="sell",
                    quantity=100,
                    net_amount=Decimal("990.00"),
                    execution_date=date(2026, 6, 30),
                )
            )

        ledger.unlock_positions(date(2026, 7, 1))

        self.assertEqual(position.sellable_quantity, 100)
        ledger.apply_fill(
            _fill(
                security_id="000001",
                side="sell",
                quantity=100,
                net_amount=Decimal("990.00"),
                execution_date=date(2026, 7, 1),
            )
        )
        self.assertEqual(position.total_quantity, 0)

    def test_unlock_positions_only_unlocks_due_lots(self) -> None:
        ledger = _ledger(initial_cash=Decimal("0.00"))
        ledger.positions["000001"] = PositionState(
            "000001",
            lots=[
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1000.00"),
                    trade_date=date(2026, 6, 30),
                    sellable_from=date(2026, 7, 1),
                ),
                PositionLot(
                    quantity=200,
                    cost_basis=Decimal("2200.00"),
                    trade_date=date(2026, 7, 1),
                    sellable_from=date(2026, 7, 2),
                ),
            ],
        )

        self.assertEqual(ledger.unlock_positions(date(2026, 7, 1)), 1)
        position = ledger.positions["000001"]
        self.assertEqual(position.sellable_quantity, 100)
        self.assertEqual(position.unsellable_quantity, 200)

        self.assertEqual(ledger.unlock_positions(date(2026, 7, 2)), 1)
        self.assertEqual(position.sellable_quantity, 300)
        self.assertEqual(position.unsellable_quantity, 0)
        _assert_position_aggregates_are_lot_derived(self, position)

    def test_sell_consumes_fifo_lots_and_records_realized_pnl(self) -> None:
        ledger = _ledger(initial_cash=Decimal("0.00"))
        ledger.positions["000001"] = PositionState(
            "000001",
            lots=[
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1005.01"),
                    trade_date=date(2026, 6, 29),
                    sellable_from=date(2026, 6, 30),
                    is_unlocked=True,
                ),
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1205.01"),
                    trade_date=date(2026, 6, 30),
                    sellable_from=date(2026, 7, 1),
                    is_unlocked=True,
                ),
            ],
        )

        entry = ledger.apply_fill(
            _fill(
                security_id="000001",
                side="sell",
                quantity=150,
                net_amount=Decimal("1790.00"),
                execution_date=date(2026, 7, 1),
            )
        )

        position = ledger.positions["000001"]
        self.assertEqual(ledger.cash.available_cash, Decimal("1790.00"))
        self.assertEqual(position.total_quantity, 50)
        self.assertEqual(position.cost_basis, Decimal("602.50"))
        self.assertEqual(len(position.lots), 1)
        self.assertEqual(position.lots[0].quantity, 50)
        self.assertEqual(position.lots[0].cost_basis, Decimal("602.50"))
        self.assertEqual(entry.cost_basis_delta, Decimal("-1607.52"))
        self.assertEqual(entry.realized_pnl, Decimal("182.48"))
        _assert_position_aggregates_are_lot_derived(self, position)

    def test_sell_with_insufficient_sellable_quantity_raises_and_does_not_mutate(self) -> None:
        ledger = _ledger(initial_cash=Decimal("100.00"))
        ledger.positions["000001"] = PositionState(
            "000001",
            lots=[
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1000.00"),
                    trade_date=date(2026, 6, 29),
                    sellable_from=date(2026, 6, 30),
                )
            ],
        )
        before_cash = deepcopy(ledger.cash)
        before_positions = deepcopy(ledger.positions)
        before_entries = list(ledger.ledger_entries)

        with self.assertRaises(InsufficientSellableQuantityError):
            ledger.apply_fill(
                _fill(
                    security_id="000001",
                    side="sell",
                    quantity=1,
                    net_amount=Decimal("9.00"),
                )
            )

        self.assertEqual(ledger.cash, before_cash)
        self.assertEqual(ledger.positions, before_positions)
        self.assertEqual(ledger.ledger_entries, before_entries)

    def test_sell_lock_prevents_reusing_the_same_inventory(self) -> None:
        ledger = _ledger(initial_cash=Decimal("0.00"))
        ledger.positions["000001"] = PositionState(
            "000001",
            lots=[
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1000.00"),
                    trade_date=date(2026, 6, 29),
                    sellable_from=date(2026, 6, 30),
                    is_unlocked=True,
                )
            ],
        )

        entry = ledger.lock_for_sell("000001", 100, trade_date=date(2026, 6, 30))

        position = ledger.positions["000001"]
        self.assertEqual(entry.event_type, "SELL_LOCK")
        self.assertEqual(entry.locked_quantity_delta, 100)
        self.assertEqual(position.locked_quantity, 100)
        self.assertEqual(position.sellable_quantity, 0)
        with self.assertRaises(InsufficientSellableQuantityError):
            ledger.lock_for_sell("000001", 1, trade_date=date(2026, 6, 30))
        _assert_position_aggregates_are_lot_derived(self, position)

    def test_sell_lot_size_allows_round_lot_quantity(self) -> None:
        ledger = _ledger_with_unlocked_lot(quantity=250)

        ledger.lock_for_sell("000001", 200, trade_date=date(2026, 6, 30))

        position = ledger.positions["000001"]
        self.assertEqual(position.locked_quantity, 200)
        self.assertEqual(position.sellable_quantity, 50)

    def test_sell_lot_size_allows_full_position_with_odd_lot_tail(self) -> None:
        ledger = _ledger_with_unlocked_lot(quantity=150)

        ledger.lock_for_sell("000001", 150, trade_date=date(2026, 6, 30))

        position = ledger.positions["000001"]
        self.assertEqual(position.locked_quantity, 150)
        self.assertEqual(position.sellable_quantity, 0)

    def test_sell_lot_size_rejects_partial_odd_lot_sell(self) -> None:
        ledger = _ledger_with_unlocked_lot(quantity=250)
        before_positions = deepcopy(ledger.positions)
        before_entries = list(ledger.ledger_entries)

        with self.assertRaisesRegex(OddLotSellError, "ODD_LOT_SELL"):
            ledger.lock_for_sell("000001", 150, trade_date=date(2026, 6, 30))

        self.assertEqual(ledger.positions, before_positions)
        self.assertEqual(ledger.ledger_entries, before_entries)

    def test_filled_sell_consumes_locked_inventory(self) -> None:
        ledger = _ledger(initial_cash=Decimal("0.00"))
        ledger.positions["000001"] = PositionState(
            "000001",
            lots=[
                PositionLot(
                    quantity=100,
                    cost_basis=Decimal("1000.00"),
                    trade_date=date(2026, 6, 29),
                    sellable_from=date(2026, 6, 30),
                    is_unlocked=True,
                )
            ],
        )
        ledger.lock_for_sell("000001", 100, trade_date=date(2026, 6, 30))

        fill_entry = ledger.apply_fill(
            _fill(
                security_id="000001",
                side="sell",
                quantity=100,
                net_amount=Decimal("990.00"),
                execution_date=date(2026, 7, 1),
            )
        )

        position = ledger.positions["000001"]
        self.assertEqual(position.total_quantity, 0)
        self.assertEqual(position.locked_quantity, 0)
        self.assertEqual(position.sellable_quantity, 0)
        self.assertEqual(ledger.cash.available_cash, Decimal("990.00"))
        self.assertEqual(fill_entry.realized_pnl, Decimal("-10.00"))
        _assert_position_aggregates_are_lot_derived(self, position)

    def test_rejected_or_suspended_sell_releases_locked_inventory(self) -> None:
        for status in ("REJECTED", "SUSPENDED", "UNFILLED"):
            with self.subTest(status=status):
                ledger = _ledger(initial_cash=Decimal("0.00"))
                ledger.positions["000001"] = PositionState(
                    "000001",
                    lots=[
                        PositionLot(
                            quantity=100,
                            cost_basis=Decimal("1000.00"),
                            trade_date=date(2026, 6, 29),
                            sellable_from=date(2026, 6, 30),
                            is_unlocked=True,
                        )
                    ],
                )
                ledger.lock_for_sell("000001", 100, trade_date=date(2026, 6, 30))
                ledger.apply_fill(
                    _fill(
                        security_id="000001",
                        side="sell",
                        quantity=100,
                        net_amount=Decimal("990.00"),
                        status=status,
                    )
                )

                release = ledger.release_lock(
                    "000001", 100, trade_date=date(2026, 7, 1)
                )

                position = ledger.positions["000001"]
                self.assertEqual(release.event_type, "SELL_LOCK_RELEASE")
                self.assertEqual(position.locked_quantity, 0)
                self.assertEqual(position.sellable_quantity, 100)
                _assert_position_aggregates_are_lot_derived(self, position)

    def test_non_filled_entries_do_not_change_any_ledger_state(self) -> None:
        for status in ("REJECTED", "SUSPENDED", "UNFILLED"):
            with self.subTest(status=status):
                ledger = _ledger(initial_cash=Decimal("10000.00"))
                before_cash = deepcopy(ledger.cash)
                before_positions = deepcopy(ledger.positions)
                before_entries = list(ledger.ledger_entries)

                result = ledger.apply_fill(
                    _fill(
                        security_id="000001",
                        side="buy",
                        quantity=100,
                        net_amount=Decimal("1005.01"),
                        status=status,
                    )
                )

                self.assertIsNone(result)
                self.assertEqual(ledger.cash, before_cash)
                self.assertEqual(ledger.positions, before_positions)
                self.assertEqual(ledger.ledger_entries, before_entries)

    def test_invariants_hold_after_operation_sequence(self) -> None:
        ledger = _ledger(initial_cash=Decimal("10000.00"))
        fills = [
            _fill("000001", "buy", 100, Decimal("1005.01"), date(2026, 6, 30)),
            _fill("000001", "buy", 100, Decimal("1205.01"), date(2026, 7, 1)),
            _fill("000001", "sell", 120, Decimal("1430.00"), date(2026, 7, 2)),
            _fill("000002", "buy", 200, Decimal("2005.02"), date(2026, 7, 2)),
        ]

        for fill in fills:
            if fill.order_intent.side == "sell":
                ledger.unlock_positions(fill.execution_date)
            ledger.apply_fill(fill)
            ledger.assert_invariants()

        for position in ledger.positions.values():
            _assert_position_aggregates_are_lot_derived(self, position)

    def test_decimal_money_values_do_not_accumulate_float_drift(self) -> None:
        ledger = _ledger(initial_cash=Decimal("1.00"))

        ledger.apply_fill(
            _fill(
                security_id="000001",
                side="buy",
                quantity=3,
                net_amount=Decimal("0.30"),
                execution_date=date(2026, 6, 30),
            )
        )
        ledger.unlock_positions(date(2026, 7, 1))
        ledger.apply_fill(
            _fill(
                security_id="000001",
                side="sell",
                quantity=1,
                net_amount=Decimal("0.10"),
                execution_date=date(2026, 7, 1),
            )
        )

        position = ledger.positions["000001"]
        self.assertEqual(ledger.cash.available_cash, Decimal("0.80"))
        self.assertEqual(position.cost_basis, Decimal("0.20"))
        self.assertEqual(ledger.ledger_entries[-1].realized_pnl, Decimal("0.00"))
        self.assertIsInstance(ledger.cash.available_cash, Decimal)
        _assert_position_aggregates_are_lot_derived(self, position)


def _ledger(initial_cash: Decimal) -> PortfolioLedger:
    return PortfolioLedger(
        cash=CashState(settled_cash=initial_cash, available_cash=initial_cash),
        calendar=trading_calendar_from_dates(
            [
                date(2026, 6, 29),
                date(2026, 6, 30),
                date(2026, 7, 1),
                date(2026, 7, 2),
                date(2026, 7, 3),
            ]
        ),
    )


def _ledger_with_unlocked_lot(quantity: int) -> PortfolioLedger:
    ledger = _ledger(initial_cash=Decimal("0.00"))
    ledger.positions["000001"] = PositionState(
        "000001",
        lots=[
            PositionLot(
                quantity=quantity,
                cost_basis=Decimal(quantity) * Decimal("10.00"),
                trade_date=date(2026, 6, 29),
                sellable_from=date(2026, 6, 30),
                is_unlocked=True,
            )
        ],
    )
    return ledger


def _fill(
    security_id: str,
    side: str,
    quantity: int,
    net_amount: Decimal,
    execution_date: date = date(2026, 6, 30),
    status: str = "FILLED",
) -> FillLedgerEntry:
    return FillLedgerEntry(
        order_intent=OrderIntent(
            security_id=security_id,
            side=side,
            quantity=quantity,
            decision_date=date(2026, 6, 29),
            reason="manual_fixture",
        ),
        intent_date=date(2026, 6, 29),
        execution_date=execution_date,
        execution_price=10.0,
        filled_quantity=quantity if status == "FILLED" else 0,
        status=status,
        reason=f"MANUAL_{status}",
        requested_quantity=quantity,
        gross_amount=net_amount,
        net_amount=net_amount,
    )


def _assert_position_aggregates_are_lot_derived(
    test_case: unittest.TestCase, position: PositionState
) -> None:
    test_case.assertEqual(
        position.total_quantity, sum(lot.quantity for lot in position.lots)
    )
    test_case.assertEqual(
        position.locked_quantity, sum(lot.locked_quantity for lot in position.lots)
    )
    test_case.assertEqual(
        position.sellable_quantity,
        sum(
            lot.quantity - lot.locked_quantity
            for lot in position.lots
            if lot.is_unlocked
        ),
    )
    test_case.assertEqual(
        position.unsellable_quantity,
        sum(lot.quantity for lot in position.lots if not lot.is_unlocked),
    )
    test_case.assertEqual(
        position.locked_quantity + position.sellable_quantity + position.unsellable_quantity,
        position.total_quantity,
    )
    test_case.assertEqual(
        position.cost_basis,
        sum((lot.cost_basis for lot in position.lots), Decimal("0.00")).quantize(
            Decimal("0.01")
        ),
    )


if __name__ == "__main__":
    unittest.main()
