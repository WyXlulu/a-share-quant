from __future__ import annotations

import copy
import hashlib
import json
from datetime import date
from typing import Any

import pandas as pd

from src.domain import DataContractError


GOLDEN_SLICE_SECURITY_IDS = (
    "601398",
    "600036",
    "601939",
    "600519",
    "000858",
    "600900",
    "600028",
    "601318",
    "000333",
    "000651",
    "600276",
    "601668",
)
EXCLUDED_RECENT_IPO_SECURITY_IDS = (
    "001280",
    "688047",
    "688506",
    "688521",
    "688981",
)
GOLDEN_SLICE_START = "2020-01-01"
GOLDEN_SLICE_END = "2023-12-31"
UNFROZEN_HASH = "UNFROZEN"


class GoldenSliceManifestError(DataContractError):
    """Raised when the golden slice manifest violates freeze governance."""


def build_unfrozen_manifest(ca_verification_slots: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "manifest_version": 1,
        "security_ids": list(GOLDEN_SLICE_SECURITY_IDS),
        "time_window": {"start": GOLDEN_SLICE_START, "end": GOLDEN_SLICE_END},
        "trading_calendar_version": "data/l1_raw/trading_calendar.parquet",
        "rule_table_version": {
            "corporate_action_reference_price": "cn_a_share_ex_right_reference_price_2011+",
            "fee_schedule": "execution_fee_schedule_versioned",
        },
        "safety_latency_config_version": "daily_bar_t1500_asia_shanghai_v1",
        "selection_reasons": {
            security_id: "large-cap manually auditable golden-slice candidate"
            for security_id in GOLDEN_SLICE_SECURITY_IDS
        },
        "excluded_securities": {
            security_id: "recent IPO / incomplete L2 corporate-action ledger coverage; excluded before results"
            for security_id in EXCLUDED_RECENT_IPO_SECURITY_IDS
        },
        "allowed_feature_categories": [
            "PIT_DERIVED_ADJUSTED_RETURNS",
            "CROSS_SECTIONAL_MOMENTUM",
            "FUTURE_RETURN_LABELS_FOR_EVALUATION_ONLY",
        ],
        "ca_verification_slots": ca_verification_slots or [],
        "selection_frozen_at": None,
        "selection_independent_of_strategy_results": True,
        "manifest_hash": UNFROZEN_HASH,
    }


def compute_manifest_hash(manifest: dict[str, Any]) -> str:
    payload = copy.deepcopy(manifest)
    payload.pop("manifest_hash", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def freeze(manifest: dict[str, Any], *, frozen_at: str) -> dict[str, Any]:
    frozen = copy.deepcopy(manifest)
    frozen["selection_frozen_at"] = frozen_at
    frozen["manifest_hash"] = compute_manifest_hash(frozen)
    return frozen


def assert_frozen_and_consistent(manifest: dict[str, Any]) -> None:
    recorded_hash = manifest.get("manifest_hash")
    if not manifest.get("selection_frozen_at") or recorded_hash in (None, "", UNFROZEN_HASH):
        raise GoldenSliceManifestError("golden slice manifest is not frozen")
    current_hash = compute_manifest_hash(manifest)
    if current_hash != recorded_hash:
        raise GoldenSliceManifestError(
            "golden slice manifest hash mismatch; selection may have changed after freeze"
        )


def build_ca_verification_slots(
    actions: pd.DataFrame,
    *,
    security_ids: tuple[str, ...] = GOLDEN_SLICE_SECURITY_IDS,
    start: str = GOLDEN_SLICE_START,
    end: str = GOLDEN_SLICE_END,
) -> list[dict[str, Any]]:
    _require_columns(
        actions,
        [
            "security_id",
            "ex_date",
            "action_type",
            "cash_dividend_per_share",
            "share_ratio",
            "rights_price_per_share",
            "available_at",
            "source_id",
        ],
    )
    rows = actions.copy()
    rows["_security_id"] = rows["security_id"].astype(str).str.zfill(6)
    rows["_ex_date"] = pd.to_datetime(rows["ex_date"], errors="raise").dt.date
    start_date = pd.Timestamp(start).date()
    end_date = pd.Timestamp(end).date()
    selected_ids = set(security_ids)
    selected = rows.loc[
        rows["_security_id"].isin(selected_ids)
        & rows["_ex_date"].between(start_date, end_date)
    ].copy()
    selected = selected.sort_values(["_security_id", "_ex_date", "action_type", "available_at"])
    counts = selected.groupby("_security_id").size().to_dict()

    slots: list[dict[str, Any]] = []
    for _, row in selected.iterrows():
        security_id = str(row["_security_id"])
        action_type = str(row["action_type"])
        slots.append(
            {
                "security_id": security_id,
                "ex_date": row["_ex_date"].isoformat(),
                "action_type": action_type,
                "ledger_cash_dividend_per_share": _string_or_blank(row["cash_dividend_per_share"]),
                "ledger_share_ratio": _string_or_blank(row["share_ratio"]),
                "ledger_rights_price_per_share": _string_or_blank(
                    row["rights_price_per_share"]
                ),
                "available_at": str(row["available_at"]),
                "source_id": str(row["source_id"]),
                "ledger_claimed_ca_count_for_security": int(counts.get(security_id, 0)),
                "manual_focus": _manual_focus_reason(action_type, int(counts.get(security_id, 0))),
                "verified_cash_dividend": "",
                "verified_ratio": "",
                "verified_source": "",
                "verified_by": "",
                "verified_at": "",
            }
        )
    return slots


def _manual_focus_reason(action_type: str, count: int) -> str:
    reasons: list[str] = []
    if count > 12:
        reasons.append("CA_COUNT_GT_3_PER_YEAR")
    if action_type not in {"CASH_DIVIDEND", "STOCK_DIVIDEND"}:
        reasons.append(f"COMPLEX_ACTION:{action_type}")
    return ";".join(reasons)


def _require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise DataContractError(f"golden slice CA checklist missing columns: {missing}")


def _string_or_blank(value: Any) -> str:
    if pd.isna(value):
        return ""
    return str(value)
