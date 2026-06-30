from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.data.akshare_adapter import (
    akshare_revision_id,
    fetch_current_a_share_names,
    fetch_current_st_symbols,
    fetch_current_status_overrides,
    fetch_exchange_listing_info,
)


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
L1_DAILY_BAR_PATH = Path("data/l1_raw/daily_bar_raw.parquet")
OUTPUT_PATH = Path("data/l1_raw/security_master.parquet")
MANIFEST_PATH = Path("data/l1_raw/security_master_manifest.json")
SNAPSHOT_ID = "20260630_security_master_akshare"
UNKNOWN_LIST_DATE_AVAILABLE_AT = "1990-12-19T15:00:00+08:00"

POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY = "CURRENT_SNAPSHOT_ONLY"
POINT_IN_TIME_STABLE_BY_CODE_PREFIX = "STABLE_BY_CODE_PREFIX"
POINT_IN_TIME_ONE_TIME_EVENT = "ONE_TIME_EVENT_BEST_EFFORT"
EXPLORATORY_TAINTED = "EXPLORATORY_TAINTED"
BEST_EFFORT_RELIABLE = "BEST_EFFORT_RELIABLE"


def now_shanghai_iso() -> str:
    return datetime.now(SHANGHAI_TZ).replace(microsecond=0).isoformat()


def shanghai_close_iso(day: date) -> str:
    return datetime.combine(day, time(15, 0), tzinfo=SHANGHAI_TZ).isoformat()


def snapshot_date_from_id(snapshot_id: str) -> date:
    snapshot_date = snapshot_id.split("_", maxsplit=1)[0]
    return datetime.strptime(snapshot_date, "%Y%m%d").date()


def available_at_from_date(value: object) -> str:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return UNKNOWN_LIST_DATE_AVAILABLE_AT
    return shanghai_close_iso(timestamp.date())


def board_from_security_id(security_id: str) -> str:
    if security_id.startswith(("688", "689")):
        return "科创板"
    if security_id.startswith(("300", "301")):
        return "创业板"
    if security_id.startswith(("8", "9", "4")):
        return "北交所"
    return "主板"


def _read_l1_security_ids() -> list[str]:
    if not L1_DAILY_BAR_PATH.exists():
        raise FileNotFoundError(f"L1 daily bar not found: {L1_DAILY_BAR_PATH}")

    l1 = pd.read_parquet(L1_DAILY_BAR_PATH, columns=["security_id"])
    security_ids = sorted(l1["security_id"].astype(str).str.zfill(6).unique().tolist())
    if not security_ids:
        raise ValueError("No security_id found in L1 daily bar")
    return security_ids


def build_security_master() -> dict[str, object]:
    as_of = now_shanghai_iso()
    snapshot_available_at = shanghai_close_iso(snapshot_date_from_id(SNAPSHOT_ID))
    security_ids = _read_l1_security_ids()
    master = pd.DataFrame({"security_id": security_ids})

    names = fetch_current_a_share_names()
    listings = fetch_exchange_listing_info()
    st_symbols = fetch_current_st_symbols()
    status_overrides = fetch_current_status_overrides()

    master = master.merge(names.frame, on="security_id", how="left")
    master = master.merge(listings.frame, on="security_id", how="left")

    master["name"] = master["name"].fillna(master["exchange_name"])
    master["board"] = master["security_id"].map(board_from_security_id)
    master["delist_date"] = pd.NaT

    if st_symbols.source_id == "UNAVAILABLE":
        master["is_st"] = "UNAVAILABLE"
    else:
        st_set = set(st_symbols.frame["security_id"].astype(str).str.zfill(6).tolist())
        master["is_st"] = master["security_id"].isin(st_set)

    master["status"] = "正常"
    if status_overrides.source_id == "UNAVAILABLE":
        master["status"] = "UNAVAILABLE"
    elif not status_overrides.frame.empty:
        override_map = dict(
            zip(
                status_overrides.frame["security_id"].astype(str).str.zfill(6),
                status_overrides.frame["status"].astype(str),
                strict=False,
            )
        )
        master["status"] = master["security_id"].map(override_map).fillna(master["status"])

    master["available_at"] = master["list_date"].map(available_at_from_date)
    list_date_missing_count = int(pd.to_datetime(master["list_date"], errors="coerce").isna().sum())
    master["board_available_at"] = master["available_at"]
    master["list_date_available_at"] = master["available_at"]
    master["delist_date_available_at"] = master["delist_date"].map(available_at_from_date)
    master.loc[master["delist_date"].isna(), "delist_date_available_at"] = master["available_at"]
    master["is_st_available_at"] = snapshot_available_at
    master["status_available_at"] = snapshot_available_at

    master["board_as_of"] = as_of
    master["board_point_in_time_capability"] = POINT_IN_TIME_STABLE_BY_CODE_PREFIX
    master["board_evidence_level"] = BEST_EFFORT_RELIABLE
    master["is_st_as_of"] = as_of
    master["is_st_point_in_time_capability"] = POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY
    master["is_st_evidence_level"] = EXPLORATORY_TAINTED
    master["status_as_of"] = as_of
    master["status_point_in_time_capability"] = POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY
    master["status_evidence_level"] = EXPLORATORY_TAINTED
    master["list_date_as_of"] = as_of
    master["list_date_point_in_time_capability"] = POINT_IN_TIME_ONE_TIME_EVENT
    master["list_date_evidence_level"] = BEST_EFFORT_RELIABLE
    master["delist_date_as_of"] = as_of
    master["delist_date_point_in_time_capability"] = POINT_IN_TIME_ONE_TIME_EVENT
    master["delist_date_evidence_level"] = BEST_EFFORT_RELIABLE
    master["snapshot_id"] = SNAPSHOT_ID
    master["revision_id"] = akshare_revision_id()

    output_columns = [
        "security_id",
        "name",
        "list_date",
        "delist_date",
        "board",
        "is_st",
        "status",
        "available_at",
        "board_available_at",
        "board_as_of",
        "board_point_in_time_capability",
        "board_evidence_level",
        "is_st_available_at",
        "is_st_as_of",
        "is_st_point_in_time_capability",
        "is_st_evidence_level",
        "status_available_at",
        "status_as_of",
        "status_point_in_time_capability",
        "status_evidence_level",
        "list_date_available_at",
        "list_date_as_of",
        "list_date_point_in_time_capability",
        "list_date_evidence_level",
        "delist_date_available_at",
        "delist_date_as_of",
        "delist_date_point_in_time_capability",
        "delist_date_evidence_level",
        "list_date_source_id",
        "snapshot_id",
        "revision_id",
    ]
    master = master[output_columns].sort_values("security_id").reset_index(drop=True)
    master["list_date"] = pd.to_datetime(master["list_date"], errors="coerce").dt.date.astype(str)
    master.loc[master["list_date"].eq("NaT"), "list_date"] = None
    master["delist_date"] = None

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    master.to_parquet(OUTPUT_PATH, index=False)

    manifest: dict[str, object] = {
        "manifest_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "snapshot_id": SNAPSHOT_ID,
        "output_path": OUTPUT_PATH.as_posix(),
        "security_count": int(len(master)),
        "l1_daily_bar_path": L1_DAILY_BAR_PATH.as_posix(),
        "snapshot_available_at": snapshot_available_at,
        "unknown_list_date_available_at": UNKNOWN_LIST_DATE_AVAILABLE_AT,
        "list_date_missing_count": list_date_missing_count,
        "available_at_semantics": {
            "row_available_at": (
                "Structural visibility for stable security identity attributes. "
                "Uses list_date close in Asia/Shanghai; missing list_date uses "
                f"{UNKNOWN_LIST_DATE_AVAILABLE_AT} and is counted in list_date_missing_count."
            ),
            "stable_fields": {
                "fields": ["list_date", "delist_date", "board"],
                "available_at": "corresponding event/list_date close in Asia/Shanghai",
            },
            "current_snapshot_fields": {
                "fields": ["is_st", "status"],
                "available_at": (
                    "snapshot date close in Asia/Shanghai. Values reflect only the pull-time "
                    "current snapshot and are not historical point-in-time facts."
                ),
            },
        },
        "source_ids": {
            "names": names.source_id,
            "listing_info": listings.source_id,
            "current_st": st_symbols.source_id,
            "current_status": status_overrides.source_id,
        },
        "source_errors": {
            "names": names.errors,
            "listing_info": listings.errors,
            "current_st": st_symbols.errors,
            "current_status": status_overrides.errors,
        },
        "field_capabilities": {
            "board": {
                "source": "code_prefix_rule",
                "point_in_time_capability": POINT_IN_TIME_STABLE_BY_CODE_PREFIX,
                "evidence_level": BEST_EFFORT_RELIABLE,
                "available_at_semantics": "board_available_at = list_date close, or conservative fallback when list_date is missing",
            },
            "list_date": {
                "source": "akshare exchange listing tables",
                "point_in_time_capability": POINT_IN_TIME_ONE_TIME_EVENT,
                "evidence_level": BEST_EFFORT_RELIABLE,
                "available_at_semantics": "list_date_available_at = list_date close, or conservative fallback when list_date is missing",
            },
            "delist_date": {
                "source": "not present for current HS300 universe",
                "point_in_time_capability": POINT_IN_TIME_ONE_TIME_EVENT,
                "evidence_level": BEST_EFFORT_RELIABLE,
                "available_at_semantics": "delist_date_available_at = delist_date close when present; otherwise row structural available_at",
            },
            "is_st": {
                "source": st_symbols.source_id,
                "point_in_time_capability": POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY,
                "evidence_level": EXPLORATORY_TAINTED,
                "available_at_semantics": "is_st_available_at = snapshot date close; not visible before that asof_ts",
                "usage_warning": "非时点 ST 状态, 不得用于可信回测的历史时点判断",
            },
            "status": {
                "source": status_overrides.source_id,
                "point_in_time_capability": POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY,
                "evidence_level": EXPLORATORY_TAINTED,
                "available_at_semantics": "status_available_at = snapshot date close; not visible before that asof_ts",
                "usage_warning": "非时点交易状态, 不得用于可信回测的历史时点判断",
            },
        },
        "board_distribution": master["board"].value_counts().sort_index().to_dict(),
        "st_count": None
        if st_symbols.source_id == "UNAVAILABLE"
        else int(master["is_st"].eq(True).sum()),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    manifest = build_security_master()
    print(f"security_count={manifest['security_count']}", flush=True)
    print(f"board_distribution={manifest['board_distribution']}", flush=True)
    print(f"st_count={manifest['st_count']}", flush=True)
    print(f"saved={OUTPUT_PATH}", flush=True)
    print(f"manifest={MANIFEST_PATH}", flush=True)


if __name__ == "__main__":
    main()
