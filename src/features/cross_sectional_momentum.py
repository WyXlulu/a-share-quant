from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd

from src.domain import DataContractError
from src.features.pit_adjustment_service import (
    AdjustedReturnStatus,
    CumulativeAdjustedReturnResult,
    EVIDENCE_STATUS,
    PITAdjustmentService,
)


# 12-1 momentum spans 252 trading days in total, skips the most recent 21
# trading days, then scores the remaining 231-trading-day momentum window.
FULL_LOOKBACK_SPAN_TRADING_DAYS = 252
LOOKBACK_TRADING_DAYS = 231
SKIP_RECENT_TRADING_DAYS = 21
RANKING_METHOD = "ordinal_descending_rank"


@dataclass(frozen=True)
class CrossSectionalMomentumPoint:
    security_id: str
    momentum_score: Decimal | None
    cross_sectional_rank: int | None
    status: AdjustedReturnStatus


@dataclass(frozen=True)
class CrossSectionalMomentumExclusion:
    security_id: str
    status: AdjustedReturnStatus
    reason: str


@dataclass(frozen=True)
class ExclusionReasonCount:
    status: AdjustedReturnStatus
    count: int


@dataclass(frozen=True)
class CrossSectionalMomentumSignal:
    asof_ts: str
    score_asof_ts: str
    derivation_asof_ts: str
    ranking_method: str
    lookback_trading_days: int
    skip_recent_trading_days: int
    points: tuple[CrossSectionalMomentumPoint, ...]
    excluded_securities: tuple[CrossSectionalMomentumExclusion, ...]
    exclusion_reason_counts: tuple[ExclusionReasonCount, ...]
    universe_size_after_exclusion: int
    evidence_status: str
    input_snapshot_id: str
    signal_manifest_hash: str


def calculate_cross_sectional_momentum_signal(
    security_ids: Iterable[str],
    asof_ts: Any,
    derivation_asof_ts: Any,
    adjustment_service: PITAdjustmentService,
) -> CrossSectionalMomentumSignal:
    """Compute fixed 12-1 cross-sectional momentum from PIT-adjusted returns.

    Ranking uses deterministic ordinal rank, descending by momentum score and
    then security_id for tie breaks. This keeps the first signal parameter-free
    and directly auditable from hand-calculated cumulative returns.
    """
    securities = tuple(dict.fromkeys(str(security_id).zfill(6) for security_id in security_ids))
    asof = _asof_timestamp(asof_ts)
    derivation_asof = _asof_timestamp(derivation_asof_ts)
    score_date = _score_asof_date(
        securities,
        asof,
        derivation_asof,
        adjustment_service,
    )
    score_asof = pd.Timestamp(datetime.combine(score_date, time(15, 0, 0)), tz=asof.tz)

    cumulative_results: list[CumulativeAdjustedReturnResult] = []
    included: list[CumulativeAdjustedReturnResult] = []
    excluded: list[CrossSectionalMomentumExclusion] = []
    for security_id in securities:
        result = adjustment_service.cumulative_adjusted_return(
            security_id,
            score_asof,
            LOOKBACK_TRADING_DAYS,
            derivation_asof,
        )
        cumulative_results.append(result)
        if result.status == AdjustedReturnStatus.OK and result.adjusted_return is not None:
            included.append(result)
            continue
        excluded.append(
            CrossSectionalMomentumExclusion(
                security_id=result.security_id,
                status=result.status,
                reason=result.block_reason or result.status.value,
            )
        )

    ranked_scores = _rank_scores(included)
    points = tuple(
        CrossSectionalMomentumPoint(
            result.security_id,
            result.adjusted_return if result.security_id in ranked_scores else None,
            ranked_scores.get(result.security_id),
            result.status,
        )
        for result in cumulative_results
    )
    counts = _exclusion_counts(excluded)
    input_snapshot_id = _join_snapshot_ids(result.input_snapshot_id for result in cumulative_results)
    manifest_hash = _manifest_hash(
        asof.isoformat(),
        score_asof.isoformat(),
        derivation_asof.isoformat(),
        securities,
        points,
        excluded,
        counts,
        input_snapshot_id,
    )
    return CrossSectionalMomentumSignal(
        asof_ts=asof.isoformat(),
        score_asof_ts=score_asof.isoformat(),
        derivation_asof_ts=derivation_asof.isoformat(),
        ranking_method=RANKING_METHOD,
        lookback_trading_days=LOOKBACK_TRADING_DAYS,
        skip_recent_trading_days=SKIP_RECENT_TRADING_DAYS,
        points=points,
        excluded_securities=tuple(excluded),
        exclusion_reason_counts=counts,
        universe_size_after_exclusion=len(included),
        evidence_status=EVIDENCE_STATUS,
        input_snapshot_id=input_snapshot_id,
        signal_manifest_hash=manifest_hash,
    )


def _score_asof_date(
    securities: tuple[str, ...],
    asof: pd.Timestamp,
    derivation_asof: pd.Timestamp,
    adjustment_service: PITAdjustmentService,
) -> date:
    if not securities:
        raise ValueError("security_ids must not be empty")
    trade_dates = _visible_trading_dates(securities, asof, derivation_asof, adjustment_service)
    index = bisect_left(trade_dates, asof.date())
    if index >= len(trade_dates) or trade_dates[index] != asof.date():
        index -= 1
    target_index = index - SKIP_RECENT_TRADING_DAYS
    if target_index < 0:
        raise DataContractError(
            "not enough visible trading days to skip recent window for momentum signal"
        )
    return trade_dates[target_index]


def _visible_trading_dates(
    securities: tuple[str, ...],
    asof: pd.Timestamp,
    derivation_asof: pd.Timestamp,
    adjustment_service: PITAdjustmentService,
) -> tuple[date, ...]:
    trade_dates: set[date] = set()
    for security_id in securities:
        bars = adjustment_service._daily_bars(security_id, derivation_asof)  # noqa: SLF001
        if "trade_date_key" not in bars.columns:
            raise DataContractError("PITAdjustmentService daily bars missing trade_date_key")
        trade_dates.update(
            trade_date
            for trade_date in bars["trade_date_key"].dropna().tolist()
            if trade_date <= asof.date()
        )
    if not trade_dates:
        raise DataContractError("no visible trading dates for momentum signal")
    return tuple(sorted(trade_dates))


def _rank_scores(results: list[CumulativeAdjustedReturnResult]) -> dict[str, int]:
    ordered = sorted(
        results,
        key=lambda result: (-(result.adjusted_return or Decimal("0")), result.security_id),
    )
    return {result.security_id: index + 1 for index, result in enumerate(ordered)}


def _exclusion_counts(
    excluded: list[CrossSectionalMomentumExclusion],
) -> tuple[ExclusionReasonCount, ...]:
    counts = {
        AdjustedReturnStatus.BLOCKED: 0,
        AdjustedReturnStatus.NO_DATA: 0,
    }
    for item in excluded:
        if item.status in counts:
            counts[item.status] += 1
    return tuple(
        ExclusionReasonCount(status, count)
        for status, count in counts.items()
        if count > 0
    )


def _join_snapshot_ids(snapshot_ids: Iterable[str]) -> str:
    pieces: list[str] = []
    for snapshot_id in snapshot_ids:
        if not snapshot_id:
            continue
        pieces.extend(piece for piece in snapshot_id.split(";") if piece)
    return ";".join(sorted(dict.fromkeys(pieces)))


def _manifest_hash(
    asof_ts: str,
    score_asof_ts: str,
    derivation_asof_ts: str,
    securities: tuple[str, ...],
    points: tuple[CrossSectionalMomentumPoint, ...],
    excluded: list[CrossSectionalMomentumExclusion],
    counts: tuple[ExclusionReasonCount, ...],
    input_snapshot_id: str,
) -> str:
    manifest = {
        "asof_ts": asof_ts,
        "score_asof_ts": score_asof_ts,
        "derivation_asof_ts": derivation_asof_ts,
        "ranking_method": RANKING_METHOD,
        "lookback_trading_days": LOOKBACK_TRADING_DAYS,
        "skip_recent_trading_days": SKIP_RECENT_TRADING_DAYS,
        "security_ids": securities,
        "points": [
            {
                "security_id": point.security_id,
                "momentum_score": str(point.momentum_score) if point.momentum_score is not None else None,
                "cross_sectional_rank": point.cross_sectional_rank,
                "status": point.status.value,
            }
            for point in points
        ],
        "excluded_securities": [
            {
                "security_id": item.security_id,
                "status": item.status.value,
                "reason": item.reason,
            }
            for item in excluded
        ],
        "exclusion_reason_counts": [
            {"status": item.status.value, "count": item.count}
            for item in counts
        ],
        "input_snapshot_id": input_snapshot_id,
        "evidence_status": EVIDENCE_STATUS,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _asof_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError("momentum asof timestamp must be timezone-aware")
    return timestamp.tz_convert("Asia/Shanghai")
