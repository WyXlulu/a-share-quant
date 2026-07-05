from __future__ import annotations

import unittest

import pandas as pd

from src.data.akshare_adapter import _build_normalized_corporate_actions
from src.domain import DataContractError


class AkshareCorporateActionAdapterTest(unittest.TestCase):
    def test_corporate_action_with_effect_but_missing_ex_date_fails_closed(self) -> None:
        with self.assertRaisesRegex(DataContractError, "must include ex_date"):
            _build_normalized_corporate_actions(
                symbol="600000",
                ex_date=pd.Series([pd.NaT]),
                cash_per_10=pd.Series([10]),
                share_per_10=pd.Series([0]),
                source_id="fixture.missing_ex_date",
                record_date=None,
                announcement_date=pd.Series(["2026-01-01"]),
                description=pd.Series(["cash dividend missing ex date"]),
            )


if __name__ == "__main__":
    unittest.main()
