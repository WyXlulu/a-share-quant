from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

import pandas as pd

from src.domain import DataContractError
from src.engine.backtest_runner import CachedPITDataPortal
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.portfolio_ledger import (
    CashState,
    PortfolioLedger,
    PositionLot,
    PositionState,
)
from src.market_calendar import TradingCalendar


AUDIT_QUANTITY = 1000
AUDIT_TOTAL_COST_BASIS = Decimal("10000.00")
CASH_AUDIT_KEYS = (
    ("000651", date(2021, 8, 23)),
    ("000651", date(2022, 4, 8)),
    ("000333", date(2023, 6, 1)),
)
STOCK_AUDIT_KEYS = (
    ("600276", date(2020, 5, 25)),
    ("600276", date(2021, 6, 10)),
)


@dataclass(frozen=True)
class CashDividendAuditResult:
    security_id: str
    ex_date: date
    prior_position: int
    actual_per_share: Decimal
    ex_right_deduction_per_share: Decimal
    expected_receivable_cash_delta: Decimal
    actual_receivable_cash_delta: Decimal


@dataclass(frozen=True)
class StockDividendAuditResult:
    security_id: str
    ex_date: date
    prior_position: int
    share_ratio: Decimal
    expected_share_delta: int
    actual_share_delta: int
    total_cost_basis_before: Decimal
    total_cost_basis_after: Decimal
    cost_basis_delta: Decimal
    per_share_cost_before: Decimal
    per_share_cost_after: Decimal
    new_lot_sellable_from: date
    new_lot_is_unlocked: bool


@dataclass(frozen=True)
class DeterministicLedgerAuditResult:
    cash_dividends: tuple[CashDividendAuditResult, ...]
    stock_dividends: tuple[StockDividendAuditResult, ...]


def run_deterministic_ledger_audit(
    *,
    corporate_action_path: Path,
    calendar: TradingCalendar,
) -> DeterministicLedgerAuditResult:
    actions = pd.read_parquet(corporate_action_path)
    required = {
        "security_id",
        "ex_date",
        "cash_dividend_per_share",
        "ex_right_cash_deduction_per_share",
        "share_ratio",
    }
    missing = sorted(required - set(actions.columns))
    if missing:
        raise DataContractError(
            f"deterministic ledger audit CA input missing columns: {missing}"
        )
    portal = CachedPITDataPortal(
        {"corporate_actions": corporate_action_path},
        calendar,
    )
    handler = CorporateActionHandler(calendar, portal)
    by_key = {
        (
            str(row.security_id).zfill(6),
            pd.Timestamp(row.ex_date).date(),
        ): row
        for row in actions.itertuples(index=False)
    }

    cash_results = tuple(
        _audit_cash_event(handler, by_key, security_id, ex_date)
        for security_id, ex_date in CASH_AUDIT_KEYS
    )
    stock_results = tuple(
        _audit_stock_event(handler, by_key, security_id, ex_date)
        for security_id, ex_date in STOCK_AUDIT_KEYS
    )
    return DeterministicLedgerAuditResult(cash_results, stock_results)


def _audit_cash_event(
    handler: CorporateActionHandler,
    by_key: dict[tuple[str, date], Any],
    security_id: str,
    ex_date: date,
) -> CashDividendAuditResult:
    action = _required_action(by_key, security_id, ex_date)
    actual_per_share = Decimal(str(action.cash_dividend_per_share))
    deduction = Decimal(str(action.ex_right_cash_deduction_per_share))
    if actual_per_share == deduction:
        raise DataContractError(
            f"cash audit event must exercise distinct fields: {security_id}/{ex_date}"
        )
    ledger = _ledger_with_position(security_id)
    receivable_before = ledger.cash.receivable_cash
    handler.process_day(ledger, ex_date)
    actual_delta = ledger.cash.receivable_cash - receivable_before
    expected_delta = _money(actual_per_share * Decimal(AUDIT_QUANTITY))
    deduction_delta = _money(deduction * Decimal(AUDIT_QUANTITY))
    if actual_delta != expected_delta:
        raise DataContractError(
            "cash ledger did not consume actual entitlement: "
            f"{security_id}/{ex_date}; expected={expected_delta}, actual={actual_delta}"
        )
    if actual_delta == deduction_delta:
        raise DataContractError(
            "cash ledger silently consumed ex-right deduction instead of actual entitlement"
        )
    return CashDividendAuditResult(
        security_id=security_id,
        ex_date=ex_date,
        prior_position=AUDIT_QUANTITY,
        actual_per_share=actual_per_share,
        ex_right_deduction_per_share=deduction,
        expected_receivable_cash_delta=expected_delta,
        actual_receivable_cash_delta=actual_delta,
    )


def _audit_stock_event(
    handler: CorporateActionHandler,
    by_key: dict[tuple[str, date], Any],
    security_id: str,
    ex_date: date,
) -> StockDividendAuditResult:
    action = _required_action(by_key, security_id, ex_date)
    ratio = Decimal(str(action.share_ratio))
    if ratio != Decimal("0.2"):
        raise DataContractError(
            f"stock audit requires verified 0.2 ratio: {security_id}/{ex_date}"
        )
    ledger = _ledger_with_position(security_id)
    position = ledger.positions[security_id]
    total_cost_before = position.cost_basis
    per_share_before = total_cost_before / Decimal(position.total_quantity)
    entries = handler.process_day(ledger, ex_date)
    stock_entries = [
        entry for entry in entries if entry.event_type == "CA_SHARES_ADJUSTED"
    ]
    if len(stock_entries) != 1:
        raise DataContractError(
            f"stock audit expected one share adjustment: {security_id}/{ex_date}"
        )
    entry = stock_entries[0]
    expected_share_delta = int(Decimal(AUDIT_QUANTITY) * ratio)
    if entry.quantity_delta != expected_share_delta:
        raise DataContractError(
            f"stock dividend share delta mismatch: {security_id}/{ex_date}"
        )
    total_cost_after = position.cost_basis
    if total_cost_after != total_cost_before or entry.cost_basis_delta != Decimal("0.00"):
        raise DataContractError(
            f"stock dividend changed total cost basis: {security_id}/{ex_date}"
        )
    new_lots = [
        lot
        for lot in position.lots
        if lot.source == "STOCK_DIVIDEND" and lot.trade_date == ex_date
    ]
    if len(new_lots) != 1:
        raise DataContractError(
            f"stock audit expected one new stock-dividend lot: {security_id}/{ex_date}"
        )
    new_lot = new_lots[0]
    if new_lot.sellable_from != ex_date or not new_lot.is_unlocked:
        raise DataContractError(
            f"stock dividend lot is not immediately sellable: {security_id}/{ex_date}"
        )
    per_share_after = total_cost_after / Decimal(position.total_quantity)
    if per_share_after >= per_share_before:
        raise DataContractError(
            f"stock dividend did not dilute per-share cost: {security_id}/{ex_date}"
        )
    return StockDividendAuditResult(
        security_id=security_id,
        ex_date=ex_date,
        prior_position=AUDIT_QUANTITY,
        share_ratio=ratio,
        expected_share_delta=expected_share_delta,
        actual_share_delta=entry.quantity_delta,
        total_cost_basis_before=total_cost_before,
        total_cost_basis_after=total_cost_after,
        cost_basis_delta=entry.cost_basis_delta,
        per_share_cost_before=per_share_before,
        per_share_cost_after=per_share_after,
        new_lot_sellable_from=new_lot.sellable_from,
        new_lot_is_unlocked=new_lot.is_unlocked,
    )


def _required_action(
    by_key: dict[tuple[str, date], Any],
    security_id: str,
    ex_date: date,
) -> Any:
    try:
        return by_key[(security_id, ex_date)]
    except KeyError as exc:
        raise DataContractError(
            f"deterministic ledger audit event is missing: {security_id}/{ex_date}"
        ) from exc


def _ledger_with_position(security_id: str) -> PortfolioLedger:
    position = PositionState(
        security_id,
        [
            PositionLot(
                quantity=AUDIT_QUANTITY,
                cost_basis=AUDIT_TOTAL_COST_BASIS,
                trade_date=date(2019, 1, 1),
                sellable_from=date(2019, 1, 2),
                is_unlocked=True,
            )
        ],
    )
    return PortfolioLedger(
        CashState(),
        positions={security_id: position},
    )


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
