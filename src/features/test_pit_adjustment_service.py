from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState
from src.features.pit_adjustment_service import AdjustedReturnStatus, PITAdjustmentService
from src.market_calendar import trading_calendar_from_dates


ASOF = "2026-02-28T15:00:00+08:00"


class PITAdjustmentServiceTest(unittest.TestCase):
    def test_daily_adjusted_return_uses_ca_reference_price_and_raw_close_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "9.50"),
                    _bar_row("000001", date(2026, 1, 6), "10.45"),
                ],
                ca_rows=[
                    _ca_row(
                        "000001",
                        date(2026, 1, 5),
                        "CASH_DIVIDEND",
                        cash="1.00",
                        available_at="2026-01-02T15:00:00+08:00",
                    )
                ],
            )

            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 6),
                ASOF,
            )

            self.assertEqual([point.status for point in series.points], [AdjustedReturnStatus.OK] * 2)
            # Official formula hand calculation: reference=(10.00-1.00)/(1+0)=9.00;
            # adjusted_return=9.50/9.00-1.
            self.assertEqual(series.points[0].reference_price, Decimal("9.00"))
            self.assertEqual(
                series.points[0].adjusted_return,
                Decimal("9.50") / Decimal("9.00") - Decimal("1"),
            )
            # Non-CA day hand calculation: reference=prior close 9.50;
            # adjusted_return=10.45/9.50-1=0.10.
            self.assertEqual(series.points[1].reference_price, Decimal("9.50"))
            self.assertEqual(series.points[1].adjusted_return, Decimal("0.1"))

    def test_cumulative_return_is_blocked_and_never_falls_back_to_raw_skip_compounding(self) -> None:
        trade_dates = _business_dates(date(2026, 1, 2), 21)
        window_dates = trade_dates[1:]
        blocked_date = window_dates[9]
        closes = {trade_date: Decimal(100 + index) for index, trade_date in enumerate(trade_dates)}
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", trade_date, str(close))
                    for trade_date, close in closes.items()
                ],
                ca_rows=[
                    _ca_row(
                        "000001",
                        blocked_date,
                        "MERGER",
                        available_at="2026-01-02T15:00:00+08:00",
                    )
                ],
            )

            result = service.cumulative_adjusted_return(
                "000001",
                f"{window_dates[-1].isoformat()}T15:00:00+08:00",
                20,
                ASOF,
            )

            raw_skip_compounded = Decimal("1")
            for trade_date in window_dates:
                if trade_date == blocked_date:
                    continue
                previous_date = trade_dates[trade_dates.index(trade_date) - 1]
                raw_skip_compounded *= closes[trade_date] / closes[previous_date]
            raw_skip_return = raw_skip_compounded - Decimal("1")

            self.assertEqual(result.status, AdjustedReturnStatus.BLOCKED)
            self.assertIsNone(result.adjusted_return)
            self.assertEqual(result.block_reason, "UNSUPPORTED_CA_TYPE:MERGER")
            self.assertNotEqual(result.adjusted_return, raw_skip_return)

    def test_visibility_classification_is_shared_with_corporate_action_handler_boundary(self) -> None:
        late_ca = _ca_row(
            "000001",
            date(2026, 1, 5),
            "CASH_DIVIDEND",
            cash="0.30",
            available_at="2026-01-05T15:00:00+08:00",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "10.00"),
                ],
                ca_rows=[late_ca],
            )
            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 5),
                ASOF,
            )

            handler = CorporateActionHandler(
                trading_calendar_from_dates([date(2026, 1, 2), date(2026, 1, 5)]),
                service.portal,
            )
            ledger = _ledger_with_position()
            entries = handler.process_day(ledger, date(2026, 1, 5))

            self.assertEqual(series.points[0].status, AdjustedReturnStatus.BLOCKED)
            self.assertEqual(series.points[0].block_reason, "CA_AVAILABLE_AFTER_APPLICATION_ASOF")
            self.assertEqual([entry.event_type for entry in entries], ["UNPROCESSED_CA"])
            self.assertEqual(
                entries[0].fill_reason,
                "UNPROCESSED_CA:CA_AVAILABLE_AFTER_APPLICATION_ASOF",
            )

    def test_ok_outputs_remain_exploratory_tainted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "10.50"),
                ],
                ca_rows=[],
            )

            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 5),
                ASOF,
            )

            self.assertEqual(series.points[0].status, AdjustedReturnStatus.OK)
            self.assertEqual(series.evidence_status, "EXPLORATORY_TAINTED")


def _service(
    tmpdir: str,
    *,
    daily_rows: list[dict[str, object]],
    ca_rows: list[dict[str, object]],
) -> PITAdjustmentService:
    tmp = Path(tmpdir)
    daily_path = tmp / "daily_bar_raw.parquet"
    ca_path = tmp / "corporate_actions.parquet"
    pd.DataFrame(daily_rows).to_parquet(daily_path, index=False)
    pd.DataFrame(ca_rows, columns=_CA_COLUMNS).to_parquet(ca_path, index=False)
    return PITAdjustmentService(
        PITDataPortal({"daily_bar_raw": daily_path, "corporate_actions": ca_path})
    )


def _bar_row(security_id: str, trade_date: date, close: str) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date.isoformat(),
        "close": close,
        "event_ts": f"{trade_date.isoformat()}T15:00:00+08:00",
        "available_at": f"{trade_date.isoformat()}T15:00:00+08:00",
        "snapshot_id": "daily-fixture",
    }


def _ca_row(
    security_id: str,
    ex_date: date,
    action_type: str,
    *,
    cash: str = "0",
    share_ratio: str = "0",
    rights_price_per_share: str | None = None,
    available_at: str,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "ex_date": f"{ex_date.isoformat()}T00:00:00+08:00",
        "action_type": action_type,
        "cash_dividend_per_share": cash,
        "share_ratio": share_ratio,
        "rights_price_per_share": rights_price_per_share,
        "event_ts": f"{ex_date.isoformat()}T15:00:00+08:00",
        "available_at": available_at,
        "source_id": "fixture",
        "snapshot_id": "ca-fixture",
    }


def _ledger_with_position() -> PortfolioLedger:
    ledger = PortfolioLedger(
        CashState(settled_cash=Decimal("100.00"), available_cash=Decimal("100.00")),
        calendar=trading_calendar_from_dates([date(2026, 1, 2), date(2026, 1, 5)]),
    )
    ledger.positions["000001"] = PositionState(
        "000001",
        [
            PositionLot(
                quantity=1000,
                cost_basis=Decimal("10000.00"),
                trade_date=date(2026, 1, 2),
                sellable_from=date(2026, 1, 5),
                is_unlocked=True,
            )
        ],
    )
    return ledger


def _business_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


_CA_COLUMNS = [
    "security_id",
    "ex_date",
    "action_type",
    "cash_dividend_per_share",
    "share_ratio",
    "rights_price_per_share",
    "event_ts",
    "available_at",
    "source_id",
    "snapshot_id",
]


if __name__ == "__main__":
    unittest.main()
