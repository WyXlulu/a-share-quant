from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from src.data import PITDataPortal
from src.domain import PriceBasis
from src.features import cross_sectional_momentum
from src.features.cross_sectional_momentum import (
    LOOKBACK_TRADING_DAYS,
    SKIP_RECENT_TRADING_DAYS,
    CrossSectionalMomentumSignal,
    calculate_cross_sectional_momentum_signal,
)
from src.features.pit_adjustment_service import (
    AdjustedReturnStatus,
    CumulativeAdjustedReturnResult,
    EVIDENCE_STATUS,
    PITAdjustmentService,
)
from src.market_calendar import trading_calendar_from_dates


ASOF = "2026-01-08T15:00:00+08:00"
DERIVATION_ASOF = "2026-01-08T15:00:00+08:00"


class CrossSectionalMomentumTest(unittest.TestCase):
    def test_scores_and_ordinal_ranks_use_hand_calculated_12_minus_1_returns(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 21), 253)
        asof = _ts(trade_dates[-1])
        derivation_asof = _ts(trade_dates[-1])

        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(
                tmpdir,
                daily_rows=_daily_rows_for_scores(
                    trade_dates,
                    {
                        "000001": Decimal("0.20"),
                        "000002": Decimal("0.10"),
                        "000003": Decimal("-0.10"),
                    },
                ),
                ca_rows=[],
            )

            signal = calculate_cross_sectional_momentum_signal(
                ["000001", "000002", "000003"],
                asof,
                derivation_asof,
                service,
            )

            points = _points_by_security(signal)
            self.assertEqual(signal.score_asof_ts, _ts(trade_dates[-1 - SKIP_RECENT_TRADING_DAYS]))
            # Hand calculation: the cumulative window compounds close(t-21)/close(t-252)-1.
            # The final 21 closes are set to an extreme value and must not affect scores.
            self.assertEqual(points["000001"].momentum_score, Decimal("120") / Decimal("100") - Decimal("1"))
            self.assertEqual(points["000002"].momentum_score, Decimal("110") / Decimal("100") - Decimal("1"))
            self.assertEqual(points["000003"].momentum_score, Decimal("90") / Decimal("100") - Decimal("1"))
            self.assertEqual(points["000001"].cross_sectional_rank, 1)
            self.assertEqual(points["000002"].cross_sectional_rank, 2)
            self.assertEqual(points["000003"].cross_sectional_rank, 3)
            self.assertEqual(signal.universe_size_after_exclusion, 3)
            self.assertEqual(signal.excluded_securities, tuple())

    def test_blocked_security_is_excluded_and_does_not_enter_ranking(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 21), 253)
        ca_rows = [
            _ca_row(
                "000999",
                trade_dates[100],
                "MERGER",
                available_at="2025-01-21T15:00:00+08:00",
            )
        ]
        daily_rows = _daily_rows_for_scores(
            trade_dates,
            {
                "000001": Decimal("0.20"),
                "000002": Decimal("0.10"),
                "000999": Decimal("0.50"),
            },
        )

        with tempfile.TemporaryDirectory() as clean_tmp, tempfile.TemporaryDirectory() as blocked_tmp:
            clean_service = _service(
                clean_tmp,
                daily_rows=[row for row in daily_rows if row["security_id"] != "000999"],
                ca_rows=[],
            )
            blocked_service = _service(blocked_tmp, daily_rows=daily_rows, ca_rows=ca_rows)
            clean_signal = calculate_cross_sectional_momentum_signal(
                ["000001", "000002"],
                _ts(trade_dates[-1]),
                _ts(trade_dates[-1]),
                clean_service,
            )
            blocked_signal = calculate_cross_sectional_momentum_signal(
                ["000001", "000999", "000002"],
                _ts(trade_dates[-1]),
                _ts(trade_dates[-1]),
                blocked_service,
            )

            clean_points = _points_by_security(clean_signal)
            blocked_points = _points_by_security(blocked_signal)
            blocked_point = blocked_points["000999"]
            self.assertEqual(blocked_point.status, AdjustedReturnStatus.BLOCKED)
            self.assertIsNone(blocked_point.momentum_score)
            self.assertIsNone(blocked_point.cross_sectional_rank)
            self.assertEqual(blocked_signal.excluded_securities[0].security_id, "000999")
            self.assertEqual(blocked_signal.excluded_securities[0].status, AdjustedReturnStatus.BLOCKED)
            self.assertEqual(blocked_signal.exclusion_reason_counts[0].status, AdjustedReturnStatus.BLOCKED)
            self.assertEqual(blocked_signal.exclusion_reason_counts[0].count, 1)
            # Guardrail: if the blocked 50% score were silently imputed as 0/mean/median
            # and ranked, the output universe or ranks below would change.
            for security_id in ("000001", "000002"):
                self.assertEqual(
                    blocked_points[security_id].cross_sectional_rank,
                    clean_points[security_id].cross_sectional_rank,
                )
            self.assertEqual(blocked_signal.universe_size_after_exclusion, 2)

    def test_no_data_security_is_excluded_and_counted(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 21), 253)
        full_rows = _daily_rows_for_scores(
            trade_dates,
            {
                "000001": Decimal("0.20"),
                "000002": Decimal("0.10"),
            },
        )
        short_rows = _daily_rows_for_scores(
            trade_dates[-200:],
            {
                "000003": Decimal("0.30"),
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            signal = calculate_cross_sectional_momentum_signal(
                ["000001", "000002", "000003"],
                _ts(trade_dates[-1]),
                _ts(trade_dates[-1]),
                _service(tmpdir, daily_rows=full_rows + short_rows, ca_rows=[]),
            )

            point = _points_by_security(signal)["000003"]
            self.assertEqual(point.status, AdjustedReturnStatus.NO_DATA)
            self.assertIsNone(point.momentum_score)
            self.assertIsNone(point.cross_sectional_rank)
            self.assertEqual(signal.exclusion_reason_counts[0].status, AdjustedReturnStatus.NO_DATA)
            self.assertEqual(signal.exclusion_reason_counts[0].count, 1)
            self.assertEqual(signal.universe_size_after_exclusion, 2)

    def test_signal_outputs_remain_exploratory_tainted(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 21), 253)
        with tempfile.TemporaryDirectory() as tmpdir:
            signal = calculate_cross_sectional_momentum_signal(
                ["000001"],
                _ts(trade_dates[-1]),
                _ts(trade_dates[-1]),
                _service(
                    tmpdir,
                    daily_rows=_daily_rows_for_scores(trade_dates, {"000001": Decimal("0.20")}),
                    ca_rows=[],
                ),
            )

            self.assertEqual(signal.evidence_status, "EXPLORATORY_TAINTED")
            self.assertEqual(signal.evidence_status, EVIDENCE_STATUS)

    def test_causality_uses_injected_service_and_trade_calendar_anchor(self) -> None:
        trade_dates = _business_dates(date(2025, 1, 21), 253)
        fake_service = _GuardedMomentumService(trade_dates)

        signal = calculate_cross_sectional_momentum_signal(
            ["000001", "000002"],
            _ts(trade_dates[-1]),
            _ts(trade_dates[-1]),
            fake_service,  # type: ignore[arg-type]
        )

        module_source = inspect.getsource(cross_sectional_momentum)
        self.assertNotIn("read_parquet", module_source)
        self.assertNotIn("PITDataPortal", module_source)
        self.assertNotIn(".portal", module_source)
        self.assertNotIn(".query(", module_source)
        self.assertNotIn("end_date=None", module_source)
        expected_score_date = trade_dates[-1 - SKIP_RECENT_TRADING_DAYS]
        self.assertEqual(fake_service.cumulative_asof_dates, [expected_score_date, expected_score_date])
        self.assertEqual(fake_service.lookbacks, [LOOKBACK_TRADING_DAYS, LOOKBACK_TRADING_DAYS])
        self.assertLessEqual(max(fake_service.cumulative_asof_dates), trade_dates[-1])
        self.assertEqual(signal.universe_size_after_exclusion, 2)


class _GuardedMomentumService:
    def __init__(self, trade_dates: list[date]) -> None:
        self.trade_dates = trade_dates
        self.cumulative_asof_dates: list[date] = []
        self.lookbacks: list[int] = []

    def _daily_bars(self, security_id: str, derivation_asof: Any) -> pd.DataFrame:
        del security_id, derivation_asof
        return pd.DataFrame(
            {
                "trade_date_key": self.trade_dates,
                "snapshot_id": ["guarded-fixture"] * len(self.trade_dates),
            }
        )

    def cumulative_adjusted_return(
        self,
        security_id: str,
        asof_ts: Any,
        lookback_trading_days: int,
        derivation_asof_ts: Any,
    ) -> CumulativeAdjustedReturnResult:
        del derivation_asof_ts
        self.cumulative_asof_dates.append(pd.Timestamp(asof_ts).date())
        self.lookbacks.append(lookback_trading_days)
        return CumulativeAdjustedReturnResult(
            str(security_id).zfill(6),
            pd.Timestamp(asof_ts).isoformat(),
            LOOKBACK_TRADING_DAYS,
            AdjustedReturnStatus.OK,
            Decimal("0.10") if str(security_id).zfill(6) == "000001" else Decimal("0.05"),
            EVIDENCE_STATUS,
            PriceBasis.PIT_DERIVED,
            pd.Timestamp(asof_ts).isoformat(),
            "guarded-fixture",
            tuple(),
        )


def _service(
    tmpdir: str,
    *,
    daily_rows: list[dict[str, object]],
    ca_rows: list[dict[str, object]],
) -> PITAdjustmentService:
    calendar = trading_calendar_from_dates([pd.Timestamp(row["trade_date"]).date() for row in daily_rows])
    return PITAdjustmentService(PITDataPortal(_write_fixture(tmpdir, daily_rows=daily_rows, ca_rows=ca_rows)), calendar)


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


def _daily_rows_for_scores(
    trade_dates: list[date],
    security_scores: dict[str, Decimal],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    anchor_index = max(0, len(trade_dates) - 1 - SKIP_RECENT_TRADING_DAYS)
    for security_id, score in security_scores.items():
        anchor_close = Decimal("100") * (Decimal("1") + score)
        for index, trade_date in enumerate(trade_dates):
            close = Decimal("100")
            if index == anchor_index:
                close = anchor_close
            elif index > anchor_index:
                close = Decimal("999")
            rows.append(_bar_row(security_id, trade_date, str(close)))
    return rows


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


def _business_dates(start: date, count: int) -> list[date]:
    dates: list[date] = []
    current = start
    while len(dates) < count:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return dates


def _points_by_security(signal: CrossSectionalMomentumSignal):
    return {point.security_id: point for point in signal.points}


def _ts(trade_date: date) -> str:
    return f"{trade_date.isoformat()}T15:00:00+08:00"


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
