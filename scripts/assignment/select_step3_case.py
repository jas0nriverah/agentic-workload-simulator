#!/usr/bin/env python3
"""Select the highest-ratio eligible Step 1 trajectory deterministically."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "assignment.step3-selection.v1"


class SelectionError(ValueError):
    """The Step 3 selection input is incomplete or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _boolean(value: str, field: str, run_id: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise SelectionError(f"{run_id}: {field} must be true or false")


def _positive(value: str, field: str, run_id: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SelectionError(f"{run_id}: {field} must be numeric") from exc
    lower_ok = number >= 0 if allow_zero else number > 0
    if not math.isfinite(number) or not lower_ok:
        qualifier = "non-negative" if allow_zero else "positive"
        raise SelectionError(f"{run_id}: {field} must be finite and {qualifier}")
    return number


def select(rows: list[dict[str, str]], source_sha256: str) -> dict[str, Any]:
    eligible: list[tuple[float, str, str, str, dict[str, str]]] = []
    seen_runs: set[str] = set()
    for row in rows:
        run_id = row.get("run_id", "")
        if not run_id or run_id in seen_runs:
            raise SelectionError("trajectory rows require unique non-empty run_id values")
        seen_runs.add(run_id)
        if row.get("config_id") != "shared-baseline":
            continue
        if row.get("status") != "completed" or row.get("provenance") not in {
            "measured", "derived_from_measured"
        }:
            continue
        if row.get("suite") not in {"lite", "verified"}:
            raise SelectionError(f"{run_id}: unsupported suite")
        _boolean(row.get("submitted", ""), "submitted", run_id)
        _boolean(row.get("official_resolved", ""), "official_resolved", run_id)
        tool = _positive(row.get("tool_wall_ms", ""), "tool_wall_ms", run_id, allow_zero=True)
        model = _positive(row.get("model_wall_ms", ""), "model_wall_ms", run_id)
        e2e = _positive(row.get("e2e_wall_ms", ""), "e2e_wall_ms", run_id)
        try:
            tool_count = int(row.get("tool_event_count", ""))
            model_count = int(row.get("model_event_count", ""))
        except (TypeError, ValueError) as exc:
            raise SelectionError(f"{run_id}: event counts must be integers") from exc
        if tool_count <= 0 or model_count <= 0:
            raise SelectionError(f"{run_id}: eligible baseline requires tool and model events")
        if tool + model > e2e * 1.05:
            raise SelectionError(f"{run_id}: phase wall time exceeds E2E wall time")
        ratio = _positive(row.get("tool_model_ratio", ""), "tool_model_ratio", run_id, allow_zero=True)
        expected = tool / model
        if not math.isclose(ratio, expected, rel_tol=1e-9, abs_tol=1e-12):
            raise SelectionError(f"{run_id}: ratio does not equal tool_wall_ms/model_wall_ms")
        eligible.append((ratio, row["suite"], row.get("instance_id", ""), run_id, row))
    if not eligible:
        raise SelectionError("no completed, officially evaluated shared-baseline trajectory is eligible")
    eligible.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    ratio, _suite, _instance_id, _run_id, selected = eligible[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_trajectories_sha256": source_sha256,
        "metric": "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
        "selection_policy": "ratio_descending,suite_ascending,instance_id_ascending,run_id_ascending",
        "eligible_count": len(eligible),
        "selected": {
            "run_id": selected["run_id"],
            "suite": selected["suite"],
            "repository": selected.get("repository"),
            "category": selected.get("category"),
            "instance_id": selected.get("instance_id"),
            "config_id": selected["config_id"],
            "repeat_id": selected.get("repeat_id"),
            "tool_wall_ms": float(selected["tool_wall_ms"]),
            "model_wall_ms": float(selected["model_wall_ms"]),
            "e2e_wall_ms": float(selected["e2e_wall_ms"]),
            "tool_model_ratio": ratio,
            "tool_event_count": int(selected["tool_event_count"]),
            "model_event_count": int(selected["model_event_count"]),
            "hardware_id": selected.get("hardware_id"),
            "model_revision": selected.get("model_revision"),
            "swe_agent_revision": selected.get("swe_agent_revision"),
            "swe_bench_revision": selected.get("swe_bench_revision"),
            "command_sha256": selected.get("command_sha256"),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectories", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sha256-sidecar", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    sidecar = args.sha256_sidecar or Path(f"{args.output}.sha256")
    try:
        if args.output == sidecar:
            raise SelectionError("output and SHA-256 sidecar must differ")
        if not args.force and (args.output.exists() or sidecar.exists()):
            raise SelectionError("refusing to overwrite selection output; pass --force")
        source_sha256 = _sha256(args.trajectories)
        with args.trajectories.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        result = select(rows, source_sha256)
        payload = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        _atomic_write(args.output, payload)
        _atomic_write(sidecar, f"{digest}  {args.output.name}\n".encode("utf-8"))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, SelectionError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
