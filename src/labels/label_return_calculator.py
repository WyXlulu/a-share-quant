from __future__ import annotations

from datetime import date
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.domain import DataContractError, PriceBasis
from src.features.pit_adjustment_service import (
    AdjustedReturnStatus,
    CorporateActionEventRef,
    PITAdjustmentService,
)
from src.market_calendar import TradingCalendar
from src.labels.label_data_portal import (
    LABEL_EVIDENCE_STATUS,
    FutureReturnLabel,
    FutureReturnLabelStatus,
    LabelSpec,
)


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 30
LABEL_OBSERVED_HOUR = 15


def calculate_future_return_labels(
    security_ids: Iterable[str],
    signal_asof_ts: str | pd.Timestamp,
    adjustment_service: PITAdjustmentService,
    calendar: TradingCalendar,
    label_spec: LabelSpec = LabelSpec(),
) -> tuple[FutureReturnLabel, ...]:
    _assert_supported_label_spec(label_spec)
    signal_asof = _timestamp(signal_asof_ts, "signal_asof_ts")
    signal_date = signal_asof.date()
    if not calendar.is_trading_day(signal_date):
        raise DataContractError("signal_asof_ts must fall on a trading day")

    entry_date = calendar.next_trading_day(signal_date, label_spec.entry_lag_trading_days)
    exit_date = calendar.next_trading_day(signal_date, label_spec.exit_lag_trading_days)
    entry_ts = _market_open_ts(entry_date)
    exit_ts = _market_open_ts(exit_date)
    label_observed_at = _label_observed_ts(exit_date)

    labels: list[FutureReturnLabel] = []
    for security_id in security_ids:
        security = str(security_id).zfill(6)
        result = adjustment_service.open_to_open_adjusted_return(
            security,
            entry_date,
            exit_date,
            label_observed_at,
        )
        status = _label_status(result.status, result.block_reason)
        labels.append(
            FutureReturnLabel(
                security_id=security,
                signal_asof_ts=signal_asof.isoformat(),
                entry_ts=entry_ts.isoformat(),
                exit_ts=exit_ts.isoformat(),
                future_return=result.adjusted_return if status == FutureReturnLabelStatus.OK else None,
                label_end_ts=exit_ts.isoformat(),
                label_observed_at=label_observed_at.isoformat(),
                label_spec=label_spec.name,
                price_basis=PriceBasis.PIT_DERIVED,
                corporate_action_manifest=_corporate_action_manifest(result.ca_events_applied),
                input_snapshot_id=result.input_snapshot_id,
                status=status,
                evidence_status=LABEL_EVIDENCE_STATUS,
            )
        )
    return tuple(labels)


def _assert_supported_label_spec(label_spec: LabelSpec) -> None:
    if label_spec.holding_period_trading_days != 21:
        raise DataContractError("label holding period must be 21 trading days")
    if label_spec.entry_lag_trading_days != 1 or label_spec.exit_lag_trading_days != 22:
        raise DataContractError("label entry/exit lags must be T+1 open to T+H+1 open")
    if label_spec.price_basis != PriceBasis.PIT_DERIVED:
        raise DataContractError("future return labels must use PIT_DERIVED price basis")


def _label_status(
    service_status: AdjustedReturnStatus,
    block_reason: str | None,
) -> FutureReturnLabelStatus:
    if service_status == AdjustedReturnStatus.OK:
        return FutureReturnLabelStatus.OK
    if block_reason == "MISSING_ENTRY_OPEN":
        return FutureReturnLabelStatus.NOT_TRADABLE_ENTRY
    if block_reason == "MISSING_EXIT_OPEN":
        return FutureReturnLabelStatus.NOT_TRADABLE_EXIT
    if service_status == AdjustedReturnStatus.BLOCKED:
        return FutureReturnLabelStatus.BLOCKED
    return FutureReturnLabelStatus.NO_DATA


def _corporate_action_manifest(events: tuple[CorporateActionEventRef, ...]) -> str:
    if not events:
        return "no_visible_ca_events"
    return ";".join(
        f"{event.security_id}:{event.ex_date.isoformat()}:{event.action_type}:{event.source_id}"
        for event in events
    )


def _market_open_ts(day: date) -> pd.Timestamp:
    return pd.Timestamp(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        tz=ASIA_SHANGHAI,
    )


def _label_observed_ts(day: date) -> pd.Timestamp:
    return pd.Timestamp(
        year=day.year,
        month=day.month,
        day=day.day,
        hour=LABEL_OBSERVED_HOUR,
        minute=0,
        tz=ASIA_SHANGHAI,
    )


def _timestamp(value: str | pd.Timestamp, label: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError(f"{label} must be timezone-aware")
    return timestamp.tz_convert(ASIA_SHANGHAI)
