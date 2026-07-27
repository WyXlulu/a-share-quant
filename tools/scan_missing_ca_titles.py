from __future__ import annotations

import argparse
import csv
import hashlib
import random
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests


RAW_DIR = Path("data/golden_slice/cninfo_raw")
PDF_DIR = Path("data/golden_slice/cninfo_pdf")
ALL_TITLES_PATH = RAW_DIR / "all_titles_manifest.csv"
LISTING_PATH = RAW_DIR / "listing_manifest.csv"
STATIC_HOST = "https://static.cninfo.com.cn"
SESSION_WARMUP_URL = (
    "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"
)
HISTORICAL_DATE_RANGE = "2018-09-01~2018-12-01"
HISTORICAL_RAW_DIR = Path(
    "data/golden_slice/cninfo_raw_2018-09-01_2018-12-01"
)
MIN_PDF_BYTES = 10 * 1024

SECURITY_IDS = (
    "000333",
    "000651",
    "000858",
    "600028",
    "600036",
    "600276",
    "600519",
    "600900",
    "601318",
    "601398",
    "601668",
    "601939",
)
BUSINESS_TERMS = (
    "分红",
    "派息",
    "股息",
    "分派",
    "利润分配",
    "权益分派",
    "现金分红",
    "红利",
    "除权",
    "除息",
    "送股",
    "转增",
    "配股",
    "缩股",
)
LISTING_COLUMNS = (
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
)


@dataclass(frozen=True)
class MissingAnnouncement:
    security_id: str
    announcement_title: str
    disclosure_ts: str
    adjunct_url: str
    title_type: str
    pdf_filename: str


def main() -> None:
    args = parse_args()
    if args.fetch_historical:
        all_titles_path = fetch_historical_all_titles()
    else:
        all_titles_path = ALL_TITLES_PATH
    all_titles = read_csv(all_titles_path)
    listing_rows = read_csv(LISTING_PATH)
    validate_columns(all_titles, listing_rows)

    candidates, counts = scan_candidates(all_titles)
    missing_rows = missing_candidates(candidates, listing_rows)
    pending = assign_pdf_filenames(missing_rows, listing_rows)

    print_scan_report(counts, candidates, pending)
    if args.scan_only:
        return

    validate_existing_listing_pdfs(listing_rows)
    session = create_session()
    warm_up_session(session)
    downloaded_rows = download_pending_pdfs(session, pending)
    append_listing_rows(downloaded_rows)
    verify_append(downloaded_rows)
    print(
        f"completed: appended={len(downloaded_rows)}, "
        f"listing_total={len(listing_rows) + len(downloaded_rows)}, "
        f"pdf_total={len(list(PDF_DIR.glob('*.pdf')))}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scan the local CNINFO all-title manifest for missing corporate-action "
            "announcements and download their original PDFs."
        )
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Print the offline title scan and difference without network access.",
    )
    parser.add_argument(
        "--fetch-historical",
        action="store_true",
        help=(
            "Fetch the three CNINFO channels for 2018-09-01 through "
            "2018-12-01 into an isolated raw directory, then scan and download "
            "the business-title difference."
        ),
    )
    return parser.parse_args()


def fetch_historical_all_titles() -> Path:
    import fetch_cninfo_qyfp as fetch

    HISTORICAL_RAW_DIR.mkdir(parents=True, exist_ok=True)
    fetch.DATE_RANGE = HISTORICAL_DATE_RANGE
    fetch.RAW_DIR = HISTORICAL_RAW_DIR

    session = fetch.create_session()
    fetch.warm_up_session(session)
    stock_list = fetch.fetch_stock_list(session)
    queries, _ = fetch.resolve_security_queries(stock_list)
    all_channel_records: dict[
        str, dict[str, list[dict[str, object]]]
    ] = {}
    print(f"historical_date_range={HISTORICAL_DATE_RANGE}")
    print("historical_channel_counts:")
    ch1_empty_short_window: list[str] = []
    ch1_nonempty_short_window: list[str] = []
    for query in queries:
        all_channel_records[query.code] = {}
        counts: dict[str, int] = {}
        for channel, channel_params in fetch.CHANNELS.items():
            pages = fetch.read_cached_channel(query.code, channel)
            if pages is None:
                pages = fetch.fetch_channel(
                    session,
                    query,
                    channel,
                    channel_params,
                )
                fetch.write_raw_response(query.code, channel, pages)
            records = fetch.flatten_announcements(pages)
            all_channel_records[query.code][channel] = records
            counts[channel] = len(records)
        if counts["ch3"] == 0:
            raise RuntimeError(
                f"{query.code} ch3 returned 0 announcements; "
                "historical short-window fetch is fail-closed"
            )
        if counts["ch1"] == 0:
            ch1_empty_short_window.append(query.code)
        else:
            ch1_nonempty_short_window.append(query.code)
        print(
            f"  {query.code}: ch1={counts['ch1']}, "
            f"ch2={counts['ch2']}, ch3={counts['ch3']}"
        )
    print(
        "ch1_empty_short_window="
        + (",".join(ch1_empty_short_window) or "NONE")
    )
    print(
        "ch1_nonempty_short_window_focus="
        + (",".join(ch1_nonempty_short_window) or "NONE")
    )
    fetch.write_all_titles_manifest(all_channel_records)
    return HISTORICAL_RAW_DIR / "all_titles_manifest.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"required manifest not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise RuntimeError(f"required manifest is empty: {path}")
    return rows


def validate_columns(
    all_titles: list[dict[str, str]],
    listing_rows: list[dict[str, str]],
) -> None:
    required_all_titles = {
        "security_id",
        "announcementTitle",
        "disclosure_ts",
        "adjunctUrl",
    }
    missing_all_titles = required_all_titles - set(all_titles[0])
    if missing_all_titles:
        raise RuntimeError(
            f"all-title manifest missing columns: {sorted(missing_all_titles)}"
        )
    if tuple(listing_rows[0]) != LISTING_COLUMNS:
        raise RuntimeError(
            "listing manifest columns changed: "
            f"expected={LISTING_COLUMNS}, actual={tuple(listing_rows[0])}"
        )


def scan_candidates(
    all_titles: list[dict[str, str]],
) -> tuple[list[dict[str, str]], Counter[str]]:
    seen_urls: dict[str, dict[str, str]] = {}
    candidates: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    present_security_ids = {
        normalize_security_id(row["security_id"]) for row in all_titles
    }
    if present_security_ids != set(SECURITY_IDS):
        raise RuntimeError(
            "all-title manifest security universe changed: "
            f"expected={sorted(SECURITY_IDS)}, actual={sorted(present_security_ids)}"
        )

    for row in all_titles:
        title = row["announcementTitle"]
        if not any(term in title for term in BUSINESS_TERMS):
            continue
        security_id = normalize_security_id(row["security_id"])
        adjunct_url = normalize_adjunct_url(row["adjunctUrl"])
        normalized = {
            "security_id": security_id,
            "announcementTitle": title,
            "disclosure_ts": normalize_disclosure_ts(row["disclosure_ts"]),
            "adjunctUrl": adjunct_url,
        }
        previous = seen_urls.get(adjunct_url)
        if previous is not None:
            if previous != normalized:
                raise RuntimeError(
                    "same adjunctUrl has conflicting all-title metadata: "
                    f"url={adjunct_url}, first={previous}, second={normalized}"
                )
            raise RuntimeError(f"duplicate adjunctUrl in all-title manifest: {adjunct_url}")
        seen_urls[adjunct_url] = normalized
        candidates.append(normalized)
        counts[security_id] += 1
    return candidates, counts


def missing_candidates(
    candidates: list[dict[str, str]],
    listing_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    listing_by_url: dict[str, dict[str, str]] = {}
    for row in listing_rows:
        adjunct_url = normalize_adjunct_url(row["adjunctUrl"])
        if adjunct_url in listing_by_url:
            raise RuntimeError(f"duplicate adjunctUrl in listing manifest: {adjunct_url}")
        listing_by_url[adjunct_url] = row
    return [
        row for row in candidates if row["adjunctUrl"] not in listing_by_url
    ]


def assign_pdf_filenames(
    missing_rows: list[dict[str, str]],
    listing_rows: list[dict[str, str]],
) -> list[MissingAnnouncement]:
    sequence_by_security_date: Counter[tuple[str, str]] = Counter()
    for row in listing_rows:
        security_id = normalize_security_id(row["security_id"])
        disclosure_date = normalize_disclosure_ts(row["disclosure_ts"])[:10]
        sequence_by_security_date[(security_id, disclosure_date)] += 1

    pending: list[MissingAnnouncement] = []
    for row in missing_rows:
        security_id = row["security_id"]
        disclosure_ts = row["disclosure_ts"]
        disclosure_date = disclosure_ts[:10]
        key = (security_id, disclosure_date)
        sequence_by_security_date[key] += 1
        pending.append(
            MissingAnnouncement(
                security_id=security_id,
                announcement_title=row["announcementTitle"],
                disclosure_ts=disclosure_ts,
                adjunct_url=row["adjunctUrl"],
                title_type=classify_title(row["announcementTitle"]),
                pdf_filename=(
                    f"{security_id}_{disclosure_date}_"
                    f"{sequence_by_security_date[key]}.pdf"
                ),
            )
        )
    return pending


def classify_title(title: str) -> str:
    if any(
        marker in title
        for marker in (
            "实施公告",
            "实施的公告",
            "股息派发公告",
            "支付H股股东",
        )
    ):
        return "实施类"
    if any(
        marker in title
        for marker in (
            "利润分配方案",
            "利润分配预案",
            "利润分配的公告",
            "分派方案",
            "分红方案",
            "分红回报规划",
            "现金分红计划",
        )
    ):
        return "方案预案类"
    return "其他"


def print_scan_report(
    counts: Counter[str],
    candidates: list[dict[str, str]],
    pending: list[MissingAnnouncement],
) -> None:
    print("candidate_counts:")
    for security_id in SECURITY_IDS:
        print(f"  {security_id}: {counts[security_id]}")
    print(f"candidate_total={len(candidates)}")
    print(f"missing_total={len(pending)}")
    print("missing_rows:")
    current_security_id = ""
    for row in pending:
        if row.security_id != current_security_id:
            current_security_id = row.security_id
            print(f"[{current_security_id}]")
        print(
            f"  {row.title_type} | {row.disclosure_ts[:10]} | "
            f"{row.announcement_title} | {row.adjunct_url}"
        )


def validate_existing_listing_pdfs(
    listing_rows: list[dict[str, str]],
) -> None:
    sequence_by_security_date: Counter[tuple[str, str]] = Counter()
    for row in listing_rows:
        security_id = normalize_security_id(row["security_id"])
        disclosure_date = normalize_disclosure_ts(row["disclosure_ts"])[:10]
        key = (security_id, disclosure_date)
        sequence_by_security_date[key] += 1
        pdf_path = (
            PDF_DIR
            / f"{security_id}_{disclosure_date}_{sequence_by_security_date[key]}.pdf"
        )
        if not pdf_path.exists():
            raise RuntimeError(f"existing listing PDF is missing: {pdf_path}")
        sha256, byte_count = hash_file(pdf_path)
        if sha256 != row["pdf_sha256"] or str(byte_count) != row["pdf_bytes"]:
            raise RuntimeError(
                f"existing listing PDF differs from manifest: {pdf_path}; "
                f"actual=({sha256},{byte_count}), "
                f"expected=({row['pdf_sha256']},{row['pdf_bytes']})"
            )
        ensure_pdf_bytes(pdf_path.name, byte_count)


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": SESSION_WARMUP_URL,
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


def download_pending_pdfs(
    session: requests.Session,
    pending: list[MissingAnnouncement],
) -> list[dict[str, str]]:
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    completed: list[dict[str, str]] = []
    for index, announcement in enumerate(pending, start=1):
        pdf_url = (
            f"{STATIC_HOST}/{announcement.adjunct_url.lstrip('/')}"
        )
        time.sleep(random.uniform(1.0, 2.0))
        content = get_pdf_with_retry(session, pdf_url)
        ensure_pdf_bytes(pdf_url, len(content))
        if not content.startswith(b"%PDF"):
            raise RuntimeError(
                f"download is not a PDF document: url={pdf_url}, "
                f"prefix={content[:32]!r}"
            )

        target = PDF_DIR / announcement.pdf_filename
        remote_sha256 = hashlib.sha256(content).hexdigest()
        if target.exists():
            local_sha256, local_bytes = hash_file(target)
            if local_sha256 != remote_sha256 or local_bytes != len(content):
                raise RuntimeError(
                    f"existing pending PDF differs from CNINFO; refusing overwrite: "
                    f"{target}; local=({local_sha256},{local_bytes}), "
                    f"remote=({remote_sha256},{len(content)})"
                )
        else:
            temp_path = target.with_suffix(".pdf.part")
            if temp_path.exists():
                raise RuntimeError(f"stale partial download requires review: {temp_path}")
            temp_path.write_bytes(content)
            temp_sha256, temp_bytes = hash_file(temp_path)
            if temp_sha256 != remote_sha256 or temp_bytes != len(content):
                raise RuntimeError(
                    f"temporary PDF verification failed: {temp_path}"
                )
            temp_path.replace(target)

        completed.append(
            {
                "security_id": announcement.security_id,
                "channels": "ch3_scan",
                "announcementTitle": announcement.announcement_title,
                # The all-title manifest has no source epoch field. It is left
                # empty rather than reconstructed from a date-level timestamp.
                "announcementTime_epoch_ms": "",
                "disclosure_ts": announcement.disclosure_ts,
                "adjunctUrl": announcement.adjunct_url,
                "pdf_full_url": pdf_url,
                "pdf_sha256": remote_sha256,
                "pdf_bytes": str(len(content)),
                "fetched_at": fetched_at,
                "corr_anchor_adjunctUrl": "",
            }
        )
        print(
            f"pdf {index}/{len(pending)}: {announcement.pdf_filename} "
            f"{len(content)} bytes {remote_sha256}",
            flush=True,
        )
    return completed


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


def append_listing_rows(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    original_bytes = LISTING_PATH.read_bytes()
    if not original_bytes.endswith((b"\n", b"\r")):
        raise RuntimeError("listing manifest does not end with a newline")
    with LISTING_PATH.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=LISTING_COLUMNS,
            lineterminator="\n",
        )
        writer.writerows(rows)
    updated_bytes = LISTING_PATH.read_bytes()
    if not updated_bytes.startswith(original_bytes):
        raise RuntimeError("existing listing manifest bytes changed during append")


def verify_append(appended_rows: list[dict[str, str]]) -> None:
    rows = read_csv(LISTING_PATH)
    urls = Counter(normalize_adjunct_url(row["adjunctUrl"]) for row in rows)
    for appended in appended_rows:
        adjunct_url = normalize_adjunct_url(appended["adjunctUrl"])
        if urls[adjunct_url] != 1:
            raise RuntimeError(
                f"appended listing URL count is not one: {adjunct_url}"
            )
        target = expected_pdf_path_for_url(rows, adjunct_url)
        sha256, byte_count = hash_file(target)
        if (
            sha256 != appended["pdf_sha256"]
            or str(byte_count) != appended["pdf_bytes"]
        ):
            raise RuntimeError(f"appended PDF verification failed: {target}")


def expected_pdf_path_for_url(
    listing_rows: list[dict[str, str]],
    target_url: str,
) -> Path:
    sequence_by_security_date: Counter[tuple[str, str]] = Counter()
    for row in listing_rows:
        security_id = normalize_security_id(row["security_id"])
        disclosure_date = normalize_disclosure_ts(row["disclosure_ts"])[:10]
        key = (security_id, disclosure_date)
        sequence_by_security_date[key] += 1
        if normalize_adjunct_url(row["adjunctUrl"]) == target_url:
            return (
                PDF_DIR
                / f"{security_id}_{disclosure_date}_"
                f"{sequence_by_security_date[key]}.pdf"
            )
    raise RuntimeError(f"appended URL not found in listing manifest: {target_url}")


def normalize_security_id(value: str) -> str:
    normalized = value.strip().zfill(6)
    if normalized not in SECURITY_IDS:
        raise RuntimeError(f"unexpected security_id: {value!r}")
    return normalized


def normalize_adjunct_url(value: str) -> str:
    normalized = value.strip()
    if "static.cninfo.com.cn/" in normalized:
        normalized = normalized.split("static.cninfo.com.cn/", 1)[-1]
    normalized = normalized.lstrip("/")
    if not normalized:
        raise RuntimeError("empty adjunctUrl")
    return normalized


def normalize_disclosure_ts(value: str) -> str:
    raw = value.strip()
    formats: Iterable[str] = (
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    )
    for date_format in formats:
        try:
            parsed = datetime.strptime(raw, date_format)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    raise RuntimeError(f"unsupported disclosure_ts format: {value!r}")


def hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            byte_count += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), byte_count


def ensure_pdf_bytes(label: str, byte_count: int) -> None:
    if byte_count < MIN_PDF_BYTES:
        raise RuntimeError(
            f"PDF is smaller than {MIN_PDF_BYTES} bytes: "
            f"{label}; actual={byte_count}"
        )


if __name__ == "__main__":
    main()
