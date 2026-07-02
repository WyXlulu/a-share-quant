from __future__ import annotations

import sys
import unittest
from pathlib import Path


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for package_dir in sorted(Path("src").iterdir()):
        if package_dir.is_dir() and (package_dir / "__init__.py").exists():
            suite.addTests(
                loader.discover(
                    start_dir=str(package_dir),
                    pattern="test_*.py",
                    top_level_dir=".",
                )
            )
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    skipped = len(result.skipped)
    failures = len(result.failures)
    errors = len(result.errors)
    expected_failures = len(result.expectedFailures)
    unexpected_successes = len(result.unexpectedSuccesses)
    passed = (
        result.testsRun
        - skipped
        - failures
        - errors
        - expected_failures
        - unexpected_successes
    )
    print(
        "summary: "
        f"total={result.testsRun}, "
        f"passed={passed}, "
        f"skipped={skipped}, "
        f"failures={failures}, "
        f"errors={errors}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
