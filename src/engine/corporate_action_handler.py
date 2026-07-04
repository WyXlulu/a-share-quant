from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import PITDataPortal
from src.domain import (
    CorporateActionVisibilityStatus,
    DataContractError,
    SUPPORTED_LEDGER_ACTION_TYPES,
    evaluate_corporate_action_visibility,
)
from src.engine.portfolio_ledger import PortfolioLedger, PortfolioLedgerEntry
from src.market_calendar import TradingCalendar


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
CA_DAY_CUTOVER_TIME = time(9, 0, 0)
CA_DAY_CLOSE_TIME = time(15, 0, 0)


@dataclass(frozen=True)
class CorporateActionHandler:
    calendar: TradingCalendar
    portal: PITDataPortal

    def process_day(
        self,
        ledger: PortfolioLedger,
        trade_date: date,
    ) -> list[PortfolioLedgerEntry]:
        """Apply corporate actions after unlock_positions and before same-day trading.

        Cash dividends use a tax-gross simplification already chosen for the project:
        ex-date accrues receivable cash, and payment is simplified to the first
        trading day after ex-date because the current ledger has no payment-date field.
        Real payment dates vary by company and must be corrected when a richer data
        source is available.
        """
        entries: list[PortfolioLedgerEntry] = []
        entries.extend(self._pay_due_cash_dividends(ledger, trade_date))

        actions = self._visible_actions(trade_date)
        if actions is None:
            return entries + self._mark_unprocessed_for_positions(
                ledger,
                trade_date,
                "CORPORATE_ACTION_TABLE_UNAVAILABLE",
            )
        if actions.empty:
            entries.extend(self._mark_same_day_unavailable_actions(ledger, trade_date, actions))
            return entries

        held_ids = {
            security_id
            for security_id, position in ledger.positions.items()
            if position.total_quantity > 0
        }
        if not held_ids:
            return entries

        entries.extend(self._mark_same_day_unavailable_actions(ledger, trade_date, actions))
        actions = actions.loc[actions["security_id"].astype(str).str.zfill(6).isin(held_ids)].copy()
        if actions.empty:
            return entries

        actions = _latest_visible_revision(actions)
        cutover_asof = _ca_day_asof(trade_date, CA_DAY_CUTOVER_TIME)
        for row in actions.itertuples(index=False):
            security_id = str(getattr(row, "security_id")).zfill(6)
            visibility = evaluate_corporate_action_visibility(
                row,
                cutover_asof,
                supported_action_types=SUPPORTED_LEDGER_ACTION_TYPES,
            )
            cash_per_share = _decimal(getattr(row, "cash_dividend_per_share"))
            share_ratio = _decimal(getattr(row, "share_ratio"))

            if visibility.status == CorporateActionVisibilityStatus.UNSUPPORTED_TYPE:
                entry = ledger.mark_unsupported_corporate_event(
                    security_id,
                    trade_date,
                    str(getattr(row, "action_type")),
                )
                if entry is not None:
                    entries.append(entry)
                continue
            if visibility.status != CorporateActionVisibilityStatus.VISIBLE_APPLICABLE:
                entry = ledger.mark_unprocessed_corporate_action(
                    security_id,
                    trade_date,
                    visibility.reason,
                )
                if entry is not None:
                    entries.append(entry)
                continue

            if cash_per_share > Decimal("0"):
                entry = ledger.accrue_cash_dividend(
                    security_id,
                    trade_date,
                    cash_per_share,
                )
                if entry is not None:
                    entries.append(entry)

            if share_ratio > Decimal("0"):
                entry = ledger.apply_stock_dividend(
                    security_id,
                    trade_date,
                    share_ratio,
                )
                if entry is not None:
                    entries.append(entry)

        return entries

    def _pay_due_cash_dividends(
        self,
        ledger: PortfolioLedger,
        trade_date: date,
    ) -> list[PortfolioLedgerEntry]:
        entries: list[PortfolioLedgerEntry] = []
        pending_keys = sorted(ledger.pending_cash_dividends)
        for security_id, ex_date in pending_keys:
            try:
                pay_date = self.calendar.next_trading_day(ex_date)
            except IndexError:
                continue
            if pay_date != trade_date:
                continue
            entry = ledger.pay_cash_dividend(security_id, ex_date, trade_date)
            if entry is not None:
                entries.append(entry)
        return entries

    def _visible_actions(self, trade_date: date) -> pd.DataFrame | None:
        asof_ts = _ca_day_asof(trade_date, CA_DAY_CUTOVER_TIME)
        try:
            rows = self.portal.query(
                "corporate_actions",
                asof_ts,
                columns=[
                    "security_id",
                    "ex_date",
                    "action_type",
                    "cash_dividend_per_share",
                    "share_ratio",
                    "available_at",
                ],
            )
        except DataContractError:
            return None
        if rows.empty:
            return rows

        ex_dates = pd.to_datetime(rows["ex_date"], errors="raise").dt.date
        return rows.loc[ex_dates == trade_date].copy()

    def _mark_same_day_unavailable_actions(
        self,
        ledger: PortfolioLedger,
        trade_date: date,
        day_cutover_actions: pd.DataFrame,
    ) -> list[PortfolioLedgerEntry]:
        day_close_actions = self._actions_visible_at(trade_date, CA_DAY_CLOSE_TIME)
        if day_close_actions.empty:
            return []

        cutover_keys = _action_keys(day_cutover_actions)
        cutover_asof = _ca_day_asof(trade_date, CA_DAY_CUTOVER_TIME)
        held_ids = {
            security_id
            for security_id, position in ledger.positions.items()
            if position.total_quantity > 0
        }
        entries: list[PortfolioLedgerEntry] = []
        for row in day_close_actions.itertuples(index=False):
            security_id = str(getattr(row, "security_id")).zfill(6)
            action_type = str(getattr(row, "action_type"))
            action_key = (
                security_id,
                pd.Timestamp(getattr(row, "ex_date")).date(),
                action_type,
            )
            if security_id not in held_ids or action_key in cutover_keys:
                continue
            visibility = evaluate_corporate_action_visibility(
                row,
                cutover_asof,
                supported_action_types=SUPPORTED_LEDGER_ACTION_TYPES,
            )
            if visibility.status != CorporateActionVisibilityStatus.UNPROCESSED_BOUNDARY:
                continue
            entry = ledger.mark_unprocessed_corporate_action(
                security_id,
                trade_date,
                visibility.reason,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    def _actions_visible_at(self, trade_date: date, asof_time: time) -> pd.DataFrame:
        asof_ts = _ca_day_asof(trade_date, asof_time)
        try:
            rows = self.portal.query(
                "corporate_actions",
                asof_ts,
                columns=[
                    "security_id",
                    "ex_date",
                    "action_type",
                    "cash_dividend_per_share",
                    "share_ratio",
                    "available_at",
                ],
            )
        except DataContractError:
            return pd.DataFrame()
        if rows.empty:
            return rows
        ex_dates = pd.to_datetime(rows["ex_date"], errors="raise").dt.date
        return rows.loc[ex_dates == trade_date].copy()

    def _mark_unprocessed_for_positions(
        self,
        ledger: PortfolioLedger,
        trade_date: date,
        reason: str,
    ) -> list[PortfolioLedgerEntry]:
        entries: list[PortfolioLedgerEntry] = []
        for security_id, position in sorted(ledger.positions.items()):
            if position.total_quantity <= 0:
                continue
            entry = ledger.mark_unprocessed_corporate_action(
                security_id,
                trade_date,
                reason,
            )
            if entry is not None:
                entries.append(entry)
        return entries


def _latest_visible_revision(actions: pd.DataFrame) -> pd.DataFrame:
    actions = actions.copy()
    actions["_available_at_sort"] = pd.to_datetime(actions["available_at"], errors="raise")
    return (
        actions.sort_values(["security_id", "ex_date", "action_type", "_available_at_sort"])
        .drop_duplicates(["security_id", "ex_date", "action_type"], keep="last")
        .drop(columns=["_available_at_sort"])
        .reset_index(drop=True)
    )


def _action_keys(actions: pd.DataFrame) -> set[tuple[str, date, str]]:
    if actions.empty:
        return set()
    ex_dates = pd.to_datetime(actions["ex_date"], errors="raise").dt.date
    return {
        (str(security_id).zfill(6), ex_date, str(action_type))
        for security_id, ex_date, action_type in zip(
            actions["security_id"],
            ex_dates,
            actions["action_type"],
        )
    }


def _decimal(value: object) -> Decimal:
    if pd.isna(value):
        return Decimal("0")
    return Decimal(str(value))


def _ca_day_asof(trade_date: date, asof_time: time) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(trade_date, asof_time, tzinfo=ASIA_SHANGHAI))
