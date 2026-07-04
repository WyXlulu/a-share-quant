from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.data import PITDataPortal
from src.domain import (
    CorporateActionVisibilityStatus,
    DataContractError,
    PriceBasis,
    SUPPORTED_FACTOR_ACTION_TYPES,
    calculate_ex_right_reference_price,
    evaluate_corporate_action_visibility,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
EVIDENCE_STATUS = "EXPLORATORY_TAINTED"
# Next step: add LT-002B fast/slow portal equivalence sentinel outside this first cut.


class AdjustedReturnStatus(StrEnum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    NO_DATA = "NO_DATA"


@dataclass(frozen=True)
class CorporateActionEventRef:
    security_id: str
    ex_date: date
    action_type: str
    available_at: str
    source_id: str
    snapshot_id: str


@dataclass(frozen=True)
class AdjustedReturnPoint:
    security_id: str
    trade_date: date
    status: AdjustedReturnStatus
    adjusted_return: Decimal | None
    reference_price: Decimal | None
    raw_close: Decimal | None
    ca_on_date: tuple[str, ...]
    block_reason: str | None = None


@dataclass(frozen=True)
class AdjustedReturnSeries:
    security_id: str
    points: tuple[AdjustedReturnPoint, ...]
    evidence_status: str
    price_basis: PriceBasis
    derivation_asof_ts: str
    input_snapshot_id: str
    ca_events_applied: tuple[CorporateActionEventRef, ...]


@dataclass(frozen=True)
class CumulativeAdjustedReturnResult:
    security_id: str
    asof_ts: str
    lookback_trading_days: int
    status: AdjustedReturnStatus
    adjusted_return: Decimal | None
    evidence_status: str
    price_basis: PriceBasis
    derivation_asof_ts: str
    input_snapshot_id: str
    ca_events_applied: tuple[CorporateActionEventRef, ...]
    block_reason: str | None = None


@dataclass(frozen=True)
class AdjustmentFactorPoint:
    security_id: str
    trade_date: date
    status: AdjustedReturnStatus
    factor: Decimal | None
    block_reason: str | None = None


@dataclass(frozen=True)
class AdjustmentFactorSeries:
    security_id: str
    points: tuple[AdjustmentFactorPoint, ...]
    evidence_status: str
    price_basis: PriceBasis
    derivation_asof_ts: str
    input_snapshot_id: str
    ca_events_applied: tuple[CorporateActionEventRef, ...]


@dataclass(frozen=True)
class PITAdjustmentService:
    portal: PITDataPortal

    def daily_adjusted_return_series(
        self,
        security_id: str,
        start_ts: Any,
        end_ts: Any,
        derivation_asof_ts: Any,
    ) -> AdjustedReturnSeries:
        security = str(security_id).zfill(6)
        start_date = _date(start_ts)
        end_date = _date(end_ts)
        derivation_asof = _asof_timestamp(derivation_asof_ts)

        bars = self._daily_bars(security, derivation_asof)
        actions = self._corporate_actions(security, derivation_asof)
        action_map = _actions_by_ex_date(actions)
        daily_snapshots = _snapshot_ids(bars)
        ca_snapshots = _snapshot_ids(actions)

        points: list[AdjustedReturnPoint] = []
        dated_bars = bars.loc[bars["trade_date_key"].between(start_date, end_date)].copy()
        for row in dated_bars.itertuples(index=False):
            trade_date = getattr(row, "trade_date_key")
            raw_close = _decimal_or_none(getattr(row, "close"))
            previous_close = _previous_close(bars, trade_date)
            ca_rows = action_map.get(trade_date, pd.DataFrame())
            ca_types = _action_types(ca_rows)

            if raw_close is None or previous_close is None:
                points.append(
                    AdjustedReturnPoint(
                        security,
                        trade_date,
                        AdjustedReturnStatus.NO_DATA,
                        None,
                        None,
                        raw_close,
                        ca_types,
                        "MISSING_CLOSE_OR_PREVIOUS_CLOSE",
                    )
                )
                continue

            block_reason = _block_reason(ca_rows, derivation_asof)
            if block_reason is not None:
                points.append(
                    AdjustedReturnPoint(
                        security,
                        trade_date,
                        AdjustedReturnStatus.BLOCKED,
                        None,
                        None,
                        raw_close,
                        ca_types,
                        block_reason,
                    )
                )
                continue

            reference_price = previous_close
            if not ca_rows.empty:
                cash_dividend, split_ratio, rights_ratio, rights_price = _pricing_inputs(ca_rows)
                reference_price = calculate_ex_right_reference_price(
                    trade_date,
                    previous_close,
                    cash_dividend,
                    split_ratio,
                    rights_ratio,
                    rights_price,
                )

            points.append(
                AdjustedReturnPoint(
                    security,
                    trade_date,
                    AdjustedReturnStatus.OK,
                    raw_close / reference_price - Decimal("1"),
                    reference_price,
                    raw_close,
                    ca_types,
                )
            )

        return AdjustedReturnSeries(
            security_id=security,
            points=tuple(points),
            evidence_status=EVIDENCE_STATUS,
            price_basis=PriceBasis.PIT_DERIVED,
            derivation_asof_ts=derivation_asof.isoformat(),
            input_snapshot_id=_join_snapshot_ids(daily_snapshots + ca_snapshots),
            ca_events_applied=_applied_event_refs(actions, derivation_asof),
        )

    def cumulative_adjusted_return(
        self,
        security_id: str,
        asof_ts: Any,
        lookback_trading_days: int,
        derivation_asof_ts: Any,
    ) -> CumulativeAdjustedReturnResult:
        if lookback_trading_days <= 0:
            raise ValueError("lookback_trading_days must be positive")

        security = str(security_id).zfill(6)
        asof = _asof_timestamp(asof_ts)
        derivation_asof = _asof_timestamp(derivation_asof_ts)
        bars = self._daily_bars(security, derivation_asof)
        eligible = bars.loc[bars["trade_date_key"].le(asof.date())].sort_values("trade_date_key")
        window = eligible.tail(lookback_trading_days)
        if window.empty:
            return CumulativeAdjustedReturnResult(
                security,
                asof.isoformat(),
                lookback_trading_days,
                AdjustedReturnStatus.NO_DATA,
                None,
                EVIDENCE_STATUS,
                PriceBasis.PIT_DERIVED,
                derivation_asof.isoformat(),
                _join_snapshot_ids(_snapshot_ids(bars)),
                tuple(),
                "NO_VISIBLE_DAILY_BARS",
            )

        series = self.daily_adjusted_return_series(
            security,
            window.iloc[0]["trade_date_key"],
            window.iloc[-1]["trade_date_key"],
            derivation_asof,
        )
        blocked = next((point for point in series.points if point.status == AdjustedReturnStatus.BLOCKED), None)
        if blocked is not None:
            return CumulativeAdjustedReturnResult(
                security,
                asof.isoformat(),
                lookback_trading_days,
                AdjustedReturnStatus.BLOCKED,
                None,
                series.evidence_status,
                series.price_basis,
                series.derivation_asof_ts,
                series.input_snapshot_id,
                series.ca_events_applied,
                blocked.block_reason,
            )

        missing = next((point for point in series.points if point.status == AdjustedReturnStatus.NO_DATA), None)
        if missing is not None:
            return CumulativeAdjustedReturnResult(
                security,
                asof.isoformat(),
                lookback_trading_days,
                AdjustedReturnStatus.NO_DATA,
                None,
                series.evidence_status,
                series.price_basis,
                series.derivation_asof_ts,
                series.input_snapshot_id,
                series.ca_events_applied,
                missing.block_reason,
            )

        compounded = Decimal("1")
        for point in series.points:
            assert point.adjusted_return is not None
            compounded *= Decimal("1") + point.adjusted_return

        return CumulativeAdjustedReturnResult(
            security,
            asof.isoformat(),
            lookback_trading_days,
            AdjustedReturnStatus.OK,
            compounded - Decimal("1"),
            series.evidence_status,
            series.price_basis,
            series.derivation_asof_ts,
            series.input_snapshot_id,
            series.ca_events_applied,
        )

    def adjustment_factor_series(
        self,
        security_id: str,
        start_ts: Any,
        end_ts: Any,
        derivation_asof_ts: Any,
    ) -> AdjustmentFactorSeries:
        returns = self.daily_adjusted_return_series(
            security_id,
            start_ts,
            end_ts,
            derivation_asof_ts,
        )
        factor = Decimal("1")
        points: list[AdjustmentFactorPoint] = []
        for point in returns.points:
            if point.status != AdjustedReturnStatus.OK or point.adjusted_return is None:
                points.append(
                    AdjustmentFactorPoint(
                        point.security_id,
                        point.trade_date,
                        point.status,
                        None,
                        point.block_reason,
                    )
                )
                continue
            factor *= Decimal("1") + point.adjusted_return
            points.append(
                AdjustmentFactorPoint(
                    point.security_id,
                    point.trade_date,
                    AdjustedReturnStatus.OK,
                    factor,
                )
            )
        return AdjustmentFactorSeries(
            returns.security_id,
            tuple(points),
            returns.evidence_status,
            returns.price_basis,
            returns.derivation_asof_ts,
            returns.input_snapshot_id,
            returns.ca_events_applied,
        )

    def _daily_bars(self, security_id: str, derivation_asof: pd.Timestamp) -> pd.DataFrame:
        rows = self.portal.query(
            "daily_bar_raw",
            derivation_asof,
            security_ids=[security_id],
            columns=[
                "security_id",
                "trade_date",
                "close",
                "event_ts",
                "available_at",
                "price_basis",
                "snapshot_id",
            ],
        )
        _require_columns(
            rows,
            ["security_id", "trade_date", "close", "price_basis", "snapshot_id"],
            "daily_bar_raw",
        )
        _assert_raw_unadjusted(rows)
        if rows.empty:
            return rows.assign(trade_date_key=pd.Series(dtype="object"))
        rows = rows.copy()
        rows["trade_date_key"] = pd.to_datetime(rows["trade_date"], errors="raise").dt.date
        return rows.sort_values("trade_date_key").reset_index(drop=True)

    def _corporate_actions(self, security_id: str, derivation_asof: pd.Timestamp) -> pd.DataFrame:
        try:
            rows = self.portal.query(
                "corporate_actions",
                derivation_asof,
                security_ids=[security_id],
            )
        except DataContractError:
            raise
        _require_columns(
            rows,
            [
                "security_id",
                "ex_date",
                "action_type",
                "cash_dividend_per_share",
                "share_ratio",
                "available_at",
                "source_id",
                "snapshot_id",
            ],
            "corporate_actions",
        )
        if rows.empty:
            return rows.assign(ex_date_key=pd.Series(dtype="object"))
        rows = rows.copy()
        rows["ex_date_key"] = pd.to_datetime(rows["ex_date"], errors="raise").dt.date
        rows["_available_at_sort"] = pd.to_datetime(rows["available_at"], errors="raise")
        return (
            rows.sort_values(["security_id", "ex_date_key", "action_type", "_available_at_sort"])
            .drop_duplicates(["security_id", "ex_date_key", "action_type"], keep="last")
            .reset_index(drop=True)
        )


def _actions_by_ex_date(actions: pd.DataFrame) -> dict[date, pd.DataFrame]:
    if actions.empty:
        return {}
    return {ex_date: rows.copy() for ex_date, rows in actions.groupby("ex_date_key", sort=False)}


def _action_types(actions: pd.DataFrame) -> tuple[str, ...]:
    if actions.empty:
        return tuple()
    return tuple(str(action_type) for action_type in actions["action_type"].tolist())


def _block_reason(actions: pd.DataFrame, derivation_asof: pd.Timestamp) -> str | None:
    if actions.empty:
        return None
    for row in actions.itertuples(index=False):
        visibility = evaluate_corporate_action_visibility(
            row,
            derivation_asof,
            supported_action_types=SUPPORTED_FACTOR_ACTION_TYPES,
        )
        if visibility.status in (
            CorporateActionVisibilityStatus.UNPROCESSED_BOUNDARY,
            CorporateActionVisibilityStatus.UNSUPPORTED_TYPE,
        ):
            return visibility.reason
    try:
        _pricing_inputs(actions)
    except DataContractError as exc:
        return str(exc)
    return None


def _pricing_inputs(actions: pd.DataFrame) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    cash_dividend = Decimal("0")
    split_ratio = Decimal("0")
    rights_ratio = Decimal("0")
    rights_consideration = Decimal("0")
    for row in actions.itertuples(index=False):
        action_type = str(getattr(row, "action_type"))
        if action_type == "RIGHTS_ISSUE":
            row_rights_ratio = _decimal_or_zero(_optional_field(row, "rights_ratio", getattr(row, "share_ratio")))
            row_rights_price = _rights_price(row)
            if row_rights_ratio > Decimal("0") and row_rights_price is None:
                raise DataContractError("RIGHTS_ISSUE missing rights_price; cannot construct PIT factor")
            rights_ratio += row_rights_ratio
            rights_consideration += row_rights_ratio * (row_rights_price or Decimal("0"))
            continue
        cash_dividend += _decimal_or_zero(getattr(row, "cash_dividend_per_share"))
        split_ratio += _decimal_or_zero(getattr(row, "share_ratio"))

    rights_price = Decimal("0")
    if rights_ratio > Decimal("0"):
        rights_price = rights_consideration / rights_ratio
    return cash_dividend, split_ratio, rights_ratio, rights_price


def _applied_event_refs(actions: pd.DataFrame, derivation_asof: pd.Timestamp) -> tuple[CorporateActionEventRef, ...]:
    if actions.empty:
        return tuple()
    refs: list[CorporateActionEventRef] = []
    for row in actions.itertuples(index=False):
        ex_date = getattr(row, "ex_date_key")
        if ex_date > derivation_asof.date():
            continue
        visibility = evaluate_corporate_action_visibility(
            row,
            derivation_asof,
            supported_action_types=SUPPORTED_FACTOR_ACTION_TYPES,
        )
        if visibility.status != CorporateActionVisibilityStatus.VISIBLE_APPLICABLE:
            continue
        refs.append(
            CorporateActionEventRef(
                str(getattr(row, "security_id")).zfill(6),
                ex_date,
                str(getattr(row, "action_type")),
                str(getattr(row, "available_at")),
                str(getattr(row, "source_id")),
                str(getattr(row, "snapshot_id")),
            )
        )
    return tuple(refs)


def _previous_close(bars: pd.DataFrame, trade_date: date) -> Decimal | None:
    previous = bars.loc[bars["trade_date_key"].lt(trade_date)].tail(1)
    if previous.empty:
        return None
    return _decimal_or_none(previous.iloc[0]["close"])


def _snapshot_ids(rows: pd.DataFrame) -> list[str]:
    if rows.empty:
        return []
    if "snapshot_id" not in rows.columns:
        return []
    return sorted(str(value) for value in rows["snapshot_id"].dropna().unique().tolist())


def _join_snapshot_ids(snapshot_ids: list[str]) -> str:
    unique = sorted(dict.fromkeys(snapshot_ids))
    return ";".join(unique)


def _rights_price(row: Any) -> Decimal | None:
    for field_name in ("rights_price", "rights_price_per_share"):
        value = _optional_field(row, field_name, None)
        if value is not None and not pd.isna(value):
            return _decimal_or_zero(value)
    return None


def _optional_field(row: Any, field_name: str, default: Any) -> Any:
    return getattr(row, field_name, default)


def _decimal_or_none(value: Any) -> Decimal | None:
    if pd.isna(value):
        return None
    return Decimal(str(value))


def _decimal_or_zero(value: Any) -> Decimal:
    if pd.isna(value):
        return Decimal("0")
    return Decimal(str(value))


def _require_columns(rows: pd.DataFrame, columns: list[str], table: str) -> None:
    missing = [column for column in columns if column not in rows.columns]
    if missing:
        raise DataContractError(f"{table} missing required PIT adjustment columns: {missing}")


def _assert_raw_unadjusted(rows: pd.DataFrame) -> None:
    invalid = rows.loc[
        rows["price_basis"].isna()
        | rows["price_basis"].astype(str).ne(PriceBasis.RAW_UNADJUSTED.value)
    ]
    if not invalid.empty:
        observed = sorted(str(value) for value in invalid["price_basis"].dropna().unique().tolist())
        raise DataContractError(
            "daily_bar_raw.price_basis must be RAW_UNADJUSTED for PIT adjustment; "
            f"observed={observed}"
        )


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _asof_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError("asof timestamp must be timezone-aware")
    return timestamp.tz_convert(ASIA_SHANGHAI)
