from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError, PriceBasis
from src.golden_slice.manifest import GOLDEN_SLICE_SECURITY_IDS


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_START_DATE = date(2018, 11, 1)
SNAPSHOT_END_DATE = date(2024, 2, 29)
ADJUSTMENT_ONLY_SUFFIX = "ADJUSTMENT_ONLY"
GOLDEN_SLICE_CA_SOURCE_ID = "GOLDEN_SLICE_VERIFIED"
GOLDEN_SLICE_CA_EVIDENCE_LEVEL = "MANUALLY_VERIFIED_OFFICIAL_PDF"


@dataclass(frozen=True)
class GoldenSliceSnapshot:
    snapshot_id: str
    root_dir: Path
    daily_bar_path: Path
    corporate_action_path: Path
    l1_row_count: int
    l1_rows_by_security: tuple[tuple[str, int], ...]
    ca_row_count: int
    source_snapshot_ids: tuple[str, ...]
    daily_bar_sha256: str
    corporate_action_sha256: str


def build_adjustment_only_snapshot(
    manifest: dict[str, Any],
    *,
    l1_path: Path,
    output_dir: Path,
    snapshot_id: str,
) -> GoldenSliceSnapshot:
    _assert_adjustment_only_snapshot_id(snapshot_id)
    actions = manifest.get("verified_corporate_actions")
    if not isinstance(actions, list) or len(actions) != 76:
        raise DataContractError("golden slice snapshot requires exactly 76 verified CA records")

    bars = pd.read_parquet(l1_path)
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
    bars["_trade_date"] = pd.to_datetime(bars["trade_date"], errors="raise").dt.date
    requested_ids = set(GOLDEN_SLICE_SECURITY_IDS)
    bars = bars.loc[
        bars["_security_id"].isin(requested_ids)
        & bars["_trade_date"].between(SNAPSHOT_START_DATE, SNAPSHOT_END_DATE)
    ].copy()
    if bars.empty:
        raise DataContractError("golden slice L1 snapshot selection is empty")
    observed_ids = set(bars["_security_id"].unique().tolist())
    if observed_ids != requested_ids:
        raise DataContractError(
            "golden slice L1 snapshot is missing securities: "
            f"{sorted(requested_ids - observed_ids)}"
        )
    invalid_basis = bars.loc[
        bars["price_basis"].isna()
        | bars["price_basis"].astype(str).ne(PriceBasis.RAW_UNADJUSTED.value)
    ]
    if not invalid_basis.empty:
        observed = sorted(
            str(value)
            for value in invalid_basis["price_basis"].dropna().unique().tolist()
        )
        raise DataContractError(
            "golden slice L1 snapshot requires RAW_UNADJUSTED rows; "
            f"observed={observed}"
        )
    duplicate_bars = bars.duplicated(["_security_id", "_trade_date"], keep=False)
    if duplicate_bars.any():
        raise DataContractError(
            "golden slice L1 snapshot has duplicate security/trade_date rows"
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
    counts = Counter(bars["security_id"].tolist())

    ca_rows = pd.DataFrame(
        [_snapshot_ca_row(action, snapshot_id) for action in actions]
    ).sort_values(["security_id", "ex_date"]).reset_index(drop=True)
    if "available_at" in ca_rows.columns:
        raise DataContractError(
            "ADJUSTMENT_ONLY CA snapshot must not materialize available_at"
        )
    if not ca_rows["disclosure_time_known"].eq(False).all():  # noqa: E712
        raise DataContractError(
            "all golden slice CA rows must use disclosure_time_known=False"
        )
    duplicate_actions = ca_rows.duplicated(["security_id", "ex_date"], keep=False)
    if duplicate_actions.any():
        raise DataContractError(
            "golden slice CA snapshot has duplicate security/ex_date rows"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    daily_bar_path = output_dir / "daily_bar_raw.parquet"
    corporate_action_path = output_dir / "corporate_actions.parquet"
    bars.to_parquet(daily_bar_path, index=False)
    ca_rows.to_parquet(corporate_action_path, index=False)

    return GoldenSliceSnapshot(
        snapshot_id=snapshot_id,
        root_dir=output_dir,
        daily_bar_path=daily_bar_path,
        corporate_action_path=corporate_action_path,
        l1_row_count=len(bars),
        l1_rows_by_security=tuple(sorted(counts.items())),
        ca_row_count=len(ca_rows),
        source_snapshot_ids=source_snapshot_ids,
        daily_bar_sha256=_sha256_file(daily_bar_path),
        corporate_action_sha256=_sha256_file(corporate_action_path),
    )


def load_adjustment_only_snapshot(
    *,
    output_dir: Path,
    snapshot_id: str,
) -> GoldenSliceSnapshot:
    _assert_adjustment_only_snapshot_id(snapshot_id)
    daily_bar_path = output_dir / "daily_bar_raw.parquet"
    corporate_action_path = output_dir / "corporate_actions.parquet"
    for path in (daily_bar_path, corporate_action_path):
        if not path.exists():
            raise DataContractError(
                f"existing golden slice snapshot file is missing: {path}"
            )

    bars = pd.read_parquet(daily_bar_path)
    actions = pd.read_parquet(corporate_action_path)
    _require_columns(
        bars,
        ["security_id", "trade_date", "price_basis", "snapshot_id", "source_snapshot_id"],
        "existing daily_bar_raw",
    )
    _require_columns(
        actions,
        [
            "security_id",
            "ex_date",
            "cash_dividend_per_share",
            "share_ratio",
            "disclosure_time_known",
            "snapshot_id",
        ],
        "existing corporate_actions",
    )
    if len(actions) != 76:
        raise DataContractError(
            f"existing golden slice snapshot requires 76 CA rows; observed={len(actions)}"
        )
    if "available_at" in actions.columns:
        raise DataContractError(
            "existing ADJUSTMENT_ONLY CA snapshot must not contain available_at"
        )
    if not actions["disclosure_time_known"].eq(False).all():  # noqa: E712
        raise DataContractError(
            "existing golden slice CA snapshot must use scheme X for every row"
        )
    for label, frame in (("daily_bar_raw", bars), ("corporate_actions", actions)):
        observed = set(frame["snapshot_id"].astype(str).unique().tolist())
        if observed != {snapshot_id}:
            raise DataContractError(
                f"existing {label} snapshot_id mismatch: {sorted(observed)}"
            )
    invalid_basis = bars.loc[
        bars["price_basis"].isna()
        | bars["price_basis"].astype(str).ne(PriceBasis.RAW_UNADJUSTED.value)
    ]
    if not invalid_basis.empty:
        raise DataContractError(
            "existing golden slice L1 snapshot contains non-RAW_UNADJUSTED rows"
        )

    counts = Counter(bars["security_id"].astype(str).str.zfill(6).tolist())
    return GoldenSliceSnapshot(
        snapshot_id=snapshot_id,
        root_dir=output_dir,
        daily_bar_path=daily_bar_path,
        corporate_action_path=corporate_action_path,
        l1_row_count=len(bars),
        l1_rows_by_security=tuple(sorted(counts.items())),
        ca_row_count=len(actions),
        source_snapshot_ids=tuple(
            sorted(bars["source_snapshot_id"].astype(str).unique().tolist())
        ),
        daily_bar_sha256=_sha256_file(daily_bar_path),
        corporate_action_sha256=_sha256_file(corporate_action_path),
    )


def _snapshot_ca_row(action: dict[str, Any], snapshot_id: str) -> dict[str, Any]:
    share_ratio = float(action["share_ratio"])
    action_type = "STOCK_DIVIDEND" if share_ratio > 0 else "CASH_DIVIDEND"
    ex_date = pd.Timestamp(action["ex_date"]).date()
    # Naming debt: combined cash+stock events use STOCK_DIVIDEND while retaining
    # both pricing inputs. This snapshot is adjustment-only and must not be fed
    # to the execution ledger, where cash and stock entitlements are distinct.
    #
    # The legacy schema exposes one cash field. For 4a it intentionally carries
    # the ex-right deduction, while the actual entitlement is preserved in the
    # explicit companion field for a future 4b execution-specific snapshot.
    return {
        "security_id": str(action["security_id"]).zfill(6),
        "ex_date": action["ex_date"],
        "record_date": action["record_date"],
        "event_ts": _market_close_timestamp(ex_date).isoformat(),
        "action_type": action_type,
        "cash_dividend_per_share": action[
            "ex_right_cash_deduction_per_share"
        ],
        "cash_dividend_actual_per_share": action["cash_dividend_per_share"],
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


def _assert_adjustment_only_snapshot_id(snapshot_id: str) -> None:
    if not snapshot_id.startswith("golden_slice_"):
        raise DataContractError("golden slice snapshot_id must start with golden_slice_")
    if not snapshot_id.endswith(f"_{ADJUSTMENT_ONLY_SUFFIX}"):
        raise DataContractError(
            "golden slice feature snapshot_id must end with ADJUSTMENT_ONLY"
        )


def _market_close_timestamp(day: date) -> pd.Timestamp:
    return pd.Timestamp(
        datetime.combine(day, time(15, 0), tzinfo=ASIA_SHANGHAI)
    )


def _require_columns(frame: pd.DataFrame, columns: list[str], label: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise DataContractError(f"{label} missing required columns: {missing}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
