#!/usr/bin/env python3
"""Bind the adaptive event protocol to one reviewed live holdout runtime.

This module is the shared boundary used by the request proxy and the pinned
SWE-agent hook.  It derives only pre-execution features, never persists prompt
or command content, and delegates every durable prediction/label transition to
``AdaptiveEventProtocol``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol

from scripts.assignment.adaptive_event_protocol import (
    AdaptiveEventProtocol,
    AdaptiveProtocolError,
    FrozenCalibrationModel,
    verify_trajectory_prediction,
)
from agentic_sim.assignment.event_simulator import HardwareProfile
from agentic_sim.assignment.tool_features import extract_tool_features


RUNTIME_CONFIG_SCHEMA = "assignment.adaptive-runtime-config.v1"
HEX = frozenset("0123456789abcdef")


class AdaptiveRuntimeError(AdaptiveProtocolError):
    """A live adaptive runtime input is missing, mutable, or unsafe."""


class TokenCounter(Protocol):
    def count_chat(self, messages: list[dict[str, Any]]) -> int: ...


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - HEX:
        raise AdaptiveRuntimeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AdaptiveRuntimeError(f"{label} must be a non-empty string")
    return value


def _regular_absolute_path(value: Any, label: str, *, directory: bool = False) -> Path:
    raw = Path(_require_text(value, label)).expanduser()
    if not raw.is_absolute():
        raise AdaptiveRuntimeError(f"{label} must be absolute")
    path = raw.resolve()
    if raw.is_symlink() or (not path.is_dir() if directory else not path.is_file()):
        kind = "directory" if directory else "regular file"
        raise AdaptiveRuntimeError(f"{label} must be an existing {kind}: {raw}")
    return path


def _verify_hashed_file(path: Path, label: str) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file():
        raise AdaptiveRuntimeError(f"{label} must be a regular file: {path}")
    try:
        payload = path.read_bytes()
        candidates = list(dict.fromkeys((Path(str(path) + ".sha256"), path.with_suffix(".sha256"))))
        existing = [candidate for candidate in candidates if candidate.exists()]
        if len(existing) != 1:
            raise AdaptiveRuntimeError(
                f"{label} requires exactly one recognized SHA-256 sidecar"
            )
        sidecar = existing[0]
        claim = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise AdaptiveRuntimeError(f"cannot read {label} or sidecar: {exc}") from exc
    digest = _sha_bytes(payload)
    if sidecar.is_symlink() or not sidecar.is_file() or claim != f"{digest}  {path.name}\n":
        raise AdaptiveRuntimeError(f"{label} or SHA-256 sidecar was tampered with")
    return payload, digest


class PinnedTokenizerCounter:
    """Count the exact chat-template token sequence from a verified snapshot."""

    def __init__(self, snapshot: Path, revision: str, file_hashes: Mapping[str, str]):
        if not file_hashes or set(file_hashes) < {"tokenizer.json", "tokenizer_config.json"}:
            raise AdaptiveRuntimeError(
                "tokenizer.required_files_sha256 must include tokenizer.json and tokenizer_config.json"
            )
        for name, expected in file_hashes.items():
            if not isinstance(name, str) or not name or Path(name).name != name:
                raise AdaptiveRuntimeError("tokenizer file names must be plain relative basenames")
            expected = _require_sha(expected, f"tokenizer.required_files_sha256.{name}")
            candidate = snapshot / name
            if candidate.is_symlink() or not candidate.is_file():
                raise AdaptiveRuntimeError(f"pinned tokenizer file is unavailable: {candidate}")
            if _sha_bytes(candidate.read_bytes()) != expected:
                raise AdaptiveRuntimeError(f"pinned tokenizer file hash mismatch: {name}")
        try:
            from transformers import AutoTokenizer  # type: ignore

            self._tokenizer = AutoTokenizer.from_pretrained(
                str(snapshot),
                revision=revision,
                local_files_only=True,
                trust_remote_code=False,
            )
        except Exception as exc:
            raise AdaptiveRuntimeError(f"cannot load pinned local tokenizer: {type(exc).__name__}") from exc

    def count_chat(self, messages: list[dict[str, Any]]) -> int:
        try:
            encoded = self._tokenizer.apply_chat_template(
                messages,
                tokenize=True,
                add_generation_prompt=True,
            )
        except Exception as exc:
            raise AdaptiveRuntimeError(f"cannot tokenize reviewed chat request: {type(exc).__name__}") from exc
        if hasattr(encoded, "ids"):
            encoded = encoded.ids
        if not isinstance(encoded, list) or not all(isinstance(item, int) for item in encoded):
            raise AdaptiveRuntimeError("pinned tokenizer returned an unsupported token sequence")
        return len(encoded)


def _load_json_object(payload: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise AdaptiveRuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AdaptiveRuntimeError(f"{label} must be a JSON object")
    return value


@dataclass
class AdaptiveRuntime:
    config_path: Path
    config_sha256: str
    run_id: str
    hardware: HardwareProfile
    protocol: AdaptiveEventProtocol
    token_counter: TokenCounter

    @classmethod
    def load(
        cls,
        config_path: Path,
        *,
        token_counter: TokenCounter | None = None,
    ) -> "AdaptiveRuntime":
        config_path = Path(config_path).expanduser().resolve()
        payload, config_sha = _verify_hashed_file(config_path, "adaptive runtime config")
        value = _load_json_object(payload, "adaptive runtime config")
        required = {
            "schema_version",
            "run_id",
            "protocol_root",
            "calibration_model_path",
            "split_manifest_path",
            "runtime_manifest_path",
            "hardware_profile_path",
            "bindings",
            "tokenizer",
            "pre_trajectory_e2e",
        }
        if set(value) != required or value.get("schema_version") != RUNTIME_CONFIG_SCHEMA:
            raise AdaptiveRuntimeError("adaptive runtime config has missing, unknown, or unsupported fields")
        run_id = _require_text(value["run_id"], "run_id")
        protocol_root_raw = Path(_require_text(value["protocol_root"], "protocol_root")).expanduser()
        if not protocol_root_raw.is_absolute() or protocol_root_raw.is_symlink():
            raise AdaptiveRuntimeError("protocol_root must be an absolute non-symlink path")
        protocol_root = protocol_root_raw.resolve()

        bindings = value["bindings"]
        binding_fields = {
            "split_manifest_sha256",
            "runtime_manifest_sha256",
            "hardware_profile_sha256",
            "model_revision_sha256",
        }
        if not isinstance(bindings, dict) or set(bindings) != binding_fields:
            raise AdaptiveRuntimeError("adaptive runtime bindings are incomplete")
        bindings = {name: _require_sha(bindings[name], f"bindings.{name}") for name in binding_fields}

        split_path = _regular_absolute_path(value["split_manifest_path"], "split_manifest_path")
        runtime_path = _regular_absolute_path(value["runtime_manifest_path"], "runtime_manifest_path")
        hardware_path = _regular_absolute_path(value["hardware_profile_path"], "hardware_profile_path")
        split_payload, split_sha = _verify_hashed_file(split_path, "split manifest")
        runtime_payload, runtime_sha = _verify_hashed_file(runtime_path, "runtime manifest")
        hardware_payload, hardware_sha = _verify_hashed_file(hardware_path, "hardware profile")
        for actual, field in (
            (split_sha, "split_manifest_sha256"),
            (runtime_sha, "runtime_manifest_sha256"),
            (hardware_sha, "hardware_profile_sha256"),
        ):
            if actual != bindings[field]:
                raise AdaptiveRuntimeError(f"{field} does not match its immutable file")
        split = _load_json_object(split_payload, "split manifest")
        if (
            set(split) != {"schema_version", "calibration_run_ids", "holdout_run_ids"}
            or split.get("schema_version") != "assignment.event-split-manifest.v1"
            or not isinstance(split.get("calibration_run_ids"), list)
            or not isinstance(split.get("holdout_run_ids"), list)
            or not split["calibration_run_ids"]
            or not split["holdout_run_ids"]
            or len(set(split["calibration_run_ids"])) != len(split["calibration_run_ids"])
            or len(set(split["holdout_run_ids"])) != len(split["holdout_run_ids"])
            or run_id not in split["holdout_run_ids"]
            or run_id in split["calibration_run_ids"]
        ):
            raise AdaptiveRuntimeError("split manifest schema or IDs are invalid")
        runtime_manifest = _load_json_object(runtime_payload, "runtime manifest")
        if (
            runtime_manifest.get("schema_version") != "assignment-runtime-manifest.v1"
            or not isinstance(runtime_manifest.get("model"), Mapping)
            or not isinstance(runtime_manifest["model"].get("revision"), str)
            or not runtime_manifest["model"]["revision"]
            or _sha_bytes(runtime_manifest["model"]["revision"].encode("utf-8"))
            != bindings["model_revision_sha256"]
        ):
            raise AdaptiveRuntimeError("runtime manifest model revision does not match its binding")
        try:
            hardware = HardwareProfile.from_mapping(_load_json_object(hardware_payload, "hardware profile"))
        except Exception as exc:
            raise AdaptiveRuntimeError(str(exc)) from exc

        tokenizer = value["tokenizer"]
        tokenizer_fields = {"snapshot_path", "revision", "required_files_sha256"}
        if not isinstance(tokenizer, dict) or set(tokenizer) != tokenizer_fields:
            raise AdaptiveRuntimeError("tokenizer binding is incomplete")
        revision = _require_text(tokenizer["revision"], "tokenizer.revision")
        if _sha_bytes(revision.encode("utf-8")) != bindings["model_revision_sha256"]:
            raise AdaptiveRuntimeError("tokenizer/model revision does not match model_revision_sha256")
        snapshot = _regular_absolute_path(tokenizer["snapshot_path"], "tokenizer.snapshot_path", directory=True)
        hashes = tokenizer["required_files_sha256"]
        if not isinstance(hashes, dict):
            raise AdaptiveRuntimeError("tokenizer.required_files_sha256 must be an object")
        counter = token_counter or PinnedTokenizerCounter(snapshot, revision, hashes)

        calibration_path = _regular_absolute_path(value["calibration_model_path"], "calibration_model_path")
        model = FrozenCalibrationModel.load(calibration_path)
        protocol = AdaptiveEventProtocol(protocol_root, model, **bindings)
        pre = value["pre_trajectory_e2e"]
        if not isinstance(pre, dict) or set(pre) != {
            "predicted_ms",
            "prediction_artifact_path",
            "prediction_artifact_sha256",
        }:
            raise AdaptiveRuntimeError(
                "live adaptive pre_trajectory_e2e must contain a numeric prediction and its artifact binding"
            )
        predicted = pre.get("predicted_ms")
        if isinstance(predicted, bool) or not isinstance(predicted, (int, float)) or predicted <= 0:
            raise AdaptiveRuntimeError("live adaptive predicted_ms must be positive")
        prediction_path = _regular_absolute_path(
            pre["prediction_artifact_path"],
            "pre_trajectory_e2e.prediction_artifact_path",
        )
        prediction_sha = _require_sha(
            pre["prediction_artifact_sha256"],
            "pre_trajectory_e2e.prediction_artifact_sha256",
        )
        try:
            prediction, actual_prediction_sha = verify_trajectory_prediction(
                prediction_path,
                model,
                run_id=run_id,
                hardware=hardware.to_mapping(),
            )
        except AdaptiveProtocolError as exc:
            raise AdaptiveRuntimeError(str(exc)) from exc
        if actual_prediction_sha != prediction_sha:
            raise AdaptiveRuntimeError("pre-trajectory E2E prediction artifact hash does not match its config binding")
        if float(prediction["predicted_ms"]) != float(predicted):
            raise AdaptiveRuntimeError("pre-trajectory E2E prediction does not match its config value")
        protocol.arm_trajectory(
            run_id,
            predicted_e2e_ms=prediction["predicted_ms"],
            e2e_prediction_artifact_path=str(prediction_path),
            e2e_prediction_artifact_sha256=actual_prediction_sha,
            calibration_model_path=str(calibration_path),
            hardware_profile_path=str(hardware_path),
        )
        return cls(config_path, config_sha, run_id, hardware, protocol, counter)

    def predict_model_request(self, request_id: str, body: bytes) -> dict[str, Any]:
        payload = _load_json_object(body, "model request")
        if set(payload) & {"usage", "response", "duration_ms", "wall_ms", "output_tokens"}:
            raise AdaptiveRuntimeError("model request contains target-derived fields")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or not all(isinstance(row, dict) for row in messages):
            raise AdaptiveRuntimeError("model request messages must be a non-empty object list")
        maximum = payload.get("max_completion_tokens", payload.get("max_tokens"))
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise AdaptiveRuntimeError("model request requires a positive declared output budget")
        token_count = self.token_counter.count_chat(messages)
        features = {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": _require_text(request_id, "request_id"),
            "run_id": self.run_id,
            "split": "holdout",
            "input_tokens": token_count,
            "context_tokens": token_count,
            "max_output_tokens": maximum,
            "hardware": self.hardware.to_mapping(),
        }
        return self.protocol.predict_event("model", features)

    def reveal_model_request(
        self,
        request_id: str,
        *,
        observed_ms: float | None,
        output_tokens: int | None = None,
        response_sha256: str | None = None,
        unavailable_reason: str | None = None,
    ) -> dict[str, Any]:
        if unavailable_reason is not None:
            label: dict[str, Any] = {"status": "unavailable", "unavailable_reason": unavailable_reason}
        else:
            label = {"status": "completed", "observed_ms": observed_ms}
            if output_tokens is not None:
                label["output_tokens"] = output_tokens
            if response_sha256 is not None:
                label["response_sha256"] = response_sha256
        return self.protocol.reveal_event_label("model", request_id, label)

    def predict_tool_action(self, event_id: str, action: str) -> dict[str, Any]:
        extracted = extract_tool_features(action)
        features = {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": _require_text(event_id, "event_id"),
            "run_id": self.run_id,
            "split": "holdout",
            "operation_class": extracted.operation_class,
            "declared_command_bytes": extracted.declared_command_bytes,
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": extracted.declared_path_count,
            "tool_name": extracted.tool_name,
            "subcommand": extracted.subcommand,
            "command_prefix": extracted.command_prefix,
            "command_sha256": extracted.command_sha256,
            "has_pipe": extracted.has_pipe,
            "has_glob": extracted.has_glob,
            "extractor_id": extracted.extractor_id,
            "extractor_sha256": extracted.extractor_sha256,
            "hardware": self.hardware.to_mapping(),
        }
        return self.protocol.predict_event("tool", features)

    def reveal_tool_action(
        self,
        event_id: str,
        *,
        observed_ms: float | None,
        unavailable_reason: str | None = None,
    ) -> dict[str, Any]:
        label = (
            {"status": "unavailable", "unavailable_reason": unavailable_reason}
            if unavailable_reason is not None
            else {"status": "completed", "observed_ms": observed_ms}
        )
        return self.protocol.reveal_event_label("tool", event_id, label)
