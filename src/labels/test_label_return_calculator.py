from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data import PITDataPortal
from src.domain import PriceBasis
from src.features.cross_sectional_momentum import CrossSectionalMomentumPoint, CrossSectionalMomentumSignal
from src.features.momentum_ic_evaluation import evaluate_momentum_rank_ic
from src.features.pit_adjustment_service import AdjustedReturnStatus, PITAdjustmentService
from src.labels import FutureReturnLabelStatus, calculate_future_return_labels
from src.market_calendar import trading_calendar_from_dates


SIGNAL_ASOF = "2026-01-02T15:00:00+08:00"
WRONG_DERIVATION_ASOF = SIGNAL_ASOF
CORRECT_DERIVATION_ASOF = "2026-02-03T15:00:00+08:00"
IMMATURE_EVALUATION_ASOF = "2026-02-02T15:00:00+08:00"


TRADING_DATES = (
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
    date(2026, 2, 2),
    date(2026, 2, 3),
)


class LabelReturnCalculatorTest(unittest.TestCase):
    def test_signal_day_asof_cannot_see_future_open_prices_but_label_asof_can(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(tmpdir, _base_daily_rows(), _ca_rows())

            blocked = service.open_to_open_adjusted_return(
                "000001",
                date(2026, 1, 5),
                date(2026, 2, 3),
                WRONG_DERIVATION_ASOF,
            )
            labels = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )

            self.assertEqual(blocked.status, AdjustedReturnStatus.NO_DATA)
            self.assertIsNone(blocked.adjusted_return)
            self.assertEqual(blocked.block_reason, "MISSING_ENTRY_OPEN")
            self.assertEqual(labels[0].status, FutureReturnLabelStatus.OK)
            self.assertEqual(labels[0].future_return, Decimal("0.1"))
            self.assertNotEqual(labels[0].label_observed_at, SIGNAL_ASOF)
            self.assertEqual(labels[0].label_observed_at, CORRECT_DERIVATION_ASOF)

    def test_future_return_label_uses_hand_calculated_open_to_open_pit_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(tmpdir, _base_daily_rows(), _ca_rows())

            labels = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )

            label = labels[0]
            # T+1 open=10.00, T+22 open=9.90, CA factor on 2026-01-06 is
            # reference/previous_close=(10.00-1.00)/10.00=0.9.
            # PIT open-to-open return=9.90/(10.00*0.9)-1=0.10.
            self.assertEqual(label.future_return, Decimal("0.1"))
            self.assertEqual(label.entry_ts, "2026-01-05T09:30:00+08:00")
            self.assertEqual(label.exit_ts, "2026-02-03T09:30:00+08:00")
            self.assertEqual(label.price_basis, PriceBasis.PIT_DERIVED)
            self.assertEqual(label.evidence_status, "EXPLORATORY_TAINTED")
            self.assertEqual(label.status, FutureReturnLabelStatus.OK)
            self.assertIn("000001:2026-01-06:CASH_DIVIDEND:fixture", label.corporate_action_manifest)

    def test_calculator_return_equals_direct_pit_open_to_open_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(tmpdir, _base_daily_rows(), _ca_rows())

            direct = service.open_to_open_adjusted_return(
                "000001",
                date(2026, 1, 5),
                date(2026, 2, 3),
                CORRECT_DERIVATION_ASOF,
            )
            label = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )[0]

            self.assertEqual(direct.status, AdjustedReturnStatus.OK)
            self.assertEqual(label.future_return, direct.adjusted_return)
            self.assertEqual(label.input_snapshot_id, direct.input_snapshot_id)
            self.assertEqual(label.corporate_action_manifest, _manifest(direct.ca_events_applied))

    def test_endpoint_suspensions_invalidate_entry_or_exit_but_middle_open_gap_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as entry_tmp:
            service = _service(entry_tmp, _base_daily_rows(entry_open=None), _ca_rows())
            label = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )[0]
            self.assertEqual(label.status, FutureReturnLabelStatus.NOT_TRADABLE_ENTRY)
            self.assertIsNone(label.future_return)

        with tempfile.TemporaryDirectory() as exit_tmp:
            service = _service(exit_tmp, _base_daily_rows(exit_open=None), _ca_rows())
            label = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )[0]
            self.assertEqual(label.status, FutureReturnLabelStatus.NOT_TRADABLE_EXIT)
            self.assertIsNone(label.future_return)

        with tempfile.TemporaryDirectory() as middle_tmp:
            service = _service(middle_tmp, _base_daily_rows(middle_open=None), _ca_rows())
            label = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )[0]
            self.assertEqual(label.status, FutureReturnLabelStatus.OK)
            self.assertEqual(label.future_return, Decimal("0.1"))

    def test_calculated_immature_label_is_excluded_by_existing_lt003_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _service(tmpdir, _base_daily_rows(), _ca_rows())
            label = calculate_future_return_labels(
                ["000001"],
                SIGNAL_ASOF,
                service,
                trading_calendar_from_dates(TRADING_DATES),
            )[0]

            result = evaluate_momentum_rank_ic(
                [_signal()],
                [label],
                IMMATURE_EVALUATION_ASOF,
            )

            self.assertEqual(result.rank_ic_series[0].sample_size, 0)
            self.assertEqual(result.rank_ic_series[0].immature_label_count, 1)
            self.assertEqual(result.immature_label_count, 1)


def _service(
    tmpdir: str,
    daily_rows: list[dict[str, object]],
    ca_rows: list[dict[str, object]],
) -> PITAdjustmentService:
    return PITAdjustmentService(
        PITDataPortal(_write_fixture(tmpdir, daily_rows, ca_rows)),
        trading_calendar_from_dates(TRADING_DATES),
    )


def _write_fixture(
    tmpdir: str,
    daily_rows: list[dict[str, object]],
    ca_rows: list[dict[str, object]],
) -> dict[str, Path]:
    tmp = Path(tmpdir)
    daily_path = tmp / "daily_bar_raw.parquet"
    ca_path = tmp / "corporate_actions.parquet"
    pd.DataFrame(daily_rows).to_parquet(daily_path, index=False)
    pd.DataFrame(ca_rows, columns=_CA_COLUMNS).to_parquet(ca_path, index=False)
    return {"daily_bar_raw": daily_path, "corporate_actions": ca_path}


def _base_daily_rows(
    *,
    entry_open: str | None = "10.00",
    exit_open: str | None = "9.90",
    middle_open: str | None = "9.40",
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, trade_date in enumerate(TRADING_DATES):
        open_price = "9.40"
        close = "9.40"
        if trade_date == date(2026, 1, 2):
            open_price = "10.00"
            close = "10.00"
        if trade_date == date(2026, 1, 5):
            open_price = entry_open
            close = "10.00"
        if trade_date == date(2026, 1, 6):
            open_price = "9.10"
            close = "9.40"
        if trade_date == date(2026, 1, 13):
            open_price = middle_open
            close = "9.40"
        if trade_date == date(2026, 2, 3):
            open_price = exit_open
            close = "9.90"
        rows.append(_bar_row("000001", trade_date, open_price, close, snapshot_id=f"daily-{index:02d}"))
    return rows


def _bar_row(
    security_id: str,
    trade_date: date,
    open_price: str | None,
    close: str,
    *,
    snapshot_id: str,
) -> dict[str, object]:
    return {
        "security_id": security_id,
        "trade_date": trade_date.isoformat(),
        "open": open_price,
        "close": close,
        "event_ts": f"{trade_date.isoformat()}T15:00:00+08:00",
        "available_at": f"{trade_date.isoformat()}T15:00:00+08:00",
        "price_basis": PriceBasis.RAW_UNADJUSTED.value,
        "snapshot_id": snapshot_id,
    }


def _ca_rows() -> list[dict[str, object]]:
    return [
        {
            "security_id": "000001",
            "ex_date": "2026-01-06T00:00:00+08:00",
            "action_type": "CASH_DIVIDEND",
            "cash_dividend_per_share": "1.00",
            "share_ratio": "0",
            "rights_price_per_share": None,
            "event_ts": "2026-01-06T15:00:00+08:00",
            "available_at": "2026-01-05T15:00:00+08:00",
            "source_id": "fixture",
            "snapshot_id": "ca-fixture",
        }
    ]


def _signal() -> CrossSectionalMomentumSignal:
    point = CrossSectionalMomentumPoint(
        "000001",
        Decimal("0.20"),
        1,
        AdjustedReturnStatus.OK,
    )
    return CrossSectionalMomentumSignal(
        asof_ts=SIGNAL_ASOF,
        score_asof_ts=SIGNAL_ASOF,
        derivation_asof_ts=SIGNAL_ASOF,
        ranking_method="ordinal_descending_rank",
        lookback_trading_days=231,
        skip_recent_trading_days=21,
        points=(point,),
        excluded_securities=tuple(),
        exclusion_reason_counts=tuple(),
        universe_size_after_exclusion=1,
        evidence_status="EXPLORATORY_TAINTED",
        input_snapshot_id="signal-fixture",
        signal_manifest_hash="signal-hash",
    )


def _manifest(events) -> str:
    if not events:
        return "no_visible_ca_events"
    return ";".join(
        f"{event.security_id}:{event.ex_date.isoformat()}:{event.action_type}:{event.source_id}"
        for event in events
    )


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
