from __future__ import annotations

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

import pandas as pd

from src.data.corporate_action_availability import resolve_ca_available_at
from src.golden_slice.manifest import (
    GoldenSliceManifestError,
    assert_frozen_and_consistent,
)
from src.market_calendar import TradingCalendar


MANIFEST_PATH = Path(__file__).with_name("golden_slice_manifest.json")


def _load_manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


class VerifiedCorporateActionManifestTest(unittest.TestCase):
    def test_frozen_manifest_contains_76_verified_actions_and_is_consistent(self) -> None:
        manifest = _load_manifest()

        self.assertEqual(len(manifest["verified_corporate_actions"]), 76)
        assert_frozen_and_consistent(manifest)

    def test_tampering_verified_cash_amount_breaks_manifest_hash(self) -> None:
        manifest = _load_manifest()
        tampered = copy.deepcopy(manifest)
        tampered["verified_corporate_actions"][0]["cash_dividend_per_share"] += 0.01

        with self.assertRaisesRegex(GoldenSliceManifestError, "hash mismatch"):
            assert_frozen_and_consistent(tampered)

    def test_all_76_actions_pass_time_unknown_availability_resolver(self) -> None:
        manifest = _load_manifest()
        actions = manifest["verified_corporate_actions"]

        for action in actions:
            expected = pd.Timestamp(action["derived_available_at"])
            # The expected next trading date was materialized from the frozen real
            # TradingCalendar. A one-date calendar keeps this test data-independent
            # while still exercising the same fail-closed resolver for every action.
            calendar = TradingCalendar((expected.date(),))
            actual = resolve_ca_available_at(action, calendar)
            self.assertEqual(actual, expected, action["source_pdf_filename"])

        self.assertEqual(len(actions), 76)

    def test_gree_2021_adjusted_deduction_is_distinct_from_cash_paid(self) -> None:
        manifest = _load_manifest()
        action = next(
            action
            for action in manifest["verified_corporate_actions"]
            if action["security_id"] == "000651"
            and action["disclosure_date"] == "2021-08-14"
        )

        cash_paid = Decimal(str(action["cash_dividend_per_share"]))
        ex_right_deduction = Decimal(
            str(action["ex_right_cash_deduction_per_share"])
        )

        self.assertEqual(cash_paid, Decimal("3.0"))
        self.assertEqual(ex_right_deduction, Decimal("2.784787"))
        self.assertNotEqual(cash_paid, ex_right_deduction)

    def test_hengrui_has_exactly_three_stock_dividends_at_point_two(self) -> None:
        manifest = _load_manifest()
        nonzero_share_actions = [
            action
            for action in manifest["verified_corporate_actions"]
            if Decimal(str(action["share_ratio"])) != Decimal("0")
        ]

        self.assertEqual(len(nonzero_share_actions), 3)
        self.assertEqual(
            {action["security_id"] for action in nonzero_share_actions},
            {"600276"},
        )
        self.assertTrue(
            all(
                Decimal(str(action["share_ratio"])) == Decimal("0.2")
                for action in nonzero_share_actions
            )
        )


if __name__ == "__main__":
    unittest.main()
