from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState
from src.market_calendar import trading_calendar_from_dates


class CorporateActionHandlerTest(unittest.TestCase):
    def test_ex011_cash_dividend_accrues_then_pays_without_changing_cash_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _handler(tmpdir, [_ca_row("000001", _T0, "CASH_DIVIDEND", cash=0.30)])
            ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))

            ex_entries = handler.process_day(ledger, _T0)
            cash_total_after_accrual = ledger.cash.available_cash + ledger.cash.receivable_cash
            pay_entries = handler.process_day(ledger, _T1)
            cash_total_after_payment = ledger.cash.available_cash + ledger.cash.receivable_cash

            self.assertEqual([entry.event_type for entry in ex_entries], ["CA_DIVIDEND_ACCRUED"])
            self.assertEqual([entry.event_type for entry in pay_entries], ["CA_DIVIDEND_PAID"])
            self.assertEqual(ledger.cash.receivable_cash, Decimal("0.00"))
            self.assertEqual(ledger.cash.available_cash, Decimal("400.00"))
            self.assertEqual(cash_total_after_accrual, Decimal("400.00"))
            self.assertEqual(cash_total_after_payment, cash_total_after_accrual)

    def test_ex012_stock_dividend_adjusts_quantity_without_changing_cost_basis(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _handler(tmpdir, [_ca_row("000001", _T0, "STOCK_DIVIDEND", share_ratio=0.20)])
            ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))

            entries = handler.process_day(ledger, _T0)
            position = ledger.positions["000001"]

            self.assertEqual([entry.event_type for entry in entries], ["CA_SHARES_ADJUSTED"])
            self.assertEqual(position.total_quantity, 1200)
            self.assertEqual(position.cost_basis, Decimal("10000.00"))
            self.assertEqual(_average_cost(position), Decimal("8.333333333333333333333333333"))
            self.assertEqual(position.sellable_quantity, 1200)
            self.assertEqual(position.lots[-1].source, "STOCK_DIVIDEND")
            self.assertEqual(position.lots[-1].cost_basis, Decimal("0.00"))
            ledger.assert_invariants()

    def test_ex013_rights_issue_taints_security_and_backtest_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _handler(tmpdir, [_ca_row("000001", _T0, "RIGHTS_ISSUE")])
            ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))

            entries = handler.process_day(ledger, _T0)
            next_day_entries = handler.process_day(ledger, _T1)

            self.assertEqual([entry.event_type for entry in entries], ["UNSUPPORTED_CORPORATE_EVENT"])
            self.assertIn("000001", ledger.tainted_securities)
            self.assertEqual(next_day_entries, [])
            self.assertEqual(ledger.positions["000001"].total_quantity, 1000)

    def test_same_day_late_visible_ca_is_marked_unprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _handler(
                tmpdir,
                [
                    _ca_row(
                        "000001",
                        _T0,
                        "CASH_DIVIDEND",
                        cash=0.30,
                        available_at="2026-01-05T15:00:00+08:00",
                    )
                ],
            )
            ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))

            entries = handler.process_day(ledger, _T0)

            self.assertEqual([entry.event_type for entry in entries], ["UNPROCESSED_CA"])
            self.assertIn("000001", ledger.tainted_securities)
            self.assertEqual(ledger.cash.receivable_cash, Decimal("0.00"))

    def test_ex014_future_ca_revision_does_not_change_cutoff_closed_ledger(self) -> None:
        original = [_ca_row("000001", _T0, "CASH_DIVIDEND", cash=0.10)]
        revised = original + [
            _ca_row(
                "000001",
                _T0,
                "CASH_DIVIDEND",
                cash=0.90,
                available_at="2026-01-07T15:00:00+08:00",
            )
        ]

        original_ledger = _run_until_cutoff(original)
        revised_ledger = _run_until_cutoff(revised)

        self.assertEqual(_event_snapshot(original_ledger), _event_snapshot(revised_ledger))
        self.assertEqual(original_ledger.cash, revised_ledger.cash)
        self.assertEqual(original_ledger.positions, revised_ledger.positions)

    def test_ca_without_position_creates_no_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handler = _handler(tmpdir, [_ca_row("000002", _T0, "CASH_DIVIDEND", cash=0.30)])
            ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))

            entries = handler.process_day(ledger, _T0)

            self.assertEqual(entries, [])
            self.assertEqual(ledger.ledger_entries, [])
            self.assertEqual(ledger.cash.receivable_cash, Decimal("0.00"))


def _run_until_cutoff(rows: list[dict[str, object]]) -> PortfolioLedger:
    with tempfile.TemporaryDirectory() as tmpdir:
        handler = _handler(tmpdir, rows)
        ledger = _ledger_with_position(quantity=1000, cost_basis=Decimal("10000.00"))
        handler.process_day(ledger, _T0)
        handler.process_day(ledger, _T1)
        return ledger


def _handler(tmpdir: str, rows: list[dict[str, object]]) -> CorporateActionHandler:
    path = Path(tmpdir) / "corporate_actions.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    portal = PITDataPortal({"corporate_actions": path})
    return CorporateActionHandler(_calendar(), portal)


def _ledger_with_position(quantity: int, cost_basis: Decimal) -> PortfolioLedger:
    ledger = PortfolioLedger(
        CashState(settled_cash=Decimal("100.00"), available_cash=Decimal("100.00")),
        calendar=_calendar(),
    )
    ledger.positions["000001"] = PositionState(
        "000001",
        [
            PositionLot(
                quantity=quantity,
                cost_basis=cost_basis,
                trade_date=date(2026, 1, 2),
                sellable_from=_T0,
                is_unlocked=True,
            )
        ],
    )
    return ledger


def _ca_row(
    security_id: str,
    ex_date: date,
    action_type: str,
    *,
    cash: float = 0.0,
    share_ratio: float = 0.0,
    available_at: str = "2026-01-02T15:00:00+08:00",
) -> dict[str, object]:
    ex_date_str = ex_date.isoformat()
    return {
        "security_id": security_id,
        "ex_date": f"{ex_date_str}T00:00:00+08:00",
        "action_type": action_type,
        "cash_dividend_per_share": cash,
        "share_ratio": share_ratio,
        "event_ts": f"{ex_date_str}T15:00:00+08:00",
        "available_at": available_at,
        "source_id": "fixture",
        "snapshot_id": "fixture",
    }


def _calendar():
    return trading_calendar_from_dates([date(2026, 1, 2), _T0, _T1, date(2026, 1, 7)])


def _average_cost(position: PositionState) -> Decimal:
    return position.cost_basis / Decimal(position.total_quantity)


def _event_snapshot(ledger: PortfolioLedger) -> list[tuple[object, ...]]:
    return [
        (
            entry.event_type,
            entry.security_id,
            entry.trade_date,
            entry.quantity_delta,
            entry.cash_delta,
            entry.cost_basis_delta,
            entry.fill_reason,
            entry.position_quantity_after,
            entry.available_cash_after,
        )
        for entry in ledger.ledger_entries
    ]


_T0 = date(2026, 1, 5)
_T1 = date(2026, 1, 6)


if __name__ == "__main__":
    unittest.main()
