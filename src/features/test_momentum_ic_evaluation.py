from __future__ import annotations

import inspect
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.domain import PriceBasis
from src.features import cross_sectional_momentum, momentum_ic_evaluation
from src.features.cross_sectional_momentum import (
    CrossSectionalMomentumPoint,
    CrossSectionalMomentumSignal,
    ExclusionReasonCount,
    calculate_cross_sectional_momentum_signal,
)
from src.features.momentum_ic_evaluation import evaluate_momentum_rank_ic
from src.features.pit_adjustment_service import AdjustedReturnStatus
from src.labels import LabelDataPortal, LabelSpec


SIGNAL_ASOF = "2026-01-08T15:00:00+08:00"
ENTRY_TS = "2026-01-09T09:30:00+08:00"
EXIT_TS = "2026-02-09T09:30:00+08:00"
MATURE_EVAL_ASOF = "2026-02-10T15:00:00+08:00"


class MomentumICEvaluationTest(unittest.TestCase):
    def test_signal_function_has_no_future_return_path_and_label_portal_is_separate(self) -> None:
        signal_signature = inspect.signature(calculate_cross_sectional_momentum_signal)
        self.assertEqual(
            tuple(signal_signature.parameters),
            ("security_ids", "asof_ts", "derivation_asof_ts", "adjustment_service"),
        )
        for parameter in signal_signature.parameters:
            self.assertNotIn("label", parameter.lower())
            self.assertNotIn("future", parameter.lower())
            self.assertNotIn("return", parameter.lower())

        signal_source = inspect.getsource(cross_sectional_momentum)
        self.assertNotIn("LabelDataPortal", signal_source)
        self.assertNotIn("FutureReturnLabel", signal_source)
        self.assertNotIn("future_return", signal_source)
        self.assertNotIn("label_observed_at", signal_source)

        eval_signature = inspect.signature(evaluate_momentum_rank_ic)
        self.assertIn("future_returns", eval_signature.parameters)
        self.assertFalse(hasattr(LabelDataPortal(Path("unused.parquet")), "cumulative_adjusted_return"))
        self.assertFalse(hasattr(LabelDataPortal(Path("unused.parquet")), "_daily_bars"))

        with tempfile.TemporaryDirectory() as tmpdir:
            portal = _label_portal(tmpdir, _label_rows({"000001": Decimal("0.10")}))
            labels = portal.query_future_outcome_inputs(
                ["000001"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            self.assertEqual(labels[0].price_basis, PriceBasis.PIT_DERIVED)
            self.assertEqual(labels[0].corporate_action_manifest, "pit-ca-fixture")

    def test_lt003_immature_label_is_excluded_and_counted(self) -> None:
        signal = _signal(
            {
                "000001": Decimal("0.30"),
                "000002": Decimal("0.20"),
                "000003": Decimal("0.10"),
            }
        )
        rows = _label_rows(
            {
                "000001": Decimal("0.12"),
                "000002": Decimal("0.06"),
                "000003": Decimal("0.50"),
            },
            overrides={
                "000003": {
                    "label_observed_at": "2026-02-11T15:00:00+08:00",
                }
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            labels = _label_portal(tmpdir, rows).query_future_outcome_inputs(
                ["000001", "000002", "000003"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            result = evaluate_momentum_rank_ic([signal], labels, MATURE_EVAL_ASOF)

            self.assertEqual(result.rank_ic_series[0].sample_size, 2)
            self.assertEqual(result.rank_ic_series[0].immature_label_count, 1)
            self.assertEqual(result.immature_label_count, 1)
            self.assertEqual(result.rank_ic_series[0].rank_ic, Decimal("1"))
            self.assertEqual(result.coverage_series[0].coverage, Decimal("2") / Decimal("3"))
            self.assertNotIn("000003", _ranked_label_ids(signal, labels, result))

    def test_rank_ic_matches_hand_calculated_spearman(self) -> None:
        signal = _signal(
            {
                "000001": Decimal("0.50"),
                "000002": Decimal("0.40"),
                "000003": Decimal("0.30"),
                "000004": Decimal("0.20"),
                "000005": Decimal("0.10"),
            }
        )
        # Hand ranks:
        # score ranks A/B/C/D/E = 1/2/3/4/5.
        # future ranks A/C/B/E/D = 1/2/3/4/5, so sum(d^2)=4.
        # Spearman = 1 - 6*4/(5*(25-1)) = 0.8.
        rows = _label_rows(
            {
                "000001": Decimal("0.10"),
                "000002": Decimal("0.04"),
                "000003": Decimal("0.08"),
                "000004": Decimal("-0.01"),
                "000005": Decimal("0.02"),
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            labels = _label_portal(tmpdir, rows).query_future_outcome_inputs(
                ["000001", "000002", "000003", "000004", "000005"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            result = evaluate_momentum_rank_ic([signal], labels, MATURE_EVAL_ASOF)

            self.assertEqual(result.rank_ic_series[0].rank_ic, Decimal("0.8"))
            self.assertEqual(result.rank_ic_mean, Decimal("0.8"))
            self.assertEqual(result.quantile_returns[0].mean_future_return, Decimal("0.10"))
            self.assertEqual(result.quantile_returns[1].mean_future_return, Decimal("0.04"))
            self.assertEqual(result.quantile_returns[2].mean_future_return, Decimal("0.08"))
            self.assertEqual(result.quantile_returns[3].mean_future_return, Decimal("-0.01"))
            self.assertEqual(result.quantile_returns[4].mean_future_return, Decimal("0.02"))
            self.assertEqual(result.quantile_monotonicity, "NOT_MONOTONIC")

    def test_small_sample_confidence_interval_is_marked_unavailable(self) -> None:
        signal = _signal({"000001": Decimal("0.20"), "000002": Decimal("0.10")})
        rows = _label_rows({"000001": Decimal("0.05"), "000002": Decimal("0.01")})

        with tempfile.TemporaryDirectory() as tmpdir:
            labels = _label_portal(tmpdir, rows).query_future_outcome_inputs(
                ["000001", "000002"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            result = evaluate_momentum_rank_ic([signal], labels, MATURE_EVAL_ASOF)

            self.assertIn("moving_block_bootstrap_non_iid", result.ci_method)
            self.assertIn("block_length=21", result.ci_method)
            self.assertIn("ci_unavailable_insufficient_samples(n=1<=block=21)", result.ci_method)
            self.assertNotIn("iid", result.ci_method.replace("non_iid", ""))
            self.assertEqual(result.ci_bounds, (None, None))
            self.assertNotIsInstance(result.ci_bounds[0], Decimal)
            self.assertNotIsInstance(result.ci_bounds[1], Decimal)

    def test_confidence_interval_uses_block_bootstrap_when_sample_exceeds_block_length(self) -> None:
        signal = _signal({"000001": Decimal("0.20"), "000002": Decimal("0.10")})
        rows = _label_rows({"000001": Decimal("0.05"), "000002": Decimal("0.01")})

        with tempfile.TemporaryDirectory() as tmpdir:
            labels = _label_portal(tmpdir, rows).query_future_outcome_inputs(
                ["000001", "000002"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            result = evaluate_momentum_rank_ic([signal] * 22, labels, MATURE_EVAL_ASOF)

            self.assertIn("moving_block_bootstrap_non_iid", result.ci_method)
            self.assertIn("block_length=21", result.ci_method)
            self.assertNotIn("ci_unavailable_insufficient_samples", result.ci_method)
            self.assertNotIn("iid", result.ci_method.replace("non_iid", ""))
            self.assertEqual(result.ci_bounds, (Decimal("1"), Decimal("1")))

    def test_ic_output_is_exploratory_tainted_and_warns_survivor_bias(self) -> None:
        signal = _signal({"000001": Decimal("0.20"), "000002": Decimal("0.10")})
        rows = _label_rows({"000001": Decimal("0.05"), "000002": Decimal("0.01")})

        with tempfile.TemporaryDirectory() as tmpdir:
            labels = _label_portal(tmpdir, rows).query_future_outcome_inputs(
                ["000001", "000002"],
                ENTRY_TS,
                EXIT_TS,
                LabelSpec(),
            )
            result = evaluate_momentum_rank_ic([signal], labels, MATURE_EVAL_ASOF)

            self.assertEqual(result.evidence_status, "EXPLORATORY_TAINTED")
            self.assertIn("survivor-bias", result.survivor_bias_warning)
            self.assertIn("do not treat as alpha evidence", result.survivor_bias_warning)
            self.assertEqual(result.holding_period_days, 21)


def _signal(scores: dict[str, Decimal]) -> CrossSectionalMomentumSignal:
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ranks = {security_id: index + 1 for index, (security_id, _) in enumerate(ordered)}
    points = tuple(
        CrossSectionalMomentumPoint(
            security_id,
            score,
            ranks[security_id],
            AdjustedReturnStatus.OK,
        )
        for security_id, score in scores.items()
    )
    return CrossSectionalMomentumSignal(
        asof_ts=SIGNAL_ASOF,
        score_asof_ts="2025-12-08T15:00:00+08:00",
        derivation_asof_ts=SIGNAL_ASOF,
        ranking_method="ordinal_descending_rank",
        lookback_trading_days=231,
        skip_recent_trading_days=21,
        points=points,
        excluded_securities=tuple(),
        exclusion_reason_counts=tuple(),
        universe_size_after_exclusion=len(points),
        evidence_status="EXPLORATORY_TAINTED",
        input_snapshot_id="signal-fixture",
        signal_manifest_hash="signal-hash-fixture",
    )


def _label_portal(tmpdir: str, rows: list[dict[str, object]]) -> LabelDataPortal:
    path = Path(tmpdir) / "future_return_labels.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return LabelDataPortal(path)


def _label_rows(
    returns: dict[str, Decimal],
    *,
    overrides: dict[str, dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    overrides = overrides or {}
    for security_id, future_return in returns.items():
        row = {
            "security_id": security_id,
            "signal_asof_ts": SIGNAL_ASOF,
            "entry_ts": ENTRY_TS,
            "exit_ts": EXIT_TS,
            "future_return": str(future_return),
            "label_end_ts": EXIT_TS,
            "label_observed_at": "2026-02-09T15:00:00+08:00",
            "label_spec": LabelSpec().name,
            "price_basis": PriceBasis.PIT_DERIVED.value,
            "corporate_action_manifest": "pit-ca-fixture",
            "snapshot_id": "label-fixture",
        }
        row.update(overrides.get(security_id, {}))
        rows.append(row)
    return rows


def _ranked_label_ids(
    signal: CrossSectionalMomentumSignal,
    labels,
    result,
) -> tuple[str, ...]:
    if result.rank_ic_series[0].rank_ic is None:
        return tuple()
    mature = {
        label.security_id
        for label in labels
        if pd.Timestamp(label.label_observed_at) <= pd.Timestamp(MATURE_EVAL_ASOF)
        and pd.Timestamp(label.label_end_ts) <= pd.Timestamp(MATURE_EVAL_ASOF)
    }
    return tuple(
        point.security_id
        for point in signal.points
        if point.security_id in mature and point.momentum_score is not None
    )


if __name__ == "__main__":
    unittest.main()
