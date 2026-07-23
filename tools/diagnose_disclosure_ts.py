from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


RAW_DIR = Path("data/golden_slice/cninfo_raw")
SAMPLE_PATH = RAW_DIR / "600519_ch1.json"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
SECURITY_CODES = (
    "600519",
    "600276",
    "600036",
    "600900",
    "601398",
    "601668",
    "601939",
    "601318",
    "600028",
    "000858",
    "000333",
    "000651",
)
CHANNELS = ("ch1", "ch2", "ch3")
BUSINESS_PATTERN = re.compile(r"权益分派|分红|派息|现金红利|送股|转增|配股")
CORRECTION_PATTERN = re.compile(r"更正|补充|订正")
TIME_KEY_PATTERN = re.compile(r"time|date", re.IGNORECASE)
ROUTE_B_WINDOW_MS = 15 * 24 * 60 * 60 * 1000


def main() -> None:
    records_by_code = load_all_channel_records()
    print_first_three_records(records_by_code["600519"]["ch1"][:3])

    listing_records = rebuild_listing_records(records_by_code)
    if len(listing_records) != 149:
        raise RuntimeError(
            "Rebuilt listing count does not match the completed evidence run: "
            f"expected=149, actual={len(listing_records)}"
        )

    nonzero_hms = print_listing_time_statistics(listing_records)
    print_all_time_like_fields(records_by_code, nonzero_hms)


def load_all_channel_records() -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for code in SECURITY_CODES:
        result[code] = {}
        for channel in CHANNELS:
            path = RAW_DIR / f"{code}_{channel}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("code") != code or payload.get("channel") != channel:
                raise RuntimeError(f"Raw response identity mismatch: {path}")
            pages = payload.get("responses")
            if not isinstance(pages, list) or not pages:
                raise RuntimeError(f"Raw response has no pages: {path}")
            records = flatten_announcements(pages, path)
            total = int(pages[0]["totalRecordNum"])
            if len(records) != total:
                raise RuntimeError(
                    f"Raw response total mismatch for {path}: "
                    f"total={total}, records={len(records)}"
                )
            result[code][channel] = records
    return result


def flatten_announcements(
    pages: list[dict[str, Any]], path: Path
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page in pages:
        announcements = page.get("announcements") or []
        if not isinstance(announcements, list):
            raise RuntimeError(f"announcements is not a list: {path}")
        records.extend(announcements)
    return records


def print_first_three_records(records: list[dict[str, Any]]) -> None:
    if len(records) < 3:
        raise RuntimeError(f"Sample file has fewer than three announcements: {SAMPLE_PATH}")
    print(f"SAMPLE_FILE: {SAMPLE_PATH}")
    for index, record in enumerate(records, start=1):
        print(f"\n=== SAMPLE {index}: ALL KEY/VALUE PAIRS ===")
        print(json.dumps(record, ensure_ascii=False, indent=2))
        raw = record.get("announcementTime")
        print(f"announcementTime raw: {raw!r}")
        print(f"as epoch seconds:      {convert_epoch(raw, divisor=1)}")
        print(f"as epoch milliseconds: {convert_epoch(raw, divisor=1000)}")


def convert_epoch(raw: Any, divisor: int) -> str:
    try:
        numeric = int(raw)
        converted = datetime.fromtimestamp(
            numeric / divisor, tz=timezone.utc
        ).astimezone(SHANGHAI_TZ)
        return converted.isoformat(timespec="seconds")
    except (OSError, OverflowError, TypeError, ValueError) as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def rebuild_listing_records(
    records_by_code: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    listing_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for code, channels in records_by_code.items():
        for channel in ("ch1", "ch2"):
            for record in channels[channel]:
                add_listing_record(listing_by_key, code, record)

    anchors_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (code, _), record in listing_by_key.items():
        if not CORRECTION_PATTERN.search(title_of(record)):
            anchors_by_code[code].append(record)

    for code, channels in records_by_code.items():
        for record in channels["ch3"]:
            title = title_of(record)
            route_a = bool(
                BUSINESS_PATTERN.search(title) and CORRECTION_PATTERN.search(title)
            )
            route_b = has_route_b_anchor(record, anchors_by_code[code])
            if route_a or route_b:
                add_listing_record(listing_by_key, code, record)

    return list(listing_by_key.values())


def add_listing_record(
    listing_by_key: dict[tuple[str, str], dict[str, Any]],
    code: str,
    record: dict[str, Any],
) -> None:
    adjunct_url = normalize_adjunct_url(record.get("adjunctUrl"))
    if not adjunct_url:
        raise RuntimeError(f"{code} announcement has no adjunctUrl")
    listing_by_key.setdefault((code, adjunct_url), record)


def has_route_b_anchor(
    correction: dict[str, Any], anchors: list[dict[str, Any]]
) -> bool:
    if not CORRECTION_PATTERN.search(title_of(correction)):
        return False
    correction_time = raw_announcement_time(correction)
    for anchor in anchors:
        delta = correction_time - raw_announcement_time(anchor)
        if 0 <= delta <= ROUTE_B_WINDOW_MS:
            return True
    return False


def title_of(record: dict[str, Any]) -> str:
    return str(record.get("announcementTitle") or "")


def normalize_adjunct_url(raw: Any) -> str:
    value = str(raw or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.split("static.cninfo.com.cn/", 1)[-1].lstrip("/")
    return value.lstrip("/")


def raw_announcement_time(record: dict[str, Any]) -> int:
    raw = record.get("announcementTime")
    if raw is None:
        raise RuntimeError(f"announcementTime missing: {title_of(record)}")
    return int(raw)


def print_listing_time_statistics(listing_records: list[dict[str, Any]]) -> int:
    lengths: Counter[int] = Counter()
    nonzero_hms = 0
    nonzero_records: list[dict[str, Any]] = []
    distinct_hms: Counter[str] = Counter()
    for record in listing_records:
        raw = raw_announcement_time(record)
        lengths[len(str(raw))] += 1
        converted = datetime.fromtimestamp(
            raw / 1000, tz=timezone.utc
        ).astimezone(SHANGHAI_TZ)
        hms = converted.strftime("%H:%M:%S")
        distinct_hms[hms] += 1
        if (converted.hour, converted.minute, converted.second) != (0, 0, 0):
            nonzero_hms += 1
            nonzero_records.append(
                {
                    "security_id": record.get("secCode"),
                    "announcementTitle": title_of(record),
                    "announcementTime_raw": raw,
                    "converted_Asia_Shanghai": converted.isoformat(
                        timespec="seconds"
                    ),
                    "adjunctUrl": normalize_adjunct_url(record.get("adjunctUrl")),
                }
            )

    print("\n=== LISTING 149: announcementTime STATISTICS ===")
    print(
        "raw string length distribution: "
        + json.dumps(dict(sorted(lengths.items())), ensure_ascii=False)
    )
    print(f"milliseconds conversion with non-zero HH:MM:SS: {nonzero_hms}")
    print(
        "HH:MM:SS distribution after milliseconds conversion: "
        + json.dumps(dict(sorted(distinct_hms.items())), ensure_ascii=False)
    )
    print(
        "non-zero HH:MM:SS records: "
        + json.dumps(nonzero_records, ensure_ascii=False, indent=2)
    )
    return nonzero_hms


def print_all_time_like_fields(
    records_by_code: dict[str, dict[str, list[dict[str, Any]]]],
    listing_nonzero_hms: int,
) -> None:
    stats: dict[str, dict[str, Any]] = {}
    page_column_values: Counter[str] = Counter()
    total_records = 0
    for channels in records_by_code.values():
        for records in channels.values():
            for record in records:
                total_records += 1
                for key, value in record.items():
                    if TIME_KEY_PATTERN.search(key):
                        field = stats.setdefault(
                            key,
                            {"present": 0, "non_null": 0, "examples": []},
                        )
                        field["present"] += 1
                        if value is not None:
                            field["non_null"] += 1
                            if value not in field["examples"] and len(field["examples"]) < 5:
                                field["examples"].append(value)
                if "pageColumn" in record:
                    page_column_values[str(record["pageColumn"])] += 1

    print("\n=== ALL RAW ANNOUNCEMENTS: TIME/DATE-LIKE FIELDS ===")
    print(f"announcement records scanned across all cached channels: {total_records}")
    for key in sorted(stats):
        field = stats[key]
        print(
            f"{key}: present={field['present']}, non_null={field['non_null']}, "
            f"examples={json.dumps(field['examples'], ensure_ascii=False)}"
        )
    for expected in ("pubTime", "adjunctTime", "announcementTimeStr"):
        if expected not in stats:
            print(f"{expected}: ABSENT")
    print(
        "pageColumn values: "
        + json.dumps(dict(sorted(page_column_values.items())), ensure_ascii=False)
    )

    precise_candidates = [
        key
        for key, field in stats.items()
        if key != "announcementTime" and field["non_null"] > 0
    ]
    if precise_candidates:
        print(
            "OTHER NON-NULL TIME/DATE CANDIDATES: "
            + json.dumps(sorted(precise_candidates), ensure_ascii=False)
        )
    else:
        print("OTHER NON-NULL TIME/DATE CANDIDATES: none")
        if listing_nonzero_hms:
            print(
                "CONCLUSION: announcementTime是唯一非空时间字段；少数记录保留秒级时刻，"
                "其余记录由源头归一到日期零点，原始接口无可替代的精确披露时刻字段"
            )
        else:
            print("CONCLUSION: 原始接口无精确到分钟的披露时刻")


if __name__ == "__main__":
    main()
