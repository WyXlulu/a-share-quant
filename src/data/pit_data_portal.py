from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError
from src.market_calendar import TradingCalendar

from .corporate_action_availability import (
    materialize_explicit_ca_available_at,
    resolve_ca_available_at,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
REQUIRED_VISIBILITY_COLUMN = "available_at"
SUPPORTED_TABLES = frozenset({"daily_bar_raw", "security_master", "corporate_actions"})
DEFAULT_TABLE_PATHS = {
    "daily_bar_raw": Path("data/l1_raw/daily_bar_raw.parquet"),
    "security_master": Path("data/l1_raw/security_master.parquet"),
    "corporate_actions": Path("data/l2_corporate_actions/corporate_actions.parquet"),
}
SECURITY_MASTER_TAINT_FIELDS = ("is_st", "status")
SECURITY_MASTER_COMPANION_SUFFIXES = ("available_at", "point_in_time_capability", "evidence_level")


@dataclass(frozen=True)
class PITDataPortal:
    table_paths: dict[str, Path] = field(default_factory=lambda: DEFAULT_TABLE_PATHS.copy())
    trading_calendar: TradingCalendar | None = None

    def query(
        self,
        table: str,
        asof_ts: str | pd.Timestamp,
        security_ids: Iterable[str] | None = None,
        columns: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        if table not in SUPPORTED_TABLES:
            raise DataContractError(f"Unsupported PIT table: {table}")

        asof = _parse_asia_shanghai_timestamp(asof_ts, "asof_ts")
        rows = self._read_table(table)
        if table == "corporate_actions":
            available_at = pd.Series(
                [
                    resolve_ca_available_at(row, self.trading_calendar)
                    for _, row in rows.iterrows()
                ],
                index=rows.index,
            )
            rows = materialize_explicit_ca_available_at(rows, available_at)
        else:
            self._assert_available_at(rows, table)
            available_at = _parse_timestamp_series(
                rows[REQUIRED_VISIBILITY_COLUMN],
                table,
                REQUIRED_VISIBILITY_COLUMN,
            )
        visible = rows.loc[available_at.le(asof)].copy()

        if table == "daily_bar_raw" and "event_ts" in visible.columns:
            event_ts = _parse_timestamp_series(visible["event_ts"], table, "event_ts")
            visible = visible.loc[event_ts.le(asof)].copy()

        if security_ids is not None:
            if "security_id" not in visible.columns:
                raise DataContractError(f"{table} is missing required security_id column")
            requested_ids = {str(security_id).zfill(6) for security_id in security_ids}
            visible = visible.loc[visible["security_id"].astype(str).str.zfill(6).isin(requested_ids)].copy()

        if table == "security_master":
            visible = _mask_security_master_future_fields(visible, asof)

        output_columns = self._resolve_columns(table, visible, columns)
        result = visible.loc[:, output_columns].copy()
        result.attrs["asof_ts"] = asof.isoformat()
        result.attrs["table"] = table
        result.attrs["field_capabilities"] = _field_capabilities(table, visible, output_columns)
        result.attrs["visibility_predicate"] = f"{REQUIRED_VISIBILITY_COLUMN} <= {asof.isoformat()}"
        return result

    def _read_table(self, table: str) -> pd.DataFrame:
        path = self.table_paths.get(table)
        if path is None:
            raise DataContractError(f"No storage path configured for table: {table}")
        if not path.exists():
            raise DataContractError(f"PIT table file does not exist: {path}")
        return pd.read_parquet(path)

    def _assert_available_at(self, rows: pd.DataFrame, table: str) -> None:
        if REQUIRED_VISIBILITY_COLUMN not in rows.columns:
            raise DataContractError(
                f"{table} is missing required {REQUIRED_VISIBILITY_COLUMN}; fail-closed"
            )

    def _resolve_columns(
        self,
        table: str,
        rows: pd.DataFrame,
        columns: Iterable[str] | None,
    ) -> list[str]:
        if columns is None:
            requested = list(rows.columns)
        else:
            requested = list(dict.fromkeys(columns))

        missing = [column for column in requested if column not in rows.columns]
        if missing:
            raise DataContractError(f"{table} missing requested columns: {missing}")

        if table == "security_master":
            requested = _append_security_master_companion_columns(requested, rows.columns)

        return requested


def _mask_security_master_future_fields(rows: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    masked = rows.copy()
    for field_name in SECURITY_MASTER_TAINT_FIELDS:
        if field_name not in masked.columns:
            continue

        availability_column = f"{field_name}_available_at"
        if availability_column not in masked.columns:
            raise DataContractError(
                f"security_master.{field_name} is missing field-level available_at column: "
                f"{availability_column}"
            )

        field_available_at = _parse_timestamp_series(
            masked[availability_column],
            "security_master",
            availability_column,
        )
        future_field = field_available_at.gt(asof)
        for column in (field_name, f"{field_name}_as_of", availability_column):
            if column in masked.columns:
                masked[column] = masked[column].astype("object")
                masked.loc[future_field, column] = pd.NA
    return masked


def _append_security_master_companion_columns(requested: list[str], available_columns: Iterable[str]) -> list[str]:
    columns = list(requested)
    available = set(available_columns)
    for field_name in SECURITY_MASTER_TAINT_FIELDS:
        if field_name not in requested:
            continue
        for suffix in SECURITY_MASTER_COMPANION_SUFFIXES:
            companion = f"{field_name}_{suffix}"
            if companion not in available:
                raise DataContractError(
                    f"security_master.{field_name} is missing taint companion column: {companion}"
                )
            if companion not in columns:
                columns.append(companion)
    return columns


def _field_capabilities(table: str, rows: pd.DataFrame, columns: Iterable[str]) -> dict[str, dict[str, str]]:
    if table != "security_master":
        return {}

    capabilities: dict[str, dict[str, str]] = {}
    output_columns = set(columns)
    for field_name in SECURITY_MASTER_TAINT_FIELDS:
        if field_name not in output_columns:
            continue
        capability_col = f"{field_name}_point_in_time_capability"
        evidence_col = f"{field_name}_evidence_level"
        availability_col = f"{field_name}_available_at"
        capabilities[field_name] = {}
        if availability_col in rows.columns:
            capabilities[field_name]["available_at"] = _first_non_null(rows[availability_col])
        if capability_col in rows.columns:
            capabilities[field_name]["point_in_time_capability"] = _first_non_null(rows[capability_col])
        if evidence_col in rows.columns:
            capabilities[field_name]["evidence_level"] = _first_non_null(rows[evidence_col])
    return capabilities


def _first_non_null(values: pd.Series) -> str:
    non_null = values.dropna()
    if non_null.empty:
        return ""
    return str(non_null.iloc[0])


def _parse_asia_shanghai_timestamp(value: str | pd.Timestamp, label: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError(f"{label} must be timezone-aware Asia/Shanghai timestamp")
    if timestamp.utcoffset() != timedelta(hours=8):
        raise DataContractError(f"{label} must use Asia/Shanghai +08:00 offset")
    return timestamp.tz_convert(ASIA_SHANGHAI)


def _parse_timestamp_series(values: pd.Series, table: str, column: str) -> pd.Series:
    parsed_values: list[pd.Timestamp] = []
    for index, value in values.items():
        try:
            parsed_values.append(_parse_asia_shanghai_timestamp(value, f"{table}.{column}[{index}]"))
        except Exception as exc:
            if isinstance(exc, DataContractError):
                raise
            raise DataContractError(f"{table}.{column}[{index}] is not a valid timestamp: {value}") from exc
    return pd.Series(parsed_values, index=values.index)
