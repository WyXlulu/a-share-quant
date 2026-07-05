from __future__ import annotations

import copy
import unittest

from src.golden_slice.manifest import (
    GoldenSliceManifestError,
    assert_frozen_and_consistent,
    build_unfrozen_manifest,
    freeze,
)


class GoldenSliceManifestGovernanceTest(unittest.TestCase):
    def test_unfrozen_manifest_fails_closed(self) -> None:
        manifest = build_unfrozen_manifest()

        with self.assertRaisesRegex(GoldenSliceManifestError, "not frozen"):
            assert_frozen_and_consistent(manifest)

    def test_frozen_manifest_tamper_fails_closed(self) -> None:
        manifest = freeze(
            build_unfrozen_manifest(),
            frozen_at="2026-07-05T15:00:00+08:00",
        )
        tampered = copy.deepcopy(manifest)
        tampered["security_ids"].append("600000")

        with self.assertRaisesRegex(GoldenSliceManifestError, "hash mismatch"):
            assert_frozen_and_consistent(tampered)

    def test_frozen_manifest_without_changes_passes(self) -> None:
        manifest = freeze(
            build_unfrozen_manifest(),
            frozen_at="2026-07-05T15:00:00+08:00",
        )

        assert_frozen_and_consistent(manifest)


if __name__ == "__main__":
    unittest.main()
