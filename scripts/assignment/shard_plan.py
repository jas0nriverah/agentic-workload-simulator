#!/usr/bin/env python3
"""Create immutable, coverage-bound shards of a sealed assignment plan.

Sharding parallelizes independent SWE-bench cases while preserving the
assignment's serial reason-action loop inside each case.  The generated shard
plans are not independent experiments: every one is bound to the exact parent
plan and the manifest proves that their union is complete and disjoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping


SCHEMA_VERSION = "assignment-steps-1-3-plan.v1"
SHARDS_SCHEMA = "assignment-plan-shards.v1"
ASSIGNMENT = "round_robin_plan_order_v1"


class ShardError(ValueError):
    """The parent plan or requested shard set is unsafe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ShardError(message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        raise ShardError(f"cannot hash {path}: {exc}") from exc


def _verify_sidecar(path: Path, sidecar: Path, label: str) -> str:
    _require(path.is_file() and not path.is_symlink(), f"{label} is not a regular file: {path}")
    _require(sidecar.is_file() and not sidecar.is_symlink(), f"{label} sidecar is unavailable: {sidecar}")
    digest = _sha256(path)
    try:
        claim = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShardError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    _require(claim == f"{digest}  {path.name}\n", f"{label} sidecar does not match exact bytes")
    return digest


def _read_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ShardError(f"cannot read parent plan {path}: {exc}") from exc
    _require(len(lines) >= 2, "parent plan must contain a header and at least one case")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        _require(bool(line.strip()), f"parent plan has a blank line at {line_number}")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ShardError(f"parent plan line {line_number} is not valid JSON") from exc
        _require(isinstance(value, dict), f"parent plan line {line_number} is not an object")
        rows.append(value)
    header, cases = rows[0], rows[1:]
    _require(header.get("record_type") == "plan", "parent plan header is invalid")
    _require(header.get("schema_version") == SCHEMA_VERSION, "parent plan schema is unsupported")
    _require(header.get("planning_only") is True, "parent plan must be planning_only")
    _require(isinstance(header.get("plan_id"), str) and header["plan_id"], "parent plan_id is required")
    _require(header.get("concurrency") == 1, "parent plan must enforce concurrency=1")
    _require(header.get("execution_case_count") == len(cases), "parent plan case count is inconsistent")
    seen: set[str] = set()
    for index, case in enumerate(cases, 2):
        _require(case.get("record_type") == "case", f"parent case {index} is invalid")
        _require(case.get("schema_version") == SCHEMA_VERSION, f"parent case {index} schema is invalid")
        _require(case.get("plan_id") == header["plan_id"], f"parent case {index} plan identity is invalid")
        key = case.get("resume_key")
        _require(isinstance(key, str) and key and key not in seen, f"parent case {index} has a duplicate resume_key")
        seen.add(key)
        _require(case.get("concurrency") == 1, f"parent case {index} does not enforce concurrency=1")
    return header, cases


def _atomic_write(path: Path, payload: bytes, *, force: bool) -> None:
    if not force:
        _require(not path.exists(), f"refusing to overwrite {path}; pass --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_json(path: Path, value: Mapping[str, Any], *, force: bool) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write(path, payload, force=force)
    digest = _sha256_bytes(payload)
    _atomic_write(
        Path(str(path) + ".sha256"),
        f"{digest}  {path.name}\n".encode("ascii"),
        force=force,
    )
    return digest


def _render_plan(header: Mapping[str, Any], cases: list[Mapping[str, Any]]) -> bytes:
    return (
        "\n".join([_canonical(header), *(_canonical(case) for case in cases)]) + "\n"
    ).encode("utf-8")


def _coverage_sha256(keys: list[str]) -> str:
    return _sha256_bytes((_canonical(sorted(keys)) + "\n").encode("utf-8"))


def shard(
    parent_plan: Path,
    *,
    parent_sidecar: Path,
    shard_count: int,
    output_dir: Path,
    manifest_path: Path,
    force: bool = False,
) -> dict[str, Any]:
    _require(shard_count > 0, "shard_count must be positive")
    parent_plan = parent_plan.expanduser().resolve()
    parent_sidecar = parent_sidecar.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    parent_sha = _verify_sidecar(parent_plan, parent_sidecar, "parent plan")
    header, cases = _read_plan(parent_plan)
    _require(shard_count <= len(cases), "shard_count cannot exceed parent case count")
    _require(
        not any(key in header for key in (
            "parent_plan_sha256",
            "shard_index",
            "shard_count",
            "shard_assignment",
        )),
        "cannot shard an already-sharded plan",
    )

    reconciliation = header.get("reconciliation")
    parent_original_count = len(cases)
    if isinstance(reconciliation, Mapping):
        value = reconciliation.get("original_case_count")
        _require(isinstance(value, int) and value >= len(cases), "parent reconciliation original count is invalid")
        parent_original_count = value

    output_dir.mkdir(parents=True, exist_ok=True)
    shard_records: list[dict[str, Any]] = []
    all_keys: list[str] = []
    for index in range(shard_count):
        selected = [case for position, case in enumerate(cases) if position % shard_count == index]
        all_keys.extend(str(case["resume_key"]) for case in selected)
        shard_header = {
            key: value
            for key, value in header.items()
            if key != "reconciliation"
        }
        shard_header.update({
            "parent_plan_sha256": parent_sha,
            "parent_execution_case_count": len(cases),
            "parent_original_case_count": parent_original_count,
            "shard_index": index,
            "shard_count": shard_count,
            "shard_assignment": ASSIGNMENT,
            "execution_case_count": len(selected),
        })
        shard_path = output_dir / f"shard-{index:03d}-of-{shard_count:03d}.jsonl"
        shard_sidecar = Path(str(shard_path) + ".sha256")
        if force:
            _require(not shard_sidecar.is_symlink(), f"refusing symlink sidecar: {shard_sidecar}")
        payload = _render_plan(shard_header, selected)
        _atomic_write(shard_path, payload, force=force)
        shard_sha = _sha256_bytes(payload)
        _atomic_write(
            shard_sidecar,
            f"{shard_sha}  {shard_path.name}\n".encode("ascii"),
            force=force,
        )
        shard_records.append({
            "index": index,
            "path": os.path.relpath(shard_path, manifest_path.parent),
            "sha256": shard_sha,
            "case_count": len(selected),
            "resume_keys_sha256": _coverage_sha256([str(case["resume_key"]) for case in selected]),
        })

    _require(len(all_keys) == len(set(all_keys)), "shard assignment produced duplicate resume keys")
    manifest = {
        "schema_version": SHARDS_SCHEMA,
        "assignment": ASSIGNMENT,
        "parent_plan_path": os.path.relpath(parent_plan, manifest_path.parent),
        "parent_plan_sha256": parent_sha,
        "parent_execution_case_count": len(cases),
        "parent_original_case_count": parent_original_count,
        "plan_id": header["plan_id"],
        "shard_count": shard_count,
        "shards": shard_records,
        "coverage_sha256": _coverage_sha256(all_keys),
    }
    _write_json(manifest_path, manifest, force=force)
    return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", required=True, type=Path)
    result.add_argument("--plan-sha256-sidecar", type=Path)
    result.add_argument("--shard-count", required=True, type=int)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--manifest", required=True, type=Path)
    result.add_argument("--force", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    sidecar = args.plan_sha256_sidecar or Path(str(args.plan) + ".sha256")
    try:
        manifest = shard(
            args.plan,
            parent_sidecar=sidecar,
            shard_count=args.shard_count,
            output_dir=args.output_dir,
            manifest_path=args.manifest,
            force=args.force,
        )
        print(json.dumps(manifest, sort_keys=True))
        return 0
    except (OSError, ShardError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
