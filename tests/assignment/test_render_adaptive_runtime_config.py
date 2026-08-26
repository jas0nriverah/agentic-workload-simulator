from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.adaptive_event_protocol import (
    FrozenCalibrationModel,
    freeze_calibration_model,
    freeze_trajectory_prediction,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/render_adaptive_runtime_config.py"
HARDWARE = {
    "schema_version": "assignment.hardware-profile.v1",
    "hardware_id": "fixture-h100",
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


def _payload(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_hashed(path: Path, value: dict, *, appended: bool) -> str:
    payload = _payload(value)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    sidecar = Path(str(path) + ".sha256") if appended else path.with_suffix(".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


class RenderAdaptiveRuntimeConfigTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, Path | float]:
        repo = root / "repo"
        repo.mkdir()
        output_dir = root / "case-output"
        output_dir.mkdir()
        case = output_dir / "case.json"
        resume_key = "assignment-case-v1:holdout"
        case.write_text(json.dumps({"schema_version": "assignment-steps-1-3-plan.v1", "resume_key": resume_key}) + "\n")
        run_id = "assignment-" + hashlib.sha256(resume_key.encode()).hexdigest()[:16]

        runtime = root / "runtime.json"
        revision = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
        runtime_sha = _write_hashed(runtime, {
            "schema_version": "assignment-runtime-manifest.v1",
            "repository_root": str(repo),
            "model": {"revision": revision},
        }, appended=True)
        split = root / "split.json"
        split_sha = _write_hashed(split, {
            "schema_version": "assignment.event-split-manifest.v1",
            "calibration_run_ids": ["cal-1"],
            "holdout_run_ids": [run_id],
        }, appended=False)
        hardware = root / "hardware.json"
        hardware_sha = _write_hashed(hardware, {
            **HARDWARE,
        }, appended=False)
        model = root / "model.json"
        revision_sha = hashlib.sha256(revision.encode()).hexdigest()
        freeze_calibration_model(
            {
                "tool_event": {"coefficients": [10.0] + [0.0] * 13},
                "model_event": {"coefficients": [20.0] + [0.0] * 7},
                "trajectory": {"coefficients": [100.0] + [0.0] * 4},
            },
            model,
            calibration_run_ids=["cal-1"],
            split_manifest_sha256=split_sha,
            runtime_manifest_sha256=runtime_sha,
            hardware_profile_sha256=hardware_sha,
            model_revision_sha256=revision_sha,
        )
        tokenizer = root / "tokenizer"
        tokenizer.mkdir()
        (tokenizer / "tokenizer.json").write_text("{}\n")
        (tokenizer / "tokenizer_config.json").write_text("{}\n")
        frozen_model = FrozenCalibrationModel.load(model)
        e2e_prediction = root / "e2e-prediction.json"
        forecast_hardware = dict(HARDWARE)
        forecast_tool = {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": "forecast-tool-1",
            "run_id": run_id,
            "split": "holdout",
            "operation_class": "read",
            "declared_command_bytes": 128,
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": 1,
            "hardware": forecast_hardware,
        }
        forecast_model = {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": "forecast-model-1",
            "run_id": run_id,
            "split": "holdout",
            "input_tokens": 128,
            "context_tokens": 128,
            "max_output_tokens": 128,
            "hardware": forecast_hardware,
        }
        freeze_trajectory_prediction(
            frozen_model,
            run_id=run_id,
            hardware=forecast_hardware,
            tool_events=[forecast_tool],
            model_events=[forecast_model],
            output_path=e2e_prediction,
        )
        return {
            "case": case,
            "runtime": runtime,
            "split": split,
            "hardware": hardware,
            "model": model,
            "tokenizer": tokenizer,
            "protocol": output_dir / "adaptive-protocol",
            "e2e_prediction": e2e_prediction,
            "output": output_dir / "adaptive-runtime.json",
        }

    def _run(self, fixture: dict[str, Path | float], *extra: str):
        command = [sys.executable, str(SCRIPT)]
        for option, key in (
            ("--case-spec", "case"),
            ("--runtime-manifest", "runtime"),
            ("--split-manifest", "split"),
            ("--hardware-profile", "hardware"),
            ("--calibration-model", "model"),
            ("--tokenizer-snapshot", "tokenizer"),
            ("--protocol-root", "protocol"),
            ("--e2e-prediction", "e2e_prediction"),
            ("--output", "output"),
        ):
            command.extend((option, str(fixture[key])))
        command.extend(extra)
        return subprocess.run(command, capture_output=True, text=True, cwd=ROOT)

    def test_validation_only_is_deterministic_and_accepts_both_canonical_sidecar_styles(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            first = self._run(fixture, "--validation-only")
            second = self._run(fixture, "--validation-only")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            value = json.loads(first.stdout)
            self.assertEqual(value["pre_trajectory_e2e"]["predicted_ms"], 100.0)
            self.assertEqual(
                value["pre_trajectory_e2e"]["prediction_artifact_sha256"],
                hashlib.sha256(Path(fixture["e2e_prediction"]).read_bytes()).hexdigest(),
            )
            self.assertFalse(Path(fixture["output"]).exists())

    def test_writes_mode_0600_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            result = self._run(fixture)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Path(fixture["output"])
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(str(output) + ".sha256").stat().st_mode), 0o600)
            self.assertNotEqual(self._run(fixture).returncode, 0)

    def test_rejects_undeclared_holdout_and_ambiguous_sidecars(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            split = Path(fixture["split"])
            Path(str(split) + ".sha256").write_text(split.with_suffix(".sha256").read_text())
            result = self._run(fixture, "--validation-only")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exactly one", result.stderr)

    def test_rejects_prediction_artifact_recalculation_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self._fixture(Path(temporary))
            prediction = Path(fixture["e2e_prediction"])
            value = json.loads(prediction.read_text(encoding="utf-8"))
            value["predicted_ms"] = 101.0
            prediction.write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(prediction.read_bytes()).hexdigest()
            prediction.with_suffix(".sha256").write_text(
                f"{digest}  {prediction.name}\n",
                encoding="utf-8",
            )
            result = self._run(fixture, "--validation-only")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not reproducible", result.stderr)


if __name__ == "__main__":
    unittest.main()
