#!/usr/bin/env python3
"""Inventory a first SWE-agent attempt without rewriting or normalizing it."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
try:
    from rehearse_linux import CheckFailure, scan_tree, sha256  # noqa: E402
except ModuleNotFoundError:  # direct loading by a test runner
    helper_spec = importlib.util.spec_from_file_location("rehearse_linux", SCRIPT_DIR / "rehearse_linux.py")
    if not helper_spec or not helper_spec.loader:
        raise
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    CheckFailure, scan_tree, sha256 = helper.CheckFailure, helper.scan_tree, helper.sha256


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def json_inventory(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    raw = path.read_bytes()
    # SWE-agent v1.1.0 writes `.traj` as one JSON document (not JSONL).
    # Keep `.jsonl` as the only line-delimited format so inventory remains
    # lossless and does not reject a genuine first trajectory.
    if suffix in {".json", ".traj"}:
        value = json.loads(raw.decode("utf-8"))
        values = [value]
    else:
        values = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
        if not values:
            raise CheckFailure(f"JSONL artifact is empty: {path}")
    keys = sorted({key for value in values if isinstance(value, dict) for key in value})
    key_types = {
        key: sorted({type_name(value[key]) for value in values if isinstance(value, dict) and key in value})
        for key in keys
    }
    return {
        "format": "json" if suffix in {".json", ".traj"} else "jsonl",
        "records": len(values),
        "top_level_types": sorted({type_name(value) for value in values}),
        "top_level_keys": keys,
        "key_types": key_types,
    }


def classify(path: Path) -> str | None:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in {".traj", ".jsonl"} or "trajectory" in name:
        return "trajectory"
    if suffix in {".json", ".yaml", ".yml"} and (name == "config.json" or "config" in name):
        return "config"
    if suffix == ".json" and ("pred" in name or "prediction" in name):
        return "predictions"
    if suffix in {".log", ".out", ".err"}:
        return "log"
    if suffix in {".json", ".yaml", ".yml"} and ("status" in name or "exit_status" in name):
        return "status"
    return None


def inventory(root: Path, max_file_bytes: int = 64 * 1024 * 1024) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise CheckFailure(f"attempt root does not exist or is not a directory: {root}")
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    records: list[dict[str, Any]] = []
    found: set[str] = set()
    for path in files:
        kind = classify(path)
        if kind is None:
            continue
        if path.stat().st_size > max_file_bytes:
            raise CheckFailure(f"artifact exceeds max size: {path}")
        before = sha256(path)
        record: dict[str, Any] = {
            "path": str(path.relative_to(root)),
            "kind": kind,
            "bytes": path.stat().st_size,
            "sha256": before,
        }
        if path.suffix.lower() in {".json", ".jsonl", ".traj"}:
            record["json"] = json_inventory(path)
        after = sha256(path)
        if before != after:
            raise CheckFailure(f"artifact changed while being inventoried: {path}")
        records.append(record)
        found.add(kind)
    required = {"trajectory", "predictions", "config", "log", "status"}
    missing = sorted(required - found)
    if missing:
        raise CheckFailure(f"attempt inventory is missing required artifact kinds: {missing}")
    # SWE-agent logs and `.traj` records legitimately contain absolute paths
    # from the isolated container. Release-source hygiene rejects private
    # paths separately; this first-trajectory inventory must still preserve
    # and hash those records without rewriting them.
    scan_tree(root, max_file_bytes, reject_absolute_paths=False)
    return {
        "schema_version": "sweagent-output-inventory.v1",
        "status": "pass",
        "root": str(root),
        "byte_preserving": True,
        "files": records,
        "artifact_kinds": sorted(found),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-file-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args(argv)
    try:
        result = inventory(args.root, args.max_file_bytes)
        code = 0
    except (CheckFailure, OSError, ValueError, UnicodeError) as exc:
        result = {"schema_version": "sweagent-output-inventory.v1", "status": "fail", "error": str(exc)}
        code = 1
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
