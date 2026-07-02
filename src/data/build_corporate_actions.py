from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from random import uniform
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd

from src.data.akshare_adapter import (
    akshare_revision_id,
    fetch_hs300_constituents,
    fetch_stock_corporate_actions,
)


INDEX_SYMBOL = "000300"
START_DATE = date(2015, 6, 30)
END_DATE = date(2026, 6, 30)
SNAPSHOT_ID = "20260630_corporate_actions_akshare"
L1_DAILY_BAR_PATH = Path("data/l1_raw/daily_bar_raw.parquet")
OUTPUT_PATH = Path("data/l2_corporate_actions/corporate_actions.parquet")
MANIFEST_PATH = Path("data/l2_corporate_actions/manifest.json")
REQUEST_DELAY_SECONDS = (0.15, 0.35)
MAX_ATTEMPTS = 2
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

EXPLORATORY_TAINTED = "EXPLORATORY_TAINTED"
POINT_IN_TIME_CAPABILITY = "ANNOUNCEMENT_DATE_CLOSE_BEST_EFFORT"

REQUIRED_COLUMNS = [
    "security_id",
    "ex_date",
    "action_type",
    "cash_dividend_per_share",
    "share_ratio",
    "event_ts",
    "available_at",
    "source_id",
    "snapshot_id",
]


def shanghai_close(day: date) -> datetime:
    return datetime.combine(day, time(15, 0), tzinfo=SHANGHAI_TZ)


def _read_universe() -> tuple[list[str], dict[str, object]]:
    if L1_DAILY_BAR_PATH.exists():
        l1 = pd.read_parquet(L1_DAILY_BAR_PATH, columns=["security_id"])
        symbols = sorted(l1["security_id"].astype(str).str.zfill(6).unique().tolist())
        return symbols, {
            "index_symbol": INDEX_SYMBOL,
            "source": L1_DAILY_BAR_PATH.as_posix(),
            "source_id": "data.l1_raw.daily_bar_raw.security_id",
            "source_errors": [],
        }

    constituents = fetch_hs300_constituents(INDEX_SYMBOL)
    return constituents.symbols, {
        "index_symbol": INDEX_SYMBOL,
        "source": "akshare current HS300 constituents",
        "source_id": constituents.source_id,
        "source_errors": constituents.errors,
    }


def _fetch_symbol_with_retries(symbol: str) -> tuple[pd.DataFrame, str, list[str], list[str]]:
    last_errors: list[str] = []
    last_gaps: list[str] = []
    last_source_id = "UNAVAILABLE"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = fetch_stock_corporate_actions(symbol, START_DATE, END_DATE)
        last_errors = result.errors
        last_gaps = result.coverage_gaps
        last_source_id = result.source_id
        if not result.frame.empty or result.source_id != "UNAVAILABLE":
            return result.frame, result.source_id, result.errors, result.coverage_gaps
        if attempt < MAX_ATTEMPTS:
            sleep(1.0 * attempt)

    return pd.DataFrame(), last_source_id, last_errors, last_gaps


def _attach_pit_columns(frame: pd.DataFrame, revision_id: str) -> pd.DataFrame:
    actions = frame.copy()
    ex_dates = pd.to_datetime(actions["ex_date"], errors="raise").dt.date
    if "announcement_date" in actions.columns:
        announcement_dates = pd.to_datetime(actions["announcement_date"], errors="coerce").dt.date
    else:
        announcement_dates = pd.Series(pd.NaT, index=actions.index)

    available_dates = announcement_dates.where(announcement_dates.notna(), ex_dates)
    actions["ex_date"] = pd.to_datetime(ex_dates, errors="raise").dt.tz_localize(SHANGHAI_TZ)
    actions["event_ts"] = actions["ex_date"] + pd.Timedelta(hours=15)
    actions["available_at"] = pd.to_datetime(available_dates, errors="raise").dt.tz_localize(
        SHANGHAI_TZ
    ) + pd.Timedelta(hours=15)
    actions["snapshot_id"] = SNAPSHOT_ID
    actions["revision_id"] = revision_id
    actions["point_in_time_capability"] = POINT_IN_TIME_CAPABILITY
    actions["evidence_level"] = EXPLORATORY_TAINTED

    for column in ("record_date", "announcement_date"):
        if column in actions.columns:
            values = pd.to_datetime(actions[column], errors="coerce").dt.date.astype(str)
            actions[column] = values.mask(values.eq("NaT"))

    return actions.sort_values(["security_id", "ex_date", "action_type"]).reset_index(drop=True)


def _availability_diagnostics(actions: pd.DataFrame) -> dict[str, object]:
    if actions.empty:
        return {
            "announcement_date_non_null_count": 0,
            "announcement_date_non_null_ratio": 0.0,
            "available_at_fallback_to_ex_date_count": 0,
            "ex_date_minus_announcement_date_days_min": None,
            "ex_date_minus_announcement_date_days_max": None,
            "zero_day_lag_count": 0,
            "zero_day_lag_security_ids": [],
            "zero_day_lag_known_limitation": (
                "No zero-day announcement records in this snapshot."
            ),
        }

    ex_dates = pd.to_datetime(actions["ex_date"], errors="raise").dt.date
    announcement_dates = pd.to_datetime(actions["announcement_date"], errors="coerce").dt.date
    announcement_present = announcement_dates.notna()
    diffs = pd.Series(
        [
            (ex_date - announcement_date).days if pd.notna(announcement_date) else None
            for ex_date, announcement_date in zip(ex_dates, announcement_dates)
        ],
        index=actions.index,
        dtype="object",
    )
    valid_diffs = diffs.dropna().astype(int)
    zero_day = valid_diffs.eq(0)
    zero_day_security_ids = sorted(
        actions.loc[zero_day[zero_day].index, "security_id"].astype(str).str.zfill(6).unique().tolist()
    )
    return {
        "announcement_date_non_null_count": int(announcement_present.sum()),
        "announcement_date_non_null_ratio": float(announcement_present.mean()),
        "available_at_fallback_to_ex_date_count": int((~announcement_present).sum()),
        "ex_date_minus_announcement_date_days_min": int(valid_diffs.min()) if not valid_diffs.empty else None,
        "ex_date_minus_announcement_date_days_max": int(valid_diffs.max()) if not valid_diffs.empty else None,
        "zero_day_lag_count": int(zero_day.sum()),
        "zero_day_lag_security_ids": zero_day_security_ids,
        "zero_day_lag_known_limitation": (
            "Records announced on the ex-date use announcement-date 15:00 available_at; "
            "they remain invisible at that ex-date open."
        ),
    }


def _empty_output_frame() -> pd.DataFrame:
    columns = REQUIRED_COLUMNS + [
        "record_date",
        "announcement_date",
        "action_description",
        "revision_id",
        "point_in_time_capability",
        "evidence_level",
    ]
    return pd.DataFrame(columns=columns)


def build_corporate_actions() -> dict[str, object]:
    symbols, universe = _read_universe()
    revision_id = akshare_revision_id()
    frames: list[pd.DataFrame] = []
    failures: list[dict[str, object]] = []
    source_errors: dict[str, list[str]] = {}
    gap_symbols: dict[str, list[str]] = {}
    no_action_symbols: list[str] = []

    print(
        f"building_corporate_actions snapshot_id={SNAPSHOT_ID}, symbols={len(symbols)}, "
        f"start={START_DATE}, end={END_DATE}",
        flush=True,
    )

    for index, symbol in enumerate(symbols, start=1):
        try:
            frame, source_id, errors, gaps = _fetch_symbol_with_retries(symbol)
        except Exception as exc:  # noqa: BLE001
            failures.append({"security_id": symbol, "reason": f"{type(exc).__name__}: {exc}"})
            frame = pd.DataFrame()
            source_id = "UNAVAILABLE"
            errors = []
            gaps = []

        if errors:
            source_errors[symbol] = errors
        for gap in gaps:
            gap_symbols.setdefault(gap, []).append(symbol)

        if frame.empty:
            if source_id == "UNAVAILABLE":
                failures.append({"security_id": symbol, "reason": "all corporate action providers unavailable"})
            else:
                no_action_symbols.append(symbol)
        else:
            frames.append(frame)

        if index % 25 == 0 or index == len(symbols):
            print(
                f"progress={index}/{len(symbols)}, rows_so_far={sum(len(item) for item in frames)}, "
                f"failures={len(failures)}",
                flush=True,
            )

        if index < len(symbols):
            sleep(uniform(*REQUEST_DELAY_SECONDS))

    if frames:
        actions = _attach_pit_columns(pd.concat(frames, ignore_index=True), revision_id)
    else:
        actions = _empty_output_frame()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    actions.to_parquet(OUTPUT_PATH, index=False)

    action_type_distribution = actions["action_type"].value_counts().sort_index().to_dict()
    source_distribution = actions["source_id"].value_counts().sort_index().to_dict()
    availability_diagnostics = _availability_diagnostics(actions)
    manifest: dict[str, object] = {
        "manifest_version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "snapshot_id": SNAPSHOT_ID,
        "output_path": OUTPUT_PATH.as_posix(),
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "rows": int(len(actions)),
        "security_count_requested": len(symbols),
        "security_count_with_actions": int(actions["security_id"].nunique()) if not actions.empty else 0,
        "security_count_no_actions_observed": len(no_action_symbols),
        "security_count_failed": len(failures),
        "universe": universe,
        "source_capability": {
            "source_id": "akshare.corporate_actions_adapter",
            "provider_fallback_order": [
                "akshare.stock_fhps_detail_em",
                "akshare.stock_dividend_cninfo",
                "akshare.stock_fhps_detail_ths",
                "akshare.stock_history_dividend_detail.dividend",
            ],
            "rights_issue_provider": "akshare.stock_history_dividend_detail.rights",
            "source_ids_observed": sorted(actions["source_id"].dropna().unique().tolist())
            if not actions.empty
            else [],
        },
        "evidence_level": EXPLORATORY_TAINTED,
        "point_in_time_capability": {
            "level": POINT_IN_TIME_CAPABILITY,
            "available_at_semantics": (
                "event_ts is set to the ex_date close, 15:00 Asia/Shanghai; "
                "available_at is set to the announcement_date close, 15:00 Asia/Shanghai. "
                "If announcement_date is missing, available_at defensively falls back to "
                "the ex_date close and the fallback is counted in availability_diagnostics."
            ),
            "availability_diagnostics": availability_diagnostics,
        },
        "best_effort_warning": (
            "This is a best-effort corporate action ledger. Completeness and accuracy have "
            "not been manually verified event by event."
        ),
        "coverage_gaps": {
            "SPLIT": "Not separately identified by the selected dividend/rights interfaces.",
            "MERGER": "Not covered by the selected dividend/rights interfaces.",
            "DELIST": "Not covered by the selected dividend/rights interfaces.",
            "RIGHTS_ISSUE": "Best effort via Sina rights provider only; provider failures are listed.",
        },
        "coverage_gap_symbols": {
            gap: sorted(values) for gap, values in sorted(gap_symbols.items())
        },
        "action_type_distribution": action_type_distribution,
        "source_distribution": source_distribution,
        "failures": failures,
        "source_errors": source_errors,
        "no_action_symbols": no_action_symbols,
        "required_columns": REQUIRED_COLUMNS,
        "revision_id": revision_id,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    manifest = build_corporate_actions()
    print(f"security_count_requested={manifest['security_count_requested']}", flush=True)
    print(f"security_count_with_actions={manifest['security_count_with_actions']}", flush=True)
    print(f"rows={manifest['rows']}", flush=True)
    print(f"action_type_distribution={manifest['action_type_distribution']}", flush=True)
    print(f"failed_symbols={manifest['security_count_failed']}", flush=True)
    print(f"saved={OUTPUT_PATH}", flush=True)
    print(f"manifest={MANIFEST_PATH}", flush=True)


if __name__ == "__main__":
    main()
