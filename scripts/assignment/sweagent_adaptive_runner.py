#!/usr/bin/env python3
"""Run pinned SWE-agent with the sealed adaptive tool-event hook attached.

The request proxy owns model-event prediction/reveal.  This wrapper owns tool
events and the trajectory boundary.  Both processes use the same locked,
append-only protocol root, so every event prediction is durable before its
measured label can be appended.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.assignment.adaptive_runtime import AdaptiveRuntime, AdaptiveRuntimeError  # noqa: E402


CONFIG_ENV = "ASSIGNMENT_ADAPTIVE_RUNTIME_CONFIG"


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_frozen_score(root: Path, score: dict[str, Any]) -> tuple[Path, str]:
    payload = _canonical_bytes(score)
    digest = hashlib.sha256(payload).hexdigest()
    path = root / "adaptive_score_report.json"
    sidecar = Path(str(path) + ".sha256")
    sidecar_payload = f"{digest}  {path.name}\n".encode("ascii")
    if path.exists() or sidecar.exists():
        if path.is_file() and sidecar.is_file() and path.read_bytes() == payload and sidecar.read_bytes() == sidecar_payload:
            return path, digest
        raise AdaptiveRuntimeError(f"refusing to overwrite frozen adaptive score: {path}")
    root.mkdir(parents=True, exist_ok=True)
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, contents in ((path, payload), (sidecar, sidecar_payload)):
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=root)
            candidate = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.append((candidate, destination))
        for candidate, destination in temporary:
            os.replace(candidate, destination)
    finally:
        for candidate, _destination in temporary:
            if candidate.exists():
                candidate.unlink()
    return path, digest


def build_hook(runtime: AdaptiveRuntime, hook_base: type) -> Any:
    """Build against the pinned SWE-agent hook type without importing it offline."""

    class AdaptiveToolHook(hook_base):
        def __init__(self) -> None:
            self.started_mono_ns: int | None = None
            self.next_tool_ordinal = 0
            self.pending_tool_id: str | None = None

        def on_run_start(self) -> None:
            if self.started_mono_ns is not None:
                raise AdaptiveRuntimeError("adaptive trajectory was started more than once")
            self.started_mono_ns = time.monotonic_ns()

        def on_action_started(self, *, step: Any) -> None:
            if self.pending_tool_id is not None:
                raise AdaptiveRuntimeError("prior tool event has no revealed label")
            identifier = f"{runtime.run_id}:tool:{self.next_tool_ordinal:06d}"
            runtime.predict_tool_action(identifier, step.action)
            self.pending_tool_id = identifier

        def on_action_executed(self, *, step: Any) -> None:
            if self.pending_tool_id is None:
                raise AdaptiveRuntimeError("tool label has no durable prediction")
            elapsed_ms = float(step.execution_time) * 1000.0
            runtime.reveal_tool_action(self.pending_tool_id, observed_ms=elapsed_ms)
            self.pending_tool_id = None
            self.next_tool_ordinal += 1

        def on_run_done(self, *, trajectory: Any, info: Any) -> None:
            del trajectory, info
            if self.started_mono_ns is None:
                raise AdaptiveRuntimeError("adaptive trajectory end has no start witness")
            if self.pending_tool_id is not None:
                raise AdaptiveRuntimeError("cannot close trajectory with an unrevealed tool event")
            ended_mono_ns = time.monotonic_ns()
            elapsed_ms = (ended_mono_ns - self.started_mono_ns) / 1_000_000
            runtime.protocol.freeze_prediction_manifest()
            runtime.protocol.reveal_trajectory_label(elapsed_ms)
            score = runtime.protocol.score()
            path, digest = _write_frozen_score(runtime.protocol.root, score)
            print(
                json.dumps(
                    {
                        "adaptive_score_path": str(path),
                        "adaptive_score_sha256": digest,
                        "passed": score["passed"],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    return AdaptiveToolHook()


def main(argv: list[str] | None = None) -> int:
    config = os.environ.get(CONFIG_ENV)
    if not config:
        raise AdaptiveRuntimeError(f"{CONFIG_ENV} is required")
    runtime = AdaptiveRuntime.load(Path(config))

    # Imports occur only on the pinned execution host.  Local/offline tests can
    # exercise build_hook without installing SWE-agent.
    from sweagent.agent.hooks.abstract import AbstractAgentHook
    from sweagent.run import run_batch

    original_get_agent = run_batch.get_agent_from_config
    attached = False

    def get_agent_with_adaptive_hook(agent_config: Any) -> Any:
        nonlocal attached
        if attached:
            raise AdaptiveRuntimeError("adaptive runner requires exactly one SWE-agent instance")
        agent = original_get_agent(agent_config)
        agent.add_hook(build_hook(runtime, AbstractAgentHook))
        attached = True
        return agent

    run_batch.get_agent_from_config = get_agent_with_adaptive_hook
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "run-batch":
        arguments = arguments[1:]
    run_batch.run_from_cli(arguments)
    if not attached:
        raise AdaptiveRuntimeError("SWE-agent completed without constructing the reviewed agent")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AdaptiveRuntimeError as exc:
        print(f"adaptive_runtime_error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
