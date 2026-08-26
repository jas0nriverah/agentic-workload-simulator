import csv
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INGEST = ROOT / "scripts/assignment/ingest_sweagent_run.py"
COMPILE = ROOT / "scripts/assignment/compile_dataset.py"
INGEST_SPEC = importlib.util.spec_from_file_location("assignment_ingest", INGEST)
assert INGEST_SPEC is not None and INGEST_SPEC.loader is not None
INGEST_MODULE = importlib.util.module_from_spec(INGEST_SPEC)
INGEST_SPEC.loader.exec_module(INGEST_MODULE)
COMPILE_SPEC = importlib.util.spec_from_file_location("assignment_compile", COMPILE)
assert COMPILE_SPEC is not None and COMPILE_SPEC.loader is not None
COMPILE_MODULE = importlib.util.module_from_spec(COMPILE_SPEC)
COMPILE_SPEC.loader.exec_module(COMPILE_MODULE)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def write_sidecar(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


class AssignmentDatasetPipelineTests(unittest.TestCase):
    def test_sweagent_tool_vocabulary_is_classified_for_step3(self):
        expectations = {
            "open_file README.md": "read",
            "scroll_down": "read",
            "search_file bug src": "search",
            "list_files src": "traversal",
            "str_replace path old new": "patch",
            "create_file notes.txt": "write",
            "pytest -q": "test",
            "python3 script.py": "shell",
        }
        for action, expected in expectations.items():
            with self.subTest(action=action):
                self.assertEqual(INGEST_MODULE._operation(action)[1], expected)

    def _sources(self, root: Path) -> tuple[list[str], Path]:
        case_root = root / "case"
        case_root.mkdir()
        spec = {
            "run_id": "lite-repo-case-baseline-r0",
            "suite": "lite",
            "repository": "owner/repo",
            "category": "web",
            "instance_id": "owner__repo-1",
            "config_id": "shared-baseline",
            "repeat_id": "r0",
            "hardware_id": "h100-reference",
            "model_revision": "a" * 40,
            "swe_agent_revision": "b" * 40,
            "swe_bench_revision": "c" * 40,
            "command_sha256": "d" * 64,
            "settings": {"max_output_tokens": 256},
            "sweep_parameter": None,
            "sweep_value": None,
        }
        trajectory = {
            "trajectory": [
                {"action": "cat README.md", "execution_time": 0.1},
                {"action": "pytest -q", "execution_time": 0.1},
            ]
        }
        proxy = {
            "path": "/v1/chat/completions",
            "request_id": "request-1",
            "status_code": 200,
            "start_mono_ns": 1_000_000_000,
            "end_mono_ns": 1_300_000_000,
            "duration_ms": 300.0,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "request_bytes": 80,
            "response_bytes": 40,
        }
        summary = {"status": "completed", "duration_ms": 600.0}
        paths = {
            "spec": case_root / "spec.json",
            "trajectory": case_root / "source.traj",
            "proxy": case_root / "proxy.jsonl",
            "summary": case_root / "summary.json",
            "evaluator": case_root / "evaluator.json",
            "report": case_root / "official-report.json",
            "dataset": case_root / "dataset.jsonl",
            "predictions": case_root / "predictions.json",
            "manifest": root / "runtime-manifest.json",
            "case_result": case_root / "case_result.json",
        }
        write_json(paths["spec"], spec)
        write_json(paths["trajectory"], trajectory)
        paths["proxy"].write_text(json.dumps(proxy) + "\n", encoding="utf-8")
        write_json(paths["summary"], summary)
        write_json(
            paths["report"],
            {
                "total_instances": 1,
                "submitted_instances": 1,
                "completed_instances": 1,
                "resolved_instances": 0,
                "unresolved_instances": 1,
                "error_ids": [],
            },
        )
        paths["dataset"].write_text(
            json.dumps({"instance_id": spec["instance_id"]}) + "\n", encoding="utf-8"
        )
        write_json(
            paths["predictions"],
            [{"instance_id": spec["instance_id"], "model_patch": "patch"}],
        )
        write_json(
            paths["evaluator"],
            {
                "schema_version": "assignment-official-evaluator.v1",
                "official_resolved": False,
                "submitted": True,
                "instance_id": spec["instance_id"],
                "run_id": spec["run_id"],
                "report_path": str(paths["report"]),
                "report_sha256": hashlib.sha256(paths["report"].read_bytes()).hexdigest(),
                "dataset_path": str(paths["dataset"]),
                "dataset_sha256": hashlib.sha256(paths["dataset"].read_bytes()).hexdigest(),
                "predictions_path": str(paths["predictions"]),
                "predictions_sha256": hashlib.sha256(paths["predictions"].read_bytes()).hexdigest(),
                "counts": {
                    "total_instances": 1,
                    "submitted_instances": 1,
                    "completed_instances": 1,
                    "resolved_instances": 0,
                    "unresolved_instances": 1,
                    "error_instances": 0,
                },
            },
        )
        integrity = {
            "case_runner_sha256": "1" * 64,
            "evaluator_adapter_sha256": "2" * 64,
            "request_config_sha256": "3" * 64,
        }
        write_json(
            paths["manifest"],
            {
                "schema_version": "assignment-runtime-manifest.v1",
                "required_branch": "fixture",
                "required_commit": "e" * 40,
                "integrity": {
                    "case_runner_path": "/fixture/case-runner.py",
                    "case_runner_sha256": integrity["case_runner_sha256"],
                    "evaluator_adapter_path": "/fixture/evaluator.py",
                    "evaluator_adapter_sha256": integrity["evaluator_adapter_sha256"],
                    "request_config_path": "/fixture/request.yaml",
                    "request_config_sha256": integrity["request_config_sha256"],
                },
                "pins": {
                    "model_revision": spec["model_revision"],
                    "tokenizer_revision": "f" * 40,
                    "swe_agent_revision": spec["swe_agent_revision"],
                    "swe_bench_revision": spec["swe_bench_revision"],
                    "vllm_version": "0.10.0",
                },
            },
        )
        write_sidecar(paths["manifest"])
        artifacts = []
        for label in ("spec", "trajectory", "proxy", "summary", "evaluator", "report", "predictions"):
            source = paths[label]
            artifacts.append(
                {
                    "kind": label,
                    "path": str(source.relative_to(case_root)),
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "size": source.stat().st_size,
                }
            )
        write_json(
            paths["case_result"],
            {
                "schema_version": "assignment-case-result.v1",
                "resume_key": "fixture-resume",
                "status": "completed",
                "run_id": spec["run_id"],
                "case_sha256": "4" * 64,
                "manifest_sha256": hashlib.sha256(paths["manifest"].read_bytes()).hexdigest(),
                "integrity": integrity,
                "git": {"branch": "fixture", "commit": "e" * 40},
                "hardware": {"gpus": [{"name": "fixture", "memory_mib": 80000, "compute_capability": "9.0"}]},
                "runner": {"status": "completed", "returncode": 0, "output_dir": ".", "command_hash": spec["command_sha256"]},
                "evaluator": {"status": "completed", "runner_status": "completed", "result_path": paths["evaluator"].name, "official_resolved": False, "submitted": True},
                "normalization_sources": {
                    "run_spec": paths["spec"].name,
                    "trajectory": paths["trajectory"].name,
                    "model_events": paths["proxy"].name,
                    "runner_summary": paths["summary"].name,
                    "official_evaluator_result": paths["evaluator"].name,
                },
                "artifacts": artifacts,
            },
        )
        write_sidecar(paths["case_result"])
        normalized = root / "normalized" / "case"
        command = [
            sys.executable, str(INGEST),
            "--run-spec", str(paths["spec"]),
            "--trajectory", str(paths["trajectory"]),
            "--model-events", str(paths["proxy"]),
            "--runner-summary", str(paths["summary"]),
            "--evaluator-result", str(paths["evaluator"]),
            "--runtime-manifest", str(paths["manifest"]),
            "--case-result", str(paths["case_result"]),
            "--output-dir", str(normalized),
        ]
        return command, normalized

    def test_ingest_compile_is_portable_and_uses_assignment_ratio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, normalized = self._sources(root)
            ingested = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(ingested.returncode, 0, ingested.stdout + ingested.stderr)
            trajectory = json.loads((normalized / "trajectory.json").read_text())
            self.assertEqual(trajectory["tool_events_path"], "tool_events.jsonl")
            self.assertEqual(trajectory["model_events_path"], "model_events.jsonl")
            self.assertAlmostEqual(trajectory["tool_model_ratio"], 2 / 3)
            tool = json.loads((normalized / "tool_events.jsonl").read_text().splitlines()[0])
            model = json.loads((normalized / "model_events.jsonl").read_text())
            self.assertGreater(tool["command_bytes"], 0)
            self.assertEqual(model["max_output_tokens"], 256)

            relocated = root / "relocated"
            normalized.parent.rename(relocated)
            output = root / "dataset"
            compiled = subprocess.run(
                [sys.executable, str(COMPILE), "--runs-root", str(relocated), "--output-dir", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            with (output / "trajectories.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(float(rows[0]["tool_model_ratio"]), 2 / 3)
            inventory = json.loads((output / "inventory.json").read_text())
            self.assertEqual(
                inventory["ratio_definition"],
                "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
            )
            expected = hashlib.sha256((output / "inventory.json").read_bytes()).hexdigest()
            self.assertEqual((output / "inventory.sha256").read_text(), f"{expected}  inventory.json\n")

    def test_step2_baseline_is_bound_once_per_knob_without_double_counting(self):
        baseline = {
            "run_id": "baseline",
            "suite": "lite",
            "instance_id": "owner__repo-1",
            "repeat_id": "r0",
            "config_id": "shared-baseline",
            "e2e_wall_ms": 100,
        }
        measured = [
            {
                "run_id": "sweep-call-limit-10",
                "suite": "lite",
                "instance_id": "owner__repo-1",
                "repeat_id": "r0",
                "sweep_parameter": "call_limit",
                "sweep_value": "10",
            }
        ]
        rows, bindings = COMPILE_MODULE._baseline_sweep_rows(
            [baseline], measured,
            {
                "call_limit": 30,
                "max_output_tokens": 2048,
                "observation_length": 100000,
                "temperature": 0.0,
            },
        )
        derived = rows[1:]
        self.assertEqual(len(derived), 4)
        self.assertEqual(len(bindings), 4)
        self.assertEqual(
            {(row["sweep_parameter"], row["sweep_value"]) for row in derived},
            {("call_limit", "30"), ("max_output_tokens", "2048"),
             ("observation_length", "100000"), ("temperature", "0.0")},
        )
        self.assertEqual(len({row["run_id"] for row in rows}), len(rows))

    def test_failed_model_request_cannot_enter_completed_trajectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, _normalized = self._sources(root)
            proxy = root / "case" / "proxy.jsonl"
            row = json.loads(proxy.read_text())
            row["status_code"] = 500
            write_json(proxy, row)
            case_result_path = root / "case" / "case_result.json"
            case_result = json.loads(case_result_path.read_text(encoding="utf-8"))
            proxy_record = next(item for item in case_result["artifacts"] if item["path"] == "proxy.jsonl")
            proxy_record["sha256"] = hashlib.sha256(proxy.read_bytes()).hexdigest()
            proxy_record["size"] = proxy.stat().st_size
            write_json(case_result_path, case_result)
            write_sidecar(case_result_path)
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no positive model-request wall time", result.stderr + result.stdout)

    def test_manual_outcome_flags_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, _normalized = self._sources(root)
            command.extend(["--submitted", "true", "--resolved", "true"])
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unrecognized arguments", result.stderr)

    def test_tampered_official_report_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, _normalized = self._sources(root)
            write_json(root / "case" / "official-report.json", {"tampered": True})
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed after execution", result.stderr + result.stdout)

    def test_uninventoried_or_tampered_source_cannot_claim_measured_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command, _normalized = self._sources(root)
            proxy = root / "case" / "proxy.jsonl"
            proxy.write_text(proxy.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
            result = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed after execution", result.stderr + result.stdout)

    def test_case_result_and_runtime_manifest_sidecars_are_mandatory(self):
        for filename, expected in (
            ("case/case_result.json.sha256", "case result SHA-256 sidecar is missing"),
            ("runtime-manifest.json.sha256", "runtime manifest SHA-256 sidecar is missing"),
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                command, _normalized = self._sources(root)
                (root / filename).unlink()
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr + result.stdout)


if __name__ == "__main__":
    unittest.main()
