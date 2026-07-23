from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


CNINFO_HOST = "https://www.cninfo.com.cn"
STATIC_HOST = "https://static.cninfo.com.cn"
SESSION_WARMUP_URL = (
    "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"
)
STOCK_LIST_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
ANNOUNCEMENT_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
DATE_RANGE = "2018-12-01~2024-03-31"
PAGE_SIZE = 30
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
RAW_DIR = Path("data/golden_slice/cninfo_raw")
PDF_DIR = Path("data/golden_slice/cninfo_pdf")

CHANNELS = {
    "ch1": {"category": "category_qyfpxzcs_szsh", "searchkey": ""},
    "ch2": {"category": "", "searchkey": "权益分派"},
    "ch3": {"category": "", "searchkey": ""},
}

SECURITIES = {
    "600519": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "600276": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "600036": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "600900": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "601398": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "601668": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "601939": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "601318": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "600028": {"column": "sse", "plate": "sh", "constructed_prefix": "gssh0"},
    "000858": {"column": "szse", "plate": "sz", "constructed_prefix": "gssz0"},
    "000333": {"column": "szse", "plate": "sz", "constructed_prefix": "gssz0"},
    "000651": {"column": "szse", "plate": "sz", "constructed_prefix": "gssz0"},
}

BUSINESS_PATTERN = re.compile(r"权益分派|分红|派息|现金红利|送股|转增|配股")
CORRECTION_PATTERN = re.compile(r"更正|补充|订正")
KEYWORD_FAMILIES = [
    "权益分派",
    "分红派息",
    "现金红利",
    "配股",
    "转增",
    "送股",
    "合并",
    "吸收合并",
    "要约",
    "退市",
    "更正",
    "补充",
]


@dataclass(frozen=True)
class SecurityQuery:
    code: str
    org_id: str
    constructed_org_id: str
    column: str
    plate: str

    @property
    def stock_param(self) -> str:
        return f"{self.code},{self.org_id}"


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    session = create_session()
    warm_up_session(session)
    stock_list = fetch_stock_list(session)
    queries, org_mismatches = resolve_security_queries(stock_list)

    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]] = {}
    raw_counts: dict[str, dict[str, int]] = {}
    ch2_empty: dict[str, bool] = {}

    for query in queries:
        all_channel_records[query.code] = {}
        raw_counts[query.code] = {}
        for channel, channel_params in CHANNELS.items():
            pages = read_cached_channel(query.code, channel)
            if pages is None:
                pages = fetch_channel(session, query, channel, channel_params)
                write_raw_response(query.code, channel, pages)
            records = flatten_announcements(pages)
            if channel in {"ch1", "ch3"} and not records:
                raise RuntimeError(
                    f"{query.code} {channel} returned 0 announcements; fail-closed"
                )
            if channel == "ch2":
                ch2_empty[query.code] = not records
            all_channel_records[query.code][channel] = records
            raw_counts[query.code][channel] = len(records)

    listing_rows = build_listing_rows(all_channel_records)
    previous_manifest_hashes = read_previous_manifest_hashes()
    write_all_titles_manifest(all_channel_records)
    download_pdfs(session, listing_rows, previous_manifest_hashes)
    write_listing_manifest(listing_rows)
    summary = build_summary(
        raw_counts,
        ch2_empty,
        listing_rows,
        all_channel_records,
        queries,
        org_mismatches,
    )
    write_summary(summary)
    print(
        json.dumps(
            {
                "status": "complete",
                "listing_manifest_rows": summary["listing_manifest_rows"],
                "pdf_success_count": summary["pdf_success_count"],
                "summary_path": str(RAW_DIR / "fetch_summary.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": CNINFO_HOST,
            "Referer": SESSION_WARMUP_URL,
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


def warm_up_session(session: requests.Session) -> None:
    response = session.get(SESSION_WARMUP_URL, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            "CNINFO session warmup failed: "
            f"HTTP {response.status_code}; body={response.text[:500]}"
        )


def fetch_stock_list(session: requests.Session) -> list[dict[str, Any]]:
    response = session.get(STOCK_LIST_URL, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(
            f"CNINFO stock list failed: HTTP {response.status_code}; "
            f"body={response.text[:500]}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"CNINFO stock list is not valid JSON: body={response.text[:500]}"
        ) from exc
    stock_list = payload.get("stockList")
    if not isinstance(stock_list, list) or not stock_list:
        raise RuntimeError("CNINFO stockList missing or empty")
    return stock_list


def resolve_security_queries(
    stock_list: list[dict[str, Any]],
) -> tuple[list[SecurityQuery], list[dict[str, str]]]:
    by_code: dict[str, dict[str, Any]] = {}
    for row in stock_list:
        code = str(row.get("code") or row.get("stockCode") or "")
        if code:
            by_code[code] = row

    queries: list[SecurityQuery] = []
    mismatches: list[dict[str, str]] = []
    missing: list[str] = []
    for code, metadata in SECURITIES.items():
        row = by_code.get(code)
        if row is None:
            missing.append(code)
            continue
        org_id = str(row.get("orgId") or row.get("orgid") or "")
        if not org_id:
            missing.append(code)
            continue
        constructed_org_id = f"{metadata['constructed_prefix']}{code}"
        if org_id != constructed_org_id:
            mismatches.append(
                {
                    "security_id": code,
                    "parsed_org_id": org_id,
                    "constructed_org_id": constructed_org_id,
                }
            )
        queries.append(
            SecurityQuery(
                code=code,
                org_id=org_id,
                constructed_org_id=constructed_org_id,
                column=metadata["column"],
                plate=metadata["plate"],
            )
        )
    if missing:
        raise RuntimeError(f"CNINFO stockList missing orgId for: {', '.join(missing)}")
    return queries, mismatches


def fetch_channel(
    session: requests.Session,
    query: SecurityQuery,
    channel: str,
    channel_params: dict[str, str],
) -> list[dict[str, Any]]:
    sleep_between_requests()
    first_payload = build_query_payload(query, channel_params, page_num=1)
    first_page = post_json_with_retry(session, first_payload)
    total = parse_total_record_num(query.code, channel, first_page)
    pages = [first_page]
    fetched = len(flatten_announcements([first_page]))
    page_num = 2
    while fetched < total:
        sleep_between_requests()
        page_payload = build_query_payload(query, channel_params, page_num=page_num)
        page = post_json_with_retry(session, page_payload)
        pages.append(page)
        fetched = len(flatten_announcements(pages))
        page_num += 1

    if fetched != total:
        raise RuntimeError(
            f"{query.code} {channel} totalRecordNum mismatch: total={total}, "
            f"fetched={fetched}"
        )
    return pages


def build_query_payload(
    query: SecurityQuery, channel_params: dict[str, str], page_num: int
) -> dict[str, str | int]:
    return {
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
        "seDate": DATE_RANGE,
        "sortName": "",
        "sortType": "",
        "isHLtitle": "true",
    }


def post_json_with_retry(
    session: requests.Session, payload: dict[str, str | int]
) -> dict[str, Any]:
    last_status: int | None = None
    last_body = ""
    for attempt in range(1, 4):
        try:
            response = session.post(ANNOUNCEMENT_QUERY_URL, data=payload, timeout=30)
            last_status = response.status_code
            last_body = response.text
            if response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data:
                    return data
        except requests.RequestException as exc:
            last_status = None
            last_body = repr(exc)
        if attempt < 3:
            time.sleep(3)
    raise RuntimeError(
        "CNINFO announcement POST failed after 3 attempts: "
        f"HTTP {last_status}; body={last_body[:500]}"
    )


def parse_total_record_num(code: str, channel: str, page: dict[str, Any]) -> int:
    raw_total = page.get("totalRecordNum")
    if raw_total is None:
        raise RuntimeError(f"{code} {channel} missing totalRecordNum")
    try:
        return int(raw_total)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{code} {channel} invalid totalRecordNum={raw_total!r}"
        ) from exc


def flatten_announcements(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for page in pages:
        announcements = page.get("announcements")
        if announcements is None:
            announcements = []
        if not isinstance(announcements, list):
            raise RuntimeError("CNINFO announcements field is not a list")
        records.extend(announcements)
    return records


def write_raw_response(code: str, channel: str, pages: list[dict[str, Any]]) -> None:
    path = RAW_DIR / f"{code}_{channel}.json"
    payload = {
        "code": code,
        "channel": channel,
        "responses": pages,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_cached_channel(code: str, channel: str) -> list[dict[str, Any]] | None:
    path = RAW_DIR / f"{code}_{channel}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cached channel response is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Cached channel response is not an object: {path}")
    if payload.get("code") != code or payload.get("channel") != channel:
        raise RuntimeError(f"Cached channel identity mismatch: {path}")
    pages = payload.get("responses")
    if not isinstance(pages, list) or not pages:
        raise RuntimeError(f"Cached channel has no response pages: {path}")
    total = parse_total_record_num(code, channel, pages[0])
    fetched = len(flatten_announcements(pages))
    if fetched != total:
        raise RuntimeError(
            f"Cached {code} {channel} totalRecordNum mismatch: "
            f"total={total}, fetched={fetched}"
        )
    return pages


def build_listing_rows(
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    row_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for code, channel_records in all_channel_records.items():
        for channel in ("ch1", "ch2"):
            for record in channel_records[channel]:
                add_listing_row(row_by_key, code, channel, record)

    anchors_by_code = build_correction_anchors(row_by_key)
    for code, channel_records in all_channel_records.items():
        for record in channel_records["ch3"]:
            title = get_title(record)
            route_a = bool(
                BUSINESS_PATTERN.search(title) and CORRECTION_PATTERN.search(title)
            )
            route_b_anchor = find_route_b_anchor(
                code, title, record, anchors_by_code.get(code, [])
            )
            if route_a or route_b_anchor is not None:
                add_listing_row(
                    row_by_key,
                    code,
                    "ch3_corr",
                    record,
                    correction_routes=[
                        route
                        for route, matched in (
                            ("A", route_a),
                            ("B", route_b_anchor is not None),
                        )
                        if matched
                    ],
                    correction_anchor_adjunct_url=(
                        route_b_anchor["adjunctUrl"] if route_b_anchor else ""
                    ),
                )

    rows = list(row_by_key.values())
    rows.sort(
        key=lambda row: (
            row["security_id"],
            row["announcementTime_epoch_ms"],
            row["adjunctUrl"],
        )
    )
    for row in rows:
        row["channels"] = "|".join(sorted(row.pop("_channels")))
        row["_correction_routes"] = "|".join(sorted(row["_correction_routes"]))
    return rows


def build_correction_anchors(
    row_by_key: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    anchors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in row_by_key.values():
        # A correction notice cannot anchor itself. The anchor is an earlier
        # ch1/ch2 distribution notice for the same security.
        if CORRECTION_PATTERN.search(row["announcementTitle"]):
            continue
        anchors[row["security_id"]].append(row)
    for rows in anchors.values():
        rows.sort(key=lambda row: row["announcementTime_epoch_ms"])
    return anchors


def find_route_b_anchor(
    code: str,
    title: str,
    record: dict[str, Any],
    anchors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not CORRECTION_PATTERN.search(title):
        return None
    correction_dt = epoch_ms_to_shanghai(
        get_announcement_time_ms(code, title, record)
    )
    eligible: list[dict[str, Any]] = []
    for anchor in anchors:
        anchor_dt = epoch_ms_to_shanghai(anchor["announcementTime_epoch_ms"])
        delta = correction_dt - anchor_dt
        if timedelta(0) <= delta <= timedelta(days=15):
            eligible.append(anchor)
    if not eligible:
        return None
    return max(eligible, key=lambda row: row["announcementTime_epoch_ms"])


def add_listing_row(
    row_by_key: dict[tuple[str, str], dict[str, Any]],
    code: str,
    channel: str,
    record: dict[str, Any],
    correction_routes: list[str] | None = None,
    correction_anchor_adjunct_url: str = "",
) -> None:
    adjunct_url = normalize_adjunct_url(record.get("adjunctUrl"))
    if not adjunct_url:
        raise RuntimeError(f"{code} announcement missing adjunctUrl: {record}")
    title = get_title(record)
    announcement_time = get_announcement_time_ms(code, title, record)
    key = (code, adjunct_url)
    if key not in row_by_key:
        row_by_key[key] = {
            "security_id": code,
            "_channels": set(),
            "announcementTitle": title,
            "announcementTime_epoch_ms": announcement_time,
            "disclosure_ts": format_shanghai_minute(announcement_time),
            "adjunctUrl": adjunct_url,
            "pdf_full_url": f"{STATIC_HOST}/{adjunct_url}",
            "pdf_sha256": "",
            "pdf_bytes": "",
            "fetched_at": "",
            "corr_anchor_adjunctUrl": "",
            "_correction_routes": set(),
        }
    row_by_key[key]["_channels"].add(channel)
    if correction_routes:
        row_by_key[key]["_correction_routes"].update(correction_routes)
    if correction_anchor_adjunct_url:
        existing = row_by_key[key]["corr_anchor_adjunctUrl"]
        if existing and existing != correction_anchor_adjunct_url:
            raise RuntimeError(
                f"Conflicting correction anchors for {code} {adjunct_url}: "
                f"{existing} vs {correction_anchor_adjunct_url}"
            )
        row_by_key[key]["corr_anchor_adjunctUrl"] = correction_anchor_adjunct_url


def normalize_adjunct_url(value: Any) -> str:
    if value is None:
        return ""
    raw = str(value).strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.split("static.cninfo.com.cn/", 1)[-1].lstrip("/")
    return raw.lstrip("/")


def get_title(record: dict[str, Any]) -> str:
    return str(record.get("announcementTitle") or "").strip()


def get_announcement_time_ms(
    code: str, title: str, record: dict[str, Any]
) -> int:
    raw_time = record.get("announcementTime")
    if raw_time is None:
        raise RuntimeError(f"{code} announcement missing announcementTime: {title}")
    try:
        return int(raw_time)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"{code} announcement invalid announcementTime={raw_time!r}: {title}"
        ) from exc


def format_shanghai_minute(epoch_ms: int) -> str:
    return epoch_ms_to_shanghai(epoch_ms).strftime("%Y-%m-%d %H:%M")


def epoch_ms_to_shanghai(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).astimezone(
        SHANGHAI_TZ
    )


def write_all_titles_manifest(
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
) -> None:
    path = RAW_DIR / "all_titles_manifest.csv"
    rows: list[dict[str, Any]] = []
    for code, channel_records in all_channel_records.items():
        for record in channel_records["ch3"]:
            rows.append(
                {
                    "security_id": code,
                    "announcementTitle": get_title(record),
                    "disclosure_ts": format_shanghai_minute(
                        get_announcement_time_ms(code, get_title(record), record)
                    ),
                    "adjunctUrl": normalize_adjunct_url(record.get("adjunctUrl")),
                }
            )
    rows.sort(key=lambda row: (row["security_id"], row["disclosure_ts"], row["adjunctUrl"]))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "security_id",
                "announcementTitle",
                "disclosure_ts",
                "adjunctUrl",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def read_previous_manifest_hashes() -> dict[tuple[str, str], dict[str, str]]:
    path = RAW_DIR / "listing_manifest.csv"
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return {
            (row["security_id"], row["adjunctUrl"]): {
                "pdf_sha256": row.get("pdf_sha256", ""),
                "pdf_bytes": row.get("pdf_bytes", ""),
                "fetched_at": row.get("fetched_at", ""),
            }
            for row in reader
        }


def download_pdfs(
    session: requests.Session,
    listing_rows: list[dict[str, Any]],
    previous_manifest_hashes: dict[tuple[str, str], dict[str, str]],
) -> None:
    sequence_by_code_date: Counter[tuple[str, str]] = Counter()
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for row in listing_rows:
        disclosure_date = row["disclosure_ts"][:10]
        key = (row["security_id"], disclosure_date)
        sequence_by_code_date[key] += 1
        pdf_path = (
            PDF_DIR
            / f"{row['security_id']}_{disclosure_date}_{sequence_by_code_date[key]}.pdf"
        )
        previous = previous_manifest_hashes.get((row["security_id"], row["adjunctUrl"]))
        if pdf_path.exists():
            sha256, byte_count = hash_file(pdf_path)
            if previous:
                expected_sha256 = previous["pdf_sha256"]
                expected_bytes = previous["pdf_bytes"]
                if sha256 != expected_sha256 or str(byte_count) != str(expected_bytes):
                    raise RuntimeError(
                        f"Existing PDF hash mismatch for {pdf_path}: "
                        f"actual=({sha256}, {byte_count}) "
                        f"expected=({expected_sha256}, {expected_bytes})"
                    )
            else:
                sleep_between_requests()
                remote_content = get_pdf_with_retry(session, row["pdf_full_url"])
                remote_sha256 = hashlib.sha256(remote_content).hexdigest()
                if sha256 != remote_sha256 or byte_count != len(remote_content):
                    raise RuntimeError(
                        f"Existing PDF cannot be verified for {pdf_path}: "
                        f"local=({sha256}, {byte_count}) "
                        f"remote=({remote_sha256}, {len(remote_content)})"
                    )
            ensure_pdf_size(pdf_path, byte_count)
            row["pdf_sha256"] = sha256
            row["pdf_bytes"] = str(byte_count)
            row["fetched_at"] = previous.get("fetched_at") if previous else fetched_at
            continue

        sleep_between_requests()
        content = get_pdf_with_retry(session, row["pdf_full_url"])
        if len(content) < 10 * 1024:
            raise RuntimeError(
                f"Downloaded PDF too small for {row['pdf_full_url']}: {len(content)} bytes"
            )
        pdf_path.write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        row["pdf_sha256"] = sha256
        row["pdf_bytes"] = str(len(content))
        row["fetched_at"] = fetched_at


def get_pdf_with_retry(session: requests.Session, url: str) -> bytes:
    last_status: int | None = None
    last_body = b""
    for attempt in range(1, 4):
        try:
            response = session.get(url, timeout=60)
            last_status = response.status_code
            last_body = response.content
            if response.status_code == 200 and response.content:
                return response.content
        except requests.RequestException as exc:
            last_status = None
            last_body = repr(exc).encode("utf-8", errors="replace")
        if attempt < 3:
            time.sleep(3)
    raise RuntimeError(
        "CNINFO PDF download failed after 3 attempts: "
        f"HTTP {last_status}; body={last_body[:500]!r}; url={url}"
    )


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), byte_count


def ensure_pdf_size(path: Path, byte_count: int) -> None:
    if byte_count < 10 * 1024:
        raise RuntimeError(f"Existing PDF too small for {path}: {byte_count} bytes")


def write_listing_manifest(listing_rows: list[dict[str, Any]]) -> None:
    path = RAW_DIR / "listing_manifest.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "security_id",
                "channels",
                "announcementTitle",
                "announcementTime_epoch_ms",
                "disclosure_ts",
                "adjunctUrl",
                "pdf_full_url",
                "pdf_sha256",
                "pdf_bytes",
                "fetched_at",
                "corr_anchor_adjunctUrl",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in listing_rows
            ]
        )


def build_summary(
    raw_counts: dict[str, dict[str, int]],
    ch2_empty: dict[str, bool],
    listing_rows: list[dict[str, Any]],
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
    queries: list[SecurityQuery],
    org_mismatches: list[dict[str, str]],
) -> dict[str, Any]:
    ch_diffs = build_channel_diffs(all_channel_records)
    implementation_rows = [
        row for row in listing_rows if "权益分派实施公告" in row["announcementTitle"]
    ]
    non_implementation_rows = [
        row for row in listing_rows if "权益分派实施公告" not in row["announcementTitle"]
    ]
    after_1500_rows = [
        row
        for row in listing_rows
        if is_after_shanghai_1500(row["announcementTime_epoch_ms"])
    ]
    keyword_hits = build_keyword_hits(all_channel_records)
    corrections = build_corrections_report(listing_rows, all_channel_records)
    pdf_count = len(listing_rows)
    small_pdfs = [row for row in listing_rows if int(row["pdf_bytes"]) <= 10 * 1024]
    return {
        "requests_version": requests.__version__,
        "raw_counts": raw_counts,
        "ch2_empty": ch2_empty,
        "ch1_ch2_diffs": ch_diffs,
        "listing_manifest_rows": len(listing_rows),
        "implementation_title_count": len(implementation_rows),
        "non_implementation_title_count": len(non_implementation_rows),
        "non_implementation_titles": [
            row_brief(row) for row in non_implementation_rows
        ],
        "after_1500_count": len(after_1500_rows),
        "after_1500_ratio": (
            round(len(after_1500_rows) / len(listing_rows), 6)
            if listing_rows
            else None
        ),
        "601318_rows": [
            row_brief(row)
            for row in listing_rows
            if row["security_id"] == "601318"
        ],
        "keyword_hits": keyword_hits,
        "ch3_corrections": corrections,
        "org_id_comparison": [
            {
                "security_id": query.code,
                "parsed_org_id": query.org_id,
                "constructed_org_id": query.constructed_org_id,
                "matches": query.org_id == query.constructed_org_id,
            }
            for query in queries
        ],
        "org_id_mismatches": org_mismatches,
        "listing_count_by_security": dict(
            sorted(Counter(row["security_id"] for row in listing_rows).items())
        ),
        "pdf_success_count": pdf_count,
        "pdf_all_size_gt_10kb": not small_pdfs,
        "pdf_small_files": [row_brief(row) for row in small_pdfs],
    }


def build_channel_diffs(
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for code, channel_records in all_channel_records.items():
        ch1_by_url = {
            normalize_adjunct_url(record.get("adjunctUrl")): get_title(record)
            for record in channel_records["ch1"]
        }
        ch2_by_url = {
            normalize_adjunct_url(record.get("adjunctUrl")): get_title(record)
            for record in channel_records["ch2"]
        }
        only_ch1 = [
            ch1_by_url[url] for url in sorted(set(ch1_by_url) - set(ch2_by_url))
        ]
        only_ch2 = [
            ch2_by_url[url] for url in sorted(set(ch2_by_url) - set(ch1_by_url))
        ]
        result[code] = {"only_ch1": only_ch1, "only_ch2": only_ch2}
    return result


def build_keyword_hits(
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, list[dict[str, str]]]:
    hits: dict[str, list[dict[str, str]]] = {keyword: [] for keyword in KEYWORD_FAMILIES}
    for code, channel_records in all_channel_records.items():
        for record in channel_records["ch3"]:
            title = get_title(record)
            disclosure_ts = format_shanghai_minute(
                get_announcement_time_ms(code, title, record)
            )
            for keyword in KEYWORD_FAMILIES:
                if keyword in title:
                    hits[keyword].append(
                        {
                            "security_id": code,
                            "announcementTitle": title,
                            "disclosure_ts": disclosure_ts,
                        }
                    )
    for rows in hits.values():
        rows.sort(
            key=lambda row: (
                row["security_id"],
                row["disclosure_ts"],
                row["announcementTitle"],
            )
        )
    return hits


def build_corrections_report(
    listing_rows: list[dict[str, Any]],
    all_channel_records: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, str]]:
    corrections: list[dict[str, str]] = []
    listing_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in listing_rows:
        listing_by_code[row["security_id"]].append(row)
    for row in listing_rows:
        if "ch3_corr" not in row["channels"]:
            continue
        nearby = nearby_equity_titles(
            row["disclosure_ts"],
            [
                candidate
                for candidate in listing_by_code[row["security_id"]]
                if "ch3_corr" not in candidate["channels"]
            ],
        )
        corrections.append(
            {
                "security_id": row["security_id"],
                "announcementTitle": row["announcementTitle"],
                "disclosure_ts": row["disclosure_ts"],
                "matched_routes": row["_correction_routes"],
                "corr_anchor_adjunctUrl": row["corr_anchor_adjunctUrl"],
                "matched_business_terms": "|".join(
                    sorted(set(BUSINESS_PATTERN.findall(row["announcementTitle"])))
                ),
                "possible_revised_announcements": " || ".join(nearby),
            }
        )
    corrections.sort(
        key=lambda row: (
            row["security_id"],
            row["disclosure_ts"],
            row["announcementTitle"],
        )
    )
    return corrections


def nearby_equity_titles(
    disclosure_ts: str, equity_rows: list[dict[str, Any]], limit: int = 3
) -> list[str]:
    disclosure_dt = datetime.strptime(disclosure_ts, "%Y-%m-%d %H:%M").replace(
        tzinfo=SHANGHAI_TZ
    )
    scored: list[tuple[float, str]] = []
    for row in equity_rows:
        row_dt = datetime.strptime(row["disclosure_ts"], "%Y-%m-%d %H:%M").replace(
            tzinfo=SHANGHAI_TZ
        )
        distance = abs((disclosure_dt - row_dt).total_seconds())
        scored.append((distance, f"{row['disclosure_ts']} {row['announcementTitle']}"))
    return [title for _, title in sorted(scored)[:limit]]


def row_brief(row: dict[str, Any]) -> dict[str, str]:
    return {
        "security_id": str(row["security_id"]),
        "announcementTitle": str(row["announcementTitle"]),
        "disclosure_ts": str(row["disclosure_ts"]),
        "url": str(row["pdf_full_url"]),
    }


def is_after_shanghai_1500(epoch_ms: int) -> bool:
    local_dt = epoch_ms_to_shanghai(epoch_ms)
    return (local_dt.hour, local_dt.minute, local_dt.second, local_dt.microsecond) > (
        15,
        0,
        0,
        0,
    )


def write_summary(summary: dict[str, Any]) -> None:
    path = RAW_DIR / "fetch_summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def sleep_between_requests() -> None:
    time.sleep(random.uniform(1.0, 2.0))


if __name__ == "__main__":
    main()
