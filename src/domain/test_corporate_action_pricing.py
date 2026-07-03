from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal

from src.domain.corporate_action_pricing import (
    CorporateActionPricingRuleError,
    calculate_ex_right_reference_price,
)


class CorporateActionReferencePriceRuleTest(unittest.TestCase):
    """Official formula source: SSE 2023 rule 4.3.2 and SZSE 2023 rule 4.4.2."""

    def test_cash_dividend_uses_official_formula(self) -> None:
        # ((10.00 - 1.00) + 0 * 0) / (1 + 0 + 0) = 9.00
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("10.00"),
                Decimal("1.00"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            ),
            Decimal("9.00"),
        )

    def test_split_transfer_uses_official_denominator(self) -> None:
        # ((12.00 - 0) + 0 * 0) / (1 + 0.5 + 0) = 8.00
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("12.00"),
                Decimal("0"),
                Decimal("0.5"),
                Decimal("0"),
                Decimal("0"),
            ),
            Decimal("8.0"),
        )

    def test_rights_issue_uses_official_paid_rights_term(self) -> None:
        # ((12.00 - 0) + 6.00 * 0.5) / (1 + 0 + 0.5) = 10.00
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("12.00"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0.5"),
                Decimal("6.00"),
            ),
            Decimal("1E+1"),
        )

    def test_cash_and_split_transfer_combination_uses_official_formula(self) -> None:
        # ((11.00 - 1.00) + 0 * 0) / (1 + 0.25 + 0) = 8.00
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("11.00"),
                Decimal("1.00"),
                Decimal("0.25"),
                Decimal("0"),
                Decimal("0"),
            ),
            Decimal("8"),
        )

    def test_cash_split_and_rights_combination_uses_full_official_formula(self) -> None:
        # Official SZSE 2023 rule 4.4.2 formula hand calculation:
        # numerator = (10 - 0.5) + 6 * 0.2 = 10.7
        # denominator = 1 + 0.3 + 0.2 = 1.5
        # reference = 10.7 / 1.5 = 107 / 15.
        # If the denominator is incorrectly wired as 1+s (=1.3), the result is
        # about 8.2308; if wired as 1+r (=1.2), it is about 8.9167.
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("10"),
                Decimal("0.5"),
                Decimal("0.3"),
                Decimal("0.2"),
                Decimal("6"),
            ),
            Decimal("107") / Decimal("15"),
        )

    def test_split_transfer_remains_unchanged_when_rights_inputs_are_zero(self) -> None:
        # Official formula hand calculation:
        # ((10 - 0) + 0 * 0) / (1 + 0.3 + 0) = 10 / 1.3 = 100 / 13.
        self.assertEqual(
            calculate_ex_right_reference_price(
                date(2023, 2, 17),
                Decimal("10"),
                Decimal("0"),
                Decimal("0.3"),
                Decimal("0"),
                Decimal("0"),
            ),
            Decimal("100") / Decimal("13"),
        )

    def test_pricing_before_earliest_known_rule_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            CorporateActionPricingRuleError,
            "earliest_known_effective_date=2023-02-17",
        ):
            calculate_ex_right_reference_price(
                date(2023, 2, 16),
                Decimal("10.00"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
            )


if __name__ == "__main__":
    unittest.main()
