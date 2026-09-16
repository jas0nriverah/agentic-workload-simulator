"""Runnable offline D9 candidate simulator.

This adapter combines the strongest retained candidates behind one explicit
request contract:

* semantic CPU class hybrid for historical tool wall;
* feature-free historical completed-request proxy;
* repository-aware start-known E2E control; and
* the bounded trace-conditioned relative NNLS E2E candidate from the salvage
  pass, when its declared workload list is supplied.

Native queue/prefill/decode, atomic CPU operations, and lifecycle spans are
returned as explicit unsupported predictions.  They are not mapped to a
historical proxy target.  All predictions carry the complete hardware profile
and a transfer status.  The retained models do not contain a validated
hardware scaling law, so changing the profile never causes silent rescaling.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from hardware_profile import (
    HardwareProfile,
    default_hardware_profile,
    hardware_profile_sha256,
)
from conditional_gpu_model import (
    ConditionalGpuArtifactError,
    load_artifact as load_conditional_gpu_artifact,
    predict as predict_conditional_gpu,
)
from native_model import (
    NativeArtifactError,
    load_artifact as load_native_artifact,
    predict as predict_native,
)


class PredictionContractError(ValueError):
    """A request contains a forbidden label or does not match its target."""


_FORBIDDEN_INPUT_NAMES = frozenset(
    {
        "observed_ms",
        "target_ms",
        "wall_ms",
        "duration_ms",
        "latency_ms",
        "residual_ms",
        "current_output_tokens",
        "actual_output_tokens",
        "completion_tokens",
        "response_tokens",
        "phase_timings",
        "queue_ms",
        "prefill_ms",
        "decode_ms",
        "cache_state",
        "future_actions",
        "outcome",
        "status",
    }
)

TARGETS = (
    "repaired_semantic_action",
    "conditional_repaired_e2e",
    "conditional_native_phase",
    "historical_cpu_tool_wall",
    "historical_gpu_request_proxy_wall",
    "conditional_gpu_request_proxy_wall",
    "historical_start_known_e2e",
    "conditional_trace_e2e",
    "conditional_native_e2e",
    "assignment_cpu_event",
    "assignment_gpu_event",
)
UNSUPPORTED_TARGETS = (
    "native_queue",
    "native_prefill",
    "native_decode",
    "native_gpu_request",
    "atomic_cpu_operation",
    "lifecycle_span",
    "native_e2e",
)
ASSIGNMENT_TARGETS = (
    "assignment_cpu_event",
    "assignment_gpu_event",
)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PredictionContractError(f"cannot read artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PredictionContractError(f"artifact must be a JSON object: {path}")
    return value


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PredictionContractError(f"{name} must be a finite number >= 0")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise PredictionContractError(f"{name} must be a finite number >= 0")
    return result


def _positive_prediction(value: Any) -> float:
    result = _finite_nonnegative(value, "predicted_ms")
    if result <= 0:
        # All retained candidate models are positive-duration models.  Keeping
        # a strictly positive output prevents a malformed artifact from being
        # mistaken for a valid exact-zero native prediction.
        raise PredictionContractError("predicted_ms must be > 0 for retained candidates")
    return result


def _ensure_action(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PredictionContractError("action must be a non-empty string")
    return value.strip()


def _ensure_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PredictionContractError(f"{name} must be an object")
    return value


def _check_keys(
    inputs: Mapping[str, Any], required: set[str], allowed: set[str], target: str
) -> None:
    unknown = sorted(set(inputs) - allowed)
    missing = sorted(required - set(inputs))
    if unknown:
        raise PredictionContractError(
            f"{target} rejects input field(s): {', '.join(unknown)}"
        )
    if missing:
        raise PredictionContractError(
            f"{target} requires input field(s): {', '.join(missing)}"
        )
    forbidden = sorted(set(inputs) & _FORBIDDEN_INPUT_NAMES)
    if forbidden:
        raise PredictionContractError(
            f"prediction request rejects measured or post-event field(s): {', '.join(forbidden)}"
        )


def _exact_keys(inputs: Mapping[str, Any], expected: set[str], target: str) -> None:
    _check_keys(inputs, expected, expected, target)


def _repo_root() -> Path:
    # .../<repo>/docs/d9-salvage-20260910/simulator/d9_simulator.py
    return Path(__file__).resolve().parents[3]


def _default_artifact_root() -> Path:
    return _repo_root() / "docs" / "offline-deliverables-20260909"


def _default_conditional_gpu_model_path() -> Path:
    return _repo_root() / "docs" / "d9-salvage-20260910" / "conditional" / "fit_artifact.json"


def _default_native_fit_artifact_path() -> Path:
    return _repo_root() / "docs" / "d9-salvage-20260910" / "native" / "fit_artifact.json"


def _load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PredictionContractError(f"cannot load Python artifact: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_cpu_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    root = _repo_root()
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    predictor_path = (
        root
        / "docs"
        / "offline-deliverables-20260909"
        / "cpu"
        / "cpu_event_predictor.py"
    )
    predictor = _load_module(predictor_path, "d9_salvage_cpu_event_predictor")
    from agentic_sim.assignment.cpu_event_model import enrich_row
    from agentic_sim.assignment.semantic_cpu_model import semantic_features
    from agentic_sim.assignment.tool_features import extract_tool_features

    return predictor, enrich_row, semantic_features, extract_tool_features, src


def _cpu_feature_row(action: str) -> dict[str, Any]:
    predictor, enrich_row, semantic_features, extract_tool_features, _src = _load_cpu_dependencies()
    extracted = extract_tool_features(action)
    raw = enrich_row({**asdict(extracted), "action": action})
    semantic = semantic_features(action, "")
    return {
        "original_class": raw["operation_class"],
        "features": predictor.canonical_features(raw, semantic),
        "feature_status": "ok",
    }


def _hardware_meta(profile: HardwareProfile, *, uses_hardware: bool = False) -> dict[str, Any]:
    reference = default_hardware_profile()
    reference_hash = hardware_profile_sha256(reference)
    profile_hash = hardware_profile_sha256(profile)
    same = profile_hash == reference_hash
    return {
        "profile": profile.to_mapping(),
        "profile_sha256": profile_hash,
        "reference_profile_id": reference.hardware_id,
        "reference_profile_sha256": reference_hash,
        "profile_provenance": "legacy_historical_script_assumption_not_fresh_host_measurement",
        "transfer_status": "reference_profile" if same else "unvalidated_transfer",
        "scaling_applied": False,
        "sensitivity_status": (
            "model_hardware_parameterized_unvalidated"
            if uses_hardware
            else "candidate_hardware_effect_unmodeled"
        ),
    }


@dataclass
class D9Simulator:
    """Serve retained offline candidates with explicit provenance."""

    artifact_root: Path
    hardware: HardwareProfile
    workload_model_path: Path | None = None
    conditional_gpu_model_path: Path | None = None
    native_fit_artifact_path: Path | None = None

    def __init__(
        self,
        artifact_root: Path | str | None = None,
        hardware: HardwareProfile | Mapping[str, Any] | None = None,
        workload_model_path: Path | str | None = None,
        conditional_gpu_model_path: Path | str | None = None,
        native_fit_artifact_path: Path | str | None = None,
    ) -> None:
        self.artifact_root = (
            Path(artifact_root) if artifact_root is not None else _default_artifact_root()
        )
        self.hardware = (
            hardware
            if isinstance(hardware, HardwareProfile)
            else HardwareProfile.from_mapping(hardware)
            if hardware is not None
            else default_hardware_profile()
        )
        self.workload_model_path = Path(workload_model_path) if workload_model_path else None
        self.conditional_gpu_model_path = (
            Path(conditional_gpu_model_path)
            if conditional_gpu_model_path
            else _default_conditional_gpu_model_path()
        )
        self.native_fit_artifact_path = (
            Path(native_fit_artifact_path)
            if native_fit_artifact_path
            else _default_native_fit_artifact_path()
        )
        if not self.artifact_root.is_dir():
            raise PredictionContractError(f"artifact root is not a directory: {self.artifact_root}")

    def _result(
        self,
        *,
        target: str,
        predicted_ms: float | None,
        status: str,
        contract: str,
        inputs: Mapping[str, Any],
        artifact: Path | None,
        notes: list[str] | None = None,
        uses_hardware: bool = False,
    ) -> dict[str, Any]:
        artifact_name: str | None = None
        if artifact is not None:
            try:
                artifact_name = str(artifact.relative_to(self.artifact_root))
            except ValueError:
                # The optional v3 artifact may live in a temporary review
                # directory or the salvage directory rather than beside the
                # retained proxy bundle.
                artifact_name = str(artifact)
        result: dict[str, Any] = {
            "schema_version": "assignment.d9-salvage-prediction.v1",
            "target": target,
            "status": status,
            "predicted_ms": predicted_ms,
            "contract": contract,
            "inputs": dict(inputs),
            "hardware": _hardware_meta(self.hardware, uses_hardware=uses_hardware),
            "provenance": {
                "artifact": artifact_name,
                "artifact_sha256": (
                    _sha256_path(artifact) if artifact and artifact.is_file() else None
                ),
                "prediction_code": _sha256_path(Path(__file__).resolve()),
            },
            "notes": list(notes or []),
        }
        if predicted_ms is not None:
            result["predicted_ms"] = _positive_prediction(predicted_ms)
        return result

    def _predict_cpu(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        _exact_keys(inputs, {"action"}, "historical_cpu_tool_wall")
        action = _ensure_action(inputs["action"])
        model_path = self.artifact_root / "cpu" / "class_hybrid_model.json"
        payload = _read_json(model_path)
        predictor, _enrich, _semantic, _extract, _src = _load_cpu_dependencies()
        row = _cpu_feature_row(action)
        details = predictor.predict_class_hybrid(payload, row)
        notes = [
            "historical tool-event wall target; this is not an atomic CPU operation target",
            "CPU/GPU/storage profile is recorded but no cross-hardware scaling law is applied",
            "action descriptors are prospective; exact action text is not used as a prediction key",
        ]
        result = self._result(
            target="historical_cpu_tool_wall",
            predicted_ms=float(details["prediction"]),
            status="predicted",
            contract="pre_event_action_descriptors",
            inputs={"action_sha256": hashlib.sha256(action.encode("utf-8")).hexdigest()},
            artifact=model_path,
            notes=notes,
        )
        result["model_details"] = {
            key: details[key]
            for key in ("selected_candidate", "selected_level", "support", "fallback_reason")
            if key in details
        }
        result["feature_contract"] = {
            "available": "action-derived semantic buckets",
            "forbidden": sorted(
                _FORBIDDEN_INPUT_NAMES | {"repository", "case_id", "exact_command"}
            ),
        }
        return result

    def _predict_repaired(self, target: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Integrate retained repaired candidates without inventing hardware scaling."""
        base = _repo_root() / 'docs/d9-salvage-20260910'
        if hardware_profile_sha256(self.hardware) != hardware_profile_sha256(default_hardware_profile()):
            raise PredictionContractError('repaired candidates do not support changed hardware profiles')
        details = {}
        if target == 'repaired_semantic_action':
            path = base / 'semantic_repaired/refined_fit_artifact.json'
            module = _load_module(base / 'semantic_repaired/predict.py', 'integrated_repaired_semantic')
            artifact = _read_json(path)
            try:
                details = module.predict_request(dict(inputs), artifact)
            except ValueError as exc:
                raise PredictionContractError(str(exc)) from exc
            prediction = details['predicted_ms']
            contract = 'pre_action_semantics_reference_cpu'
        elif target == 'conditional_repaired_e2e':
            path = base / 'e2e_composition/fit_artifact.json'
            module = _load_module(base / 'e2e_composition/predict.py', 'integrated_repaired_e2e')
            artifact = _read_json(path)
            try:
                details = module.predict_request(dict(inputs), artifact)
            except (ValueError, KeyError, TypeError) as exc:
                raise PredictionContractError(str(exc)) from exc
            prediction = details['selected_direct_e2e_ms'] if details['selected_model'] == 'direct' else details['composed_e2e_ms']
            contract = 'conditional_aggregate_repaired_e2e_not_event_sum'
        else:
            expected = {'phase','prompt_tokens','completion_tokens','cached_tokens','cache_trace','hardware_domain'}
            if set(inputs) != expected or inputs.get('cache_trace') is not True:
                raise PredictionContractError('native phase requires phase, integer token counts, cache_trace=true and hardware_domain')
            if inputs['phase'] not in {'queue','prefill','decode'}:
                raise PredictionContractError('phase must be queue, prefill or decode')
            for key in ('prompt_tokens','completion_tokens','cached_tokens'):
                if isinstance(inputs[key], bool) or not isinstance(inputs[key], int) or inputs[key] < 0:
                    raise PredictionContractError('token counts must be nonnegative integers')
            if inputs['cached_tokens'] > inputs['prompt_tokens']:
                raise PredictionContractError('cached tokens exceed prompt tokens')
            path = base / 'native_refinement/fit_artifact.json'
            artifact = _read_json(path)
            if inputs['hardware_domain'] != artifact['hardware_domain']:
                raise PredictionContractError('native hardware domain mismatch')
            module = _load_module(base / 'native_refinement/compare_phases.py', 'integrated_native_phases')
            model = artifact['models'][inputs['phase']]
            prediction = module.predict(model, inputs)
            details = {'phase':inputs['phase'], 'candidate':model['candidate'], 'hardware_domain':artifact['hardware_domain']}
            contract = 'conditional_native_phase_supplied_cache_trace'
        result = self._result(target=target, predicted_ms=None, status='predicted', contract=contract,
                              inputs=inputs, artifact=path,
                              notes=['Grouped development candidate; literal D9 not passed.',
                                     'Do not sum nested CPU/native/E2E targets; no hardware scaling applied.'])
        result['predicted_ms'] = _finite_nonnegative(prediction, 'predicted_ms')
        result['model_details'] = details
        result['hardware']['transfer_status'] = 'reference_evidence_only_transfer_unsupported'
        if 'hardware_domain' in details:
            result['hardware']['verified_evidence_domain'] = details['hardware_domain']
        result['provenance']['adapter_code_sha256'] = _sha256_path(Path(module.__file__))
        return result

    def _predict_gpu_proxy(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        _exact_keys(inputs, set(), "historical_gpu_request_proxy_wall")
        model_path = self.artifact_root / "gpu_lifecycle" / "gpu_proxy_chosen_model.json"
        payload = _read_json(model_path)
        if payload.get("candidate") != "global_median":
            raise PredictionContractError("unsupported GPU proxy artifact candidate")
        predicted = payload.get("median_ms")
        notes = [
            "historical completed-request wall proxy; not native queue/prefill/decode",
            "feature-free candidate avoids assuming a prompt count is available",
            "hardware profile is recorded for transfer review; no hardware scaling is applied",
        ]
        return self._result(
            target="historical_gpu_request_proxy_wall",
            predicted_ms=_positive_prediction(predicted),
            status="predicted",
            contract="feature_free_historical_proxy",
            inputs={},
            artifact=model_path,
            notes=notes,
        )

    def _predict_conditional_gpu(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        expected = {"input_tokens", "context_tokens", "output_tokens", "max_output_tokens"}
        _exact_keys(inputs, expected, "conditional_gpu_request_proxy_wall")
        model_path = self.conditional_gpu_model_path
        try:
            payload = load_conditional_gpu_artifact(model_path)
            predicted, model_details = predict_conditional_gpu(payload, inputs)
        except ConditionalGpuArtifactError as exc:
            raise PredictionContractError(str(exc)) from exc
        notes = [
            "trace-conditioned historical request-proxy wall target; not native queue/prefill/decode",
            "input, context, output, and max-output token descriptors are supplied workload fields",
            "realized output tokens are permitted only under this conditional replay contract",
            "hardware transfer is unvalidated and no scaling is applied",
        ]
        result = self._result(
            target="conditional_gpu_request_proxy_wall",
            predicted_ms=predicted,
            status="predicted",
            contract="trace_conditioned_declared_request_workload",
            inputs={name: float(inputs[name]) for name in sorted(expected)},
            artifact=model_path,
            notes=notes,
        )
        result["model_details"] = model_details
        comparison_path = model_path.with_name("comparison.json")
        if comparison_path.is_file():
            comparison = _read_json(comparison_path)
            metrics = comparison.get("fixed_outer_oof_metrics", {})
            candidate = model_details["candidate"]
            if isinstance(metrics, Mapping) and isinstance(metrics.get(candidate), Mapping):
                result["model_details"]["development_metrics"] = dict(metrics[candidate])
        return result

    def _predict_start_e2e(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(inputs, Mapping):
            raise PredictionContractError("historical_start_known_e2e inputs must be an object")
        unknown = sorted(set(inputs) - {"repository"})
        forbidden = sorted(set(inputs) & _FORBIDDEN_INPUT_NAMES)
        if unknown or forbidden:
            fields = sorted(set(unknown) | set(forbidden))
            raise PredictionContractError(
                "historical_start_known_e2e rejects input field(s): " + ", ".join(fields)
            )
        repository = inputs.get("repository", "")
        if repository is not None and not isinstance(repository, str):
            raise PredictionContractError("repository must be text")
        model_path = self.artifact_root / "e2e" / "candidate.json"
        payload = _read_json(model_path)
        by_repo = payload.get("repository_ms")
        if not isinstance(by_repo, Mapping):
            by_repo = {}
        predicted = by_repo.get(repository, payload.get("global_ms"))
        notes = [
            "direct historical accepted-attempt E2E target; it does not explain event composition",
            "repository is assumed known at run start",
            "hardware transfer is unvalidated and no scaling is applied",
        ]
        return self._result(
            target="historical_start_known_e2e",
            predicted_ms=_positive_prediction(predicted),
            status="predicted",
            contract="start_known_repository",
            inputs={"repository": repository or ""},
            artifact=model_path,
            notes=notes,
        )

    def _predict_conditional_e2e(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        expected = {"tool_count", "request_count", "input_tokens", "output_tokens"}
        _exact_keys(inputs, expected, "conditional_trace_e2e")
        values = {name: _finite_nonnegative(inputs[name], name) for name in expected}
        model_path = _repo_root() / "docs" / "d9-salvage-20260910" / "e2e" / "model.json"
        payload = _read_json(model_path)
        coefficients = payload.get("coefficients")
        if not isinstance(coefficients, Mapping):
            raise PredictionContractError("conditional E2E artifact lacks coefficients")
        raw = coefficients.get("conditional_relative_nnls")
        if not isinstance(raw, list) or len(raw) != 5:
            raise PredictionContractError("conditional E2E relative model coefficients are invalid")
        design = (
            1.0,
            values["tool_count"] / 100.0,
            values["request_count"] / 100.0,
            values["input_tokens"] / 1_000_000.0,
            values["output_tokens"] / 10_000.0,
        )
        predicted = sum(
            _finite_nonnegative(value, "coefficient") * basis
            for value, basis in zip(raw, design)
        )
        notes = [
            "trace-conditioned direct E2E; declared workload counts and token sums are inputs",
            "realized output tokens are permitted only under this conditional replay contract",
            "this target is not prospective full-trajectory forecasting and is not "
            "event composition",
            "hardware transfer is unvalidated and no scaling is applied",
        ]
        result = self._result(
            target="conditional_trace_e2e",
            predicted_ms=_positive_prediction(predicted),
            status="predicted",
            contract="trace_conditioned_declared_workload",
            inputs=values,
            artifact=model_path,
            notes=notes,
        )
        report_path = model_path.with_name("report.json")
        report = _read_json(report_path) if report_path.is_file() else {}
        development_metrics = (
            report.get("metrics", {}).get("conditional_relative_nnls", {})
            if isinstance(report.get("metrics"), Mapping)
            else {}
        )
        result["model_details"] = {
            "candidate": "conditional_relative_nnls",
            "features": payload.get("features", []),
            "development_metrics": development_metrics,
            "selection_status": "development_grouped_outer_folds_not_untouched_holdout",
        }
        return result

    def _predict_native_e2e(self, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Predict the direct native:e2e request target conditionally.

        The token candidate is the default.  A cache candidate is selected
        only by the explicit ``cache_trace: true`` request form, because
        cached-token counts describe a realized native trace and are not a
        prospective cache-state forecast.
        """

        base = {"prompt_tokens", "completion_tokens"}
        cache_fields = base | {"cache_trace", "cached_tokens"}
        keys = set(inputs)
        if keys == base:
            cache_trace = False
        elif keys == cache_fields:
            if inputs.get("cache_trace") is not True:
                raise PredictionContractError(
                    "conditional_native_e2e requires cache_trace=true to use cached_tokens"
                )
            cache_trace = True
        else:
            unknown = sorted(keys - cache_fields)
            if unknown:
                raise PredictionContractError(
                    "conditional_native_e2e rejects input field(s): " + ", ".join(unknown)
                )
            missing = sorted((cache_fields if "cache_trace" in keys or "cached_tokens" in keys else base) - keys)
            if missing:
                if "cached_tokens" in keys and "cache_trace" not in keys:
                    raise PredictionContractError(
                        "conditional_native_e2e requires cache_trace=true to use cached_tokens"
                    )
                raise PredictionContractError(
                    "conditional_native_e2e requires input field(s): " + ", ".join(missing)
                )
            raise PredictionContractError(
                "conditional_native_e2e requires cache_trace=true to use cached_tokens"
            )

        forbidden = sorted((keys & _FORBIDDEN_INPUT_NAMES) - {"completion_tokens"})
        if forbidden:
            raise PredictionContractError(
                "prediction request rejects measured or post-event field(s): "
                + ", ".join(forbidden)
            )
        values = {
            "prompt_tokens": _finite_nonnegative(inputs["prompt_tokens"], "prompt_tokens"),
            "completion_tokens": _finite_nonnegative(
                inputs["completion_tokens"], "completion_tokens"
            ),
        }
        if cache_trace:
            values["cache_trace"] = True
            values["cached_tokens"] = _finite_nonnegative(
                inputs["cached_tokens"], "cached_tokens"
            )

        model_path = self.native_fit_artifact_path
        predictor_path = _repo_root() / "docs" / "d9-salvage-20260910" / "native" / "run_native_comparison.py"
        try:
            artifact_payload = load_native_artifact(model_path)
            predicted, model_details = predict_native(
                artifact_payload,
                values,
                cache_trace=cache_trace,
                predictor_path=predictor_path,
            )
        except NativeArtifactError as exc:
            raise PredictionContractError(str(exc)) from exc

        notes = [
            "direct native:e2e request target; queue, prefill, and decode diagnostics are not summed",
            "prompt and completion counts are supplied descriptors of a realized native workload trace",
            "completion_tokens is conditional replay input and is not a prospective online feature",
            "cross-hardware transfer is unvalidated; no hardware scaling is applied",
        ]
        if cache_trace:
            notes.extend(
                [
                    "cache candidate is enabled only with an explicit realized cache trace",
                    "cached_tokens describes that trace and is not a prospective cache-state prediction",
                ]
            )
        else:
            notes.append("token candidate is the default; no cache state is inferred")

        result_inputs = {
            "prompt_tokens": values["prompt_tokens"],
            "completion_tokens": values["completion_tokens"],
        }
        if cache_trace:
            result_inputs["cache_trace"] = True
            result_inputs["cached_tokens"] = values["cached_tokens"]
        result = self._result(
            target="conditional_native_e2e",
            predicted_ms=predicted,
            status="predicted",
            contract=(
                "trace_conditioned_native_e2e_cache"
                if cache_trace
                else "trace_conditioned_native_e2e"
            ),
            inputs=result_inputs,
            artifact=model_path,
            notes=notes,
        )
        result["hardware"].update(
            {
                "fit_hardware_domain": model_details["hardware_domain"],
                "cross_hardware_status": model_details["cross_hardware_status"],
                "transfer_status": "unvalidated_fit_hardware_domain",
                "sensitivity_status": "native_fit_domain_only_unvalidated",
            }
        )
        result["model_details"] = model_details
        report_path = model_path.with_name("report.json")
        if report_path.is_file():
            report = _read_json(report_path)
            metrics = report.get("metrics")
            candidate = model_details["candidate"]
            if isinstance(metrics, Mapping) and isinstance(metrics.get(candidate), Mapping):
                primary = metrics[candidate].get("primary")
                if isinstance(primary, Mapping):
                    result["model_details"]["development_metrics"] = dict(primary)
        result["feature_contract"] = {
            "required": ["prompt_tokens", "completion_tokens"],
            "optional_cache": "cached_tokens only when cache_trace=true",
            "forbidden": sorted(
                (_FORBIDDEN_INPUT_NAMES - {"completion_tokens"})
                | {"observed_ms", "queue_ms", "prefill_ms", "decode_ms", "e2e_ms"}
            ),
            "target_boundary": "native:e2e",
        }
        return result

    def _predict_assignment_event(self, target: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """Use the shared v3 assignment model when an artifact is supplied.

        The retained historical package does not contain a serialized v3
        artifact, so this path is opt-in for a later repaired fit or a small
        review fixture.  It is kept here because it is the one path where the
        explicit hardware profile actually changes the prediction design row.
        Its transfer status remains unvalidated until paired destination labels
        exist.
        """

        if self.workload_model_path is None:
            return self._result(
                target=target,
                predicted_ms=None,
                status="unsupported_no_workload_model_artifact",
                contract="assignment_v3_hardware_parameterized",
                inputs=inputs,
                artifact=None,
                notes=[
                    "supply --workload-model with an assignment.workload-simulator.v3 artifact",
                    "the retained historical candidate does not include an independently "
                    "frozen v3 artifact",
                ],
                uses_hardware=True,
            )
        model_path = self.workload_model_path.resolve()
        if not model_path.is_file():
            raise PredictionContractError(f"workload model artifact is not a file: {model_path}")
        payload = _read_json(model_path)
        if payload.get("schema_version") not in {
            "assignment.workload-simulator.v3",
            "assignment.workload-simulator.v2",
        }:
            raise PredictionContractError("unsupported workload model artifact schema")
        root = _repo_root()
        src = root / "src"
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
        from agentic_sim.assignment.event_simulator import ModelEventInput, ToolEventInput
        from agentic_sim.assignment.workload_simulator import WorkloadSimulator, WorkloadToolInput

        model = WorkloadSimulator.from_mapping(payload)
        if target == "assignment_cpu_event":
            expected = {"action", "event_id", "run_id", "split"}
            optional = {"repository", "instance_id", "declared_read_bytes", "declared_write_bytes"}
            _check_keys(inputs, expected, expected | optional, target)
            if inputs["split"] not in {"holdout", "calibration"}:
                raise PredictionContractError(
                    "assignment_cpu_event split must be holdout or calibration"
                )
            item = WorkloadToolInput.from_action(
                _ensure_action(inputs["action"]),
                event_id=str(inputs["event_id"]),
                run_id=str(inputs["run_id"]),
                split=str(inputs["split"]),
                hardware=self.hardware.to_mapping(),
                repository=str(inputs.get("repository") or ""),
                instance_id=str(inputs.get("instance_id") or ""),
                declared_read_bytes=int(inputs.get("declared_read_bytes") or 0),
                declared_write_bytes=int(inputs.get("declared_write_bytes") or 0),
            )
            predicted = model.predict_tool_ms(item)
            contract = "assignment_v3_logged_cpu_descriptor"
            notes = [
                "shared v3 semantic CPU model; hardware frequency assumption is encoded "
                "by the artifact",
                "transfer accuracy remains unvalidated until paired destination measurements exist",
            ]
        else:
            expected = {
                "request_id",
                "run_id",
                "split",
                "input_tokens",
                "context_tokens",
                "max_output_tokens",
                "output_tokens",
            }
            _exact_keys(inputs, expected, target)
            if inputs["split"] not in {"holdout", "calibration"}:
                raise PredictionContractError(
                    "assignment_gpu_event split must be holdout or calibration"
                )
            item = ModelEventInput.from_mapping(
                {
                    "schema_version": "assignment.model-event-input.v1",
                    "request_id": str(inputs["request_id"]),
                    "run_id": str(inputs["run_id"]),
                    "split": str(inputs["split"]),
                    "input_tokens": int(inputs["input_tokens"]),
                    "context_tokens": int(inputs["context_tokens"]),
                    "max_output_tokens": int(inputs["max_output_tokens"]),
                    "output_tokens": int(inputs["output_tokens"]),
                    "hardware": self.hardware.to_mapping(),
                }
            )
            predicted = model.predict_model_ms(item)
            contract = "assignment_v3_logged_gpu_descriptor"
            notes = [
                "shared v3 GPU model uses logged output_tokens under the assignment "
                "logged-event contract",
                "this is conditional replay when output length came from the recorded workload",
                "transfer accuracy remains unvalidated until paired destination measurements exist",
            ]
        return self._result(
            target=target,
            predicted_ms=predicted,
            status="predicted",
            contract=contract,
            inputs={key: value for key, value in inputs.items() if key not in {"action"}},
            artifact=model_path,
            notes=notes,
            uses_hardware=True,
        )

    def predict(self, target: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if target in {'repaired_semantic_action', 'conditional_repaired_e2e', 'conditional_native_phase'}:
            return self._predict_repaired(target, inputs)
        if target == "historical_cpu_tool_wall":
            return self._predict_cpu(inputs)
        if target == "historical_gpu_request_proxy_wall":
            return self._predict_gpu_proxy(inputs)
        if target == "conditional_gpu_request_proxy_wall":
            return self._predict_conditional_gpu(inputs)
        if target == "historical_start_known_e2e":
            return self._predict_start_e2e(inputs)
        if target == "conditional_trace_e2e":
            return self._predict_conditional_e2e(inputs)
        if target == "conditional_native_e2e":
            return self._predict_native_e2e(inputs)
        if target in ASSIGNMENT_TARGETS:
            return self._predict_assignment_event(target, inputs)
        if target in UNSUPPORTED_TARGETS:
            return self._result(
                target=target,
                predicted_ms=None,
                status="unsupported_no_independent_calibration",
                contract="unsupported_target",
                inputs=inputs,
                artifact=None,
                notes=[
                    "retained confirmation evidence is excluded from fitting",
                    "this target is not integrated under the requested prediction contract",
                    "the target remains in strict score denominators",
                ],
            )
        raise PredictionContractError(f"unsupported target: {target}")

    def predict_request(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise PredictionContractError("request must be an object")
        unknown = sorted(set(request) - {"target", "inputs"})
        if unknown:
            raise PredictionContractError("request has unknown field(s): " + ", ".join(unknown))
        if not isinstance(request.get("target"), str) or not request["target"].strip():
            raise PredictionContractError("request target must be non-empty text")
        inputs = _ensure_mapping(request.get("inputs", {}), "inputs")
        return self.predict(request["target"], inputs)


def predict_request(
    request: Mapping[str, Any],
    *,
    artifact_root: Path | str | None = None,
    hardware: HardwareProfile | Mapping[str, Any] | None = None,
    workload_model_path: Path | str | None = None,
    conditional_gpu_model_path: Path | str | None = None,
    native_fit_artifact_path: Path | str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper for one explicit offline prediction request."""

    return D9Simulator(
        artifact_root=artifact_root,
        hardware=hardware,
        workload_model_path=workload_model_path,
        conditional_gpu_model_path=conditional_gpu_model_path,
        native_fit_artifact_path=native_fit_artifact_path,
    ).predict_request(request)


__all__ = [
    "D9Simulator",
    "PredictionContractError",
    "TARGETS",
    "ASSIGNMENT_TARGETS",
    "UNSUPPORTED_TARGETS",
    "predict_request",
]
