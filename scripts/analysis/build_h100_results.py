#!/usr/bin/env python3
"""Build the canonical, provenance-first H100 results package.

The builder intentionally starts from tracked evaluator summaries and compact
measurement manifests.  It does not parse prose progress notes or infer missing
hardware counters.  Running it twice against the same inputs produces identical
outputs.
"""

from __future__ import annotations

import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


BASELINE_SOURCES = (
    (
        "lite-diverse-6",
        "lite",
        "sweagent_output.gcp-h100-diverse-lite-lite-diverse-6-worker-00.json",
    ),
    (
        "lite-diverse-next-6",
        "lite",
        "sweagent_output.gcp-h100-diverse-lite-next-lite-diverse-next-6-worker-00.json",
    ),
    (
        "lite-diverse-batch03-6",
        "lite",
        "sweagent_output.gcp-h100-diverse-lite-b03-lite-diverse-batch03-6-worker-00.json",
    ),
    (
        "lite-diverse-batch04-6",
        "lite",
        "sweagent_output.gcp-h100-diverse-lite-b04-lite-diverse-batch04-6-worker-00.json",
    ),
    (
        "lite-diverse-batch05-6",
        "lite",
        "sweagent_output.gcp-h100-diverse-lite-b05-lite-diverse-batch05-6-worker-00.json",
    ),
    (
        "lite-production-2",
        "lite",
        "sweagent_output.gcp-lite-production-2-lite-production-2-worker-00.json",
    ),
    (
        "lite-production-2",
        "lite",
        "sweagent_output.gcp-lite-production-2-lite-production-2-worker-01.json",
    ),
    (
        "verified-diverse-6",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-verified-diverse-6-worker-00.json",
    ),
    (
        "verified-diverse-next-6",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-next-verified-diverse-next-6-worker-00.json",
    ),
    (
        "verified-diverse-batch03-6",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-b03-verified-diverse-batch03-6-worker-00.json",
    ),
    (
        "verified-diverse-batch04-6",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-b04-verified-diverse-batch04-6-worker-00.json",
    ),
    (
        "verified-diverse-batch05-6",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-b05-verified-diverse-batch05-6-worker-00.json",
    ),
    (
        "verified-extra-1",
        "verified",
        "sweagent_output.gcp-h100-diverse-verified-extra-1-verified-extra-1-worker-00.json",
    ),
)

ADDITIONAL_SOURCES = (
    (
        "additional-lite-14182",
        "lite",
        "sweagent_output.gcp-h100-additional-lite-20260823-additional-lite-14182-worker-00.json",
    ),
    (
        "additional-verified-14365",
        "verified",
        "sweagent_output.gcp-h100-additional-verified-20260823-additional-verified-14365-worker-00.json",
    ),
)

AUXILIARY_SOURCES = (
    (
        "calls20-lite",
        "lite",
        "sweep_support",
        "sweagent_output.gcp-h100-calls20-lite-20260823-calls20-lite-worker-00.json",
    ),
    (
        "temp02-lite",
        "lite",
        "sweep_support",
        "sweagent_output.gcp-h100-temp02-lite-20260823-temp02-lite-worker-00.json",
    ),
    (
        "diversity-flask-5063",
        "lite",
        "diversity_repeat",
        "sweagent_output.gcp-h100-diversity-flask-20260823-diversity-flask-5063-worker-00.json",
    ),
    (
        "diversity-requests-2317",
        "lite",
        "diversity_repeat",
        "sweagent_output.gcp-h100-diversity-requests-2317-20260823-diversity-requests-2317-worker-00.json",
    ),
    (
        "profile-requests-2317",
        "lite",
        "profile_repeat",
        "sweagent_output.gcp-h100-profile-requests-2317-rerun-20260823-profile-requests-2317-rerun-worker-00.json",
    ),
)

MEASUREMENT_FILES = (
    "project/GCP_H100_PROGRESS.json",
    "project/GCP_H100_MEASUREMENTS.json",
    "project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json",
    "project/GCP_H100_PROFILE_REQUESTS_2317_20260823.json",
    "project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json",
    "project/GCP_H100_VLLM_CALIBRATION_20260823E.json",
    "project/GCP_H100_KINETO_SIMULATOR_20260824.json",
    "project/GCP_H100_KINETO_TRAJECTORY_20260824.json",
    "project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json",
    "project/MODAL_LITE_SWEEP_MEASURED.json",
)

SWEEP_KNOBS = {
    "agent.model.per_instance_call_limit": "maximum_calls",
    "completion_kwargs.max_tokens": "maximum_output_tokens",
    "agent.templates.max_observation_length": "observation_length",
    "agent.model.temperature": "temperature",
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_for(instance_id: str) -> str:
    return instance_id.rsplit("-", 1)[0].replace("__", "/")


def validate_evaluator(data: dict[str, Any], path: Path) -> None:
    pairs = {
        "submitted_instances": "submitted_ids",
        "completed_instances": "completed_ids",
        "resolved_instances": "resolved_ids",
        "unresolved_instances": "unresolved_ids",
        "empty_patch_instances": "empty_patch_ids",
        "error_instances": "error_ids",
    }
    for count_key, ids_key in pairs.items():
        if data[count_key] != len(data[ids_key]):
            raise ValueError(f"{path}: {count_key} disagrees with {ids_key}")
    completed = set(data["completed_ids"])
    resolved = set(data["resolved_ids"])
    unresolved = set(data["unresolved_ids"])
    empty = set(data["empty_patch_ids"])
    incomplete = set(data["incomplete_ids"])
    if completed != resolved | unresolved or resolved & unresolved:
        raise ValueError(f"{path}: completed/resolved/unresolved partition is invalid")
    if set(data["submitted_ids"]) != completed | empty:
        raise ValueError(f"{path}: submitted/completed/empty partition is invalid")
    if data["total_instances"] != len(completed | empty | incomplete):
        raise ValueError(f"{path}: total instance count is invalid")


def evaluator_rows(
    root: Path,
    sources: Iterable[tuple[str, str, str]],
    cohort: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for batch, suite, relative in sources:
        path = root / relative
        data = load_json(path)
        validate_evaluator(data, path)
        statuses: dict[str, str] = {}
        for status, key in (
            ("resolved", "resolved_ids"),
            ("unresolved", "unresolved_ids"),
            ("empty_patch", "empty_patch_ids"),
            ("incomplete", "incomplete_ids"),
            ("error", "error_ids"),
        ):
            for instance_id in data[key]:
                statuses[instance_id] = status
        for instance_id in sorted(statuses):
            status = statuses[instance_id]
            rows.append(
                {
                    "cohort": cohort,
                    "suite": suite,
                    "batch": batch,
                    "instance_id": instance_id,
                    "repository": repository_for(instance_id),
                    "status": status,
                    "completed": status in {"resolved", "unresolved"},
                    "submitted": status in {"resolved", "unresolved", "empty_patch"},
                    "official_resolved": status == "resolved",
                    "source_file": relative,
                    "source_sha256": sha256(path),
                }
            )
    return rows


def summarize_population(rows: list[dict[str, Any]], suite: str) -> dict[str, Any]:
    suite_rows = [row for row in rows if row["suite"] == suite]
    completed = [row for row in suite_rows if row["completed"]]
    resolved = [row for row in completed if row["official_resolved"]]
    selected = len(suite_rows)
    return {
        "selected_instances": selected,
        "submitted_instances": sum(bool(row["submitted"]) for row in suite_rows),
        "completed_instances": len(completed),
        "resolved_instances": len(resolved),
        "unresolved_instances": len(completed) - len(resolved),
        "empty_patch_instances": sum(row["status"] == "empty_patch" for row in suite_rows),
        "incomplete_instances": sum(row["status"] == "incomplete" for row in suite_rows),
        "resolved_rate_completed_percent": len(resolved) / len(completed) * 100.0,
        "resolved_rate_selected_percent": len(resolved) / selected * 100.0,
        "unique_repositories": sorted({row["repository"] for row in completed}),
        "unique_repository_count": len({row["repository"] for row in completed}),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    if not rows and fields is None:
        raise ValueError(f"cannot infer columns for empty CSV {path}")
    columns = fields or list(rows[0])
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=columns,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def build_sweep_rows(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = root / "project/MODAL_LITE_SWEEP_MEASURED.json"
    data = load_json(source)
    baseline = next(cell for cell in data["cells"] if cell["knob"] == "baseline")
    successful = [cell for cell in data["cells"] if "trajectory_wall_seconds" in cell]
    rows: list[dict[str, Any]] = []
    for knob, short_name in SWEEP_KNOBS.items():
        value = baseline["value"][knob]
        rows.append(sweep_row(baseline, knob, short_name, value, True, source, root))
    for cell in successful:
        if cell["knob"] == "baseline":
            continue
        rows.append(
            sweep_row(
                cell,
                cell["knob"],
                SWEEP_KNOBS[cell["knob"]],
                cell["value"],
                False,
                source,
                root,
            )
        )
    rows.sort(key=lambda row: (row["sweep"], float(row["value"])))
    counts = Counter(row["sweep"] for row in rows)
    if counts != Counter({name: 4 for name in SWEEP_KNOBS.values()}):
        raise ValueError(f"sweep endpoint coverage is incomplete: {counts}")
    exclusions = []
    for cell in data["cells"]:
        if "trajectory_wall_seconds" not in cell:
            exclusions.append(
                {
                    "artifact": cell["cell_id"],
                    "scope": "modal_sweep",
                    "reason": cell.get("failure", "infrastructure failure"),
                    "treatment": "excluded; successful retry is the attributable endpoint",
                    "source_file": str(source.relative_to(root)),
                }
            )
    return rows, exclusions


def sweep_row(
    cell: dict[str, Any],
    knob: str,
    short_name: str,
    value: Any,
    baseline: bool,
    source: Path,
    root: Path,
) -> dict[str, Any]:
    return {
        "provider": "modal",
        "suite": "lite",
        "instance_id": "astropy__astropy-12907",
        "sweep": short_name,
        "downstream_setting": knob,
        "value": value,
        "is_shared_baseline": baseline,
        "cell_id": cell["cell_id"],
        "trajectory_wall_seconds": cell["trajectory_wall_seconds"],
        "official_evaluation_seconds": cell["official_evaluation_seconds"],
        "official_resolved": cell["official_resolved"],
        "official_unresolved": cell["official_unresolved"],
        "empty_patch": cell["empty_patch"],
        "agent_returncode": cell["agent_returncode"],
        "evaluator_returncode": cell["official_evaluator_returncode"],
        "source_file": str(source.relative_to(root)),
        "source_sha256": sha256(source),
    }


def build_simulator(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source = root / "project/GCP_H100_KINETO_SIMULATOR_20260824.json"
    data = load_json(source)
    records = data["matrix"]["records"]
    calibration = [row for row in records if row["split"] == "calibration"]
    holdout = [row for row in records if row["split"] == "holdout"]
    residuals = [
        (row["wall_ms"] - row["cpu_exclusive_interval_ms"] - row["device_activity_union_ms"])
        / 1000.0
        for row in calibration
    ]
    fixed_seconds = statistics.median(residuals)
    predictions = []
    for row in holdout:
        observed = row["wall_ms"] / 1000.0
        predicted = (
            fixed_seconds
            + row["cpu_exclusive_interval_ms"] / 1000.0
            + row["device_activity_union_ms"] / 1000.0
        )
        error = predicted - observed
        predictions.append(
            {
                "case_id": row["case_id"],
                "observed_seconds": observed,
                "predicted_seconds": predicted,
                "error_seconds": error,
                "absolute_percentage_error": abs(error) / observed * 100.0,
            }
        )
    mae = statistics.mean(abs(row["error_seconds"]) for row in predictions)
    mape = statistics.mean(row["absolute_percentage_error"] for row in predictions)
    recorded = data["simulator_holdout"]
    for label, derived, reported in (
        ("fixed_seconds", fixed_seconds, recorded["fixed_seconds"]),
        ("MAE", mae, recorded["mean_absolute_error_seconds"]),
        ("MAPE", mape, recorded["mean_absolute_percentage_error"]),
    ):
        if abs(derived - reported) > 1e-9:
            raise ValueError(f"independent {label} does not match {source}")
    summary = {
        "calibration_records": len(calibration),
        "holdout_records": len(holdout),
        "calibration_run_ids": [row["case_id"] for row in calibration],
        "holdout_run_ids": [row["case_id"] for row in holdout],
        "fixed_seconds": fixed_seconds,
        "mean_absolute_error_seconds": mae,
        "mean_absolute_percentage_error": mape,
        "assignment_target_percent": recorded["assignment_target_percent"],
        "target_met_for_controlled_matrix": mape < recorded["assignment_target_percent"],
        "holdout_predictions": predictions,
        "split_predeclared_in": "scripts/observability/run_kineto_matrix.py",
        "source_file": str(source.relative_to(root)),
        "source_sha256": sha256(source),
    }
    table = []
    for row in records:
        table.append(
            {
                **row,
                "observed_seconds": row["wall_ms"] / 1000.0,
                "cpu_seconds": row["cpu_exclusive_interval_ms"] / 1000.0,
                "gpu_seconds_at_reference": row["device_activity_union_ms"] / 1000.0,
                "source_file": str(source.relative_to(root)),
            }
        )
    return summary, table


def build_service_calibration(root: Path) -> list[dict[str, Any]]:
    source = root / "project/GCP_H100_VLLM_CALIBRATION_20260823E.json"
    data = load_json(source)
    rows = []
    for condition in data["conditions"]:
        metrics = condition["benchmark_metrics"]
        rows.append(
            {
                "input_tokens": condition["input_tokens"],
                "output_tokens": condition["output_tokens"],
                "num_prompts": condition["num_prompts"],
                "max_concurrency": condition["max_concurrency"],
                "benchmark_duration_seconds": metrics["benchmark_duration_seconds"],
                "request_throughput_req_per_s": metrics["request_throughput_req_per_s"],
                "output_token_throughput_tok_per_s": metrics[
                    "output_token_throughput_tok_per_s"
                ],
                "total_token_throughput_tok_per_s": metrics[
                    "total_token_throughput_tok_per_s"
                ],
                "median_ttft_ms": metrics["median_ttft_ms"],
                "p99_ttft_ms": metrics["p99_ttft_ms"],
                "median_tpot_ms": metrics["median_tpot_ms"],
                "p99_tpot_ms": metrics["p99_tpot_ms"],
                "gpu_time_available": False,
                "source_file": str(source.relative_to(root)),
                "source_sha256": sha256(source),
            }
        )
    return rows


def build_observability(root: Path) -> list[dict[str, Any]]:
    trajectory_path = root / "project/GCP_H100_KINETO_TRAJECTORY_20260824.json"
    trajectory = load_json(trajectory_path)
    attribution = trajectory["request_device_attribution"]
    process_path = root / "project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json"
    process = load_json(process_path)
    request_process_samples = sum(row["samples"] for row in process["per_request"])
    request_worker_samples = sum(row["pid2320_samples"] for row in process["per_request"])
    strace_path = root / "project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json"
    strace = load_json(strace_path)["run"]
    requests_path = root / "project/GCP_H100_PROFILE_REQUESTS_2317_20260823.json"
    requests = load_json(requests_path)
    ncu_path = root / "project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json"
    ncu = load_json(ncu_path)
    return [
        {
            "measurement": "real_sweagent_kineto",
            "scope": "31 serialized requests in one Lite Astropy trajectory",
            "measurement_type": "direct_kineto_cpu_cuda_activity",
            "request_count": attribution["request_count"],
            "prompt_tokens": attribution["prompt_tokens"],
            "completion_tokens": attribution["completion_tokens"],
            "request_wall_ms": attribution["request_wall_ms_sum"],
            "device_activity_union_ms": attribution["device_activity_union_ms"],
            "kernel_duration_sum_ms": attribution["kernel_duration_sum_ms"],
            "gpu_utilization_max_percent": "",
            "process_rows": "",
            "worker_samples": "",
            "file_operation_lines": "",
            "status": "measured",
            "source_file": str(trajectory_path.relative_to(root)),
        },
        {
            "measurement": "process_attribution_whole_capture",
            "scope": "whole process-sampler capture window",
            "measurement_type": "process_level_nvml_sampling",
            "request_count": process["request_count"],
            "prompt_tokens": process["prompt_tokens_sum"],
            "completion_tokens": process["completion_tokens_sum"],
            "request_wall_ms": process["request_duration_ms_sum"],
            "device_activity_union_ms": "",
            "kernel_duration_sum_ms": "",
            "gpu_utilization_max_percent": process["aggregate"]["gpu_util_max_pct"],
            "process_rows": process["valid_process_rows"],
            "worker_samples": process["aggregate"]["pid2320_samples"],
            "file_operation_lines": "",
            "status": "measured_sampled_utilization",
            "source_file": str(process_path.relative_to(root)),
        },
        {
            "measurement": "process_attribution_request_overlap",
            "scope": "sum of rows overlapping the 31 request windows",
            "measurement_type": "process_level_nvml_sampling",
            "request_count": process["overlap_request_count"],
            "prompt_tokens": process["prompt_tokens_sum"],
            "completion_tokens": process["completion_tokens_sum"],
            "request_wall_ms": process["request_duration_ms_sum"],
            "device_activity_union_ms": "",
            "kernel_duration_sum_ms": "",
            "gpu_utilization_max_percent": process["aggregate"]["gpu_util_max_pct"],
            "process_rows": request_process_samples,
            "worker_samples": request_worker_samples,
            "file_operation_lines": "",
            "status": "measured_sampled_utilization",
            "source_file": str(process_path.relative_to(root)),
        },
        {
            "measurement": "astropy_cpu_tool_strace",
            "scope": "one real profile-only Lite trajectory",
            "measurement_type": "strace_plus_100ms_aggregate_nvml",
            "request_count": strace["request_proxy_events"],
            "prompt_tokens": strace["prompt_tokens"],
            "completion_tokens": strace["completion_tokens"],
            "request_wall_ms": strace["model_request_duration_ms_sum"],
            "device_activity_union_ms": "",
            "kernel_duration_sum_ms": "",
            "gpu_utilization_max_percent": strace["gpu_max_utilization_percent"],
            "process_rows": strace["strace_lines"],
            "worker_samples": strace["gpu_samples"],
            "file_operation_lines": strace["strace_file_operation_lines"],
            "status": "measured_profile_only",
            "source_file": str(strace_path.relative_to(root)),
        },
        {
            "measurement": "requests_cpu_tool_strace",
            "scope": "one completed officially evaluated Requests Lite trajectory",
            "measurement_type": "strace_plus_1s_aggregate_dmon",
            "request_count": 1,
            "prompt_tokens": requests["measurement_contract"]["request_sample"]["prompt_tokens"],
            "completion_tokens": requests["measurement_contract"]["request_sample"][
                "completion_tokens"
            ],
            "request_wall_ms": requests["measurement_contract"]["request_sample"]["duration_ms"],
            "device_activity_union_ms": "",
            "kernel_duration_sum_ms": "",
            "gpu_utilization_max_percent": "",
            "process_rows": requests["profiling"]["strace"]["lines"],
            "worker_samples": requests["profiling"]["gpu_sampler"]["raw_lines"],
            "file_operation_lines": "",
            "status": "measured_compact_manifest_only",
            "source_file": str(requests_path.relative_to(root)),
        },
        {
            "measurement": "ncu_hardware_counters",
            "scope": "bounded in-container capability probe",
            "measurement_type": "nvidia_ncu_performance_counters",
            "request_count": "",
            "prompt_tokens": "",
            "completion_tokens": "",
            "request_wall_ms": "",
            "device_activity_union_ms": "",
            "kernel_duration_sum_ms": "",
            "gpu_utilization_max_percent": "",
            "process_rows": "",
            "worker_samples": "",
            "file_operation_lines": "",
            "status": f"blocked: {ncu['observed']['error']}",
            "source_file": str(ncu_path.relative_to(root)),
        },
    ]


def build_repository_coverage(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["completed"]:
            grouped[(row["suite"], row["repository"])].append(row)
    output = []
    for (suite, repository), group in sorted(grouped.items()):
        resolved = sum(bool(row["official_resolved"]) for row in group)
        output.append(
            {
                "suite": suite,
                "repository": repository,
                "completed_instances": len(group),
                "resolved_instances": resolved,
                "unresolved_instances": len(group) - resolved,
                "resolved_rate_percent": resolved / len(group) * 100.0,
            }
        )
    return output


def build_source_inventory(root: Path) -> list[dict[str, Any]]:
    candidates: set[Path] = set()
    candidates.update(root.glob("sweagent_output*.json"))
    candidates.update((root / "project").glob("GCP_H100_*"))
    candidates.update((root / "project").glob("MODAL_*_MEASURED.json"))
    for directory in (root / "project").glob("modal-*"):
        if directory.is_dir():
            candidates.update(path for path in directory.rglob("*") if path.is_file())
    candidates.update(root / relative for relative in MEASUREMENT_FILES)
    rows = []
    for path in sorted(candidates):
        relative = str(path.relative_to(root))
        if relative.startswith("project/h100_results/"):
            continue
        if path.name.startswith("sweagent_output"):
            role = "official_evaluator_summary"
        elif path.name.endswith("_MEASURED.json"):
            role = "modal_measurement_manifest"
        elif path.name.startswith("GCP_H100_"):
            role = "gcp_h100_compact_evidence"
        else:
            role = "tracked_raw_or_intermediate_modal_evidence"
        rows.append(
            {
                "path": relative,
                "role": role,
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return rows


def build_claims(root: Path) -> list[dict[str, Any]]:
    return [
        claim(
            "Lite primary cohort",
            "32 selected/completed; 8 resolved; 25.0%",
            "direct",
            "population_runs.csv",
        ),
        claim(
            "Verified primary cohort",
            "32 selected, 30 submitted, 29 completed, 10 resolved; "
            "34.4827586% completed-case and 31.25% selected-cohort rate",
            "direct",
            "population_runs.csv",
        ),
        claim(
            "Canonical unique completed inventory",
            "32 Lite and 29 Verified; additional Lite duplicates a baseline ID",
            "derived_set_union",
            "evaluation_attempts.csv",
        ),
        claim(
            "Simulator controlled holdout",
            "4 calibration, 2 predeclared holdout, 10.7156323% MAPE",
            "independently_recomputed",
            "kineto_matrix.csv",
        ),
        claim(
            "Real SWE-agent Kineto",
            "31 requests, 462104 prompt + 9688 completion tokens, 56063.685931 ms device union",
            "direct_kineto_activity",
            "project/GCP_H100_KINETO_TRAJECTORY_20260824.json",
        ),
        claim(
            "Process attribution whole window",
            "3456 rows, 385 vLLM worker samples, 92% peak sampled SM utilization",
            "process_nvml_sampling",
            "project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json",
        ),
        claim(
            "Process attribution request overlap",
            "31/31 requests overlap process samples and the vLLM worker; 2666/298 overlapping samples",
            "derived_from_per_request_rows",
            "observability_summary.csv",
        ),
        claim(
            "Four sweep axes",
            "4 measured endpoint values on each axis, including one shared baseline",
            "direct_modal_single_instance",
            "sweep_results.csv",
        ),
        claim(
            "NCU hardware counters",
            "unavailable: ERR_NVGPUCTRPERM; no report created",
            "measured_capability_failure",
            "project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json",
        ),
        claim(
            "H100 acquisition decision",
            "closed; remaining limitations are analysis/generalization or cross-GPU questions",
            "audit_decision",
            "H100_RESULTS.md",
        ),
    ]


def claim(name: str, value: str, evidence_type: str, source_file: str) -> dict[str, str]:
    return {
        "claim": name,
        "verified_value": value,
        "evidence_type": evidence_type,
        "source_file": source_file,
    }


def build_exclusions(extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return extra + [
        {
            "artifact": "psf__requests-1724",
            "scope": "verified primary cohort",
            "reason": "reproducible environment-install failure",
            "treatment": "retained as incomplete; excluded from completed denominator",
            "source_file": "population_runs.csv",
        },
        {
            "artifact": "django__django-10097",
            "scope": "verified primary cohort",
            "reason": "reproducible environment-install failure",
            "treatment": "retained as incomplete; excluded from completed denominator",
            "source_file": "population_runs.csv",
        },
        {
            "artifact": "pylint-dev__pylint-4604",
            "scope": "verified primary cohort",
            "reason": "empty generated patch",
            "treatment": "retained as submitted empty patch; excluded from completed denominator",
            "source_file": "population_runs.csv",
        },
        {
            "artifact": "astropy__astropy-14182 additional Lite",
            "scope": "canonical unique inventory",
            "reason": "duplicates an already completed baseline instance ID",
            "treatment": "retained as an additional attempt; does not increase unique count",
            "source_file": "evaluation_attempts.csv",
        },
        {
            "artifact": "historical NVML integrations",
            "scope": "exact device time",
            "reason": "sampled aggregate utilization is not direct device activity",
            "treatment": "retained as sampled-utilization evidence; excluded from exact GPU seconds",
            "source_file": "observability_summary.csv",
        },
        {
            "artifact": "NCU capability probe",
            "scope": "hardware performance counters",
            "reason": "ERR_NVGPUCTRPERM and no NCU report",
            "treatment": "recorded as blocked; no counter values claimed",
            "source_file": "project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json",
        },
        {
            "artifact": "compact GCP population summaries",
            "scope": "average end-to-end latency and cost",
            "reason": "summaries contain outcomes/IDs but no per-instance latency or price ledger",
            "treatment": "no population latency, energy, dollar-cost, or efficiency claim",
            "source_file": "population_runs.csv",
        },
        {
            "artifact": "Modal sweep dataset revision",
            "scope": "strict pinned-dataset reproduction",
            "reason": "evaluator observed a different revision and contract was not enforced",
            "treatment": "sweep retained as measured one-instance sensitivity evidence only",
            "source_file": "project/MODAL_LITE_SWEEP_MEASURED.json",
        },
    ]


def build(root: Path, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    output = (output_dir or root / "project/h100_results").resolve()
    output.mkdir(parents=True, exist_ok=True)

    baseline = evaluator_rows(root, BASELINE_SOURCES, "baseline_population")
    additional = evaluator_rows(root, ADDITIONAL_SOURCES, "additional_pinned")
    auxiliary = []
    for batch, suite, role, relative in AUXILIARY_SOURCES:
        auxiliary.extend(evaluator_rows(root, ((batch, suite, relative),), role))

    for suite in ("lite", "verified"):
        completed_ids = {
            row["instance_id"]
            for row in baseline + additional
            if row["suite"] == suite and row["completed"]
        }
        for row in auxiliary:
            if row["suite"] == suite and row["completed"] and row["instance_id"] not in completed_ids:
                raise ValueError(
                    f"auxiliary {suite} instance {row['instance_id']} is not represented in the "
                    "canonical inventory"
                )

    # The primary Lite cohort is the five six-instance batches plus the two
    # production controls (32 selected).  Its later additional Astropy attempt
    # repeats an existing instance and remains an auxiliary attempt.  The
    # primary Verified cohort is the five six-instance batches, the extra
    # control, and the final additional pinned instance (32 selected).
    primary_population = baseline + [
        {**row, "cohort": "primary_population_extension"}
        for row in additional
        if row["suite"] == "verified"
    ]
    populations = {
        suite: summarize_population(primary_population, suite)
        for suite in ("lite", "verified")
    }
    unique_completed = {
        suite: sorted(
            {
                row["instance_id"]
                for row in baseline + additional
                if row["suite"] == suite and row["completed"]
            }
        )
        for suite in ("lite", "verified")
    }
    if {suite: len(ids) for suite, ids in unique_completed.items()} != {
        "lite": 32,
        "verified": 29,
    }:
        raise ValueError("canonical completed instance inventory changed unexpectedly")

    sweep_rows, sweep_exclusions = build_sweep_rows(root)
    simulator, kineto_rows = build_simulator(root)
    service_rows = build_service_calibration(root)
    observability_rows = build_observability(root)
    repository_rows = build_repository_coverage(primary_population)
    claims = build_claims(root)
    exclusions = build_exclusions(sweep_exclusions)
    inventory = build_source_inventory(root)

    population_fields = [
        "cohort",
        "suite",
        "batch",
        "instance_id",
        "repository",
        "status",
        "completed",
        "submitted",
        "official_resolved",
        "source_file",
        "source_sha256",
    ]
    write_csv(output / "population_runs.csv", primary_population, population_fields)
    write_csv(
        output / "evaluation_attempts.csv",
        baseline + additional + auxiliary,
        population_fields,
    )
    write_csv(output / "repository_coverage.csv", repository_rows)
    write_csv(output / "sweep_results.csv", sweep_rows)
    write_csv(output / "kineto_matrix.csv", kineto_rows)
    write_csv(output / "service_calibration.csv", service_rows)
    write_csv(output / "observability_summary.csv", observability_rows)
    write_csv(output / "source_inventory.csv", inventory)
    write_csv(output / "claim_provenance.csv", claims)
    write_csv(output / "exclusions.csv", exclusions)

    trajectory = load_json(root / "project/GCP_H100_KINETO_TRAJECTORY_20260824.json")
    process = load_json(root / "project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json")
    ncu = load_json(root / "project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json")
    sweep = load_json(root / "project/MODAL_LITE_SWEEP_MEASURED.json")
    canonical = {
        "schema_version": "canonical-h100-results.v1",
        "audit_as_of_utc": "2026-08-24",
        "status": "H100 DATA ACQUISITION CLOSED",
        "hardware": "NVIDIA H100 80GB HBM3",
        "runtime": {
            "model": trajectory["model"],
            "model_revision": trajectory["model_revision"],
            "vllm_image": trajectory["vllm_image"],
            "sweagent_revision": trajectory["sweagent_revision"],
            "precision": "bf16",
        },
        "primary_evaluation_cohorts": populations,
        "canonical_unique_completed_inventory": {
            "definition": "set union of completed baseline and additional pinned instance IDs",
            "lite_count": len(unique_completed["lite"]),
            "verified_count": len(unique_completed["verified"]),
            "lite_instance_ids": unique_completed["lite"],
            "verified_instance_ids": unique_completed["verified"],
        },
        "simulator": simulator,
        "real_sweagent_kineto": {
            **trajectory["request_device_attribution"],
            "suite": trajectory["suite"],
            "instance_id": trajectory["instance_id"],
            "official_resolved": trajectory["official_evaluator"]["resolved"],
        },
        "process_attribution": {
            "request_count": process["request_count"],
            "requests_with_process_overlap": process["overlap_request_count"],
            "requests_with_vllm_worker_overlap": process["overlap_pid2320_request_count"],
            "whole_capture_valid_process_rows": process["valid_process_rows"],
            "whole_capture_vllm_worker_samples": process["aggregate"]["pid2320_samples"],
            "request_overlap_process_samples": sum(row["samples"] for row in process["per_request"]),
            "request_overlap_vllm_worker_samples": sum(
                row["pid2320_samples"] for row in process["per_request"]
            ),
            "sampled_device_utilization_peak_percent": process["aggregate"][
                "gpu_util_max_pct"
            ],
            "sampled_worker_sm_peak_percent": process["aggregate"]["pid2320_sm_max_pct"],
            "sampled_worker_memory_peak_percent": process["aggregate"][
                "pid2320_mem_max_pct"
            ],
        },
        "sweeps": {
            "provider": sweep["provider"],
            "suite": sweep["dataset"],
            "instance_id": sweep["instance_id"],
            "axes": {name: 4 for name in SWEEP_KNOBS.values()},
            "attributable_endpoint_rows": 16,
            "shared_baseline_rows": 4,
            "unique_successful_trajectories": 13,
            "failed_initial_attempts_retained": 2,
            "dataset_revision_contract_enforced": sweep[
                "evaluator_dataset_revision_contract_enforced"
            ],
        },
        "ncu": {
            "status": "blocked",
            "error": ncu["observed"]["error"],
            "report_created": ncu["observed"]["ncu_report_created"],
            "hardware_counter_metrics_claimed": False,
        },
        "files": {
            "population_runs": "population_runs.csv",
            "evaluation_attempts": "evaluation_attempts.csv",
            "repository_coverage": "repository_coverage.csv",
            "sweep_results": "sweep_results.csv",
            "kineto_matrix": "kineto_matrix.csv",
            "service_calibration": "service_calibration.csv",
            "observability_summary": "observability_summary.csv",
            "source_inventory": "source_inventory.csv",
            "claim_provenance": "claim_provenance.csv",
            "exclusions": "exclusions.csv",
        },
    }
    (output / "canonical_results.json").write_text(
        json.dumps(canonical, indent=2, sort_keys=True) + "\n"
    )
    return canonical


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    canonical = build(root)
    print(
        json.dumps(
            {
                "status": canonical["status"],
                "lite": canonical["canonical_unique_completed_inventory"]["lite_count"],
                "verified": canonical["canonical_unique_completed_inventory"][
                    "verified_count"
                ],
                "holdout_mape": canonical["simulator"]["mean_absolute_percentage_error"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
