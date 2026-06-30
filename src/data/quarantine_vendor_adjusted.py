from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from src.domain import PriceBasis, SOURCE_SEMANTICS_UNVERIFIED_FOR_PIT


RAW_DIR = Path("data/raw")
QUARANTINE_DIR = Path("data/quarantine/vendor_adjusted")
MANIFEST_PATH = QUARANTINE_DIR / "manifest.json"

VENDOR_ADJUSTED_FILES = [
    "600519.parquet",
    "hs300_daily.parquet",
    "hs300_daily_10y.parquet",
]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quarantine_vendor_adjusted() -> dict[str, object]:
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    files: list[dict[str, object]] = []
    for file_name in VENDOR_ADJUSTED_FILES:
        source_path = RAW_DIR / file_name
        destination_path = QUARANTINE_DIR / file_name

        if source_path.exists():
            shutil.move(str(source_path), str(destination_path))
            status = "moved"
        elif destination_path.exists():
            status = "already_quarantined"
        else:
            status = "missing"

        if destination_path.exists():
            files.append(
                {
                    "file_name": file_name,
                    "original_path": source_path.as_posix(),
                    "quarantine_path": destination_path.as_posix(),
                    "status": status,
                    "size_bytes": destination_path.stat().st_size,
                    "sha256": _sha256(destination_path),
                }
            )
        else:
            files.append(
                {
                    "file_name": file_name,
                    "original_path": source_path.as_posix(),
                    "quarantine_path": destination_path.as_posix(),
                    "status": status,
                    "size_bytes": None,
                    "sha256": None,
                }
            )

    manifest: dict[str, object] = {
        "manifest_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "price_basis": PriceBasis.VENDOR_ADJUSTED.value,
        "source_semantics": SOURCE_SEMANTICS_UNVERIFIED_FOR_PIT,
        "read_policy": "FORBIDDEN_FOR_L1_FEATURE_EXECUTION",
        "files": files,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    manifest = quarantine_vendor_adjusted()
    moved_count = sum(1 for item in manifest["files"] if item["status"] == "moved")
    available_count = sum(1 for item in manifest["files"] if item["sha256"])
    print(f"quarantined_available={available_count}")
    print(f"moved_now={moved_count}")
    print(f"manifest={MANIFEST_PATH}")


if __name__ == "__main__":
    main()
