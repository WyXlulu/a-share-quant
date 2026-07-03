from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

# Sources retrieved on 2026-07-03:
# - 上海证券交易所交易规则（2023年修订）, section 4.3.2
#   https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20250519_10779396.shtml
# - 深圳证券交易所交易规则（2023年修订）, section 4.4.2
#   https://www.szse.cn/lawrules/rule/stock/trade/t20230217_598773.html
#
# Official formula:
# reference = ((prev_close - cash_dividend) + rights_price * share_change_ratio)
#             / (1 + share_change_ratio)
#
# The function below decomposes the official share-change ratio into a zero-price
# split/transfer component and a paid rights-issue component, so the numerator
# only adds the paid rights consideration k * Q.


class CorporateActionPricingRuleError(ValueError):
    """Raised when no dated official pricing rule can be resolved."""


@dataclass(frozen=True)
class CorporateActionReferencePriceRule:
    effective_date: date
    version: str
    source_name: str
    source_url: str


KNOWN_REFERENCE_PRICE_RULES: tuple[CorporateActionReferencePriceRule, ...] = (
    CorporateActionReferencePriceRule(
        effective_date=date(2023, 2, 17),
        version="cn_a_share_ex_right_reference_price_2023",
        source_name="上海证券交易所交易规则（2023年修订）第4.3.2条；"
        "深圳证券交易所交易规则（2023年修订）第4.4.2条",
        source_url="https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20250519_10779396.shtml; "
        "https://www.szse.cn/lawrules/rule/stock/trade/t20230217_598773.html",
    ),
    CorporateActionReferencePriceRule(
        effective_date=date(2026, 7, 6),
        version="cn_a_share_ex_right_reference_price_2026",
        source_name="上海证券交易所交易规则（2026年修订）第4.3.2条；"
        "深圳证券交易所交易规则（2026年修订）第4.4.2条",
        source_url="https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml; "
        "https://www.szse.cn/lawrules/rule/trade/current/t20260424_620190.html",
    ),
)


def calculate_ex_right_reference_price(
    pricing_date: date,
    prev_close: Decimal | int | str,
    cash_dividend_pretax: Decimal | int | str,
    split_transfer_ratio: Decimal | int | str,
    rights_ratio: Decimal | int | str,
    rights_price: Decimal | int | str,
) -> Decimal:
    """Return the official A-share ex-right/ex-dividend reference price."""

    resolve_reference_price_rule(pricing_date)
    previous_close = _decimal(prev_close)
    cash_dividend = _decimal(cash_dividend_pretax)
    split_ratio = _decimal(split_transfer_ratio)
    paid_rights_ratio = _decimal(rights_ratio)
    paid_rights_price = _decimal(rights_price)

    denominator = Decimal("1") + split_ratio + paid_rights_ratio
    if denominator <= Decimal("0"):
        raise CorporateActionPricingRuleError(
            "invalid corporate-action ratios for reference price: denominator <= 0"
        )

    numerator = previous_close - cash_dividend + paid_rights_price * paid_rights_ratio
    return numerator / denominator


def resolve_reference_price_rule(pricing_date: date) -> CorporateActionReferencePriceRule:
    active_rules = [
        rule
        for rule in KNOWN_REFERENCE_PRICE_RULES
        if rule.effective_date <= pricing_date
    ]
    if not active_rules:
        earliest = min(rule.effective_date for rule in KNOWN_REFERENCE_PRICE_RULES)
        raise CorporateActionPricingRuleError(
            "no known official ex-right reference-price rule for "
            f"pricing_date={pricing_date}; earliest_known_effective_date={earliest}"
        )
    return sorted(active_rules, key=lambda rule: rule.effective_date)[-1]


def _decimal(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "CorporateActionPricingRuleError",
    "CorporateActionReferencePriceRule",
    "KNOWN_REFERENCE_PRICE_RULES",
    "calculate_ex_right_reference_price",
    "resolve_reference_price_rule",
]
