from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.domain import DataContractError, TradeStatus
from src.engine.event_clock import ClockContext
from src.features.cross_sectional_momentum import (
    LOOKBACK_TRADING_DAYS,
    RANKING_METHOD,
    SKIP_RECENT_TRADING_DAYS,
    CrossSectionalMomentumExclusion,
    CrossSectionalMomentumPoint,
    CrossSectionalMomentumSignal,
    ExclusionReasonCount,
)
from src.features.pit_adjustment_service import AdjustedReturnStatus, EVIDENCE_STATUS
from src.market_calendar import TradingCalendar
from src.portfolio.momentum_strategy import (
    MomentumStrategyConfig,
    SignalDrivenMomentumStrategy,
    build_equal_weight_targets,
)


SIGNAL_PROJECTION_COLUMNS = (
    "signal_asof_ts",
    "score_asof_ts",
    "security_id",
    "signal_status",
    "momentum_score",
    "cross_sectional_rank",
)
FORBIDDEN_FUTURE_COLUMNS = frozenset(
    {
        "future_return",
        "label_status",
        "label_end_ts",
        "label_observed_at",
        "rank_ic",
    }
)


@dataclass(frozen=True)
class OrderedSignalBinding:
    signal_dates: tuple[date, ...]
    signal_manifest_hashes: tuple[str, ...]
    predictions_sha256: str
    projected_columns: tuple[str, ...]
    signals_by_date: dict[date, CrossSectionalMomentumSignal]


def load_ordered_signal_binding(
    *,
    predictions_path: Path,
    feature_manifest_path: Path,
    calendar: TradingCalendar,
    start_date: date,
    end_date: date,
    expected_security_ids: Iterable[str],
) -> OrderedSignalBinding:
    if FORBIDDEN_FUTURE_COLUMNS.intersection(SIGNAL_PROJECTION_COLUMNS):
        raise AssertionError("future columns must never enter signal projection")
    # Column projection is the physical leakage boundary: pandas/pyarrow does
    # not load label or future-return columns into this process.
    frame = pd.read_parquet(
        predictions_path,
        columns=list(SIGNAL_PROJECTION_COLUMNS),
    )
    manifest = json.loads(feature_manifest_path.read_text(encoding="utf-8"))
    signal_dates = tuple(calendar.between(start_date, end_date))
    if not signal_dates or signal_dates[0] != start_date or signal_dates[-1] != end_date:
        raise DataContractError(
            "real TradingCalendar does not cover the 4b signal window"
        )

    _require_projection_columns(frame)
    frame = frame.copy()
    frame["_signal_ts"] = frame["signal_asof_ts"].map(pd.Timestamp)
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() is None
        for timestamp in frame["_signal_ts"]
    ):
        raise DataContractError("projected signal timestamps must be timezone-aware")
    frame["_signal_date"] = frame["_signal_ts"].map(lambda value: value.date())
    observed_dates = tuple(sorted(frame["_signal_date"].unique().tolist()))
    if observed_dates != signal_dates:
        raise DataContractError(
            "predictions signal dates do not match real TradingCalendar exactly"
        )
    if frame.duplicated(["_signal_date", "security_id"], keep=False).any():
        raise DataContractError(
            "predictions contain duplicate signal_date/security_id keys"
        )
    counts = frame.groupby("_signal_date")["security_id"].nunique()
    if counts.empty or int(counts.max()) > len(tuple(expected_security_ids)):
        raise DataContractError("predictions contain more than 12 securities per day")

    manifest_hashes = tuple(manifest.get("signal_manifest_hashes", ()))
    if manifest.get("signal_day_count") != len(signal_dates):
        raise DataContractError(
            "feature manifest signal_day_count does not match calendar"
        )
    if len(manifest_hashes) != len(signal_dates):
        raise DataContractError(
            "feature manifest must contain one ordered hash per signal day"
        )

    expected_ids = {
        str(security_id).zfill(6) for security_id in expected_security_ids
    }
    signals_by_date: dict[date, CrossSectionalMomentumSignal] = {}
    for signal_date, manifest_hash in zip(signal_dates, manifest_hashes):
        day_rows = frame.loc[frame["_signal_date"].eq(signal_date)].copy()
        observed_ids = set(
            day_rows["security_id"].astype(str).str.zfill(6).tolist()
        )
        if not observed_ids.issubset(expected_ids):
            raise DataContractError(
                f"predictions contain out-of-slice securities on {signal_date}"
            )
        signals_by_date[signal_date] = _signal_from_projected_rows(
            day_rows,
            str(manifest_hash),
        )

    return OrderedSignalBinding(
        signal_dates=signal_dates,
        signal_manifest_hashes=manifest_hashes,
        predictions_sha256=_sha256_file(predictions_path),
        projected_columns=SIGNAL_PROJECTION_COLUMNS,
        signals_by_date=signals_by_date,
    )


def monthly_first_trading_days(
    calendar: TradingCalendar,
    start_date: date,
    end_date: date,
) -> tuple[date, ...]:
    monthly: dict[tuple[int, int], date] = {}
    for trade_date in calendar.between(start_date, end_date):
        monthly.setdefault((trade_date.year, trade_date.month), trade_date)
    return tuple(monthly.values())


@dataclass
class PrecomputedMomentumStrategy:
    binding: OrderedSignalBinding
    portfolio_ledger: object
    config: MomentumStrategyConfig
    rebalance_dates: frozenset[date]
    _delegate: SignalDrivenMomentumStrategy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # The delegate is never allowed to run on_bar; it is used only for the
        # already-tested target-to-OrderIntent diff path.
        security_ids = tuple(
            sorted(
                {
                    point.security_id
                    for signal in self.binding.signals_by_date.values()
                    for point in signal.points
                }
            )
        )
        self._delegate = SignalDrivenMomentumStrategy(
            security_ids=security_ids,
            adjustment_service=None,  # type: ignore[arg-type]
            portfolio_ledger=self.portfolio_ledger,
            config=self.config,
        )

    def on_bar(self, ctx: ClockContext):
        if ctx.trade_date not in self.rebalance_dates:
            return []
        signal = self.binding.signals_by_date.get(ctx.trade_date)
        if signal is None:
            raise DataContractError(
                f"precomputed signal missing rebalance date {ctx.trade_date}"
            )
        tradable = set(self._delegate._tradable_universe(ctx))  # noqa: SLF001
        filtered_signal = _filter_signal_to_tradable(signal, tradable)
        targets = build_equal_weight_targets(
            filtered_signal,
            top_n=self.config.top_n,
            max_single_name_weight=self.config.max_single_name_weight,
        )
        self._delegate.latest_signal = filtered_signal
        self._delegate.latest_target_weights = targets
        return self._delegate._diff_to_order_intents(  # noqa: SLF001
            ctx,
            filtered_signal,
            targets,
        )


def _signal_from_projected_rows(
    rows: pd.DataFrame,
    manifest_hash: str,
) -> CrossSectionalMomentumSignal:
    points: list[CrossSectionalMomentumPoint] = []
    exclusions: list[CrossSectionalMomentumExclusion] = []
    for row in rows.sort_values("security_id").itertuples(index=False):
        status = AdjustedReturnStatus(str(row.signal_status))
        momentum_score = (
            None
            if pd.isna(row.momentum_score)
            else Decimal(str(row.momentum_score))
        )
        cross_sectional_rank = (
            None
            if pd.isna(row.cross_sectional_rank)
            else int(row.cross_sectional_rank)
        )
        security_id = str(row.security_id).zfill(6)
        points.append(
            CrossSectionalMomentumPoint(
                security_id=security_id,
                momentum_score=momentum_score,
                cross_sectional_rank=cross_sectional_rank,
                status=status,
            )
        )
        if status != AdjustedReturnStatus.OK:
            exclusions.append(
                CrossSectionalMomentumExclusion(
                    security_id=security_id,
                    status=status,
                    reason=status.value,
                )
            )
    counts = Counter(exclusion.status for exclusion in exclusions)
    first = rows.iloc[0]
    return CrossSectionalMomentumSignal(
        asof_ts=str(first["signal_asof_ts"]),
        score_asof_ts=str(first["score_asof_ts"]),
        derivation_asof_ts=str(first["signal_asof_ts"]),
        ranking_method=RANKING_METHOD,
        lookback_trading_days=LOOKBACK_TRADING_DAYS,
        skip_recent_trading_days=SKIP_RECENT_TRADING_DAYS,
        points=tuple(points),
        excluded_securities=tuple(exclusions),
        exclusion_reason_counts=tuple(
            ExclusionReasonCount(status, count)
            for status, count in sorted(counts.items(), key=lambda item: item[0].value)
        ),
        universe_size_after_exclusion=sum(
            point.status == AdjustedReturnStatus.OK for point in points
        ),
        evidence_status=EVIDENCE_STATUS,
        input_snapshot_id="4a_ordered_signal_projection",
        signal_manifest_hash=manifest_hash,
    )


def _filter_signal_to_tradable(
    signal: CrossSectionalMomentumSignal,
    tradable: set[str],
) -> CrossSectionalMomentumSignal:
    points = tuple(
        point for point in signal.points if point.security_id in tradable
    )
    excluded = tuple(
        item for item in signal.excluded_securities if item.security_id in tradable
    )
    counts = Counter(item.status for item in excluded)
    return replace(
        signal,
        points=points,
        excluded_securities=excluded,
        exclusion_reason_counts=tuple(
            ExclusionReasonCount(status, count)
            for status, count in sorted(counts.items(), key=lambda item: item[0].value)
        ),
        universe_size_after_exclusion=sum(
            point.status == AdjustedReturnStatus.OK for point in points
        ),
    )


def _require_projection_columns(frame: pd.DataFrame) -> None:
    missing = [
        column for column in SIGNAL_PROJECTION_COLUMNS if column not in frame.columns
    ]
    if missing:
        raise DataContractError(
            f"predictions missing projected signal columns: {missing}"
        )
    unexpected = FORBIDDEN_FUTURE_COLUMNS.intersection(frame.columns)
    if unexpected:
        raise DataContractError(
            f"future columns crossed physical signal projection: {sorted(unexpected)}"
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
