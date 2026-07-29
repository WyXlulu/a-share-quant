from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace

from src.domain.corporate_action_inputs import extract_rights_issue_terms
from src.domain.contracts import DataContractError


class CorporateActionInputsTest(unittest.TestCase):
    def test_extracts_production_rights_issue_schema(self) -> None:
        action = SimpleNamespace(
            share_ratio="0.2",
            rights_price_per_share="6.5",
        )

        self.assertEqual(
            extract_rights_issue_terms(action),
            (Decimal("0.2"), Decimal("6.5")),
        )

    def test_prefers_canonical_rights_fields(self) -> None:
        action = {
            "rights_ratio": "0.3",
            "share_ratio": "0.2",
            "rights_price": "5.0",
            "rights_price_per_share": "6.5",
        }

        self.assertEqual(
            extract_rights_issue_terms(action),
            (Decimal("0.3"), Decimal("5.0")),
        )

    def test_positive_rights_ratio_without_price_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            DataContractError,
            "missing rights_price/rights_price_per_share",
        ):
            extract_rights_issue_terms({"share_ratio": "0.2"})

    def test_missing_rights_ratio_fields_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            DataContractError,
            "missing rights_ratio/share_ratio",
        ):
            extract_rights_issue_terms({"rights_price_per_share": "6.5"})


if __name__ == "__main__":
    unittest.main()
