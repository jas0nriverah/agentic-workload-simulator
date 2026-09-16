#!/usr/bin/env python3
"""Compare bounded native GPU phase models on repaired D9 evidence.

The primary target is the measured ``native:e2e`` request event.  Queue,
prefill, and decode are reported as separate diagnostics and are never added to
native E2E.  All candidates use five folds grouped by the 25 repaired instance
IDs; repeated executions of one instance stay together.

The token candidates are explicitly conditional on a supplied realized native
workload trace.  Prompt, completion, maximum-output, and optional cached-token
counts are descriptors of that trace, not prospective inputs to a future
request.  No measured latency, residual, status, or outcome field enters any
design row.  The NNLS implementation is reused from the dependency-free D9 E2E
comparison module.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
DEFAULT_DATASET = HERE.parent / "evidence" / "native_phase_dataset.jsonl"
DEFAULT_MANIFEST = HERE.parent / "evidence" / "calibration_input_manifest.json"
DEFAULT_OUTPUT = HERE
COMPARE_PATH = HERE.parent / "e2e" / "compare.py"

EXPECTED_DATASET_SHA256 = "f54ebb7b387bb212475295e2e3dc171c169fc52c8c73624fba9b9661b50d41e0"
EXPECTED_MANIFEST_SHA256 = "8c4f3a156d90e2d4b6f4c608f49dde647719096f9c059c5889c65a3598588afd"
FOLD_PREFIX = "assignment.d9.native-fold-v1:"
FOLDS = 5
MIN_EVENTS = 25
MIN_INSTANCES = 3
WITHIN_THRESHOLD_PERCENT = 25.0
PHASES = ("e2e", "queue", "prefill", "decode")
CANDIDATES = (
    "hardware_domain_median",
    "relative_nnls_token",
    "relative_nnls_token_cache",
)
# The first bounded pass used log1p token features.  Preserve its primary and
# diagnostic results as a rejected design-review record after the scaled-linear
# correction; it is not one of the fixed candidates below.
REJECTED_LOG_DIAGNOSTIC = {
    "schema": "d9.native-rejected-log-diagnostic.v1",
    "candidate": "rejected_log1p_token_regression",
    "status": "rejected_after_design_review",
    "features": ["intercept", "log1p(prompt_tokens)", "log1p(completion_tokens)", "log1p(max_output_tokens)"],
    "primary_target": "native:e2e",
    "primary_metrics": {
        "hardware_domain_median_baseline_within25_percent": 21.057692307692307,
        "relative_log_token_within25_percent": 32.5,
        "relative_log_token_mean_ape_percent": 47.14388932326342,
        "relative_log_token_median_ape_percent": 40.156396361256924,
        "relative_log_token_p95_ape_percent": 93.804866299661,
        "relative_log_token_worst_ape_percent": 145.66596812516704,
        "relative_log_token_runs_all_requests_within25": 0,
    },
    "diagnostic_metrics": {
        "queue_within25_percent": 41.77884615384615,
        "prefill_within25_percent": 39.71153846153846,
        "decode_within25_percent": 29.807692307692307,
    },
    "reason": "log1p features underfit the near-linear completion-token workload and were replaced before the final fixed comparison",
    "correction": "scaled nonnegative linear token design; cache candidate uses uncached prompt and prompt-completion interaction with phase-specific prefill/decode designs",
}
TOKEN_FEATURE_NAMES = (
    "intercept",
    "prompt_tokens_div_1000",
    "completion_tokens_div_1000",
)


class ValidationError(ValueError):
    """Input evidence or a generated artifact violates the comparison contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_compare_module() -> Any:
    spec = importlib.util.spec_from_file_location("d9_e2e_compare_dependency", COMPARE_PATH)
    if spec is None or spec.loader is None:
        raise ValidationError(f"cannot load NNLS dependency: {COMPARE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMPARE = load_compare_module()


def fold_for_instance(instance_id: str) -> int:
    digest = hashlib.sha256((FOLD_PREFIX + instance_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % FOLDS


def finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValidationError(f"{field} must be finite and nonnegative")
    return number


def finite_target(value: Any, field: str) -> float:
    # Zero is a valid target.  Metrics handle it by exact equality rather than
    # adding an epsilon to the denominator.
    return finite_nonnegative(value, field)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"JSON object required: {path}")
    return value


def design(row: Mapping[str, Any], candidate: str, phase: str = "e2e") -> list[float]:
    """Return an allowed token design; labels and timing fields are ignored."""
    if candidate == "relative_nnls_token":
        prompt = finite_nonnegative(row.get("prompt_tokens"), "prompt_tokens")
        completion = finite_nonnegative(row.get("completion_tokens"), "completion_tokens")
        return [1.0, prompt / 1000.0, completion / 1000.0]
    if candidate == "relative_nnls_token_cache":
        prompt = finite_nonnegative(row.get("prompt_tokens"), "prompt_tokens")
        completion = finite_nonnegative(row.get("completion_tokens"), "completion_tokens")
        cached = finite_nonnegative(row.get("cached_tokens"), "cached_tokens")
        if cached > prompt:
            raise ValidationError("cached_tokens cannot exceed prompt_tokens")
        uncached_prompt = prompt - cached
        if phase == "prefill":
            # Prefill is conditioned on uncached work and declared context.
            return [1.0, uncached_prompt / 1000.0, prompt / 1000.0]
        if phase == "decode":
            # Decode is conditioned on generated work and its prompt/context
            # interaction; cached prompt work is represented through context.
            return [1.0, completion / 1000.0, prompt * completion / 1_000_000.0]
        return [1.0, uncached_prompt / 1000.0, completion / 1000.0, prompt * completion / 1_000_000.0]
    raise ValidationError(f"design is undefined for {candidate}")


def feature_names(candidate: str, phase: str = "e2e") -> list[str]:
    if candidate == "relative_nnls_token":
        return list(TOKEN_FEATURE_NAMES)
    if candidate == "relative_nnls_token_cache":
        if phase == "prefill":
            return ["intercept", "uncached_prompt_tokens_div_1000", "prompt_tokens_div_1000"]
        if phase == "decode":
            return ["intercept", "completion_tokens_div_1000", "prompt_completion_tokens_div_1e6"]
        return [
            "intercept",
            "uncached_prompt_tokens_div_1000",
            "completion_tokens_div_1000",
            "prompt_completion_tokens_div_1e6",
        ]
    return []


def _request_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row["case_id"]), str(row["instance_id"]), str(row["physical_request_id"]))


def load_dataset(dataset_path: Path, manifest_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
    dataset_sha = sha256_file(dataset_path)
    manifest_sha = sha256_file(manifest_path)
    if dataset_sha != EXPECTED_DATASET_SHA256:
        raise ValidationError(f"verified native dataset SHA mismatch: {dataset_sha}")
    if manifest_sha != EXPECTED_MANIFEST_SHA256:
        raise ValidationError(f"verified native manifest SHA mismatch: {manifest_sha}")
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != "d9.calibration-input-manifest.v2":
        raise ValidationError("unexpected calibration input manifest schema")
    if manifest.get("native_phase_dataset_sha256") != dataset_sha:
        raise ValidationError("manifest does not bind the native dataset SHA")
    expected = manifest.get("aggregate", {})
    if expected.get("native_phase_rows") != 8320 or expected.get("native_physical_requests") != 2080:
        raise ValidationError("manifest aggregate is not the verified 8320-row/2080-request dataset")

    rows: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"invalid native dataset JSON at line {line_number}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ValidationError(f"native dataset row {line_number} is not an object")
            if raw.get("schema") != "d9.native-phase-fit-row.v1":
                raise ValidationError(f"unexpected native row schema at line {line_number}")
            if raw.get("partition") != "train_calibration" or raw.get("record_role") != "TARGET":
                raise ValidationError(f"native row {line_number} is outside train TARGET scope")
            phase_value = raw.get("native_component")
            if phase_value not in PHASES:
                raise ValidationError(f"native row {line_number} has unsupported phase")
            if raw.get("event_class") != "native:" + phase_value:
                raise ValidationError(f"native row {line_number} has inconsistent event class")
            for key in ("instance_id", "case_id", "physical_request_id", "event_id", "hardware_domain"):
                if not isinstance(raw.get(key), str) or not raw[key]:
                    raise ValidationError(f"native row {line_number} lacks {key}")
            raw["observed_ms"] = finite_target(raw.get("observed_ms"), f"observed_ms line {line_number}")
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "max_output_tokens"):
                raw[key] = finite_nonnegative(raw.get(key), f"{key} line {line_number}")
            if raw["cached_tokens"] > raw["prompt_tokens"]:
                raise ValidationError(f"cached_tokens exceeds prompt_tokens at line {line_number}")
            if raw.get("token_provenance") != "native_finished_request_post_event":
                raise ValidationError(f"native row {line_number} has unbound token provenance")
            if raw.get("prediction_feature_status") != "excluded_post_event_token_label":
                raise ValidationError(f"native row {line_number} does not exclude token labels from prospective features")
            rows.append(raw)
    if len(rows) != 8320:
        raise ValidationError(f"expected 8320 native rows, got {len(rows)}")
    return rows, manifest, {"dataset_sha256": dataset_sha, "manifest_sha256": manifest_sha}


def group_requests(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicate_cells: list[dict[str, Any]] = []
    for row in rows:
        key = _request_key(row)
        record = grouped.setdefault(
            key,
            {
                "case_id": row["case_id"],
                "instance_id": row["instance_id"],
                "physical_request_id": row["physical_request_id"],
                "queue_ordinal": row.get("queue_ordinal"),
                "hardware_domain": row["hardware_domain"],
                "tokens": {
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "cached_tokens": row["cached_tokens"],
                    "max_output_tokens": row["max_output_tokens"],
                },
                "observed_ms": {},
                "rows": {},
                "fold": fold_for_instance(str(row["instance_id"])),
                "missing_source": [],
            },
        )
        phase = row["native_component"]
        if phase in record["rows"]:
            duplicate_cells.append({"request": list(key), "phase": phase, "event_id": row.get("event_id")})
        record["rows"][phase] = row
        record["observed_ms"][phase] = row["observed_ms"]
        if record["hardware_domain"] != row["hardware_domain"]:
            record["missing_source"].append("hardware_domain_changed_within_request")
        for field, value in record["tokens"].items():
            if value != row[field]:
                record["missing_source"].append(f"{field}_changed_within_request")
    requests = sorted(grouped.values(), key=lambda item: (str(item["case_id"]), str(item["physical_request_id"])))
    missing_phase_requests = []
    for record in requests:
        missing = [phase for phase in PHASES if phase not in record["rows"]]
        if missing:
            record["missing_source"].append("missing_phase:" + ",".join(missing))
            missing_phase_requests.append({
                "case_id": record["case_id"],
                "instance_id": record["instance_id"],
                "physical_request_id": record["physical_request_id"],
                "missing_phases": missing,
            })
    report = {
        "expected_requests": 2080,
        "request_groups": len(requests),
        "duplicate_phase_cells": duplicate_cells,
        "missing_phase_requests": missing_phase_requests,
        "request_source_defect_count": sum(bool(record["missing_source"]) for record in requests),
    }
    return requests, report


def _supported(rows: list[dict[str, Any]]) -> bool:
    return len(rows) >= MIN_EVENTS and len({str(row["instance_id"]) for row in rows}) >= MIN_INSTANCES


def _rows_for_phase_domain(rows: Iterable[dict[str, Any]], phase: str, domain: str | None) -> list[dict[str, Any]]:
    return [row for row in rows if row["native_component"] == phase and (domain is None or row["hardware_domain"] == domain)]


def _fit_regression(rows: list[dict[str, Any]], candidate: str, phase: str = "e2e") -> dict[str, Any]:
    if candidate not in {"relative_nnls_token", "relative_nnls_token_cache"}:
        raise ValidationError(f"not a regression candidate: {candidate}")
    positive = [row for row in rows if row["observed_ms"] > 0]
    zero_targets = len(rows) - len(positive)
    if not positive:
        return {
            "status": "unsupported_no_positive_training_targets",
            "candidate": candidate,
            "kind": "relative_nonnegative_nnls",
            "feature_names": feature_names(candidate, phase),
            "zero_training_target_count": zero_targets,
        }
    x = [design(row, candidate, phase) for row in positive]
    y = [float(row["observed_ms"]) for row in positive]
    coefficients = [float(value) for value in COMPARE.fit(x, y, relative=True)]
    if len(coefficients) != len(feature_names(candidate, phase)) or any(value < -1e-9 for value in coefficients):
        raise ValidationError(f"NNLS returned an invalid coefficient vector for {candidate}")
    return {
        "status": "fitted",
        "candidate": candidate,
        "kind": "relative_nonnegative_nnls",
        "feature_names": feature_names(candidate, phase),
        "coefficients": [max(0.0, value) for value in coefficients],
        "relative_loss": "sum((prediction-observed)^2 / observed^2) over positive training targets",
        "zero_training_target_count": zero_targets,
        "positive_training_target_count": len(positive),
        "nnls_dependency": str(COMPARE_PATH),
        "nnls_dependency_sha256": sha256_file(COMPARE_PATH),
    }


def _fit_model(rows: list[dict[str, Any]], phase: str, candidate: str, request_domain: str) -> dict[str, Any]:
    """Fit a candidate with exact-domain support and global train backoff."""
    exact = _rows_for_phase_domain(rows, phase, request_domain)
    all_phase = _rows_for_phase_domain(rows, phase, None)
    if _supported(exact):
        fit_rows = exact
        scope = "exact_hardware_domain"
        backoff = []
    elif _supported(all_phase):
        fit_rows = all_phase
        scope = "all_train_hardware_domains_backoff"
        backoff = ["exact_hardware_domain", "all_train_hardware_domains_backoff"]
    else:
        median_rows = exact or all_phase
        median_model = {
            "status": "fitted" if median_rows else "unsupported_no_training_rows",
            "candidate": "hardware_domain_median",
            "kind": "hardware_domain_median",
            "phase": phase,
            "fit_scope": "exact_hardware_domain" if exact else "all_train_hardware_domains_backoff",
            "train_event_count": len(median_rows),
            "train_instance_count": len({str(row["instance_id"]) for row in median_rows}),
            "median_ms": statistics.median([row["observed_ms"] for row in median_rows]) if median_rows else None,
        }
        if candidate == "hardware_domain_median":
            return median_model
        return {
            "status": "fitted_with_median_backoff" if median_rows else "unsupported_no_training_rows",
            "candidate": candidate,
            "kind": "hardware_domain_median_backoff",
            "phase": phase,
            "fit_scope": "median_backoff",
            "train_event_count": len(median_rows),
            "train_instance_count": len({str(row["instance_id"]) for row in median_rows}),
            "fallback": median_model,
            "unsupported_reason": "neither exact nor global train partition met support threshold",
        }
    if candidate == "hardware_domain_median":
        return {
            "status": "fitted",
            "candidate": candidate,
            "kind": "hardware_domain_median",
            "phase": phase,
            "fit_scope": scope,
            "backoff_chain": backoff,
            "train_event_count": len(fit_rows),
            "train_instance_count": len({str(row["instance_id"]) for row in fit_rows}),
            "median_ms": statistics.median([row["observed_ms"] for row in fit_rows]),
        }
    regression = _fit_regression(fit_rows, candidate, phase)
    regression.update(
        {
            "phase": phase,
            "fit_scope": scope,
            "backoff_chain": backoff,
            "train_event_count": len(fit_rows),
            "train_instance_count": len({str(row["instance_id"]) for row in fit_rows}),
            "hardware_domain": request_domain,
        }
    )
    if regression["status"] == "unsupported_no_positive_training_targets":
        median_model = _fit_model(rows, phase, "hardware_domain_median", request_domain)
        regression["status"] = "fitted_with_median_backoff" if median_model.get("status") == "fitted" else regression["status"]
        regression["kind"] = "hardware_domain_median_backoff"
        regression["fallback"] = median_model
    return regression


def _predict_model(model: Mapping[str, Any], row: Mapping[str, Any], phase: str = "e2e") -> tuple[float | None, str | None]:
    status = model.get("status")
    if status == "unsupported_no_training_rows":
        return None, "unsupported_no_training_rows"
    kind = model.get("kind")
    if kind == "hardware_domain_median":
        median_value = model.get("median_ms")
        if median_value is None:
            return None, "unsupported_no_training_rows"
        return max(0.0, float(median_value)), "hardware_domain_median"
    if kind == "hardware_domain_median_backoff":
        fallback = model.get("fallback")
        if isinstance(fallback, Mapping):
            value, reason = _predict_model(fallback, row, phase)
            return value, "median_backoff:" + (reason or "unknown") if value is not None else reason
        return None, "unsupported_no_median_backoff"
    if kind == "relative_nonnegative_nnls":
        try:
            vector = design(row, str(model["candidate"]), phase)
        except (KeyError, TypeError, ValidationError) as exc:
            fallback = model.get("fallback")
            if isinstance(fallback, Mapping):
                value, reason = _predict_model(fallback, row, phase)
                return value, "feature_missing_median_backoff:" + str(exc) if value is not None else reason
            return None, "feature_missing:" + str(exc)
        coefficients = [float(value) for value in model.get("coefficients", [])]
        if len(coefficients) != len(vector):
            return None, "invalid_coefficient_width"
        return max(0.0, float(COMPARE.predict(coefficients, vector))), "relative_nnls"
    return None, "unsupported_model_kind"


def _percent_error(observed: float, predicted: float) -> tuple[float | None, bool, str]:
    if observed == 0.0:
        if predicted == 0.0:
            return 0.0, True, "exact_zero"
        return None, False, "infinite_due_to_zero_target_nonzero_prediction"
    ape = abs(predicted - observed) / observed * 100.0
    return ape, ape <= WITHIN_THRESHOLD_PERCENT, "finite"


def metric(rows: Iterable[dict[str, Any]], candidate: str, phase: str) -> dict[str, Any]:
    # Keep every request in the denominator.  A missing source phase is an
    # explicit unsupported/missing prediction, never silently removed from
    # coverage.
    population = list(rows)
    scored = [
        row for row in population
        if row["observed_ms"].get(phase) is not None
        and row["predictions"].get(candidate, {}).get(phase) is not None
    ]
    missing_rows = len(population) - len(scored)
    errors: list[float] = []
    infinite_count = 0
    exact_zero_pass = 0
    within_count = 0
    for row in scored:
        observed = float(row["observed_ms"][phase])
        predicted = float(row["predictions"][candidate][phase])
        ape, within, status = _percent_error(observed, predicted)
        if status == "exact_zero":
            exact_zero_pass += 1
        if status.startswith("infinite"):
            infinite_count += 1
        else:
            assert ape is not None
            errors.append(ape)
        within_count += int(within)
    ordered = sorted(errors + ([math.inf] * infinite_count))
    p95_value = ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else None
    worst_value = ordered[-1] if ordered else None
    by_run: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for row in population:
        by_run[str(row["case_id"])].append(row)
    run_pass = 0
    run_missing = 0
    for run_rows in by_run.values():
        statuses = []
        for row in run_rows:
            if row["observed_ms"].get(phase) is None or row["predictions"].get(candidate, {}).get(phase) is None:
                statuses.append(None)
                continue
            statuses.append(_percent_error(float(row["observed_ms"][phase]), float(row["predictions"][candidate][phase]))[1])
        if any(value is None for value in statuses):
            run_missing += 1
        elif statuses and all(statuses):
            run_pass += 1
    return {
        "candidate": candidate,
        "phase": phase,
        "population_requests": len(population),
        "predicted_requests": len(scored),
        "missing_prediction_requests": missing_rows,
        "coverage_percent": 100.0 * len(scored) / len(population) if population else 0.0,
        "within25_count": within_count,
        "within25_percent": 100.0 * within_count / len(scored) if scored else None,
        "mean_ape_percent": statistics.fmean(errors) if errors else (0.0 if exact_zero_pass else None),
        "median_ape_percent": statistics.median(errors) if errors else (0.0 if exact_zero_pass else None),
        "p95_ape_percent_nearest_rank": None if p95_value is None or math.isinf(p95_value) else p95_value,
        "p95_status": "infinite_due_to_zero_target_nonzero_prediction" if p95_value is not None and math.isinf(p95_value) else "finite_or_exact_zero",
        "worst_ape_percent": None if worst_value is None or math.isinf(worst_value) else worst_value,
        "worst_status": "infinite_due_to_zero_target_nonzero_prediction" if worst_value is not None and math.isinf(worst_value) else "finite_or_exact_zero",
        "zero_target_scored_count": sum(float(row["observed_ms"][phase]) == 0.0 for row in scored),
        "zero_target_exact_pass_count": exact_zero_pass,
        "infinite_error_count": infinite_count,
        "runs": len(by_run),
        "runs_all_requests_within25_count": run_pass,
        "runs_all_requests_within25_percent": 100.0 * run_pass / len(by_run) if by_run else 0.0,
        "runs_with_missing_predictions": run_missing,
    }


def compact_prediction(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "d9.native-request-prediction.v1",
        "case_id": record["case_id"],
        "instance_id": record["instance_id"],
        "queue_ordinal": record.get("queue_ordinal"),
        "physical_request_id": record["physical_request_id"],
        "fold": record["fold"],
        "hardware_domain": record["hardware_domain"],
        "tokens": record["tokens"],
        "observed_ms": record["observed_ms"],
        "predictions_ms": record["predictions"],
        "prediction_sources": record["prediction_sources"],
        "missing_predictions": record["missing_predictions"],
    }


def run_comparison(dataset_path: Path, manifest_path: Path, output_dir: Path) -> dict[str, Any]:
    rows, manifest, hashes = load_dataset(dataset_path, manifest_path)
    requests, grouping_report = group_requests(rows)
    if len(requests) != 2080:
        raise ValidationError(f"expected all 2080 requests to remain, got {len(requests)}")
    instance_ids = {str(row["instance_id"]) for row in requests}
    if len(instance_ids) != 25:
        raise ValidationError(f"expected 25 grouped instances, got {len(instance_ids)}")
    fold_counts = collections.Counter(int(row["fold"]) for row in requests)
    if set(fold_counts) != set(range(FOLDS)):
        raise ValidationError(f"grouped folds do not cover all five folds: {fold_counts}")
    instance_folds: dict[str, set[int]] = collections.defaultdict(set)
    for row in requests:
        instance_folds[str(row["instance_id"])].add(int(row["fold"]))
    if any(len(folds) != 1 for folds in instance_folds.values()):
        raise ValidationError("an instance was split across grouped folds")

    for request in requests:
        request["predictions"] = {candidate: {} for candidate in CANDIDATES}
        request["prediction_sources"] = {candidate: {} for candidate in CANDIDATES}
        request["missing_predictions"] = []

    fold_models: dict[int, dict[str, dict[str, dict[str, Any]]]] = {}
    fold_support: dict[str, Any] = {}
    for fold in range(FOLDS):
        train_rows = [row for row in rows if fold_for_instance(str(row["instance_id"])) != fold]
        test_requests = [request for request in requests if request["fold"] == fold]
        fold_models[fold] = {phase: {} for phase in PHASES}
        fold_support[str(fold)] = {
            "train_instances": len({str(row["instance_id"]) for row in train_rows}),
            "test_instances": len({str(request["instance_id"]) for request in test_requests}),
            "train_events_by_phase": {phase: len(_rows_for_phase_domain(train_rows, phase, None)) for phase in PHASES},
            "test_requests": len(test_requests),
        }
        for phase in PHASES:
            for candidate in CANDIDATES:
                # All verified rows share one stable H100 domain.  Keeping the
                # request domain in the call makes the supported backoff rule
                # explicit if a future verified dataset adds another domain.
                domain = str(test_requests[0]["hardware_domain"]) if test_requests else str(rows[0]["hardware_domain"])
                model = _fit_model(train_rows, phase, candidate, domain)
                fold_models[fold][phase][candidate] = model
                fold_support[str(fold)].setdefault("models", {}).setdefault(phase, {})[candidate] = {
                    key: value
                    for key, value in model.items()
                    if key in {"status", "kind", "fit_scope", "backoff_chain", "train_event_count", "train_instance_count", "zero_training_target_count", "positive_training_target_count", "unsupported_reason"}
                }
        for request in test_requests:
            for phase in PHASES:
                source_row = request["rows"].get(phase)
                if source_row is None:
                    for candidate in CANDIDATES:
                        request["missing_predictions"].append({"candidate": candidate, "phase": phase, "reason": "missing_source_phase"})
                    continue
                for candidate in CANDIDATES:
                    model = fold_models[fold][phase][candidate]
                    prediction, source = _predict_model(model, source_row, phase)
                    if prediction is None:
                        request["missing_predictions"].append({"candidate": candidate, "phase": phase, "reason": source or "unsupported"})
                    else:
                        request["predictions"][candidate][phase] = prediction
                        request["prediction_sources"][candidate][phase] = source

    # Full-training artifacts are for optional later simulator calls.  OOF
    # metrics above remain the only comparison score in this report.
    all_domain = str(rows[0]["hardware_domain"])
    full_models: dict[str, dict[str, Any]] = {phase: {} for phase in PHASES}
    for phase in PHASES:
        for candidate in CANDIDATES:
            full_models[phase][candidate] = _fit_model(rows, phase, candidate, all_domain)

    candidate_metrics: dict[str, Any] = {}
    for candidate in CANDIDATES:
        candidate_metrics[candidate] = {
            "primary": metric(requests, candidate, "e2e"),
            "diagnostics": {phase: metric(requests, candidate, phase) for phase in ("queue", "prefill", "decode")},
            "all_required_phases_within25_request_count": sum(
                all(
                    request["predictions"].get(candidate, {}).get(phase) is not None
                    and _percent_error(float(request["observed_ms"][phase]), float(request["predictions"][candidate][phase]))[1]
                    for phase in PHASES
                )
                for request in requests
            ),
        }
        candidate_metrics[candidate]["folds"] = {
            str(fold): {
                "primary": metric([request for request in requests if request["fold"] == fold], candidate, "e2e"),
                "diagnostics": {
                    phase: metric([request for request in requests if request["fold"] == fold], candidate, phase)
                    for phase in ("queue", "prefill", "decode")
                },
            }
            for fold in range(FOLDS)
        }

    missing_cells = [
        {
            "case_id": request["case_id"],
            "instance_id": request["instance_id"],
            "physical_request_id": request["physical_request_id"],
            "missing_predictions": request["missing_predictions"],
        }
        for request in requests
        if request["missing_predictions"]
    ]
    missing_report = {
        "schema": "d9.native-missing-predictions.v1",
        "expected_requests": 2080,
        "preserved_request_groups": len(requests),
        "missing_request_groups": len(missing_cells),
        "missing_prediction_cells": sum(len(item["missing_predictions"]) for item in missing_cells),
        "rows": missing_cells,
        "status": "complete" if not missing_cells else "partial_with_explicit_missing_rows",
    }

    report = {
        "schema": "d9.native-model-comparison.v1",
        "status": "development_oof_comparison_complete",
        "primary_target": "native:e2e",
        "diagnostic_targets": ["native:queue", "native:prefill", "native:decode"],
        "candidate_order": list(CANDIDATES),
        "rejected_log_diagnostic": REJECTED_LOG_DIAGNOSTIC,
        "candidate_contracts": {
            "hardware_domain_median": {
                "inputs": [],
                "contract": "measured training target median within verified hardware domain with supported-train backoff",
            },
            "relative_nnls_token": {
                "inputs": ["prompt_tokens", "completion_tokens"],
                "design": "[1, prompt_tokens/1000, completion_tokens/1000]",
                "contract": "conditional_on_realized_native_workload_trace; output/completion counts are not prospective inputs; max_output_tokens is constant in this cohort and omitted",
            },
            "relative_nnls_token_cache": {
                "inputs": ["prompt_tokens", "completion_tokens", "cached_tokens"],
                "design": {
                    "e2e_or_queue": "[1, uncached_prompt_tokens/1000, completion_tokens/1000, prompt_tokens*completion_tokens/1e6]",
                    "prefill": "[1, uncached_prompt_tokens/1000, prompt_tokens/1000]",
                    "decode": "[1, completion_tokens/1000, prompt_tokens*completion_tokens/1e6]",
                },
                "contract": "conditional on realized native workload and cache trace; not prospective cache-state prediction",
            },
        },
        "feature_contract": {
            "allowed_workload_descriptors": ["prompt_tokens", "completion_tokens"],
            "declared_constant_descriptor": "max_output_tokens=2048 retained for provenance and omitted from the design",
            "optional_conditional_cache_descriptor": "cached_tokens",
            "excluded_fields": ["observed_ms", "queue_ms", "prefill_ms", "decode_ms", "e2e_ms", "residual", "status", "outcome", "event timing"],
            "zero_target_policy": "exact zero prediction passes; nonzero prediction is an infinite error; no epsilon is added",
        },
        "split": {
            "folds": FOLDS,
            "grouping_unit": "instance_id",
            "instance_count": len(instance_ids),
            "fold_rule": "sha256('assignment.d9.native-fold-v1:' + instance_id) first 8 bytes modulo 5",
            "fold_counts_by_request": dict(sorted(fold_counts.items())),
            "repeated_case_executions_stay_with_instance": True,
        },
        "support_rule": {
            "minimum_training_events": MIN_EVENTS,
            "minimum_training_instances": MIN_INSTANCES,
            "backoff": ["exact_hardware_domain", "all_train_hardware_domains", "unsupported"],
            "prediction_missingness_remains_visible": True,
        },
        "source": {
            "dataset_path": str(dataset_path),
            "manifest_path": str(manifest_path),
            **hashes,
            "nnls_dependency_path": str(COMPARE_PATH),
            "nnls_dependency_sha256": sha256_file(COMPARE_PATH),
            "verified_hardware_domain": all_domain,
        },
        "population": {
            "requests": len(requests),
            "phase_rows": len(rows),
            "instances": len(instance_ids),
            "cases": len({str(request["case_id"]) for request in requests}),
            "grouping_report": grouping_report,
        },
        "fold_support": fold_support,
        "metrics": candidate_metrics,
        "missing_predictions": missing_report,
        "limitations": [
            "This is a development out-of-fold comparison on the repaired train_calibration population, not a sealed evaluator holdout.",
            "All observations use one verified static H100 GPU inventory domain; cross-hardware accuracy is unvalidated.",
            "Completion and cached-token counts are available only after a realized native request trace, so the token and cache candidates are conditional replay models and are not prospective online forecasts.",
            "Native E2E is reported directly; queue/prefill/decode diagnostics are separate and are never summed into an E2E claim.",
            "The report does not claim the literal D9 all-event acceptance gate from aggregate coverage metrics.",
        ],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = output_dir / "predictions.jsonl"
    with prediction_path.open("w", encoding="utf-8") as stream:
        for request in requests:
            stream.write(json.dumps(compact_prediction(request), sort_keys=True, separators=(",", ":")) + "\n")
    fit_artifact = {
        "schema": "d9.native-fit-artifact.v1",
        "status": "full_train_fit_for_conditional_replay_only",
        "primary_target": "native:e2e",
        "candidate_order": list(CANDIDATES),
        "source": report["source"],
        "verified_hardware_domain": all_domain,
        "feature_contract": report["feature_contract"],
        "support_rule": report["support_rule"],
        "full_training_models": full_models,
        "prediction_interface": {
            "request_identity_fields": ["case_id", "instance_id", "physical_request_id"],
            "required_conditional_inputs": ["prompt_tokens", "completion_tokens"],
            "optional_conditional_cache_input": "cached_tokens",
            "target_outputs": ["native:e2e", "native:queue", "native:prefill", "native:decode"],
            "prospective_status": "unsupported_without_realized_workload_and_cache_trace",
            "cross_hardware_status": "unvalidated",
        },
    }
    _write_json(output_dir / "fit_artifact.json", fit_artifact)
    _write_json(output_dir / "missing_predictions.json", missing_report)
    _write_json(output_dir / "rejected_log_diagnostic.json", REJECTED_LOG_DIAGNOSTIC | {
        "dataset_sha256": hashes["dataset_sha256"],
        "manifest_sha256": hashes["manifest_sha256"],
    })
    _write_json(output_dir / "report.json", report)
    return report


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = run_comparison(args.dataset.resolve(), args.manifest.resolve(), args.output_dir.resolve())
    print(json.dumps({
        "status": report["status"],
        "primary_target": report["primary_target"],
        "requests": report["population"]["requests"],
        "missing_prediction_cells": report["missing_predictions"]["missing_prediction_cells"],
        "primary_metrics": {name: value["primary"] for name, value in report["metrics"].items()},
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
