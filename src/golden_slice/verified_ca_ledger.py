from __future__ import annotations

import csv
import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from src.data.corporate_action_availability import resolve_ca_available_at
from src.domain import DataContractError
from src.golden_slice.manifest import build_unfrozen_manifest, freeze
from src.market_calendar import TradingCalendar


EXPECTED_INPUT_ROWS = 88
EXPECTED_ORDINARY_ROWS_BEFORE_DEDUP = 77
EXPECTED_PREFERRED_ROWS = 11
EXPECTED_VERIFIED_ACTIONS = 76
ORDINARY_SHARE_TYPE = "A股普通股"
PREFERRED_SHARE_TYPE = "优先股(非A股普通股)"
DISCARDED_CORRECTION_SOURCE = "000333_2021-05-26_1.pdf"
RETAINED_CORRECTION_SOURCE = "000333_2021-05-26_2.pdf"
EVIDENCE_LEVEL = "MANUALLY_VERIFIED_OFFICIAL_PDF"

EXPECTED_COUNTS_BY_SECURITY = {
    "000333": 5,
    "000651": 9,
    "000858": 5,
    "600028": 10,
    "600036": 5,
    "600276": 5,
    "600519": 7,
    "600900": 5,
    "601318": 10,
    "601398": 5,
    "601668": 5,
    "601939": 5,
}


@dataclass(frozen=True)
class VerifiedCorporateAction:
    security_id: str
    ex_date: date
    record_date: date
    cash_dividend_per_share: Decimal
    ex_right_cash_deduction_per_share: Decimal
    share_ratio: Decimal
    rights_ratio: Decimal
    rights_price: Decimal
    disclosure_date: date
    disclosure_time_known: bool
    disclosure_ts: None
    source_pdf_filename: str
    source_pdf_sha256: str
    source_announcement_title: str
    verified_by: str
    verified_at: str
    evidence_level: str

    def to_manifest_record(self, *, derived_available_at: pd.Timestamp) -> dict[str, Any]:
        return {
            "security_id": self.security_id,
            "ex_date": self.ex_date.isoformat(),
            "record_date": self.record_date.isoformat(),
            "cash_dividend_per_share": float(self.cash_dividend_per_share),
            "ex_right_cash_deduction_per_share": float(
                self.ex_right_cash_deduction_per_share
            ),
            "share_ratio": float(self.share_ratio),
            "rights_ratio": float(self.rights_ratio),
            "rights_price": float(self.rights_price),
            "disclosure_date": self.disclosure_date.isoformat(),
            "disclosure_time_known": self.disclosure_time_known,
            "disclosure_ts": self.disclosure_ts,
            "derived_available_at": derived_available_at.isoformat(),
            "source_pdf_filename": self.source_pdf_filename,
            "source_pdf_sha256": self.source_pdf_sha256,
            "source_announcement_title": self.source_announcement_title,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at,
            "evidence_level": self.evidence_level,
        }


@dataclass(frozen=True)
class VerifiedCALoadResult:
    actions: tuple[VerifiedCorporateAction, ...]
    input_row_count: int
    ordinary_row_count_before_dedup: int
    excluded_preferred_row_count: int
    discarded_duplicate_source: str
    adjustment_section_count: int
    deduction_difference_count: int
    source_rows_without_listing_entry: int
    source_csv_sha256: str
    listing_manifest_sha256: str


def load_verified_corporate_actions(
    verified_csv: Path,
    listing_manifest_csv: Path,
    pdf_dir: Path,
    *,
    verified_by: str,
    verified_at: str,
) -> VerifiedCALoadResult:
    rows = _read_csv(verified_csv)
    _require_input_shape(rows)

    ordinary_rows = [row for row in rows if row["类型"].strip() == ORDINARY_SHARE_TYPE]
    preferred_rows = [row for row in rows if row["类型"].strip() == PREFERRED_SHARE_TYPE]
    if len(ordinary_rows) != EXPECTED_ORDINARY_ROWS_BEFORE_DEDUP:
        raise DataContractError(
            "verified CA ordinary-share row count mismatch: "
            f"expected={EXPECTED_ORDINARY_ROWS_BEFORE_DEDUP}, actual={len(ordinary_rows)}"
        )
    if len(preferred_rows) != EXPECTED_PREFERRED_ROWS:
        raise DataContractError(
            "verified CA preferred-share row count mismatch: "
            f"expected={EXPECTED_PREFERRED_ROWS}, actual={len(preferred_rows)}"
        )

    _validate_correction_pair(ordinary_rows)
    deduplicated = [
        row
        for row in ordinary_rows
        if row["源文件"].strip() != DISCARDED_CORRECTION_SOURCE
    ]
    if len(deduplicated) != EXPECTED_VERIFIED_ACTIONS:
        raise DataContractError(
            "verified CA row count after correction dedup mismatch: "
            f"expected={EXPECTED_VERIFIED_ACTIONS}, actual={len(deduplicated)}"
        )

    listing_by_filename = _listing_rows_by_pdf_filename(listing_manifest_csv)
    actions: list[VerifiedCorporateAction] = []
    source_rows_without_listing_entry = 0
    seen_keys: set[tuple[str, date]] = set()

    for row in deduplicated:
        action = _build_action(
            row,
            listing_by_filename=listing_by_filename,
            pdf_dir=pdf_dir,
            verified_by=verified_by,
            verified_at=verified_at,
        )
        key = (action.security_id, action.ex_date)
        if key in seen_keys:
            raise DataContractError(f"duplicate verified CA security/ex_date key: {key}")
        seen_keys.add(key)
        actions.append(action)
        if action.source_pdf_filename not in listing_by_filename:
            source_rows_without_listing_entry += 1

    actions.sort(key=lambda action: (action.security_id, action.ex_date))
    counts = Counter(action.security_id for action in actions)
    if dict(sorted(counts.items())) != EXPECTED_COUNTS_BY_SECURITY:
        raise DataContractError(
            "verified CA per-security counts mismatch: "
            f"expected={EXPECTED_COUNTS_BY_SECURITY}, actual={dict(sorted(counts.items()))}"
        )

    adjustment_section_count = sum(
        row["有无折算节"].strip() == "有" for row in deduplicated
    )
    deduction_difference_count = sum(
        _required_decimal(row["每股现金红利_税前"], "每股现金红利_税前")
        != _optional_decimal(
            row["除权实际扣减_每股"],
            fallback=_required_decimal(row["每股现金红利_税前"], "每股现金红利_税前"),
            label="除权实际扣减_每股",
        )
        for row in deduplicated
    )
    if adjustment_section_count != 23 or deduction_difference_count != 22:
        raise DataContractError(
            "verified CA adjustment statistics mismatch after correction dedup: "
            f"adjustment_sections={adjustment_section_count}, "
            f"deduction_differences={deduction_difference_count}"
        )

    nonzero_share_actions = [
        action for action in actions if action.share_ratio != Decimal("0")
    ]
    if len(nonzero_share_actions) != 3 or any(
        action.security_id != "600276" or action.share_ratio != Decimal("0.2")
        for action in nonzero_share_actions
    ):
        raise DataContractError(
            "verified CA stock-dividend contract mismatch; expected three "
            "600276 actions with share_ratio=0.2"
        )
    if any(
        action.rights_ratio != Decimal("0") or action.rights_price != Decimal("0")
        for action in actions
    ):
        raise DataContractError("verified CA ledger must contain no rights issue")

    return VerifiedCALoadResult(
        actions=tuple(actions),
        input_row_count=len(rows),
        ordinary_row_count_before_dedup=len(ordinary_rows),
        excluded_preferred_row_count=len(preferred_rows),
        discarded_duplicate_source=DISCARDED_CORRECTION_SOURCE,
        adjustment_section_count=adjustment_section_count,
        deduction_difference_count=deduction_difference_count,
        source_rows_without_listing_entry=source_rows_without_listing_entry,
        source_csv_sha256=_sha256_file(verified_csv),
        listing_manifest_sha256=_sha256_file(listing_manifest_csv),
    )


def build_frozen_verified_manifest(
    load_result: VerifiedCALoadResult,
    trading_calendar: TradingCalendar,
    *,
    frozen_at: str,
) -> dict[str, Any]:
    if len(load_result.actions) != EXPECTED_VERIFIED_ACTIONS:
        raise DataContractError(
            f"cannot freeze golden slice with {len(load_result.actions)} verified CAs"
        )

    signal_dates = trading_calendar.between("2020-01-01", "2023-12-31")
    if not signal_dates:
        raise DataContractError("golden slice signal window has no trading days")
    first_signal_date = signal_dates[0]
    last_signal_date = signal_dates[-1]
    dependency_start = trading_calendar.previous_trading_day(first_signal_date, 252)
    dependency_end = trading_calendar.next_trading_day(last_signal_date, 22)
    expected_boundaries = (
        date(2020, 1, 2),
        date(2023, 12, 29),
        date(2018, 12, 19),
        date(2024, 1, 31),
    )
    actual_boundaries = (
        first_signal_date,
        last_signal_date,
        dependency_start,
        dependency_end,
    )
    if actual_boundaries != expected_boundaries:
        raise DataContractError(
            "golden slice dependency boundaries differ from the real TradingCalendar: "
            f"expected={expected_boundaries}, actual={actual_boundaries}"
        )

    manifest_actions: list[dict[str, Any]] = []
    for action in load_result.actions:
        available_at = resolve_ca_available_at(
            {
                "disclosure_time_known": action.disclosure_time_known,
                "disclosure_date": action.disclosure_date,
                "disclosure_ts": action.disclosure_ts,
                "ex_date": action.ex_date,
            },
            trading_calendar,
        )
        manifest_actions.append(
            action.to_manifest_record(derived_available_at=available_at)
        )

    ex_dates = [action.ex_date for action in load_result.actions]
    if (min(ex_dates), max(ex_dates)) != (date(2019, 2, 25), date(2023, 12, 20)):
        raise DataContractError(
            "verified CA ex-date range mismatch: "
            f"actual={[min(ex_dates).isoformat(), max(ex_dates).isoformat()]}"
        )

    manifest = build_unfrozen_manifest()
    manifest["manifest_version"] = 2
    manifest.pop("ca_verification_slots", None)
    manifest["verified_corporate_actions"] = manifest_actions
    manifest["corporate_action_verification"] = {
        "source_csv": "data/golden_slice/ca_verified_88.csv",
        "source_csv_sha256": load_result.source_csv_sha256,
        "listing_manifest": "data/golden_slice/cninfo_raw/listing_manifest.csv",
        "listing_manifest_sha256": load_result.listing_manifest_sha256,
        "input_rows": load_result.input_row_count,
        "ordinary_share_rows_before_dedup": (
            load_result.ordinary_row_count_before_dedup
        ),
        "preferred_share_rows_excluded": load_result.excluded_preferred_row_count,
        "preferred_share_exclusion_reason": (
            "Preferred-share dividends do not cause ex-dividend adjustment of the "
            "A-share ordinary stock."
        ),
        "discarded_duplicate_source": load_result.discarded_duplicate_source,
        "retained_corrected_source": RETAINED_CORRECTION_SOURCE,
        "verified_action_count": len(load_result.actions),
        "source_rows_without_listing_entry": (
            load_result.source_rows_without_listing_entry
        ),
        "adjustment_section_count": load_result.adjustment_section_count,
        "cash_deduction_value_difference_count": (
            load_result.deduction_difference_count
        ),
        "cash_deduction_equal_despite_adjustment_section": (
            "000333/2019-05-23"
        ),
        "largest_cash_deduction_difference_example": {
            "security_id": "000651",
            "disclosure_date": "2021-08-14",
            "cash_dividend_per_share": 3.0,
            "ex_right_cash_deduction_per_share": 2.784787,
        },
        "cash_field_semantics": {
            "cash_dividend_per_share": (
                "Actual per-share cash paid to shares participating in the "
                "distribution; used by the cash ledger."
            ),
            "ex_right_cash_deduction_per_share": (
                "Per-share cash deduction used by the ex-right reference price "
                "after total-share-capital adjustment."
            ),
            "warning": (
                "The two fields are not interchangeable; mixing them biases the "
                "ex-date reference-price adjustment."
            ),
        },
        "rights_issue_verified_count": 0,
        "nonzero_share_ratio_count": 3,
        "disclosure_time_policy": {
            "disclosure_time_known": False,
            "reason": (
                "Historical CNINFO evidence is date-level. The two 000333 records "
                "dated 2021-05-26 carry 07:49:57/07:49:58 one second apart, which "
                "resembles batch-upload metadata and is insufficient to establish "
                "a true disclosure time. All records therefore use the conservative "
                "next-trading-day policy."
            ),
            "resolver": "src.data.corporate_action_availability.resolve_ca_available_at",
            "resolver_precheck_passed": len(manifest_actions),
        },
        "verified_by": "George",
        "verified_at": manifest_actions[0]["verified_at"],
        "evidence_level": EVIDENCE_LEVEL,
    }
    manifest["dependency_coverage"] = {
        "signal_window": {"start": "2020-01-01", "end": "2023-12-31"},
        "first_signal_trading_day": first_signal_date.isoformat(),
        "last_signal_trading_day": last_signal_date.isoformat(),
        "momentum_total_lookback_trading_days": 252,
        "momentum_scoring_window_trading_days": 231,
        "momentum_skip_trading_days": 21,
        "dependency_start_trading_day": dependency_start.isoformat(),
        "label_holding_period_trading_days": 21,
        "label_exit_offset_trading_days": 22,
        "dependency_end_trading_day": dependency_end.isoformat(),
        "verified_ca_ex_date_range": {
            "start": min(ex_dates).isoformat(),
            "end": max(ex_dates).isoformat(),
        },
        "acquired_cninfo_evidence_window": {
            "start": "2018-09-01",
            "end": "2024-03-31",
        },
        "no_a_share_ordinary_ex_event_observed_intervals": [
            {
                "start": "2018-12-19",
                "end": "2019-02-24",
                "statement": (
                    "Within acquired evidence, full-title scanning and physical "
                    "ex-date feature scanning found no A-share ordinary-stock "
                    "ex-right event."
                ),
            },
            {
                "start": "2023-12-21",
                "end": "2024-02-29",
                "statement": (
                    "Within acquired evidence, physical ex-date feature scanning "
                    "found no A-share ordinary-stock ex-right event."
                ),
            },
        ],
        "evidence_pdf_count_at_freeze": 252,
        "coverage_statement_strength": "IN_ACQUIRED_EVIDENCE_NO_EVENT_FOUND",
        "lookback_note": (
            "2018-12-19 is derived from the real TradingCalendar by stepping back "
            "252 trading days from 2020-01-02. The 252-day dependency is the full "
            "lookback span, not the 231-day scoring window; skipped prices remain "
            "part of the dependency."
        ),
    }
    return freeze(manifest, frozen_at=frozen_at)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise DataContractError(f"verified CA source is missing: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _require_input_shape(rows: list[dict[str, str]]) -> None:
    if len(rows) != EXPECTED_INPUT_ROWS:
        raise DataContractError(
            f"verified CA input row count mismatch: expected={EXPECTED_INPUT_ROWS}, "
            f"actual={len(rows)}"
        )
    required = {
        "code",
        "公告日",
        "公告标题",
        "每股现金红利_税前",
        "除权实际扣减_每股",
        "有无折算节",
        "送股比例_每股",
        "股权登记日",
        "除权息日",
        "类型",
        "源文件",
    }
    missing = required - set(rows[0])
    if missing:
        raise DataContractError(f"verified CA source missing columns: {sorted(missing)}")
    unknown_types = {row["类型"].strip() for row in rows} - {
        ORDINARY_SHARE_TYPE,
        PREFERRED_SHARE_TYPE,
    }
    if unknown_types:
        raise DataContractError(f"verified CA source has unsupported types: {unknown_types}")


def _validate_correction_pair(rows: list[dict[str, str]]) -> None:
    by_source = {row["源文件"].strip(): row for row in rows}
    if DISCARDED_CORRECTION_SOURCE not in by_source:
        raise DataContractError(
            f"expected correction predecessor is missing: {DISCARDED_CORRECTION_SOURCE}"
        )
    if RETAINED_CORRECTION_SOURCE not in by_source:
        raise DataContractError(
            f"expected corrected source is missing: {RETAINED_CORRECTION_SOURCE}"
        )
    compare_columns = (
        "code",
        "公告日",
        "每股现金红利_税前",
        "除权实际扣减_每股",
        "有无折算节",
        "送股比例_每股",
        "股权登记日",
        "除权息日",
        "类型",
    )
    predecessor = by_source[DISCARDED_CORRECTION_SOURCE]
    corrected = by_source[RETAINED_CORRECTION_SOURCE]
    if any(predecessor[column].strip() != corrected[column].strip() for column in compare_columns):
        raise DataContractError(
            "000333 correction pair differs in verified business fields; cannot deduplicate"
        )


def _build_action(
    row: dict[str, str],
    *,
    listing_by_filename: dict[str, dict[str, str]],
    pdf_dir: Path,
    verified_by: str,
    verified_at: str,
) -> VerifiedCorporateAction:
    security_id = row["code"].strip().zfill(6)
    disclosure_date = _required_date(row["公告日"], "公告日")
    record_date = _required_date(row["股权登记日"], "股权登记日")
    ex_date = _required_date(row["除权息日"], "除权息日")
    if not disclosure_date <= record_date < ex_date:
        raise DataContractError(
            "verified CA date order must satisfy disclosure_date <= record_date < ex_date: "
            f"{security_id}, {disclosure_date}, {record_date}, {ex_date}"
        )

    cash = _required_decimal(row["每股现金红利_税前"], "每股现金红利_税前")
    if cash <= 0:
        raise DataContractError(
            f"verified CA cash dividend must be positive: {security_id}/{ex_date}"
        )
    deduction = _optional_decimal(
        row["除权实际扣减_每股"],
        fallback=cash,
        label="除权实际扣减_每股",
    )
    if deduction <= 0:
        raise DataContractError(
            f"verified CA ex-right cash deduction must be positive: {security_id}/{ex_date}"
        )
    share_ratio = _optional_decimal(
        row["送股比例_每股"],
        fallback=Decimal("0"),
        label="送股比例_每股",
    )
    if share_ratio < 0:
        raise DataContractError(
            f"verified CA share ratio cannot be negative: {security_id}/{ex_date}"
        )

    source_filename = row["源文件"].strip()
    if not source_filename.startswith(f"{security_id}_"):
        raise DataContractError(
            f"verified CA source filename/security mismatch: {source_filename}"
        )
    pdf_path = pdf_dir / source_filename
    if not pdf_path.exists():
        raise DataContractError(f"verified CA source PDF is missing: {pdf_path}")
    actual_sha256 = _sha256_file(pdf_path)
    listing_row = listing_by_filename.get(source_filename)
    if listing_row is not None and actual_sha256 != listing_row["pdf_sha256"].strip():
        raise DataContractError(
            f"verified CA source PDF hash differs from listing manifest: {source_filename}"
        )

    return VerifiedCorporateAction(
        security_id=security_id,
        ex_date=ex_date,
        record_date=record_date,
        cash_dividend_per_share=cash,
        ex_right_cash_deduction_per_share=deduction,
        share_ratio=share_ratio,
        rights_ratio=Decimal("0"),
        rights_price=Decimal("0"),
        disclosure_date=disclosure_date,
        disclosure_time_known=False,
        disclosure_ts=None,
        source_pdf_filename=source_filename,
        source_pdf_sha256=actual_sha256,
        source_announcement_title=row["公告标题"].strip(),
        verified_by=verified_by,
        verified_at=verified_at,
        evidence_level=EVIDENCE_LEVEL,
    )


def _listing_rows_by_pdf_filename(path: Path) -> dict[str, dict[str, str]]:
    rows = _read_csv(path)
    required = {"security_id", "disclosure_ts", "pdf_sha256"}
    if not rows or required - set(rows[0]):
        raise DataContractError("CNINFO listing manifest is missing required columns")
    counters: Counter[tuple[str, str]] = Counter()
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        security_id = row["security_id"].strip().zfill(6)
        disclosure_date = row["disclosure_ts"].strip()[:10]
        if not disclosure_date:
            raise DataContractError("CNINFO listing row has no disclosure date")
        key = (security_id, disclosure_date)
        counters[key] += 1
        filename = f"{security_id}_{disclosure_date}_{counters[key]}.pdf"
        if filename in indexed:
            raise DataContractError(f"duplicate derived listing filename: {filename}")
        indexed[filename] = row
    return indexed


def _required_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (TypeError, ValueError) as exc:
        raise DataContractError(f"verified CA {label} is missing or invalid: {value}") from exc


def _required_decimal(value: str, label: str) -> Decimal:
    if not value.strip():
        raise DataContractError(f"verified CA {label} is required")
    try:
        return Decimal(value.strip())
    except InvalidOperation as exc:
        raise DataContractError(f"verified CA {label} is invalid: {value}") from exc


def _optional_decimal(value: str, *, fallback: Decimal, label: str) -> Decimal:
    if not value.strip():
        return fallback
    return _required_decimal(value, label)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
