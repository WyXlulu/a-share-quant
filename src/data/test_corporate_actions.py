from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from src.data.pit_data_portal import PITDataPortal


REQUIRED_COLUMNS = {
    "security_id",
    "ex_date",
    "action_type",
    "cash_dividend_per_share",
    "share_ratio",
    "event_ts",
    "available_at",
    "source_id",
    "snapshot_id",
}


class CorporateActionsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path("data/l2_corporate_actions/corporate_actions.parquet")
        self.manifest_path = Path("data/l2_corporate_actions/manifest.json")
        for path in (self.path, self.manifest_path):
            if not path.exists():
                self.skipTest(f"本地数据文件不存在: {path}")
        self.actions = pd.read_parquet(self.path)
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def test_schema_contains_required_fields_and_timezone_timestamps(self) -> None:
        self.assertTrue(REQUIRED_COLUMNS.issubset(self.actions.columns))
        self.assertIsNotNone(self.actions["ex_date"].dt.tz)
        self.assertIsNotNone(self.actions["event_ts"].dt.tz)
        self.assertIsNotNone(self.actions["available_at"].dt.tz)
        self.assertEqual(str(self.actions["available_at"].dt.tz), "Asia/Shanghai")
        self.assertEqual(self.manifest["evidence_level"], "EXPLORATORY_TAINTED")

    def test_600519_has_historical_cash_dividends_classified_correctly(self) -> None:
        moutai = self.actions.loc[self.actions["security_id"].astype(str).eq("600519")].copy()
        self.assertFalse(moutai.empty)
        cash_rows = moutai.loc[moutai["cash_dividend_per_share"].gt(0)]
        self.assertFalse(cash_rows.empty)
        pure_cash_rows = cash_rows.loc[cash_rows["share_ratio"].eq(0)]
        self.assertFalse(pure_cash_rows.empty)
        self.assertTrue(pure_cash_rows["action_type"].eq("CASH_DIVIDEND").all())

    def test_pit_portal_asof_filters_corporate_actions_available_at(self) -> None:
        moutai = self.actions.loc[self.actions["security_id"].astype(str).eq("600519")].sort_values(
            "available_at"
        )
        target = moutai.iloc[0]
        before = target["available_at"] - pd.Timedelta(seconds=1)
        after = target["available_at"] + pd.Timedelta(seconds=1)

        portal = PITDataPortal({"corporate_actions": self.path})
        before_rows = portal.query("corporate_actions", before, security_ids=["600519"])
        after_rows = portal.query("corporate_actions", after, security_ids=["600519"])

        self.assertTrue(before_rows.empty)
        self.assertFalse(after_rows.empty)
        self.assertTrue(after_rows["available_at"].le(after).all())


if __name__ == "__main__":
    unittest.main()
