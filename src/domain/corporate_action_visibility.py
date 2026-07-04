from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Iterable


SUPPORTED_FACTOR_ACTION_TYPES = frozenset(
    {"CASH_DIVIDEND", "STOCK_DIVIDEND", "RIGHTS_ISSUE"}
)
SUPPORTED_LEDGER_ACTION_TYPES = frozenset({"CASH_DIVIDEND", "STOCK_DIVIDEND"})


class CorporateActionVisibilityStatus(StrEnum):
    VISIBLE_APPLICABLE = "VISIBLE_APPLICABLE"
    NOT_YET_VISIBLE = "NOT_YET_VISIBLE"
    UNPROCESSED_BOUNDARY = "UNPROCESSED_BOUNDARY"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"


@dataclass(frozen=True)
class CorporateActionVisibilityResult:
    status: CorporateActionVisibilityStatus
    reason: str


def evaluate_corporate_action_visibility(
    action: Any,
    asof_ts: Any,
    *,
    supported_action_types: Iterable[str] = SUPPORTED_FACTOR_ACTION_TYPES,
) -> CorporateActionVisibilityResult:
    """Classify whether a corporate action is usable at an application as-of."""

    action_type = str(_field(action, "action_type"))
    supported_types = frozenset(str(action_type) for action_type in supported_action_types)
    available_at = _timestamp(_field(action, "available_at"))
    ex_date = _date(_field(action, "ex_date"))
    asof = _timestamp(asof_ts)

    if available_at > asof:
        if ex_date <= asof.date():
            return CorporateActionVisibilityResult(
                CorporateActionVisibilityStatus.UNPROCESSED_BOUNDARY,
                "CA_AVAILABLE_AFTER_APPLICATION_ASOF",
            )
        return CorporateActionVisibilityResult(
            CorporateActionVisibilityStatus.NOT_YET_VISIBLE,
            "CA_NOT_YET_VISIBLE_AT_ASOF",
        )

    if action_type not in supported_types:
        return CorporateActionVisibilityResult(
            CorporateActionVisibilityStatus.UNSUPPORTED_TYPE,
            f"UNSUPPORTED_CA_TYPE:{action_type}",
        )

    return CorporateActionVisibilityResult(
        CorporateActionVisibilityStatus.VISIBLE_APPLICABLE,
        "CA_VISIBLE_AND_APPLICABLE",
    )


def _field(action: Any, name: str) -> Any:
    if isinstance(action, dict):
        return action[name]
    if hasattr(action, name):
        return getattr(action, name)
    try:
        return action[name]
    except Exception as exc:
        raise KeyError(f"corporate action is missing field {name}") from exc


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _timestamp(value).date()


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime()
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    return datetime.fromisoformat(str(value))


__all__ = [
    "CorporateActionVisibilityResult",
    "CorporateActionVisibilityStatus",
    "SUPPORTED_FACTOR_ACTION_TYPES",
    "SUPPORTED_LEDGER_ACTION_TYPES",
    "evaluate_corporate_action_visibility",
]
