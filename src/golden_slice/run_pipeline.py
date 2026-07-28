from __future__ import annotations

import argparse
import json
import shutil
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import PITDataPortal
from src.data.corporate_action_availability import resolve_ca_available_at
from src.domain import DataContractError
from src.engine.backtest_runner import CachedPITDataPortal
from src.features.cross_sectional_momentum import (
    FULL_LOOKBACK_SPAN_TRADING_DAYS,
    LOOKBACK_TRADING_DAYS,
    SKIP_RECENT_TRADING_DAYS,
    CrossSectionalMomentumSignal,
    calculate_cross_sectional_momentum_signal,
)
from src.features.momentum_ic_evaluation import (
    CI_BLOCK_LENGTH,
    MomentumICEvaluationResult,
    evaluate_momentum_rank_ic,
)
from src.features.pit_adjustment_service import (
    AdjustedReturnStatus,
    PITAdjustmentService,
)
from src.golden_slice.manifest import (
    GOLDEN_SLICE_SECURITY_IDS,
    assert_frozen_and_consistent,
)
from src.golden_slice.snapshot import (
    SNAPSHOT_END_DATE,
    SNAPSHOT_START_DATE,
    GoldenSliceSnapshot,
    build_adjustment_only_snapshot,
    load_adjustment_only_snapshot,
)
from src.labels import FutureReturnLabel
from src.labels.label_return_calculator import calculate_future_return_labels
from src.market_calendar import TradingCalendar, trading_calendar_from_dates


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
EXPECTED_MANIFEST_HASH = (
    "94e025b6a0b259c56751c6d3f3953c4a804aeea1c85b3b73dac9f9b2f468d4ae"
)
EVIDENCE_STATUS = "EXPLORATORY_TAINTED"
AUDIT_STATUS = "PENDING_AUDIT"
VALIDATION_SCOPE = "GOLDEN_SLICE_PIPELINE"
SIGNAL_START_DATE = date(2020, 1, 2)
SIGNAL_END_DATE = date(2023, 12, 29)
BLOCKED_RATE_LIMIT = Decimal("0.05")

DEFAULT_MANIFEST_PATH = Path("src/golden_slice/golden_slice_manifest.json")
DEFAULT_L1_PATH = Path("data/l1_raw/daily_bar_raw.parquet")
DEFAULT_CALENDAR_PATH = Path("data/l1_raw/trading_calendar.parquet")
DEFAULT_L2_PATH = Path(
    "data/l2_corporate_actions/corporate_actions.parquet"
)
DEFAULT_ARTIFACTS_ROOT = Path("artifacts")


@dataclass(frozen=True)
class PipelineRunResult:
    experiment_id: str
    artifact_dir: Path
    snapshot: GoldenSliceSnapshot
    gate_manifest_hash: str
    portal_equivalence_check: dict[str, Any]
    standard_portal_interrupted_run: dict[str, str] | None
    cached_pipeline_elapsed_seconds: Decimal
    availability_samples: tuple[dict[str, str], ...]
    hengrui_reference_check: dict[str, str]
    return_status_counts: tuple[tuple[str, int], ...]
    blocked_reason_counts: tuple[tuple[str, int], ...]
    signal_day_count: int
    average_scorable_security_count: Decimal
    label_status_counts: tuple[tuple[str, int], ...]
    ic_result: MomentumICEvaluationResult
    akshare_only_count: int
    verified_only_count: int


def run_golden_slice_pipeline(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    l1_path: Path = DEFAULT_L1_PATH,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
    l2_path: Path = DEFAULT_L2_PATH,
    artifacts_root: Path = DEFAULT_ARTIFACTS_ROOT,
    run_date: date | None = None,
    standard_portal_cpu_seconds: Decimal | None = None,
    standard_portal_wall_seconds: Decimal | None = None,
) -> PipelineRunResult:
    # Governance gate is deliberately the first operation. No output directory
    # or snapshot may exist before the frozen selection has passed this check.
    manifest = _load_frozen_manifest(manifest_path)
    assert_frozen_and_consistent(manifest)
    manifest_hash = str(manifest.get("manifest_hash", ""))
    if manifest_hash != EXPECTED_MANIFEST_HASH:
        raise DataContractError(
            "golden slice manifest hash is not the approved freeze: "
            f"expected={EXPECTED_MANIFEST_HASH}, actual={manifest_hash}"
        )
    if (standard_portal_cpu_seconds is None) != (
        standard_portal_wall_seconds is None
    ):
        raise DataContractError(
            "standard Portal diagnostic requires both CPU and wall seconds"
        )
    standard_portal_interrupted_run = None
    if standard_portal_cpu_seconds is not None:
        standard_portal_interrupted_run = {
            "status": "INTERRUPTED_BY_OPERATOR",
            "cpu_seconds": str(standard_portal_cpu_seconds),
            "wall_seconds": str(standard_portal_wall_seconds),
            "reason": (
                "standard PITDataPortal daily full-chain run was healthy but "
                "too slow for observable completion"
            ),
        }

    effective_date = run_date or datetime.now(ASIA_SHANGHAI).date()
    snapshot_id = (
        f"golden_slice_{effective_date.isoformat()}_ADJUSTMENT_ONLY"
    )
    experiment_id = (
        "golden_slice_momentum_ic_"
        f"{effective_date.strftime('%Y%m%d')}_{manifest_hash[:8]}"
    )
    artifact_dir = artifacts_root / experiment_id
    calendar = _load_trading_calendar(calendar_path)
    snapshot_dir = artifact_dir / "snapshot"
    if artifact_dir.exists():
        _assert_only_reusable_snapshot_exists(artifact_dir)
        snapshot = load_adjustment_only_snapshot(
            output_dir=snapshot_dir,
            snapshot_id=snapshot_id,
        )
        snapshot_reused = True
    else:
        snapshot = build_adjustment_only_snapshot(
            manifest,
            l1_path=l1_path,
            output_dir=snapshot_dir,
            snapshot_id=snapshot_id,
        )
        snapshot_reused = False

    progress_path = artifact_dir / "progress.log"
    pipeline_started_at = perf_counter()
    _progress(
        progress_path,
        "frozen manifest gate PASS; "
        f"snapshot={'reused' if snapshot_reused else 'built'}; "
        "portal=CachedPITDataPortal",
    )
    table_paths = {
        "daily_bar_raw": snapshot.daily_bar_path,
        "corporate_actions": snapshot.corporate_action_path,
    }
    slow_portal = PITDataPortal(
        table_paths=table_paths,
        trading_calendar=calendar,
    )
    portal = CachedPITDataPortal(
        table_paths,
        calendar,
    )
    portal_equivalence_check = _assert_fast_slow_portal_equivalence(
        slow_portal,
        portal,
        calendar,
        progress_path,
    )
    service = PITAdjustmentService(portal=portal, calendar=calendar)

    availability_records, availability_samples = _assert_scheme_x_path(
        snapshot,
        portal,
        calendar,
    )
    _progress(
        progress_path,
        "scheme X path PASS: 76/76; starting combined-event check",
    )
    hengrui_checks = _assert_hengrui_combined_inputs(
        snapshot,
        service,
        calendar,
    )
    hengrui_reference_check = next(
        check
        for check in hengrui_checks
        if check["ex_date"] == "2020-05-25"
    )
    _progress(
        progress_path,
        "combined cash+stock event check PASS; starting full-window status scan",
    )

    status_diagnostics = _return_status_diagnostics(
        service,
        snapshot,
    )
    blocked_rate = Decimal(status_diagnostics["blocked_count"]) / Decimal(
        status_diagnostics["point_count"]
    )
    if blocked_rate > BLOCKED_RATE_LIMIT:
        raise DataContractError(
            "golden slice daily adjustment BLOCKED rate exceeds 5%; "
            f"blocked={status_diagnostics['blocked_count']}, "
            f"points={status_diagnostics['point_count']}, "
            f"reasons={status_diagnostics['blocked_reason_counts']}"
        )
    _progress(
        progress_path,
        "full-window status scan PASS: "
        f"{status_diagnostics['status_counts']}; starting daily signals",
    )

    signal_dates = calendar.between(SIGNAL_START_DATE, SIGNAL_END_DATE)
    if (
        not signal_dates
        or signal_dates[0] != SIGNAL_START_DATE
        or signal_dates[-1] != SIGNAL_END_DATE
    ):
        raise DataContractError(
            "real TradingCalendar does not cover the declared signal window"
        )

    signals: list[CrossSectionalMomentumSignal] = []
    labels: list[FutureReturnLabel] = []
    signal_loop_started_at = perf_counter()
    for index, signal_date in enumerate(signal_dates, start=1):
        signal_asof = _market_close_timestamp(signal_date)
        # Signal derivation is physically closed at T 15:00. Labels are created
        # only after the signal and internally use each sample's T+22 15:00.
        signals.append(
            calculate_cross_sectional_momentum_signal(
                GOLDEN_SLICE_SECURITY_IDS,
                signal_asof,
                signal_asof,
                service,
            )
        )
        labels.extend(
            calculate_future_return_labels(
                GOLDEN_SLICE_SECURITY_IDS,
                signal_asof,
                service,
                calendar,
            )
        )
        if index % 100 == 0 or index == len(signal_dates):
            elapsed = perf_counter() - signal_loop_started_at
            remaining = (elapsed / index) * (len(signal_dates) - index)
            _progress(
                progress_path,
                "signal progress: "
                f"{index}/{len(signal_dates)}; "
                f"elapsed={elapsed:.1f}s; eta={remaining:.1f}s",
            )

    _progress(progress_path, "daily signals and labels complete; starting RankIC")
    evaluation_asof = _market_close_timestamp(
        calendar.next_trading_day(SIGNAL_END_DATE, 22)
    )
    ic_result = evaluate_momentum_rank_ic(
        signals,
        labels,
        evaluation_asof,
    )
    scorable_counts = [
        signal.universe_size_after_exclusion for signal in signals
    ]
    average_scorable = (
        Decimal(sum(scorable_counts)) / Decimal(len(scorable_counts))
    )
    label_status_counts = Counter(label.status.value for label in labels)

    _progress(progress_path, "RankIC complete; starting L2 bidirectional cross-check")
    ca_diff = _cross_check_akshare_l2(
        manifest["verified_corporate_actions"],
        l2_path,
    )
    predictions = _prediction_frame(signals, labels, ic_result)
    predictions_path = artifact_dir / "predictions.parquet"
    predictions.to_parquet(predictions_path, index=False)
    cached_pipeline_elapsed_seconds = Decimal(
        str(perf_counter() - pipeline_started_at)
    )

    common_status = {
        "evidence_status": EVIDENCE_STATUS,
        "audit_status": AUDIT_STATUS,
        "validation_scope": VALIDATION_SCOPE,
        "validation_scope_manifest_hash": manifest_hash,
    }
    created_at = datetime.now(ASIA_SHANGHAI).isoformat(timespec="seconds")
    shutil.copyfile(
        manifest_path,
        artifact_dir / "golden_slice_manifest.json",
    )
    _write_json(
        artifact_dir / "data_lineage.json",
        {
            **common_status,
            "experiment_id": experiment_id,
            "created_at": created_at,
            "snapshot_id": snapshot.snapshot_id,
            "source_paths": {
                "frozen_manifest": manifest_path.as_posix(),
                "l1_daily_bar": l1_path.as_posix(),
                "trading_calendar": calendar_path.as_posix(),
                "akshare_l2_cross_check": l2_path.as_posix(),
            },
            "snapshot_paths": {
                "daily_bar_raw": snapshot.daily_bar_path.as_posix(),
                "corporate_actions": snapshot.corporate_action_path.as_posix(),
                "predictions": predictions_path.as_posix(),
            },
            "snapshot_hashes": {
                "daily_bar_raw_sha256": snapshot.daily_bar_sha256,
                "corporate_actions_sha256": snapshot.corporate_action_sha256,
            },
            "source_snapshot_ids": list(snapshot.source_snapshot_ids),
            "l1_row_count": snapshot.l1_row_count,
            "l1_rows_by_security": dict(snapshot.l1_rows_by_security),
            "ca_row_count": snapshot.ca_row_count,
            "price_basis": "RAW_UNADJUSTED",
            "snapshot_purpose": "ADJUSTMENT_ONLY",
            "execution_use_prohibited": True,
            "portal": "CachedPITDataPortal",
            "fast_slow_sample_equivalence": portal_equivalence_check,
        },
    )
    _write_json(
        artifact_dir / "feature_manifest.json",
        {
            **common_status,
            "feature": "cross_sectional_momentum_12_minus_1",
            "security_ids": list(GOLDEN_SLICE_SECURITY_IDS),
            "signal_window": {
                "start": SIGNAL_START_DATE.isoformat(),
                "end": SIGNAL_END_DATE.isoformat(),
            },
            "signal_frequency": "daily_trading_day",
            "signal_asof_policy": (
                "asof_ts=derivation_asof_ts=T 15:00 Asia/Shanghai"
            ),
            "full_lookback_span_trading_days": (
                FULL_LOOKBACK_SPAN_TRADING_DAYS
            ),
            "lookback_trading_days": LOOKBACK_TRADING_DAYS,
            "skip_recent_trading_days": SKIP_RECENT_TRADING_DAYS,
            "signal_day_count": len(signals),
            "signal_manifest_hashes": [
                signal.signal_manifest_hash for signal in signals
            ],
        },
    )
    _write_json(
        artifact_dir / "label_manifest.json",
        {
            **common_status,
            "label": "forward_21d_t1_to_t22_open_pit_adjusted_return",
            "entry": "T+1 open",
            "exit": "T+22 open",
            "label_derivation_asof_policy": (
                "each label uses its own exit date T+22 at 15:00 "
                "Asia/Shanghai inside calculate_future_return_labels"
            ),
            "evaluation_asof_ts": evaluation_asof.isoformat(),
            "label_count": len(labels),
            "label_status_counts": dict(sorted(label_status_counts.items())),
            "immature_label_count_in_ic": ic_result.immature_label_count,
            "missing_label_count_in_ic": ic_result.missing_label_count,
        },
    )
    _write_json(
        artifact_dir / "corporate_action_manifest.json",
        {
            **common_status,
            "snapshot_id": snapshot.snapshot_id,
            "source_id": "GOLDEN_SLICE_VERIFIED",
            "record_count": 76,
            "scheme_x_resolver": (
                "src.data.corporate_action_availability."
                "resolve_ca_available_at"
            ),
            "scheme_x_path_assertion": (
                "76/76 disclosure_time_known=False; no input available_at "
                "column; derived by real TradingCalendar"
            ),
            "availability_records": availability_records,
            "combined_event_checks": hengrui_checks,
            "naming_debt": [
                (
                    "Three 600276 cash+stock events use action_type="
                    "STOCK_DIVIDEND while retaining nonzero cash and share "
                    "inputs; the single action_type cannot express both legs."
                ),
                (
                    "cash_dividend_per_share carries the ex-right cash "
                    "deduction for adjustment consumers. The actual cash "
                    "entitlement is preserved in "
                    "cash_dividend_actual_per_share. This ADJUSTMENT_ONLY "
                    "snapshot must not be used by 4b execution."
                ),
            ],
            "records": manifest["verified_corporate_actions"],
        },
    )
    diagnostics = _diagnostics_payload(
        common_status=common_status,
        snapshot=snapshot,
        status_diagnostics=status_diagnostics,
        signals=signals,
        labels=labels,
        label_status_counts=label_status_counts,
        average_scorable=average_scorable,
        ic_result=ic_result,
        ca_diff=ca_diff,
        hengrui_reference_check=hengrui_reference_check,
        availability_samples=availability_samples,
        portal_equivalence_check=portal_equivalence_check,
        standard_portal_interrupted_run=standard_portal_interrupted_run,
        cached_pipeline_elapsed_seconds=cached_pipeline_elapsed_seconds,
    )
    _write_json(artifact_dir / "diagnostics.json", diagnostics)
    (artifact_dir / "report.md").write_text(
        _report_markdown(
            experiment_id=experiment_id,
            manifest_hash=manifest_hash,
            snapshot=snapshot,
            status_diagnostics=status_diagnostics,
            signals=signals,
            labels=labels,
            average_scorable=average_scorable,
            ic_result=ic_result,
            ca_diff=ca_diff,
            hengrui_reference_check=hengrui_reference_check,
            availability_samples=availability_samples,
            portal_equivalence_check=portal_equivalence_check,
            standard_portal_interrupted_run=standard_portal_interrupted_run,
            cached_pipeline_elapsed_seconds=cached_pipeline_elapsed_seconds,
        ),
        encoding="utf-8",
    )
    _progress(
        progress_path,
        "pipeline complete; "
        f"cached_elapsed={cached_pipeline_elapsed_seconds}s; artifacts written",
    )

    return PipelineRunResult(
        experiment_id=experiment_id,
        artifact_dir=artifact_dir,
        snapshot=snapshot,
        gate_manifest_hash=manifest_hash,
        portal_equivalence_check=portal_equivalence_check,
        standard_portal_interrupted_run=standard_portal_interrupted_run,
        cached_pipeline_elapsed_seconds=cached_pipeline_elapsed_seconds,
        availability_samples=availability_samples,
        hengrui_reference_check=hengrui_reference_check,
        return_status_counts=tuple(
            sorted(status_diagnostics["status_counts"].items())
        ),
        blocked_reason_counts=tuple(
            sorted(status_diagnostics["blocked_reason_counts"].items())
        ),
        signal_day_count=len(signals),
        average_scorable_security_count=average_scorable,
        label_status_counts=tuple(sorted(label_status_counts.items())),
        ic_result=ic_result,
        akshare_only_count=len(ca_diff["akshare_only"]),
        verified_only_count=len(ca_diff["verified_only"]),
    )


def _load_frozen_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise DataContractError(f"golden slice manifest does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_trading_calendar(path: Path) -> TradingCalendar:
    frame = pd.read_parquet(path)
    if "trade_date" not in frame.columns:
        raise DataContractError("trading calendar parquet missing trade_date")
    return trading_calendar_from_dates(frame["trade_date"])


def _assert_only_reusable_snapshot_exists(artifact_dir: Path) -> None:
    expected = {
        Path("snapshot/daily_bar_raw.parquet"),
        Path("snapshot/corporate_actions.parquet"),
    }
    observed = {
        path.relative_to(artifact_dir)
        for path in artifact_dir.rglob("*")
        if path.is_file()
    }
    if observed != expected:
        raise DataContractError(
            "existing artifact directory is not a reusable snapshot-only "
            f"partial run: observed={sorted(path.as_posix() for path in observed)}"
        )


def _assert_fast_slow_portal_equivalence(
    slow_portal: PITDataPortal,
    fast_portal: CachedPITDataPortal,
    calendar: TradingCalendar,
    progress_path: Path,
) -> dict[str, Any]:
    security_id = "600276"
    event_date = date(2020, 5, 25)
    start_date = calendar.previous_trading_day(event_date, 10)
    end_date = calendar.next_trading_day(event_date, 9)
    derivation_asof = _market_close_timestamp(end_date)
    slow = PITAdjustmentService(slow_portal, calendar).daily_adjusted_return_series(
        security_id,
        start_date,
        end_date,
        derivation_asof,
    )
    fast = PITAdjustmentService(fast_portal, calendar).daily_adjusted_return_series(
        security_id,
        start_date,
        end_date,
        derivation_asof,
    )
    slow_points = tuple(
        (
            point.trade_date,
            point.status,
            point.reference_price,
            point.adjusted_return,
            point.raw_close,
            point.ca_on_date,
            point.block_reason,
        )
        for point in slow.points
    )
    fast_points = tuple(
        (
            point.trade_date,
            point.status,
            point.reference_price,
            point.adjusted_return,
            point.raw_close,
            point.ca_on_date,
            point.block_reason,
        )
        for point in fast.points
    )
    if slow_points != fast_points:
        first_difference = next(
            (
                {
                    "index": index,
                    "slow": str(slow_point),
                    "fast": str(fast_point),
                }
                for index, (slow_point, fast_point) in enumerate(
                    zip(slow_points, fast_points)
                )
                if slow_point != fast_point
            ),
            {
                "slow_point_count": len(slow_points),
                "fast_point_count": len(fast_points),
            },
        )
        raise DataContractError(
            "standard/cached Portal adjustment sample mismatch: "
            f"{first_difference}"
        )
    if slow != fast:
        raise DataContractError(
            "standard/cached Portal sample metadata mismatch despite equal points"
        )
    result = {
        "status": "PASS",
        "security_id": security_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "trading_day_count": len(slow.points),
        "includes_ca_ex_date": event_date.isoformat(),
        "compared_point_fields": [
            "trade_date",
            "status",
            "reference_price",
            "adjusted_return",
            "raw_close",
            "ca_on_date",
            "block_reason",
        ],
        "full_series_metadata_equal": True,
    }
    _progress(
        progress_path,
        "standard/cached Portal 20-day equivalence PASS: "
        f"{security_id} {start_date}..{end_date}",
    )
    return result


def _assert_scheme_x_path(
    snapshot: GoldenSliceSnapshot,
    portal: PITDataPortal,
    calendar: TradingCalendar,
) -> tuple[list[dict[str, str]], tuple[dict[str, str], ...]]:
    raw_actions = pd.read_parquet(snapshot.corporate_action_path)
    if "available_at" in raw_actions.columns:
        raise DataContractError(
            "scheme X assertion failed: input CA snapshot contains available_at"
        )
    if len(raw_actions) != 76:
        raise DataContractError("scheme X assertion requires exactly 76 CA rows")
    if not raw_actions["disclosure_time_known"].eq(False).all():  # noqa: E712
        raise DataContractError(
            "scheme X assertion failed: not all CA rows declare unknown time"
        )

    availability_records: list[dict[str, str]] = []
    for row in raw_actions.to_dict(orient="records"):
        resolved = resolve_ca_available_at(row, calendar)
        expected_day = calendar.next_trading_day(row["disclosure_date"])
        if resolved.date() != expected_day or (
            resolved.hour,
            resolved.minute,
            resolved.second,
        ) != (9, 30, 0):
            raise DataContractError(
                "scheme X did not resolve to next trading day at 09:30: "
                f"{row['security_id']}/{row['ex_date']}"
            )
        availability_records.append(
            {
                "security_id": str(row["security_id"]).zfill(6),
                "ex_date": pd.Timestamp(row["ex_date"]).date().isoformat(),
                "disclosure_date": pd.Timestamp(
                    row["disclosure_date"]
                ).date().isoformat(),
                "derived_available_at": resolved.isoformat(),
                "source_pdf_filename": str(row["source_pdf_filename"]),
            }
        )

    visible = portal.query(
        "corporate_actions",
        _market_close_timestamp(SNAPSHOT_END_DATE),
    )
    if len(visible) != 76 or "available_at" not in visible.columns:
        raise DataContractError(
            "scheme X portal materialization did not return 76 visible CA rows"
        )
    portal_values = {
        (
            str(row.security_id).zfill(6),
            pd.Timestamp(row.ex_date).date().isoformat(),
        ): pd.Timestamp(row.available_at).isoformat()
        for row in visible.itertuples(index=False)
    }
    for record in availability_records:
        key = (record["security_id"], record["ex_date"])
        if portal_values.get(key) != record["derived_available_at"]:
            raise DataContractError(
                f"scheme X direct/portal availability mismatch: {key}"
            )

    sample_keys = (
        ("000333", "2019-05-30"),
        ("600276", "2020-05-25"),
        ("601939", "2023-07-14"),
    )
    by_key = {
        (record["security_id"], record["ex_date"]): record
        for record in availability_records
    }
    samples = tuple(by_key[key] for key in sample_keys)
    return availability_records, samples


def _assert_hengrui_combined_inputs(
    snapshot: GoldenSliceSnapshot,
    service: PITAdjustmentService,
    calendar: TradingCalendar,
) -> list[dict[str, str]]:
    actions = pd.read_parquet(snapshot.corporate_action_path)
    hengrui = actions.loc[
        actions["security_id"].astype(str).str.zfill(6).eq("600276")
        & actions["share_ratio"].astype(float).gt(0)
    ].copy()
    if len(hengrui) != 3:
        raise DataContractError(
            f"expected three 600276 combined events, observed={len(hengrui)}"
        )
    bars = pd.read_parquet(snapshot.daily_bar_path)
    bars["_trade_date"] = pd.to_datetime(bars["trade_date"]).dt.date
    bars = bars.loc[
        bars["security_id"].astype(str).str.zfill(6).eq("600276")
    ].copy()

    checks: list[dict[str, str]] = []
    for action in hengrui.itertuples(index=False):
        ex_date = pd.Timestamp(action.ex_date).date()
        previous_date = calendar.previous_trading_day(ex_date)
        previous_rows = bars.loc[bars["_trade_date"].eq(previous_date)]
        if previous_rows.empty or pd.isna(previous_rows.iloc[-1]["close"]):
            raise DataContractError(
                f"600276 combined-event check missing previous close: {ex_date}"
            )
        previous_close = Decimal(str(previous_rows.iloc[-1]["close"]))
        cash = Decimal(str(action.cash_dividend_per_share))
        share_ratio = Decimal(str(action.share_ratio))
        if (
            str(action.action_type) != "STOCK_DIVIDEND"
            or cash <= 0
            or share_ratio <= 0
        ):
            raise DataContractError(
                f"600276 combined-event inputs are incomplete: {ex_date}"
            )

        series = service.daily_adjusted_return_series(
            "600276",
            ex_date,
            ex_date,
            _market_close_timestamp(ex_date),
        )
        if len(series.points) != 1:
            raise DataContractError(
                f"600276 combined-event service point missing: {ex_date}"
            )
        point = series.points[0]
        if (
            point.status != AdjustedReturnStatus.OK
            or point.reference_price is None
        ):
            raise DataContractError(
                "600276 combined-event reference price unavailable: "
                f"{ex_date}/{point.status}/{point.block_reason}"
            )
        expected = (previous_close - cash) / (
            Decimal("1") + share_ratio
        )
        cash_only = previous_close - cash
        share_only = previous_close / (Decimal("1") + share_ratio)
        if point.reference_price != expected:
            raise DataContractError(
                "PITAdjustmentService did not consume both cash and stock "
                f"inputs for 600276/{ex_date}: "
                f"expected={expected}, actual={point.reference_price}"
            )
        if point.reference_price in (cash_only, share_only):
            raise DataContractError(
                "600276 combined-event result collapsed to a single input leg"
            )
        checks.append(
            {
                "security_id": "600276",
                "ex_date": ex_date.isoformat(),
                "previous_trading_day": previous_date.isoformat(),
                "previous_close": str(previous_close),
                "cash_deduction": str(cash),
                "share_ratio": str(share_ratio),
                "denominator": str(Decimal("1") + share_ratio),
                "hand_calculated_reference_price": str(expected),
                "service_reference_price": str(point.reference_price),
                "action_type": str(action.action_type),
            }
        )
    return checks


def _return_status_diagnostics(
    service: PITAdjustmentService,
    snapshot: GoldenSliceSnapshot,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    no_data_reasons: Counter[str] = Counter()
    per_security: dict[str, dict[str, int]] = {}
    derivation_asof = _market_close_timestamp(SNAPSHOT_END_DATE)

    for security_id in GOLDEN_SLICE_SECURITY_IDS:
        series = service.daily_adjusted_return_series(
            security_id,
            SNAPSHOT_START_DATE,
            SNAPSHOT_END_DATE,
            derivation_asof,
        )
        local_counts = Counter(point.status.value for point in series.points)
        per_security[security_id] = dict(sorted(local_counts.items()))
        for point in series.points:
            status_counts[point.status.value] += 1
            if point.status == AdjustedReturnStatus.BLOCKED:
                blocked_reasons[point.block_reason or "BLOCKED_WITHOUT_REASON"] += 1
            elif point.status == AdjustedReturnStatus.NO_DATA:
                no_data_reasons[point.block_reason or "NO_DATA_WITHOUT_REASON"] += 1

    point_count = sum(status_counts.values())
    return {
        "window": {
            "start": SNAPSHOT_START_DATE.isoformat(),
            "end": SNAPSHOT_END_DATE.isoformat(),
        },
        "point_count": point_count,
        "status_counts": dict(sorted(status_counts.items())),
        "blocked_count": status_counts[AdjustedReturnStatus.BLOCKED.value],
        "blocked_rate": str(
            Decimal(status_counts[AdjustedReturnStatus.BLOCKED.value])
            / Decimal(point_count)
        ),
        "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
        "no_data_reason_counts": dict(sorted(no_data_reasons.items())),
        "per_security_status_counts": per_security,
    }


def _cross_check_akshare_l2(
    verified_actions: list[dict[str, Any]],
    l2_path: Path,
) -> dict[str, Any]:
    l2 = pd.read_parquet(l2_path)
    required = {
        "security_id",
        "ex_date",
        "action_type",
        "cash_dividend_per_share",
        "share_ratio",
    }
    missing = required - set(l2.columns)
    if missing:
        raise DataContractError(
            f"akshare L2 cross-check missing columns: {sorted(missing)}"
        )
    ids = set(GOLDEN_SLICE_SECURITY_IDS)
    l2 = l2.copy()
    l2["_security_id"] = l2["security_id"].astype(str).str.zfill(6)
    l2["_ex_date"] = pd.to_datetime(l2["ex_date"], errors="raise").dt.date
    l2 = l2.loc[
        l2["_security_id"].isin(ids)
        & l2["_ex_date"].between(date(2019, 2, 25), date(2023, 12, 20))
    ].copy()

    verified_by_key = {
        (str(action["security_id"]).zfill(6), date.fromisoformat(action["ex_date"])): action
        for action in verified_actions
    }
    l2_keys = {
        (str(row["_security_id"]), row["_ex_date"])
        for _, row in l2.iterrows()
    }
    verified_keys = set(verified_by_key)
    akshare_only_keys = sorted(l2_keys - verified_keys)
    verified_only_keys = sorted(verified_keys - l2_keys)

    akshare_only: list[dict[str, Any]] = []
    for security_id, ex_date in akshare_only_keys:
        rows = l2.loc[
            l2["_security_id"].eq(security_id)
            & l2["_ex_date"].eq(ex_date)
        ]
        for row in rows.to_dict(orient="records"):
            akshare_only.append(
                {
                    "security_id": security_id,
                    "ex_date": ex_date.isoformat(),
                    "action_type": _json_scalar(row.get("action_type")),
                    "cash_dividend_per_share": _json_scalar(
                        row.get("cash_dividend_per_share")
                    ),
                    "share_ratio": _json_scalar(row.get("share_ratio")),
                    "source_id": _json_scalar(row.get("source_id")),
                }
            )
    verified_only = [
        {
            "security_id": security_id,
            "ex_date": ex_date.isoformat(),
            "action_type": (
                "STOCK_DIVIDEND"
                if float(verified_by_key[(security_id, ex_date)]["share_ratio"]) > 0
                else "CASH_DIVIDEND"
            ),
            "cash_dividend_per_share": verified_by_key[
                (security_id, ex_date)
            ]["cash_dividend_per_share"],
            "ex_right_cash_deduction_per_share": verified_by_key[
                (security_id, ex_date)
            ]["ex_right_cash_deduction_per_share"],
            "share_ratio": verified_by_key[(security_id, ex_date)][
                "share_ratio"
            ],
            "source_pdf_filename": verified_by_key[(security_id, ex_date)][
                "source_pdf_filename"
            ],
        }
        for security_id, ex_date in verified_only_keys
    ]
    return {
        "comparison_key": ["security_id", "ex_date"],
        "range": {"start": "2019-02-25", "end": "2023-12-20"},
        "akshare_key_count": len(l2_keys),
        "verified_key_count": len(verified_keys),
        "akshare_only": akshare_only,
        "verified_only": verified_only,
    }


def _prediction_frame(
    signals: list[CrossSectionalMomentumSignal],
    labels: list[FutureReturnLabel],
    ic_result: MomentumICEvaluationResult,
) -> pd.DataFrame:
    labels_by_key = {
        (label.signal_asof_ts, label.security_id): label for label in labels
    }
    ic_by_asof = {
        point.asof_ts: point.rank_ic for point in ic_result.rank_ic_series
    }
    rows: list[dict[str, Any]] = []
    for signal in signals:
        exclusions = {
            item.security_id: item.reason
            for item in signal.excluded_securities
        }
        for point in signal.points:
            label = labels_by_key.get((signal.asof_ts, point.security_id))
            rows.append(
                {
                    "signal_asof_ts": signal.asof_ts,
                    "score_asof_ts": signal.score_asof_ts,
                    "security_id": point.security_id,
                    "signal_status": point.status.value,
                    "signal_exclusion_reason": exclusions.get(point.security_id),
                    "momentum_score": _float_or_none(point.momentum_score),
                    "cross_sectional_rank": point.cross_sectional_rank,
                    "label_status": label.status.value if label else None,
                    "future_return": (
                        _float_or_none(label.future_return) if label else None
                    ),
                    "label_end_ts": label.label_end_ts if label else None,
                    "label_observed_at": (
                        label.label_observed_at if label else None
                    ),
                    "rank_ic": _float_or_none(
                        ic_by_asof.get(signal.asof_ts)
                    ),
                    "evidence_status": EVIDENCE_STATUS,
                    "audit_status": AUDIT_STATUS,
                    "validation_scope": VALIDATION_SCOPE,
                }
            )
    return pd.DataFrame(rows)


def _diagnostics_payload(
    *,
    common_status: dict[str, str],
    snapshot: GoldenSliceSnapshot,
    status_diagnostics: dict[str, Any],
    signals: list[CrossSectionalMomentumSignal],
    labels: list[FutureReturnLabel],
    label_status_counts: Counter[str],
    average_scorable: Decimal,
    ic_result: MomentumICEvaluationResult,
    ca_diff: dict[str, Any],
    hengrui_reference_check: dict[str, str],
    availability_samples: tuple[dict[str, str], ...],
    portal_equivalence_check: dict[str, Any],
    standard_portal_interrupted_run: dict[str, str] | None,
    cached_pipeline_elapsed_seconds: Decimal,
) -> dict[str, Any]:
    coverage_values = [point.coverage for point in ic_result.coverage_series]
    valid_ic_count = sum(
        point.rank_ic is not None for point in ic_result.rank_ic_series
    )
    return {
        **common_status,
        "snapshot": {
            "snapshot_id": snapshot.snapshot_id,
            "l1_row_count": snapshot.l1_row_count,
            "l1_rows_by_security": dict(snapshot.l1_rows_by_security),
            "ca_row_count": snapshot.ca_row_count,
            "source_snapshot_ids_preserved": list(snapshot.source_snapshot_ids),
        },
        "scheme_x_samples": list(availability_samples),
        "portal_runtime": {
            "full_pipeline_portal": "CachedPITDataPortal",
            "fast_slow_sample_equivalence": portal_equivalence_check,
            "standard_portal_interrupted_run": standard_portal_interrupted_run,
            "cached_pipeline_elapsed_seconds": str(
                cached_pipeline_elapsed_seconds
            ),
            "timing_comparison_caveat": (
                "The standard run was interrupted before completion, while "
                "cached elapsed is wall time and includes any host suspension; "
                "the two values do not define a benchmark speedup ratio."
            ),
        },
        "hengrui_reference_price_check": hengrui_reference_check,
        "daily_adjusted_return_status": status_diagnostics,
        "signal": {
            "signal_day_count": len(signals),
            "average_scorable_security_count": str(average_scorable),
            "minimum_scorable_security_count": min(
                signal.universe_size_after_exclusion for signal in signals
            ),
            "maximum_scorable_security_count": max(
                signal.universe_size_after_exclusion for signal in signals
            ),
        },
        "labels": {
            "label_count": len(labels),
            "status_counts": dict(sorted(label_status_counts.items())),
            "immature_label_count_in_ic": ic_result.immature_label_count,
            "missing_label_count_in_ic": ic_result.missing_label_count,
        },
        "rank_ic": {
            "valid_day_count": valid_ic_count,
            "rank_ic_mean": _json_scalar(ic_result.rank_ic_mean),
            "icir": _json_scalar(ic_result.icir),
            "ci_method": ic_result.ci_method,
            "ci_bounds": [_json_scalar(value) for value in ic_result.ci_bounds],
            "ci_available": all(
                value is not None for value in ic_result.ci_bounds
            ),
            "quantile_returns": [
                {
                    **asdict(item),
                    "mean_future_return": _json_scalar(
                        item.mean_future_return
                    ),
                }
                for item in ic_result.quantile_returns
            ],
            "quantile_monotonicity": ic_result.quantile_monotonicity,
        },
        "coverage": {
            "mean": _json_scalar(_mean_decimal(coverage_values)),
            "minimum": _json_scalar(min(coverage_values)),
            "maximum": _json_scalar(max(coverage_values)),
            "zero_coverage_days": sum(
                value == Decimal("0") for value in coverage_values
            ),
        },
        "akshare_l2_cross_check": ca_diff,
        "statistical_limitations": [
            "Only 12 securities are present in the cross-section.",
            "Daily RankIC has a very small cross-sectional sample and high noise.",
            (
                f"The non-IID block bootstrap uses block={CI_BLOCK_LENGTH}; "
                "its interval remains fragile at this universe size."
            ),
        ],
    }


def _report_markdown(
    *,
    experiment_id: str,
    manifest_hash: str,
    snapshot: GoldenSliceSnapshot,
    status_diagnostics: dict[str, Any],
    signals: list[CrossSectionalMomentumSignal],
    labels: list[FutureReturnLabel],
    average_scorable: Decimal,
    ic_result: MomentumICEvaluationResult,
    ca_diff: dict[str, Any],
    hengrui_reference_check: dict[str, str],
    availability_samples: tuple[dict[str, str], ...],
    portal_equivalence_check: dict[str, Any],
    standard_portal_interrupted_run: dict[str, str] | None,
    cached_pipeline_elapsed_seconds: Decimal,
) -> str:
    coverage_values = [point.coverage for point in ic_result.coverage_series]
    label_counts = Counter(label.status.value for label in labels)
    lines = [
        "# Golden Slice Pipeline Report",
        "",
        "> This result is limited to honest pipeline behavior inside the frozen "
        "golden slice. It does not establish strategy effectiveness in the full market.",
        "> BACKTEST_VALIDATED status may be granted only by a later audit of all "
        "ten conditions in BACKTEST_DESIGN section 12.3. This run does not self-certify.",
        "",
        "## Status",
        "",
        f"- experiment_id: `{experiment_id}`",
        f"- evidence_status: `{EVIDENCE_STATUS}`",
        f"- audit_status: `{AUDIT_STATUS}`",
        f"- validation_scope: `{VALIDATION_SCOPE}`",
        f"- validation_scope_manifest_hash: `{manifest_hash}`",
        "",
        "## Statistical Limitations",
        "",
        "**The cross-section contains only 12 securities. Daily RankIC therefore "
        "has a very small cross-sectional sample and high noise. RankIC is not "
        "evidence of strategy effectiveness. A mean near zero or a confidence "
        "interval crossing zero does not invalidate the pipeline-honesty objective.**",
        "",
        f"The moving-block bootstrap uses block length {CI_BLOCK_LENGTH}. It "
        "addresses overlapping-label dependence but remains statistically fragile "
        "with this small universe and the observed coverage.",
        "",
        "## Governance And Snapshot",
        "",
        "- frozen manifest gate: PASS",
        f"- snapshot_id: `{snapshot.snapshot_id}`",
        f"- L1 rows: {snapshot.l1_row_count}",
        f"- CA rows: {snapshot.ca_row_count}",
        "- snapshot purpose: ADJUSTMENT_ONLY; prohibited for 4b execution",
        "- source_snapshot_id retained: YES",
        "- scheme X path: 76/76 used disclosure_time_known=False and real TradingCalendar",
        "- full pipeline portal: CachedPITDataPortal",
        "- standard/cached 20-trading-day equivalence: PASS",
        f"- equivalence sample: {portal_equivalence_check['security_id']} "
        f"{portal_equivalence_check['start_date']}.."
        f"{portal_equivalence_check['end_date']}",
        f"- cached pipeline elapsed seconds: {cached_pipeline_elapsed_seconds}",
        "- timing comparison caveat: the standard run did not complete, and "
        "cached elapsed is wall time including any host suspension; no speedup "
        "ratio is inferred",
        "",
        "Scheme X samples:",
        "",
        "| security_id | disclosure_date | ex_date | derived_available_at |",
        "|---|---|---|---|",
    ]
    for sample in availability_samples:
        lines.append(
            f"| {sample['security_id']} | {sample['disclosure_date']} | "
            f"{sample['ex_date']} | {sample['derived_available_at']} |"
        )
    if standard_portal_interrupted_run is not None:
        lines.extend(
            [
                "",
                "### Standard Portal Runtime Diagnostic",
                "",
                "The initial standard PITDataPortal run remained healthy but was "
                "operator-interrupted before result generation. Its snapshot was "
                "reused unchanged for the cached run.",
                "",
                "- accumulated CPU seconds: "
                f"{standard_portal_interrupted_run['cpu_seconds']}",
                "- wall-clock seconds: "
                f"{standard_portal_interrupted_run['wall_seconds']}",
                "- result artifacts produced before interruption: none beyond "
                "the two snapshot parquet files",
            ]
        )
    lines.extend(
        [
            "",
            "## Combined Cash And Stock-Dividend Check",
            "",
            "The three 600276 events use action_type=STOCK_DIVIDEND but retain "
            "both cash and share-ratio pricing inputs. This is a documented "
            "ADJUSTMENT_ONLY schema debt.",
            "",
            f"- ex_date: {hengrui_reference_check['ex_date']}",
            f"- previous close: {hengrui_reference_check['previous_close']}",
            f"- cash deduction: {hengrui_reference_check['cash_deduction']}",
            f"- denominator: {hengrui_reference_check['denominator']}",
            "- hand-calculated reference price: "
            f"{hengrui_reference_check['hand_calculated_reference_price']}",
            "- service reference price: "
            f"{hengrui_reference_check['service_reference_price']}",
            "",
            "## Negative Diagnostics",
            "",
            f"- total daily adjustment points: {status_diagnostics['point_count']}",
            f"- status counts: `{json.dumps(status_diagnostics['status_counts'], sort_keys=True)}`",
            f"- BLOCKED reasons: `{json.dumps(status_diagnostics['blocked_reason_counts'], sort_keys=True)}`",
            f"- NO_DATA reasons: `{json.dumps(status_diagnostics['no_data_reason_counts'], sort_keys=True)}`",
            f"- label status counts: `{json.dumps(dict(sorted(label_counts.items())), sort_keys=True)}`",
            f"- immature labels excluded by IC: {ic_result.immature_label_count}",
            f"- missing labels in IC: {ic_result.missing_label_count}",
            f"- mean coverage: {_json_scalar(_mean_decimal(coverage_values))}",
            f"- minimum coverage: {_json_scalar(min(coverage_values))}",
            f"- zero-coverage days: {sum(value == Decimal('0') for value in coverage_values)}",
            f"- CI available: {all(value is not None for value in ic_result.ci_bounds)}",
            "",
            "## Signal And RankIC",
            "",
            f"- signal days: {len(signals)}",
            f"- average scorable securities/day: {average_scorable}",
            f"- RankIC mean: {_json_scalar(ic_result.rank_ic_mean)}",
            f"- ICIR: {_json_scalar(ic_result.icir)}",
            f"- CI method: `{ic_result.ci_method}`",
            f"- CI bounds: `{[_json_scalar(value) for value in ic_result.ci_bounds]}`",
            f"- quantile monotonicity: `{ic_result.quantile_monotonicity}`",
            "",
            "| quantile | mean future return | sample count |",
            "|---:|---:|---:|",
        ]
    )
    for item in ic_result.quantile_returns:
        lines.append(
            f"| {item.quantile} | {_json_scalar(item.mean_future_return)} | "
            f"{item.sample_count} |"
        )
    lines.extend(
        [
            "",
            "## Akshare L2 Bidirectional Difference",
            "",
            "Comparison key is `(security_id, ex_date)` only. Amount, action_type, "
            "and share_ratio are reported but do not define presence.",
            "",
            f"- akshare only: {len(ca_diff['akshare_only'])}",
            f"- verified only: {len(ca_diff['verified_only'])}",
            "",
            "### Akshare only",
            "",
        ]
    )
    lines.extend(_markdown_diff_rows(ca_diff["akshare_only"]))
    lines.extend(["", "### Verified only", ""])
    lines.extend(_markdown_diff_rows(ca_diff["verified_only"]))
    return "\n".join(lines) + "\n"


def _markdown_diff_rows(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["None."]
    output = [
        "| security_id | ex_date | action_type | cash | share_ratio | source |",
        "|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        output.append(
            f"| {row.get('security_id', '')} | {row.get('ex_date', '')} | "
            f"{row.get('action_type', '')} | "
            f"{row.get('cash_dividend_per_share', '')} | "
            f"{row.get('share_ratio', '')} | "
            f"{row.get('source_id', row.get('source_pdf_filename', ''))} |"
        )
    return output


def _market_close_timestamp(day: date) -> pd.Timestamp:
    return pd.Timestamp(
        datetime.combine(day, time(15, 0), tzinfo=ASIA_SHANGHAI)
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=_json_default,
        )
        + "\n",
        encoding="utf-8",
    )


def _progress(path: Path, message: str) -> None:
    timestamp = datetime.now(ASIA_SHANGHAI).isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if pd.isna(value):
        return None
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _json_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, pd.Timestamp)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def _float_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _mean_decimal(values: Iterable[Decimal]) -> Decimal | None:
    materialized = list(values)
    if not materialized:
        return None
    return sum(materialized, Decimal("0")) / Decimal(len(materialized))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the frozen golden-slice adjustment and RankIC pipeline."
    )
    parser.add_argument(
        "--standard-portal-cpu-seconds",
        type=Decimal,
    )
    parser.add_argument(
        "--standard-portal-wall-seconds",
        type=Decimal,
    )
    args = parser.parse_args()
    result = run_golden_slice_pipeline(
        standard_portal_cpu_seconds=args.standard_portal_cpu_seconds,
        standard_portal_wall_seconds=args.standard_portal_wall_seconds,
    )
    print(
        json.dumps(
            {
                "experiment_id": result.experiment_id,
                "artifact_dir": result.artifact_dir.as_posix(),
                "snapshot_id": result.snapshot.snapshot_id,
                "l1_row_count": result.snapshot.l1_row_count,
                "ca_row_count": result.snapshot.ca_row_count,
                "portal_equivalence_check": result.portal_equivalence_check,
                "standard_portal_interrupted_run": (
                    result.standard_portal_interrupted_run
                ),
                "cached_pipeline_elapsed_seconds": str(
                    result.cached_pipeline_elapsed_seconds
                ),
                "signal_day_count": result.signal_day_count,
                "average_scorable_security_count": str(
                    result.average_scorable_security_count
                ),
                "rank_ic_mean": _json_scalar(
                    result.ic_result.rank_ic_mean
                ),
                "icir": _json_scalar(result.ic_result.icir),
                "ci_bounds": [
                    _json_scalar(value)
                    for value in result.ic_result.ci_bounds
                ],
                "akshare_only_count": result.akshare_only_count,
                "verified_only_count": result.verified_only_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
