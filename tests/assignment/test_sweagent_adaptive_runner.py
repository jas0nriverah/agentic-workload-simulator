"""Focused offline tests for the reviewed SWE-agent adaptive wrapper."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from scripts.assignment import sweagent_adaptive_runner as runner
from scripts.assignment.sweagent_adaptive_runner import AdaptiveRuntimeError


class FakeHookBase:
    """Minimal stand-in for SWE-agent's AbstractAgentHook."""


class FakeStep:
    def __init__(self, action: str = "read file", execution_time: float = 0.025) -> None:
        self.action = action
        self.execution_time = execution_time


class FakeProtocol:
    def __init__(self, root: Path, events: list[tuple[object, ...]]) -> None:
        self.root = root
        self.events = events

    def freeze_prediction_manifest(self) -> str:
        self.events.append(("freeze_manifest",))
        return "manifest-sha"

    def reveal_trajectory_label(self, elapsed_ms: float) -> dict[str, object]:
        self.events.append(("reveal_trajectory", elapsed_ms))
        return {"observed_ms": elapsed_ms}

    def score(self) -> dict[str, object]:
        self.events.append(("score",))
        return {
            "schema_version": "assignment.adaptive-event-score.v1",
            "passed": True,
            "coverage_complete": True,
        }


class FakeRuntime:
    def __init__(self, root: Path, events: list[tuple[object, ...]]) -> None:
        self.run_id = "holdout-test-01"
        self.events = events
        self.protocol = FakeProtocol(root, events)

    def predict_tool_action(self, identifier: str, action: str) -> dict[str, object]:
        self.events.append(("predict_tool", identifier, action))
        return {"prediction": {"predicted_ms": 25.0}}

    def reveal_tool_action(self, identifier: str, *, observed_ms: float) -> dict[str, object]:
        self.events.append(("reveal_tool", identifier, observed_ms))
        return {"label": {"observed_ms": observed_ms}}


class UnavailableRuntime(FakeRuntime):
    def reveal_tool_action(self, identifier: str, *, observed_ms: float) -> dict[str, object]:
        del identifier, observed_ms
        self.events.append(("unavailable_tool",))
        raise AdaptiveRuntimeError("tool measurement unavailable")


class FakeAgent:
    def __init__(self) -> None:
        self.hooks: list[object] = []

    def add_hook(self, hook: object) -> None:
        self.hooks.append(hook)


class AdaptiveRunnerTests(unittest.TestCase):
    def test_tool_prediction_is_before_reveal_and_trajectory_freezes_before_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[tuple[object, ...]] = []
            runtime = FakeRuntime(Path(temporary), events)
            hook = runner.build_hook(runtime, FakeHookBase)

            with patch.object(runner.time, "monotonic_ns", side_effect=[100, 8_000_100]):
                hook.on_run_start()
                hook.on_action_started(step=FakeStep())
                hook.on_action_executed(step=FakeStep(execution_time=0.025))
                hook.on_run_done(trajectory=object(), info=object())

            kinds = [event[0] for event in events]
            self.assertEqual(
                kinds,
                ["predict_tool", "reveal_tool", "freeze_manifest", "reveal_trajectory", "score"],
            )
            self.assertLess(kinds.index("predict_tool"), kinds.index("reveal_tool"))
            self.assertLess(kinds.index("freeze_manifest"), kinds.index("reveal_trajectory"))
            score_path = Path(temporary) / "adaptive_score_report.json"
            sidecar = Path(str(score_path) + ".sha256")
            self.assertTrue(score_path.is_file())
            self.assertTrue(sidecar.is_file())
            digest = hashlib.sha256(score_path.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="utf-8"), f"{digest}  {score_path.name}\n")
            self.assertEqual(json.loads(score_path.read_text(encoding="utf-8"))["passed"], True)

    def test_pending_and_unavailable_tool_events_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            events: list[tuple[object, ...]] = []
            runtime = FakeRuntime(Path(temporary), events)
            hook = runner.build_hook(runtime, FakeHookBase)

            with self.assertRaisesRegex(AdaptiveRuntimeError, "no durable prediction"):
                hook.on_action_executed(step=FakeStep())

            hook.on_run_start()
            hook.on_action_started(step=FakeStep())
            with self.assertRaisesRegex(AdaptiveRuntimeError, "prior tool event"):
                hook.on_action_started(step=FakeStep())

            with self.assertRaisesRegex(AdaptiveRuntimeError, "unrevealed tool event"):
                hook.on_run_done(trajectory=object(), info=object())
            self.assertNotIn(("freeze_manifest",), events)
            self.assertNotIn(("score",), events)

        with tempfile.TemporaryDirectory() as temporary:
            events = []
            runtime = UnavailableRuntime(Path(temporary), events)
            hook = runner.build_hook(runtime, FakeHookBase)
            hook.on_run_start()
            hook.on_action_started(step=FakeStep())
            with self.assertRaisesRegex(AdaptiveRuntimeError, "unavailable"):
                hook.on_action_executed(step=FakeStep())
            with self.assertRaisesRegex(AdaptiveRuntimeError, "unrevealed tool event"):
                hook.on_run_done(trajectory=object(), info=object())
            self.assertNotIn(("freeze_manifest",), events)

    def test_main_strips_leading_run_batch_and_attaches_exactly_one_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = FakeRuntime(Path(temporary), [])
            captured: dict[str, object] = {}
            run_batch_module = types.ModuleType("sweagent.run.run_batch")

            def get_agent(_agent_config: object) -> FakeAgent:
                return FakeAgent()

            def run_from_cli(arguments: list[str]) -> None:
                captured["argv"] = list(arguments)
                run_batch_module.get_agent_from_config(object())

            run_batch_module.get_agent_from_config = get_agent  # type: ignore[attr-defined]
            run_batch_module.run_from_cli = run_from_cli  # type: ignore[attr-defined]
            sweagent_module = types.ModuleType("sweagent")
            sweagent_run_module = types.ModuleType("sweagent.run")
            sweagent_run_module.run_batch = run_batch_module  # type: ignore[attr-defined]
            sweagent_agent_module = types.ModuleType("sweagent.agent")
            sweagent_hooks_module = types.ModuleType("sweagent.agent.hooks")
            sweagent_abstract_module = types.ModuleType("sweagent.agent.hooks.abstract")
            sweagent_abstract_module.AbstractAgentHook = FakeHookBase  # type: ignore[attr-defined]

            modules = {
                "sweagent": sweagent_module,
                "sweagent.run": sweagent_run_module,
                "sweagent.run.run_batch": run_batch_module,
                "sweagent.agent": sweagent_agent_module,
                "sweagent.agent.hooks": sweagent_hooks_module,
                "sweagent.agent.hooks.abstract": sweagent_abstract_module,
            }
            with patch.dict(sys.modules, modules), patch.dict(
                os.environ, {runner.CONFIG_ENV: str(Path(temporary) / "adaptive-runtime.json")}
            ), patch.object(runner.AdaptiveRuntime, "load", return_value=runtime):
                result = runner.main(["run-batch", "--instances", "one.jsonl", "--num-workers", "1"])

            self.assertEqual(result, 0)
            self.assertEqual(captured["argv"], ["--instances", "one.jsonl", "--num-workers", "1"])


if __name__ == "__main__":
    unittest.main()
