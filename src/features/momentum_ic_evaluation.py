from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

import pandas as pd

from src.domain import DataContractError, PriceBasis
from src.features.cross_sectional_momentum import CrossSectionalMomentumSignal
from src.features.pit_adjustment_service import AdjustedReturnStatus, EVIDENCE_STATUS
from src.labels import FutureReturnLabel


HOLDING_PERIOD_DAYS = 21
CI_BLOCK_LENGTH = HOLDING_PERIOD_DAYS
CI_ITERATIONS = 500
SURVIVOR_BIAS_WARNING = (
    "EXPLORATORY_TAINTED: IC uses the supplied signal universe and may retain "
    "survivor-bias or current-constituent taint; do not treat as alpha evidence."
)


@dataclass(frozen=True)
class RankICPoint:
    asof_ts: str
    rank_ic: Decimal | None
    sample_size: int
    universe_size: int
    immature_label_count: int
    missing_label_count: int


@dataclass(frozen=True)
class CoveragePoint:
    asof_ts: str
    sample_size: int
    universe_size: int
    coverage: Decimal


@dataclass(frozen=True)
class QuantileReturn:
    quantile: int
    mean_future_return: Decimal | None
    sample_count: int


@dataclass(frozen=True)
class MomentumICEvaluationResult:
    rank_ic_series: tuple[RankICPoint, ...]
    rank_ic_mean: Decimal | None
    icir: Decimal | None
    quantile_returns: tuple[QuantileReturn, ...]
    quantile_monotonicity: str
    coverage_series: tuple[CoveragePoint, ...]
    ci_method: str
    ci_bounds: tuple[Decimal | None, Decimal | None]
    holding_period_days: int
    evidence_status: str
    survivor_bias_warning: str
    immature_label_count: int
    missing_label_count: int
    input_snapshot_id: str


def evaluate_momentum_rank_ic(
    signals: Iterable[CrossSectionalMomentumSignal],
    future_returns: Iterable[FutureReturnLabel],
    evaluation_asof_ts: str | pd.Timestamp,
) -> MomentumICEvaluationResult:
    evaluation_asof = _timestamp(evaluation_asof_ts, "evaluation_asof_ts")
    labels = tuple(future_returns)
    for label in labels:
        _assert_label_price_basis(label)
    labels_by_key = {(label.signal_asof_ts, label.security_id): label for label in labels}

    rank_ic_points: list[RankICPoint] = []
    coverage_points: list[CoveragePoint] = []
    all_pairs: list[tuple[str, str, Decimal, Decimal]] = []
    snapshot_ids: list[str] = []

    for signal in signals:
        _assert_signal_taint(signal)
        daily_scores: dict[str, Decimal] = {}
        daily_returns: dict[str, Decimal] = {}
        immature_count = 0
        missing_count = 0

        for point in signal.points:
            if point.status != AdjustedReturnStatus.OK or point.momentum_score is None:
                continue
            daily_scores[point.security_id] = point.momentum_score
            label = labels_by_key.get((signal.asof_ts, point.security_id))
            if label is None or label.future_return is None:
                missing_count += 1
                continue
            snapshot_ids.append(label.input_snapshot_id)
            if not _is_mature(label, evaluation_asof):
                immature_count += 1
                continue
            daily_returns[point.security_id] = label.future_return

        usable_ids = tuple(security_id for security_id in daily_scores if security_id in daily_returns)
        rank_ic = None
        if len(usable_ids) >= 2:
            score_values = {security_id: daily_scores[security_id] for security_id in usable_ids}
            return_values = {security_id: daily_returns[security_id] for security_id in usable_ids}
            rank_ic = _spearman_rank_ic(score_values, return_values)
            for security_id in usable_ids:
                all_pairs.append((signal.asof_ts, security_id, daily_scores[security_id], daily_returns[security_id]))

        universe_size = signal.universe_size_after_exclusion
        sample_size = len(usable_ids)
        rank_ic_points.append(
            RankICPoint(
                signal.asof_ts,
                rank_ic,
                sample_size,
                universe_size,
                immature_count,
                missing_count,
            )
        )
        coverage_points.append(
            CoveragePoint(
                signal.asof_ts,
                sample_size,
                universe_size,
                Decimal(sample_size) / Decimal(universe_size) if universe_size else Decimal("0"),
            )
        )

    rank_ics = [point.rank_ic for point in rank_ic_points if point.rank_ic is not None]
    rank_ic_mean = _mean(rank_ics)
    icir = _icir(rank_ics)
    ci_bounds = _block_bootstrap_ci(rank_ics, CI_BLOCK_LENGTH, CI_ITERATIONS)
    ci_method = _ci_method(rank_ics, CI_BLOCK_LENGTH, CI_ITERATIONS)
    input_snapshot_id = _join_snapshot_ids(snapshot_ids)
    quantile_returns = _quantile_returns(all_pairs)
    return MomentumICEvaluationResult(
        rank_ic_series=tuple(rank_ic_points),
        rank_ic_mean=rank_ic_mean,
        icir=icir,
        quantile_returns=quantile_returns,
        quantile_monotonicity=_quantile_monotonicity(quantile_returns),
        coverage_series=tuple(coverage_points),
        ci_method=ci_method,
        ci_bounds=ci_bounds,
        holding_period_days=HOLDING_PERIOD_DAYS,
        evidence_status=EVIDENCE_STATUS,
        survivor_bias_warning=SURVIVOR_BIAS_WARNING,
        immature_label_count=sum(point.immature_label_count for point in rank_ic_points),
        missing_label_count=sum(point.missing_label_count for point in rank_ic_points),
        input_snapshot_id=input_snapshot_id,
    )


def _assert_signal_taint(signal: CrossSectionalMomentumSignal) -> None:
    if signal.evidence_status != EVIDENCE_STATUS:
        raise DataContractError("momentum IC requires EXPLORATORY_TAINTED signal inputs")


def _assert_label_price_basis(label: FutureReturnLabel) -> None:
    if label.price_basis != PriceBasis.PIT_DERIVED:
        raise DataContractError("momentum IC future return labels must use PIT_DERIVED price basis")


def _is_mature(label: FutureReturnLabel, evaluation_asof: pd.Timestamp) -> bool:
    label_end = _timestamp(label.label_end_ts, "label_end_ts")
    observed = _timestamp(label.label_observed_at, "label_observed_at")
    return label_end <= evaluation_asof and observed <= evaluation_asof


def _spearman_rank_ic(scores: dict[str, Decimal], future_returns: dict[str, Decimal]) -> Decimal:
    if scores.keys() != future_returns.keys():
        raise DataContractError("RankIC score/return keys must match")
    n = len(scores)
    if n < 2:
        raise DataContractError("RankIC requires at least two paired samples")
    score_ranks = _descending_ordinal_ranks(scores)
    return_ranks = _descending_ordinal_ranks(future_returns)
    squared_diff = sum((score_ranks[security_id] - return_ranks[security_id]) ** 2 for security_id in scores)
    return Decimal("1") - (Decimal(6 * squared_diff) / Decimal(n * (n * n - 1)))


def _descending_ordinal_ranks(values: dict[str, Decimal]) -> dict[str, int]:
    ordered = sorted(values.items(), key=lambda item: (-item[1], item[0]))
    return {security_id: index + 1 for index, (security_id, _) in enumerate(ordered)}


def _quantile_returns(
    pairs: list[tuple[str, str, Decimal, Decimal]],
) -> tuple[QuantileReturn, ...]:
    returns_by_quantile: dict[int, list[Decimal]] = {quantile: [] for quantile in range(1, 6)}
    by_day: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for asof_ts, security_id, score, future_return in pairs:
        by_day.setdefault(asof_ts, []).append((security_id, score, future_return))

    for rows in by_day.values():
        ordered = sorted(rows, key=lambda item: (-item[1], item[0]))
        n = len(ordered)
        for index, (_, _, future_return) in enumerate(ordered):
            quantile = min(5, (index * 5 // n) + 1)
            returns_by_quantile[quantile].append(future_return)

    return tuple(
        QuantileReturn(
            quantile,
            _mean(values),
            len(values),
        )
        for quantile, values in returns_by_quantile.items()
    )


def _quantile_monotonicity(quantile_returns: tuple[QuantileReturn, ...]) -> str:
    values = [item.mean_future_return for item in quantile_returns if item.mean_future_return is not None]
    if len(values) < 2:
        return "INSUFFICIENT"
    if all(left <= right for left, right in zip(values, values[1:])):
        return "INCREASING"
    if all(left >= right for left, right in zip(values, values[1:])):
        return "DECREASING"
    return "NOT_MONOTONIC"


def _icir(values: list[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = _mean(values)
    assert mean is not None
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    if variance == 0:
        return None
    return Decimal(str(float(mean) / (float(variance) ** 0.5)))


def _block_bootstrap_ci(
    values: list[Decimal],
    block_length: int,
    iterations: int,
) -> tuple[Decimal | None, Decimal | None]:
    if not values:
        return None, None
    if len(values) <= block_length:
        return None, None

    blocks = [values[index : index + block_length] for index in range(0, len(values) - block_length + 1)]
    rng = random.Random(0)
    bootstrapped_means: list[Decimal] = []
    for _ in range(iterations):
        sampled: list[Decimal] = []
        while len(sampled) < len(values):
            sampled.extend(rng.choice(blocks))
        bootstrapped_means.append(_mean(sampled[: len(values)]) or Decimal("0"))
    ordered = sorted(bootstrapped_means)
    lower_index = int(Decimal("0.025") * Decimal(len(ordered) - 1))
    upper_index = int(Decimal("0.975") * Decimal(len(ordered) - 1))
    return ordered[lower_index], ordered[upper_index]


def _ci_method(values: list[Decimal], block_length: int, iterations: int) -> str:
    method = f"moving_block_bootstrap_non_iid(block_length={block_length},iterations={iterations})"
    if len(values) <= block_length:
        return f"{method};ci_unavailable_insufficient_samples(n={len(values)}<=block={block_length})"
    return method


def _mean(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _join_snapshot_ids(snapshot_ids: Iterable[str]) -> str:
    pieces: list[str] = []
    for snapshot_id in snapshot_ids:
        if not snapshot_id:
            continue
        pieces.extend(piece for piece in snapshot_id.split(";") if piece)
    return ";".join(sorted(dict.fromkeys(pieces)))


def _timestamp(value: str | pd.Timestamp, label: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError(f"{label} must be timezone-aware")
    return timestamp.tz_convert("Asia/Shanghai")
