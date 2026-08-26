import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.assignment.test_event_simulator import calibration_records, holdout_features


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/evaluate_predictions.py"


def write_json(path, value, *, sidecar=False):
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    if sidecar:
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        path.with_suffix(".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def run_cli(*arguments):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, arguments)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def prepare_frozen(root):
    tools, models, trajectories = calibration_records()
    holdout_tools, holdout_models = holdout_features()
    calibration = root / "calibration.json"
    features = root / "holdout_features.json"
    capture_receipt = root / "capture_receipt.json"
    receipt = root / "prepare_receipt.json"
    manifest = root / "prediction_manifest.json"
    calibration_value = {
        "schema_version": "assignment.event-calibration-set.v1",
        "tool_events": tools,
        "model_events": models,
        "trajectories": trajectories,
    }
    features_value = {
        "schema_version": "assignment.event-holdout-features.v1",
        "protocol_mode": "static_predeclared",
        "tool_events": holdout_tools,
        "model_events": holdout_models,
    }
    write_json(calibration, calibration_value, sidecar=True)
    write_json(features, features_value, sidecar=True)
    calibration_digest = hashlib.sha256(calibration.read_bytes()).hexdigest()
    features_digest = hashlib.sha256(features.read_bytes()).hexdigest()
    hardware_digest = hashlib.sha256(
        json.dumps(holdout_tools[0]["hardware"], indent=2, sort_keys=True).encode("utf-8") + b"\n"
    ).hexdigest()
    capture_value = {
        "schema_version": "assignment.event-feature-capture-receipt.v1",
        "protocol_mode": "static_predeclared",
        "protocol_scope": "predeclared_static_workload_only",
        "split_manifest_sha256": "a" * 64,
        "hardware_profile_sha256": hardware_digest,
        "runtime_manifest_sha256": "c" * 64,
        "feature_journal_sha256": "e" * 64,
        "captured_features_sha256": features_digest,
        "split_manifest_path": str((root / "split.json").resolve()),
        "hardware_profile_path": str((root / "hardware.json").resolve()),
        "runtime_manifest_path": str((root / "runtime.json").resolve()),
        "feature_journal_path": str((root / "feature_journal.json").resolve()),
        "captured_features_path": str(features.resolve()),
        "capture_script_path": str((ROOT / "scripts/assignment/build_event_protocol.py").resolve()),
        "capture_script_sha256": hashlib.sha256(
            (ROOT / "scripts/assignment/build_event_protocol.py").read_bytes()
        ).hexdigest(),
        "holdout_run_ids": ["hold-1", "hold-2"],
        "holdout_labels_accessed": False,
        "target_derived_fields_rejected": True,
        "chronology_witness": {
            "captured_at_utc": "2026-01-01T00:00:00Z",
            "monotonic_ns": 1,
            "boot_id": None,
        },
    }
    write_json(capture_receipt, capture_value, sidecar=True)
    capture_digest = hashlib.sha256(capture_receipt.read_bytes()).hexdigest()
    write_json(
        receipt,
        {
            "schema_version": "assignment.event-protocol-prepare-receipt.v1",
            "protocol_mode": "static_predeclared",
            "protocol_scope": "predeclared_static_workload_only",
            "split_manifest_sha256": "a" * 64,
            "hardware_profile_sha256": hardware_digest,
            "calibration_sha256": calibration_digest,
            "holdout_features_sha256": features_digest,
            "capture_receipt_sha256": capture_digest,
            "captured_features_sha256": features_digest,
            "runtime_manifest_sha256": "c" * 64,
            "capture_script_sha256": capture_value["capture_script_sha256"],
            "capture_chronology_witness": capture_value["chronology_witness"],
            "calibration_run_ids": [row["run_id"] for row in trajectories],
            "holdout_run_ids": ["hold-1", "hold-2"],
            "holdout_labels_accessed": False,
            "calibration_tool_declared_read_bytes": 0,
            "calibration_tool_declared_write_bytes": 0,
            "forbidden_holdout_feature_classes": ["wall", "cpu", "cuda", "kineto", "timestamps", "output_tokens", "response_bytes"],
        },
        sidecar=True,
    )
    result = run_cli(
        "fit-freeze",
        "--calibration",
        calibration,
        "--holdout-features",
        features,
        "--prediction-manifest",
        manifest,
        "--prepare-receipt",
        receipt,
    )
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return manifest, json.loads(result.stdout)


def labels_for(manifest_path, *, multiplier=1.0):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return {
        "schema_version": "assignment.event-holdout-labels.v1",
        "prediction_manifest_sha256": digest,
        **{
            key: value
            for key, value in manifest["event_protocol_binding"].items()
            if key != "protocol_mode"
        },
        "tool_events": [
            {
                "schema_version": "assignment.tool-holdout-label.v1",
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "status": "completed",
                "observed_ms": row["predicted_ms"] * multiplier,
                "unavailable_reason": None,
            }
            for row in manifest["tool_predictions"]
        ],
        "model_events": [
            {
                "schema_version": "assignment.model-holdout-label.v1",
                "request_id": row["request_id"],
                "run_id": row["run_id"],
                "status": "completed",
                "observed_ms": row["predicted_ms"] * multiplier,
                "unavailable_reason": None,
            }
            for row in manifest["model_predictions"]
        ],
        "trajectories": [
            {
                "schema_version": "assignment.trajectory-holdout-label.v1",
                "run_id": row["run_id"],
                "status": "completed",
                "observed_ms": row["predicted_ms"] * multiplier,
                "unavailable_reason": None,
            }
            for row in manifest["trajectory_predictions"]
        ],
    }


class FreezeCliTests(unittest.TestCase):
    def test_fit_freeze_is_separate_and_does_not_accept_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, result = prepare_frozen(root)
            self.assertEqual(result["status"], "predictions_frozen")
            self.assertFalse(result["holdout_labels_accessed"])
            self.assertTrue(manifest.is_file())
            self.assertTrue(manifest.with_suffix(".sha256").is_file())
            help_result = run_cli("fit-freeze", "--help")
            self.assertEqual(help_result.returncode, 0)
            self.assertNotIn("holdout-labels", help_result.stdout)

    def test_fit_freeze_rejects_label_smuggling_in_holdout_features(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepare_frozen(root)
            calibration = root / "calibration.json"
            features = root / "holdout_features.json"
            receipt = root / "prepare_receipt.json"
            value = json.loads(features.read_text(encoding="utf-8"))
            value["model_events"][0]["wall_ms"] = 10.0
            # Simulate an attacker who also recomputes local sidecars/receipt:
            # the strict feature schema must still reject the measured label.
            write_json(features, value, sidecar=True)
            receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
            receipt_value["holdout_features_sha256"] = hashlib.sha256(features.read_bytes()).hexdigest()
            write_json(receipt, receipt_value, sidecar=True)
            result = run_cli(
                "fit-freeze",
                "--calibration",
                calibration,
                "--holdout-features",
                features,
                "--prediction-manifest",
                root / "second-manifest.json",
                "--prepare-receipt",
                receipt,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not bind", result.stderr)


class EvaluationGateTests(unittest.TestCase):
    def test_exact_predictions_pass_every_event_and_e2e_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels = root / "labels.json"
            report_path = root / "report.json"
            write_json(labels, labels_for(manifest), sidecar=True)
            result = run_cli(
                "score",
                "--prediction-manifest",
                manifest,
                "--holdout-labels",
                labels,
                "--output",
                report_path,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["passed"])
            self.assertTrue(report["summaries"]["tool_events"]["all_available_within_25_percent"])
            self.assertTrue(report["summaries"]["model_events"]["all_available_within_25_percent"])
            self.assertTrue(report["summaries"]["trajectories"]["all_available_within_25_percent"])
            self.assertEqual(json.loads(report_path.read_text()), report)
            self.assertEqual(
                report_path.with_suffix(".sha256").read_text(encoding="utf-8"),
                f"{hashlib.sha256(report_path.read_bytes()).hexdigest()}  report.json\n",
            )
            self.assertIn("prediction_manifest_sha256", report)
            self.assertIn("holdout_labels_sha256", report)

    def test_single_event_above_25_percent_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels_value = labels_for(manifest)
            labels_value["tool_events"][0]["observed_ms"] *= 2.0
            labels = root / "labels.json"
            write_json(labels, labels_value, sidecar=True)
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertFalse(report["passed"])
            self.assertGreater(
                report["summaries"]["tool_events"]["max_absolute_percentage_error"], 25.0
            )

    def test_single_trajectory_above_25_percent_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels_value = labels_for(manifest)
            labels_value["trajectories"][0]["observed_ms"] *= 2.0
            labels = root / "labels.json"
            write_json(labels, labels_value, sidecar=True)
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 1)
            report = json.loads(result.stdout)
            self.assertFalse(report["summaries"]["trajectories"]["all_available_within_25_percent"])

    def test_unavailable_event_is_reported_but_trajectory_must_be_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels_value = labels_for(manifest)
            labels_value["tool_events"][0].update(
                status="unavailable", observed_ms=None, unavailable_reason="trace missing"
            )
            labels = root / "labels.json"
            write_json(labels, labels_value, sidecar=True)
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertFalse(report["passed"])
            self.assertFalse(report["coverage_complete"])
            self.assertEqual(report["summaries"]["tool_events"]["unavailable_count"], 1)

            labels_value = labels_for(manifest)
            labels_value["trajectories"][0].update(
                status="unavailable", observed_ms=None, unavailable_reason="run failed"
            )
            write_json(labels, labels_value, sidecar=True)
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("trajectory", result.stderr)

    def test_hash_mismatch_and_manifest_tampering_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels_value = labels_for(manifest)
            labels_value["prediction_manifest_sha256"] = "0" * 64
            labels = root / "labels.json"
            write_json(labels, labels_value, sidecar=True)
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("not bound", result.stderr)

            write_json(labels, labels_for(manifest), sidecar=True)
            manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("tampered", result.stderr)

    def test_score_rejects_holdout_label_sidecar_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest, _ = prepare_frozen(root)
            labels = root / "labels.json"
            write_json(labels, labels_for(manifest), sidecar=True)
            labels.write_text(labels.read_text(encoding="utf-8") + " ", encoding="utf-8")
            result = run_cli(
                "score", "--prediction-manifest", manifest, "--holdout-labels", labels
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("holdout label set or SHA-256 sidecar was tampered", result.stderr)


if __name__ == "__main__":
    unittest.main()
