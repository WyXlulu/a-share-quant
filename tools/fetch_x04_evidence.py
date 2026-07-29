from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from tools.fetch_cninfo_qyfp import (
    CHANNELS,
    PAGE_SIZE,
    STATIC_HOST,
    SecurityQuery,
    create_session,
    fetch_stock_list,
    flatten_announcements,
    format_shanghai_minute,
    get_announcement_time_ms,
    get_title,
    hash_file,
    normalize_adjunct_url,
    parse_total_record_num,
    sleep_between_requests,
    warm_up_session,
)


OUTPUT_DIR = Path("data/x04_evidence")
MANIFEST_PATH = OUTPUT_DIR / "x04_manifest.csv"
SUMMARY_PATH = OUTPUT_DIR / "x04_fetch_summary.json"
MIN_PDF_BYTES = 10 * 1024
IMPLEMENTATION_MARKERS = (
    "权益分派实施",
    "分红派息实施",
    "利润分配实施",
)
MANIFEST_FIELDS = (
    "security_id",
    "announcement_title",
    "disclosure_ts",
    "adjunctUrl",
    "pdf_full_url",
    "pdf_filename",
    "pdf_sha256",
    "pdf_bytes",
    "fetched_at",
    "acquisition_method",
)


@dataclass(frozen=True)
class QueryWindow:
    security_id: str
    start_date: str
    end_date: str

    @property
    def date_range(self) -> str:
        return f"{self.start_date}~{self.end_date}"

    @property
    def key(self) -> str:
        return f"{self.security_id}:{self.date_range}"


WINDOWS = (
    QueryWindow("300760", "2025-05-01", "2025-06-30"),
    QueryWindow("300760", "2026-05-01", "2026-06-30"),
    QueryWindow("301308", "2026-05-01", "2026-06-30"),
)

TARGETS = (
    {
        "target_id": "300760/2025-05-29",
        "security_id": "300760",
        "window_key": "300760:2025-05-01~2025-06-30",
        "title_fragments": ("2024年度", "2025年第一次中期", "实施"),
    },
    {
        "target_id": "300760/2026-05-28",
        "security_id": "300760",
        "window_key": "300760:2026-05-01~2026-06-30",
        "title_fragments": ("2025年度", "2026年第一次中期", "实施"),
    },
    {
        "target_id": "301308/2026-06-02",
        "security_id": "301308",
        "window_key": "301308:2026-05-01~2026-06-30",
        "title_fragments": ("2025年度", "2026年第一次中期", "实施"),
    },
)

JIANGBOLONG_DIRECT = {
    "security_id": "301308",
    "announcement_title": "2025年度、2026年第一次中期权益分派实施公告",
    "disclosure_ts": "2026-05-26 00:00",
    "adjunctUrl": "finalpage/2026-05-26/1225331662.PDF",
    "pdf_full_url": (
        "https://static.cninfo.com.cn/finalpage/2026-05-26/1225331662.PDF"
    ),
    "acquisition_method": "direct_link",
}


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    session = create_session()
    warm_up_session(session)
    stock_list = fetch_stock_list(session)
    queries = resolve_queries(stock_list)

    records_by_window: dict[str, dict[str, list[dict[str, Any]]]] = {}
    count_matrix: dict[str, dict[str, int]] = {}
    all_implementation_rows: list[dict[str, Any]] = []
    all_titles_by_window: dict[str, list[dict[str, str]]] = {}

    for window in WINDOWS:
        query = queries[window.security_id]
        channel_records: dict[str, list[dict[str, Any]]] = {}
        count_matrix[window.key] = {}
        for channel, channel_params in CHANNELS.items():
            records = fetch_channel_for_window(
                session,
                query,
                window,
                channel,
                channel_params,
            )
            channel_records[channel] = records
            count_matrix[window.key][channel] = len(records)
        records_by_window[window.key] = channel_records
        all_implementation_rows.extend(
            collect_implementation_titles(window, channel_records)
        )
        all_titles_by_window[window.key] = collect_all_titles(
            window, channel_records["ch3"]
        )

    listing_rows = build_download_rows(records_by_window)
    jiangbolong_target = TARGETS[2]
    jiangbolong_channel_hit = any(
        row["security_id"] == jiangbolong_target["security_id"]
        and all(
            fragment in row["announcement_title"]
            for fragment in jiangbolong_target["title_fragments"]
        )
        for row in listing_rows
    )
    if not jiangbolong_channel_hit:
        listing_rows.append(dict(JIANGBOLONG_DIRECT))

    listing_rows.sort(
        key=lambda row: (
            row["security_id"],
            row["disclosure_ts"],
            row["adjunctUrl"],
        )
    )
    previous_manifest = read_previous_manifest()
    download_pdfs(session, listing_rows, previous_manifest)
    write_manifest(listing_rows)

    target_status = build_target_status(listing_rows)
    missing_target_windows = {
        target["target_id"]: all_titles_by_window[target["window_key"]]
        for target in TARGETS
        if not any(
            target["target_id"] == status["target_id"] and status["landed"]
            for status in target_status
        )
    }
    summary = {
        "status": "complete",
        "requests_version": requests.__version__,
        "count_matrix": count_matrix,
        "implementation_titles": sorted(
            deduplicate_report_rows(all_implementation_rows),
            key=lambda row: (
                row["security_id"],
                row["disclosure_ts"],
                row["adjunctUrl"],
            ),
        ),
        "download_count": len(listing_rows),
        "downloads": [
            {
                field: row[field]
                for field in (
                    "security_id",
                    "announcement_title",
                    "disclosure_ts",
                    "adjunctUrl",
                    "pdf_filename",
                    "pdf_sha256",
                    "pdf_bytes",
                    "acquisition_method",
                )
            }
            for row in listing_rows
        ],
        "target_status": target_status,
        "missing_target_window_titles": missing_target_windows,
        "manifest_path": str(MANIFEST_PATH),
    }
    SUMMARY_PATH.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def resolve_queries(stock_list: list[dict[str, Any]]) -> dict[str, SecurityQuery]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in stock_list:
        code = str(row.get("code") or row.get("stockCode") or "")
        if code:
            by_code[code] = row

    queries: dict[str, SecurityQuery] = {}
    missing: list[str] = []
    for code in sorted({window.security_id for window in WINDOWS}):
        row = by_code.get(code)
        org_id = str((row or {}).get("orgId") or (row or {}).get("orgid") or "")
        if not org_id:
            missing.append(code)
            continue
        queries[code] = SecurityQuery(
            code=code,
            org_id=org_id,
            constructed_org_id="",
            column="szse",
            plate="sz",
        )
    if missing:
        raise RuntimeError(
            "CNINFO stockList missing parsed orgId for: " + ", ".join(missing)
        )
    return queries


def fetch_channel_for_window(
    session: requests.Session,
    query: SecurityQuery,
    window: QueryWindow,
    channel: str,
    channel_params: dict[str, str],
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    page_num = 1
    expected_total: int | None = None
    while expected_total is None or len(flatten_announcements(pages)) < expected_total:
        sleep_between_requests()
        payload = {
            "tabName": "fulltext",
            "pageSize": PAGE_SIZE,
            "pageNum": page_num,
            "column": query.column,
            "plate": query.plate,
            "stock": query.stock_param,
            "searchkey": channel_params["searchkey"],
            "secid": "",
            "category": channel_params["category"],
            "trade": "",
            "seDate": window.date_range,
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        page = post_json_utf8_with_retry(session, payload)
        page_total = parse_total_record_num(query.code, channel, page)
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise RuntimeError(
                f"{window.key} {channel} totalRecordNum changed while paging: "
                f"{expected_total} -> {page_total}"
            )
        pages.append(page)
        page_num += 1

    records = flatten_announcements(pages)
    if len(records) != expected_total:
        raise RuntimeError(
            f"{window.key} {channel} totalRecordNum mismatch: "
            f"total={expected_total}, fetched={len(records)}"
        )
    return records


def post_json_utf8_with_retry(
    session: requests.Session, payload: dict[str, str | int]
) -> dict[str, Any]:
    last_status: int | None = None
    last_body = b""
    for attempt in range(1, 4):
        try:
            response = session.post(
                "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                data=payload,
                timeout=30,
            )
            last_status = response.status_code
            last_body = response.content
            if response.status_code == 200:
                try:
                    data = json.loads(response.content.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
                if isinstance(data, dict) and data:
                    return data
        except requests.RequestException as exc:
            last_status = None
            last_body = repr(exc).encode("utf-8", errors="replace")
        if attempt < 3:
            time.sleep(3)
    raise RuntimeError(
        "CNINFO announcement POST failed after 3 attempts: "
        f"HTTP {last_status}; "
        f"body={last_body[:500].decode('utf-8', errors='replace')}"
    )


def collect_implementation_titles(
    window: QueryWindow,
    channel_records: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for channel, records in channel_records.items():
        for record in records:
            title = get_title(record)
            if "实施" not in title:
                continue
            rows.append(
                {
                    "security_id": window.security_id,
                    "window": window.date_range,
                    "channel": channel,
                    "announcement_title": title,
                    "disclosure_ts": format_shanghai_minute(
                        get_announcement_time_ms(
                            window.security_id, title, record
                        )
                    ),
                    "adjunctUrl": normalize_adjunct_url(record.get("adjunctUrl")),
                }
            )
    return rows


def collect_all_titles(
    window: QueryWindow, records: list[dict[str, Any]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for record in records:
        title = get_title(record)
        rows.append(
            {
                "security_id": window.security_id,
                "window": window.date_range,
                "announcement_title": title,
                "disclosure_ts": format_shanghai_minute(
                    get_announcement_time_ms(window.security_id, title, record)
                ),
                "adjunctUrl": normalize_adjunct_url(record.get("adjunctUrl")),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            row["disclosure_ts"],
            row["announcement_title"],
            row["adjunctUrl"],
        ),
    )


def deduplicate_report_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    channels: dict[tuple[str, str, str], set[str]] = {}
    for row in rows:
        key = (row["security_id"], row["window"], row["adjunctUrl"])
        if key not in by_key:
            by_key[key] = dict(row)
            channels[key] = set()
        channels[key].add(row["channel"])
    for key, row in by_key.items():
        row["channel"] = "|".join(sorted(channels[key]))
    return list(by_key.values())


def build_download_rows(
    records_by_window: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    by_url: dict[tuple[str, str], dict[str, Any]] = {}
    channels_by_url: dict[tuple[str, str], set[str]] = {}
    for window in WINDOWS:
        implementation_urls = {
            normalize_adjunct_url(record.get("adjunctUrl"))
            for records in records_by_window[window.key].values()
            for record in records
            if any(marker in get_title(record) for marker in IMPLEMENTATION_MARKERS)
        }
        for channel, records in records_by_window[window.key].items():
            for record in records:
                title = get_title(record)
                adjunct_url = normalize_adjunct_url(record.get("adjunctUrl"))
                if adjunct_url not in implementation_urls:
                    continue
                if not adjunct_url:
                    raise RuntimeError(
                        f"{window.security_id} implementation notice missing "
                        f"adjunctUrl: {title}"
                    )
                key = (window.security_id, adjunct_url)
                if key not in by_url:
                    announcement_time = get_announcement_time_ms(
                        window.security_id, title, record
                    )
                    by_url[key] = {
                        "security_id": window.security_id,
                        "announcement_title": title,
                        "disclosure_ts": format_shanghai_minute(announcement_time),
                        "adjunctUrl": adjunct_url,
                        "pdf_full_url": f"{STATIC_HOST}/{adjunct_url}",
                        "acquisition_method": "",
                    }
                    channels_by_url[key] = set()
                channels_by_url[key].add(channel)
    for key, row in by_url.items():
        row["acquisition_method"] = (
            "channel:" + "|".join(sorted(channels_by_url[key]))
        )
    return list(by_url.values())


def read_previous_manifest() -> dict[tuple[str, str], dict[str, str]]:
    if not MANIFEST_PATH.exists():
        return {}
    with MANIFEST_PATH.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing_columns = set(MANIFEST_FIELDS) - set(reader.fieldnames or [])
        if missing_columns:
            raise RuntimeError(
                f"Existing manifest missing columns: {sorted(missing_columns)}"
            )
        return {
            (row["security_id"], row["adjunctUrl"]): dict(row)
            for row in reader
        }


def download_pdfs(
    session: requests.Session,
    rows: list[dict[str, Any]],
    previous_manifest: dict[tuple[str, str], dict[str, str]],
) -> None:
    sequence_by_code_date: Counter[tuple[str, str]] = Counter()
    fetched_at_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for row in rows:
        disclosure_date = row["disclosure_ts"][:10]
        sequence_key = (row["security_id"], disclosure_date)
        sequence_by_code_date[sequence_key] += 1
        filename = (
            f"{row['security_id']}_{disclosure_date}_"
            f"{sequence_by_code_date[sequence_key]}.pdf"
        )
        path = OUTPUT_DIR / filename
        previous = previous_manifest.get(
            (row["security_id"], row["adjunctUrl"])
        )
        if path.exists():
            actual_sha256, actual_bytes = hash_file(path)
            if previous is None:
                sleep_between_requests()
                remote = get_pdf_with_retry(session, row["pdf_full_url"])
                expected_sha256 = hashlib.sha256(remote).hexdigest()
                expected_bytes = len(remote)
            else:
                expected_sha256 = previous["pdf_sha256"]
                expected_bytes = int(previous["pdf_bytes"])
            if (
                actual_sha256 != expected_sha256
                or actual_bytes != expected_bytes
            ):
                raise RuntimeError(
                    f"Existing PDF mismatch for {path}: "
                    f"actual=({actual_sha256}, {actual_bytes}), "
                    f"expected=({expected_sha256}, {expected_bytes})"
                )
            ensure_pdf_size(path, actual_bytes)
            row["pdf_filename"] = filename
            row["pdf_sha256"] = actual_sha256
            row["pdf_bytes"] = actual_bytes
            row["fetched_at"] = (
                previous["fetched_at"] if previous else fetched_at_now
            )
            continue

        sleep_between_requests()
        content = get_pdf_with_retry(session, row["pdf_full_url"])
        if len(content) < MIN_PDF_BYTES:
            raise RuntimeError(
                f"Downloaded PDF too small for {row['pdf_full_url']}: "
                f"{len(content)} bytes"
            )
        path.write_bytes(content)
        row["pdf_filename"] = filename
        row["pdf_sha256"] = hashlib.sha256(content).hexdigest()
        row["pdf_bytes"] = len(content)
        row["fetched_at"] = fetched_at_now


def get_pdf_with_retry(session: requests.Session, url: str) -> bytes:
    last_status: int | None = None
    last_body = b""
    last_redirects: list[int] = []
    for attempt in range(1, 4):
        try:
            response = session.get(url, timeout=60, allow_redirects=True)
            last_status = response.status_code
            last_body = response.content
            last_redirects = [item.status_code for item in response.history]
            if response.status_code == 200 and response.content:
                return response.content
        except requests.RequestException as exc:
            last_status = None
            last_body = repr(exc).encode("utf-8", errors="replace")
            last_redirects = []
        if attempt < 3:
            time.sleep(3)
    raise RuntimeError(
        "CNINFO PDF download failed after 3 attempts: "
        f"HTTP {last_status}; redirect_statuses={last_redirects}; "
        f"body={last_body[:500]!r}; url={url}"
    )


def ensure_pdf_size(path: Path, byte_count: int) -> None:
    if byte_count < MIN_PDF_BYTES:
        raise RuntimeError(f"Existing PDF too small for {path}: {byte_count}")


def write_manifest(rows: list[dict[str, Any]]) -> None:
    with MANIFEST_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(
            [{field: row[field] for field in MANIFEST_FIELDS} for row in rows]
        )


def build_target_status(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for target in TARGETS:
        matches = [
            row
            for row in rows
            if row["security_id"] == target["security_id"]
            and all(
                fragment in row["announcement_title"]
                for fragment in target["title_fragments"]
            )
        ]
        result.append(
            {
                "target_id": target["target_id"],
                "landed": bool(matches),
                "matching_pdfs": [
                    {
                        "announcement_title": row["announcement_title"],
                        "pdf_filename": row["pdf_filename"],
                        "adjunctUrl": row["adjunctUrl"],
                        "acquisition_method": row["acquisition_method"],
                    }
                    for row in matches
                ],
            }
        )
    return result


if __name__ == "__main__":
    main()
