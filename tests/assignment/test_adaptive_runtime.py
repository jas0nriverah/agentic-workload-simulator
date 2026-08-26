"""Focused tests for the live adaptive runtime boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.assignment.adaptive_event_protocol import (
    FrozenCalibrationModel,
    freeze_calibration_model,
    freeze_trajectory_prediction,
)
from scripts.assignment.adaptive_runtime import AdaptiveRuntime, AdaptiveRuntimeError


HARDWARE = {
    "schema_version": "assignment.hardware-profile.v1",
    "hardware_id": "h100-adaptive-test",
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


class FakeTokenCounter:
    """Deterministic stand-in that proves tests do not import transformers."""

    def __init__(self, count: int = 400) -> None:
        self.count = count
        self.messages: list[dict[str, object]] | None = None

    def count_chat(self, messages: list[dict[str, object]]) -> int:
        self.messages = messages
        return self.count


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_hashed_json(path: Path, value: dict[str, object]) -> str:
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(payload)
    path.with_name(path.name + ".sha256").write_text(
        f"{_digest(payload)}  {path.name}\n", encoding="utf-8"
    )
    return _digest(payload)


def _model_blocks() -> dict[str, dict[str, object]]:
    return {
        "tool_event": {"alpha": 0.1, "coefficients": [10.0] + [0.0] * 13},
        "model_event": {"alpha": 0.1, "coefficients": [20.0] + [0.0] * 7},
        "trajectory": {"alpha": 0.1, "coefficients": [100.0] + [0.0] * 4},
    }


class AdaptiveRuntimeTests(unittest.TestCase):
    def _fixture(self, directory: Path) -> tuple[AdaptiveRuntime, FakeTokenCounter, Path]:
        protocol_root = directory / "adaptive-root"
        protocol_root.mkdir()

        split_path = directory / "split-manifest.json"
        runtime_path = directory / "runtime-manifest.json"
        hardware_path = directory / "hardware-profile.json"
        split_sha = _write_hashed_json(
            split_path,
            {
                "schema_version": "assignment.event-split-manifest.v1",
                "calibration_run_ids": ["calibration-0"],
                "holdout_run_ids": ["holdout-trajectory-1"],
            },
        )
        runtime_sha = _write_hashed_json(
            runtime_path,
            {
                "schema_version": "assignment-runtime-manifest.v1",
                "model": {"revision": "tokenizer-revision-test"},
            },
        )
        hardware_sha = _write_hashed_json(hardware_path, HARDWARE)

        model_path = directory / "calibration-model.json"
        revision = "tokenizer-revision-test"
        revision_sha = _digest(revision.encode())
        freeze_calibration_model(
            _model_blocks(),
            model_path,
            calibration_run_ids=["calibration-0"],
            split_manifest_sha256=split_sha,
            runtime_manifest_sha256=runtime_sha,
            hardware_profile_sha256=hardware_sha,
            model_revision_sha256=revision_sha,
        )

        tokenizer_snapshot = directory / "tokenizer-snapshot"
        tokenizer_snapshot.mkdir()
        tokenizer_hashes: dict[str, str] = {}
        for name, text in {
            "tokenizer.json": "{}\n",
            "tokenizer_config.json": "{}\n",
        }.items():
            candidate = tokenizer_snapshot / name
            candidate.write_text(text, encoding="utf-8")
            tokenizer_hashes[name] = _digest(candidate.read_bytes())

        e2e_prediction_path = directory / "e2e-prediction.json"
        freeze_trajectory_prediction(
            FrozenCalibrationModel.load(model_path),
            run_id="holdout-trajectory-1",
            hardware=HARDWARE,
            tool_events=[{
                "schema_version": "assignment.tool-event-input.v1",
                "event_id": "forecast-tool-1",
                "run_id": "holdout-trajectory-1",
                "split": "holdout",
                "operation_class": "read",
                "declared_command_bytes": 128,
                "declared_read_bytes": 0,
                "declared_write_bytes": 0,
                "declared_path_count": 1,
                "hardware": HARDWARE,
            }],
            model_events=[{
                "schema_version": "assignment.model-event-input.v1",
                "request_id": "forecast-model-1",
                "run_id": "holdout-trajectory-1",
                "split": "holdout",
                "input_tokens": 128,
                "context_tokens": 128,
                "max_output_tokens": 128,
                "hardware": HARDWARE,
            }],
            output_path=e2e_prediction_path,
        )
        e2e_prediction_sha = _digest(e2e_prediction_path.read_bytes())
        config_path = directory / "adaptive-runtime.json"
        _write_hashed_json(
            config_path,
            {
                "schema_version": "assignment.adaptive-runtime-config.v1",
                "run_id": "holdout-trajectory-1",
                "protocol_root": str(protocol_root),
                "calibration_model_path": str(model_path),
                "split_manifest_path": str(split_path),
                "runtime_manifest_path": str(runtime_path),
                "hardware_profile_path": str(hardware_path),
                "bindings": {
                    "split_manifest_sha256": split_sha,
                    "runtime_manifest_sha256": runtime_sha,
                    "hardware_profile_sha256": hardware_sha,
                    "model_revision_sha256": revision_sha,
                },
                "tokenizer": {
                    "snapshot_path": str(tokenizer_snapshot),
                    "revision": revision,
                    "required_files_sha256": tokenizer_hashes,
                },
                "pre_trajectory_e2e": {
                    "predicted_ms": 100.0,
                    "prediction_artifact_path": str(e2e_prediction_path),
                    "prediction_artifact_sha256": e2e_prediction_sha,
                },
            },
        )
        counter = FakeTokenCounter()
        return AdaptiveRuntime.load(config_path, token_counter=counter), counter, config_path

    def test_model_and_tool_predictions_reveal_and_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime, counter, _config_path = self._fixture(Path(temporary))
            prompt = "PROMPT_SECRET_7f0d2f"
            action = "ACTION_SECRET_9a31c2; rg --files ./private-path"
            request = {
                "messages": [{"role": "user", "content": prompt}],
                "max_completion_tokens": 128,
                "temperature": 0.0,
            }

            model_prediction = runtime.predict_model_request("request-1", json.dumps(request).encode())
            tool_prediction = None
            runtime.reveal_model_request(
                "request-1",
                observed_ms=20.0,
                output_tokens=64,
                response_sha256="a" * 64,
            )
            tool_prediction = runtime.predict_tool_action("tool-1", action)
            runtime.reveal_tool_action("tool-1", observed_ms=10.0)
            runtime.protocol.freeze_prediction_manifest()
            runtime.protocol.reveal_trajectory_label(100.0)
            score = runtime.protocol.score()

            self.assertEqual(counter.messages, request["messages"])
            self.assertEqual(model_prediction["prediction"]["predicted_ms"], 20.0)
            self.assertEqual(tool_prediction["prediction"]["predicted_ms"], 10.0)
            self.assertTrue(score["coverage_complete"])
            self.assertTrue(score["passed"])
            self.assertEqual(score["unavailable_event_count"], 0)

    def test_hash_sidecars_and_bindings_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _runtime, _counter, config_path = self._fixture(directory)
            split_path = directory / "split-manifest.json"
            split_path.write_text('{"schema_version":"split.v1","holdout":["tampered"]}\n', encoding="utf-8")
            with self.assertRaisesRegex(AdaptiveRuntimeError, "SHA-256 sidecar was tampered"):
                AdaptiveRuntime.load(config_path, token_counter=FakeTokenCounter())

            split_sha = _digest(split_path.read_bytes())
            split_path.with_name(split_path.name + ".sha256").write_text(
                f"{split_sha}  {split_path.name}\n", encoding="utf-8"
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["bindings"]["split_manifest_sha256"] = "b" * 64
            _write_hashed_json(config_path, config)
            with self.assertRaisesRegex(AdaptiveRuntimeError, "split_manifest_sha256 does not match"):
                AdaptiveRuntime.load(config_path, token_counter=FakeTokenCounter())

    def test_accepts_canonical_suffix_sidecars_and_rejects_ambiguous_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _runtime, _counter, config_path = self._fixture(directory)
            split_path = directory / "split-manifest.json"
            appended = Path(str(split_path) + ".sha256")
            suffix = split_path.with_suffix(".sha256")
            appended.replace(suffix)
            AdaptiveRuntime.load(config_path, token_counter=FakeTokenCounter())
            appended.write_text(suffix.read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaisesRegex(AdaptiveRuntimeError, "exactly one recognized"):
                AdaptiveRuntime.load(config_path, token_counter=FakeTokenCounter())

    def test_target_derived_request_fields_are_rejected_before_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime, counter, _config_path = self._fixture(Path(temporary))
            base = {
                "messages": [{"role": "user", "content": "do not persist this"}],
                "max_tokens": 64,
            }
            for field in ("usage", "response", "duration_ms", "wall_ms", "output_tokens"):
                request = dict(base)
                request[field] = 1
                with self.subTest(field=field):
                    with self.assertRaisesRegex(AdaptiveRuntimeError, "target-derived"):
                        runtime.predict_model_request(f"bad-{field}", json.dumps(request).encode())
            self.assertIsNone(counter.messages)

    def test_adaptive_journal_contains_no_prompt_or_action_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            runtime, _counter, _config_path = self._fixture(directory)
            prompt = "UNIQUE_PROMPT_PLAINTEXT_SHOULD_NEVER_BE_WRITTEN"
            action = "UNIQUE_ACTION_PLAINTEXT_SHOULD_NEVER_BE_WRITTEN"
            runtime.predict_model_request(
                "request-secret",
                json.dumps(
                    {
                        "messages": [{"role": "user", "content": prompt}],
                        "max_completion_tokens": 32,
                    }
                ).encode(),
            )
            runtime.reveal_model_request("request-secret", observed_ms=20.0)
            runtime.predict_tool_action("tool-secret", action)
            journal = runtime.protocol.journal_path.read_text(encoding="utf-8")
            self.assertNotIn(prompt, journal)
            self.assertNotIn(action, journal)
            self.assertIn("request-secret", journal)
            self.assertIn("tool-secret", journal)


if __name__ == "__main__":
    unittest.main()
