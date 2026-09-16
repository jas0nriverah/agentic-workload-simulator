#!/usr/bin/env python3
"""Fit the reviewed assignment workload model on the retained cohort.

This is the production calibration entry point for the reviewed CPU model.
It consumes only the calibration cache and preserved action map exposed by
``d9_cpu_review.load``/``recover``.  Those helpers quarantine the historical
holdout and contaminated instance before labels or actions are materialized.
The command performs an in-sample serialization parity check; it does not
score a holdout and never invokes the original evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

# Import the stabilized production model first.  d9_cpu_review is imported
# only after this module boundary is loaded; its load/recover helpers are the
# calibration-only, metadata-quarantine data boundary for this command.
from agentic_sim.assignment.event_simulator import ModelEventInput  # noqa: E402
from agentic_sim.assignment.workload_simulator import (  # noqa: E402
    WORKLOAD_MODEL_SCHEMA,
    WorkloadSimulator,
    WorkloadToolInput,
    workload_cpu_row,
)
from scripts.assignment.d9_assignment_eval import HARDWARE, model_input  # noqa: E402
from scripts.assignment.d9_cpu_review import (  # noqa: E402
    ACTION_CACHE,
    _cpu_predict,
    _feature_row,
    _fit_cpu_model,
    _fit_row,
    load,
    recover,
)


_ROUTE_TINY_DELTA_MS = 1e-3


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return result


def _write_generated_json(path: Path, value: Mapping[str, Any]) -> str:
    """Write an immutable generated artifact and its exact SHA-256 sidecar."""
    payload = _canonical_bytes(value)
    digest = _sha256_bytes(payload)
    sidecar_payload = f"{digest}  {path.name}\n".encode("ascii")
    sidecar = path.with_suffix(path.suffix + ".sha256")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = [candidate for candidate in (path, sidecar) if candidate.exists()]
    if existing:
        if path.is_file() and sidecar.is_file() and path.read_bytes() == payload and sidecar.read_bytes() == sidecar_payload:
            return digest
        raise FileExistsError(f"refusing to overwrite generated artifact: {path}")
    temporary: list[tuple[Path, Path]] = []
    try:
        for destination, contents in ((path, payload), (sidecar, sidecar_payload)):
            descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=path.parent)
            candidate = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.append((candidate, destination))
        for candidate, destination in temporary:
            os.replace(candidate, destination)
    finally:
        for candidate, _destination in temporary:
            if candidate.exists():
                candidate.unlink()
    return digest


def _tool_features(
    row: Mapping[str, Any], action: str
) -> WorkloadToolInput:
    return WorkloadToolInput.from_action(
        action,
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        split="calibration",
        hardware=HARDWARE,
        repository=str(row.get("repository") or ""),
        instance_id=str(row.get("instance_id") or ""),
    )


def _model_features(row: Mapping[str, Any]) -> ModelEventInput:
    payload = dict(row)
    payload["split"] = "calibration"
    return model_input(payload)


def _build_calibration_rows(
    data: Mapping[str, Any], actions: Mapping[str, str]
) -> tuple[
    list[tuple[WorkloadToolInput, float]],
    list[tuple[ModelEventInput, float]],
    list[tuple[str, float]],
]:
    tools: list[tuple[WorkloadToolInput, float]] = []
    for row in data.get("tools") or []:
        event_id = str(row["event_id"])
        if event_id not in actions:
            raise ValueError(f"preserved action map missing retained event: {event_id}")
        tools.append((_tool_features(row, actions[event_id]), _positive(row["observed_ms"], "tool observed_ms")))

    models: list[tuple[ModelEventInput, float]] = []
    for row in data.get("models") or []:
        models.append((_model_features(row), _positive(row["observed_ms"], "model observed_ms")))

    trajectories = [
        (str(row["run_id"]), _positive(row["observed_ms"], "trajectory observed_ms"))
        for row in data.get("trajectories") or []
    ]
    return tools, models, trajectories


def _same_prediction(left: float, right: float) -> bool:
    return math.isfinite(left) and math.isfinite(right) and math.isclose(
        left, right, rel_tol=0.0, abs_tol=1e-9
    )


def _calibration_route_parity(
    center: str,
    data: Mapping[str, Any],
    actions: Mapping[str, str],
    simulator: WorkloadSimulator,
    tools: Sequence[tuple[WorkloadToolInput, float]],
) -> dict[str, Any]:
    """Compare the harness's raw-row route with WorkloadToolInput serving.

    The reviewed harness fits the selected semantic model on its preserved
    cache rows (cached flags plus recovered action), then serves a feature row
    built by ``_feature_row``.  This intentionally differs from the production
    route's canonical ``WorkloadToolInput.from_action`` path.  Keeping this
    check separate from serialization parity exposes any cache/extractor
    disagreement instead of silently changing either feature route.
    """
    candidate = "semantic_repo_gate" if center == "gate" else "semantic_repo_median"
    harness_rows = [_fit_row(row, actions) for row in data.get("tools") or []]
    harness_model = _fit_cpu_model(candidate, harness_rows)
    tool_by_event = {feature.event_id: feature for feature, _target in tools}
    mismatches: list[dict[str, Any]] = []
    max_delta = 0.0
    max_relative = 0.0
    tiny_count = 0
    substantive_count = 0
    for row in data.get("tools") or []:
        event_id = str(row["event_id"])
        harness_prediction, _semantic = _cpu_predict(
            harness_model, _feature_row(row, actions)
        )
        features = tool_by_event[event_id]
        production_prediction = simulator.predict_tool_ms(features)
        delta = abs(production_prediction - harness_prediction)
        relative = delta / max(abs(harness_prediction), 1e-9)
        max_delta = max(max_delta, delta)
        max_relative = max(max_relative, relative)
        if _same_prediction(production_prediction, harness_prediction):
            continue
        substantive = delta > _ROUTE_TINY_DELTA_MS
        if substantive:
            substantive_count += 1
        else:
            tiny_count += 1
        if len(mismatches) < 20:
            mismatches.append(
                {
                    "event_id": event_id,
                    "harness_prediction_ms": harness_prediction,
                    "workload_tool_input_prediction_ms": production_prediction,
                    "abs_delta_ms": delta,
                    "relative_delta": relative,
                    "substantive": substantive,
                    "cached_recursive": row.get("recursive"),
                    "canonical_recursive": workload_cpu_row(features).get("recursive"),
                }
            )
    return {
        "model_candidate": candidate,
        "rows_checked": len(harness_rows),
        "mismatch_count": tiny_count + substantive_count,
        "mismatch_examples": mismatches,
        "reported_example_limit": 20,
        "max_abs_delta_ms": max_delta,
        "max_relative_delta": max_relative,
        "tiny_delta_threshold_ms": _ROUTE_TINY_DELTA_MS,
        "tiny_mismatch_count": tiny_count,
        "substantive_mismatch_count": substantive_count,
        "all_predictions_match_within_float_tolerance": tiny_count + substantive_count == 0,
        "route": "d9_cpu_review._fit_row/_feature_row -> SemanticCpuModel versus WorkloadToolInput.from_action -> WorkloadSimulator",
    }


def _parity(
    simulator: WorkloadSimulator,
    restored: WorkloadSimulator,
    tools: Sequence[tuple[WorkloadToolInput, float]],
    models: Sequence[tuple[ModelEventInput, float]],
    trajectories: Sequence[tuple[str, float]],
) -> dict[str, Any]:
    tool_mismatches: list[str] = []
    for features, _target in tools:
        before = simulator.predict_tool_ms(features)
        after = restored.predict_tool_ms(features)
        if not _same_prediction(before, after):
            tool_mismatches.append(features.event_id)

    model_mismatches: list[str] = []
    for features, _target in models:
        before = simulator.predict_model_ms(features)
        after = restored.predict_model_ms(features)
        if not _same_prediction(before, after):
            model_mismatches.append(features.request_id)

    by_run: dict[str, dict[str, float]] = {}
    for features, _target in tools:
        item = by_run.setdefault(features.run_id, {"tool_ms": 0.0, "model_ms": 0.0, "n_tools": 0.0, "n_models": 0.0})
        item["tool_ms"] += simulator.predict_tool_ms(features)
        item["n_tools"] += 1.0
    for features, _target in models:
        item = by_run.setdefault(features.run_id, {"tool_ms": 0.0, "model_ms": 0.0, "n_tools": 0.0, "n_models": 0.0})
        item["model_ms"] += simulator.predict_model_ms(features)
        item["n_models"] += 1.0

    event_sum_mismatches: list[str] = []
    e2e_mismatches: list[str] = []
    for run_id, _target in trajectories:
        item = by_run[run_id]
        before_sum = simulator.predict_event_sum_ms(item["tool_ms"], item["model_ms"])
        after_tool_ms = sum(
            restored.predict_tool_ms(features) for features, _ in tools if features.run_id == run_id
        )
        after_model_ms = sum(
            restored.predict_model_ms(features) for features, _ in models if features.run_id == run_id
        )
        after_sum = restored.predict_event_sum_ms(after_tool_ms, after_model_ms)
        if not _same_prediction(before_sum, after_sum):
            event_sum_mismatches.append(run_id)
        before_e2e = simulator.predict_e2e_ms(
            item["tool_ms"], item["model_ms"], item["n_tools"], item["n_models"]
        )
        after_e2e = restored.predict_e2e_ms(
            after_tool_ms, after_model_ms, item["n_tools"], item["n_models"]
        )
        if not _same_prediction(before_e2e, after_e2e):
            e2e_mismatches.append(run_id)

    return {
        "tool_rows_checked": len(tools),
        "model_rows_checked": len(models),
        "trajectory_rows_checked": len(trajectories),
        "tool_prediction_mismatches": tool_mismatches,
        "model_prediction_mismatches": model_mismatches,
        "event_sum_mismatches": event_sum_mismatches,
        "e2e_mismatches": e2e_mismatches,
        "all_tool_predictions_match": not tool_mismatches,
        "all_model_predictions_match": not model_mismatches,
        "all_event_sums_match": not event_sum_mismatches,
        "all_e2e_predictions_match": not e2e_mismatches,
    }


def _source_hashes() -> dict[str, str]:
    paths = {
        "d9_cpu_fit_reviewed.py": Path(__file__).resolve(),
        "d9_cpu_review.py": ROOT / "scripts/assignment/d9_cpu_review.py",
        "d9_assignment_eval.py": ROOT / "scripts/assignment/d9_assignment_eval.py",
        "workload_simulator.py": ROOT / "src/agentic_sim/assignment/workload_simulator.py",
        "semantic_cpu_model.py": ROOT / "src/agentic_sim/assignment/semantic_cpu_model.py",
        "event_simulator.py": ROOT / "src/agentic_sim/assignment/event_simulator.py",
        "tool_features.py": ROOT / "src/agentic_sim/assignment/tool_features.py",
    }
    return {name: _sha256_path(path) for name, path in sorted(paths.items())}


def fit_reviewed(center: str, output: Path) -> tuple[Path, Path, dict[str, Any]]:
    """Fit, freeze, reload, and parity-check the retained calibration cohort."""
    data = load()
    actions = recover(data)
    tools, models, trajectories = _build_calibration_rows(data, actions)
    simulator = WorkloadSimulator.fit(
        tools,
        models,
        trajectories,
        select_alpha=False,
        cpu_kind="semantic",
        cpu_center=center,
        e2e_mode="additive_overhead",
    )
    calibration_route_parity = _calibration_route_parity(
        center,
        data,
        actions,
        simulator,
        tools,
    )
    if calibration_route_parity["substantive_mismatch_count"]:
        raise RuntimeError(
            "calibration feature-route parity failed: "
            + json.dumps(calibration_route_parity, sort_keys=True)
        )
    artifact = simulator.to_mapping()
    if artifact.get("schema_version") != WORKLOAD_MODEL_SCHEMA:
        raise RuntimeError("reviewed fit did not produce a v3 workload model")
    artifact_digest = _write_generated_json(output, artifact)
    restored = WorkloadSimulator.from_mapping(json.loads(output.read_text(encoding="utf-8")))
    serialization_parity = _parity(simulator, restored, tools, models, trajectories)
    if not all(
        serialization_parity[key]
        for key in (
            "all_tool_predictions_match",
            "all_model_predictions_match",
            "all_event_sums_match",
            "all_e2e_predictions_match",
        )
    ):
        raise RuntimeError(f"serialized workload model parity failed: {serialization_parity}")

    cache_sha = data.get("_cache_sha256")
    manifest: dict[str, Any] = {
        "schema_version": "assignment.d9-cpu-reviewed-fit-manifest.v1",
        "artifact_schema_version": artifact["schema_version"],
        "artifact_path": str(output),
        "artifact_sha256": artifact_digest,
        "artifact_model_sha256": artifact.get("model_sha256"),
        "center": center,
        "fit_mode": "semantic_cpu_plus_nonnegative_runner_overhead",
        "calibration_only": True,
        "holdout_scored": False,
        "original_evaluator_invoked": False,
        "retained_counts": {
            "tools": len(tools),
            "models": len(models),
            "trajectories": len(trajectories),
        },
        "excluded_counts": data.get("_exclusions", {}).get("excluded_counts", {}),
        "calibration_run_ids": sorted({run_id for run_id, _ in trajectories}),
        "cache_sha256": cache_sha,
        "actions_sha256": _sha256_path(ACTION_CACHE),
        "source_sha256": _source_hashes(),
        "calibration_route_parity": calibration_route_parity,
        "serialization_parity": serialization_parity,
    }
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    _write_generated_json(manifest_path, manifest)
    return output, manifest_path, manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center", choices=("median", "gate"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output, manifest, _ = fit_reviewed(args.center, args.output)
    print(json.dumps({"model": str(output), "manifest": str(manifest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
