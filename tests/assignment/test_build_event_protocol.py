import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from agentic_sim.assignment.schema import MODEL_EVENT_FIELDS, TOOL_EVENT_FIELDS, TRAJECTORY_FIELDS


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "scripts" / "assignment" / "build_event_protocol.py"
EVALUATOR = ROOT / "scripts" / "assignment" / "evaluate_predictions.py"


def _write_json(path, value, *, sidecar=False):
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    if sidecar:
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def _hardware():
    return {
        "schema_version": "assignment.hardware-profile.v1",
        "hardware_id": "test-h100",
        "architecture": "Hopper",
        "cpu_cores": 16,
        "cpu_threads": 32,
        "cpu_base_ghz": 3.0,
        "system_memory_gib": 128.0,
        "storage_read_mbps": 5000.0,
        "storage_write_mbps": 3000.0,
        "gpu_count": 1,
        "gpu_compute_capability": 9.0,
        "gpu_memory_gib": 80.0,
        "gpu_memory_bandwidth_gbps": 3350.0,
        "gpu_bf16_tflops": 989.0,
    }


def _sha(seed):
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _trajectory(run_id, index, status="completed"):
    tool = 10.0 + index
    model = 40.0 + index * 2
    return {
        "schema_version": "assignment.trajectory.v1",
        "run_id": run_id,
        "suite": "lite",
        "repository": "repo",
        "category": "category",
        "instance_id": f"instance-{index}",
        "config_id": "baseline",
        "repeat_id": "r1",
        "sweep_parameter": None,
        "sweep_value": None,
        "status": status,
        "submitted": True if status == "completed" else None,
        "official_resolved": False if status == "completed" else None,
        "e2e_wall_ms": 80.0 + index if status == "completed" else None,
        "tool_wall_ms": tool if status == "completed" else None,
        "model_wall_ms": model if status == "completed" else None,
        "tool_model_ratio": tool / model if status == "completed" else None,
        "tool_event_count": 1,
        "model_event_count": 1,
        "hardware_id": "test-h100",
        "model_revision": "model-rev",
        "swe_agent_revision": "agent-rev",
        "swe_bench_revision": "bench-rev",
        "command_sha256": _sha(run_id),
        "tool_events_path": "tool_events.jsonl",
        "model_events_path": "model_events.jsonl",
        "unavailable_reason": None if status == "completed" else "failed run",
        "provenance": "measured" if status == "completed" else "unavailable",
    }


def _tool(run_id, index, status="completed"):
    return {
        "schema_version": "assignment.tool-event.v1",
        "run_id": run_id,
        "suite": "lite",
        "repository": "repo",
        "instance_id": f"instance-{index}",
        "config_id": "baseline",
        "repeat_id": "r1",
        "event_id": f"{run_id}-tool",
        "ordinal": 0,
        "tool_name": "cat",
        "operation_class": "read",
        "status": status,
        "start_mono_ns": None,
        "end_mono_ns": None,
        "wall_ms": 10.0 + index if status == "completed" else None,
        "command_bytes": 100 + index,
        "cpu_ms": 2.0 if status == "completed" else None,
        "bytes_read": 9999.0,
        "bytes_written": 4444.0,
        "command_sha256": _sha(run_id + "-tool"),
        "timing_scope": "process",
        "provenance": "measured" if status == "completed" else "unavailable",
    }


def _model(run_id, index, status="completed"):
    return {
        "schema_version": "assignment.model-event.v1",
        "run_id": run_id,
        "suite": "lite",
        "repository": "repo",
        "instance_id": f"instance-{index}",
        "config_id": "baseline",
        "repeat_id": "r1",
        "request_id": f"{run_id}-model",
        "ordinal": 0,
        "status": status,
        "start_mono_ns": None,
        "end_mono_ns": None,
        "wall_ms": 40.0 + index * 2 if status == "completed" else None,
        "input_tokens": 100 + index,
        "max_output_tokens": 64,
        "output_tokens": 40,
        "context_tokens": 300 + index,
        "request_bytes": 500,
        "response_bytes": 700,
        "cpu_activity_union_ms": 1.0 if status == "completed" else None,
        "cuda_activity_union_ms": 2.0 if status == "completed" else None,
        "kernel_duration_sum_ms": 2.5 if status == "completed" else None,
        "timing_scope": "request",
        "provenance": "measured" if status == "completed" else "unavailable",
    }


def _write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if value is None else str(value).lower() if isinstance(value, bool) else value for key, value in row.items()})


class EventProtocolAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cal_ids = tuple(f"cal-{number}" for number in range(4))
        self.hold_ids = ("hold-0", "hold-1")
        self.split = self.root / "split.json"
        _write_json(self.split, {"schema_version": "assignment.event-split-manifest.v1", "calibration_run_ids": list(self.cal_ids), "holdout_run_ids": list(self.hold_ids)}, sidecar=True)
        self.hardware = self.root / "hardware.json"
        _write_json(self.hardware, _hardware())
        self.runtime = self.root / "runtime.json"
        _write_json(self.runtime, {
            "schema_version": "assignment-runtime-manifest.v1",
            "required_branch": "parallel-h100-shards",
            "required_commit": "1" * 40,
            "repository_root": str(ROOT),
            "integrity": {
                "case_runner_path": str(ROOT / "scripts/assignment/sweagent_case_runner.py"),
                "case_runner_sha256": "a" * 64,
                "evaluator_adapter_path": str(ROOT / "scripts/assignment/evaluate_swebench_case.py"),
                "evaluator_adapter_sha256": "b" * 64,
                "request_config_path": str(ROOT / "cloud/lambda/sweagent_request.yaml"),
                "request_config_sha256": "c" * 64,
                "request_proxy_path": str(ROOT / "scripts/observability/request_proxy.py"),
                "request_proxy_sha256": "d" * 64,
                "adaptive_runner_path": str(ROOT / "scripts/assignment/sweagent_adaptive_runner.py"),
                "adaptive_runner_sha256": "e" * 64,
                "adaptive_runtime_path": str(ROOT / "scripts/assignment/adaptive_runtime.py"),
                "adaptive_runtime_sha256": "f" * 64,
                "adaptive_protocol_path": str(ROOT / "scripts/assignment/adaptive_event_protocol.py"),
                "adaptive_protocol_sha256": "1" * 64,
                "event_simulator_path": str(ROOT / "src/agentic_sim/assignment/event_simulator.py"),
                "event_simulator_sha256": "2" * 64,
            },
            "pins": {}, "datasets": {}, "model": {}, "runner": {}, "evaluator": {},
            "hardware": {"one_gpu_only": True}, "deadlines": {},
        }, sidecar=True)
        self.cal_traj = self.root / "calibration_trajectories.csv"
        self.cal_tools = self.root / "calibration_tools.csv"
        self.cal_models = self.root / "calibration_models.csv"
        _write_csv(self.cal_traj, TRAJECTORY_FIELDS, [_trajectory(run, index) for index, run in enumerate(self.cal_ids)])
        _write_csv(self.cal_tools, TOOL_EVENT_FIELDS, [_tool(run, index) for index, run in enumerate(self.cal_ids)])
        _write_csv(self.cal_models, MODEL_EVENT_FIELDS, [_model(run, index) for index, run in enumerate(self.cal_ids)])
        self.holdout_features = self.root / "holdout_features.json"
        _write_json(self.holdout_features, {
            "schema_version": "assignment.event-holdout-features.v1",
            "protocol_mode": "static_predeclared",
            "tool_events": [self._hold_tool(run, index) for index, run in enumerate(self.hold_ids)],
            "model_events": [self._hold_model(run, index) for index, run in enumerate(self.hold_ids)],
        }, sidecar=True)

    def tearDown(self):
        self.temp.cleanup()

    def _hold_tool(self, run_id, index):
        return {"schema_version": "assignment.tool-event-input.v1", "event_id": f"{run_id}-tool", "run_id": run_id, "split": "holdout", "operation_class": "read", "declared_command_bytes": 150 + index, "declared_read_bytes": 0, "declared_write_bytes": 0, "declared_path_count": 0, "hardware": _hardware()}

    def _hold_model(self, run_id, index):
        return {"schema_version": "assignment.model-event-input.v1", "request_id": f"{run_id}-model", "run_id": run_id, "split": "holdout", "input_tokens": 130 + index, "context_tokens": 330 + index, "max_output_tokens": 64, "hardware": _hardware()}

    def _run(self, *arguments, expect=0):
        result = subprocess.run([sys.executable, str(BUILDER), *map(str, arguments)], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, expect, msg=result.stderr)
        return result

    def _prepare(self, directory=None):
        directory = directory or self.root / "prepared"
        capture = self._capture(directory.parent / (directory.name + "-capture"))
        self._run("prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", directory)
        return directory

    def _capture(self, directory=None, *, expect=0):
        directory = directory or self.root / "capture"
        self._run("capture-features", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--runtime-manifest", self.runtime, "--feature-journal", self.holdout_features, "--output-dir", directory, expect=expect)
        return directory / "capture_receipt.json"

    def _freeze(self, directory):
        manifest = directory / "prediction_manifest.json"
        result = subprocess.run([sys.executable, str(EVALUATOR), "fit-freeze", "--calibration", directory / "calibration.json", "--holdout-features", directory / "holdout_features.json", "--prediction-manifest", manifest], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        return manifest

    def _write_holdout_tables(self):
        trajectories = self.root / "holdout_trajectories.csv"
        tools = self.root / "holdout_tools.csv"
        models = self.root / "holdout_models.csv"
        _write_csv(trajectories, TRAJECTORY_FIELDS, [_trajectory(run, index + 5) for index, run in enumerate(self.hold_ids)])
        _write_csv(tools, TOOL_EVENT_FIELDS, [_tool(run, index + 5) for index, run in enumerate(self.hold_ids)])
        _write_csv(models, MODEL_EVENT_FIELDS, [_model(run, index + 5) for index, run in enumerate(self.hold_ids)])
        return trajectories, tools, models

    def test_prepare_maps_only_pre_event_features_and_hashes_outputs(self):
        output = self._prepare()
        calibration = json.loads((output / "calibration.json").read_text(encoding="utf-8"))
        holdout = json.loads((output / "holdout_features.json").read_text(encoding="utf-8"))
        receipt = json.loads((output / "prepare_receipt.json").read_text(encoding="utf-8"))
        self.assertEqual({row["features"]["declared_read_bytes"] for row in calibration["tool_events"]}, {0})
        self.assertEqual({row["features"]["declared_write_bytes"] for row in calibration["tool_events"]}, {0})
        self.assertEqual([row["features"]["input_tokens"] for row in calibration["model_events"]], [100, 101, 102, 103])
        self.assertNotIn("output_tokens", calibration["model_events"][0]["features"])
        self.assertEqual(receipt["holdout_labels_accessed"], False)
        self.assertEqual(sorted(row["run_id"] for row in holdout["tool_events"]), list(self.hold_ids))
        capture_copy = output / "capture_receipt.json"
        self.assertTrue(capture_copy.is_file())
        self.assertTrue(capture_copy.with_suffix(".sha256").is_file())
        self.assertEqual(
            receipt["capture_receipt_sha256"], hashlib.sha256(capture_copy.read_bytes()).hexdigest()
        )
        self.assertEqual(receipt["captured_features_sha256"], receipt["holdout_features_sha256"])
        for path in (output / "calibration.json", output / "holdout_features.json", output / "prepare_receipt.json"):
            self.assertTrue(path.with_suffix(".sha256").is_file())

    def test_capture_features_binds_sources_and_records_bounded_chronology(self):
        capture = self._capture()
        receipt = json.loads(capture.read_text(encoding="utf-8"))
        copied = capture.parent / "holdout_features.json"
        self.assertEqual(receipt["holdout_labels_accessed"], False)
        self.assertTrue(receipt["target_derived_fields_rejected"])
        self.assertEqual(receipt["captured_features_sha256"], hashlib.sha256(copied.read_bytes()).hexdigest())
        self.assertEqual(receipt["feature_journal_sha256"], hashlib.sha256(self.holdout_features.read_bytes()).hexdigest())
        self.assertEqual(set(receipt["chronology_witness"]), {"captured_at_utc", "monotonic_ns", "boot_id"})
        self.assertTrue(capture.with_suffix(".sha256").is_file())
        self.assertTrue(copied.with_suffix(".sha256").is_file())

    def test_capture_rejects_target_fields_and_tampered_runtime(self):
        value = json.loads(self.holdout_features.read_text(encoding="utf-8"))
        value["model_events"][0]["wall_ms"] = 12.0
        _write_json(self.holdout_features, value, sidecar=True)
        result = subprocess.run([sys.executable, str(BUILDER), "capture-features", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--runtime-manifest", self.runtime, "--feature-journal", self.holdout_features, "--output-dir", self.root / "bad-target"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("target-derived", result.stderr)
        value["model_events"][0].pop("wall_ms")
        _write_json(self.holdout_features, value, sidecar=True)
        self.runtime.with_suffix(".sha256").write_text("0" * 64 + "  runtime.json\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(BUILDER), "capture-features", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--runtime-manifest", self.runtime, "--feature-journal", self.holdout_features, "--output-dir", self.root / "bad-runtime"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("runtime manifest", result.stderr)

    def test_prepare_requires_verified_capture_copy(self):
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", self.root / "missing.json", "--output-dir", self.root / "missing-capture"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("capture receipt", result.stderr)

    def test_prepare_rejects_tampered_captured_payload(self):
        capture = self._capture()
        copied = capture.parent / "holdout_features.json"
        copied.write_text(copied.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "bad-copy"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("captured holdout feature set", result.stderr)

    def test_prepare_is_deterministic_and_refuses_overwrite(self):
        first = self._prepare(self.root / "first")
        second = self._prepare(self.root / "second")
        self.assertEqual((first / "calibration.json").read_bytes(), (second / "calibration.json").read_bytes())
        self.assertEqual((first / "holdout_features.json").read_bytes(), (second / "holdout_features.json").read_bytes())
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", self.root / "first-capture" / "capture_receipt.json", "--output-dir", first], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("refusing to overwrite", result.stderr)

    def test_prepare_rejects_feature_leakage_and_split_coverage_gap(self):
        value = json.loads(self.holdout_features.read_text(encoding="utf-8"))
        value["model_events"][0]["output_tokens"] = 123
        _write_json(self.holdout_features, value, sidecar=True)
        self._capture(self.root / "bad-capture", expect=2)
        value["model_events"][0].pop("output_tokens")
        value["model_events"].pop()
        _write_json(self.holdout_features, value, sidecar=True)
        value["model_events"].append(self._hold_model("hold-1", 1))
        _write_json(self.holdout_features, value, sidecar=True)
        capture = self._capture(self.root / "gap-capture")
        copied = capture.parent / "holdout_features.json"
        copied_value = json.loads(copied.read_text(encoding="utf-8"))
        copied_value["model_events"].pop()
        _write_json(copied, copied_value, sidecar=True)
        capture_value = json.loads(capture.read_text(encoding="utf-8"))
        capture_value["captured_features_sha256"] = hashlib.sha256(copied.read_bytes()).hexdigest()
        _write_json(capture, capture_value, sidecar=True)
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "gap"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("lacks events", result.stderr)

    def test_prepare_rejects_tampered_split_and_hardware_mismatch(self):
        capture = self._capture(self.root / "split-capture")
        self.split.with_suffix(".sha256").write_text("0" * 64 + "  split.json\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "bad-split"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("tampered", result.stderr)
        _write_json(self.split, {"schema_version": "assignment.event-split-manifest.v1", "calibration_run_ids": list(self.cal_ids), "holdout_run_ids": list(self.hold_ids)}, sidecar=True)
        hardware_value = json.loads(self.hardware.read_text(encoding="utf-8"))
        hardware_value["gpu_count"] = 2
        _write_json(self.hardware, hardware_value, sidecar=True)
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "bad-hardware"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("hardware profile", result.stderr)

    def test_prepare_rejects_mismatched_calibration_trajectory_provenance(self):
        with self.cal_traj.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["hardware_id"] = "test-a100"
        _write_csv(self.cal_traj, TRAJECTORY_FIELDS, rows)
        capture = self._capture(self.root / "calibration-capture")
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "bad-calibration-hardware"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("trajectory hardware_id", result.stderr)

    def test_static_prepare_rejects_adaptive_feature_claim(self):
        value = json.loads(self.holdout_features.read_text(encoding="utf-8"))
        value["protocol_mode"] = "adaptive_online"
        _write_json(self.holdout_features, value, sidecar=True)
        result = subprocess.run([sys.executable, str(BUILDER), "capture-features", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--runtime-manifest", self.runtime, "--feature-journal", self.holdout_features, "--output-dir", self.root / "adaptive"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("rejects adaptive", result.stderr)

    def test_prepare_rejects_canonical_event_count_gap(self):
        with self.cal_traj.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["tool_event_count"] = "2"
        _write_csv(self.cal_traj, TRAJECTORY_FIELDS, rows)
        capture = self._capture(self.root / "count-capture")
        result = subprocess.run([sys.executable, str(BUILDER), "prepare", "--split-manifest", self.split, "--hardware-profile", self.hardware, "--calibration-trajectories", self.cal_traj, "--calibration-tool-events", self.cal_tools, "--calibration-model-events", self.cal_models, "--capture-receipt", capture, "--output-dir", self.root / "bad-count"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("count mismatch", result.stderr)

    def test_reveal_requires_verified_frozen_predictions_before_holdout_read(self):
        prepared = self._prepare()
        manifest = self._freeze(prepared)
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = subprocess.run([sys.executable, str(BUILDER), "reveal", "--split-manifest", self.split, "--prediction-manifest", manifest, "--holdout-trajectories", self.root / "does-not-exist.csv", "--holdout-tool-events", self.root / "does-not-exist.csv", "--holdout-model-events", self.root / "does-not-exist.csv", "--output-labels", self.root / "labels.json"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("tampered", result.stderr)
        self.assertNotIn("does-not-exist", result.stderr)

    def test_reveal_binds_exact_labels_to_manifest_after_freeze(self):
        prepared = self._prepare()
        manifest = self._freeze(prepared)
        trajectories, tools, models = self._write_holdout_tables()
        labels = self.root / "labels.json"
        result = self._run("reveal", "--split-manifest", self.split, "--prediction-manifest", manifest, "--holdout-trajectories", trajectories, "--holdout-tool-events", tools, "--holdout-model-events", models, "--output-labels", labels)
        reported = json.loads(result.stdout)
        payload = json.loads(labels.read_text(encoding="utf-8"))
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        self.assertEqual(payload["prediction_manifest_sha256"], digest)
        self.assertIn("prepare_receipt_sha256", payload)
        self.assertIn("calibration_sha256", payload)
        self.assertIn("holdout_features_sha256", payload)
        self.assertEqual(reported["prediction_manifest_sha256"], digest)
        self.assertEqual(len(payload["tool_events"]), 2)
        self.assertEqual(len(payload["model_events"]), 2)
        self.assertEqual(len(payload["trajectories"]), 2)
        self.assertTrue(labels.with_suffix(".sha256").is_file())

    def test_fit_and_reveal_require_receipt_bound_inputs(self):
        prepared = self._prepare()
        receipt = prepared / "prepare_receipt.json"
        receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
        receipt_value["hardware_profile_sha256"] = "0" * 64
        _write_json(receipt, receipt_value, sidecar=True)
        result = subprocess.run([sys.executable, str(EVALUATOR), "fit-freeze", "--calibration", prepared / "calibration.json", "--holdout-features", prepared / "holdout_features.json", "--prediction-manifest", prepared / "manifest.json", "--prepare-receipt", receipt], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = prepared / "manifest.json"
        trajectories, tools, models = self._write_holdout_tables()
        receipt_value["hardware_profile_sha256"] = "1" * 64
        _write_json(receipt, receipt_value, sidecar=True)
        result = subprocess.run([sys.executable, str(BUILDER), "reveal", "--split-manifest", self.split, "--prediction-manifest", manifest, "--prepare-receipt", receipt, "--holdout-trajectories", trajectories, "--holdout-tool-events", tools, "--holdout-model-events", models, "--output-labels", self.root / "labels.json"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not bound", result.stderr)

    def test_reveal_rejects_missing_or_mismatched_predicted_event(self):
        prepared = self._prepare()
        manifest = self._freeze(prepared)
        trajectories, tools, models = self._write_holdout_tables()
        with tools.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["event_id"] = "unexpected-tool"
        _write_csv(tools, TOOL_EVENT_FIELDS, rows)
        result = subprocess.run([sys.executable, str(BUILDER), "reveal", "--split-manifest", self.split, "--prediction-manifest", manifest, "--holdout-trajectories", trajectories, "--holdout-tool-events", tools, "--holdout-model-events", models, "--output-labels", self.root / "labels.json"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("coverage mismatch", result.stderr)

    def test_reveal_rejects_malformed_but_hash_consistent_prediction_manifest(self):
        prepared = self._prepare()
        manifest = self._freeze(prepared)
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["tool_predictions"][0]["predicted_ms"] = 0
        _write_json(manifest, value, sidecar=True)
        trajectories, tools, models = self._write_holdout_tables()
        result = subprocess.run([sys.executable, str(BUILDER), "reveal", "--split-manifest", self.split, "--prediction-manifest", manifest, "--holdout-trajectories", trajectories, "--holdout-tool-events", tools, "--holdout-model-events", models, "--output-labels", self.root / "labels.json"], text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive finite predicted_ms", result.stderr)


if __name__ == "__main__":
    unittest.main()
