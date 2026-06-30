from __future__ import annotations

from enum import StrEnum


class DataContractError(ValueError):
    """Raised when a data artifact violates the frozen data contract."""


class PriceBasis(StrEnum):
    RAW_UNADJUSTED = "RAW_UNADJUSTED"
    VENDOR_ADJUSTED = "VENDOR_ADJUSTED"


class TradeStatus(StrEnum):
    NORMAL = "正常"
    SUSPENDED = "停牌"
    MISSING = "缺失"


class BarFrequency(StrEnum):
    DAILY = "daily"


SOURCE_SEMANTICS_UNVERIFIED_FOR_PIT = "UNVERIFIED_FOR_PIT"
DAILY_SAFETY_LATENCY_VERSION = "daily_bar_t1500_asia_shanghai_v1"

DAILY_BAR_REQUIRED_COLUMNS = [
    "security_id",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "trade_status",
    "event_ts",
    "available_at",
    "price_basis",
    "source_id",
    "revision_id",
    "snapshot_id",
    "declared_safety_latency_version",
    "bar_frequency",
]
