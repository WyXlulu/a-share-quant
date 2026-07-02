from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import TradeStatus
from src.engine.backtest_runner import BacktestConfig, BacktestRunner, CachedPITDataPortal
from src.engine.execution import LimitRuleTable
from src.market_calendar import trading_calendar_from_dates


class BacktestRunnerFastPathTest(unittest.TestCase):
    def test_cached_portal_matches_original_pit_portal_on_three_month_event_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _write_fixture(Path(tmpdir))
            calendar = trading_calendar_from_dates(_TRADE_DATES)
            config = BacktestConfig(
                start_date=_TRADE_DATES[0],
                end_date=_TRADE_DATES[-1],
                initial_cash=Decimal("100000.00"),
                rebalance_every_n_days=5,
                target_count=3,
                order_quantity=100,
                table_paths=paths,
                calendar_path=Path(tmpdir) / "unused_calendar.parquet",
                output_dir=Path(tmpdir) / "output",
            )

            slow = BacktestRunner(
                config,
                calendar=calendar,
                portal=PITDataPortal(paths),
            ).run()
            fast = BacktestRunner(
                config,
                calendar=calendar,
                portal=CachedPITDataPortal(paths),
            ).run()

        self.assertEqual(slow.locked_orders, fast.locked_orders)
        self.assertEqual(slow.fills, fast.fills)
        self.assertEqual(slow.ledger_entries, fast.ledger_entries)
        self.assertEqual(slow.nav_csv_bytes(), fast.nav_csv_bytes())
        self.assertTrue(_has_rebalance_orders(fast))
        self.assertTrue(_has_limit_rejection(fast))
        self.assertTrue(_has_suspended_fill(fast))
        self.assertTrue(_has_cash_dividend(fast))


def _write_fixture(tmp: Path) -> dict[str, Path]:
    daily_path = tmp / "daily_bar_raw.parquet"
    master_path = tmp / "security_master.parquet"
    ca_path = tmp / "corporate_actions.parquet"
    pd.DataFrame(_daily_rows()).to_parquet(daily_path, index=False)
    pd.DataFrame(_security_master_rows()).to_parquet(master_path, index=False)
    pd.DataFrame(_corporate_action_rows()).to_parquet(ca_path, index=False)
    return {
        "daily_bar_raw": daily_path,
        "security_master": master_path,
        "corporate_actions": ca_path,
    }


def _daily_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, trade_date in enumerate(_TRADE_DATES):
        for offset, security_id in enumerate(_SECURITY_IDS):
            close = 10.0 + offset + (index % 7) * 0.03
            open_price = close
            trade_status = TradeStatus.NORMAL.value
            if trade_date == date(2026, 1, 5) and security_id == "000002":
                open_price = 12.10
                close = 11.80
            if trade_date == date(2026, 1, 5) and security_id == "000003":
                open_price = None
                close = None
                trade_status = TradeStatus.SUSPENDED.value
            rows.append(
                _bar_row(
                    security_id=security_id,
                    trade_date=trade_date,
                    open_price=open_price,
                    close=close,
                    trade_status=trade_status,
                )
            )
    return rows


def _bar_row(
    *,
    security_id: str,
    trade_date: date,
    open_price: float | None,
    close: float | None,
    trade_status: str,
) -> dict[str, object]:
    trade_date_text = trade_date.isoformat()
    return {
        "security_id": security_id,
        "trade_date": trade_date_text,
        "open": open_price,
        "high": open_price,
        "low": open_price,
        "close": close,
        "volume": 1000000,
        "amount": 100000000.0,
        "trade_status": trade_status,
        "event_ts": f"{trade_date_text}T15:00:00+08:00",
        "available_at": f"{trade_date_text}T15:00:00+08:00",
        "snapshot_id": "fast_path_equivalence_fixture",
    }


def _security_master_rows() -> list[dict[str, object]]:
    board = next(iter(LimitRuleTable().rules_by_board))
    return [
        {
            "security_id": security_id,
            "board": board,
            "list_date": "2020-01-01",
            "available_at": "2020-01-01T15:00:00+08:00",
            "snapshot_id": "fast_path_equivalence_fixture",
        }
        for security_id in _SECURITY_IDS
    ]


def _corporate_action_rows() -> list[dict[str, object]]:
    return [
        {
            "security_id": "000001",
            "ex_date": "2026-01-06T00:00:00+08:00",
            "action_type": "CASH_DIVIDEND",
            "cash_dividend_per_share": 0.20,
            "share_ratio": 0.0,
            "event_ts": "2026-01-06T15:00:00+08:00",
            "available_at": "2026-01-05T15:00:00+08:00",
            "source_id": "fast_path_equivalence_fixture",
            "snapshot_id": "fast_path_equivalence_fixture",
        }
    ]


def _has_rebalance_orders(result) -> bool:
    decision_dates = {order.order_intent.decision_date for order in result.locked_orders}
    return len(decision_dates) >= 2


def _has_limit_rejection(result) -> bool:
    return any(fill.reason == "LIMIT_UP_NO_BUY" for fill in result.fills)


def _has_suspended_fill(result) -> bool:
    return any(fill.reason == "NO_TRADE_SUSPENDED" for fill in result.fills)


def _has_cash_dividend(result) -> bool:
    return any(entry.event_type == "CA_DIVIDEND_ACCRUED" for entry in result.ledger_entries)


_TRADE_DATES = [timestamp.date() for timestamp in pd.bdate_range("2026-01-02", "2026-03-31")]
_SECURITY_IDS = ("000001", "000002", "000003", "000004")


if __name__ == "__main__":
    unittest.main()
