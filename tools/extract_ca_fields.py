from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from pypdf import PdfReader


MANIFEST_PATH = Path("data/golden_slice/cninfo_raw/listing_manifest.csv")
PDF_DIR = Path("data/golden_slice/cninfo_pdf")
OUTPUT_PATH = Path("data/golden_slice/ca_extraction_for_review.csv")
IMPLEMENTATION_MARKER = "实施公告"
REQUIRED_CORRECTION_TITLE = "关于2020年度利润分配实施公告的更正公告"
OUTPUT_COLUMNS = (
    "security_id",
    "pdf_filename",
    "announcement_title",
    "cash_dividend_raw",
    "cash_dividend_quote",
    "ex_date",
    "ex_date_quote",
    "record_date",
    "record_date_quote",
    "announcement_date",
    "announcement_date_quote",
    "share_ratio",
    "share_ratio_quote",
    "extraction_confidence",
    "notes",
)

NUMERIC_DATE = r"\d{4}\s*(?:年|[./-])\s*\d{1,2}\s*(?:月|[./-])\s*\d{1,2}\s*日?"
CHINESE_NUMERAL_DATE = (
    r"[〇零一二三四五六七八九]{4}\s*年\s*"
    r"[〇零一二三四五六七八九十]{1,3}\s*月\s*"
    r"[〇零一二三四五六七八九十]{1,3}\s*日"
)
DATE_VALUE = rf"(?:{NUMERIC_DATE}|{CHINESE_NUMERAL_DATE})"
CASH_VALUE_PATTERNS = (
    re.compile(
        r"(?P<value>(?:[AＡ]\s*股\s*)?每\s*股"
        r"(?:(?!每\s*股|每\s*10\s*股|元|。|；).){0,60}?"
        r"\d+(?:\.\d+)?\s*元)"
        r"(?P<tax_context>\s*[（(]\s*含\s*税(?:\s*[）)])?)"
    ),
    re.compile(
        r"(?P<value>每\s*10\s*股"
        r"(?:(?!每\s*股|每\s*10\s*股|元|。|；).){0,70}?"
        r"\d+(?:\.\d+)?\s*元(?:人民币)?(?:现金)?)"
        r"(?P<tax_context>\s*[（(]\s*含\s*税(?:\s*[）)])?)"
    ),
)
EXPLICIT_RECORD_DATE_PATTERNS = (
    re.compile(
        rf"股权登记日(?:为|是)?\s*(?:：|:)?\s*(?P<date>{DATE_VALUE})"
    ),
)
EXPLICIT_EX_DATE_PATTERNS = (
    re.compile(
        rf"除权\s*(?:（\s*息\s*）|\(\s*息\s*\)|除息)?\s*日"
        rf"(?:为|是)?\s*(?:：|:)?\s*(?P<date>{DATE_VALUE})"
    ),
    re.compile(
        rf"除息日(?:为|是)?\s*(?:：|:)?\s*(?P<date>{DATE_VALUE})"
    ),
    re.compile(
        rf"除权除息日(?:为|是)?\s*(?:：|:)?\s*(?P<date>{DATE_VALUE})"
    ),
)
RELATED_DATE_TABLE_PATTERN = re.compile(
    rf"股权登记日\s+最后交易日\s+除权\s*(?:（\s*息\s*）|\(\s*息\s*\))\s*日"
    rf"(?:(?!一、|二、|三、).){{0,100}}?"
    rf"[AＡ]\s*股\s+(?P<record>{DATE_VALUE})\s+(?:-|－|—|–)\s+"
    rf"(?P<ex>{DATE_VALUE})"
)
ANNOUNCEMENT_DATE_PATTERN = re.compile(
    rf"(?:董事会|监事会)\s*(?P<date>{DATE_VALUE})"
)
NONZERO_SHARE_PATTERNS = (
    re.compile(r"每\s*10\s*股\s*(?:派\s*)?送(?:红)?股\s*\d+(?:\.\d+)?\s*股"),
    re.compile(r"每\s*10\s*股\s*转\s*增\s*\d+(?:\.\d+)?\s*股"),
    re.compile(r"每\s*股\s*(?:派\s*)?送(?:红)?股\s*\d+(?:\.\d+)?\s*股"),
    re.compile(r"每\s*股\s*转\s*增\s*\d+(?:\.\d+)?\s*股"),
)
NO_SHARE_COMBINED_PATTERNS = (
    re.compile(
        r"不\s*(?:派\s*)?送\s*红\s*股\s*[,，、;；]?\s*(?:也\s*)?"
        r"不\s*(?:以|进行)?\s*(?:资本\s*)?公\s*积\s*金\s*转\s*增\s*股\s*本"
    ),
    re.compile(
        r"不\s*(?:派\s*)?送\s*股\s*[,，、;；]?\s*(?:也\s*)?"
        r"不\s*转\s*增"
    ),
    re.compile(
        r"仅\s*进行\s*现金\s*红利\s*分配\s*[,，、;；]?\s*"
        r"无\s*资本\s*公\s*积\s*金\s*转\s*增\s*股\s*本等其他形式的分配方案"
    ),
)
NO_TRANSFER_ONLY_PATTERNS = (
    re.compile(
        r"不\s*以\s*(?:资本\s*)?公\s*积\s*金\s*转\s*增\s*股\s*本"
    ),
    re.compile(
        r"不\s*进行\s*(?:资本\s*)?公\s*积\s*金\s*转\s*增\s*股\s*本"
    ),
)


@dataclass(frozen=True)
class PdfText:
    pages: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    value: str
    quote: str
    page_number: int
    context: str = ""


def main() -> None:
    manifest_rows = read_manifest()
    rows_with_filenames = attach_pdf_filenames(manifest_rows)
    selected = select_implementation_rows(rows_with_filenames)

    output_rows: list[dict[str, str]] = []
    for row in selected:
        pdf_path = PDF_DIR / row["pdf_filename"]
        validate_pdf(pdf_path, row)
        output_rows.append(extract_row(row, extract_pdf_text(pdf_path)))

    write_output(output_rows)
    print_summary(output_rows)


def read_manifest() -> list[dict[str, str]]:
    if not MANIFEST_PATH.exists():
        raise RuntimeError(f"listing manifest not found: {MANIFEST_PATH}")
    with MANIFEST_PATH.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {
        "security_id",
        "announcementTitle",
        "disclosure_ts",
        "adjunctUrl",
        "pdf_sha256",
        "pdf_bytes",
    }
    missing = sorted(required - set(rows[0] if rows else ()))
    if missing:
        raise RuntimeError(f"listing manifest missing columns: {missing}")
    if not rows:
        raise RuntimeError("listing manifest is empty")
    return rows


def attach_pdf_filenames(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    sequence_by_security_date: Counter[tuple[str, str]] = Counter()
    attached: list[dict[str, str]] = []
    for row in rows:
        security_id = row["security_id"].zfill(6)
        disclosure_date = row["disclosure_ts"][:10]
        key = (security_id, disclosure_date)
        sequence_by_security_date[key] += 1
        item = dict(row)
        item["pdf_filename"] = (
            f"{security_id}_{disclosure_date}_{sequence_by_security_date[key]}.pdf"
        )
        attached.append(item)
    return attached


def select_implementation_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    selected = [
        row
        for row in rows
        if IMPLEMENTATION_MARKER in row["announcementTitle"]
        or row["announcementTitle"] == REQUIRED_CORRECTION_TITLE
    ]
    if not any(
        row["announcementTitle"] == REQUIRED_CORRECTION_TITLE for row in selected
    ):
        raise RuntimeError(
            "required 000333 correction notice is missing from selected announcements"
        )
    return selected


def validate_pdf(path: Path, manifest_row: dict[str, str]) -> None:
    if not path.exists():
        raise RuntimeError(f"selected PDF not found: {path}")
    content = path.read_bytes()
    if len(content) < 10 * 1024:
        raise RuntimeError(f"selected PDF is smaller than 10KB: {path}")
    expected_bytes = int(manifest_row["pdf_bytes"])
    if len(content) != expected_bytes:
        raise RuntimeError(
            f"PDF byte count differs from manifest: {path}; "
            f"expected={expected_bytes}, actual={len(content)}"
        )
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != manifest_row["pdf_sha256"]:
        raise RuntimeError(
            f"PDF sha256 differs from manifest: {path}; "
            f"expected={manifest_row['pdf_sha256']}, actual={actual_sha256}"
        )


def extract_pdf_text(path: Path) -> PdfText:
    reader = PdfReader(path)
    pages = tuple(normalize_page_text(page.extract_text() or "") for page in reader.pages)
    if not any(pages):
        raise RuntimeError(f"PDF has no extractable text: {path}")
    return PdfText(pages=pages)


def normalize_page_text(text: str) -> str:
    text = text.replace("\uf06c", " ").replace("\u00a0", " ").replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).strip()


def extract_row(manifest_row: dict[str, str], pdf_text: PdfText) -> dict[str, str]:
    notes: list[str] = []
    field_pages: dict[str, int] = {}

    cash, cash_notes = extract_cash_dividend(pdf_text)
    notes.extend(cash_notes)
    if cash:
        field_pages["cash"] = cash.page_number

    record_date, ex_date, date_notes = extract_action_dates(pdf_text)
    notes.extend(date_notes)
    if record_date:
        field_pages["record_date"] = record_date.page_number
    if ex_date:
        field_pages["ex_date"] = ex_date.page_number

    announcement_date, announcement_notes = extract_announcement_date(pdf_text)
    notes.extend(announcement_notes)
    if announcement_date:
        field_pages["announcement_date"] = announcement_date.page_number

    share_ratio, share_notes, nonzero_share = extract_share_ratio(pdf_text)
    notes.extend(share_notes)
    if share_ratio:
        field_pages["share_ratio"] = share_ratio.page_number

    title = manifest_row["announcementTitle"]
    if "已取消" in title:
        notes.append("公告标题标记已取消,须与同日更新后公告人工并读")
    if "更正公告" in title:
        notes.append("更正公告仅提取其自身明确列示字段,未从被更正公告补全")
    if nonzero_share:
        notes.append("NONZERO_SHARE_TRANSFER_REVIEW_REQUIRED")

    required_candidates = (cash, ex_date, record_date, announcement_date, share_ratio)
    if any(candidate is None for candidate in required_candidates):
        notes.append("至少一个必核字段未从本PDF唯一提取")
    if field_pages:
        notes.append(
            "PDF页码:"
            + ",".join(
                f"{field}=p{page}" for field, page in sorted(field_pages.items())
            )
        )

    confidence = "low" if notes_requiring_review(notes) else "high"
    return {
        "security_id": manifest_row["security_id"].zfill(6),
        "pdf_filename": manifest_row["pdf_filename"],
        "announcement_title": title,
        "cash_dividend_raw": cash.value if cash else "",
        "cash_dividend_quote": cash.quote if cash else "",
        "ex_date": ex_date.value if ex_date else "",
        "ex_date_quote": ex_date.quote if ex_date else "",
        "record_date": record_date.value if record_date else "",
        "record_date_quote": record_date.quote if record_date else "",
        "announcement_date": announcement_date.value if announcement_date else "",
        "announcement_date_quote": announcement_date.quote if announcement_date else "",
        "share_ratio": share_ratio.value if share_ratio else "",
        "share_ratio_quote": share_ratio.quote if share_ratio else "",
        "extraction_confidence": confidence,
        "notes": "; ".join(dict.fromkeys(notes)),
    }


def extract_cash_dividend(pdf_text: PdfText) -> tuple[Candidate | None, list[str]]:
    candidates: list[Candidate] = []
    for page_number, page in enumerate(pdf_text.pages, start=1):
        for pattern in CASH_VALUE_PATTERNS:
            for match in pattern.finditer(page):
                value = clean_fragment(match.group("value"))
                quote = quote_around(page, match.start(), match.end())
                context = "multiple_tax_values" if "扣税后" in quote else ""
                candidates.append(
                    Candidate(
                        value=value,
                        quote=quote,
                        page_number=page_number,
                        context=context,
                    )
                )

    unique = deduplicate_candidates(candidates)
    if not unique:
        return None, ["未找到明确带含税标识的每股或每10股现金分红原句"]

    selected = select_cash_candidate(unique)
    notes: list[str] = []
    distinct_values = sorted({candidate.value for candidate in unique})
    distinct_identities = {cash_identity(candidate.value) for candidate in unique}
    if len(distinct_identities) > 1:
        notes.append(
            "PDF含多个税前现金金额候选:"
            + "|".join(distinct_values)
            + ",已按原文中的实际/本次实施表述优先提取"
        )
    if selected.context == "multiple_tax_values":
        notes.append("现金分红原句同时列示扣税后金额,本列仅摘录含税值")
    return selected, notes


def select_cash_candidate(candidates: list[Candidate]) -> Candidate:
    def score(candidate: Candidate) -> tuple[int, int, int]:
        quote = candidate.quote
        explicit_actual = int("实际" in quote or "本次权益分派方案" in quote)
        implementation = int("实施" in quote or "每股分配比例" in quote)
        return (explicit_actual, implementation, -candidate.page_number)

    return max(candidates, key=score)


def extract_action_dates(
    pdf_text: PdfText,
) -> tuple[Candidate | None, Candidate | None, list[str]]:
    record_candidates = find_labeled_date_candidates(
        pdf_text, EXPLICIT_RECORD_DATE_PATTERNS
    )
    ex_candidates = find_labeled_date_candidates(pdf_text, EXPLICIT_EX_DATE_PATTERNS)

    for page_number, page in enumerate(pdf_text.pages, start=1):
        for match in RELATED_DATE_TABLE_PATTERN.finditer(page):
            quote = quote_around(page, match.start(), match.end())
            record_candidates.append(
                Candidate(
                    value=clean_fragment(match.group("record")),
                    quote=quote,
                    page_number=page_number,
                    context="related_date_table",
                )
            )
            ex_candidates.append(
                Candidate(
                    value=clean_fragment(match.group("ex")),
                    quote=quote,
                    page_number=page_number,
                    context="related_date_table",
                )
            )

    record, record_notes = select_unique_date(
        record_candidates, "股权登记日"
    )
    ex_date, ex_notes = select_unique_date(ex_candidates, "除权除息日")
    return record, ex_date, record_notes + ex_notes


def find_labeled_date_candidates(
    pdf_text: PdfText,
    patterns: Iterable[re.Pattern[str]],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for page_number, page in enumerate(pdf_text.pages, start=1):
        for pattern in patterns:
            for match in pattern.finditer(page):
                candidates.append(
                    Candidate(
                        value=clean_fragment(match.group("date")),
                        quote=quote_around(page, match.start(), match.end()),
                        page_number=page_number,
                    )
                )
    return deduplicate_candidates(candidates)


def select_unique_date(
    candidates: list[Candidate],
    label: str,
) -> tuple[Candidate | None, list[str]]:
    if not candidates:
        return None, [f"未找到明确{label}原句"]
    by_value: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_value[date_identity(candidate.value)].append(candidate)
    if len(by_value) > 1:
        values = sorted(
            {candidate.value for grouped in by_value.values() for candidate in grouped}
        )
        return None, [f"{label}存在多个不同日期候选:" + "|".join(values)]
    grouped = next(iter(by_value.values()))
    return grouped[0], []


def extract_announcement_date(
    pdf_text: PdfText,
) -> tuple[Candidate | None, list[str]]:
    candidates: list[Candidate] = []
    for page_number, page in enumerate(pdf_text.pages, start=1):
        for match in ANNOUNCEMENT_DATE_PATTERN.finditer(page):
            candidates.append(
                Candidate(
                    value=clean_fragment(match.group("date")),
                    quote=quote_around(page, match.start(), match.end()),
                    page_number=page_number,
                )
            )
    for page_number in range(1, len(pdf_text.pages)):
        current_page_without_page_number = re.sub(
            r"^\d{1,3}\s+", "", pdf_text.pages[page_number], count=1
        )
        boundary_text = (
            pdf_text.pages[page_number - 1][-160:]
            + " "
            + current_page_without_page_number[:160]
        )
        for match in ANNOUNCEMENT_DATE_PATTERN.finditer(boundary_text):
            candidates.append(
                Candidate(
                    value=clean_fragment(match.group("date")),
                    quote=quote_around(
                        boundary_text, match.start(), match.end()
                    ),
                    page_number=page_number + 1,
                )
            )
    unique = deduplicate_candidates(candidates)
    if not unique:
        return None, ["未找到董事会/监事会落款发布日期原句"]
    last = max(unique, key=lambda candidate: candidate.page_number)
    distinct = {compact_date(candidate.value) for candidate in unique}
    notes = (
        ["PDF出现多个董事会/监事会落款日期,已提取末页落款供人工确认"]
        if len(distinct) > 1
        else []
    )
    return last, notes


def extract_share_ratio(
    pdf_text: PdfText,
) -> tuple[Candidate | None, list[str], bool]:
    nonzero = find_pattern_candidates(pdf_text, NONZERO_SHARE_PATTERNS)
    if nonzero:
        values = "|".join(candidate.value for candidate in deduplicate_candidates(nonzero))
        selected = deduplicate_candidates(nonzero)[0]
        return (
            selected,
            ["发现明确非零送转候选:" + values],
            True,
        )

    combined_zero = find_pattern_candidates(pdf_text, NO_SHARE_COMBINED_PATTERNS)
    combined_unique = deduplicate_candidates(combined_zero)
    if combined_unique:
        return combined_unique[0], [], False

    transfer_only = deduplicate_candidates(
        find_pattern_candidates(pdf_text, NO_TRANSFER_ONLY_PATTERNS)
    )
    if transfer_only:
        return (
            transfer_only[0],
            ["原文仅明确不转增,未明确写出不送股"],
            False,
        )
    return None, ["未找到明确不送不转或非零送转比例原句"], False


def find_pattern_candidates(
    pdf_text: PdfText,
    patterns: Iterable[re.Pattern[str]],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for page_number, page in enumerate(pdf_text.pages, start=1):
        for pattern in patterns:
            for match in pattern.finditer(page):
                candidates.append(
                    Candidate(
                        value=clean_fragment(match.group(0)),
                        quote=quote_around(page, match.start(), match.end()),
                        page_number=page_number,
                    )
                )
    return candidates


def quote_around(text: str, start: int, end: int) -> str:
    left = max(
        text.rfind("。", max(0, start - 240), start),
        text.rfind("；", max(0, start - 240), start),
        text.rfind("！", max(0, start - 240), start),
        text.rfind("？", max(0, start - 240), start),
    )
    left = left + 1 if left >= 0 else max(0, start - 120)

    right_candidates = [
        position
        for punctuation in ("。", "；", "！", "？")
        if (position := text.find(punctuation, end, min(len(text), end + 240))) >= 0
    ]
    right = min(right_candidates) + 1 if right_candidates else min(len(text), end + 120)
    quote = clean_fragment(text[left:right])

    if len(quote) < 15:
        left = max(0, start - 80)
        right = min(len(text), end + 80)
        quote = clean_fragment(text[left:right])
    return quote


def clean_fragment(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def compact_date(value: str) -> str:
    return re.sub(r"\s+", "", value).replace(".", "-").replace("/", "-")


def date_identity(value: str) -> tuple[str, ...]:
    digits = re.findall(r"\d+", value)
    if len(digits) == 3:
        return tuple(str(int(component)) for component in digits)
    return (compact_date(value),)


def cash_identity(value: str) -> tuple[str, Decimal | str]:
    unit = "10" if re.search(r"每\s*10\s*股", value) else "1"
    amounts = re.findall(r"(\d+(?:\.\d+)?)\s*元", value)
    return unit, Decimal(amounts[0]) if amounts else clean_fragment(value)


def deduplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    unique: dict[tuple[str, str], Candidate] = {}
    for candidate in candidates:
        key = (clean_fragment(candidate.value), clean_fragment(candidate.quote))
        unique.setdefault(key, candidate)
    return list(unique.values())


def notes_requiring_review(notes: list[str]) -> bool:
    review_markers = (
        "未找到",
        "多个",
        "已取消",
        "更正公告",
        "至少一个",
        "仅明确",
        "NONZERO",
    )
    return any(marker in note for note in notes for marker in review_markers)


def write_output(rows: list[dict[str, str]]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = OUTPUT_PATH.with_suffix(".csv.tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    temp_path.replace(OUTPUT_PATH)


def print_summary(rows: list[dict[str, str]]) -> None:
    distribution = Counter(row["security_id"] for row in rows)
    low_rows = [row for row in rows if row["extraction_confidence"] == "low"]
    nonzero = [row for row in rows if "NONZERO_SHARE" in row["notes"]]
    failures = [
        row
        for row in rows
        if any(
            not row[field]
            for field in (
                "cash_dividend_raw",
                "ex_date",
                "record_date",
                "announcement_date",
                "share_ratio",
            )
        )
    ]
    print(f"output={OUTPUT_PATH}")
    print(f"selected={len(rows)}")
    print(
        "distribution="
        + ",".join(f"{security}={distribution[security]}" for security in sorted(distribution))
    )
    print(f"high={len(rows) - len(low_rows)},low={len(low_rows)}")
    print(f"nonzero_share_transfer={len(nonzero)}")
    print(f"rows_with_missing_fields={len(failures)}")


if __name__ == "__main__":
    main()
