import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.analysis.feature_validation import (
    ValidationError,
    fit,
    load_protocol,
    score,
    seal,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG = REPO_ROOT / "configs" / "h100_final_validation.json"


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _row(root: Path, split: str, case_id: str, repeat: str, wall_ms: float) -> None:
    folder = "calibration" if split == "calibration" else "holdout"
    _write(
        root / folder / case_id / repeat / "row.json",
        {
            "schema_version": "h100-final-row.v1",
            "case_id": case_id,
            "split": split,
            "repeat_id": repeat,
            "status": "completed",
            "wall_ms": wall_ms,
            "artifact_sha256": hashlib.sha256(f"{case_id}/{repeat}".encode()).hexdigest(),
        },
    )


class FeatureValidationTests(unittest.TestCase):
    def test_h100_entrypoint_dry_run_is_gpu_free_and_complete(self):
        script = REPO_ROOT / "scripts" / "cloud" / "run_h100_final_validation.sh"
        result = subprocess.run(
            [str(script), "--dry-run"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("24 calibration, 12 sealed holdouts", result.stdout)
        self.assertIn("no GPU inspection", result.stdout)
        self.assertNotIn("nvidia-smi", result.stdout)

    def test_seal_is_idempotent_and_hashes_disjoint_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = seal(CONFIG, root)
            split_before = (root / "split_manifest.json").read_bytes()
            second = seal(CONFIG, root)
            self.assertEqual(first, second)
            self.assertEqual(split_before, (root / "split_manifest.json").read_bytes())
            split = json.loads(split_before)
            self.assertEqual(len(split["calibration_case_ids"]), 24)
            self.assertEqual(len(split["sealed_holdout_case_ids"]), 12)
            self.assertTrue(set(split["calibration_case_ids"]).isdisjoint(split["sealed_holdout_case_ids"]))

    def test_sealed_split_and_feature_matrix_are_deterministic(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            seal(CONFIG, first)
            seal(CONFIG, second)
            first_split = json.loads((first / "split_manifest.json").read_text())
            second_split = json.loads((second / "split_manifest.json").read_text())
            first_split.pop("sealed_at_utc", None)
            second_split.pop("sealed_at_utc", None)
            self.assertEqual(first_split, second_split)
            self.assertEqual(
                (first / "feature_manifest.json").read_bytes(),
                (second / "feature_manifest.json").read_bytes(),
            )

    def test_fit_freezes_prediction_manifest_without_holdout_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol, _, _ = load_protocol(CONFIG)
            seal(CONFIG, root)
            for case in protocol["calibration_configs"]:
                for repeat_index, repeat in enumerate(("r01", "r02", "r03")):
                    _row(root, "calibration", case["case_id"], repeat, 1000 + case["input_tokens"] + repeat_index)
            result = fit(CONFIG, root)
            prediction_path = Path(result["prediction_manifest"])
            prediction = json.loads(prediction_path.read_text(encoding="utf-8"))
            self.assertEqual(len(prediction["predictions"]), 12)
            serialized = json.dumps(prediction)
            self.assertNotIn("wall_ms", serialized)
            self.assertNotIn("actual_completion_tokens", serialized)
            self.assertTrue((root / "derived" / "prediction_manifest.sha256").is_file())

    def test_score_requires_prediction_receipt_and_reports_holdout_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol, _, _ = load_protocol(CONFIG)
            seal(CONFIG, root)
            for case in protocol["calibration_configs"]:
                for repeat in ("r01", "r02", "r03"):
                    _row(root, "calibration", case["case_id"], repeat, 1000 + case["input_tokens"])
            fit_result = fit(CONFIG, root)
            prediction_path = Path(fit_result["prediction_manifest"])
            with self.assertRaises(ValidationError):
                score(CONFIG, root)
            for case in protocol["sealed_holdouts"]:
                for repeat in ("r01", "r02", "r03"):
                    _row(root, "sealed_holdout", case["case_id"], repeat, 1500 + case["input_tokens"])
            receipt = {
                "schema_version": "h100-holdout-reveal.v1",
                "protocol_sha256": json.loads((root / "split_manifest.json").read_text())["protocol_sha256"],
                "split_manifest_sha256": json.loads((root / "split_manifest.json").read_text())["split_sha256"],
                "prediction_manifest_sha256": hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
                "revealed_at_utc": "2026-08-24T00:00:00Z",
            }
            _write(root / "holdout_reveal_receipt.json", receipt)
            result = score(CONFIG, root)
            metrics = json.loads(Path(result["metrics"]).read_text(encoding="utf-8"))
            self.assertEqual(metrics["coverage_percent"], 100.0)
            self.assertEqual(len(metrics["cases"]), 12)
            self.assertIn("interpolation_wall", metrics)
            self.assertIn("extrapolation_wall", metrics)

    def test_score_rejects_measured_label_smuggled_into_prediction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protocol, protocol_hash, split_hash = load_protocol(CONFIG)
            seal(CONFIG, root)
            path = root / "derived" / "prediction_manifest.json"
            prediction = {
                "schema_version": "h100-feature-predictions.v1",
                "protocol_sha256": protocol_hash,
                "split_manifest_sha256": split_hash,
                "predictions": [
                    {"case_id": case["case_id"], "predicted_seconds": 1.0, "wall_ms": 2.0}
                    for case in protocol["sealed_holdouts"]
                ],
            }
            _write(path, prediction)
            _write(
                root / "holdout_reveal_receipt.json",
                {"prediction_manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            )
            with self.assertRaises(ValidationError):
                score(CONFIG, root)

    def test_runner_defers_reveal_until_after_holdout_collection(self):
        script = (REPO_ROOT / "scripts" / "cloud" / "run_h100_final_validation.sh").read_text()
        loop_end = script.index('done <<< "$CASE_LIST"')
        reveal_block = script.index('if [[ "$PHASE" == holdout ]]; then', loop_end)
        receipt_write = script.index("holdout_reveal_receipt.json", reveal_block)
        self.assertLess(loop_end, reveal_block)
        self.assertLess(reveal_block, receipt_write)
        self.assertIn("prediction_manifest.sha256", script)

    def test_handoff_declares_checkout_and_execution_contract(self):
        handoff = (REPO_ROOT / "H100_FINAL_VM_HANDOFF.md").read_text()
        for required in (
            "parallel-h100-shards",
            "git rev-parse HEAD",
            "feature_validation.py fit",
            "feature_validation.py score",
            "tail -f",
            "--resume",
            "scripts/cloud/h100_case_runner.py",
            "--validate-only",
            "Final report format",
        ):
            self.assertIn(required, handoff)


if __name__ == "__main__":
    unittest.main()
