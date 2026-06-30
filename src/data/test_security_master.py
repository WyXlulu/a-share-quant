from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from src.data.build_security_master import (
    EXPLORATORY_TAINTED,
    POINT_IN_TIME_CURRENT_SNAPSHOT_ONLY,
    board_from_security_id,
)


class SecurityMasterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.security_master_path = Path("data/l1_raw/security_master.parquet")
        cls.manifest_path = Path("data/l1_raw/security_master_manifest.json")
        cls.l1_path = Path("data/l1_raw/daily_bar_raw.parquet")
        cls.security_master = pd.read_parquet(cls.security_master_path)
        cls.l1 = pd.read_parquet(cls.l1_path, columns=["security_id"])
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    def test_board_rules_for_known_prefixes(self) -> None:
        self.assertEqual(board_from_security_id("600519"), "主板")
        self.assertEqual(board_from_security_id("000001"), "主板")
        self.assertEqual(board_from_security_id("300750"), "创业板")
        self.assertEqual(board_from_security_id("301269"), "创业板")
        self.assertEqual(board_from_security_id("688111"), "科创板")
        self.assertEqual(board_from_security_id("920000"), "北交所")

    def test_security_ids_match_l1_daily_bar(self) -> None:
        master_ids = set(self.security_master["security_id"].astype(str).str.zfill(6))
        l1_ids = set(self.l1["security_id"].astype(str).str.zfill(6))
        self.assertEqual(len(master_ids), 300)
        self.assertEqual(master_ids, l1_ids)

    def test_time_varying_status_fields_are_tainted_current_snapshot_only(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
