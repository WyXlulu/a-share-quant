from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.domain import PriceBasis, SOURCE_SEMANTICS_UNVERIFIED_FOR_PIT


class QuarantineManifestTest(unittest.TestCase):
    def test_vendor_adjusted_files_are_quarantined_and_manifested(self) -> None:
        manifest_path = Path("data/quarantine/vendor_adjusted/manifest.json")
        self.assertTrue(manifest_path.exists())

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["price_basis"], PriceBasis.VENDOR_ADJUSTED.value)
        self.assertEqual(manifest["source_semantics"], SOURCE_SEMANTICS_UNVERIFIED_FOR_PIT)
        self.assertEqual(manifest["read_policy"], "FORBIDDEN_FOR_L1_FEATURE_EXECUTION")

        available_files = [item for item in manifest["files"] if item["sha256"]]
        self.assertGreaterEqual(len(available_files), 2)
        for item in available_files:
            self.assertFalse(Path(item["original_path"]).exists())
            self.assertTrue(Path(item["quarantine_path"]).exists())


if __name__ == "__main__":
    unittest.main()
