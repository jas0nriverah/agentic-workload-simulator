#!/usr/bin/env python3
"""Create a deterministic SHA-256 inventory for an external H100 run.

The raw run stays on durable shared storage.  This inventory makes every
regular file in that run addressable and verifiable without copying the raw
trajectories, logs, or profiler artifacts into Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_inventory(root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        directory_path = Path(directory)
        for filename in filenames:
            path = directory_path / filename
            if not path.is_file():
                continue
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    return {
        "schema_version": "h100-live-matrix-raw-inventory.v1",
        "root": str(root),
        "file_count": len(files),
        "total_bytes": sum(int(entry["size_bytes"]) for entry in files),
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.raw_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"raw root is not a directory: {root}")
    inventory = build_inventory(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    temporary.replace(args.output)
    print(
        f"inventoried {inventory['file_count']} files "
        f"({inventory['total_bytes']} bytes) from {root}"
    )


if __name__ == "__main__":
    main()
