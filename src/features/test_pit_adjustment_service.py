from __future__ import annotations

import tempfile
import unittest
from dataclasses import astuple
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import (
    CorporateActionVisibilityStatus,
    DataContractError,
    PriceBasis,
    evaluate_corporate_action_visibility,
)
from src.engine.backtest_runner import CachedPITDataPortal
from src.engine.corporate_action_handler import CorporateActionHandler
from src.engine.portfolio_ledger import CashState, PortfolioLedger, PositionLot, PositionState
from src.features.pit_adjustment_service import AdjustedReturnStatus, PITAdjustmentService
from src.market_calendar import trading_calendar_from_dates


ASOF = "2026-02-28T15:00:00+08:00"


class PITAdjustmentServiceTest(unittest.TestCase):
    def test_lt002b_future_ca_available_after_cutoff_does_not_change_closed_points(self) -> None:
        cutoff = "2026-01-06T15:00:00+08:00"
        future_ca = _ca_row(
            "000001",
            date(2026, 1, 6),
            "CASH_DIVIDEND",
            cash="9.99",
            available_at="2026-01-07T15:00:00+08:00",
        )
        self.assertGreater(pd.Timestamp(future_ca["available_at"]), pd.Timestamp(cutoff))

        daily_rows = [
            _bar_row("000001", date(2026, 1, 2), "10.00"),
            _bar_row("000001", date(2026, 1, 5), "9.50"),
            _bar_row("000001", date(2026, 1, 6), "9.60"),
            _bar_row("000001", date(2026, 1, 7), "9.70"),
        ]
        visible_ca = _ca_row(
            "000001",
            date(2026, 1, 5),
            "CASH_DIVIDEND",
            cash="1.00",
            available_at="2026-01-02T15:00:00+08:00",
        )

        with tempfile.TemporaryDirectory() as original_tmp, tempfile.TemporaryDirectory() as mutated_tmp:
            cutoff_date = pd.Timestamp(cutoff).date()
            original = _service(
                original_tmp,
                daily_rows=daily_rows,
                ca_rows=[visible_ca],
            ).daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 6),
                cutoff,
            )
            mutated = _service(
                mutated_tmp,
                daily_rows=daily_rows,
                ca_rows=[visible_ca, future_ca],
            ).daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 6),
                cutoff,
            )

            self.assertEqual(len(original.points), len(mutated.points))
            for original_point, mutated_point in zip(original.points, mutated.points):
                self.assertLessEqual(original_point.trade_date, cutoff_date)
                self.assertEqual(astuple(original_point), astuple(mutated_point))
            self.assertEqual(original.ca_events_applied, mutated.ca_events_applied)

    def test_lt002c_vendor_adjusted_daily_bar_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row(
                        "000001",
                        date(2026, 1, 2),
                        "10.00",
                        price_basis=PriceBasis.VENDOR_ADJUSTED.value,
                    ),
                    _bar_row(
                        "000001",
                        date(2026, 1, 5),
                        "10.50",
                        price_basis=PriceBasis.VENDOR_ADJUSTED.value,
                    ),
                ],
                ca_rows=[],
            )

            with self.assertRaises(DataContractError):
                service.daily_adjusted_return_series(
                    "000001",
                    date(2026, 1, 5),
                    date(2026, 1, 5),
                    ASOF,
                )

    def test_cached_portal_matches_original_portal_for_all_adjustment_methods(self) -> None:
        daily_rows = [
            _bar_row("000001", date(2026, 1, 2), "10.00"),
            _bar_row("000001", date(2026, 1, 5), "9.50"),
            _bar_row("000001", date(2026, 1, 6), "10.45"),
            _bar_row("000001", date(2026, 1, 7), "10.00"),
        ]
        ca_rows = [
            _ca_row(
                "000001",
                date(2026, 1, 5),
                "CASH_DIVIDEND",
                cash="1.00",
                available_at="2026-01-02T15:00:00+08:00",
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = _write_fixture(tmpdir, daily_rows=daily_rows, ca_rows=ca_rows)
            calendar = _calendar_from_daily_rows(daily_rows)
            slow_service = PITAdjustmentService(PITDataPortal(paths), calendar)
            fast_service = PITAdjustmentService(CachedPITDataPortal(paths), calendar)

            slow_daily = slow_service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )
            fast_daily = fast_service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )
            slow_cumulative = slow_service.cumulative_adjusted_return(
                "000001",
                "2026-01-07T15:00:00+08:00",
                3,
                ASOF,
            )
            fast_cumulative = fast_service.cumulative_adjusted_return(
                "000001",
                "2026-01-07T15:00:00+08:00",
                3,
                ASOF,
            )
            slow_factor = slow_service.adjustment_factor_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )
            fast_factor = fast_service.adjustment_factor_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )

            _assert_dataclass_equal(self, slow_daily, fast_daily)
            _assert_dataclass_equal(self, slow_cumulative, fast_cumulative)
            _assert_dataclass_equal(self, slow_factor, fast_factor)

    def test_future_ca_not_visible_at_derivation_asof_yields_raw_close_return(self) -> None:
        derivation_asof = "2026-01-06T15:00:00+08:00"
        future_ca = _ca_row(
            "000001",
            date(2026, 1, 6),
            "CASH_DIVIDEND",
            cash="1.00",
            available_at="2026-01-07T15:00:00+08:00",
        )
        self.assertGreater(pd.Timestamp(future_ca["available_at"]), pd.Timestamp(derivation_asof))
        self.assertLessEqual(pd.Timestamp(future_ca["ex_date"]).date(), pd.Timestamp(derivation_asof).date())

        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 5), "10.00"),
                    _bar_row("000001", date(2026, 1, 6), "10.50"),
                ],
                ca_rows=[future_ca],
            )

            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 6),
                date(2026, 1, 6),
                derivation_asof,
            )

            # This future CA is visible only at derivation_asof >= 2026-01-07 15:00.
            # Paired with LT-002B, this proves future CA rows are truly invisible at
            # the current derivation_asof and therefore cannot change closed points.
            point = series.points[0]
            self.assertEqual(point.status, AdjustedReturnStatus.OK)
            self.assertEqual(point.ca_on_date, tuple())
            self.assertEqual(point.reference_price, Decimal("10.00"))
            self.assertEqual(point.adjusted_return, Decimal("10.50") / Decimal("10.00") - Decimal("1"))

    def test_ca_ex_date_on_missing_bar_date_blocks_daily_and_cumulative_returns(self) -> None:
        missing_trade_date = date(2026, 1, 6)
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "10.50"),
                    _bar_row("000001", date(2026, 1, 7), "10.70"),
                ],
                ca_rows=[
                    _ca_row(
                        "000001",
                        missing_trade_date,
                        "CASH_DIVIDEND",
                        cash="0.50",
                        available_at="2026-01-05T15:00:00+08:00",
                    )
                ],
            )

            daily = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )
            cumulative = service.cumulative_adjusted_return(
                "000001",
                "2026-01-07T15:00:00+08:00",
                2,
                ASOF,
            )

            self.assertEqual({point.trade_date for point in daily.points}, {date(2026, 1, 5), date(2026, 1, 7)})
            self.assertTrue(all(point.status == AdjustedReturnStatus.BLOCKED for point in daily.points))
            self.assertTrue(
                all(point.block_reason == "CA_EX_DATE_ON_MISSING_BAR_DATE" for point in daily.points)
            )
            self.assertEqual(cumulative.status, AdjustedReturnStatus.BLOCKED)
            self.assertIsNone(cumulative.adjusted_return)
            self.assertEqual(cumulative.block_reason, "CA_EX_DATE_ON_MISSING_BAR_DATE")

    def test_missing_bar_date_contract_only_blocks_ca_ex_date_inside_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "10.50"),
                    _bar_row("000001", date(2026, 1, 6), "10.60"),
                    _bar_row("000001", date(2026, 1, 7), "10.70"),
                ],
                ca_rows=[
                    _ca_row(
                        "000001",
                        date(2026, 1, 2),
                        "CASH_DIVIDEND",
                        cash="0.10",
                        available_at="2026-01-02T15:00:00+08:00",
                    ),
                    _ca_row(
                        "000001",
                        date(2026, 1, 8),
                        "CASH_DIVIDEND",
                        cash="0.20",
                        available_at="2026-01-07T15:00:00+08:00",
                    ),
                ],
            )

            daily = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 7),
                ASOF,
            )
            cumulative = service.cumulative_adjusted_return(
                "000001",
                "2026-01-07T15:00:00+08:00",
                2,
                ASOF,
            )

            self.assertEqual([point.status for point in daily.points], [AdjustedReturnStatus.OK] * 3)
            self.assertTrue(
                all(point.block_reason != "CA_EX_DATE_ON_MISSING_BAR_DATE" for point in daily.points)
            )
            self.assertEqual(cumulative.status, AdjustedReturnStatus.OK)
            self.assertNotEqual(cumulative.block_reason, "CA_EX_DATE_ON_MISSING_BAR_DATE")

    def test_non_ca_missing_bar_blocks_cross_gap_daily_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "10.50"),
                    _bar_row("000001", date(2026, 1, 8), "10.80"),
                ],
                ca_rows=[],
                calendar_dates=[
                    date(2026, 1, 2),
                    date(2026, 1, 5),
                    date(2026, 1, 6),
                    date(2026, 1, 7),
                    date(2026, 1, 8),
                ],
            )

            daily = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 8),
                ASOF,
            )
            cumulative = service.cumulative_adjusted_return(
                "000001",
                "2026-01-08T15:00:00+08:00",
                2,
                ASOF,
            )

            self.assertEqual(daily.points[0].status, AdjustedReturnStatus.OK)
            self.assertEqual(
                daily.points[0].adjusted_return,
                Decimal("10.50") / Decimal("10.00") - Decimal("1"),
            )
            blocked_point = daily.points[1]
            self.assertEqual(blocked_point.trade_date, date(2026, 1, 8))
            self.assertEqual(blocked_point.status, AdjustedReturnStatus.BLOCKED)
            self.assertIsNone(blocked_point.adjusted_return)
            self.assertEqual(blocked_point.reference_price, None)
            self.assertEqual(blocked_point.block_reason, "PREVIOUS_CLOSE_NOT_ADJACENT_TRADING_DAY")
            self.assertNotEqual(
                blocked_point.adjusted_return,
                Decimal("10.80") / Decimal("10.50") - Decimal("1"),
            )
            self.assertEqual(cumulative.status, AdjustedReturnStatus.BLOCKED)
            self.assertEqual(cumulative.block_reason, "PREVIOUS_CLOSE_NOT_ADJACENT_TRADING_DAY")

    def test_holiday_gap_uses_real_calendar_previous_trading_day_and_stays_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2024, 9, 30), "10.00"),
                    _bar_row("000001", date(2024, 10, 8), "10.50"),
                ],
                ca_rows=[],
                calendar_dates=[date(2024, 9, 30), date(2024, 10, 8)],
            )

            series = service.daily_adjusted_return_series(
                "000001",
                date(2024, 10, 8),
                date(2024, 10, 8),
                ASOF,
            )

            point = series.points[0]
            self.assertEqual(point.status, AdjustedReturnStatus.OK)
            self.assertEqual(point.reference_price, Decimal("10.00"))
            self.assertEqual(
                point.adjusted_return,
                Decimal("10.50") / Decimal("10.00") - Decimal("1"),
            )

    def test_adjacent_trading_bar_still_uses_hand_calculated_daily_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 5), "10.00"),
                    _bar_row("000001", date(2026, 1, 6), "10.50"),
                ],
                ca_rows=[],
            )

            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 6),
                date(2026, 1, 6),
                ASOF,
            )

            self.assertEqual(series.points[0].status, AdjustedReturnStatus.OK)
            self.assertEqual(series.points[0].reference_price, Decimal("10.00"))
            self.assertEqual(
                series.points[0].adjusted_return,
                Decimal("10.50") / Decimal("10.00") - Decimal("1"),
            )

    def test_handler_cutover_visibility_boundary_stays_unprocessed_boundary(self) -> None:
        late_ca = _ca_row(
            "000001",
            date(2026, 1, 5),
            "CASH_DIVIDEND",
            cash="1.00",
            available_at="2026-01-05T15:00:00+08:00",
        )

        self.assertEqual(
            evaluate_corporate_action_visibility(
                late_ca,
                "2026-01-05T09:00:00+08:00",
            ).status,
            CorporateActionVisibilityStatus.UNPROCESSED_BOUNDARY,
        )

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

    def test_late_same_day_ca_visibility_uses_each_consumer_asof(self) -> None:
        late_ca = _ca_row(
            "000001",
            date(2026, 1, 5),
            "CASH_DIVIDEND",
            cash="1.00",
            available_at="2026-01-05T15:00:00+08:00",
        )
        self.assertEqual(
            evaluate_corporate_action_visibility(
                late_ca,
                "2026-01-05T09:00:00+08:00",
            ).status,
            CorporateActionVisibilityStatus.UNPROCESSED_BOUNDARY,
        )
        self.assertEqual(
            evaluate_corporate_action_visibility(
                late_ca,
                "2026-01-05T14:59:00+08:00",
            ).status,
            CorporateActionVisibilityStatus.NOT_YET_VISIBLE,
        )
        self.assertEqual(
            evaluate_corporate_action_visibility(
                late_ca,
                "2026-01-05T15:00:00+08:00",
            ).status,
            CorporateActionVisibilityStatus.VISIBLE_APPLICABLE,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=[
                    _bar_row("000001", date(2026, 1, 2), "10.00"),
                    _bar_row("000001", date(2026, 1, 5), "9.50"),
                ],
                ca_rows=[late_ca],
            )
            series = service.daily_adjusted_return_series(
                "000001",
                date(2026, 1, 5),
                date(2026, 1, 5),
                "2026-01-05T15:00:00+08:00",
            )

            handler = CorporateActionHandler(
                trading_calendar_from_dates([date(2026, 1, 2), date(2026, 1, 5)]),
                service.portal,
            )
            ledger = _ledger_with_position()
            entries = handler.process_day(ledger, date(2026, 1, 5))

            # Official formula hand calculation: reference=(10.00-1.00)/(1+0)=9.00;
            # adjusted_return=9.50/9.00-1.
            self.assertEqual(series.points[0].status, AdjustedReturnStatus.OK)
            self.assertEqual(series.points[0].reference_price, Decimal("9.00"))
            self.assertEqual(
                series.points[0].adjusted_return,
                Decimal("9.50") / Decimal("9.00") - Decimal("1"),
            )
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
    calendar_dates: list[date] | None = None,
) -> PITAdjustmentService:
    calendar = trading_calendar_from_dates(calendar_dates) if calendar_dates is not None else _calendar_from_daily_rows(daily_rows)
    return PITAdjustmentService(PITDataPortal(_write_fixture(tmpdir, daily_rows=daily_rows, ca_rows=ca_rows)), calendar)


def _calendar_from_daily_rows(daily_rows: list[dict[str, object]]):
    return trading_calendar_from_dates([pd.Timestamp(row["trade_date"]).date() for row in daily_rows])


def _write_fixture(
    tmpdir: str,
    *,
    daily_rows: list[dict[str, object]],
    ca_rows: list[dict[str, object]],
) -> dict[str, Path]:
    tmp = Path(tmpdir)
    daily_path = tmp / "daily_bar_raw.parquet"
    ca_path = tmp / "corporate_actions.parquet"
    pd.DataFrame(daily_rows).to_parquet(daily_path, index=False)
    pd.DataFrame(ca_rows, columns=_CA_COLUMNS).to_parquet(ca_path, index=False)
    return {"daily_bar_raw": daily_path, "corporate_actions": ca_path}


def _bar_row(
    security_id: str,
    trade_date: date,
    close: str,
    *,
    price_basis: str = PriceBasis.RAW_UNADJUSTED.value,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date.isoformat(),
        "close": close,
        "event_ts": f"{trade_date.isoformat()}T15:00:00+08:00",
        "available_at": f"{trade_date.isoformat()}T15:00:00+08:00",
        "price_basis": price_basis,
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
    explicit_trading_dates = [
        date(2026, 1, 2),
        date(2026, 1, 5),
        date(2026, 1, 6),
        date(2026, 1, 7),
        date(2026, 1, 8),
        date(2026, 1, 9),
        date(2026, 1, 12),
        date(2026, 1, 13),
        date(2026, 1, 14),
        date(2026, 1, 15),
        date(2026, 1, 16),
        date(2026, 1, 19),
        date(2026, 1, 20),
        date(2026, 1, 21),
        date(2026, 1, 22),
        date(2026, 1, 23),
        date(2026, 1, 26),
        date(2026, 1, 27),
        date(2026, 1, 28),
        date(2026, 1, 29),
        date(2026, 1, 30),
    ]
    if start not in explicit_trading_dates:
        raise ValueError(f"fixture start date is not in explicit trading calendar: {start}")
    start_index = explicit_trading_dates.index(start)
    dates = explicit_trading_dates[start_index : start_index + count]
    if len(dates) != count:
        raise ValueError("fixture explicit trading calendar is too short")
    return dates


def _assert_dataclass_equal(testcase: unittest.TestCase, left: object, right: object) -> None:
    left_snapshot = astuple(left)
    right_snapshot = astuple(right)
    if left_snapshot != right_snapshot:
        testcase.fail(f"first_diff={_first_diff(left_snapshot, right_snapshot)}")


def _first_diff(left: object, right: object, path: str = "root") -> str:
    if left == right:
        return ""
    if isinstance(left, tuple) and isinstance(right, tuple):
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            diff = _first_diff(left_item, right_item, f"{path}[{index}]")
            if diff:
                return diff
        if len(left) != len(right):
            return f"{path}.len: {len(left)} != {len(right)}"
    return f"{path}: {left!r} != {right!r}"


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
