from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from src.data.pit_data_portal import PITDataPortal

try:
    from src.data.build_security_master import (
        EXPLORATORY_TAINTED,
        POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY,
        board_from_security_id,
    )
except ModuleNotFoundError as exc:
    if exc.name != "akshare":
        raise
    EXPLORATORY_TAINTED = None
    POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY = None
    board_from_security_id = None


class SecurityMasterTest(unittest.TestCase):
    def setUp(self) -> None:
        if board_from_security_id is None:
            self.skipTest("akshare未安装")

    def _load_local_data(self) -> None:
        self.security_master_path = Path("data/l1_raw/security_master.parquet")
        self.manifest_path = Path("data/l1_raw/security_master_manifest.json")
        self.l1_path = Path("data/l1_raw/daily_bar_raw.parquet")
        for path in (self.security_master_path, self.manifest_path, self.l1_path):
            if not path.exists():
                self.skipTest(f"本地数据文件不存在: {path}")
        self.security_master = pd.read_parquet(self.security_master_path)
        self.l1 = pd.read_parquet(self.l1_path, columns=["security_id"])
        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def test_board_rules_for_known_prefixes(self) -> None:
        self.assertEqual(board_from_security_id("600519"), "主板")
        self.assertEqual(board_from_security_id("000001"), "主板")
        self.assertEqual(board_from_security_id("300750"), "创业板")
        self.assertEqual(board_from_security_id("301269"), "创业板")
        self.assertEqual(board_from_security_id("688111"), "科创板")
        self.assertEqual(board_from_security_id("920000"), "北交所")

    def test_security_ids_match_l1_daily_bar(self) -> None:
        self._load_local_data()
        master_ids = set(self.security_master["security_id"].astype(str).str.zfill(6))
        l1_ids = set(self.l1["security_id"].astype(str).str.zfill(6))
        self.assertEqual(len(master_ids), 300)
        self.assertEqual(master_ids, l1_ids)

    def test_time_varying_status_fields_are_tainted_current_snapshot_only(self) -> None:
        self._load_local_data()
        for column in ["is_st", "status"]:
            self.assertTrue(
                self.security_master[f"{column}_point_in_time_capability"]
                .eq(POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY)
                .all()
            )
            self.assertTrue(self.security_master[f"{column}_evidence_level"].eq(EXPLORATORY_TAINTED).all())
            self.assertEqual(
                self.manifest["field_capabilities"][column]["point_in_time_capability"],
                POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY,
            )
            self.assertEqual(
                self.manifest["field_capabilities"][column]["evidence_level"],
                EXPLORATORY_TAINTED,
            )

    def test_current_st_snapshot_source_is_recorded_or_explicitly_unavailable(self) -> None:
        self._load_local_data()
        current_st_source = self.manifest["source_ids"]["current_st"]
        if current_st_source == "UNAVAILABLE":
            self.assertTrue(self.manifest["source_errors"]["current_st"])
            self.assertTrue(self.security_master["is_st"].eq("UNAVAILABLE").all())
        else:
            self.assertTrue(current_st_source.startswith("akshare."))
            self.assertEqual(
                self.manifest["field_capabilities"]["is_st"]["source"],
                current_st_source,
            )

    def test_available_at_columns_are_present_and_timezone_aware(self) -> None:
        self._load_local_data()
        required_columns = [
            "available_at",
            "board_available_at",
            "list_date_available_at",
            "delist_date_available_at",
            "is_st_available_at",
            "status_available_at",
        ]
        for column in required_columns:
            self.assertIn(column, self.security_master.columns)
            self.assertTrue(self.security_master[column].astype(str).str.endswith("+08:00").all())

        snapshot_available_at = self.manifest["snapshot_available_at"]
        self.assertTrue(self.security_master["is_st_available_at"].eq(snapshot_available_at).all())
        self.assertTrue(self.security_master["status_available_at"].eq(snapshot_available_at).all())
        self.assertIn("available_at_semantics", self.manifest)

        moutai = self.security_master.loc[self.security_master["security_id"].eq("600519")].iloc[0]
        self.assertEqual(moutai["list_date_available_at"], f"{moutai['list_date']}T15:00:00+08:00")
        self.assertEqual(moutai["board_available_at"], moutai["list_date_available_at"])

    def test_pit_portal_queries_security_master_and_masks_current_snapshot_before_available_at(self) -> None:
        self._load_local_data()
        snapshot_available_at = pd.Timestamp(self.manifest["snapshot_available_at"])
        before_snapshot = (snapshot_available_at - pd.Timedelta(seconds=1)).isoformat()
        after_snapshot = (snapshot_available_at + pd.Timedelta(seconds=1)).isoformat()
        portal = PITDataPortal()

        before = portal.query(
            "security_master",
            before_snapshot,
            security_ids=["600519"],
            columns=["security_id", "board", "is_st", "status"],
        )
        self.assertEqual(before["security_id"].tolist(), ["600519"])
        self.assertFalse(before["board"].isna().any())
        self.assertTrue(before["is_st"].isna().all())
        self.assertTrue(before["status"].isna().all())
        self.assertTrue(before["is_st_point_in_time_capability"].eq(POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY).all())

        after = portal.query(
            "security_master",
            after_snapshot,
            security_ids=["600519"],
            columns=["security_id", "is_st", "status"],
        )
        self.assertEqual(after["security_id"].tolist(), ["600519"])
        self.assertFalse(after["is_st"].isna().any())
        self.assertFalse(after["status"].isna().any())
        self.assertTrue(after["status_evidence_level"].eq(EXPLORATORY_TAINTED).all())


if __name__ == "__main__":
    unittest.main()
