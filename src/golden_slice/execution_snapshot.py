from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError, PriceBasis
from src.golden_slice.manifest import GOLDEN_SLICE_SECURITY_IDS
from src.golden_slice.snapshot import SNAPSHOT_END_DATE, SNAPSHOT_START_DATE


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
EXECUTION_SUFFIX = "EXECUTION"
GOLDEN_SLICE_CA_SOURCE_ID = "GOLDEN_SLICE_VERIFIED"
GOLDEN_SLICE_CA_EVIDENCE_LEVEL = "MANUALLY_VERIFIED_OFFICIAL_PDF"


@dataclass(frozen=True)
class GoldenSliceExecutionSnapshot:
    snapshot_id: str
    root_dir: Path
    daily_bar_path: Path
    corporate_action_path: Path
    security_master_path: Path
    l1_row_count: int
    l1_rows_by_security: tuple[tuple[str, int], ...]
    ca_row_count: int
    security_master_row_count: int
    source_snapshot_ids: tuple[str, ...]
    source_security_master_snapshot_ids: tuple[str, ...]
    daily_bar_sha256: str
    corporate_action_sha256: str
    security_master_sha256: str


def build_execution_snapshot(
    manifest: dict[str, Any],
    *,
    l1_path: Path,
    security_master_path: Path,
    output_dir: Path,
    snapshot_id: str,
) -> GoldenSliceExecutionSnapshot:
    _assert_execution_snapshot_id(snapshot_id)
    actions = manifest.get("verified_corporate_actions")
    if not isinstance(actions, list) or len(actions) != 76:
        raise DataContractError(
            "golden slice execution snapshot requires exactly 76 verified CA records"
        )

    bars, source_snapshot_ids = _build_daily_bars(l1_path, snapshot_id)
    ca_rows = _build_corporate_actions(actions, snapshot_id)
    master, source_master_snapshot_ids = _build_security_master(
        security_master_path,
        snapshot_id,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    daily_bar_path = output_dir / "daily_bar_raw.parquet"
    corporate_action_path = output_dir / "corporate_actions.parquet"
    output_master_path = output_dir / "security_master.parquet"
    bars.to_parquet(daily_bar_path, index=False)
    ca_rows.to_parquet(corporate_action_path, index=False)
    master.to_parquet(output_master_path, index=False)

    counts = Counter(bars["security_id"].tolist())
    return GoldenSliceExecutionSnapshot(
        snapshot_id=snapshot_id,
        root_dir=output_dir,
        daily_bar_path=daily_bar_path,
        corporate_action_path=corporate_action_path,
        security_master_path=output_master_path,
        l1_row_count=len(bars),
        l1_rows_by_security=tuple(sorted(counts.items())),
        ca_row_count=len(ca_rows),
        security_master_row_count=len(master),
        source_snapshot_ids=source_snapshot_ids,
        source_security_master_snapshot_ids=source_master_snapshot_ids,
        daily_bar_sha256=_sha256_file(daily_bar_path),
        corporate_action_sha256=_sha256_file(corporate_action_path),
        security_master_sha256=_sha256_file(output_master_path),
    )


def load_execution_snapshot(
    *,
    output_dir: Path,
    snapshot_id: str,
) -> GoldenSliceExecutionSnapshot:
    _assert_execution_snapshot_id(snapshot_id)
    daily_bar_path = output_dir / "daily_bar_raw.parquet"
    corporate_action_path = output_dir / "corporate_actions.parquet"
    security_master_path = output_dir / "security_master.parquet"
    for path in (daily_bar_path, corporate_action_path, security_master_path):
        if not path.exists():
            raise DataContractError(
                f"existing golden slice execution snapshot file is missing: {path}"
            )

    bars = pd.read_parquet(daily_bar_path)
    actions = pd.read_parquet(corporate_action_path)
    master = pd.read_parquet(security_master_path)
    _require_columns(
        bars,
        [
            "security_id",
            "trade_date",
            "price_basis",
            "snapshot_id",
            "source_snapshot_id",
        ],
        "existing daily_bar_raw",
    )
    _require_columns(
        actions,
        [
            "security_id",
            "ex_date",
            "cash_dividend_per_share",
            "ex_right_cash_deduction_per_share",
            "share_ratio",
            "disclosure_time_known",
            "snapshot_id",
        ],
        "existing corporate_actions",
    )
    _require_columns(
        master,
        [
            "security_id",
            "board",
            "list_date",
            "available_at",
            "snapshot_id",
            "source_snapshot_id",
        ],
        "existing security_master",
    )
    if len(actions) != 76:
        raise DataContractError(
            f"existing execution snapshot requires 76 CA rows; observed={len(actions)}"
        )
    if len(master) != len(GOLDEN_SLICE_SECURITY_IDS):
        raise DataContractError(
            "existing execution snapshot requires one security_master row per security"
        )
    if "available_at" in actions.columns:
        raise DataContractError(
            "execution CA snapshot must derive available_at through scheme X"
        )
    if not actions["disclosure_time_known"].eq(False).all():  # noqa: E712
        raise DataContractError(
            "execution CA snapshot must use disclosure_time_known=False"
        )
    for label, frame in (
        ("daily_bar_raw", bars),
        ("corporate_actions", actions),
        ("security_master", master),
    ):
        observed = set(frame["snapshot_id"].astype(str).unique().tolist())
        if observed != {snapshot_id}:
            raise DataContractError(
                f"existing {label} snapshot_id mismatch: {sorted(observed)}"
            )
    _assert_raw_unadjusted(bars)
    _assert_cash_field_split(actions)

    counts = Counter(bars["security_id"].astype(str).str.zfill(6).tolist())
    return GoldenSliceExecutionSnapshot(
        snapshot_id=snapshot_id,
        root_dir=output_dir,
        daily_bar_path=daily_bar_path,
        corporate_action_path=corporate_action_path,
        security_master_path=security_master_path,
        l1_row_count=len(bars),
        l1_rows_by_security=tuple(sorted(counts.items())),
        ca_row_count=len(actions),
        security_master_row_count=len(master),
        source_snapshot_ids=tuple(
            sorted(bars["source_snapshot_id"].astype(str).unique().tolist())
        ),
        source_security_master_snapshot_ids=tuple(
            sorted(master["source_snapshot_id"].astype(str).unique().tolist())
        ),
        daily_bar_sha256=_sha256_file(daily_bar_path),
        corporate_action_sha256=_sha256_file(corporate_action_path),
        security_master_sha256=_sha256_file(security_master_path),
    )


def cash_field_split_diagnostics(actions: pd.DataFrame) -> dict[str, Any]:
    _require_columns(
        actions,
        [
            "security_id",
            "ex_date",
            "cash_dividend_per_share",
            "ex_right_cash_deduction_per_share",
        ],
        "corporate_actions",
    )
    actual = actions["cash_dividend_per_share"].map(lambda value: Decimal(str(value)))
    deduction = actions["ex_right_cash_deduction_per_share"].map(
        lambda value: Decimal(str(value))
    )
    different = actions.loc[actual.ne(deduction)].copy()
    same = actions.loc[actual.eq(deduction)].copy()
    differences = actual - deduction
    maximum_index = differences.abs().idxmax()
    maximum = actions.loc[maximum_index]
    return {
        "different_count": len(different),
        "same_count": len(same),
        "maximum_difference": {
            "security_id": str(maximum["security_id"]).zfill(6),
            "ex_date": pd.Timestamp(maximum["ex_date"]).date().isoformat(),
            "cash_dividend_per_share": str(
                Decimal(str(maximum["cash_dividend_per_share"]))
            ),
            "ex_right_cash_deduction_per_share": str(
                Decimal(str(maximum["ex_right_cash_deduction_per_share"]))
            ),
            "absolute_difference": str(abs(differences.loc[maximum_index])),
        },
    }


def _build_daily_bars(
    path: Path,
    snapshot_id: str,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    bars = pd.read_parquet(path)
    _require_columns(
        bars,
        [
            "security_id",
            "trade_date",
            "available_at",
            "price_basis",
            "snapshot_id",
        ],
        "daily_bar_raw",
    )
    bars = bars.copy()
    bars["_security_id"] = bars["security_id"].astype(str).str.zfill(6)
    bars["_trade_date"] = pd.to_datetime(
        bars["trade_date"],
        errors="raise",
    ).dt.date
    requested_ids = set(GOLDEN_SLICE_SECURITY_IDS)
    bars = bars.loc[
        bars["_security_id"].isin(requested_ids)
        & bars["_trade_date"].between(SNAPSHOT_START_DATE, SNAPSHOT_END_DATE)
    ].copy()
    if set(bars["_security_id"].unique()) != requested_ids:
        raise DataContractError("execution L1 snapshot is missing requested securities")
    _assert_raw_unadjusted(bars)
    if bars.duplicated(["_security_id", "_trade_date"], keep=False).any():
        raise DataContractError(
            "execution L1 snapshot has duplicate security/trade_date rows"
        )
    bars["security_id"] = bars["_security_id"]
    bars["source_snapshot_id"] = bars["snapshot_id"].astype(str)
    source_snapshot_ids = tuple(
        sorted(bars["source_snapshot_id"].dropna().unique().tolist())
    )
    bars["snapshot_id"] = snapshot_id
    bars = (
        bars.drop(columns=["_security_id", "_trade_date"])
        .sort_values(["security_id", "trade_date"])
        .reset_index(drop=True)
    )
    return bars, source_snapshot_ids


def _build_corporate_actions(
    actions: list[dict[str, Any]],
    snapshot_id: str,
) -> pd.DataFrame:
    rows = pd.DataFrame(
        [_execution_ca_row(action, snapshot_id) for action in actions]
    ).sort_values(["security_id", "ex_date"]).reset_index(drop=True)
    if "available_at" in rows.columns:
        raise DataContractError(
            "execution CA snapshot must not materialize available_at"
        )
    if rows.duplicated(["security_id", "ex_date"], keep=False).any():
        raise DataContractError(
            "execution CA snapshot has duplicate security/ex_date rows"
        )
    _assert_cash_field_split(rows)
    return rows


def _build_security_master(
    path: Path,
    snapshot_id: str,
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    master = pd.read_parquet(path)
    _require_columns(
        master,
        [
            "security_id",
            "board",
            "list_date",
            "available_at",
            "snapshot_id",
        ],
        "security_master",
    )
    master = master.copy()
    master["_security_id"] = master["security_id"].astype(str).str.zfill(6)
    requested_ids = set(GOLDEN_SLICE_SECURITY_IDS)
    master = master.loc[master["_security_id"].isin(requested_ids)].copy()
    if set(master["_security_id"].unique()) != requested_ids:
        raise DataContractError(
            "execution security_master snapshot is missing requested securities"
        )
    if master.duplicated(["_security_id"], keep=False).any():
        raise DataContractError(
            "execution security_master snapshot has duplicate security_id rows"
        )
    if master["board"].isna().any() or master["list_date"].isna().any():
        raise DataContractError(
            "execution security_master requires board and list_date"
        )
    master["security_id"] = master["_security_id"]
    master["source_snapshot_id"] = master["snapshot_id"].astype(str)
    source_snapshot_ids = tuple(
        sorted(master["source_snapshot_id"].dropna().unique().tolist())
    )
    master["snapshot_id"] = snapshot_id
    master = (
        master.drop(columns=["_security_id"])
        .sort_values("security_id")
        .reset_index(drop=True)
    )
    return master, source_snapshot_ids


def _execution_ca_row(action: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    share_ratio = float(action["share_ratio"])
    action_type = "STOCK_DIVIDEND" if share_ratio > 0 else "CASH_DIVIDEND"
    ex_date = pd.Timestamp(action["ex_date"]).date()
    # A combined cash+stock event is represented as STOCK_DIVIDEND while both
    # legs remain populated. The execution handler consumes both numeric fields.
    return {
        "security_id": str(action["security_id"]).zfill(6),
        "ex_date": action["ex_date"],
        "record_date": action["record_date"],
        "event_ts": _market_close_timestamp(ex_date).isoformat(),
        "action_type": action_type,
        "cash_dividend_per_share": action["cash_dividend_per_share"],
        "ex_right_cash_deduction_per_share": action[
            "ex_right_cash_deduction_per_share"
        ],
        "share_ratio": share_ratio,
        "rights_ratio": 0.0,
        "rights_price": 0.0,
        "disclosure_date": action["disclosure_date"],
        "disclosure_time_known": False,
        "disclosure_ts": None,
        "snapshot_id": snapshot_id,
        "source_id": GOLDEN_SLICE_CA_SOURCE_ID,
        "evidence_level": GOLDEN_SLICE_CA_EVIDENCE_LEVEL,
        "source_pdf_filename": action["source_pdf_filename"],
        "source_pdf_sha256": action["source_pdf_sha256"],
    }


def _assert_cash_field_split(actions: pd.DataFrame) -> None:
    diagnostics = cash_field_split_diagnostics(actions)
    maximum = diagnostics["maximum_difference"]
    if diagnostics["different_count"] != 22 or diagnostics["same_count"] != 54:
        raise DataContractError(
            "execution CA cash-field split mismatch: "
            f"different={diagnostics['different_count']}, "
            f"same={diagnostics['same_count']}"
        )
    expected_maximum = {
        "security_id": "000651",
        "ex_date": "2021-08-23",
        "cash_dividend_per_share": "3.0",
        "ex_right_cash_deduction_per_share": "2.784787",
        "absolute_difference": "0.215213",
    }
    if maximum != expected_maximum:
        raise DataContractError(
            "execution CA maximum cash-field difference mismatch: "
            f"{maximum}"
        )


def _assert_raw_unadjusted(bars: pd.DataFrame) -> None:
    invalid = bars.loc[
        bars["price_basis"].isna()
        | bars["price_basis"].astype(str).ne(PriceBasis.RAW_UNADJUSTED.value)
    ]
    if not invalid.empty:
        raise DataContractError(
            "execution L1 snapshot requires RAW_UNADJUSTED rows"
        )


def _assert_execution_snapshot_id(snapshot_id: str) -> None:
    if not snapshot_id.startswith("golden_slice_"):
        raise DataContractError(
            "golden slice snapshot_id must start with golden_slice_"
        )
    if not snapshot_id.endswith(f"_{EXECUTION_SUFFIX}"):
        raise DataContractError(
            "golden slice execution snapshot_id must end with EXECUTION"
        )


def _market_close_timestamp(day: date) -> pd.Timestamp:
    return pd.Timestamp(
        datetime.combine(day, time(15, 0), tzinfo=ASIA_SHANGHAI)
    )


def _require_columns(
    frame: pd.DataFrame,
    columns: list[str],
    label: str,
) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise DataContractError(f"{label} missing required columns: {missing}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
