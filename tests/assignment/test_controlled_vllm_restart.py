import argparse
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import unittest
from unittest.mock import patch


PATH = Path(__file__).resolve().parents[2] / "scripts/validation/controlled_vllm_restart.py"
spec = importlib.util.spec_from_file_location("controlled_restart_test", PATH)
restart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restart)


class ExecCaptured(Exception):
    pass


class ControlledRestartTests(unittest.TestCase):
    def test_only_existing_reviewed_observer_can_be_reused(self):
        self.assertFalse(restart.reviewed_middleware(["python", "--port", "18222"]))
        self.assertTrue(restart.reviewed_middleware(["python", "--middleware", "serving_observer.ServingObserver", "--port", "18222"]))
        self.assertTrue(restart.reviewed_middleware(["python", "--middleware=serving_observer.ServingObserver"]))
        for values in (["other.Observer"], ["serving_observer.ServingObserver", "other.Observer"], [],
                       ["serving_observer.ServingObserver", "--middleware", "serving_observer.ServingObserver"]):
            with self.subTest(values=values), self.assertRaises(AssertionError):
                restart.reviewed_middleware(["python", "--middleware", *values])

    def fixture(self, root):
        original = {"pid": 123456, "start_ticks": 900, "state": "S", "cwd": str(root),
                    "cpu_affinity": list(range(24, 32)),
                    "argv": ["/original/venv/bin/python", "-m", "vllm.entrypoints.openai.api_server",
                             "--port", "18222", "--model", "/fixed/model", "--max-model-len", "32768",
                             "--tool-call-parser", "qwen3_coder", "--dtype", "bfloat16"],
                    "environment": {"SLURM_JOB_ID": "5741123", "SLURM_STEP_ID": "old-step",
                                    "PRESERVED_SETTING": "retained", "CUDA_VISIBLE_DEVICES": "0"}}
        restart.durable_json(root / "original.private.json", original)
        plan = {"hostname": socket.gethostname(), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "pid": original["pid"], "start_ticks": 900, "job_id": "5741123", "gpu_uuid": "GPU-exact",
                "port": 18222, "original_sha256": restart.digest(root / "original.private.json")}
        restart.durable_json(root / "plan.json", plan)
        env = {key: str(root / key) for key in ("EIC_SERVING_OBSERVER_JOURNAL", "EIC_SERVING_OBSERVER_ARTIFACT_DIR",
                "EIC_SERVER_IDENTITY", "EIC_SERVER_LEASE_ID", "EIC_COUNTER_EPOCH", "EIC_NATIVE_VLLM_JOURNAL",
                "EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256")}
        env.update(EIC_SERVER_DEDICATED="true", EIC_NATIVE_VLLM_OBSERVER="true")
        restart.durable_json(root / "observer-env.json", env)
        files = {}
        for name in ("serving_observer.py", "native_vllm_observer.py"):
            (root / name).write_text("# immutable fixture source\n")
            files[name] = restart.digest(root / name)
        restart.durable_json(root / "source.json", {"files": files})
        args = argparse.Namespace(execute=True, output=root, plan_sha256=restart.digest(root / "plan.json"),
                observer_env=root / "observer-env.json", observer_env_sha256=restart.digest(root / "observer-env.json"),
                source_manifest=root / "source.json", source_manifest_sha256=restart.digest(root / "source.json"))
        return args, original

    def test_identity_source_and_authorization_fail_before_any_signal(self):
        for fault in ("missing_execute", "plan_hash", "pid_reuse", "wrong_gpu", "wrong_cpu", "wrong_cuda", "changed_source", "precheck_failure"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args, original = self.fixture(root)
                if fault == "missing_execute": args.execute = False
                if fault == "plan_hash": args.plan_sha256 = "0" * 64
                if fault == "pid_reuse": original["start_ticks"] += 1
                if fault == "changed_source": (root / "serving_observer.py").write_text("changed")
                with patch.dict(os.environ, {"SLURM_JOB_ID": "5741123", "CUDA_VISIBLE_DEVICES": "GPU-exact" if fault == "wrong_cuda" else "0"}), patch.object(
                    restart.os, "sched_getaffinity", return_value={24} if fault == "wrong_cpu" else set(range(24, 32))
                ), patch.object(
                    restart, "process", return_value=original
                ), patch.object(restart.subprocess, "check_output", return_value="GPU-other\n" if fault == "wrong_gpu" else "GPU-exact\n"), patch.object(
                    restart.subprocess, "run", side_effect=RuntimeError("import failed") if fault == "precheck_failure" else None
                ), patch.object(restart.os, "kill") as kill, patch.object(restart.os, "execve") as execute:
                    with self.assertRaises((AssertionError, RuntimeError)):
                        restart.execute(args)
                    kill.assert_not_called()
                    execute.assert_not_called()

    def test_signals_only_bound_pid_and_preserves_launch_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args, original = self.fixture(root)
            stopped = []
            def process(pid):
                if pid == original["pid"]:
                    if stopped: raise FileNotFoundError()
                    return original
                self.assertEqual(pid, os.getpid())
                return {"start_ticks": 901}
            with patch.dict(os.environ, {"SLURM_JOB_ID": "5741123", "SLURM_STEP_ID": "replacement-step", "CUDA_VISIBLE_DEVICES": "0"}), patch.object(
                restart.os, "sched_getaffinity", return_value=set(range(24, 32))
            ), patch.object(
                restart, "process", side_effect=process
            ), patch.object(restart.subprocess, "check_output", return_value="GPU-exact\n"), patch.object(
                restart.subprocess, "run"
            ), patch.object(restart, "idle", return_value={"gauge_totals": {"running": 0}}), patch.object(
                restart.os, "kill", side_effect=lambda pid, sig: stopped.append((pid, sig))
            ), patch.object(restart.os, "chdir"), patch.object(restart.os, "execve", side_effect=ExecCaptured()) as execute:
                with self.assertRaises(ExecCaptured): restart.execute(args)
            self.assertEqual(stopped, [(original["pid"], signal.SIGTERM)])
            executable, argv, env = execute.call_args.args
            self.assertEqual(executable, original["argv"][0])
            self.assertEqual(restart.option(argv, "--max-model-len"), "65536")
            self.assertEqual(restart.option(argv, "--tool-call-parser"), "qwen3_coder")
            self.assertEqual(restart.option(argv, "--model"), "/fixed/model")
            self.assertEqual(env["PRESERVED_SETTING"], "retained")
            self.assertEqual(env["SLURM_STEP_ID"], "replacement-step")
            self.assertEqual(env["CUDA_VISIBLE_DEVICES"], "0")
            self.assertTrue((root / "restart-intent.json").is_file())
            self.assertTrue((root / "replacement-exec.json").is_file())
