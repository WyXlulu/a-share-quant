from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from src.market_calendar import trading_calendar_from_dates
from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine import DummyStrategy, EventDrivenClock, OrderIntent
from src.engine.dummy_strategy import DummyRebalanceStrategy
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState


class DummyStrategyTest(unittest.TestCase):
    def test_reproducible_order_intents_for_same_dates_and_data(self) -> None:
        first = _run_strategy()
        second = _run_strategy()

        self.assertEqual(first, second)
        self.assertTrue(all(isinstance(intent, OrderIntent) for intent in first))

    def test_orders_only_on_rebalance_days(self) -> None:
        by_date = _run_strategy_by_date(target_count=3, rebalance_every_n_days=2)

        self.assertEqual(len(by_date[date(2026, 6, 29)]), 3)
        self.assertEqual(len(by_date[date(2026, 6, 30)]), 0)
        self.assertEqual(len(by_date[date(2026, 7, 1)]), 3)

    def test_selection_uses_only_same_day_visible_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "daily_bar_raw.parquet"
            pd.DataFrame(_future_listing_rows()).to_parquet(table_path, index=False)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})
            calendar = trading_calendar_from_dates([date(2026, 6, 30), date(2026, 7, 1)])
            clock = EventDrivenClock("2026-06-30", "2026-07-01", calendar, portal)
            strategy = DummyStrategy(rebalance_every_n_days=1, target_count=2, order_quantity=100)
            by_date: dict[date, list[OrderIntent]] = {}

            def collect(ctx) -> None:
                by_date[ctx.trade_date] = strategy.on_bar(ctx)

            clock.run(collect)

            early_ids = [intent.security_id for intent in by_date[date(2026, 6, 30)]]
            later_ids = [intent.security_id for intent in by_date[date(2026, 7, 1)]]

            self.assertEqual(early_ids, ["000001"])
            self.assertEqual(later_ids, ["000001", "000002"])

    def test_quantity_is_positive_100_share_lot_multiple(self) -> None:
        intents = _run_strategy(target_count=4, order_quantity=200)

        self.assertTrue(intents)
        self.assertTrue(all(intent.quantity > 0 for intent in intents))
        self.assertTrue(all(intent.quantity % 100 == 0 for intent in intents))

    def test_rebalance_strategy_sells_holdings_outside_target_and_buys_missing_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            table_path = Path(tmpdir) / "daily_bar_raw.parquet"
            pd.DataFrame(_daily_bar_rows()).to_parquet(table_path, index=False)
            portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})
            calendar = _calendar()
            clock = EventDrivenClock("2026-06-29", "2026-06-29", calendar, portal)
            ledger = PortfolioLedger(CashState(), calendar=calendar)
            ledger.positions["000003"] = PositionState(
                "000003",
                lots=[
                    PositionLot(
                        quantity=100,
                        cost_basis=1000,
                        trade_date=date(2026, 6, 26),
                        sellable_from=date(2026, 6, 29),
                    )
                ],
            )
            ledger.unlock_positions(date(2026, 6, 29))
            strategy = DummyRebalanceStrategy(
                rebalance_every_n_days=1,
                target_count=2,
                order_quantity=100,
                portfolio_ledger=ledger,
            )
            by_date: dict[date, list[OrderIntent]] = {}

            clock.run(lambda ctx: by_date.setdefault(ctx.trade_date, strategy.on_bar(ctx)))

            intents = by_date[date(2026, 6, 29)]
            self.assertEqual(
                [(intent.security_id, intent.side, intent.quantity) for intent in intents],
                [
                    ("000003", "sell", 100),
                    ("000001", "buy", 100),
                    ("000002", "buy", 100),
                ],
            )


def _run_strategy(
    target_count: int = 3,
    rebalance_every_n_days: int = 2,
    order_quantity: int = 100,
) -> list[OrderIntent]:
    by_date = _run_strategy_by_date(target_count, rebalance_every_n_days, order_quantity)
    return [intent for intents in by_date.values() for intent in intents]


def _run_strategy_by_date(
    target_count: int = 3,
    rebalance_every_n_days: int = 2,
    order_quantity: int = 100,
) -> dict[date, list[OrderIntent]]:
    with tempfile.TemporaryDirectory() as tmpdir:
        table_path = Path(tmpdir) / "daily_bar_raw.parquet"
        pd.DataFrame(_daily_bar_rows()).to_parquet(table_path, index=False)
        portal = PITDataPortal({"daily_bar_raw": table_path, "security_master": table_path})
        calendar = _calendar()
        clock = EventDrivenClock("2026-06-29", "2026-07-01", calendar, portal)
        strategy = DummyStrategy(
            rebalance_every_n_days=rebalance_every_n_days,
            target_count=target_count,
            order_quantity=order_quantity,
        )
        by_date: dict[date, list[OrderIntent]] = {}

        def collect(ctx) -> None:
            by_date[ctx.trade_date] = strategy.on_bar(ctx)

        clock.run(collect)
        return by_date


def _calendar():
    return trading_calendar_from_dates(
        [
            date(2026, 6, 29),
            date(2026, 6, 30),
            date(2026, 7, 1),
        ]
    )


def _daily_bar_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for trade_date in ("2026-06-29", "2026-06-30", "2026-07-01"):
        for security_id in ("000003", "000001", "000002", "000004"):
            rows.append(_daily_bar_row(security_id, trade_date, TradeStatus.NORMAL.value))
        rows.append(_daily_bar_row("000000", trade_date, TradeStatus.SUSPENDED.value))
    return rows


def _future_listing_rows() -> list[dict[str, object]]:
    return [
        _daily_bar_row("000001", "2026-06-30", TradeStatus.NORMAL.value),
        _daily_bar_row("000001", "2026-07-01", TradeStatus.NORMAL.value),
        _daily_bar_row("000002", "2026-07-01", TradeStatus.NORMAL.value),
    ]


def _daily_bar_row(security_id: str, trade_date: str, trade_status: str) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date,
        "trade_status": trade_status,
        "event_ts": f"{trade_date}T15:00:00+08:00",
        "available_at": f"{trade_date}T15:00:00+08:00",
        "snapshot_id": "fixture",
    }


if __name__ == "__main__":
    unittest.main()
