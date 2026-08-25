import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentic_sim.runtime.backend import (
    BackendSelectionError,
    HealthCheckError,
    LeakageProtectionError,
    ResumeError,
    RuntimeLifecycle,
    RuntimeTimeoutError,
    artifact_root_for_backend,
    build_command,
    build_direct_command,
    build_process_environment,
    build_run_metadata,
    command_hash,
    docker_backend_available,
    deterministic_manifest,
    resolve_backend,
    select_backend,
)
from agentic_sim.runtime.vllm_config import resolve_vllm_config


class FakeProcess:
    def __init__(self, *, pid=43210, exits=False):
        self.pid = pid
        self.exits = exits
        self.killed = False
        self.wait_calls = []

    def poll(self):
        return 1 if self.exits or self.killed else None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if not self.killed and not self.exits:
            raise subprocess.TimeoutExpired("fake-vllm", timeout)
        return 0

    def kill(self):
        self.killed = True


class RuntimeBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from agentic_sim.runtime.vllm_config import resolve_vllm_config

        cls.config = resolve_vllm_config(environ={})

    def _lifecycle(self, root, *, backend="direct", process=None, health_probe=None, runner=None, monotonic=None):
        command = build_command(
            backend,
            self.config,
            container_name="test-vllm",
            python_executable="/usr/bin/python3",
        )
        return RuntimeLifecycle(
            backend=backend,
            config=self.config,
            command=command,
            environment=build_process_environment(base_environment={"PATH": "/usr/bin", "SECRET_TARGET_LABEL_PATH": "/x"}),
            artifact_root=Path(root),
            metadata=build_run_metadata(
                backend=backend,
                config=self.config,
                command=command,
                artifact_root=root,
                protocol_sha256="a" * 64,
                split_manifest_sha256="b" * 64,
                python_executable="/usr/bin/python3",
            ),
            protocol_sha256="a" * 64,
            split_manifest_sha256="b" * 64,
            process_factory=(lambda *args, **kwargs: process),
            command_runner=runner or (lambda *args, **kwargs: SimpleNamespace(returncode=0)),
            health_probe=health_probe or (lambda config, timeout: {"health": True}),
            monotonic=monotonic or (lambda: 0.0),
            sleep=lambda _: None,
        )

    def test_selectors_are_explicit_and_fail_closed(self):
        self.assertEqual(select_backend("docker"), "docker")
        self.assertEqual(select_backend("direct"), "direct")
        self.assertEqual(select_backend("docker", manifest_path=self._manifest("BACKEND=auto\n")), "docker")
        self.assertEqual(select_backend("auto", manifest_path=self._manifest("BACKEND=direct\n")), "direct")
        with self.assertRaises(BackendSelectionError):
            select_backend(None)
        with self.assertRaises(BackendSelectionError):
            select_backend("docker", manifest_path=self._manifest("RUNTIME_BACKEND=direct\n"))
        with self.assertRaises(BackendSelectionError):
            select_backend("auto", manifest_path=self._manifest("BACKEND=direct\nBACKEND=direct\n"))
        with self.assertRaises(BackendSelectionError):
            select_backend("host")

    def test_backend_command_construction_keeps_common_server_arguments_identical(self):
        docker = build_command(self._backend("docker"), self.config, container_name="test-vllm")
        direct = build_command(self._backend("direct"), self.config, python_executable="/opt/venv/bin/python")
        self.assertEqual(docker[0:2], ["docker", "run"])
        self.assertIn("--runtime", docker)
        self.assertEqual(docker[docker.index("--runtime") + 1], "nvidia")
        self.assertIn("--gpus", docker)
        self.assertIn(self.config["image"], docker)
        self.assertEqual(direct[:3], ["/opt/venv/bin/python", "-m", "vllm.entrypoints.openai.api_server"])
        self.assertNotIn("docker", " ".join(direct))
        for value in ("--model", self.config["model"], "--revision", self.config["model_revision"], "--port", str(self.config["port"]), "--dtype", "bfloat16", "--tool-call-parser", "qwen3_coder"):
            self.assertIn(value, docker)
            self.assertIn(value, direct)

    def test_selected_backend_does_not_fallback(self):
        from agentic_sim.runtime.backend import check_backend_prerequisites

        with patch("agentic_sim.runtime.backend.shutil.which", return_value=None):
            with self.assertRaisesRegex(Exception, "Docker backend selected"):
                check_backend_prerequisites("docker", self.config, check_external=False)

    def test_auto_selects_docker_only_after_toolkit_and_gpu_probe(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[-1:] == ["--format"]:
                return SimpleNamespace(returncode=0, stdout='{"nvidia":{}}')
            if command[:3] == ["docker", "info", "--format"]:
                return SimpleNamespace(returncode=0, stdout='{"nvidia":{}}')
            return SimpleNamespace(returncode=0, stdout="")

        self.assertTrue(
            docker_backend_available(
                self.config,
                runner=runner,
                executable_finder=lambda name: "/usr/bin/docker",
            )
        )
        self.assertEqual(resolve_backend("auto", self.config, availability=True), "docker")
        self.assertEqual(resolve_backend("auto", self.config, availability=False), "direct")
        self.assertGreaterEqual(len(calls), 4)

    def test_docker_unavailable_is_explicit_failure_but_direct_is_not_probed(self):
        with self.assertRaisesRegex(Exception, "refusing direct fallback"):
            resolve_backend("docker", self.config, availability=False)
        runner = Mock()
        self.assertEqual(resolve_backend("direct", self.config, runner=runner), "direct")
        runner.assert_not_called()

    def test_unprivileged_container_never_attempts_docker_in_docker(self):
        runner = Mock()
        self.assertFalse(
            docker_backend_available(
                self.config,
                runner=runner,
                executable_finder=lambda name: "/usr/bin/docker",
                environment={"container": "podman"},
            )
        )
        runner.assert_not_called()

    def test_tracing_prefix_is_shared_without_exposing_targets(self):
        docker = build_command(
            "docker",
            self.config,
            container_name="test-vllm",
            model_cache="/tmp/model-cache",
            trace_binary="/host-cuda/bin/nsys",
            trace_session="test-session",
            trace_root="/tmp/traces",
        )
        direct = build_command(
            "direct",
            self.config,
            python_executable="/opt/venv/bin/python",
            trace_binary="/usr/local/cuda/bin/nsys",
            trace_session="test-session",
        )
        for command in (docker, direct):
            rendered = " ".join(command)
            self.assertIn("--trace=cuda,osrt", rendered)
            self.assertIn("--cuda-event-trace=false", rendered)
            self.assertIn(self.config["model_revision"], rendered)
            self.assertNotIn("wall_ms", rendered)
        self.assertIn("--runtime", docker)
        self.assertIn("/host-cuda/bin/nsys", docker)
        self.assertEqual(direct[0], "/usr/local/cuda/bin/nsys")

    def test_backend_roots_are_disjoint_and_protected(self):
        docker = artifact_root_for_backend("/tmp/simulator-runs", "docker")
        direct = artifact_root_for_backend("/tmp/simulator-runs", "direct")
        self.assertNotEqual(docker, direct)
        with self.assertRaises(Exception):
            artifact_root_for_backend("/tmp/simulator-runs", "docker", protected_roots=(docker,))

    def test_direct_environment_drops_target_paths(self):
        environment = build_process_environment(
            base_environment={
                "PATH": "/usr/bin",
                "SECRET_TARGET_LABEL_PATH": "/canonical/holdout/row.json",
                "H100_HOLDOUT_WALL_MS": "123",
            }
        )
        self.assertNotIn("SECRET_TARGET_LABEL_PATH", environment)
        self.assertNotIn("H100_HOLDOUT_WALL_MS", environment)
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
        with self.assertRaises(LeakageProtectionError):
            build_direct_command(
                self.config,
                python_executable="/usr/bin/python3",
                target_paths=("/canonical/holdout/row.json",),
            )
        with self.assertRaises(LeakageProtectionError):
            build_direct_command(
                self.config,
                python_executable="/usr/bin/python3",
                phase="holdout",
                prediction_frozen=False,
            )
        with self.assertRaises(LeakageProtectionError):
            build_command(
                "docker",
                self.config,
                phase="sealed_holdout",
                prediction_frozen=False,
            )
        build_direct_command(
            self.config,
            python_executable="/usr/bin/python3",
            phase="holdout",
            prediction_frozen=True,
        )

    def test_health_failure_is_recorded_and_cleaned_up(self):
        process = FakeProcess(exits=True)
        lifecycle = self._lifecycle(
            tempfile.mkdtemp(),
            process=process,
            health_probe=lambda config, timeout: (_ for _ in ()).throw(HealthCheckError("bad health")),
        )
        with patch("agentic_sim.runtime.backend.os.killpg"):
            lifecycle.start()
            with self.assertRaises(HealthCheckError):
                lifecycle.wait_until_healthy(timeout=1)
        state = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["backend"], "direct")

    def test_direct_timeout_gracefully_then_forcibly_cleans_up(self):
        process = FakeProcess()
        lifecycle = self._lifecycle(
            tempfile.mkdtemp(),
            process=process,
            health_probe=lambda config, timeout: (_ for _ in ()).throw(HealthCheckError("not ready")),
            monotonic=self._clock([0.0, 0.1, 0.2, 2.0]),
        )
        with patch("agentic_sim.runtime.backend.os.killpg") as killpg:
            lifecycle.start()
            with self.assertRaises(RuntimeTimeoutError):
                lifecycle.wait_until_healthy(timeout=1, poll_interval=0)
        self.assertEqual([call.args[1].value for call in killpg.call_args_list], [15, 9])
        self.assertTrue(process.killed)
        state = json.loads(lifecycle.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "timed_out")

    def test_docker_timeout_uses_docker_stop_then_kill_without_direct_fallback(self):
        process = FakeProcess()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=1 if command[1] == "stop" else 0)

        lifecycle = self._lifecycle(
            tempfile.mkdtemp(),
            backend="docker",
            process=process,
            runner=runner,
            health_probe=lambda config, timeout: (_ for _ in ()).throw(HealthCheckError("not ready")),
            monotonic=self._clock([0.0, 0.1, 2.0]),
        )
        with patch("agentic_sim.runtime.backend.os.killpg") as killpg:
            lifecycle.start()
            with self.assertRaises(RuntimeTimeoutError):
                lifecycle.wait_until_healthy(timeout=1, poll_interval=0)
        self.assertEqual(calls[0][:2], ["docker", "stop"])
        self.assertEqual(calls[1], ["docker", "kill", "test-vllm"])
        killpg.assert_not_called()

    def test_resume_rejects_changed_protocol_and_accepts_matching_failure(self):
        with tempfile.TemporaryDirectory() as root:
            process = FakeProcess(exits=True)
            first = self._lifecycle(root, process=process)
            first.start()
            with self.assertRaises(HealthCheckError):
                first.wait_until_healthy(timeout=1)
            changed = self._lifecycle(root, process=FakeProcess(), monotonic=lambda: 0.0)
            changed.protocol_sha256 = "c" * 64
            with self.assertRaises(ResumeError):
                changed.start(resume=True)
            resumed = self._lifecycle(root, process=FakeProcess())
            session = resumed.start(resume=True)
            self.assertEqual(session.backend, "direct")

    def test_resume_rejects_changed_phase_or_prediction_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            first = self._lifecycle(root, process=FakeProcess(exits=True))
            first.start(phase="calibration")
            with self.assertRaises(HealthCheckError):
                first.wait_until_healthy(timeout=1)
            changed = self._lifecycle(root, process=FakeProcess())
            with self.assertRaises(ResumeError):
                changed.start(resume=True, phase="holdout", prediction_frozen=True)

    def test_run_metadata_and_manifests_are_deterministic_and_label_free(self):
        command = build_direct_command(self.config, python_executable="/usr/bin/python3")
        first = build_run_metadata(
            backend="direct",
            config=self.config,
            command=command,
            artifact_root="/tmp/runtime/direct",
            protocol_sha256="a" * 64,
            split_manifest_sha256="b" * 64,
            observed_environment={"gpu_name": "fixture", "cuda": "offline"},
        )
        second = build_run_metadata(
            backend="direct",
            config=self.config,
            command=command,
            artifact_root="/tmp/runtime/direct",
            protocol_sha256="a" * 64,
            split_manifest_sha256="b" * 64,
            observed_environment={"gpu_name": "fixture", "cuda": "offline"},
        )
        self.assertEqual(deterministic_manifest(first), deterministic_manifest(second))
        self.assertEqual(first["command_sha256"], command_hash(command))
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn("wall_ms", serialized)
        self.assertFalse(first["measured_target_access"])
        self.assertEqual(first["backend"], "direct")

    @staticmethod
    def _backend(name):
        return name

    @staticmethod
    def _manifest(contents):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        handle.write(contents)
        handle.close()
        return Path(handle.name)

    @staticmethod
    def _clock(values):
        iterator = iter(values)
        return lambda: next(iterator, values[-1])


if __name__ == "__main__":
    unittest.main()
