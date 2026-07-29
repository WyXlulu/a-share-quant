from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd

from .contracts import DataContractError


_MISSING = object()


def extract_rights_issue_terms(action: object) -> tuple[Decimal, Decimal | None]:
    """Return the rights ratio and price from either supported CA schema."""
    ratio_value = _first_present_value(action, ("rights_ratio", "share_ratio"))
    if ratio_value is _MISSING:
        raise DataContractError(
            "RIGHTS_ISSUE missing rights_ratio/share_ratio; cannot construct PIT factor"
        )
    rights_ratio = _as_decimal(ratio_value, "rights_ratio/share_ratio")

    price_value = _first_present_value(
        action,
        ("rights_price", "rights_price_per_share"),
    )
    rights_price = (
        None
        if price_value is _MISSING
        else _as_decimal(price_value, "rights_price/rights_price_per_share")
    )
    if rights_ratio > Decimal("0") and rights_price is None:
        raise DataContractError(
            "RIGHTS_ISSUE missing rights_price/rights_price_per_share; "
            "cannot construct PIT factor"
        )
    return rights_ratio, rights_price


def _first_present_value(action: object, field_names: tuple[str, ...]) -> Any:
    for field_name in field_names:
        value = _field_value(action, field_name)
        if value is not _MISSING and not _is_null(value):
            return value
    return _MISSING


def _field_value(action: object, field_name: str) -> Any:
    if isinstance(action, Mapping):
        return action.get(field_name, _MISSING)
    return getattr(action, field_name, _MISSING)


def _is_null(value: object) -> bool:
    try:
        missing = pd.isna(value)
        return bool(missing)
    except (TypeError, ValueError):
        raise DataContractError("RIGHTS_ISSUE fields must be scalar values") from None


def _as_decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DataContractError(
            f"RIGHTS_ISSUE {field_name} must be a finite decimal"
        ) from None
    if not result.is_finite():
        raise DataContractError(
            f"RIGHTS_ISSUE {field_name} must be a finite decimal"
        )
    return result
