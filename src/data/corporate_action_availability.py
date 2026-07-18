from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError
from src.market_calendar import TradingCalendar


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
CA_MARKET_OPEN_TIME = time(9, 30, 0)
CA_DISCLOSURE_DATE_COLUMN = "disclosure_date"
CA_DISCLOSURE_TIME_KNOWN_COLUMN = "disclosure_time_known"
CA_DISCLOSURE_TS_COLUMN = "disclosure_ts"


def resolve_ca_available_at(
    ca_row: Mapping[str, object] | pd.Series,
    trading_calendar: TradingCalendar | None,
) -> pd.Timestamp:
    """Resolve one corporate action's PIT availability under its declared disclosure contract."""
    time_known = _field(ca_row, CA_DISCLOSURE_TIME_KNOWN_COLUMN)

    if _is_missing(time_known):
        available_at = _field(ca_row, "available_at")
        if _is_missing(available_at):
            raise DataContractError(
                "corporate_actions row has neither disclosure_time_known nor available_at"
            )
        return _require_asia_shanghai_timestamp(available_at, "corporate_actions.available_at")

    if not pd.api.types.is_bool(time_known):
        raise DataContractError(
            "corporate_actions.disclosure_time_known must be None, True, or False"
        )

    if bool(time_known):
        disclosure_ts = _field(ca_row, CA_DISCLOSURE_TS_COLUMN)
        if _is_missing(disclosure_ts):
            raise DataContractError(
                "corporate_actions.disclosure_ts is required when disclosure_time_known=True"
            )
        resolved = _require_asia_shanghai_timestamp(
            disclosure_ts,
            "corporate_actions.disclosure_ts",
        )
        if resolved.hour == 0 and resolved.minute == 0 and resolved.second == 0:
            raise DataContractError(
                "corporate_actions.disclosure_ts cannot be date-only midnight when "
                "disclosure_time_known=True"
            )
        return resolved

    disclosure_date = _field(ca_row, CA_DISCLOSURE_DATE_COLUMN)
    if _is_missing(disclosure_date):
        raise DataContractError(
            "corporate_actions.disclosure_date is required when disclosure_time_known=False"
        )
    ex_date = _field(ca_row, "ex_date")
    if _is_missing(ex_date):
        raise DataContractError(
            "corporate_actions.ex_date is required when disclosure_time_known=False"
        )
    if trading_calendar is None:
        raise DataContractError(
            "TradingCalendar is required when corporate_actions.disclosure_time_known=False"
        )

    disclosure_day = _require_calendar_date(
        disclosure_date,
        "corporate_actions.disclosure_date",
    )
    ex_day = _require_calendar_date(ex_date, "corporate_actions.ex_date")
    try:
        visible_day = trading_calendar.next_trading_day(disclosure_day)
    except (IndexError, TypeError, ValueError) as exc:
        raise DataContractError(
            "corporate_actions.disclosure_date has no next trading day in TradingCalendar: "
            f"{disclosure_day.isoformat()}"
        ) from exc

    available_at = pd.Timestamp(
        datetime.combine(visible_day, CA_MARKET_OPEN_TIME, tzinfo=ASIA_SHANGHAI)
    )
    ex_date_start = pd.Timestamp(datetime.combine(ex_day, time.min, tzinfo=ASIA_SHANGHAI))
    if available_at >= ex_date_start:
        raise DataContractError(
            "corporate action availability cannot be verified before ex_date: "
            f"available_at={available_at.isoformat()}, ex_date={ex_day.isoformat()}"
        )
    return available_at


def materialize_explicit_ca_available_at(
    rows: pd.DataFrame,
    resolved_available_at: pd.Series,
) -> pd.DataFrame:
    """Expose derived availability only for rows opting into the disclosure-time contract."""
    if CA_DISCLOSURE_TIME_KNOWN_COLUMN not in rows.columns:
        return rows

    explicit_contract = rows[CA_DISCLOSURE_TIME_KNOWN_COLUMN].map(
        lambda value: not _is_missing(value)
    )
    if not explicit_contract.any():
        return rows

    materialized = rows.copy()
    if "available_at" in materialized.columns:
        values = materialized["available_at"].astype("object")
    else:
        values = pd.Series(pd.NA, index=materialized.index, dtype="object")
    values.loc[explicit_contract] = resolved_available_at.loc[explicit_contract].astype(
        "object"
    )
    materialized["available_at"] = values
    return materialized


def _field(row: Mapping[str, object] | pd.Series, name: str) -> object | None:
    if isinstance(row, Mapping):
        return row.get(name)
    if isinstance(row, pd.Series):
        return row[name] if name in row.index else None
    return getattr(row, name, None)


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    missing = pd.isna(value)
    try:
        return bool(missing)
    except (TypeError, ValueError):
        return False


def _require_asia_shanghai_timestamp(value: object, label: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise DataContractError(f"{label} is not a valid timestamp: {value}") from exc
    if pd.isna(timestamp):
        raise DataContractError(f"{label} is missing")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError(f"{label} must be timezone-aware Asia/Shanghai timestamp")
    if timestamp.utcoffset() != timedelta(hours=8):
        raise DataContractError(f"{label} must use Asia/Shanghai +08:00 offset")
    return timestamp.tz_convert(ASIA_SHANGHAI)


def _require_calendar_date(value: object, label: str) -> date:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise DataContractError(f"{label} is not a valid date: {value}") from exc
    if pd.isna(timestamp):
        raise DataContractError(f"{label} is missing")
    if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
        if timestamp.utcoffset() != timedelta(hours=8):
            raise DataContractError(f"{label} must use Asia/Shanghai +08:00 offset")
        timestamp = timestamp.tz_convert(ASIA_SHANGHAI)
    return timestamp.date()
