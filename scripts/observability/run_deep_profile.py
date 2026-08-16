#!/usr/bin/env python3
"""Execute one explicitly authorized deep profile through safe argv builders."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import sys
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.profilers import build_nsys_command, build_strace_command  # noqa: E402


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _capability(path: Path, mode: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid capability manifest: {path}: {type(exc).__name__}") from exc
    tool = "strace" if mode == "strace" else "nsys"
    record = value.get("tools", {}).get(tool, {})
    if record.get("status") != "available":
        raise SystemExit(f"{tool} is not recorded as available in capability manifest")
    executable = record.get("executable")
    if not isinstance(executable, str) or not executable:
        raise SystemExit(f"capability manifest has no executable for {tool}")
    return {"tool": tool, "executable": executable, "record": record}


def _write_manifest(path: Path, value: dict) -> None:
    if path.exists():
        raise SystemExit(f"refusing to overwrite profile manifest: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("strace", "nsys"), required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--observability-level", required=True, choices=("syscall", "nsys", "deep-profile"))
    parser.add_argument("--capability-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    capability = _capability(args.capability_manifest, args.mode)
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite profile output: {args.output}")
    manifest_path = Path(str(args.output) + ".profile_manifest.json")
    target = ["bash", "-lc", args.command]
    if args.mode == "strace":
        plan = build_strace_command(target, output_path=str(args.output), mode=args.observability_level, executable=capability["executable"])
    else:
        plan = build_nsys_command(target, output_path=str(args.output), mode=args.observability_level, executable=capability["executable"])
    capability_hash = hashlib.sha256(args.capability_manifest.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "observability.profile-manifest.v1",
        "profile_id": f"{args.run_id}-{args.attempt_id}-{args.mode}",
        "run_id": args.run_id,
        "attempt_id": args.attempt_id,
        "profiler": args.mode,
        "mode": "deep-profile",
        "observability_level": args.observability_level,
        "command": plan["command"],
        "command_sha256": plan["command_sha256"],
        "capability_manifest": str(args.capability_manifest),
        "capability_manifest_sha256": capability_hash,
        "tool_version_record": capability["record"].get("version"),
        "provenance": "derived",
        "overhead_class": "intrusive_separate_attempt",
        "status": "started",
        "created_at_utc": _utc(),
    }
    _write_manifest(manifest_path, manifest)
    completed = subprocess.run(plan["command"], check=False)
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["status"] = "completed" if completed.returncode == 0 else "failed"
    value["returncode"] = completed.returncode
    value["finished_at_utc"] = _utc()
    manifest_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
