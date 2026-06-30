from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from random import uniform
from time import sleep
from zoneinfo import ZoneInfo

import pandas as pd

from src.calendar import trading_calendar_from_dates
from src.data.akshare_adapter import (
    akshare_revision_id,
    fetch_exchange_trade_dates,
    fetch_hs300_constituents,
    fetch_stock_daily_raw,
)
from src.domain import (
    DAILY_BAR_REQUIRED_COLUMNS,
    DAILY_SAFETY_LATENCY_VERSION,
    BarFrequency,
    PriceBasis,
    TradeStatus,
)


INDEX_SYMBOL = "000300"
START_DATE = date(2015, 6, 30)
END_DATE = date(2026, 6, 30)
SNAPSHOT_ID = "20260630_akshare_raw"
SOURCE_ID = "akshare.raw_daily_adapter"
UNIVERSE_SOURCE = "当前沪深300成分(EXPLORATORY_TAINTED,非时点成分)"
REQUEST_DELAY_SECONDS = (0.2, 0.5)
MAX_ATTEMPTS = 3
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

OUTPUT_PATH = Path("data/l1_raw/daily_bar_raw.parquet")
MANIFEST_PATH = Path("data/l1_raw/manifest.json")
CALENDAR_PATH = Path("data/l1_raw/trading_calendar.parquet")


def available_at_iso(day: date) -> str:
    timestamp = datetime.combine(day, time(15, 0), tzinfo=SHANGHAI_TZ)
    return timestamp.isoformat()


def fetch_symbol_raw_with_retries(symbol: str) -> tuple[pd.DataFrame | None, str | None, str | None]:
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = fetch_stock_daily_raw(symbol, START_DATE, END_DATE)
        if result.frame is not None:
            return result.frame, result.source_id, None
        last_error = result.failure_reason or "unknown"

        if attempt < MAX_ATTEMPTS:
            sleep(1.0 * attempt)

    return None, None, last_error


def build_symbol_panel(
    symbol: str,
    frame: pd.DataFrame,
    trading_days: list[date],
    revision_id: str,
    source_id: str,
) -> pd.DataFrame:
    observed_dates = frame["trade_date"].dropna()
    first_observed = observed_dates.min()
    last_observed = observed_dates.max()

    panel = pd.DataFrame({"trade_date": trading_days})
    panel["security_id"] = symbol
    panel = panel.merge(frame, on=["security_id", "trade_date"], how="left")

    has_bar = panel["close"].notna()
    in_observed_range = panel["trade_date"].between(first_observed, last_observed)
    zero_volume = panel["volume"].fillna(-1).eq(0)

    panel["trade_status"] = TradeStatus.MISSING.value
    panel.loc[in_observed_range & ~has_bar, "trade_status"] = TradeStatus.SUSPENDED.value
    panel.loc[has_bar, "trade_status"] = TradeStatus.NORMAL.value
    panel.loc[has_bar & zero_volume, "trade_status"] = TradeStatus.SUSPENDED.value

    timestamps = panel["trade_date"].map(available_at_iso)
    panel["event_ts"] = timestamps
    panel["available_at"] = timestamps
    panel["price_basis"] = PriceBasis.RAW_UNADJUSTED.value
    panel["source_id"] = source_id
    panel["revision_id"] = revision_id
    panel["snapshot_id"] = SNAPSHOT_ID
    panel["declared_safety_latency_version"] = DAILY_SAFETY_LATENCY_VERSION
    panel["bar_frequency"] = BarFrequency.DAILY.value

    return panel[DAILY_BAR_REQUIRED_COLUMNS]


def build_l1_raw() -> dict[str, object]:
    calendar = trading_calendar_from_dates(fetch_exchange_trade_dates())
    trading_days = calendar.between(START_DATE, END_DATE)
    constituents = fetch_hs300_constituents(INDEX_SYMBOL)
    symbols = constituents.symbols
    revision_id = akshare_revision_id()

    frames: list[pd.DataFrame] = []
    failures: list[dict[str, str]] = []

    print(
        f"building_l1 snapshot_id={SNAPSHOT_ID}, symbols={len(symbols)}, "
        f"trading_days={len(trading_days)}, start={START_DATE}, end={END_DATE}",
        flush=True,
    )

    for index, symbol in enumerate(symbols, start=1):
        frame, source_id, failure_reason = fetch_symbol_raw_with_retries(symbol)
        if frame is None:
            failures.append({"security_id": symbol, "reason": failure_reason or "unknown"})
        else:
            try:
                frames.append(build_symbol_panel(symbol, frame, trading_days, revision_id, source_id or SOURCE_ID))
            except Exception as exc:  # noqa: BLE001
                failures.append({"security_id": symbol, "reason": f"{type(exc).__name__}: {exc}"})

        if index % 25 == 0 or index == len(symbols):
            print(
                f"progress={index}/{len(symbols)}, success={len(frames)}, failures={len(failures)}",
                flush=True,
            )

        if index < len(symbols):
            sleep(uniform(*REQUEST_DELAY_SECONDS))

    if not frames:
        raise RuntimeError("No symbols were fetched successfully")

    l1_raw = pd.concat(frames, ignore_index=True).sort_values(["security_id", "trade_date"])
    l1_raw["trade_date"] = l1_raw["trade_date"].astype(str)
    source_ids_observed = sorted(l1_raw["source_id"].dropna().unique().tolist())

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    l1_raw.to_parquet(OUTPUT_PATH, index=False)
    pd.DataFrame({"trade_date": [day.isoformat() for day in trading_days]}).to_parquet(
        CALENDAR_PATH, index=False
    )

    manifest: dict[str, object] = {
        "manifest_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_id": SNAPSHOT_ID,
        "price_basis": PriceBasis.RAW_UNADJUSTED.value,
        "bar_frequency": BarFrequency.DAILY.value,
        "source_id": SOURCE_ID,
        "source_ids_observed": source_ids_observed,
        "revision_id": revision_id,
        "declared_safety_latency_version": DAILY_SAFETY_LATENCY_VERSION,
        "start_date": START_DATE.isoformat(),
        "end_date": END_DATE.isoformat(),
        "universe": {
            "index_symbol": INDEX_SYMBOL,
            "source": UNIVERSE_SOURCE,
            "constituents_source_id": constituents.source_id,
            "constituents_source_errors": constituents.errors,
            "security_count_requested": len(symbols),
            "security_count_succeeded": len(frames),
            "security_count_failed": len(failures),
        },
        "source_capability": {
            "source_id": SOURCE_ID,
            "provider_fallback_order": [
                "akshare.stock_zh_a_hist",
                "akshare.stock_zh_a_daily_sina",
            ],
            "source_ids_observed": source_ids_observed,
            "bar_frequency": BarFrequency.DAILY.value,
            "price_basis": PriceBasis.RAW_UNADJUSTED.value,
            "schema": "daily_bar_raw",
            "future_frequency_policy": "same_schema_required_for_minute_or_other_bars",
            "time_anchor_policy": "event_ts_and_available_at_are_timezone_aware_second_precision_timestamps",
        },
        "rows": int(len(l1_raw)),
        "calendar": {
            "source_id": "akshare.tool_trade_date_hist_sina",
            "path": CALENDAR_PATH.as_posix(),
            "trading_day_count": len(trading_days),
        },
        "output_path": OUTPUT_PATH.as_posix(),
        "failures": failures,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    manifest = build_l1_raw()
    print(f"successful_symbols={manifest['universe']['security_count_succeeded']}", flush=True)
    print(f"failed_symbols={manifest['universe']['security_count_failed']}", flush=True)
    print(f"rows={manifest['rows']}", flush=True)
    if manifest["failures"]:
        print("failures:", flush=True)
        for failure in manifest["failures"]:
            print(f"- {failure['security_id']}: {failure['reason']}", flush=True)
    else:
        print("failures=[]", flush=True)
    print(f"saved={OUTPUT_PATH}", flush=True)
    print(f"manifest={MANIFEST_PATH}", flush=True)


if __name__ == "__main__":
    main()
