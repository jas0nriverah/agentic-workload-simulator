#!/usr/bin/env python3
"""Build bounded Deliverable 9 figures from saved offline evidence.

This renderer deliberately consumes already-materialized train-calibration and
out-of-fold artifacts.  It does not fit a model, open excluded evaluation
labels, or reconstruct a broad raw dataset.  Historical CPU and GPU values are
tool/model proxy walls.  Repaired native values are direct per-request
``native:e2e`` walls.  The two boundaries are kept separate in every figure.

The renderer is dependency-free apart from ``rsvg-convert`` for optional PNG
copies of the SVG figures::

    python3 docs/d9-salvage-20260910/figures/build_figures.py

The output directory is this directory by default.  A different output path
is useful for a review copy and does not alter any source artifact.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from html import escape
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]

TRAIN = ROOT / "docs" / "offline-deliverables-20260909" / "training_view"
HIST = ROOT / "docs" / "offline-deliverables-20260909"
RETAINED_FIGURES = HIST / "figures"
SALVAGE = ROOT / "docs" / "d9-salvage-20260910"
NATIVE = SALVAGE / "native"
D3_JOIN_OUTPUT = HERE / "d3_outcome_join.csv"

# This is the retained historical figure-input snapshot used by the earlier
# bounded figure compiler.  Only rows joined to exact train-view run IDs are
# admitted below.  The local derived join is kept with this packet so the
# renderer remains runnable after the external snapshot is no longer mounted.
HISTORICAL_OUTCOME_CANDIDATES = (
    Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/figures-input/trajectories.csv"),
    Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T080000Z/figures-input/trajectories.csv"),
)

SOURCE_PATHS = {
    "training_manifest": TRAIN / "manifest.json",
    "trajectories": TRAIN / "trajectories.jsonl",
    "tools": TRAIN / "tools.jsonl",
    "models": TRAIN / "models.jsonl",
    "historical_joint_predictions": HIST / "joint_hybrid_predictions.csv",
    "historical_joint_metrics": HIST / "joint_hybrid_metrics.json",
    "cpu_oof_predictions": HIST / "cpu" / "class_hybrid_predictions.csv",
    "gpu_proxy_oof_predictions": HIST / "gpu_lifecycle" / "gpu_proxy_oof_predictions.jsonl",
    "sweep_summary": RETAINED_FIGURES / "d4_paired_sweep_summary.csv",
    "sweep_pairs": RETAINED_FIGURES / "d4_eligible_paired_clusters.csv",
    "native_predictions": NATIVE / "predictions.jsonl",
    "native_report": NATIVE / "report.json",
    "native_fit_artifact": NATIVE / "fit_artifact.json",
}

PALETTE = (
    "#1769aa",
    "#7a5af8",
    "#18794e",
    "#b54708",
    "#c11574",
    "#026aa2",
    "#b42318",
    "#667085",
    "#0086c9",
    "#9e77ed",
)
INK = "#172033"
MUTED = "#475467"
GRID = "#d0d5dd"
FRAME = "#344054"
TOOL = "#d97706"
GPU = "#1769aa"
UNKNOWN = "#98a2b3"
PASS = "#18794e"
FAIL = "#b42318"


class FigureDataError(ValueError):
    """A bounded figure input does not satisfy its declared contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FigureDataError(f"unreadable JSON source: {path}: {exc}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FigureDataError(f"missing source: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FigureDataError(f"invalid JSONL {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise FigureDataError(f"JSONL row is not an object: {path}:{line_number}")
        rows.append(value)
    if not rows:
        raise FigureDataError(f"empty JSONL source: {path}")
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise FigureDataError(f"missing source: {path}")
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        raise FigureDataError(f"empty CSV source: {path}")
    return [{key: (value or "").strip() for key, value in row.items()} for row in rows]


def _number(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise FigureDataError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise FigureDataError(f"{name} must be numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise FigureDataError(f"{name} must be finite")
    if positive and number <= 0:
        raise FigureDataError(f"{name} must be > 0")
    if nonnegative and number < 0:
        raise FigureDataError(f"{name} must be >= 0")
    return number


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    number = _number(value, name, positive=positive, nonnegative=not positive)
    if not number.is_integer():
        raise FigureDataError(f"{name} must be an integer")
    return int(number)


def _bool(value: Any, name: str) -> bool:
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise FigureDataError(f"{name} must be boolean: {value!r}")


def _ape(observed: float, predicted: float) -> float:
    if observed == 0.0:
        return 0.0 if predicted == 0.0 else math.inf
    return abs(predicted - observed) / observed * 100.0


def _relative_error_metrics(rows: Iterable[Mapping[str, Any]], observed_key: str, predicted_key: str) -> dict[str, Any]:
    errors = [_ape(_number(row[observed_key], observed_key), _number(row[predicted_key], predicted_key)) for row in rows]
    finite = sorted(error for error in errors if math.isfinite(error))
    ordered = sorted(errors)
    p95 = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None
    return {
        "n": len(errors),
        "within25": sum(error <= 25.0 for error in errors),
        "within25_percent": 100.0 * sum(error <= 25.0 for error in errors) / len(errors) if errors else None,
        "mean_ape_percent": mean(finite) if finite else None,
        "median_ape_percent": median(finite) if finite else None,
        "p95_ape_percent_nearest_rank": p95,
        "worst_ape_percent": max(ordered) if ordered else None,
    }


def _require_columns(rows: list[Mapping[str, Any]], columns: Iterable[str], label: str) -> None:
    if not rows:
        raise FigureDataError(f"{label} is empty")
    missing = sorted(set(columns) - set(rows[0]))
    if missing:
        raise FigureDataError(f"{label} lacks columns: {', '.join(missing)}")


def _short_label(value: str, width: int = 17) -> str:
    value = value.replace("/", "/")
    return value if len(value) <= width else value[: width - 1] + "…"


def _sorted_categories(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    return sorted({str(row["category"]) for row in rows})


def _identity_gated_outcomes(trajectories: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], Path, dict[str, int], dict[str, Any]]:
    """Join retained outcomes after a train-view identity gate.

    The outcome snapshot contains more rows than the retained train view and
    can contain repeated instances across configurations.  Only exact run-ID
    matches are admitted; unmatched train runs remain explicitly excluded from
    the accuracy cohort rather than being filled by an instance-level guess.
    """

    source = next((candidate for candidate in HISTORICAL_OUTCOME_CANDIDATES if candidate.is_file()), None)
    if source is None and D3_JOIN_OUTPUT.is_file():
        source = D3_JOIN_OUTPUT
    if source is None:
        candidates = ", ".join(str(path) for path in HISTORICAL_OUTCOME_CANDIDATES)
        raise FigureDataError(f"missing retained outcome source; checked {candidates} and {D3_JOIN_OUTPUT}")
    source_rows = _read_csv(source)
    _require_columns(
        source_rows,
        {"run_id", "instance_id", "official_resolved"},
        "retained historical outcome source",
    )
    if "category" not in source_rows[0] and "repository" not in source_rows[0]:
        raise FigureDataError("retained historical outcome source lacks category/repository")
    by_run: dict[str, dict[str, str]] = {}
    for row in source_rows:
        run_id = str(row.get("run_id", ""))
        instance_id = str(row.get("instance_id", ""))
        if not run_id or not instance_id:
            raise FigureDataError("retained historical outcome source has an empty identity")
        if run_id in by_run:
            raise FigureDataError(f"retained historical outcome source repeats run ID: {run_id}")
        by_run[run_id] = row

    frozen = None
    packaged_manifest: dict[str, Any] | None = None
    if source == D3_JOIN_OUTPUT:
        packaged_manifest_path = D3_JOIN_OUTPUT.with_name("d3_outcome_join_manifest.json")
        packaged_manifest = _read_json(packaged_manifest_path)
        if packaged_manifest.get("schema") != "d9-salvage-d3-outcome-join.v1":
            raise FigureDataError("packaged D3 outcome join has an unknown schema")
        if packaged_manifest.get("row_count") != len(source_rows):
            raise FigureDataError("packaged D3 outcome join row count does not match its manifest")
        if packaged_manifest.get("join_sha256") != _sha256(D3_JOIN_OUTPUT):
            raise FigureDataError("packaged D3 outcome join hash does not match its manifest")
        if packaged_manifest.get("training_trajectories_sha256") != _sha256(SOURCE_PATHS["trajectories"]):
            raise FigureDataError("packaged D3 outcome join was built from a different train-view trajectory source")
        if packaged_manifest.get("join_contract") != "exact_run_id_only":
            raise FigureDataError("packaged D3 outcome join does not declare exact-run matching")
        scope_hash = str(packaged_manifest.get("scope_manifest_list_sha256", ""))
        if len(scope_hash) != 64 or any(character not in "0123456789abcdef" for character in scope_hash):
            raise FigureDataError("packaged D3 outcome join lacks a valid recorded scope hash")
    else:
        # Initial acquisition uses the frozen identity-only scope gate.  No
        # source label is touched until this check has admitted the train-view
        # identity.  Replays use the compact join manifest above and do not
        # need the original absolute assignment snapshot paths.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        try:
            from scripts.assignment.historical_analysis_scope import frozen_scope

            frozen = frozen_scope()
        except Exception as exc:  # pragma: no cover - source-boundary failure
            raise FigureDataError(f"cannot load frozen historical identity scope: {exc}") from exc
        scope_hash = frozen.artifact()["manifest_list_sha256"]

    joined: list[dict[str, Any]] = []
    match_counts = {"exact_run_id": 0, "unmatched_train_run_id": 0}
    for trajectory in trajectories:
        run_id = str(trajectory["run_id"])
        instance_id = str(trajectory["instance_id"])
        if frozen is not None:
            try:
                admitted = frozen.is_eligible({"run_id": run_id, "instance_id": instance_id})
            except Exception as exc:
                raise FigureDataError(f"historical identity scope rejected {run_id}: {exc}") from exc
            if not admitted:
                raise FigureDataError(f"train-view identity is excluded by frozen scope: {run_id}")

        candidate = by_run.get(run_id)
        match_mode = "exact_run_id"
        if candidate is None:
            match_counts["unmatched_train_run_id"] += 1
            continue

        candidate_instance = str(candidate.get("instance_id", ""))
        if candidate_instance != instance_id:
            raise FigureDataError(f"outcome instance mismatch for {run_id}: {candidate_instance} != {instance_id}")
        candidate_category = str(candidate.get("category") or candidate.get("repository") or "").strip()
        if not candidate_category or candidate_category != str(trajectory["repository"]):
            raise FigureDataError(f"outcome category mismatch for {run_id}: {candidate_category!r}")
        official = _bool(candidate.get("official_resolved"), f"{run_id}.official_resolved")
        joined.append(
            {
                "run_id": run_id,
                "instance_id": instance_id,
                "category": candidate_category,
                "official_resolved": official,
                "match_mode": match_mode,
                "source_run_id": run_id,
                "source_instance_id": candidate_instance,
                "source_config_id": str(candidate.get("config_id", "")),
            }
        )
        match_counts[match_mode] += 1
    if len({row["run_id"] for row in joined}) != len(joined):
        raise FigureDataError("identity-gated outcome join repeats a train run ID")
    source_metadata = {
        "source_sha256": _sha256(source),
        "packaged_join": source == D3_JOIN_OUTPUT,
        "scope_manifest_list_sha256": scope_hash,
    }
    return joined, source, match_counts, source_metadata


def _load_data() -> dict[str, Any]:
    manifest = _read_json(SOURCE_PATHS["training_manifest"])
    if manifest.get("partition") != "train_calibration":
        raise FigureDataError("figure source is not the retained train_calibration view")
    if manifest.get("counts", {}).get("trajectories", {}).get("retained") != 819:
        raise FigureDataError("unexpected retained trajectory count")

    trajectories = _read_jsonl(SOURCE_PATHS["trajectories"])
    joint_rows = _read_csv(SOURCE_PATHS["historical_joint_predictions"])
    _require_columns(
        trajectories,
        {"run_id", "instance_id", "repository", "outer_fold", "observed_ms", "tool_wall_ms", "model_wall_ms"},
        "training trajectories",
    )
    _require_columns(
        joint_rows,
        {
            "run_id",
            "instance_id",
            "outer_fold",
            "observed_cpu_sum_ms",
            "observed_gpu_proxy_sum_ms",
            "observed_e2e_ms",
            "predicted_cpu_sum_ms",
            "predicted_gpu_proxy_sum_ms",
            "predicted_start_known_e2e_ms",
            "start_known_e2e_pass",
        },
        "historical joint OOF predictions",
    )
    if len(trajectories) != 819 or len(joint_rows) != 819:
        raise FigureDataError("historical figure source must contain exactly 819 retained runs")
    trajectory_by_run = {str(row["run_id"]): row for row in trajectories}
    joint_by_run = {str(row["run_id"]): row for row in joint_rows}
    if len(trajectory_by_run) != 819 or len(joint_by_run) != 819:
        raise FigureDataError("historical run IDs are not unique")
    if set(trajectory_by_run) != set(joint_by_run):
        raise FigureDataError("trajectory and historical OOF run IDs do not join exactly")
    outcome_join, outcome_source, outcome_match_counts, outcome_source_metadata = _identity_gated_outcomes(trajectories)
    outcome_by_run = {row["run_id"]: row for row in outcome_join}

    ratio_rows: list[dict[str, Any]] = []
    for run_id in sorted(joint_by_run):
        trajectory = trajectory_by_run[run_id]
        joint = joint_by_run[run_id]
        outcome = outcome_by_run.get(run_id)
        if str(joint["instance_id"]) != str(trajectory["instance_id"]):
            raise FigureDataError(f"historical joint instance mismatch for {run_id}")
        # D2 remains a timing-only view over all 819 retained runs.  Accuracy
        # panels use the exact-run-ID subset admitted by the identity gate.
        category = str(trajectory["repository"] if outcome is None else outcome["category"])
        observed_tool = _number(joint["observed_cpu_sum_ms"], f"{run_id}.observed_cpu_sum_ms", positive=True)
        observed_gpu = _number(joint["observed_gpu_proxy_sum_ms"], f"{run_id}.observed_gpu_proxy_sum_ms", positive=True)
        observed_e2e = _number(joint["observed_e2e_ms"], f"{run_id}.observed_e2e_ms", positive=True)
        trajectory_e2e = _number(trajectory["observed_ms"], f"{run_id}.trajectory.observed_ms", positive=True)
        if not math.isclose(observed_e2e, trajectory_e2e, rel_tol=1e-12, abs_tol=1e-9):
            raise FigureDataError(f"historical E2E boundary mismatch for {run_id}")
        ratio = observed_tool / observed_gpu
        ratio_rows.append(
            {
                "run_id": run_id,
                "instance_id": str(joint["instance_id"]),
                "category": category,
                "official_resolved": None if outcome is None else outcome["official_resolved"],
                "outcome_match_mode": "unmatched_train_run_id" if outcome is None else outcome["match_mode"],
                "outer_fold": _integer(joint["outer_fold"], f"{run_id}.outer_fold"),
                "observed_tool_wall_ms": observed_tool,
                "observed_gpu_proxy_wall_ms": observed_gpu,
                "observed_e2e_ms": observed_e2e,
                "proxy_ratio": ratio,
                "e2e_model_within25": _bool(joint["start_known_e2e_pass"], f"{run_id}.start_known_e2e_pass"),
                "predicted_tool_wall_ms": _number(joint["predicted_cpu_sum_ms"], f"{run_id}.predicted_cpu_sum_ms", positive=True),
                "predicted_gpu_proxy_wall_ms": _number(joint["predicted_gpu_proxy_sum_ms"], f"{run_id}.predicted_gpu_proxy_sum_ms", positive=True),
                "predicted_e2e_ms": _number(joint["predicted_start_known_e2e_ms"], f"{run_id}.predicted_start_known_e2e_ms", positive=True),
            }
        )

    category_aggregates: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ratio_rows:
        if row["official_resolved"] is None:
            continue
        grouped[row["category"]].append(row)
    for category in sorted(grouped):
        group = grouped[category]
        category_aggregates.append(
            {
                "category": category,
                "n_runs": len(group),
                "n_instances": len({row["instance_id"] for row in group}),
                "mean_observed_e2e_s": mean(row["observed_e2e_ms"] for row in group) / 1000.0,
                "mean_proxy_ratio": mean(row["proxy_ratio"] for row in group),
                "official_resolved_n": sum(bool(row["official_resolved"]) for row in group),
                "official_resolved_percent": 100.0 * sum(bool(row["official_resolved"]) for row in group) / len(group),
                "e2e_model_within25_percent": 100.0 * sum(row["e2e_model_within25"] for row in group) / len(group),
                "observed_tool_wall_s": sum(row["observed_tool_wall_ms"] for row in group) / 1000.0,
                "observed_gpu_proxy_wall_s": sum(row["observed_gpu_proxy_wall_ms"] for row in group) / 1000.0,
            }
        )

    sweep_rows = _read_csv(SOURCE_PATHS["sweep_summary"])
    _require_columns(
        sweep_rows,
        {
            "parameter",
            "setting",
            "n_pairs",
            "n_instance_clusters",
            "baseline_mean_e2e_s",
            "treatment_mean_e2e_s",
            "baseline_resolved_n",
            "treatment_resolved_n",
        },
        "retained sweep summary",
    )
    expected_parameters = {"call_limit", "max_output_tokens", "observation_length", "temperature"}
    if {row["parameter"] for row in sweep_rows} != expected_parameters:
        raise FigureDataError("retained sweep summary does not contain the four required parameters")
    sweep_data: list[dict[str, Any]] = []
    for row in sweep_rows:
        pairs = _integer(row["n_pairs"], "n_pairs", positive=True)
        clusters = _integer(row["n_instance_clusters"], "n_instance_clusters", positive=True)
        baseline_resolved = _integer(row["baseline_resolved_n"], "baseline_resolved_n")
        treatment_resolved = _integer(row["treatment_resolved_n"], "treatment_resolved_n")
        if baseline_resolved > pairs or treatment_resolved > pairs:
            raise FigureDataError("sweep resolved count exceeds matched-pair denominator")
        sweep_data.append(
            {
                "parameter": row["parameter"],
                "setting": row["setting"],
                "n_pairs": pairs,
                "n_instance_clusters": clusters,
                "baseline_mean_e2e_s": _number(row["baseline_mean_e2e_s"], "baseline_mean_e2e_s", positive=True),
                "treatment_mean_e2e_s": _number(row["treatment_mean_e2e_s"], "treatment_mean_e2e_s", positive=True),
                "baseline_resolved_rate_percent": 100.0 * baseline_resolved / pairs,
                "treatment_resolved_rate_percent": 100.0 * treatment_resolved / pairs,
                "baseline_resolved_n": baseline_resolved,
                "treatment_resolved_n": treatment_resolved,
            }
        )
    if len(sweep_data) != 12:
        raise FigureDataError("expected exactly three retained settings per parameter")
    for parameter in expected_parameters:
        if sum(row["parameter"] == parameter for row in sweep_data) != 3:
            raise FigureDataError(f"sweep parameter {parameter} lacks three settings")

    selected = max(ratio_rows, key=lambda row: (row["proxy_ratio"], row["run_id"]))
    tools = _read_jsonl(SOURCE_PATHS["tools"])
    models = _read_jsonl(SOURCE_PATHS["models"])
    selected_tools = [row for row in tools if str(row["run_id"]) == selected["run_id"]]
    selected_models = [row for row in models if str(row["run_id"]) == selected["run_id"]]
    if len(selected_tools) != _integer(joint_by_run[selected["run_id"]]["cpu_event_count"], "cpu_event_count"):
        raise FigureDataError("selected run CPU event count does not match retained trajectory")
    if len(selected_models) != _integer(joint_by_run[selected["run_id"]]["gpu_request_count"], "gpu_request_count"):
        raise FigureDataError("selected run model-request count does not match retained trajectory")
    cpu_predictions = _read_csv(SOURCE_PATHS["cpu_oof_predictions"])
    cpu_by_event = {str(row["event_id"]): row for row in cpu_predictions}
    gpu_predictions = _read_jsonl(SOURCE_PATHS["gpu_proxy_oof_predictions"])
    gpu_by_request = {
        str(row["request_id"]): row
        for row in gpu_predictions
        if str(row.get("run_id")) == selected["run_id"]
    }
    event_log: list[dict[str, Any]] = []
    for ordinal, row in enumerate(selected_tools, 1):
        event_id = str(row["event_id"])
        prediction = cpu_by_event.get(event_id)
        if prediction is None:
            raise FigureDataError(f"selected CPU event has no saved OOF prediction: {event_id}")
        event_log.append(
            {
                "event_kind": "cpu_tool_wall_proxy",
                "event_index": ordinal,
                "event_id": event_id,
                "operation_class": str(row.get("operation_class", "")),
                "request_id": "",
                "input_tokens": "",
                "output_tokens": "",
                "context_tokens": "",
                "observed_ms": _number(row["observed_ms"], f"{event_id}.observed_ms", positive=True),
                "predicted_ms": _number(prediction["prediction"], f"{event_id}.prediction", positive=True),
                "within25": _bool(prediction["within25"], f"{event_id}.within25"),
                "boundary": "historical tool event wall; not atomic CPU operation",
            }
        )
    for ordinal, row in enumerate(selected_models, 1):
        request_id = str(row["request_id"])
        prediction = gpu_by_request.get(request_id)
        if prediction is None:
            raise FigureDataError(f"selected model request has no saved OOF prediction: {request_id}")
        if prediction.get("selected_candidate") != "global_median":
            raise FigureDataError("selected GPU proxy OOF source is not the retained global median")
        event_log.append(
            {
                "event_kind": "gpu_request_proxy_wall",
                "event_index": ordinal,
                "event_id": request_id,
                "operation_class": "",
                "request_id": request_id,
                "input_tokens": _integer(row["input_tokens"], f"{request_id}.input_tokens"),
                "output_tokens": _integer(row["output_tokens"], f"{request_id}.output_tokens"),
                "context_tokens": _integer(row["context_tokens"], f"{request_id}.context_tokens"),
                "observed_ms": _number(row["observed_ms"], f"{request_id}.observed_ms", positive=True),
                "predicted_ms": _number(prediction["predicted_ms"], f"{request_id}.predicted_ms", positive=True),
                "within25": _bool(prediction["within_25_percent"], f"{request_id}.within_25_percent"),
                "boundary": "historical completed-request proxy; not native queue/prefill/decode",
            }
        )
    event_log.sort(key=lambda row: (row["event_kind"], int(row["event_index"])))

    observed_tool = selected["observed_tool_wall_ms"]
    observed_gpu = selected["observed_gpu_proxy_wall_ms"]
    observed_e2e = selected["observed_e2e_ms"]
    breakdown = [
        {
            "boundary": "observed_outer_e2e_ms",
            "value_ms": observed_e2e,
            "interpretation": "direct outer E2E boundary; shown separately",
        },
        {
            "boundary": "observed_tool_wall_ms",
            "value_ms": observed_tool,
            "interpretation": "historical tool wall; separate host-work proxy boundary",
        },
        {
            "boundary": "observed_gpu_request_proxy_wall_ms",
            "value_ms": observed_gpu,
            "interpretation": "historical completed-request proxy; separate from outer E2E",
        },
        {
            "boundary": "predicted_tool_sum_ms",
            "value_ms": selected["predicted_tool_wall_ms"],
            "interpretation": "saved CPU hybrid OOF component sum; not stacked into predicted E2E",
        },
        {
            "boundary": "predicted_gpu_proxy_sum_ms",
            "value_ms": selected["predicted_gpu_proxy_wall_ms"],
            "interpretation": "saved GPU proxy OOF component sum; not stacked into predicted E2E",
        },
        {
            "boundary": "predicted_direct_e2e_ms",
            "value_ms": selected["predicted_e2e_ms"],
            "interpretation": "saved direct start-known E2E control; separate target",
        },
    ]

    native_rows = _read_jsonl(SOURCE_PATHS["native_predictions"])
    native_report = _read_json(SOURCE_PATHS["native_report"])
    native_artifact = _read_json(SOURCE_PATHS["native_fit_artifact"])
    if len(native_rows) != 2080 or native_report.get("population", {}).get("requests") != 2080:
        raise FigureDataError("native OOF source must contain exactly 2,080 requests")
    if any(row.get("missing_predictions") for row in native_rows):
        raise FigureDataError("native OOF source contains missing predictions")
    native_metrics: dict[str, Any] = {}
    for candidate in ("relative_nnls_token", "relative_nnls_token_cache"):
        metric_rows = [
            {
                "observed": row["observed_ms"]["e2e"],
                "predicted": row["predictions_ms"][candidate]["e2e"],
            }
            for row in native_rows
        ]
        recomputed = _relative_error_metrics(metric_rows, "observed", "predicted")
        report_metric = native_report["metrics"][candidate]["primary"]
        for key in ("n", "within25", "within25_percent", "p95_ape_percent_nearest_rank", "worst_ape_percent"):
            report_key = {
                "n": "population_requests",
                "within25": "within25_count",
                "within25_percent": "within25_percent",
                "p95_ape_percent_nearest_rank": "p95_ape_percent_nearest_rank",
                "worst_ape_percent": "worst_ape_percent",
            }[key]
            left = recomputed[key]
            right = report_metric[report_key]
            if isinstance(left, float) or isinstance(right, float):
                if not math.isclose(float(left), float(right), rel_tol=1e-11, abs_tol=1e-11):
                    raise FigureDataError(f"native metric mismatch for {candidate}: {key}")
            elif left != right:
                raise FigureDataError(f"native metric mismatch for {candidate}: {key}")
        native_metrics[candidate] = {
            **recomputed,
            "report_primary": report_metric,
            "fit_hardware_domain": native_artifact.get("verified_hardware_domain"),
            "target_boundary": "native:e2e direct request; phases are diagnostics",
        }

    return {
        "manifest": manifest,
        "ratio_rows": ratio_rows,
        "d3_rows": [row for row in ratio_rows if row["official_resolved"] is not None],
        "outcome_join": outcome_join,
        "outcome_source": outcome_source,
        "outcome_match_counts": outcome_match_counts,
        "outcome_source_metadata": outcome_source_metadata,
        "category_aggregates": category_aggregates,
        "sweep_data": sorted(sweep_data, key=lambda row: (row["parameter"], row["setting"])),
        "selected": selected,
        "event_log": event_log,
        "breakdown": breakdown,
        "native_rows": native_rows,
        "native_metrics": native_metrics,
        "native_report": native_report,
    }


def _write_csv(path: Path, rows: list[Mapping[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _svg_header(width: int, height: int, title: str, subtitle: str = "") -> list[str]:
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        f"<title>{escape(title)}</title>",
        f"<desc>{escape(subtitle)}</desc>",
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        "<style>",
        "text{font-family:DejaVu Sans,Arial,sans-serif;fill:#172033}",
        ".title{font-size:24px;font-weight:800}.subtitle{font-size:13px;fill:#475467;font-weight:600}",
        ".panel-title{font-size:16px;font-weight:800}.tick{font-size:11px;fill:#344054;font-weight:700}",
        ".axis-label{font-size:13px;fill:#172033;font-weight:800}.note{font-size:11px;fill:#475467;font-weight:600}",
        ".grid{stroke:#e4e7ec;stroke-width:1}.frame{fill:#ffffff;stroke:#344054;stroke-width:2}",
        ".legend{font-size:11px;fill:#344054;font-weight:700}",
        "</style>",
    ]
    lines.append(f'<text x="34" y="34" class="title">{escape(title)}</text>')
    if subtitle:
        lines.append(f'<text x="34" y="58" class="subtitle">{escape(subtitle)}</text>')
    return lines


def _svg_text(x: float, y: float, text: str, cls: str = "note", **attrs: Any) -> str:
    extra = " ".join(f'{key.replace("_", "-")}="{escape(str(value))}"' for key, value in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" {extra}>{escape(text)}</text>'


def _finish_svg(lines: list[str]) -> str:
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def _write_svg_and_png(out_dir: Path, stem: str, svg: str) -> list[str]:
    svg_path = out_dir / f"{stem}.svg"
    png_path = out_dir / f"{stem}.png"
    svg_path.write_text(svg, encoding="utf-8")
    converter = shutil.which("rsvg-convert")
    if converter:
        subprocess.run([converter, "-o", str(png_path), str(svg_path)], check=True)
    return [svg_path.name] + ([png_path.name] if png_path.is_file() else [])


def _bounds(values: Iterable[float], *, log: bool = False, pad: float = 0.08) -> tuple[float, float]:
    values = [float(value) for value in values]
    if not values:
        raise FigureDataError("plot has no numeric values")
    low = min(values)
    high = max(values)
    if log:
        if low <= 0:
            raise FigureDataError("log plot has a non-positive value")
        lo = 10 ** math.floor(math.log10(low))
        hi = 10 ** math.ceil(math.log10(high))
        if lo == hi:
            lo /= 10
            hi *= 10
        return lo, hi
    if math.isclose(low, high):
        delta = max(1.0, abs(low) * 0.1)
        return low - delta, high + delta
    delta = (high - low) * pad
    return low - delta, high + delta


def _log_position(value: float, low: float, high: float) -> float:
    return (math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low))


def _linear_position(value: float, low: float, high: float) -> float:
    return (value - low) / (high - low)


def _log_ticks(low: float, high: float) -> list[float]:
    start = math.floor(math.log10(low))
    stop = math.ceil(math.log10(high))
    ticks = [10.0**power for power in range(start, stop + 1)]
    return ticks if len(ticks) <= 7 else [low, 10 ** ((math.log10(low) + math.log10(high)) / 2), high]


def _axis_grid(
    lines: list[str],
    *,
    left: float,
    top: float,
    width: float,
    height: float,
    x_low: float,
    x_high: float,
    y_low: float,
    y_high: float,
    x_label: str,
    y_label: str,
    log_x: bool = False,
    y_ticks: list[float] | None = None,
    x_ticks: list[float] | None = None,
    show_x_tick_labels: bool = True,
    show_y_tick_labels: bool = True,
) -> None:
    lines.append(f'<rect class="frame" x="{left:.1f}" y="{top:.1f}" width="{width:.1f}" height="{height:.1f}"/>')
    x_ticks = x_ticks or (_log_ticks(x_low, x_high) if log_x else [x_low + (x_high - x_low) * i / 5 for i in range(6)])
    y_ticks = y_ticks or [y_low + (y_high - y_low) * i / 5 for i in range(6)]
    for tick in x_ticks:
        frac = _log_position(tick, x_low, x_high) if log_x else _linear_position(tick, x_low, x_high)
        x = left + frac * width
        lines.append(f'<line class="grid" x1="{x:.1f}" y1="{top:.1f}" x2="{x:.1f}" y2="{top + height:.1f}"/>')
        if show_x_tick_labels:
            label = f"{tick:.3g}"
            lines.append(_svg_text(x, top + height + 22, label, "tick", text_anchor="middle"))
    for tick in y_ticks:
        frac = _linear_position(tick, y_low, y_high)
        y = top + height - frac * height
        lines.append(f'<line class="grid" x1="{left:.1f}" y1="{y:.1f}" x2="{left + width:.1f}" y2="{y:.1f}"/>')
        if show_y_tick_labels:
            lines.append(_svg_text(left - 10, y + 4, f"{tick:.3g}", "tick", text_anchor="end"))
    lines.append(_svg_text(left + width / 2, top + height + 48, x_label, "axis-label", text_anchor="middle"))
    y_x = left - 52
    y_mid = top + height / 2
    lines.append(_svg_text(y_x, y_mid, y_label, "axis-label", text_anchor="middle", transform=f"rotate(-90 {y_x:.1f} {y_mid:.1f})"))


def _render_d2(rows: list[dict[str, Any]]) -> str:
    groups = _sorted_categories(rows)
    width = 1320
    top = 100
    bottom = 86
    left = 285
    right = 55
    height = max(560, top + bottom + len(groups) * 40)
    plot_w = width - left - right
    plot_h = height - top - bottom
    low, high = _bounds((row["proxy_ratio"] for row in rows), log=True)
    lines = _svg_header(
        width,
        height,
        "D2 · CPU/GPU proxy latency ratio by repository",
        "One dot per retained train-calibration trajectory; ratio uses recorded tool-event wall / completed model-request proxy wall.",
    )
    _axis_grid(
        lines,
        left=left,
        top=top,
        width=plot_w,
        height=plot_h,
        x_low=low,
        x_high=high,
        y_low=0,
        y_high=len(groups),
        x_label="Recorded tool-event / model-request proxy wall ratio (log scale)",
        y_label="Repository category",
        log_x=True,
        y_ticks=[index + 0.5 for index in range(len(groups))],
        show_y_tick_labels=False,
    )
    for index, category in enumerate(groups):
        y = top + plot_h - (index + 0.5) * plot_h / len(groups)
        lines.append(_svg_text(left - 12, y + 4, _short_label(category, 28), "tick", text_anchor="end"))
        color = PALETTE[index % len(PALETTE)]
        for sample_index, row in enumerate([item for item in rows if item["category"] == category]):
            x = left + _log_position(row["proxy_ratio"], low, high) * plot_w
            jitter = ((sample_index * 17 + len(category) * 3) % 17 - 8) * min(1.5, plot_h / len(groups) / 18)
            yy = y + jitter
            lines.append(
                f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="4.1" fill="{color}" fill-opacity="0.62" stroke="#ffffff" stroke-width="0.7">'
                f'<title>{escape(category)} · {escape(row["instance_id"])} · ratio={row["proxy_ratio"]:.5g}</title></circle>'
            )
    x_one = left + _log_position(1.0, low, high) * plot_w if low < 1.0 < high else None
    if x_one is not None:
        lines.append(f'<line x1="{x_one:.1f}" y1="{top}" x2="{x_one:.1f}" y2="{top + plot_h}" stroke="#b42318" stroke-width="2" stroke-dasharray="6,5"/>')
        lines.append(_svg_text(x_one + 7, top + 18, "ratio = 1", "note", fill=FAIL))
    lines.append(_svg_text(34, height - 22, "Historical proxy boundary: numerator is summed recorded tool-event wall; denominator is completed model-request proxy wall, not GPU-kernel time.", "note"))
    return _finish_svg(lines)


def _panel_title(lines: list[str], x: float, y: float, title: str) -> None:
    lines.append(_svg_text(x + 14, y + 24, title, "panel-title"))


def _render_d9_diagnostic(aggregates: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    width, height = 1540, 730
    panel_y, panel_h = 96, 515
    panel_w, gap = 470, 28
    panel_xs = [34, 34 + panel_w + gap, 34 + 2 * (panel_w + gap)]
    groups = [row["category"] for row in aggregates]
    colors = {group: PALETTE[i % len(PALETTE)] for i, group in enumerate(groups)}
    lines = _svg_header(
        width,
        height,
        "D9 diagnostic · Category latency vs model coverage",
        "This diagnostic is separate from the unavailable PDF D3 task-accuracy plot; y is saved direct-E2E model coverage within 25%, not task resolution.",
    )
    # Panel 1: category bubbles, E2E latency versus model coverage.
    x0, y0 = panel_xs[0] + 70, panel_y + 55
    pw, ph = panel_w - 95, panel_h - 120
    x_low, x_high = _bounds((row["mean_observed_e2e_s"] for row in aggregates))
    _axis_grid(lines, left=x0, top=y0, width=pw, height=ph, x_low=x_low, x_high=x_high, y_low=0, y_high=100, x_label="Mean observed outer E2E (s)", y_label="OOF model coverage ≤25% (%)", y_ticks=[0, 20, 40, 60, 80, 100])
    _panel_title(lines, panel_xs[0], panel_y, "Model coverage proxy vs average latency")
    for row in aggregates:
        x = x0 + _linear_position(row["mean_observed_e2e_s"], x_low, x_high) * pw
        y = y0 + ph - row["e2e_model_within25_percent"] / 100 * ph
        radius = 7 + 1.7 * math.sqrt(row["n_runs"])
        color = colors[row["category"]]
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2"><title>{escape(row["category"])} · n={row["n_runs"]} · coverage={row["e2e_model_within25_percent"]:.2f}%</title></circle>')
        lines.append(_svg_text(x + radius + 4, y + 4, _short_label(row["category"], 18), "note", fill=color))
    # Panel 2: category bubbles, proxy ratio versus model coverage.
    x0, y0 = panel_xs[1] + 75, panel_y + 55
    pw, ph = panel_w - 100, panel_h - 120
    ratio_low, ratio_high = _bounds((row["mean_proxy_ratio"] for row in aggregates), log=True)
    _axis_grid(lines, left=x0, top=y0, width=pw, height=ph, x_low=ratio_low, x_high=ratio_high, y_low=0, y_high=100, x_label="Mean tool / model proxy ratio (log scale)", y_label="OOF model coverage ≤25% (%)", log_x=True, y_ticks=[0, 20, 40, 60, 80, 100])
    _panel_title(lines, panel_xs[1], panel_y, "Model coverage proxy vs CPU/GPU proxy latency")
    for row in aggregates:
        x = x0 + _log_position(row["mean_proxy_ratio"], ratio_low, ratio_high) * pw
        y = y0 + ph - row["e2e_model_within25_percent"] / 100 * ph
        radius = 7 + 1.7 * math.sqrt(row["n_runs"])
        color = colors[row["category"]]
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2"><title>{escape(row["category"])} · n={row["n_runs"]} · ratio={row["mean_proxy_ratio"]:.4g}</title></circle>')
        lines.append(_svg_text(x + radius + 4, y + 4, _short_label(row["category"], 18), "note", fill=color))
    # Panel 3: every trajectory sample.
    x0, y0 = panel_xs[2] + 75, panel_y + 55
    pw, ph = panel_w - 100, panel_h - 120
    sample_ratio_low, sample_ratio_high = _bounds((row["proxy_ratio"] for row in rows), log=True)
    e2e_low, e2e_high = 0.0, max(row["observed_e2e_ms"] for row in rows) / 1000.0 * 1.08
    _axis_grid(lines, left=x0, top=y0, width=pw, height=ph, x_low=sample_ratio_low, x_high=sample_ratio_high, y_low=e2e_low, y_high=e2e_high, x_label="Tool / model proxy ratio (log scale)", y_label="Observed outer E2E (s)", log_x=True)
    _panel_title(lines, panel_xs[2], panel_y, "Per-trajectory proxy latency samples")
    for row in rows:
        x = x0 + _log_position(row["proxy_ratio"], sample_ratio_low, sample_ratio_high) * pw
        y = y0 + ph - _linear_position(row["observed_e2e_ms"] / 1000.0, e2e_low, e2e_high) * ph
        color = colors[row["category"]]
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}" fill-opacity="0.48"><title>{escape(row["category"])} · {escape(row["instance_id"])} · ratio={row["proxy_ratio"]:.4g} · E2E={row["observed_e2e_ms"] / 1000.0:.3g}s</title></circle>')
    legend_x = 60
    legend_y = 655
    for index, group in enumerate(groups):
        x = legend_x + (index % 5) * 290
        y = legend_y + (index // 5) * 20
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{colors[group]}"/>')
        lines.append(_svg_text(x + 10, y + 4, group, "legend"))
    lines.append(_svg_text(34, height - 12, "Diagnostic only: proxy rows are not native GPU timing, and model coverage is not official task accuracy.", "note"))
    return _finish_svg(lines)


def _render_d3(aggregates: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Render the retained D3 three-panel layout with identity-gated accuracy."""

    width, height = 1500, 720
    lines = _svg_header(
        width,
        height,
        "D3 · Category tradeoffs from retained resolved labels",
        "Official resolution is joined only by exact retained train-view run ID; unmatched train runs are excluded, and latency simulation never changes accuracy values.",
    )
    panel_w, gap, top, panel_h = 455, 24, 118, 390
    panel_xs = [34, 34 + panel_w + gap, 34 + 2 * (panel_w + gap)]
    groups = [row["category"] for row in aggregates]
    colors = {group: PALETTE[index % len(PALETTE)] for index, group in enumerate(groups)}

    # First panel: official task resolution versus observed outer E2E.
    x = panel_xs[0]
    _panel_title(lines, x, top, "Accuracy vs average latency")
    plot_left, plot_top = x + 70, top + 55
    plot_w, plot_h = panel_w - 95, panel_h - 125
    x_low, x_high = _bounds((row["mean_observed_e2e_s"] for row in aggregates))
    _axis_grid(
        lines,
        left=plot_left,
        top=plot_top,
        width=plot_w,
        height=plot_h,
        x_low=x_low,
        x_high=x_high,
        y_low=0,
        y_high=100,
        x_label="Mean observed outer E2E (s)",
        y_label="Official resolved (%)",
        y_ticks=[0, 20, 40, 60, 80, 100],
    )
    for row in aggregates:
        xx = plot_left + _linear_position(row["mean_observed_e2e_s"], x_low, x_high) * plot_w
        yy = plot_top + plot_h - row["official_resolved_percent"] / 100.0 * plot_h
        radius = 7 + 1.7 * math.sqrt(row["n_runs"])
        color = colors[row["category"]]
        lines.append(
            f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="{radius:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2">'
            f'<title>{escape(row["category"])} · n={row["n_runs"]} · resolved={row["official_resolved_percent"]:.2f}%</title></circle>'
        )

    # Second panel: official task resolution versus the historical proxy ratio.
    x = panel_xs[1]
    _panel_title(lines, x, top, "Accuracy vs CPU/GPU latency")
    plot_left, plot_top = x + 75, top + 55
    plot_w, plot_h = panel_w - 100, panel_h - 125
    ratio_low, ratio_high = _bounds((row["mean_proxy_ratio"] for row in aggregates), log=True)
    _axis_grid(
        lines,
        left=plot_left,
        top=plot_top,
        width=plot_w,
        height=plot_h,
        x_low=ratio_low,
        x_high=ratio_high,
        y_low=0,
        y_high=100,
        x_label="Tool / model proxy ratio (log scale)",
        y_label="Official resolved (%)",
        log_x=True,
        y_ticks=[0, 20, 40, 60, 80, 100],
    )
    for row in aggregates:
        xx = plot_left + _log_position(row["mean_proxy_ratio"], ratio_low, ratio_high) * plot_w
        yy = plot_top + plot_h - row["official_resolved_percent"] / 100.0 * plot_h
        radius = 7 + 1.7 * math.sqrt(row["n_runs"])
        color = colors[row["category"]]
        lines.append(
            f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="{radius:.1f}" fill="{color}" fill-opacity="0.28" stroke="{color}" stroke-width="2">'
            f'<title>{escape(row["category"])} · n={row["n_runs"]} · ratio={row["mean_proxy_ratio"]:.4g} · resolved={row["official_resolved_percent"]:.2f}%</title></circle>'
        )

    # Third panel: the PDF sample view is retained at the historical proxy
    # boundary.  It does not claim native GPU-kernel timing.
    x = panel_xs[2]
    _panel_title(lines, x, top, "Per-sample CPU/GPU latency")
    plot_left, plot_top = x + 70, top + 55
    plot_w, plot_h = panel_w - 95, panel_h - 125
    ratio_low, ratio_high = _bounds((row["proxy_ratio"] for row in rows), log=True)
    e2e_low, e2e_high = 0.0, max(row["observed_e2e_ms"] for row in rows) / 1000.0 * 1.08
    _axis_grid(
        lines,
        left=plot_left,
        top=plot_top,
        width=plot_w,
        height=plot_h,
        x_low=ratio_low,
        x_high=ratio_high,
        y_low=e2e_low,
        y_high=e2e_high,
        x_label="Tool / model proxy ratio (log scale)",
        y_label="Observed outer E2E (s)",
        log_x=True,
    )
    for row in rows:
        xx = plot_left + _log_position(row["proxy_ratio"], ratio_low, ratio_high) * plot_w
        yy = plot_top + plot_h - _linear_position(row["observed_e2e_ms"] / 1000.0, e2e_low, e2e_high) * plot_h
        color = colors[row["category"]]
        lines.append(
            f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="2.8" fill="{color}" fill-opacity="0.52">'
            f'<title>{escape(row["category"])} · {escape(row["instance_id"])} · ratio={row["proxy_ratio"]:.4g}</title></circle>'
        )

    # Full category legend sits below all three panels so it cannot collide
    # with the third panel's axis labels.
    legend_x, legend_y, col_width = 44, 540, 355
    for index, group in enumerate(groups):
        lx = legend_x + (index % 4) * col_width
        ly = legend_y + (index // 4) * 18
        lines.append(f'<circle cx="{lx}" cy="{ly - 4}" r="4.5" fill="{colors[group]}"/>')
        lines.append(_svg_text(lx + 9, ly, _short_label(group, 27), "legend"))
    lines.append(_svg_text(34, height - 78, "Official resolved values are copied unchanged from the identity-gated retained source; no latency simulation changes an outcome label.", "note"))
    lines.append(_svg_text(34, height - 54, "The first two panels use retained category resolution; the third uses historical tool/model proxy walls, not native GPU timing.", "note"))
    lines.append(_svg_text(34, height - 30, "CPU/GPU is the PDF label; this packet expands it to the measured tool/model proxy definition.", "note"))
    return _finish_svg(lines)


def _render_sweep_panel(rows: list[dict[str, Any]], *, title: str, width: int = 1460, height: int = 560) -> str:
    # One parameter: supported E2E tradeoff plus two explicit unavailable views.
    panel_w, gap = 445, 28
    panel_y, panel_h = 94, 390
    xs = [34, 34 + panel_w + gap, 34 + 2 * (panel_w + gap)]
    lines = _svg_header(width, height, title, "Retained matched-pair sweep; first panel is resolved-rate/latency evidence, while missing event-level ratio views stay explicit.")
    settings = sorted(rows, key=lambda row: str(row["setting"]))
    base_x = rows[0]["baseline_mean_e2e_s"]
    base_y = rows[0]["baseline_resolved_rate_percent"]
    x_values = [base_x] + [row["treatment_mean_e2e_s"] for row in rows]
    x_low, x_high = _bounds(x_values)
    x0, y0 = xs[0] + 75, panel_y + 52
    pw, ph = panel_w - 100, panel_h - 112
    _axis_grid(lines, left=x0, top=y0, width=pw, height=ph, x_low=x_low, x_high=x_high, y_low=0, y_high=100, x_label="Average E2E latency (s)", y_label="Resolved rate (%)", y_ticks=[0, 20, 40, 60, 80, 100])
    _panel_title(lines, xs[0], panel_y, "Accuracy–latency tradeoff")
    bx = x0 + _linear_position(base_x, x_low, x_high) * pw
    by = y0 + ph - base_y / 100 * ph
    lines.append(f'<polygon points="{bx:.1f},{by - 8:.1f} {bx + 8:.1f},{by:.1f} {bx:.1f},{by + 8:.1f} {bx - 8:.1f},{by:.1f}" fill="#667085" stroke="#344054" stroke-width="1.5"><title>baseline anchor · {base_x:.3g}s · {base_y:.3g}%</title></polygon>')
    lines.append(_svg_text(bx + 12, by - 7, "baseline", "note"))
    for index, row in enumerate(settings):
        x = x0 + _linear_position(row["treatment_mean_e2e_s"], x_low, x_high) * pw
        y = y0 + ph - row["treatment_resolved_rate_percent"] / 100 * ph
        lines.append(f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{x:.1f}" y2="{y:.1f}" stroke="#98a2b3" stroke-width="1.4" stroke-dasharray="5,4"/>')
        color = PALETTE[index % len(PALETTE)]
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{color}" stroke="#ffffff" stroke-width="1.5"><title>{escape(str(row["setting"]))}: {row["treatment_mean_e2e_s"]:.3g}s, {row["treatment_resolved_rate_percent"]:.3g}% resolved</title></circle>')
        lines.append(_svg_text(x + 9, y - 8, str(row["setting"]), "note", fill=color))
    for panel_index, panel_title in ((1, "CPU/GPU proxy latency view"), (2, "Per-sample CPU/GPU proxy view")):
        x = xs[panel_index]
        lines.append(f'<rect class="frame" x="{x:.1f}" y="{panel_y:.1f}" width="{panel_w:.1f}" height="{panel_h:.1f}"/>')
        _panel_title(lines, x, panel_y, panel_title)
        lines.append(_svg_text(x + panel_w / 2, panel_y + 160, "UNAVAILABLE", "panel-title", text_anchor="middle", fill="#667085"))
        lines.append(_svg_text(x + panel_w / 2, panel_y + 194, "retained sweep table has no matched", "note", text_anchor="middle"))
        lines.append(_svg_text(x + panel_w / 2, panel_y + 213, "tool/model event walls", "note", text_anchor="middle"))
        lines.append(_svg_text(x + panel_w / 2, panel_y + 250, "No CPU/GPU ratio is fabricated.", "note", text_anchor="middle"))
    lines.append(_svg_text(34, height - 36, "Baseline is one within-panel anchor; setting coordinates are 18 matched pairs across 17 instance clusters. Baseline copies are not pooled as independent samples.", "note"))
    lines.append(_svg_text(34, height - 16, "Resolved rate is task outcome evidence from the retained eligible sweep; it is separate from D9 latency-model within-25% coverage.", "note"))
    return _finish_svg(lines)


def _render_step3_breakdown(selected: Mapping[str, Any], breakdown: list[Mapping[str, Any]]) -> str:
    width, height = 1400, 760
    lines = _svg_header(width, height, "D7 · High proxy-ratio trajectory boundary comparison", f"Selected {selected['instance_id']} · ratio={selected['proxy_ratio']:.3g} · run={selected['run_id'][-12:]}; historical proxy boundaries only.")
    # Left: measured boundaries as separate bars.  Tool/model sums are not
    # proven disjoint intervals, so no residual is calculated or stacked into
    # outer E2E.
    left, top, pw, ph = 82, 130, 560, 470
    _axis_grid(lines, left=left, top=top, width=pw, height=ph, x_low=0, x_high=1, y_low=0, y_high=max(1.0, selected["observed_e2e_ms"] / 1000.0 * 1.08), x_label="Observed E2E accounting", y_label="Wall time (s)", y_ticks=[0, selected["observed_e2e_ms"] / 4000.0, selected["observed_e2e_ms"] / 2000.0, selected["observed_e2e_ms"] * 3 / 4000.0, selected["observed_e2e_ms"] / 1000.0], x_ticks=[0.25, 0.5, 0.75], show_x_tick_labels=False)
    _panel_title(lines, left, top - 34, "Observed boundaries (separate totals)")
    observed_boundaries = [
        (selected["observed_e2e_ms"] / 1000.0, "#7a5af8", "outer E2E"),
        (selected["observed_tool_wall_ms"] / 1000.0, TOOL, "tool wall"),
        (selected["observed_gpu_proxy_wall_ms"] / 1000.0, GPU, "model-request proxy"),
    ]
    total = max(1.0, selected["observed_e2e_ms"] / 1000.0 * 1.08)
    bar_w = 105
    for index, (amount, color, label) in enumerate(observed_boundaries):
        x = left + 92 + index * 142
        h = amount / total * ph
        y = top + ph - h
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="{color}" fill-opacity="0.82"><title>{label}: {amount:.3f}s</title></rect>')
        lines.append(_svg_text(x + bar_w / 2, y - 10, f"{amount:.2f}s", "note", text_anchor="middle"))
        lines.append(_svg_text(x + bar_w / 2, top + ph + 26, label, "note", text_anchor="middle"))
    # Right: predictions, intentionally unstacked.
    left2, top2, pw2, ph2 = 770, 130, 560, 470
    max_value = max(selected["predicted_tool_wall_ms"], selected["predicted_gpu_proxy_wall_ms"], selected["predicted_e2e_ms"]) / 1000.0 * 1.15
    _axis_grid(lines, left=left2, top=top2, width=pw2, height=ph2, x_low=0, x_high=1, y_low=0, y_high=max_value, x_label="Saved OOF predictions (separate targets)", y_label="Predicted wall time (s)", y_ticks=[max_value * i / 5 for i in range(6)], x_ticks=[0.25, 0.5, 0.75], show_x_tick_labels=False)
    _panel_title(lines, left2, top2 - 34, "Predicted targets (not a composition)")
    preds = [
        (selected["predicted_tool_wall_ms"] / 1000.0, TOOL, "CPU sum"),
        (selected["predicted_gpu_proxy_wall_ms"] / 1000.0, GPU, "GPU proxy sum"),
        (selected["predicted_e2e_ms"] / 1000.0, "#7a5af8", "direct E2E"),
    ]
    bar_w = 92
    for index, (value, color, label) in enumerate(preds):
        x = left2 + 105 + index * 140
        h = value / max_value * ph2
        y = top2 + ph2 - h
        lines.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="{color}" fill-opacity="0.82"><title>{label}: {value:.3f}s</title></rect>')
        lines.append(_svg_text(x + bar_w / 2, y - 10, f"{value:.2f}s", "note", text_anchor="middle"))
        lines.append(_svg_text(x + bar_w / 2, top2 + ph2 + 26, label, "note", text_anchor="middle"))
    lines.append(_svg_text(34, height - 40, "Tool wall, completed-request proxy, and outer E2E are separate historical boundaries. Their interval disjointness is not proven, so no residual or stacked decomposition is shown.", "note"))
    lines.append(_svg_text(34, height - 18, "Predicted CPU/GPU sums are component diagnostics; the direct E2E prediction is a separate target and is never stacked with them.", "note"))
    return _finish_svg(lines)


def _render_step3_events(event_log: list[Mapping[str, Any]], selected: Mapping[str, Any]) -> str:
    width, height = 1560, 840
    lines = _svg_header(width, height, "D7/D8 · Event log for the highest retained proxy-ratio trajectory", f"{selected['instance_id']} · {sum(row['event_kind'] == 'cpu_tool_wall_proxy' for row in event_log)} CPU tool events + {sum(row['event_kind'] == 'gpu_request_proxy_wall' for row in event_log)} completed model requests; no native phase decomposition claimed.")
    cpu = [row for row in event_log if row["event_kind"] == "cpu_tool_wall_proxy"]
    gpu = [row for row in event_log if row["event_kind"] == "gpu_request_proxy_wall"]
    panels = [(46, 110, 470, 520), (545, 110, 470, 520), (1044, 110, 470, 520)]
    # CPU event panel.
    left, top, pw, ph = panels[0]
    cpu_max = max(max(row["observed_ms"], row["predicted_ms"]) for row in cpu) * 1.18
    _axis_grid(lines, left=left + 60, top=top + 44, width=pw - 85, height=ph - 120, x_low=0, x_high=max(1, len(cpu)), y_low=0, y_high=cpu_max, x_label="CPU event ordinal", y_label="Wall time (ms)", y_ticks=[cpu_max * i / 4 for i in range(5)], x_ticks=[float(i) for i in range(1, len(cpu) + 1)])
    _panel_title(lines, left, top, "CPU tool event wall")
    plot_left, plot_top = left + 60, top + 44
    plot_w, plot_h = pw - 85, ph - 120
    for index, row in enumerate(cpu, 1):
        x = plot_left + (index - 0.5) / max(1, len(cpu)) * plot_w
        obs_h = row["observed_ms"] / cpu_max * plot_h
        pred_h = row["predicted_ms"] / cpu_max * plot_h
        lines.append(f'<rect x="{x - 13:.1f}" y="{plot_top + plot_h - obs_h:.1f}" width="11" height="{obs_h:.1f}" fill="{TOOL}" fill-opacity="0.72"><title>{escape(row["operation_class"])} observed {row["observed_ms"]:.3f}ms</title></rect>')
        lines.append(f'<rect x="{x + 2:.1f}" y="{plot_top + plot_h - pred_h:.1f}" width="11" height="{pred_h:.1f}" fill="#7a5af8" fill-opacity="0.72"><title>{escape(row["operation_class"])} predicted {row["predicted_ms"]:.3f}ms</title></rect>')
    lines.append(_svg_text(left + 68, top + ph - 24, "orange observed · purple saved OOF prediction", "note"))
    # GPU proxy panel.
    left, top, pw, ph = panels[1]
    gpu_max = max(max(row["observed_ms"], row["predicted_ms"]) for row in gpu) * 1.18
    _axis_grid(lines, left=left + 60, top=top + 44, width=pw - 85, height=ph - 120, x_low=0, x_high=max(1, len(gpu)), y_low=0, y_high=gpu_max, x_label="Model request ordinal", y_label="Proxy wall (ms)", y_ticks=[gpu_max * i / 4 for i in range(5)], x_ticks=[float(i) for i in range(1, len(gpu) + 1)])
    _panel_title(lines, left, top, "GPU request proxy wall")
    plot_left, plot_top = left + 60, top + 44
    plot_w, plot_h = pw - 85, ph - 120
    for index, row in enumerate(gpu, 1):
        x = plot_left + (index - 0.5) / max(1, len(gpu)) * plot_w
        obs_h = row["observed_ms"] / gpu_max * plot_h
        pred_h = row["predicted_ms"] / gpu_max * plot_h
        lines.append(f'<rect x="{x - 13:.1f}" y="{plot_top + plot_h - obs_h:.1f}" width="11" height="{obs_h:.1f}" fill="{GPU}" fill-opacity="0.72"><title>request {index} observed {row["observed_ms"]:.3f}ms</title></rect>')
        lines.append(f'<rect x="{x + 2:.1f}" y="{plot_top + plot_h - pred_h:.1f}" width="11" height="{pred_h:.1f}" fill="#7a5af8" fill-opacity="0.72"><title>request {index} predicted {row["predicted_ms"]:.3f}ms</title></rect>')
    lines.append(_svg_text(left + 68, top + ph - 24, "blue observed · purple saved OOF prediction", "note"))
    # Token descriptor panel.
    left, top, pw, ph = panels[2]
    token_values = [row["input_tokens"] for row in gpu] + [row["output_tokens"] for row in gpu] + [row["context_tokens"] for row in gpu]
    token_max = max(token_values) * 1.12
    _axis_grid(lines, left=left + 60, top=top + 44, width=pw - 85, height=ph - 120, x_low=0, x_high=max(1, len(gpu)), y_low=0, y_high=token_max, x_label="Model request ordinal", y_label="Logged tokens", y_ticks=[token_max * i / 4 for i in range(5)], x_ticks=[float(i) for i in range(1, len(gpu) + 1)])
    _panel_title(lines, left, top, "GPU workload descriptors")
    plot_left, plot_top = left + 60, top + 44
    plot_w, plot_h = pw - 85, ph - 120
    token_series = [("input", "#1769aa", "input_tokens"), ("output", "#d97706", "output_tokens"), ("context", "#18794e", "context_tokens")]
    for label, color, key in token_series:
        points = []
        for index, row in enumerate(gpu, 1):
            x = plot_left + (index - 0.5) / max(1, len(gpu)) * plot_w
            y = plot_top + plot_h - row[key] / token_max * plot_h
            points.append(f"{x:.1f},{y:.1f}")
            lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"><title>{label} tokens request {index}: {row[key]}</title></circle>')
        lines.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
    for index, (label, color, _key) in enumerate(token_series):
        x = left + 78 + index * 105
        y = top + ph - 24
        lines.append(f'<circle cx="{x}" cy="{y - 4}" r="4" fill="{color}"/>')
        lines.append(_svg_text(x + 8, y, label, "note"))
    lines.append(_svg_text(34, height - 46, "The GPU panel is a historical completed-request proxy. It records input/output/context descriptors but does not claim native prefill/decode timing.", "note"))
    lines.append(_svg_text(34, height - 24, "Use the accompanying CSV for the complete event log; the figure does not hide event rows or infer missing CPU operation classes.", "note"))
    return _finish_svg(lines)


def _render_native_quality(native_rows: list[Mapping[str, Any]]) -> str:
    width, height = 1420, 700
    lines = _svg_header(width, height, "Native GPU request E2E OOF quality", "Direct native:e2e requests only · 2,080 train-calibration requests · five instance-grouped folds · token/cache candidates are conditional replay.")
    for panel_index, candidate in enumerate(("relative_nnls_token", "relative_nnls_token_cache")):
        left, top, pw, ph = 70 + panel_index * 680, 100, 545, 470
        values = [(float(row["observed_ms"]["e2e"]), float(row["predictions_ms"][candidate]["e2e"])) for row in native_rows]
        low = min(min(pair) for pair in values) * 0.8
        high = max(max(pair) for pair in values) * 1.15
        _axis_grid(lines, left=left + 72, top=top + 42, width=pw - 100, height=ph - 110, x_low=low, x_high=high, y_low=low, y_high=high, x_label="Observed native:e2e (ms)", y_label="Predicted native:e2e (ms)")
        _panel_title(lines, left, top, "Token candidate" if candidate.endswith("token") else "Cache-trace candidate")
        plot_left, plot_top = left + 72, top + 42
        plot_w, plot_h = pw - 100, ph - 110
        def pos_x(v: float) -> float: return plot_left + _linear_position(v, low, high) * plot_w
        def pos_y(v: float) -> float: return plot_top + plot_h - _linear_position(v, low, high) * plot_h
        lines.append(f'<line x1="{pos_x(low):.1f}" y1="{pos_y(low):.1f}" x2="{pos_x(high):.1f}" y2="{pos_y(high):.1f}" stroke="#344054" stroke-width="2"/>')
        # 25% bands, shown as lines only to keep the 2,080 points legible.
        for scale, color, label in ((0.75, "#b42318", "-25%"), (1.25, "#b42318", "+25%")):
            points = []
            for observed in (low, high):
                predicted = observed * scale
                if low <= predicted <= high:
                    points.append(f"{pos_x(observed):.1f},{pos_y(predicted):.1f}")
            if len(points) == 2:
                lines.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="1.5" stroke-dasharray="6,4"/>')
        for observed, predicted in values:
            within = _ape(observed, predicted) <= 25.0
            color = PASS if within else FAIL
            lines.append(f'<circle cx="{pos_x(observed):.1f}" cy="{pos_y(predicted):.1f}" r="2.3" fill="{color}" fill-opacity="0.42"><title>observed={observed:.3f}ms predicted={predicted:.3f}ms APE={_ape(observed, predicted):.3g}%</title></circle>')
        lines.append(_svg_text(plot_left + 12, plot_top + 20, "green ≤25% · red >25%", "note"))
    lines.append(_svg_text(34, height - 44, "Primary native target is direct request E2E. Queue/prefill/decode rows are diagnostics and are not summed here.", "note"))
    lines.append(_svg_text(34, height - 22, "Cross-hardware transfer is unvalidated; cache points use a supplied realized cache trace.", "note"))
    return _finish_svg(lines)


def _write_inputs(out_dir: Path, data: Mapping[str, Any]) -> None:
    ratio_columns = ["run_id", "instance_id", "category", "official_resolved", "outcome_match_mode", "outer_fold", "observed_tool_wall_ms", "observed_gpu_proxy_wall_ms", "observed_e2e_ms", "proxy_ratio", "e2e_model_within25", "predicted_tool_wall_ms", "predicted_gpu_proxy_wall_ms", "predicted_e2e_ms"]
    _write_csv(out_dir / "d2_ratio_rows.csv", data["ratio_rows"], ratio_columns)
    _write_csv(out_dir / "d3_ratio_rows_exact_outcome_join.csv", data["d3_rows"], ratio_columns)
    category_columns = ["category", "n_runs", "n_instances", "official_resolved_n", "official_resolved_percent", "mean_observed_e2e_s", "mean_proxy_ratio", "e2e_model_within25_percent", "observed_tool_wall_s", "observed_gpu_proxy_wall_s"]
    _write_csv(out_dir / "d3_category_aggregates.csv", data["category_aggregates"], category_columns)
    outcome_columns = ["run_id", "instance_id", "category", "official_resolved", "match_mode", "source_run_id", "source_instance_id", "source_config_id"]
    _write_csv(out_dir / "d3_outcome_join.csv", data["outcome_join"], outcome_columns)
    sweep_columns = ["parameter", "setting", "n_pairs", "n_instance_clusters", "baseline_mean_e2e_s", "treatment_mean_e2e_s", "baseline_resolved_rate_percent", "treatment_resolved_rate_percent", "baseline_resolved_n", "treatment_resolved_n"]
    _write_csv(out_dir / "step2_tradeoff_rows.csv", data["sweep_data"], sweep_columns)
    event_columns = ["event_kind", "event_index", "event_id", "operation_class", "request_id", "input_tokens", "output_tokens", "context_tokens", "observed_ms", "predicted_ms", "within25", "boundary"]
    _write_csv(out_dir / "step3_event_log.csv", data["event_log"], event_columns)
    _write_csv(out_dir / "step3_breakdown.csv", data["breakdown"], ["boundary", "value_ms", "interpretation"])
    _write_json(out_dir / "native_oof_metrics.json", data["native_metrics"])


def _write_d3_join_manifest(out_dir: Path, data: Mapping[str, Any]) -> None:
    join_path = out_dir / "d3_outcome_join.csv"
    source = Path(data["outcome_source"])
    source_path = str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source)
    train_path = SOURCE_PATHS["trajectories"]
    train_path_text = str(train_path.relative_to(ROOT)) if train_path.is_relative_to(ROOT) else str(train_path)
    manifest = {
        "schema": "d9-salvage-d3-outcome-join.v1",
        "join_contract": "exact_run_id_only",
        "source_path": source_path,
        "source_sha256": data["outcome_source_metadata"]["source_sha256"],
        "training_trajectories_path": train_path_text,
        "training_trajectories_sha256": _sha256(train_path),
        "scope_manifest_list_sha256": data["outcome_source_metadata"]["scope_manifest_list_sha256"],
        "train_run_count": len(data["ratio_rows"]),
        "matched_run_count": len(data["outcome_join"]),
        "unmatched_run_count": data["outcome_match_counts"]["unmatched_train_run_id"],
        "row_count": len(data["outcome_join"]),
        "match_counts": data["outcome_match_counts"],
        "label_semantics": "official_resolved copied from retained historical figure inputs after exact run-ID identity gate; no instance fallback",
    }
    # The compact join is written before this manifest so its hash can be
    # checked when the external source is absent on replay.
    manifest["join_sha256"] = _sha256(join_path)
    _write_json(out_dir / "d3_outcome_join_manifest.json", manifest)


def _write_report(out_dir: Path, data: Mapping[str, Any], outputs: list[str]) -> None:
    selected = data["selected"]
    native = data["native_metrics"]
    lines = [
        "# Bounded D9 Steps 1–3 figure packet",
        "",
        "This packet renders saved train-calibration and out-of-fold artifacts. It does not fit a model, open excluded evaluation labels, or regenerate a broad raw dataset.",
        "",
        "## Scope and numerical checks",
        "",
        f"- Historical proxy view: {len(data['ratio_rows'])} retained train-calibration trajectories and {len(data['category_aggregates'])} retained category groups.",
        f"- D2 ratio: one dot per retained run, summed recorded tool-event wall divided by observed completed model-request proxy wall. It is not a CPU/GPU hardware ratio.",
        f"- D3 accuracy view: {len(data['d3_rows'])}/{len(data['ratio_rows'])} trajectories have exact run-ID matches to the retained outcome source; official resolved labels are plotted only for that subset. The {data['outcome_match_counts']['unmatched_train_run_id']} unmatched runs are excluded, with no instance-level fallback.",
        f"- D3 category resolution: {sum(row['official_resolved_n'] for row in data['category_aggregates'])}/{len(data['d3_rows'])} exact-matched runs resolved ({100.0 * sum(row['official_resolved_n'] for row in data['category_aggregates']) / len(data['d3_rows']):.3f}%). The latency simulation does not modify these labels.",
        "- Historical direct E2E values in the joint OOF table exactly cross-check against the retained train-view observed E2E field for all 819 run IDs.",
        f"- Step 2: 12 retained matched-pair rows, three settings for each of call limit, max output tokens, observation length, and temperature; every row has 18 pairs across 17 instance clusters.",
        f"- Step 3 selected run: `{selected['instance_id']}` with observed tool/model proxy ratio `{selected['proxy_ratio']:.6g}`; `{sum(row['event_kind'] == 'cpu_tool_wall_proxy' for row in data['event_log'])}` CPU tool rows and `{sum(row['event_kind'] == 'gpu_request_proxy_wall' for row in data['event_log'])}` model-request rows were joined to saved OOF predictions.",
        f"- Native direct request OOF: token candidate `{native['relative_nnls_token']['within25_percent']:.6f}%` within 25% and cache-trace candidate `{native['relative_nnls_token_cache']['within25_percent']:.6f}%`; both recomputed metrics match `native/report.json`.",
        "",
        "## Exact source hash anchors",
        "",
        *[
            f"- `{key}`: `{_sha256(SOURCE_PATHS[key])}`"
            for key in (
                "training_manifest",
                "trajectories",
                "historical_joint_predictions",
                "cpu_oof_predictions",
                "gpu_proxy_oof_predictions",
                "sweep_summary",
                "native_predictions",
            "native_report",
            "native_fit_artifact",
        )
        ],
        f"- `historical_outcome_source`: `{data['outcome_source']}`; `{data['outcome_source_metadata']['source_sha256']}`",
        f"- `d3_outcome_join`: `{_sha256(out_dir / 'd3_outcome_join.csv') if (out_dir / 'd3_outcome_join.csv').is_file() else 'written with the packet'}`",
        f"- `d3_scope_manifest_list`: `{data['outcome_source_metadata']['scope_manifest_list_sha256']}`",
        "",
        "## Figure inventory",
        "",
    ]
    lines.extend(f"- `{name}`" for name in sorted(outputs))
    lines.extend(
        [
            "",
            "## Exact PDF plot gaps",
            "",
            "- Step 1/D2 is supported only at the historical tool/model proxy boundary. The saved train view has no native GPU-kernel timing or independent category label, so repository is shown as the category-like grouping.",
            f"- Step 1/D3 is supported on the exact run-ID outcome join ({len(data['d3_rows'])} plotted runs; {data['outcome_match_counts']['unmatched_train_run_id']} train-view runs omitted because no exact outcome row is retained). The first two panels use official resolved rate; the third uses the historical tool/model proxy boundary. The separate D9 diagnostic remains model coverage, not task accuracy.",
            "- Step 2/D4 and D5 have resolved-rate versus average E2E points from the retained matched-pair summary. The PDF's CPU/GPU-latency panels are explicit unavailable panels because the retained sweep summary does not contain matched tool/model event walls; no ratio is fabricated.",
            "- Step 3/D7 and D8 have a complete historical proxy event log for one high-ratio trajectory, including operation classes and input/output/context descriptors. Native queue/prefill/decode and atomic CPU operation rows are not silently substituted into that breakdown.",
            "- No figure adds a residual to an E2E prediction or sums native E2E with native phase rows. Tool wall, model proxy wall, and outer E2E are shown as separate boundaries because their interval disjointness is not proven.",
            "- Accuracy labels are copied from the identity-gated retained source and are never recomputed from, or changed by, latency simulation.",
            "",
            "The packet is evidence for bounded simulator review, not a literal D9 acceptance result. Cross-hardware transfer and the all-event within-25% gate remain unproven.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(output_dir: Path) -> dict[str, Any]:
    data = _load_data()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Remove only artifacts from the immediately previous renderer naming; all
    # source evidence and unrelated review files remain untouched.
    for stale_name in (
        "d3_category_tradeoffs.svg",
        "d3_category_tradeoffs.png",
        "d3_category_tradeoffs_supported_and_gaps.svg",
        "d3_category_tradeoffs_supported_and_gaps.png",
    ):
        stale_path = output_dir / stale_name
        if stale_path.is_file():
            stale_path.unlink()
    _write_inputs(output_dir, data)
    _write_d3_join_manifest(output_dir, data)
    outputs: list[str] = []
    outputs += _write_svg_and_png(output_dir, "d2_cpu_gpu_proxy_ratio_by_repository", _render_d2(data["ratio_rows"]))
    outputs += _write_svg_and_png(output_dir, "d3_category_tradeoffs", _render_d3(data["category_aggregates"], data["d3_rows"]))
    outputs += _write_svg_and_png(output_dir, "d9_latency_model_coverage_diagnostic", _render_d9_diagnostic(data["category_aggregates"], data["ratio_rows"]))
    outputs += _write_svg_and_png(output_dir, "step3_high_ratio_breakdown", _render_step3_breakdown(data["selected"], data["breakdown"]))
    outputs += _write_svg_and_png(output_dir, "step3_high_ratio_events", _render_step3_events(data["event_log"], data["selected"]))
    outputs += _write_svg_and_png(output_dir, "native_request_oof_quality", _render_native_quality(data["native_rows"]))

    parameters = sorted({row["parameter"] for row in data["sweep_data"]})
    for parameter in parameters:
        rows = [row for row in data["sweep_data"] if row["parameter"] == parameter]
        outputs += _write_svg_and_png(output_dir, f"step2_{parameter}", _render_sweep_panel(rows, title=f"Step 2 · {parameter.replace('_', ' ').title()} sweep"))
    combined_lines = _svg_header(2250, 1320, "Step 2 · Four-hyperparameter accuracy–latency sweep", "Each row preserves the PDF's three-view layout: supported resolved-rate/E2E points, then explicit CPU/GPU proxy gaps.")
    for index, parameter in enumerate(parameters):
        row_svg = _render_sweep_panel([row for row in data["sweep_data"] if row["parameter"] == parameter], title=parameter.replace("_", " ").title(), width=1460, height=560)
        # Embed the body of the standalone SVG into a compact combined canvas.
        body = row_svg[row_svg.find("</style>") + len("</style>"):]
        body = body[: body.rfind("</svg>")]
        scale = 0.72
        tx = 15 + (index % 2) * 1125
        ty = 78 + (index // 2) * 620
        combined_lines.append(f'<g transform="translate({tx},{ty}) scale({scale})">{body}</g>')
    combined_lines.append(_svg_text(34, 1304, "Only the first view has retained values; missing event-level CPU/GPU sweep data is shown as unavailable rather than inferred.", "note"))
    outputs += _write_svg_and_png(output_dir, "step2_hyperparameter_tradeoffs", _finish_svg(combined_lines))

    outputs = sorted(set(outputs))
    _write_report(output_dir, data, outputs)
    source_records = {
        key: {
            "path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            "sha256": _sha256(path),
        }
        for key, path in SOURCE_PATHS.items()
    }
    source_records["historical_outcome_source"] = {
        "path": str(data["outcome_source"]),
        "sha256": data["outcome_source_metadata"]["source_sha256"],
    }
    checks = {
        "historical_trajectories": len(data["ratio_rows"]),
        "historical_categories": len(data["category_aggregates"]),
        "d2_ratio_points": len(data["ratio_rows"]),
        "d3_exact_outcome_points": len(data["d3_rows"]),
        "d3_unmatched_train_runs": data["outcome_match_counts"]["unmatched_train_run_id"],
        "d3_official_resolved_n": sum(row["official_resolved_n"] for row in data["category_aggregates"]),
        "historical_e2e_boundary_crosscheck": True,
        "step2_rows": len(data["sweep_data"]),
        "step3_event_rows": len(data["event_log"]),
        "native_request_rows": len(data["native_rows"]),
        "native_missing_prediction_rows": 0,
        "selected_high_ratio_run": data["selected"]["run_id"],
        "selected_high_ratio": data["selected"]["proxy_ratio"],
        "no_component_rows_added_to_direct_e2e": True,
    }
    provenance = {
        "schema": "d9-salvage-figures.v1",
        "status": "bounded_saved_artifact_render",
        "source_scope": "retained train_calibration and saved grouped OOF artifacts; outcome labels admitted only by exact train run ID; excluded evaluation labels unopened",
        "accuracy_policy": "official_resolved values copied unchanged from the exact-run identity join; latency simulation never writes or infers accuracy",
        "cohort": {
            "partition": "train_calibration",
            "historical_trajectories": 819,
            "historical_instances": 545,
            "d3_exact_outcome_runs": len(data["d3_rows"]),
            "d3_unmatched_train_runs": data["outcome_match_counts"]["unmatched_train_run_id"],
            "native_requests": 2080,
            "native_instances": data["native_report"].get("population", {}).get("instances"),
            "training_manifest_output_hashes": data["manifest"].get("output_hashes", {}),
        },
        "historical_boundary": "tool wall and completed model-request proxy wall; not native GPU timing",
        "d3_outcome_join": {
            "contract": "exact_run_id_only",
            "source_sha256": data["outcome_source_metadata"]["source_sha256"],
            "scope_manifest_list_sha256": data["outcome_source_metadata"]["scope_manifest_list_sha256"],
            "match_counts": data["outcome_match_counts"],
        },
        "native_boundary": "direct native:e2e request OOF; queue/prefill/decode diagnostics not summed",
        "source_records": source_records,
        "checks": checks,
    }
    _write_json(output_dir / "provenance.json", provenance)
    inventory = {
        "schema": "d9-salvage-figure-inventory.v1",
        "figures": [
            {"name": name, "sha256": _sha256(output_dir / name), "bytes": (output_dir / name).stat().st_size}
            for name in outputs
            if (output_dir / name).is_file()
        ],
        "inputs": [
            name
            for name in ("d2_ratio_rows.csv", "d3_ratio_rows_exact_outcome_join.csv", "d3_outcome_join.csv", "d3_outcome_join_manifest.json", "d3_category_aggregates.csv", "step2_tradeoff_rows.csv", "step3_event_log.csv", "step3_breakdown.csv", "native_oof_metrics.json")
        ],
    }
    _write_json(output_dir / "figure_manifest.json", inventory)
    return {"outputs": outputs, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE)
    args = parser.parse_args(argv)
    result = build(args.output_dir.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
