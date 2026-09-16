import hashlib
import json
import multiprocessing
from pathlib import Path
from queue import Empty
import subprocess
import sys
import tempfile
import unittest

from scripts.assignment.adaptive_event_protocol import (
    ADAPTIVE_LABELS_SCHEMA,
    AdaptiveEventProtocol,
    AdaptiveProtocolError,
    FrozenCalibrationModel,
    freeze_calibration_model,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "assignment" / "adaptive_event_protocol.py"


HARDWARE = {
    "schema_version": "assignment.hardware-profile.v1",
    "hardware_id": "h100-test",
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


def tool(run_id="holdout-1", ordinal=0):
    return {
        "schema_version": "assignment.tool-event-input.v1",
        "event_id": f"{run_id}-tool-{ordinal}",
        "run_id": run_id,
        "split": "holdout",
        "operation_class": "read",
        "declared_command_bytes": 120,
        "declared_read_bytes": 2048,
        "declared_write_bytes": 0,
        "declared_path_count": 2,
        "hardware": HARDWARE,
    }


def model(run_id="holdout-1", ordinal=0):
    return {
        "schema_version": "assignment.model-event-input.v1",
        "request_id": f"{run_id}-request-{ordinal}",
        "run_id": run_id,
        "split": "holdout",
        "input_tokens": 400,
        "context_tokens": 600,
        "max_output_tokens": 128,
        "hardware": HARDWARE,
    }


class StepClock:
    def __init__(self):
        self.value = 100

    def witness(self):
        self.value += 1
        return {
            "captured_at_utc": f"2026-08-25T00:00:{self.value:02d}Z",
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "monotonic_ns": self.value,
            "boot_id": "test-boot",
        }


def model_blocks():
    return {
        "tool_event": {"alpha": 0.1, "coefficients": [10.0] + [0.0] * 13},
        "model_event": {"alpha": 0.1, "coefficients": [20.0] + [0.0] * 7},
        "trajectory": {"alpha": 0.1, "coefficients": [100.0] + [0.0] * 4},
    }


def hashes(letter):
    return {
        name: letter * 64
        for name in (
            "split_manifest_sha256",
            "runtime_manifest_sha256",
            "hardware_profile_sha256",
            "model_revision_sha256",
        )
    }


def _concurrent_prediction_worker(root_text, ordinal, ready, start, results):
    """Construct a stale protocol snapshot, then race one journal append."""
    root = Path(root_text)
    values = hashes("a")
    protocol = AdaptiveEventProtocol(
        root,
        FrozenCalibrationModel.load(root / "calibration_model.json"),
        **values,
    )
    ready.set()
    if not start.wait(timeout=10):
        results.put(("error", "worker start timeout"))
        return
    try:
        record = protocol.predict_event("tool", tool(ordinal=ordinal))
    except AdaptiveProtocolError as exc:
        results.put(("rejected", str(exc)))
    else:
        results.put(("predicted", record["event_ordinal"]))


class AdaptiveProtocolTests(unittest.TestCase):
    def make_protocol(self, directory, clock=None):
        model_path = directory / "calibration_model.json"
        values = hashes("a")
        freeze_calibration_model(
            model_blocks(), model_path, calibration_run_ids=["cal-0", "cal-1"], **values
        )
        frozen = FrozenCalibrationModel.load(model_path)
        return AdaptiveEventProtocol(directory, frozen, clock=clock, **values)

    def test_model_is_frozen_and_bound_before_holdout(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model_path = directory / "model.json"
            values = hashes("b")
            digest = freeze_calibration_model(
                model_blocks(), model_path, calibration_run_ids=["cal-0"], **values
            )
            loaded = FrozenCalibrationModel.load(model_path)
            self.assertEqual(digest, loaded.sha256)
            self.assertEqual(values, loaded.bindings())
            bad_model = dict(model_blocks())
            bad_model["tool_event"] = {"observed_ms": 1}
            with self.assertRaisesRegex(AdaptiveProtocolError, "target-derived"):
                freeze_calibration_model(
                    bad_model, directory / "bad.json", calibration_run_ids=["cal-0"], **values
                )
            model_path.write_text(model_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(AdaptiveProtocolError, "tampered"):
                FrozenCalibrationModel.load(model_path)

    def test_arm_requires_pretrajectory_prediction_or_declared_method(self):
        with tempfile.TemporaryDirectory() as temporary:
            protocol = self.make_protocol(Path(temporary))
            with self.assertRaisesRegex(AdaptiveProtocolError, "exactly one"):
                protocol.arm_trajectory("holdout-1")
            with self.assertRaisesRegex(AdaptiveProtocolError, "exactly one"):
                protocol.arm_trajectory(
                    "holdout-1", predicted_e2e_ms=100, e2e_prediction_method="x"
                )
            arm = protocol.arm_trajectory(
                "holdout-1", e2e_prediction_method="declared_preexecution_method"
            )
            self.assertEqual(arm["split"], "holdout")
            self.assertEqual(
                arm["binding"]["calibration_model_sha256"], protocol.calibration_model.sha256
            )

    def test_feature_boundary_rejects_measured_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            protocol = self.make_protocol(Path(temporary), StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            for field in (
                "wall_ms", "cpu_ms", "cuda_ms", "kineto_ms", "output_tokens", "response_data"
            ):
                row = dict(model())
                row[field] = 1
                with self.subTest(field=field):
                    with self.assertRaisesRegex(AdaptiveProtocolError, "target-derived"):
                        protocol.predict_event("model", row)

    def test_prediction_is_durable_before_label_and_resume_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clock = StepClock()
            protocol = self.make_protocol(directory, clock)
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            predicted = protocol.predict_event("tool", tool())
            self.assertTrue(predicted["record_sha256"])
            resumed = self.make_protocol(directory, clock)
            self.assertEqual(resumed.predict_event("tool", tool()), predicted)
            with self.assertRaisesRegex(AdaptiveProtocolError, "unrevealed prediction"):
                resumed.predict_event("model", model())
            resumed.reveal_event_label("tool", tool()["event_id"], {"observed_ms": 10})
            next_prediction = resumed.predict_event("model", model())
            self.assertEqual(next_prediction["event_ordinal"], 1)

    def test_independent_protocol_instances_refresh_before_ordering_decisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clock = StepClock()
            first = self.make_protocol(directory, clock)
            second = self.make_protocol(directory, clock)
            first.arm_trajectory("holdout-1", predicted_e2e_ms=100)

            first_prediction = first.predict_event("tool", tool())
            with self.assertRaisesRegex(AdaptiveProtocolError, "unrevealed prediction"):
                second.predict_event("model", model())

            second.reveal_event_label("tool", tool()["event_id"], {"observed_ms": 10})
            next_prediction = first.predict_event("model", model())
            self.assertEqual(next_prediction["event_ordinal"], 1)
            self.assertEqual(next_prediction["chain_prev_sha256"], second._records[-1]["record_sha256"])
            self.assertEqual(first_prediction["event_ordinal"], 0)

    def test_concurrent_stale_instances_admit_only_one_pending_prediction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            protocol = self.make_protocol(directory)
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)

            context = multiprocessing.get_context("spawn")
            ready = [context.Event(), context.Event()]
            start = context.Event()
            results = context.Queue()
            workers = [
                context.Process(
                    target=_concurrent_prediction_worker,
                    args=(str(directory), ordinal, ready[ordinal], start, results),
                )
                for ordinal in range(2)
            ]
            for worker in workers:
                worker.start()
            try:
                for event in ready:
                    self.assertTrue(event.wait(timeout=15), "worker did not construct its protocol snapshot")
                start.set()
                received = []
                while len(received) < len(workers):
                    try:
                        received.append(results.get(timeout=15))
                    except Empty as exc:
                        self.fail(f"worker did not report a result: {exc}")
            finally:
                for worker in workers:
                    worker.join(timeout=15)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)

            self.assertTrue(all(worker.exitcode == 0 for worker in workers))
            self.assertEqual(sorted(status for status, _detail in received), ["predicted", "rejected"])
            rejected = next(detail for status, detail in received if status == "rejected")
            self.assertIn("unrevealed prediction", rejected)

            resumed = self.make_protocol(directory)
            self.assertEqual(len(resumed._records), 1)
            self.assertEqual(resumed._records[0]["record_type"], "event_prediction")
            self.assertEqual(resumed._records[0]["event_ordinal"], 0)

    def test_hash_chain_chronology_and_boot_id_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            clock = StepClock()
            protocol = self.make_protocol(directory, clock)
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            protocol.predict_event("tool", tool())
            record = json.loads(protocol.journal_path.read_text(encoding="utf-8"))
            record["event_ordinal"] = 4
            protocol.journal_path.write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AdaptiveProtocolError, "record hash|ordinals"):
                self.make_protocol(directory, clock)

    def test_e2e_reveal_requires_frozen_manifest_and_manifest_is_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            protocol = self.make_protocol(directory, StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            row = tool()
            protocol.predict_event("tool", row)
            protocol.reveal_event_label("tool", row["event_id"], {"observed_ms": 10})
            with self.assertRaisesRegex(AdaptiveProtocolError, "manifest"):
                protocol.reveal_trajectory_label(100)
            digest = protocol.freeze_prediction_manifest()
            self.assertEqual(digest, hashlib.sha256(protocol.manifest_path.read_bytes()).hexdigest())
            protocol.reveal_trajectory_label(100)
            with self.assertRaisesRegex(AdaptiveProtocolError, "already been revealed"):
                protocol.predict_event("model", model())
            protocol.manifest_path.write_bytes(protocol.manifest_path.read_bytes() + b" ")
            with self.assertRaisesRegex(AdaptiveProtocolError, "tampered"):
                protocol.score()

    def test_score_and_legacy_label_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            protocol = self.make_protocol(Path(temporary), StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            tool_row = tool()
            model_row = model()
            protocol.predict_event("tool", tool_row)
            protocol.reveal_event_label("tool", tool_row["event_id"], {"observed_ms": 10})
            protocol.predict_event("model", model_row)
            protocol.reveal_event_label("model", model_row["request_id"], {"observed_ms": 20})
            protocol.freeze_prediction_manifest()
            protocol.reveal_trajectory_label(100)
            report = protocol.score()
            self.assertTrue(report["passed"])
            labels = protocol.build_labels_artifact(
                evaluator_bindings={
                    "prepare_receipt_sha256": "c" * 64,
                    "calibration_sha256": protocol.calibration_model.sha256,
                    "holdout_features_sha256": "d" * 64,
                    "capture_receipt_sha256": "e" * 64,
                    "captured_features_sha256": "f" * 64,
                    "runtime_manifest_sha256": protocol.runtime_manifest_sha256,
                    "hardware_profile_sha256": protocol.hardware_profile_sha256,
                }
            )
            self.assertEqual(labels["schema_version"], ADAPTIVE_LABELS_SCHEMA)
            self.assertEqual(len(labels["tool_events"]), 1)
            self.assertEqual(len(labels["model_events"]), 1)
            self.assertEqual(len(labels["trajectories"]), 1)

    def test_unavailable_event_is_reported_but_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as temporary:
            protocol = self.make_protocol(Path(temporary), StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            row = tool()
            protocol.predict_event("tool", row)
            protocol.reveal_event_label(
                "tool", row["event_id"],
                {"status": "unavailable", "unavailable_reason": "trace missing"},
            )
            protocol.freeze_prediction_manifest()
            protocol.reveal_trajectory_label(100)
            report = protocol.score()
            self.assertFalse(report["passed"])
            self.assertEqual(report["unavailable_event_count"], 1)
            self.assertEqual(report["event_scores"][0]["status"], "unavailable")

    def test_cli_round_trip_preserves_prediction_before_reveal_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model_input = directory / "model-input.json"
            model_input.write_text(json.dumps(model_blocks()), encoding="utf-8")
            model_path = directory / "calibration-model.json"
            values = hashes("a")

            def run(*arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *arguments],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                journal = directory / "holdout" / "adaptive_events.jsonl"
                diagnostic = journal.read_text(encoding="utf-8") if journal.is_file() else ""
                self.assertEqual(
                    result.returncode,
                    expected,
                    result.stdout + result.stderr + "\nJOURNAL:\n" + diagnostic,
                )
                return result

            hash_args = [
                item
                for name, value in values.items()
                for item in ("--" + name.replace("_", "-"), value)
            ]
            run(
                "freeze-model",
                "--model-json", str(model_input),
                "--output", str(model_path),
                "--calibration-run-id", "cal-0",
                *hash_args,
            )
            protocol_root = directory / "holdout"
            common = [
                "--root", str(protocol_root),
                "--calibration-model", str(model_path),
                *hash_args,
            ]
            run("arm", *common, "--run-id", "holdout-1", "--predicted-e2e-ms", "100")

            label_path = directory / "label.json"
            label_path.write_text(json.dumps({"observed_ms": 10}), encoding="utf-8")
            run(
                "reveal-event", *common,
                "--kind", "tool", "--identifier", tool()["event_id"],
                "--label-json", str(label_path),
                expected=2,
            )

            feature_path = directory / "tool-feature.json"
            feature_path.write_text(json.dumps(tool()), encoding="utf-8")
            run("predict-event", *common, "--kind", "tool", "--features-json", str(feature_path))
            run(
                "reveal-event", *common,
                "--kind", "tool", "--identifier", tool()["event_id"],
                "--label-json", str(label_path),
            )
            run("freeze-manifest", *common)
            run("reveal-trajectory", *common, "--observed-e2e-ms", "100")
            score_path = directory / "score.json"
            score = run("score", *common, "--output", str(score_path))
            self.assertTrue(json.loads(score.stdout)["passed"])
            self.assertTrue(score_path.is_file())
            self.assertTrue(Path(str(score_path).replace(".json", ".sha256")).is_file())

    def test_cli_freezes_recomputable_pretrajectory_e2e_prediction(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            model_path = directory / "calibration-model.json"
            hardware_path = directory / "hardware.json"
            hardware_payload = (json.dumps(HARDWARE, sort_keys=True, separators=(",", ":")) + "\n").encode()
            hardware_path.write_bytes(hardware_payload)
            hardware_digest = hashlib.sha256(hardware_payload).hexdigest()
            hardware_path.with_suffix(".sha256").write_text(
                f"{hardware_digest}  {hardware_path.name}\n",
                encoding="utf-8",
            )
            values = {
                "split_manifest_sha256": "a" * 64,
                "runtime_manifest_sha256": "b" * 64,
                "hardware_profile_sha256": hardware_digest,
                "model_revision_sha256": "d" * 64,
            }
            freeze_calibration_model(
                model_blocks(),
                model_path,
                calibration_run_ids=["cal-0"],
                **values,
            )
            features_path = directory / "e2e-features.json"
            features_path.write_text(
                json.dumps(
                    {"tool_events": [tool()], "model_events": [model()]},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            output = directory / "e2e-prediction.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "freeze-e2e-prediction",
                    "--calibration-model", str(model_path),
                    "--hardware-profile", str(hardware_path),
                    "--run-id", "holdout-1",
                    "--features-json", str(features_path),
                    "--output", str(output),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            artifact = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(artifact["predicted_ms"], 100.0)
            self.assertEqual(
                output.with_suffix(".sha256").read_text(encoding="utf-8"),
                f"{hashlib.sha256(output.read_bytes()).hexdigest()}  {output.name}\n",
            )

    def test_caller_cannot_inject_prior_or_current_event_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            protocol = self.make_protocol(Path(temporary), StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            leaked = dict(model())
            leaked["prior_label_sha256s"] = ["a" * 64]
            with self.assertRaisesRegex(AdaptiveProtocolError, "journal injects"):
                protocol.predict_event("model", leaked)
            leaked = dict(model())
            leaked["output_tokens"] = 99
            with self.assertRaisesRegex(AdaptiveProtocolError, "target-derived"):
                protocol.predict_event("model", leaked)
            leaked = dict(tool())
            leaked["official_resolved"] = True
            with self.assertRaisesRegex(AdaptiveProtocolError, "target-derived"):
                protocol.predict_event("tool", leaked)

    def test_prediction_cites_only_previously_revealed_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            protocol = self.make_protocol(directory, StepClock())
            protocol.arm_trajectory("holdout-1", predicted_e2e_ms=100)
            first = protocol.predict_event("tool", tool())
            self.assertEqual(first["prior_label_sha256s"], [])
            revealed = protocol.reveal_event_label("tool", tool()["event_id"], {"observed_ms": 10})
            second = protocol.predict_event("model", model())
            self.assertEqual(second["prior_label_sha256s"], [revealed["record_sha256"]])
            journal = directory / "adaptive_events.jsonl"
            lines = journal.read_text(encoding="utf-8").splitlines()
            forged = json.loads(lines[-1])
            forged["prior_label_sha256s"] = [revealed["record_sha256"], "f" * 64]
            del forged["record_sha256"]
            from scripts.assignment.adaptive_event_protocol import _record_digest

            forged["record_sha256"] = _record_digest(forged)
            lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            journal.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AdaptiveProtocolError, "not revealed before"):
                AdaptiveEventProtocol(
                    directory,
                    protocol.calibration_model,
                    clock=StepClock(),
                    **hashes("a"),
                )


if __name__ == "__main__":
    unittest.main()
