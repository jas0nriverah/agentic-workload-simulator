from __future__ import annotations

import json
import hashlib
import http.client
import asyncio
import math
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest import mock

from agentic_sim.telemetry.features import (
    build_conditional_replay_features,
    build_model_features,
    build_tool_features,
    model_vector,
    serialize_model_vector,
)
from agentic_sim.telemetry.clock import monotonic_ns
from agentic_sim.telemetry.hardware import HardwareDescriptorError, model_hardware_features
from agentic_sim.telemetry.script_state import ScriptStateLedger
from agentic_sim.telemetry.sweagent_hooks import (
    SWEAgentEnvironmentTelemetryHook,
    SWEAgentTelemetryHook,
)
from agentic_sim.telemetry.v2 import TelemetryV2
from agentic_sim.telemetry.work import CommandProbeBinding, measure_runtime_work, parse_strace_summary
from scripts.observability.request_proxy import JsonlWriter, ProxyServer


class FeatureContractTests(unittest.TestCase):
    def test_default_script_state_is_canonical_round_trip(self):
        feature = build_tool_features("git diff")
        rebuilt = build_tool_features(
            feature["action"],
            script_state=feature["script_state"],
            action_id=feature["action_id"],
        )
        self.assertEqual(rebuilt, feature)

    def test_shell_structure_and_semantic_classification(self):
        mixed = build_tool_features("git diff | head || true")
        self.assertEqual(mixed["subcommand"], "diff")
        self.assertEqual(mixed["pipeline"]["operators"], ["|", "||"])
        self.assertEqual(mixed["pipeline"]["pipeline_stage_count"], 2)
        self.assertEqual(mixed["pipeline"]["stages"][1]["operator_before"], "|")
        self.assertEqual(mixed["pipeline"]["stages"][2]["operator_before"], "||")
        quoted = build_tool_features("echo 'pytest tests/x.py | cat'")
        self.assertEqual(quoted["test_runner"], None)
        self.assertEqual(quoted["operation_class"], "shell")
        self.assertEqual(build_tool_features("cat list")["subcommand"], None)
        self.assertEqual(build_tool_features("find .")["subcommand"], None)

        python_test = build_tool_features("python -m pytest tests/x.py")
        self.assertIsNone(python_test["subcommand"])
        self.assertEqual(python_test["module"], "pytest")
        self.assertEqual(python_test["test_runner"], "pytest")

        find_exec = build_tool_features(r"find . -exec python -m pytest {} \;")
        self.assertIsNone(find_exec["module"])
        self.assertEqual(find_exec["find_exec_child"]["executable"], "python")
        self.assertEqual(find_exec["find_exec_child"]["module"], "pytest")
        self.assertEqual(find_exec["find_exec_child"]["test_runner"], "pytest")
        self.assertIsNone(find_exec["find_exec_child"]["dynamic_child_count"])
        self.assertEqual(find_exec["operation_class"], "traversal")
        self.assertEqual(find_exec["pipeline"]["stages"][0]["find_exec_child"]["module"], "pytest")

        unittest_selector = build_tool_features("python -m unittest package.test_x.TestClass")
        self.assertEqual(unittest_selector["test_scope"], ["package.test_x.TestClass"])
        self.assertEqual(build_tool_features("python setup.py test")["test_runner"], "setup.py")

    def test_state_and_model_boundary_reject_nested_outcomes(self):
        with self.assertRaises(ValueError):
            build_tool_features("echo ok", script_state={"status": "known", "metadata": {"timing": 1}})
        with self.assertRaises(ValueError):
            build_tool_features("echo ok", script_state={"status": "known", "availability": {"status": "measured"}})
        with self.assertRaises(ValueError):
            build_model_features({"max_output_tokens": 2, "post_state": {"status": "success"}})
        with self.assertRaises(ValueError):
            build_model_features({"hardware": {"cpu_threads": 16}})
        with self.assertRaises(ValueError):
            build_model_features({"hardware": {"storage": {"bandwidth_bytes_per_s": 10}}})
        with self.assertRaises(ValueError):
            build_model_features({"hardware": {"gpu_compute_tflops": math.nan}})

    def test_work_probe_requires_action_binding(self):
        binding = CommandProbeBinding("event-1", "cat file", 100, 200)
        unbound = measure_runtime_work(
            {"bytes_read": 1000, "files_touched": 1, "provenance": "estimated"}
        )
        self.assertIsNone(unbound.bytes_read)
        summary = {
            "mode": "strace -ff -ttt -T",
            "event_id": "event-1",
            "command_sha256": binding.command_sha256,
            "start_mono_ns": 110,
            "end_mono_ns": 190,
            "pid": 55,
            "provenance": "measured",
            "bytes_read": 1000,
            "files_touched": 1,
        }
        measured = parse_strace_summary(summary, binding=binding)
        self.assertEqual(measured.bytes_read, 1000)
        self.assertEqual(measured.files_touched, 1)
        with self.assertRaises(ValueError):
            parse_strace_summary(summary, binding=CommandProbeBinding("event-2", "cat file", 100, 200))

    def test_vectors_are_whitelisted_and_replay_is_explicit(self):
        feature = build_model_features({"max_output_tokens": 10, "model": "m"})
        feature.update(
            {
                "request_id": "request-secret",
                "status": "success",
                "duration_ms": 3,
                "source_event_id": "event-secret",
                "availability": "measured",
            }
        )
        vector = model_vector(feature)
        self.assertNotIn("request_id", vector)
        self.assertNotIn("status", vector)
        self.assertNotIn("duration_ms", vector)
        self.assertNotIn("source_event_id", vector)
        self.assertEqual(serialize_model_vector(feature), serialize_model_vector(dict(feature)))
        replay = build_conditional_replay_features(
            {"max_output_tokens": 10}, output_tokens=4, duration_ms=8.0, status="success"
        )
        self.assertEqual(replay["mode"], "conditional_replay")
        self.assertEqual(replay["conditional_replay"]["output_tokens"], 4)


class ScriptStateTests(unittest.TestCase):
    def test_chronological_snapshot_and_edit_invalidation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "run.sh"
            script.write_text("echo one\n", encoding="utf-8")
            ledger = ScriptStateLedger(root)
            first = ledger.snapshot(["run.sh"], source_event_id="state-1", observed_at_mono_ns=10)
            self.assertEqual(first["status"], "known")
            self.assertEqual(first["generation"], 1)
            script.write_text("echo two\n", encoding="utf-8")
            invalidated = ledger.record_edit(["run.sh"], source_event_id="edit-1")
            self.assertEqual(invalidated["status"], "invalidated")
            self.assertIsNone(invalidated["paths"][0]["sha256"])
            refreshed = ledger.snapshot(["run.sh"], source_event_id="state-2", observed_at_mono_ns=20)
            self.assertEqual(refreshed["status"], "known")
            self.assertEqual(refreshed["generation"], 3)
            self.assertEqual([entry["event"] for entry in ledger.history()], ["snapshot", "edit", "snapshot"])


class TelemetryV2Tests(unittest.TestCase):
    def test_union_closure_uses_true_unknown_complement_and_persists_features(self):
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="run-1", writer_role="runner")
            outer = recorder.start_outer(start_mono_ns=100)
            wrapper = recorder.start_phase(
                "generic_wrapper", start_mono_ns=100, parent_event_id=outer.pre_event_id
            )
            tool = recorder.begin_tool("git diff", start_mono_ns=120)
            recorder.end_tool(tool, end_mono_ns=150)
            model = recorder.begin_request({"max_output_tokens": 2}, start_mono_ns=140)
            recorder.end_request(model, status="success", end_mono_ns=170)
            wrapper.finish(end_mono_ns=300)
            recorder.finish_outer(end_mono_ns=300)
            summary = recorder.reconcile_e2e()
            self.assertEqual(summary["measured_intervals"], [[120, 170]])
            self.assertEqual(summary["unknown_intervals"], [[100, 120], [170, 300]])
            self.assertEqual(summary["measured_phase_union_ms"], 0.00005)
            self.assertEqual(summary["unknown_residual_ms"], 0.00015)
            self.assertTrue(summary["outer_wrapper_excluded"])
            self.assertEqual(
                [row["event_kind"] for row in recorder.rows("lifecycle") if row["event_kind"] == "unknown_residual"],
                ["unknown_residual", "unknown_residual"],
            )
            tool_rows = [json.loads(line) for line in (Path(temporary) / "tool_events.jsonl").read_text().splitlines()]
            self.assertEqual(tool_rows[0]["action"], "git diff")
            self.assertEqual(tool_rows[0]["terminal"], False)
            self.assertIsNone(tool_rows[0]["duration_ms"])
            self.assertIsNone(tool_rows[1]["bytes_read"])
            self.assertIn("feature_vector_sha256", tool_rows[0])

    def test_request_retry_lineage_tokens_and_hardware_policy(self):
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(
                temporary,
                run_id="run-2",
                hardware={"cpu_threads": 32, "gpu_name": "H100", "gpu_uuid": "uuid"},
                model_hardware={
                    "cpu_frequency_hz": 3_000_000_000,
                    "gpu_memory_bandwidth_bytes_per_s": 3,
                    "gpu_compute_tflops": 4,
                    "availability": {"cpu_frequency_hz": "measured"},
                },
            )
            first = recorder.begin_request({"max_output_tokens": 2}, start_mono_ns=10)
            recorder.end_request(first, status="failure", end_mono_ns=20, error_type="HTTPError")
            second = recorder.begin_request(
                {"max_output_tokens": 2}, retry_index=1, retry_of=first.identity["physical_request_id"], start_mono_ns=30
            )
            recorder.end_request(
                second,
                end_mono_ns=40,
                response={"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                                     "prompt_tokens_details": {"cached_tokens": 2}}},
            )
            rows = [row for row in recorder.rows("model") if row["terminal"]]
            self.assertEqual(rows[0]["status"], "failure")
            self.assertEqual(rows[1]["retry_index"], 1)
            self.assertEqual(rows[1]["retry_of"], rows[0]["physical_request_id"])
            self.assertEqual(rows[1]["input_tokens"], 7)
            self.assertEqual(rows[1]["output_tokens"], 3)
            self.assertEqual(rows[1]["cached_tokens"], 2)
            self.assertIsNone(rows[1]["queue_ms"])
            self.assertEqual(rows[1]["timing_availability"]["decode_ms"], "unavailable")
            self.assertNotIn("gpu_name", rows[1]["features"]["hardware"])
            self.assertEqual(rows[1]["feature_vector"]["hardware_gpu_compute_tflops"], 4)
            self.assertEqual(json.loads((Path(temporary) / "telemetry_manifest.json").read_text())["hardware_model_policy"]["fitted_model"], False)
        with self.assertRaises(HardwareDescriptorError):
            model_hardware_features({"gpu_name": "H100"})
        with self.assertRaises(HardwareDescriptorError):
            model_hardware_features({"cpu_frequency_hz": math.nan})
        with self.assertRaises(HardwareDescriptorError):
            model_hardware_features({"gpu_compute_tflops": math.inf})


class _FakeTools:
    def __init__(self):
        self.calls = 0

    def get_state(self, *args, **kwargs):
        self.calls += 1
        return {"status": "known"}


class _FakeModel:
    def __init__(self):
        self.config = type("Config", (), {"completion_kwargs": {"max_tokens": 4}, "name": "fake"})()

    def query(self, *_args, **_kwargs):
        return {"message": "echo ok"}


class _FakeAgent:
    def __init__(self):
        self.tools = _FakeTools()
        self.model = _FakeModel()
        self._n_consecutive_timeouts = 0


class _FakeEnvironment:
    def __init__(self):
        self.hooks = []

    def add_hook(self, hook):
        hook.on_init(env=self)
        self.hooks.append(hook)

    def close(self):
        time.sleep(0.001)
        for hook in self.hooks:
            hook.on_close()


class HookIntegrationTests(unittest.TestCase):
    def test_local_work_target_uses_persistent_shell_pid_and_persists_mapping(self):
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="mapping-run")
            hook = SWEAgentTelemetryHook(recorder)
            shell = SimpleNamespace(pid=os.getpid())
            runtime = SimpleNamespace(sessions={"default": SimpleNamespace(shell=shell)})
            deployment = SimpleNamespace(runtime=runtime)
            environment = SimpleNamespace(deployment=deployment)

            target = hook._resolve_work_target({}, environment)

            self.assertEqual(target.pid, os.getpid())
            self.assertIsNone(target.container_pid)
            self.assertEqual(target.mapping_source, "swerex_local_persistent_bash_session")
            self.assertEqual(environment._assignment_v2_process_target["host_pid"], os.getpid())
            self.assertEqual(
                deployment._assignment_v2_process_target["mapping_source"],
                "swerex_local_persistent_bash_session",
            )

    def test_required_collector_bootstrap_suspends_only_pid_discovery_and_keeps_command(self):
        class Observation:
            output = "__ASSIGNMENT_V2_PERSISTENT_SHELL__ pid=321 ns=pid:[7]\n"
            exit_code = 0

        class Runtime:
            def __init__(self):
                self.calls = []

            async def run_in_session(self, action):
                self.calls.append(action)
                return Observation()

        class Collector:
            def __init__(self):
                self.started = []
                self.ended = []

            def start_action(self, **values):
                self.started.append(values)

            def end_action(self, **values):
                self.ended.append(values)

        class Service:
            def __init__(self):
                self.client = Collector()
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Environment:
            def __init__(self):
                self.runtime = Runtime()
                self.deployment = SimpleNamespace(
                    container_name="swerex-case",
                    runtime=self.runtime,
                    _config=SimpleNamespace(container_runtime="docker"),
                )

            def communicate(self, command, **_kwargs):
                action = SimpleNamespace(command=command)
                return asyncio.run(self.runtime.run_in_session(action)).output

        config = {
            "backend": "bcc",
            "trace_format": "raw individual",
            "attach_existing_process": True,
            "require_persistent_runtime_pid": True,
            "output_dir": "/tmp/collector-test",
            "socket_path": "/tmp/collector-test.sock",
            "session": "default",
        }
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="bootstrap-run")
            environment = Environment()
            agent = _FakeAgent()
            agent._env = environment
            hook = SWEAgentTelemetryHook(recorder)
            service = Service()
            with (
                mock.patch.dict(os.environ, {"ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1"}),
                mock.patch.object(hook, "_load_work_config", return_value=config),
                mock.patch(
                    "agentic_sim.telemetry.sweagent_hooks._map_container_pid_to_host",
                    return_value=501,
                ),
                mock.patch(
                    "agentic_sim.telemetry.bpf_work.launch_bpf_work_service",
                    return_value=service,
                ),
            ):
                hook.on_init(agent=agent)
                hook.on_run_start()
                hook.on_setup_attempt()

            self.assertEqual(len(environment.runtime.calls), 1)
            self.assertEqual(service.client.started, [])
            self.assertEqual(service.client.ended, [])
            self.assertEqual(environment._assignment_v2_process_target["host_pid"], 501)
            self.assertIsNone(hook._target_discovery_span)
            self.assertIs(hook._aux_span, hook._setup_span)
            discovery_rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "persistent_shell_pid_discovery" and row["terminal"]
            ]
            self.assertEqual(len(discovery_rows), 1)
            discovery = discovery_rows[0]
            self.assertTrue(discovery["target_discovery"])
            self.assertTrue(discovery["collection_suspended"])
            self.assertEqual(discovery["runtime_command"], environment.runtime.calls[0].command)
            self.assertEqual(discovery["command_exit_code"], 0)
            self.assertEqual(discovery["work_collection_status"], "suspended_for_target_discovery")
            self.assertGreater(discovery["duration_ms"], 0)
            hook.finish_setup()
            recorder.finish_outer()

    def test_environment_startup_bootstraps_before_first_communicate_and_keeps_each_command(self):
        class Observation:
            output = "__ASSIGNMENT_V2_PERSISTENT_SHELL__ pid=321 ns=pid:[7]\n"
            exit_code = 0

        class Runtime:
            def __init__(self):
                self.calls = []

            async def run_in_session(self, action):
                self.calls.append(action)
                return Observation()

        class Environment:
            def __init__(self):
                self.runtime = Runtime()
                self.deployment = SimpleNamespace(
                    runtime=self.runtime,
                    container_name="swerex-case",
                    _config=SimpleNamespace(container_runtime="docker"),
                )
                self.original_commands = []

            def communicate(self, command, **_kwargs):
                self.original_commands.append(command)
                return asyncio.run(
                    self.runtime.run_in_session(SimpleNamespace(command=command))
                ).output

        class Collector:
            def __init__(self):
                self.started = []
                self.ended = []

            def start_action(self, **values):
                self.started.append(values)

            def end_action(self, **values):
                self.ended.append(values)

        class Service:
            def __init__(self):
                self.client = Collector()
                self.stopped = False

            def stop(self):
                self.stopped = True

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="early-setup-run")
            environment = Environment()
            agent = _FakeAgent()
            agent._env = environment
            agent_hook = SWEAgentTelemetryHook(recorder)
            env_hook = SWEAgentEnvironmentTelemetryHook(recorder, agent_hook=agent_hook)
            service = Service()
            config = {
                "backend": "bcc",
                "trace_format": "raw individual",
                "attach_existing_process": True,
                "require_persistent_runtime_pid": True,
                "output_dir": str(Path(temporary) / "linux_work"),
                "socket_path": str(Path(temporary) / "collector.sock"),
                "session": "default",
            }
            with (
                mock.patch.dict(os.environ, {"ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1"}),
                mock.patch.object(agent_hook, "_load_work_config", return_value=config),
                mock.patch(
                    "agentic_sim.telemetry.sweagent_hooks._map_container_pid_to_host",
                    return_value=os.getpid(),
                ),
                mock.patch(
                    "agentic_sim.telemetry.bpf_work.launch_bpf_work_service",
                    return_value=service,
                ),
            ):
                agent_hook.on_init(agent=agent)
                env_hook.on_init(env=environment)
                env_hook.on_start_deployment()
                # This is the first synchronous command after SWE-ReX creates
                # its persistent session (the pinned LANG/LC_ALL setup path).
                environment.communicate("export LANG=C.UTF-8 && export LC_ALL=C.UTF-8")
                # Repeated setup commands must receive fresh child spans and
                # collector tokens even though they share the startup span.
                environment.communicate("cd /testbed && git reset --hard HEAD")
                env_hook.on_environment_startup()
                # Post-startup/agent setup commands have no environment span;
                # the runtime wrapper must still create a standalone child.
                environment.communicate("export SWE_AGENT_SETUP=1")
                agent_hook._stop_work_service()

            self.assertEqual(len(environment.original_commands), 4)
            self.assertTrue(environment.original_commands[0].startswith("printf '"))
            self.assertEqual(len(environment.runtime.calls), 4)
            self.assertEqual(len(service.client.started), 3)
            self.assertEqual(len(service.client.ended), 3)
            self.assertEqual(
                [entry["event_id"] for entry in service.client.started],
                [entry["event_id"] for entry in service.client.ended],
            )
            self.assertEqual(
                len({entry["event_id"] for entry in service.client.started}),
                3,
            )
            runtime_rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "runtime_command" and row["terminal"]
            ]
            self.assertEqual(len(runtime_rows), 3)
            self.assertEqual(
                [row["runtime_command"] for row in runtime_rows],
                environment.original_commands[1:],
            )
            self.assertTrue(all(row["cpu_action_required"] for row in runtime_rows))
            self.assertEqual(
                [row["work_collector_event_id"] for row in runtime_rows],
                [entry["event_id"] for entry in service.client.started],
            )
            # These are auxiliary runtime children, not tool callbacks.  The
            # terminal rows must retain the native runtime outcome and an
            # explicit unavailable work-probe reason rather than silently
            # inheriting the hook's scratch state.
            self.assertTrue(all(row["command_exit_code"] == 0 for row in runtime_rows))
            self.assertTrue(
                all(row["command_exit_code_availability"] == "measured" for row in runtime_rows)
            )
            self.assertTrue(
                all(row["work_volume_reason"] == "runtime child/work probe is unavailable" for row in runtime_rows)
            )
            self.assertTrue(all(row["runtime_child_telemetry"] is False for row in runtime_rows))
            discovery_rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "persistent_shell_pid_discovery" and row["terminal"]
            ]
            self.assertEqual(len(discovery_rows), 1)
            self.assertFalse(discovery_rows[0]["cpu_action_required"])
            self.assertTrue(discovery_rows[0]["collection_suspended"])
            self.assertIsNone(agent_hook.work_collector)

    def test_start_deployment_defers_runtime_property_until_after_deployment_start(self):
        class Observation:
            output = "ready\n"
            exit_code = 0

        class Runtime:
            def __init__(self):
                self.calls = []

            async def run_in_session(self, action):
                self.calls.append(action)
                return Observation()

        class Deployment:
            def __init__(self):
                self.started = False
                self._runtime = Runtime()

            @property
            def runtime(self):
                if not self.started:
                    raise RuntimeError("DeploymentNotStartedError")
                return self._runtime

        class Environment:
            def __init__(self):
                self.deployment = Deployment()

            def communicate(self, command, **_kwargs):
                return asyncio.run(
                    self.deployment.runtime.run_in_session(SimpleNamespace(command=command))
                ).output

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="deferred-runtime-run")
            environment = Environment()
            agent = _FakeAgent()
            # Pinned DefaultAgent constructs with no environment; it receives
            # this instance only after env.start has begun.
            agent._env = None
            agent_hook = SWEAgentTelemetryHook(recorder)
            env_hook = SWEAgentEnvironmentTelemetryHook(recorder, agent_hook=agent_hook)
            with mock.patch.dict(os.environ, {"ASSIGNMENT_TELEMETRY_V2_REQUIRED": "0"}):
                agent_hook.on_init(agent=agent)
                agent._env = environment
                env_hook.on_init(env=environment)
                # The real Docker deployment has no usable runtime property at
                # this callback; the hook must only bind the startup span.
                env_hook.on_start_deployment()
                environment.deployment.started = True
                environment.communicate("export LANG=C.UTF-8")
                env_hook.on_environment_startup()

            self.assertEqual(
                [action.command for action in environment.deployment._runtime.calls],
                ["export LANG=C.UTF-8"],
            )
            rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "runtime_command" and row["terminal"]
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["runtime_command"], "export LANG=C.UTF-8")

    def test_required_collector_end_failure_terminalizes_runtime_child(self):
        class Observation:
            output = "__ASSIGNMENT_V2_PERSISTENT_SHELL__ pid=321 ns=pid:[7]\n"
            exit_code = 0

        class Runtime:
            def __init__(self):
                self.calls = []

            async def run_in_session(self, action):
                self.calls.append(action)
                return Observation()

        class Environment:
            def __init__(self):
                self.runtime = Runtime()
                self.deployment = SimpleNamespace(
                    runtime=self.runtime,
                    container_name="swerex-case",
                    _config=SimpleNamespace(container_runtime="docker"),
                )

            def communicate(self, command, **_kwargs):
                return asyncio.run(
                    self.runtime.run_in_session(SimpleNamespace(command=command))
                ).output

        class Collector:
            def __init__(self):
                self.started = []

            def start_action(self, **values):
                self.started.append(values)

            def end_action(self, **_values):
                raise RuntimeError("fixture collector end failure")

        class Service:
            def __init__(self):
                self.client = Collector()
                self.stopped = False

            def stop(self):
                self.stopped = True

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="end-failure-run")
            environment = Environment()
            agent = _FakeAgent()
            agent._env = environment
            agent_hook = SWEAgentTelemetryHook(recorder)
            env_hook = SWEAgentEnvironmentTelemetryHook(recorder, agent_hook=agent_hook)
            service = Service()
            config = {
                "backend": "bcc",
                "trace_format": "raw individual",
                "attach_existing_process": True,
                "require_persistent_runtime_pid": True,
                "output_dir": str(Path(temporary) / "linux_work"),
                "socket_path": str(Path(temporary) / "collector.sock"),
                "session": "default",
            }
            with (
                mock.patch.dict(os.environ, {"ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1"}),
                mock.patch.object(agent_hook, "_load_work_config", return_value=config),
                mock.patch(
                    "agentic_sim.telemetry.sweagent_hooks._map_container_pid_to_host",
                    return_value=os.getpid(),
                ),
                mock.patch(
                    "agentic_sim.telemetry.bpf_work.launch_bpf_work_service",
                    return_value=service,
                ),
            ):
                agent_hook.on_init(agent=agent)
                env_hook.on_init(env=environment)
                env_hook.on_start_deployment()
                with self.assertRaisesRegex(RuntimeError, "fixture collector end failure"):
                    environment.communicate("echo setup")
                agent_hook._stop_work_service()

            runtime_rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] in {"runtime_command", "runtime_command_start"}
            ]
            self.assertEqual(len(runtime_rows), 2)
            terminal = [
                row
                for row in runtime_rows
                if row["event_kind"] == "runtime_command" and row["terminal"]
            ]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(terminal[0]["runtime_command"], "echo setup")
            self.assertEqual(terminal[0]["status"], "failure")
            self.assertEqual(terminal[0]["work_collector_status"], "unavailable")
            self.assertIn("fixture collector end failure", terminal[0]["work_collector_error"])
            self.assertEqual(sum(not row["terminal"] for row in runtime_rows), 1)
            self.assertIsNone(agent_hook.work_collector)

    def test_bash_interrupt_is_a_lifecycle_control_span_without_command_identity(self):
        class BashInterruptAction:
            action_type = "bash_interrupt"
            session = "default"

        class Runtime:
            async def run_in_session(self, action):
                return SimpleNamespace(output="", exit_code=0, action=action)

        environment = SimpleNamespace(deployment=SimpleNamespace(runtime=Runtime()))
        agent = _FakeAgent()
        agent._env = environment
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="interrupt-run")
            hook = SWEAgentTelemetryHook(recorder)
            hook.on_init(agent=agent)
            hook.on_run_start()
            hook.on_step_start()
            hook.on_actions_generated(step={"action": "echo pending"})
            hook.on_action_started(step={"action": "echo pending"})
            tool_parent_event_id = hook._tool_span.pre_event_id
            asyncio.run(environment.deployment.runtime.run_in_session(BashInterruptAction()))
            hook.on_action_executed(step={"action": "echo pending", "exit_status": 0})
            hook.on_step_done(step={"action": "echo pending"}, info={})
            hook.on_run_done(trajectory=[], info={})
            recorder.finish_outer()

            rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "bash_interrupt_control" and row["terminal"]
            ]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["control_action"], "bash_interrupt")
            self.assertEqual(row["runtime_action_class"], "BashInterruptAction")
            self.assertEqual(row["runtime_action_type"], "bash_interrupt")
            self.assertEqual(row["session"], "default")
            self.assertIsNone(row["command"])
            self.assertIsNone(row["command_sha256"])
            self.assertFalse(row["command_identity_available"])
            self.assertEqual(row["parent_event_id"], tool_parent_event_id)

    def test_docker_work_target_maps_container_pid_through_host_nspid(self):
        entries = [Path("/proc/501"), Path("/proc/502"), Path("/proc/700")]

        def namespace(pid):
            return "pid:[7]" if pid in {4321, 501, 502} else "pid:[8]"

        def nspids(pid):
            return {
                4321: (4321, 1),
                501: (501, 321),
                502: (502, 999),
                700: (700, 321),
            }.get(pid)

        def cgroups(pid):
            return {
                4321: ("0::/docker/swerex-case",),
                501: ("0::/docker/swerex-case",),
                502: ("0::/docker/swerex-case",),
                700: ("0::/docker/other-case",),
            }.get(pid, ())

        inspected = SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        with (
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks.subprocess.run",
                return_value=inspected,
            ) as run,
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._pid_namespace_link",
                side_effect=namespace,
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_nspid_values",
                side_effect=nspids,
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_cgroup_membership",
                side_effect=cgroups,
            ),
            mock.patch("agentic_sim.telemetry.sweagent_hooks.Path.iterdir", return_value=entries),
        ):
            host_pid = __import__(
                "agentic_sim.telemetry.sweagent_hooks",
                fromlist=["_map_container_pid_to_host"],
            )._map_container_pid_to_host(
                container_runtime="docker",
                container_name="swerex-case",
                container_pid=321,
                pid_namespace="pid:[7]",
            )

        self.assertEqual(host_pid, 501)
        self.assertEqual(run.call_args.args[0], [
            "docker",
            "inspect",
            "--format",
            "{{.State.Pid}}",
            "swerex-case",
        ])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_docker_work_target_rejects_unscoped_cgroup_mapping(self):
        entries = [Path("/proc/501")]
        inspected = SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        with (
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks.subprocess.run",
                return_value=inspected,
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_nspid_values",
                side_effect=lambda pid: (4321, 1) if pid == 4321 else (501, 321),
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_cgroup_membership",
                return_value=("0::/",),
            ),
            mock.patch("agentic_sim.telemetry.sweagent_hooks.Path.iterdir", return_value=entries),
        ):
            with self.assertRaisesRegex(RuntimeError, "container-specific"):
                __import__(
                    "agentic_sim.telemetry.sweagent_hooks",
                    fromlist=["_map_container_pid_to_host"],
                )._map_container_pid_to_host(
                    container_runtime="docker",
                    container_name="swerex-case",
                    container_pid=321,
                    pid_namespace="pid:[7]",
                )

    def test_docker_work_target_rejects_ambiguous_host_nspid_mapping(self):
        entries = [Path("/proc/501"), Path("/proc/502")]
        inspected = SimpleNamespace(returncode=0, stdout="4321\n", stderr="")
        with (
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks.subprocess.run",
                return_value=inspected,
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._pid_namespace_link",
                return_value="pid:[7]",
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_nspid_values",
                side_effect=lambda pid: (4321, 1) if pid == 4321 else (1, 321),
            ),
            mock.patch(
                "agentic_sim.telemetry.sweagent_hooks._proc_cgroup_membership",
                return_value=("0::/docker/swerex-case",),
            ),
            mock.patch("agentic_sim.telemetry.sweagent_hooks.Path.iterdir", return_value=entries),
        ):
            with self.assertRaisesRegex(RuntimeError, "exactly one host process"):
                __import__(
                    "agentic_sim.telemetry.sweagent_hooks",
                    fromlist=["_map_container_pid_to_host"],
                )._map_container_pid_to_host(
                    container_runtime="docker",
                    container_name="swerex-case",
                    container_pid=321,
                    pid_namespace="pid:[7]",
                )

    def test_required_work_collector_rejects_unreviewed_backend(self):
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="activation-run")
            hook = SWEAgentTelemetryHook(recorder)
            with (
                mock.patch.dict(os.environ, {"ASSIGNMENT_TELEMETRY_V2_REQUIRED": "1"}),
                mock.patch.object(hook, "_load_work_config", return_value={"backend": "ebpf"}),
            ):
                with self.assertRaisesRegex(RuntimeError, "unsupported v2 CPU collector backend"):
                    hook._ensure_work_collector(SimpleNamespace())

    def test_long_evidence_path_uses_owned_short_socket_and_preserves_trace_path(self):
        for launch_fails in (False, True):
            with self.subTest(launch_fails=launch_fails), TemporaryDirectory() as temporary:
                recorder = TelemetryV2(temporary, run_id="long-path-run")
                hook = SWEAgentTelemetryHook(recorder)
                trace = Path(temporary) / ("long-evidence-directory-" * 6) / "linux_work"
                config = {"backend": "bcc", "output_dir": str(trace)}
                service = SimpleNamespace(client=SimpleNamespace(start_action=lambda **kw: None, end_action=lambda **kw: None), stop=mock.Mock())
                seen = {}

                def launch(target, **kwargs):
                    seen.update(kwargs)
                    self.assertTrue(kwargs["socket_path"].parent.is_dir())
                    if launch_fails:
                        raise RuntimeError("fixture attach failure")
                    return service

                with (
                    mock.patch.object(hook, "_load_work_config", return_value=config),
                    mock.patch.object(hook, "_resolve_work_target", return_value=object()),
                    mock.patch("agentic_sim.telemetry.bpf_work.launch_bpf_work_service", side_effect=launch),
                ):
                    if launch_fails:
                        with self.assertRaisesRegex(RuntimeError, "fixture attach failure"):
                            hook._ensure_work_collector(SimpleNamespace())
                    else:
                        hook._ensure_work_collector(SimpleNamespace())
                        hook._stop_work_service()
                        service.stop.assert_called_once()
                self.assertEqual(seen["trace_dir"], trace)
                self.assertLess(len(os.fsencode(seen["socket_path"])), 108)
                self.assertFalse(seen["socket_path"].parent.exists())

    def test_requested_site_activation_fails_before_workload_without_pinned_agent(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = os.environ.copy()
            source_root = Path(__file__).resolve().parents[2] / "src"
            environment.update(
                {
                    "PYTHONPATH": str(source_root),
                    "ASSIGNMENT_TELEMETRY_V2_AUTO": "1",
                    "ASSIGNMENT_TELEMETRY_V2_DIR": str(root),
                    "ASSIGNMENT_TELEMETRY_V2_RUN_ID": "activation-run",
                    "ASSIGNMENT_TELEMETRY_V2_READY": str(root / "ready.json"),
                }
            )
            process = subprocess.run(
                [sys.executable, "-c", "print('WORKLOAD_RAN')"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn("WORKLOAD_RAN", process.stdout)
            self.assertIn("required v2 SWE-agent instrumentation unavailable", process.stderr)
            self.assertFalse((root / "ready.json").exists())

    def test_inherited_activation_is_scoped_to_marker_owner(self):
        """A helper Python process cannot steal the reviewed agent marker."""

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "ready.json"
            original = {
                "schema_version": "assignment.telemetry.v2.activation",
                "run_id": "owner-run",
                "attempt_id": "attempt-001",
                "handshake_nonce": "nonce",
                "writer_role": "sweagent",
                "pid": os.getpid(),
            }
            marker.write_text(json.dumps(original, sort_keys=True) + "\n", encoding="utf-8")
            environment = os.environ.copy()
            source_root = Path(__file__).resolve().parents[2] / "src"
            environment.update(
                {
                    "PYTHONPATH": str(source_root),
                    "ASSIGNMENT_TELEMETRY_V2_AUTO": "1",
                    "ASSIGNMENT_TELEMETRY_V2_READY": str(marker),
                    "ASSIGNMENT_TELEMETRY_V2_DIR": str(root),
                    "ASSIGNMENT_TELEMETRY_V2_RUN_ID": "owner-run",
                }
            )
            process = subprocess.run(
                [sys.executable, "-c", "print('HELPER_RAN')"],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertIn("HELPER_RAN", process.stdout)
            self.assertEqual(marker.read_text(encoding="utf-8"), json.dumps(original, sort_keys=True) + "\n")

    def test_pinned_callback_order_intent_actual_timeout_and_real_teardown(self):
        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="hook-run")
            agent = _FakeAgent()
            hook = SWEAgentTelemetryHook(recorder)
            hook.on_init(agent=agent)
            environment = _FakeEnvironment()
            environment.add_hook(SWEAgentEnvironmentTelemetryHook(recorder))
            hook.on_run_start()
            hook.on_step_start()
            hook.on_model_query(messages=[], agent="main")
            agent.model.query([])
            hook.on_actions_generated(step={"action": "echo ok"})
            tool_intents = [row for row in recorder.rows("tool") if row["event_kind"] == "tool_intent"]
            self.assertEqual(len(tool_intents), 1)
            hook.on_action_started(step={"action": "echo ok"})
            agent._n_consecutive_timeouts = 1
            hook.on_action_executed(step={"action": "echo ok"})
            hook.on_step_done(step={"action": "echo ok"}, info={})
            environment.close()
            hook.on_run_done(trajectory=[], info={})
            self.assertFalse(recorder._outer.closed)
            recorder.finish_outer(status="success")
            summary = recorder.reconcile_e2e()
            self.assertTrue(summary["within_tolerance"])
            tool_rows = [row for row in recorder.rows("tool") if row["event_kind"] == "tool_event"]
            self.assertEqual(tool_rows[-1]["status"], "timeout")
            teardown = [row for row in recorder.rows("lifecycle") if row["event_kind"] == "teardown"]
            self.assertEqual(len(teardown), 1)
            self.assertGreater(teardown[0]["duration_ms"], 0)
            self.assertEqual(agent.model.config.completion_kwargs, {"max_tokens": 4})

    def test_runtime_child_probe_records_actual_command_work(self):
        class Observation:
            exit_code = 0

            def __init__(self, work_volume):
                self.work_volume = work_volume

        class Runtime:
            async def run_in_session(self, action):
                self.last_command = action.command
                command_start = monotonic_ns()
                command_end = monotonic_ns()
                return Observation(
                    {
                        "bytes_read": 128,
                        "bytes_written": 64,
                        "files_touched": 2,
                        "subprocess_count": 1,
                        "source": "runtime_child_probe",
                        "provenance": "measured",
                        "event_id": self.event_id,
                        "command_sha256": hashlib.sha256(action.command.encode()).hexdigest(),
                        "start_mono_ns": command_start,
                        "end_mono_ns": command_end,
                        "pid": 1234,
                    }
                )

        class Deployment:
            def __init__(self):
                self.runtime = Runtime()

        class Environment:
            def __init__(self):
                self.deployment = Deployment()

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="work-run")
            agent = _FakeAgent()
            agent._env = Environment()
            agent.tools.guard_multiline_input = lambda _action: "python guarded.py"
            hook = SWEAgentTelemetryHook(recorder)
            hook.on_init(agent=agent)
            hook.on_run_start()
            hook.on_step_start()
            hook.on_actions_generated(step={"action": "python raw.py"})
            hook.on_action_started(step={"action": "python raw.py"})
            agent._env.deployment.runtime.event_id = hook._tool_span.pre_event_id
            action = type("BashAction", (), {"command": "python guarded.py"})()
            asyncio.run(agent._env.deployment.runtime.run_in_session(action))
            hook.on_action_executed(step={})
            hook.on_step_done(step={}, info={})
            hook.on_run_done(trajectory=[], info={})
            recorder.finish_outer()
            tool_rows = [
                row
                for row in recorder.rows("tool")
                if row["event_kind"] == "tool_event" and row["terminal"]
            ]
            self.assertEqual(len(tool_rows), 1)
            row = tool_rows[0]
            self.assertEqual(row["status"], "success")
            self.assertEqual(row["actual_action"], "python guarded.py")
            self.assertEqual(row["runtime_command"], "python guarded.py")
            self.assertEqual(row["bytes_read"], 128)
            self.assertEqual(row["files_touched"], 2)
            self.assertEqual(row["subprocess_count"], 1)
            self.assertEqual(row["measurement_availability"]["bytes_read"], "measured")
            self.assertEqual(row["work_volume_probe"], "work_volume")

    def test_native_container_script_snapshot_is_bounded_archived_and_refreshed_after_edit(self):
        class NativeEnvironment:
            repo = type("Repo", (), {"repo_name": "fixture-repo"})()

            def __init__(self):
                self.content = "#!/bin/sh\necho first\n"
                self.read_paths = []
                self.commands = []

            def communicate(self, command, **_kwargs):
                self.commands.append(command)
                if command == "pwd":
                    return "/fixture-repo\n"
                raise AssertionError(f"unexpected state command: {command}")

            def read_file(self, path, encoding=None, errors=None):
                self.read_paths.append((path, encoding, errors))
                return self.content

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="script-capture-run")
            agent = _FakeAgent()
            agent._env = NativeEnvironment()
            hook = SWEAgentTelemetryHook(recorder)
            hook.on_init(agent=agent)
            hook.on_run_start()

            hook.on_step_start()
            hook.on_actions_generated(step={"action": "bash run.sh"})
            hook.on_action_started(step={"action": "bash run.sh"})
            first_state = hook._tool_span.identity["features"]["script_state"]
            hook.on_action_executed(step={})
            hook.on_step_done(step={}, info={})

            self.assertEqual(agent._env.read_paths[0], ("/fixture-repo/run.sh", "utf-8", "strict"))
            self.assertEqual(first_state["status"], "known")
            first_descriptor = first_state["paths"][0]
            self.assertEqual(first_descriptor["size_bytes"], len(agent._env.content.encode()))
            artifact = first_descriptor["content_artifact"]
            self.assertEqual((Path(temporary) / artifact["artifact_path"]).read_text(), agent._env.content)
            self.assertEqual(artifact["encoding"], "utf-8")
            self.assertEqual(artifact["hash_basis"], "decoded_text_utf8_reencoding")
            self.assertFalse(artifact["byte_exact"])
            self.assertFalse(artifact["truncated"])
            self.assertEqual(agent._env.commands, ["pwd"])
            state_rows = [
                row
                for row in recorder.rows("lifecycle")
                if row["event_kind"] == "script_read" and row["terminal"]
            ]
            self.assertEqual(len(state_rows), 1)
            self.assertEqual(state_rows[0]["script_state"]["generation"], first_state["generation"])

            agent._env.content = "#!/bin/sh\necho second\n"
            hook.on_step_start()
            hook.on_actions_generated(step={"action": "bash run.sh"})
            hook.on_action_started(step={"action": "bash run.sh"})
            second_state = hook._tool_span.identity["features"]["script_state"]
            self.assertGreater(second_state["generation"], first_state["generation"])
            self.assertNotEqual(
                second_state["paths"][0]["content_artifact"]["sha256"],
                first_descriptor["content_artifact"]["sha256"],
            )
            hook.on_action_executed(step={})
            hook.on_step_done(step={}, info={})
            hook.on_run_done(trajectory=[], info={})
            recorder.finish_outer()

    def test_script_snapshot_does_not_apply_later_cd_to_earlier_or_unordered_scripts(self):
        class NativeEnvironment:
            def __init__(self):
                self.repo = type("Repo", (), {"repo_name": "fixture-repo"})()
                self.read_paths = []

            def communicate(self, command, **_kwargs):
                if command == "pwd":
                    return "/fixture-repo\n"
                raise AssertionError(command)

            def read_file(self, path, encoding=None, errors=None):
                self.read_paths.append((path, encoding, errors))
                return "echo fixture\n"

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="script-order-run")
            agent = _FakeAgent()
            agent._env = NativeEnvironment()
            hook = SWEAgentTelemetryHook(recorder)
            hook.on_init(agent=agent)
            hook.on_run_start()
            hook.on_step_start()
            action = "python first.py; cd subdir; python second.py"
            hook.on_actions_generated(step={"action": action})
            hook.on_action_started(step={"action": action})
            state = hook._tool_span.identity["features"]["script_state"]
            self.assertEqual(state["status"], "invalidated")
            self.assertEqual(agent._env.read_paths, [])
            hook.on_action_executed(step={})
            hook.on_step_done(step={}, info={})
            hook.on_run_done(trajectory=[], info={})
            recorder.finish_outer()

    def test_linux_work_collector_receives_exact_guarded_action_boundaries(self):
        class Collector:
            def __init__(self):
                self.started = []
                self.ended = []

            def start_action(self, **values):
                self.started.append(values)

            def end_action(self, **values):
                self.ended.append(values)

        with TemporaryDirectory() as temporary:
            recorder = TelemetryV2(temporary, run_id="collector-run")
            collector = Collector()
            agent = _FakeAgent()
            agent.tools.guard_multiline_input = lambda _action: "git diff | head"
            hook = SWEAgentTelemetryHook(recorder, work_collector=collector)
            hook.on_init(agent=agent)
            hook.on_run_start()
            hook.on_step_start()
            hook.on_actions_generated(step={"action": "git diff"})
            hook.on_action_started(step={"action": "git diff"})
            event_id = hook._tool_span.pre_event_id
            hook.on_action_executed(step={"exit_status": "timeout"})
            hook.on_step_done(step={}, info={})
            hook.on_run_done(trajectory=[], info={})
            recorder.finish_outer()
            self.assertEqual(len(collector.started), 1)
            self.assertEqual(collector.started[0]["event_id"], event_id)
            self.assertEqual(collector.started[0]["command"], "git diff | head")
            self.assertEqual(len(collector.ended), 1)
            self.assertEqual(collector.ended[0]["event_id"], event_id)
            self.assertEqual(collector.ended[0]["status"], "timeout")
            terminal = [
                row
                for row in recorder.rows("tool")
                if row["event_kind"] == "tool_event" and row["terminal"]
            ][0]
            self.assertEqual(terminal["work_collector_event_id"], event_id)
            self.assertEqual(terminal["work_collector_status"], "ended")


class _ProxyUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b'{"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


class ProxyIntegrationTests(unittest.TestCase):
    def test_proxy_writes_same_v2_model_contract_and_retry_identity(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyUpstream)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            proxy = ProxyServer(
                ("127.0.0.1", 0),
                upstream_host="127.0.0.1",
                upstream_port=upstream.server_address[1],
                writer=JsonlWriter(root / "request_proxy.jsonl"),
                timeout_seconds=2,
                max_body_bytes=4096,
                v2_output_dir=root / "telemetry_v2",
                v2_run_id="proxy-run",
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=b'{"max_tokens":8}',
                    headers={
                        "X-EIC-Logical-Request-ID": "client-logical",
                        "X-EIC-Client-Span-ID": "client-span",
                        "X-EIC-Parent-Event-ID": "client-event",
                    },
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)
            rows = [
                json.loads(line)
                for line in (root / "telemetry_v2" / "model_events.jsonl").read_text().splitlines()
                if json.loads(line)["terminal"]
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["logical_request_id"], "client-logical")
            self.assertEqual(rows[0]["client_span_id"], "client-span")
            self.assertEqual(rows[0]["parent_event_id"], "client-event")
            self.assertEqual(rows[0]["input_tokens"], 5)
            self.assertEqual(rows[0]["output_tokens"], 2)
            self.assertEqual(rows[0]["context_tokens"], 5)
            self.assertEqual(rows[0]["context_tokens_provenance"], "derived_alias_of_prompt_tokens")
            self.assertEqual(rows[0]["transport_boundary"], "request_proxy")
            artifact = rows[0]["request_payload_artifact"]
            self.assertFalse(artifact["headers_persisted"])
            request_bytes = b'{"max_tokens":8}'
            response_bytes = b'{"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}'
            self.assertEqual(
                (root / "telemetry_v2" / artifact["request"]["artifact_path"]).read_bytes(),
                request_bytes,
            )
            self.assertEqual(
                (root / "telemetry_v2" / artifact["response"]["artifact_path"]).read_bytes(),
                response_bytes,
            )
            self.assertEqual(artifact["request"]["sha256"], hashlib.sha256(request_bytes).hexdigest())
            self.assertEqual(artifact["response"]["sha256"], hashlib.sha256(response_bytes).hexdigest())


if __name__ == "__main__":
    unittest.main()
