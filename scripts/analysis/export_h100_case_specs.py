#!/usr/bin/env python3
"""Export the case catalog from an external H100 matrix run.

The catalog keeps the identity and planned settings for every case, including
cases that failed before producing ``case_result.json`` and the active case.
It is intentionally separate from the measured result JSONL files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def records(root: Path) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    case_dirs = [
        path
        for path in (root / "cases").iterdir()
        if path.is_dir() and path.name.isdigit()
    ]
    for case_dir in sorted(case_dirs, key=lambda path: int(path.name)):
        spec_path = case_dir / "case_spec.json"
        if not spec_path.is_file():
            continue
        record = json.loads(spec_path.read_text(encoding="utf-8"))
        result_path = case_dir / "case_result.json"
        record.update(
            {
                "case_directory": case_dir.relative_to(root).as_posix(),
                "case_index": int(case_dir.name),
                "case_sha256": sha256(spec_path),
                "case_result_present": result_path.is_file(),
                "case_result_sha256": sha256(result_path) if result_path.is_file() else None,
            }
        )
        output.append(record)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    root = args.raw_root.expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"raw root is not a directory: {root}")
    exported = records(root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + ".tmp")
    temporary.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in exported),
        encoding="utf-8",
    )
    temporary.replace(args.output)
    print(f"exported {len(exported)} case specs from {root}")


if __name__ == "__main__":
    main()
