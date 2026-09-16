"""Stop and retain only containers bearing one explicitly supplied attempt label."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

OWNER_LABEL = "agentic.assignment.owner"


def cleanup_owned_containers(owner: str, artifact_dir: Path) -> dict[str, Any]:
    """Never infer ownership from instance names, timestamps, or inventory diffs.

    This routine retains stopped containers and images. The exact full ID and
    owner label are rechecked before mutation. Every call gets its own evidence
    directory so a later finalization cannot overwrite an earlier snapshot.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", owner):
        raise ValueError("container cleanup requires an attempt UUID")
    root = artifact_dir / f"{time.time_ns()}-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {"schema_version": "assignment-owned-docker-cleanup.v1",
        "owner": owner, "started_epoch_ns": time.time_ns(), "containers": [],
        "errors": [], "cleanup_complete": False, "retained": True}

    def run(args: list[str], label: str, *, timeout: float = 15) -> subprocess.CompletedProcess[bytes] | None:
        try:
            result = subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["errors"].append({"operation": label, "type": type(exc).__name__, "reason": str(exc)})
            if isinstance(exc, subprocess.TimeoutExpired):
                (root / f"{label}.stdout").write_bytes(exc.stdout or b"")
                (root / f"{label}.stderr").write_bytes(exc.stderr or b"")
            return None
        (root / f"{label}.stdout").write_bytes(result.stdout)
        (root / f"{label}.stderr").write_bytes(result.stderr)
        if result.returncode:
            report["errors"].append({"operation": label, "returncode": result.returncode})
        return result

    def inspect(cid: str, label: str) -> dict[str, Any] | None:
        # Deliberately exclude Config.Env, command arguments, and arbitrary
        # labels, which may contain credentials owned by the application.
        fmt = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"created":{{json .Created}},"owner":{{json (index .Config.Labels "agentic.assignment.owner")}},"running":{{json .State.Running}},"status":{{json .State.Status}},"started":{{json .State.StartedAt}},"finished":{{json .State.FinishedAt}},"exit_code":{{json .State.ExitCode}},"pid":{{json .State.Pid}}}'
        result = run(["inspect", "--format", fmt, cid], label)
        if result is None or result.returncode:
            return None
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            report["errors"].append({"operation": label, "reason": "invalid inspect response"})
            return None
        if value.get("id") != cid or value.get("owner") != owner:
            report["errors"].append({"operation": label, "reason": "container identity or label mismatch"})
            return None
        return value

    try:
        listed = run(["ps", "-aq", "--no-trunc", "--filter", f"label={OWNER_LABEL}={owner}"], "list")
        if listed is None or listed.returncode:
            return report
        ids = listed.stdout.decode("ascii", "strict").split()
        if any(not re.fullmatch(r"[0-9a-f]{64}", cid) for cid in ids):
            report["errors"].append({"operation": "list", "reason": "noncanonical container ID"})
            return report
        all_stopped = True
        for cid in sorted(set(ids)):
            before = inspect(cid, f"{cid}.before")
            if before is None:
                all_stopped = False
                continue
            run(["logs", "--timestamps", cid], f"{cid}.logs.before")
            if before["running"]:
                stop = run(["stop", "--time", "2", cid], f"{cid}.stop", timeout=8)
                if stop is None or stop.returncode:
                    # Verify the immutable full ID and label again before KILL.
                    current = inspect(cid, f"{cid}.before-kill")
                    if current is not None and current["running"]:
                        run(["kill", cid], f"{cid}.kill", timeout=8)
            after = inspect(cid, f"{cid}.after")
            run(["logs", "--timestamps", cid], f"{cid}.logs.after")
            report["containers"].append({"id": cid, "before": before, "after": after})
            all_stopped = all_stopped and after is not None and after.get("running") is False
        report["cleanup_complete"] = all_stopped and not report["errors"]
        return report
    finally:
        report["ended_epoch_ns"] = time.time_ns()
        (root / "cleanup.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
