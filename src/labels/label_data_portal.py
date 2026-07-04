from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.domain import DataContractError, PriceBasis


HOLDING_PERIOD_TRADING_DAYS = 21
LABEL_SPEC_NAME = "forward_21d_t1_to_t22_open_pit_adjusted_return"


@dataclass(frozen=True)
class LabelSpec:
    name: str = LABEL_SPEC_NAME
    holding_period_trading_days: int = HOLDING_PERIOD_TRADING_DAYS
    entry_lag_trading_days: int = 1
    exit_lag_trading_days: int = HOLDING_PERIOD_TRADING_DAYS + 1
    price_basis: PriceBasis = PriceBasis.PIT_DERIVED


@dataclass(frozen=True)
class FutureReturnLabel:
    security_id: str
    signal_asof_ts: str
    entry_ts: str
    exit_ts: str
    future_return: Decimal | None
    label_end_ts: str
    label_observed_at: str
    label_spec: str
    price_basis: PriceBasis
    corporate_action_manifest: str
    input_snapshot_id: str


@dataclass(frozen=True)
class LabelDataPortal:
    table_path: Path

    def query_future_outcome_inputs(
        self,
        security_ids: Iterable[str],
        entry_ts: str | pd.Timestamp,
        exit_ts: str | pd.Timestamp,
        label_spec: LabelSpec,
    ) -> tuple[FutureReturnLabel, ...]:
        _assert_supported_label_spec(label_spec)
        rows = pd.read_parquet(self.table_path)
        _require_columns(rows)
        _assert_pit_adjusted(rows)

        requested_ids = {str(security_id).zfill(6) for security_id in security_ids}
        entry = _timestamp(entry_ts, "entry_ts").isoformat()
        exit_ = _timestamp(exit_ts, "exit_ts").isoformat()
        visible = rows.loc[
            rows["security_id"].astype(str).str.zfill(6).isin(requested_ids)
            & pd.to_datetime(rows["entry_ts"], errors="raise").map(lambda value: value.isoformat()).eq(entry)
            & pd.to_datetime(rows["exit_ts"], errors="raise").map(lambda value: value.isoformat()).eq(exit_)
            & rows["label_spec"].astype(str).eq(label_spec.name)
        ].copy()

        return tuple(_label_from_row(row) for row in visible.itertuples(index=False))


def _assert_supported_label_spec(label_spec: LabelSpec) -> None:
    if label_spec.name != LABEL_SPEC_NAME:
        raise DataContractError(f"unsupported label spec: {label_spec.name}")
    if label_spec.holding_period_trading_days != HOLDING_PERIOD_TRADING_DAYS:
        raise DataContractError("label holding period must be 21 trading days")
    if label_spec.entry_lag_trading_days != 1 or label_spec.exit_lag_trading_days != 22:
        raise DataContractError("label entry/exit lags must be T+1 open to T+H+1 open")
    if label_spec.price_basis != PriceBasis.PIT_DERIVED:
        raise DataContractError("future return labels must use PIT_DERIVED price basis")


def _require_columns(rows: pd.DataFrame) -> None:
    required = [
        "security_id",
        "signal_asof_ts",
        "entry_ts",
        "exit_ts",
        "future_return",
        "label_end_ts",
        "label_observed_at",
        "label_spec",
        "price_basis",
        "corporate_action_manifest",
        "snapshot_id",
    ]
    missing = [column for column in required if column not in rows.columns]
    if missing:
        raise DataContractError(f"future_return_labels missing required columns: {missing}")


def _assert_pit_adjusted(rows: pd.DataFrame) -> None:
    invalid_basis = rows.loc[rows["price_basis"].astype(str).ne(PriceBasis.PIT_DERIVED.value)]
    if not invalid_basis.empty:
        observed = sorted(str(value) for value in invalid_basis["price_basis"].dropna().unique().tolist())
        raise DataContractError(f"future_return_labels.price_basis must be PIT_DERIVED; observed={observed}")
    missing_manifest = rows.loc[rows["corporate_action_manifest"].isna()]
    if not missing_manifest.empty:
        raise DataContractError("future_return_labels missing corporate_action_manifest")


def _label_from_row(row: object) -> FutureReturnLabel:
    return FutureReturnLabel(
        security_id=str(getattr(row, "security_id")).zfill(6),
        signal_asof_ts=_timestamp(getattr(row, "signal_asof_ts"), "signal_asof_ts").isoformat(),
        entry_ts=_timestamp(getattr(row, "entry_ts"), "entry_ts").isoformat(),
        exit_ts=_timestamp(getattr(row, "exit_ts"), "exit_ts").isoformat(),
        future_return=_decimal_or_none(getattr(row, "future_return")),
        label_end_ts=_timestamp(getattr(row, "label_end_ts"), "label_end_ts").isoformat(),
        label_observed_at=_timestamp(getattr(row, "label_observed_at"), "label_observed_at").isoformat(),
        label_spec=str(getattr(row, "label_spec")),
        price_basis=PriceBasis(str(getattr(row, "price_basis"))),
        corporate_action_manifest=str(getattr(row, "corporate_action_manifest")),
        input_snapshot_id=str(getattr(row, "snapshot_id")),
    )


def _timestamp(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise DataContractError(f"{label} must be timezone-aware")
    return timestamp.tz_convert("Asia/Shanghai")


def _decimal_or_none(value: object) -> Decimal | None:
    if pd.isna(value):
        return None
    return Decimal(str(value))
