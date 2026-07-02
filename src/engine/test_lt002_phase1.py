from __future__ import annotations

import tempfile
import unittest
from dataclasses import astuple
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine.backtest_runner import BacktestConfig, BacktestRunner
from src.engine.execution import LimitRuleTable
from src.market_calendar import trading_calendar_from_dates


class LT002Phase1ScopeTest(unittest.TestCase):
    def test_phase1_closed_outputs_do_not_change_when_post_cutoff_l1_and_l2_mutate(self) -> None:
        cutoff = date(2026, 1, 6)
        original = _run_fixture(mutate_after_cutoff=False)
        mutated = _run_fixture(mutate_after_cutoff=True)

        self.assertEqual(_locked_snapshot(original.locked_orders, cutoff), _locked_snapshot(mutated.locked_orders, cutoff))
        self.assertEqual(_fill_snapshot(original.fills, cutoff), _fill_snapshot(mutated.fills, cutoff))
        self.assertEqual(_event_snapshot(original.ledger_entries, cutoff), _event_snapshot(mutated.ledger_entries, cutoff))
        self.assertEqual(_nav_snapshot(original.nav_rows, cutoff), _nav_snapshot(mutated.nav_rows, cutoff))


def _run_fixture(*, mutate_after_cutoff: bool):
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        daily_path = tmp / "daily_bar_raw.parquet"
        master_path = tmp / "security_master.parquet"
        ca_path = tmp / "corporate_actions.parquet"
        pd.DataFrame(_daily_rows(mutate_after_cutoff=mutate_after_cutoff)).to_parquet(daily_path, index=False)
        pd.DataFrame(_security_master_rows()).to_parquet(master_path, index=False)
        pd.DataFrame(_corporate_action_rows(mutate_after_cutoff=mutate_after_cutoff)).to_parquet(ca_path, index=False)

        calendar = trading_calendar_from_dates(_TRADE_DATES)
        portal = PITDataPortal(
            {
                "daily_bar_raw": daily_path,
                "security_master": master_path,
                "corporate_actions": ca_path,
            }
        )
        config = BacktestConfig(
            start_date=_TRADE_DATES[0],
            end_date=_TRADE_DATES[-1],
            initial_cash=Decimal("100000.00"),
            rebalance_every_n_days=5,
            target_count=3,
            order_quantity=100,
            table_paths={
                "daily_bar_raw": daily_path,
                "security_master": master_path,
                "corporate_actions": ca_path,
            },
            calendar_path=tmp / "unused_calendar.parquet",
            output_dir=tmp / "output",
        )
        return BacktestRunner(config, calendar=calendar, portal=portal).run()


def _daily_rows(*, mutate_after_cutoff: bool) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    base_closes = {
        "000001": [10.00, 10.20, 9.80, 9.70, 9.60],
        "000002": [10.00, 11.00, 10.80, 10.70, 10.60],
        "000003": [10.00, None, 10.10, 10.20, 10.30],
    }
    for index, trade_date in enumerate(_TRADE_DATES):
        trade_date_text = trade_date.isoformat()
        for security_id in ("000001", "000002", "000003"):
            close = base_closes[security_id][index]
            open_price = close
            status = TradeStatus.NORMAL.value
            if security_id == "000003" and trade_date == date(2026, 1, 5):
                open_price = None
                status = TradeStatus.SUSPENDED.value
            if mutate_after_cutoff and trade_date > date(2026, 1, 6):
                close = None if close is None else close + 5.0
                open_price = None if open_price is None else open_price + 5.0
            rows.append(
                _bar_row(
                    security_id,
                    trade_date_text,
                    open_price=open_price,
                    close=close,
                    trade_status=status,
                )
            )
    return rows


def _bar_row(
    security_id: str,
    trade_date: str,
    *,
    open_price: float | None,
    close: float | None,
    trade_status: str,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date,
        "open": open_price,
        "high": open_price,
        "low": open_price,
        "close": close,
        "volume": 999999,
        "amount": 100000000.0,
        "trade_status": trade_status,
        "event_ts": f"{trade_date}T15:00:00+08:00",
        "available_at": f"{trade_date}T15:00:00+08:00",
        "snapshot_id": "lt002_fixture",
    }


def _security_master_rows() -> list[dict[str, object]]:
    board = next(iter(LimitRuleTable().rules_by_board))
    return [
        {
            "security_id": security_id,
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "lt002_fixture",
        }
        for security_id in ("000001", "000002", "000003")
    ]


def _corporate_action_rows(*, mutate_after_cutoff: bool) -> list[dict[str, object]]:
    rows = [
        _ca_row("000001", date(2026, 1, 6), "CASH_DIVIDEND", cash=0.20, share_ratio=0.0),
        _ca_row("000001", date(2026, 1, 6), "STOCK_DIVIDEND", cash=0.0, share_ratio=0.10),
    ]
    if mutate_after_cutoff:
        rows.append(
            _ca_row(
                "000001",
                date(2026, 1, 8),
                "CASH_DIVIDEND",
                cash=9.99,
                share_ratio=0.0,
                available_at="2026-01-07T15:00:00+08:00",
            )
        )
    return rows


def _ca_row(
    security_id: str,
    ex_date: date,
    action_type: str,
    *,
    cash: float,
    share_ratio: float,
    available_at: str = "2026-01-02T15:00:00+08:00",
) -> dict[str, object]:
    ex_date_text = ex_date.isoformat()
    return {
        "security_id": security_id,
        "ex_date": f"{ex_date_text}T00:00:00+08:00",
        "action_type": action_type,
        "cash_dividend_per_share": cash,
        "share_ratio": share_ratio,
        "event_ts": f"{ex_date_text}T15:00:00+08:00",
        "available_at": available_at,
        "source_id": "lt002_fixture",
        "snapshot_id": "lt002_fixture",
    }


def _locked_snapshot(orders, cutoff: date):
    return [
        (
            order.order_intent.security_id,
            order.order_intent.side,
            order.order_intent.quantity,
            order.order_intent.decision_date,
            order.locked_quantity,
            order.original_quantity,
            order.reference_price,
            order.price_cap,
            order.price_floor,
            order.reserved_cash,
            order.ruleset_version,
            order.ttl,
            order.limit_check,
            order.capacity_reason,
            order.adv_window_status,
            order.limit_reference_status,
        )
        for order in orders
        if order.order_intent.decision_date <= cutoff
    ]


def _fill_snapshot(fills, cutoff: date):
    return [
        (
            fill.order_intent.security_id,
            fill.order_intent.side,
            fill.order_intent.quantity,
            fill.intent_date,
            fill.execution_date,
            fill.execution_price,
            fill.filled_quantity,
            fill.status,
            fill.reason,
            fill.requested_quantity,
            fill.limit_check,
            fill.gross_amount,
            fill.commission,
            fill.stamp_duty,
            fill.transfer_fee,
            fill.total_fee,
            fill.net_amount,
            fill.reserved_cash,
            fill.capacity_reason,
            fill.adv_window_status,
            fill.limit_reference_status,
        )
        for fill in fills
        if fill.intent_date <= cutoff
    ]


def _event_snapshot(entries, cutoff: date):
    return [
        astuple(entry)
        for entry in entries
        if entry.trade_date <= cutoff
    ]


def _nav_snapshot(rows, cutoff: date):
    return [
        (
            row.trade_date,
            row.nav,
            row.cash,
            row.holdings_market_value,
            row.event_count,
            row.realized_pnl,
            row.unrealized_pnl,
            row.dividend_accrued,
            row.fees,
        )
        for row in rows
        if row.trade_date <= cutoff
    ]


_TRADE_DATES = [
    date(2026, 1, 2),
    date(2026, 1, 5),
    date(2026, 1, 6),
    date(2026, 1, 7),
    date(2026, 1, 8),
]


if __name__ == "__main__":
    unittest.main()
