import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/run_matrix.py"
SPEC = importlib.util.spec_from_file_location("assignment_run_matrix", SCRIPT)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

from scripts.assignment import sweagent_case_runner as CASE_RUNNER


PRODUCTION_CANDIDATE = (
    ROOT.parent
    / "h100-assignment-work-20260905"
    / "assignment"
    / "submission"
    / "20260908T140000Z-offline-v2"
    / "live-plan"
    / "production-candidates"
    / "historical-control-call30-input32768.jsonl"
)


def write_config(root: Path, *, global_deadline: int = 300) -> tuple[Path, str]:
    config = root / "assignment-config.json"
    config.write_text(json.dumps({
        "schema_version": RUNNER.SCHEMA_VERSION,
        "planning_only": True,
        "plan_id": "fixture",
        "execution_limits": {
            "concurrency": 1,
            "per_case_deadline_seconds": 30,
            "global_deadline_seconds": global_deadline,
        },
        "step_2": {"task_selection": {}, "knobs": []},
    }, sort_keys=True) + "\n", encoding="utf-8")
    return config, hashlib.sha256(config.read_bytes()).hexdigest()


def write_plan(root: Path, count: int = 2, *, global_deadline: int = 300) -> tuple[Path, Path, str]:
    config, config_sha256 = write_config(root, global_deadline=global_deadline)
    plan = root / "plan.jsonl"
    header = {
        "record_type": "plan",
        "schema_version": RUNNER.SCHEMA_VERSION,
        "planning_only": True,
        "plan_id": "fixture",
        "config_sha256": config_sha256,
        "concurrency": 1,
        "per_case_deadline_seconds": 30,
        "global_deadline_seconds": global_deadline,
        "sources": {"lite": {"task_count": count}, "verified": {"task_count": 0}},
        "step_2": {
            "task_selection": {},
            "selected_task_ids": {"lite": [], "verified": []},
            "knobs": [],
        },
        "execution_case_count": count,
    }
    rows = [header]
    for index in range(count):
        rows.append({
            "record_type": "case",
            "schema_version": RUNNER.SCHEMA_VERSION,
            "plan_id": "fixture",
            "resume_key": f"case-{index}",
            "concurrency": 1,
            "per_case_deadline_seconds": 30,
        })
    plan.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    digest = hashlib.sha256(plan.read_bytes()).hexdigest()
    Path(f"{plan}.sha256").write_text(f"{digest}  {plan.name}\n", encoding="utf-8")
    return plan, config, config_sha256


def write_runtime_manifest(root: Path) -> Path:
    manifest = root / "runtime-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "assignment-runtime-manifest.v1",
    }, sort_keys=True) + "\n", encoding="utf-8")
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    Path(f"{manifest}.sha256").write_text(f"{digest}  {manifest.name}\n", encoding="utf-8")
    return manifest


def reviewed_runner_args(plan: Path, config: Path, config_sha256: str, output: Path) -> list[str]:
    runtime_manifest = write_runtime_manifest(plan.parent)
    return [
        sys.executable, str(SCRIPT), "--plan", str(plan),
        "--runner", str(RUNNER.REVIEWED_RUNNER),
        "--runtime-manifest", str(runtime_manifest),
        "--config", str(config), "--config-sha256", config_sha256,
        "--output-dir", str(output),
    ]


def fake_case_run(command: list[str], _timeout: int, _stdout: Path, _stderr: Path) -> tuple[int, bool]:
    runtime_manifest = Path(command[command.index("--runtime-manifest") + 1])
    if not runtime_manifest.is_file():
        raise AssertionError("matrix did not pass an explicit runtime manifest")
    case_path = Path(command[command.index("--case-spec") + 1])
    output_dir = Path(command[command.index("--output-dir") + 1])
    case = json.loads(case_path.read_text(encoding="utf-8"))
    (output_dir / "case_result.json").write_text(json.dumps({
        "schema_version": RUNNER.RESULT_SCHEMA,
        "resume_key": case["resume_key"],
        "status": "completed",
    }, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / "runner.lifecycle.json").write_text(json.dumps({
        "schema_version": "assignment-matrix-runner-lifecycle.v1",
        "status": "completed",
        "cleanup_complete": True,
    }, sort_keys=True) + "\n", encoding="utf-8")
    return 0, False


def execute_in_process(argv: list[str], *, fake_runner: bool = False) -> int:
    parse_argv = argv[2:] if argv[:2] == [sys.executable, str(SCRIPT)] else argv
    args = RUNNER.parser().parse_args(parse_argv)
    if fake_runner:
        with mock.patch.object(RUNNER, "_run_case", side_effect=fake_case_run):
            return RUNNER.execute(args)
    return RUNNER.execute(args)


class AssignmentRunMatrixTests(unittest.TestCase):
    @unittest.skipUnless(PRODUCTION_CANDIDATE.is_file(), "offline production candidate inventory is unavailable")
    def test_generated_production_candidate_loads_through_plan_and_case_entrypoints(self):
        header, cases = RUNNER.load_plan(PRODUCTION_CANDIDATE)
        self.assertEqual(header["schema_version"], RUNNER.PRODUCTION_SCHEMA_VERSION)
        self.assertEqual(len(cases), 1088)
        self.assertTrue(all(case["schema_version"] == RUNNER.PRODUCTION_SCHEMA_VERSION for case in cases))
        self.assertTrue(all(set(case["settings"]) == RUNNER.PRODUCTION_SETTINGS for case in cases))
        with tempfile.TemporaryDirectory(prefix="production-case-entrypoint-") as temporary:
            case_path = Path(temporary) / "case.json"
            case_path.write_text(json.dumps(cases[0], sort_keys=True) + "\n", encoding="utf-8")
            loaded = CASE_RUNNER.load_case(case_path)
        self.assertEqual(loaded["case_id"], loaded["resume_key"])
        self.assertEqual(loaded["settings"]["max_input_tokens"], 32768)

    @unittest.skipUnless(PRODUCTION_CANDIDATE.is_file(), "offline production candidate inventory is unavailable")
    def test_candidate_inventory_is_preflight_only_and_cannot_execute(self):
        with tempfile.TemporaryDirectory(prefix="production-plan-reject-") as temporary:
            root = Path(temporary)
            plan = root / "candidate.jsonl"
            plan.write_bytes(PRODUCTION_CANDIDATE.read_bytes())
            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            Path(f"{plan}.sha256").write_text(f"{digest}  {plan.name}\n", encoding="utf-8")
            args = RUNNER.parser().parse_args([
                "--plan", str(plan),
                "--runner", str(RUNNER.REVIEWED_RUNNER),
                "--runtime-manifest", str(root / "missing-manifest.json"),
                "--output-dir", str(root / "out"),
            ])
            with self.assertRaisesRegex(RUNNER.ExecutionError, "candidate inventory is loadable for preflight only"):
                RUNNER.execute(args)

    def test_reviewed_runner_hash_matches_checked_in_adapter(self):
        self.assertEqual(
            hashlib.sha256(RUNNER.REVIEWED_RUNNER.read_bytes()).hexdigest(),
            RUNNER.REVIEWED_RUNNER_SHA256,
        )

    def test_default_validates_only_and_never_calls_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            output = root / "out"
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, output), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(output.exists())
            validation = json.loads(result.stdout)
            self.assertFalse(validation["execution_started"])
            self.assertEqual(validation["runtime_manifest"], str((root / "runtime-manifest.json").absolute()))
            self.assertEqual(validation["runtime_manifest_sha256"], hashlib.sha256((root / "runtime-manifest.json").read_bytes()).hexdigest())

    def test_runtime_manifest_requires_matching_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            args = reviewed_runner_args(plan, config, config_sha256, root / "out")
            manifest = root / "runtime-manifest.json"
            manifest.write_text(json.dumps({"schema_version": "assignment-runtime-manifest.v1", "tampered": True}) + "\n", encoding="utf-8")
            result = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("runtime manifest SHA-256", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_execute_requires_explicit_paid_work_acknowledgment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, root / "out") + ["--execute"], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("acknowledge-paid-gpu-work", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_live_execution_rejects_an_arbitrary_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            arbitrary = root / "arbitrary-runner"
            arbitrary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            arbitrary.chmod(0o755)
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, root / "out") + ["--runner", str(arbitrary), "--execute", "--acknowledge-paid-gpu-work"], capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("exact path", result.stderr)

    def test_serial_execution_and_exact_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root)
            output = root / "out"
            base = reviewed_runner_args(plan, config, config_sha256, output)
            paused = execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--max-cases", "1"], fake_runner=True)
            self.assertEqual(paused, 3)
            state = json.loads((output / "run_state.json").read_text())
            self.assertEqual(state["status"], "paused")
            self.assertEqual(state["completed_resume_keys"], ["case-0"])
            self.assertEqual(set(state["completed_case_results"]), {"case-0"})
            resumed = execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--resume"], fake_runner=True)
            self.assertEqual(resumed, 0)
            state = json.loads((output / "run_state.json").read_text())
            self.assertEqual(state["status"], "completed")
            self.assertEqual(state["completed_resume_keys"], ["case-0", "case-1"])

    def test_resume_rejects_completed_key_without_immutable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root)
            output = root / "out"
            base = reviewed_runner_args(plan, config, config_sha256, output)
            self.assertEqual(execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--max-cases", "1"], fake_runner=True), 3)
            (output / "cases/00000/case_result.json").unlink()
            with self.assertRaisesRegex(RUNNER.ExecutionError, "case_result|completed case result"):
                execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--resume"], fake_runner=True)

    def test_resume_rejects_tampered_completed_result_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root)
            output = root / "out"
            base = reviewed_runner_args(plan, config, config_sha256, output)
            self.assertEqual(execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--max-cases", "1"], fake_runner=True), 3)
            result_path = output / "cases/00000/case_result.json"
            result = json.loads(result_path.read_text())
            result["plan_id"] = "attacker-plan"
            result_path.write_text(json.dumps(result) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.ExecutionError, "plan_id|immutable bound result"):
                execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--resume"], fake_runner=True)

    def test_resume_cannot_extend_deadline_or_change_max_wall(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root)
            output = root / "out"
            base = reviewed_runner_args(plan, config, config_sha256, output)
            self.assertEqual(execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--max-wall-seconds", "60", "--max-cases", "1"], fake_runner=True), 3)
            state_path = output / "run_state.json"
            state = json.loads(state_path.read_text())
            state["deadline_epoch"] += 600
            state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(RUNNER.ExecutionError, "deadline binding"):
                execute_in_process(base + ["--execute", "--acknowledge-paid-gpu-work", "--resume"], fake_runner=True)

            output2 = root / "out-2"
            base2 = reviewed_runner_args(plan, config, config_sha256, output2)
            self.assertEqual(execute_in_process(base2 + ["--execute", "--acknowledge-paid-gpu-work", "--max-wall-seconds", "60", "--max-cases", "1"], fake_runner=True), 3)
            with self.assertRaisesRegex(RUNNER.ExecutionError, "cannot change"):
                execute_in_process(base2 + ["--execute", "--acknowledge-paid-gpu-work", "--resume", "--max-wall-seconds", "120"], fake_runner=True)

    def test_config_hash_and_cardinality_are_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 2)
            wrong_hash = subprocess.run(
                reviewed_runner_args(plan, config, "0" * 64, root / "wrong-hash-out"),
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(wrong_hash.returncode, 1)
            self.assertIn("--config-sha256", wrong_hash.stderr)
            rows = [json.loads(line) for line in plan.read_text().splitlines()]
            rows[0]["sources"]["lite"]["task_count"] = 1
            plan.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            Path(f"{plan}.sha256").write_text(f"{digest}  {plan.name}\n", encoding="utf-8")
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, root / "out"), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("cardinality", result.stderr)

    def test_tampered_plan_is_rejected_before_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            plan.write_text(plan.read_text() + "\n", encoding="utf-8")
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, root / "out"), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SHA-256", result.stderr)
            self.assertFalse((root / "out").exists())

    def test_rejects_parallel_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, config, config_sha256 = write_plan(root, 1)
            rows = [json.loads(line) for line in plan.read_text().splitlines()]
            rows[0]["concurrency"] = 2
            plan.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
            digest = hashlib.sha256(plan.read_bytes()).hexdigest()
            Path(f"{plan}.sha256").write_text(f"{digest}  {plan.name}\n", encoding="utf-8")
            result = subprocess.run(reviewed_runner_args(plan, config, config_sha256, root / "out"), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("concurrency=1", result.stderr)

    def test_failure_v2_cannot_claim_completed_status(self):
        with tempfile.TemporaryDirectory(prefix="matrix-failure-contract-") as temporary:
            path = Path(temporary) / "case_result.json"
            path.write_text(
                json.dumps({
                    "schema_version": RUNNER.FAILURE_RESULT_SCHEMA,
                    "resume_key": "case-0",
                    "status": "completed",
                    "accepted": False,
                    "reason": "fixture failure",
                    "artifacts": [],
                }) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RUNNER.ExecutionError, "cannot be completed"):
                RUNNER._validate_result(path, {"resume_key": "case-0"})

    def test_result_archive_preserves_multiple_retries_and_sidecars(self):
        with tempfile.TemporaryDirectory(prefix="matrix-result-history-") as temporary:
            root = Path(temporary)
            result = root / "case_result.json"
            archived_payloads: list[bytes] = []
            archived_sidecars: list[bytes] = []
            for attempt in (1, 2, 3):
                payload = json.dumps({"attempt": attempt, "status": "failed"}, sort_keys=True).encode("utf-8") + b"\n"
                sidecar = f"{hashlib.sha256(payload).hexdigest()}  {result.name}\n".encode("ascii")
                result.write_bytes(payload)
                Path(f"{result}.sha256").write_bytes(sidecar)
                archive = RUNNER._archive_result(result, remove=True)
                archived_payloads.append(payload)
                archived_sidecars.append(sidecar)
                self.assertFalse(result.exists())
                self.assertFalse(Path(f"{result}.sha256").exists())
                self.assertEqual((archive / result.name).read_bytes(), payload)
                self.assertEqual((archive / f"{result.name}.sha256").read_bytes(), sidecar)
                archive_metadata = json.loads((archive / "archive.json").read_text(encoding="utf-8"))
                self.assertEqual(archive_metadata["sha256"], hashlib.sha256(payload).hexdigest())

            archives = sorted((root / "case_result_history").iterdir())
            self.assertEqual(len(archives), 3)
            for archive, payload, sidecar in zip(archives, archived_payloads, archived_sidecars):
                self.assertEqual((archive / result.name).read_bytes(), payload)
                self.assertEqual((archive / f"{result.name}.sha256").read_bytes(), sidecar)
            self.assertEqual(
                len({archive.name for archive in archives}),
                3,
                "each retry must receive an independent archive directory",
            )

    def test_final_failure_inventory_hashes_closed_stderr_and_other_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="matrix-final-inventory-") as temporary:
            case_root = Path(temporary) / "case"
            case_root.mkdir()
            stderr = case_root / "runner.stderr.log"
            with stderr.open("wb") as stream:
                stream.write(b"agent stderr written before finalization\n")
                stream.flush()
            nested = case_root / "runner_attempts/attempt-001"
            nested.mkdir(parents=True)
            (nested / "trajectory.json").write_bytes(b'{"steps":[]}\n')
            case = {"resume_key": "case-0"}

            marker = RUNNER._finalize_failure_result(
                case_root,
                case,
                status="failed",
                reason="runner exited after stderr was closed",
                returncode=7,
                timed_out=False,
                lifecycle={"cleanup_complete": True},
            )

            self.assertEqual(marker["schema_version"], RUNNER.FAILURE_RESULT_SCHEMA)
            self.assertEqual(marker["status"], "failed")
            self.assertEqual(marker["inventory_errors"], [])
            inventory = {item["path"]: item for item in marker["artifacts"] if item["kind"] == "file"}
            self.assertIn("runner.stderr.log", inventory)
            self.assertIn("runner_attempts/attempt-001/trajectory.json", inventory)
            for relative, record in inventory.items():
                path = case_root / relative
                self.assertEqual(record["sha256"], hashlib.sha256(path.read_bytes()).hexdigest(), relative)
                self.assertEqual(record["size"], path.stat().st_size, relative)
                self.assertEqual(record["mtime_ns"], path.stat().st_mtime_ns, relative)
            self.assertNotIn("case_result.json", inventory)
            self.assertNotIn("case_result.json.sha256", inventory)


if __name__ == "__main__":
    unittest.main()
