#!/usr/bin/env python3
"""Render the retained-cohort D9 comparison into a numerical report.

This reporter consumes only the retained review outputs written by
``d9_cpu_review.py``.  It does not load the calibration cache, recover
actions, refit a model, execute GPU work, or inspect holdout/raw sources.
The center is supplied by the root review decision; this script never selects
a model or recommends a holdout.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import random
import statistics
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.assignment.d9_cpu_review import OUT  # noqa: E402


GATE_PERCENT = 25.0
PROTOCOLS = ("original_run_id", "instance_id_grouped", "repository_transfer")
CANDIDATE_FILES = {
    "baseline": "baseline",
    "no_repo_median": "semantic_no_repo_median",
    "repo_median": "semantic_repo_median",
    "repo_gate": "semantic_repo_gate",
}
CENTER_CANDIDATE = {"median": "repo_median", "gate": "repo_gate"}
METRIC_FIELDS = ("n", "within_25_rate", "mean_ape", "median_ape", "p95_ape", "max_ape")


def _read(name: str) -> dict[str, Any]:
    path = OUT / name
    if not path.is_file():
        raise FileNotFoundError(f"required retained review artifact missing: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"review artifact must be a JSON object: {path}")
    return dict(value)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _metrics(pairs: Sequence[tuple[Any, Any]]) -> dict[str, Any]:
    checked = [(_positive(pred, "prediction"), _positive(obs, "observation")) for pred, obs in pairs]
    if not checked:
        return {
            "n": 0,
            "within25": 0,
            "within_25_rate": None,
            "mean_ape": None,
            "median_ape": None,
            "p95_ape": None,
            "max_ape": None,
            "signed_error_total_ms": 0.0,
            "absolute_error_total_ms": 0.0,
            "observed_total_ms": 0.0,
            "wape": None,
            "signed_aggregate_bias": None,
            "all_within25": False,
        }
    errors = [pred - obs for pred, obs in checked]
    apes = [abs(error) / obs * 100.0 for error, (_pred, obs) in zip(errors, checked)]
    observed_total = sum(obs for _pred, obs in checked)
    signed_total = sum(errors)
    absolute_total = sum(abs(error) for error in errors)
    within = sum(ape <= GATE_PERCENT for ape in apes)
    return {
        "n": len(checked),
        "within25": within,
        "within_25_rate": within / len(checked),
        "mean_ape": sum(apes) / len(apes),
        "median_ape": statistics.median(apes),
        "p95_ape": _percentile(apes, 0.95),
        "max_ape": max(apes),
        "signed_error_total_ms": signed_total,
        "absolute_error_total_ms": absolute_total,
        "observed_total_ms": observed_total,
        "wape": absolute_total / observed_total,
        "signed_aggregate_bias": signed_total / observed_total,
        "all_within25": within == len(checked),
    }


def _metric_view(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value.get(field) for field in METRIC_FIELDS}


def _pairs(rows: Sequence[Mapping[str, Any]], prediction: str, observed: str) -> list[tuple[Any, Any]]:
    return [(row[prediction], row[observed]) for row in rows]


def _load_protocol(protocol: str, candidates: Sequence[str]) -> dict[str, dict[str, Any]]:
    loaded: dict[str, dict[str, Any]] = {}
    for label in candidates:
        candidate = CANDIDATE_FILES[label]
        payload = _read(f"predictions_{protocol}_{candidate}.json")
        if payload.get("protocol") != protocol or payload.get("candidate") != candidate:
            raise ValueError(f"prediction artifact identity mismatch: {protocol}/{candidate}")
        for field in ("tool_events", "gpu_events", "trajectories"):
            if not isinstance(payload.get(field), list):
                raise ValueError(f"prediction artifact missing list field {field}: {protocol}/{candidate}")
        loaded[label] = payload
    return loaded


def _paired_gate_minus_median_bootstrap(
    gate_records: Sequence[Mapping[str, Any]],
    median_records: Sequence[Mapping[str, Any]],
    *,
    seed: int = 20260908,
    reps: int = 1000,
    expected_clusters: int = 703,
) -> dict[str, Any]:
    """Bootstrap the paired primary-protocol gate-minus-median event rate.

    The resampling unit is the instance, matching the primary
    ``instance_id_grouped`` protocol.  Pass/fail is recomputed from the
    predictions and positive observations so this ancillary comparison does
    not inherit a stale ``within25`` field from an input artifact.
    """

    gate_by_id = {str(row["event_id"]): row for row in gate_records}
    median_by_id = {str(row["event_id"]): row for row in median_records}
    shared_ids = sorted(set(gate_by_id) & set(median_by_id))
    by_instance: dict[str, list[tuple[bool, bool]]] = defaultdict(list)

    def passes(row: Mapping[str, Any]) -> bool:
        prediction = _positive(row["predicted_ms"], "predicted_ms")
        observed = _positive(row["observed_ms"], "observed_ms")
        return abs(prediction - observed) / observed * 100.0 <= GATE_PERCENT

    for event_id in shared_ids:
        gate = gate_by_id[event_id]
        median = median_by_id[event_id]
        by_instance[str(gate.get("instance_id"))].append((passes(gate), passes(median)))
    clusters = sorted(by_instance)
    if expected_clusters is not None and len(clusters) != expected_clusters:
        raise ValueError(
            "primary paired gate-minus-median bootstrap expected "
            f"{expected_clusters} instance clusters, found {len(clusters)}"
        )
    if not clusters:
        return {
            "n_clusters": 0,
            "n_events": 0,
            "reps": reps,
            "seed": seed,
            "cluster_key": "instance_id",
            "comparison": "repo_gate_minus_repo_median",
            "resampling": "paired event booleans with replacement by instance cluster",
            "point_delta_gate_minus_median": None,
            "interval_95_percentile": [None, None],
        }

    def rate_delta(selected: Sequence[str]) -> float:
        gate_pass = sum(int(gate) for key in selected for gate, _median in by_instance[key])
        median_pass = sum(int(median) for key in selected for _gate, median in by_instance[key])
        count = sum(len(by_instance[key]) for key in selected)
        return (gate_pass - median_pass) / count

    point = rate_delta(clusters)
    rng = random.Random(seed)
    samples = [
        rate_delta([clusters[rng.randrange(len(clusters))] for _ in clusters])
        for _ in range(reps)
    ]
    return {
        "n_clusters": len(clusters),
        "n_events": len(shared_ids),
        "reps": reps,
        "seed": seed,
        "cluster_key": "instance_id",
        "comparison": "repo_gate_minus_repo_median",
        "resampling": "paired event booleans with replacement by instance cluster",
        "point_delta_gate_minus_median": point,
        "interval_95_percentile": [_percentile(samples, 0.025), _percentile(samples, 0.975)],
    }


def _class_metrics(rows: Sequence[Mapping[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return {
        key: _metric_view(_metrics(_pairs(group, "predicted_ms", "observed_ms")))
        for key, group in sorted(groups.items())
    }


def _tool_sum_metrics(trajectories: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result = _metrics(_pairs(trajectories, "predicted_tool_sum_ms", "observed_tool_sum_ms"))
    result["total_signed_bias_ms"] = result.pop("signed_error_total_ms")
    result["absolute_signed_aggregate_bias"] = abs(result["total_signed_bias_ms"]) / result["observed_total_ms"] if result["observed_total_ms"] else None
    result["wape"] = result.pop("wape")
    return result


def _e2e_metrics(trajectories: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: _metrics(_pairs(trajectories, prediction, "observed_e2e_ms"))
        for name, prediction in {
            "direct": "direct_predicted_e2e_ms",
            "overhead": "overhead_predicted_e2e_ms",
            "legacy": "legacy_predicted_e2e_ms",
        }.items()
    }


def _joint_gates(
    tools: Sequence[Mapping[str, Any]],
    gpu: Sequence[Mapping[str, Any]],
    trajectories: Sequence[Mapping[str, Any]],
    incomplete: set[str],
    source_incoherent: set[str],
) -> dict[str, Any]:
    tools_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    gpu_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in tools:
        tools_by_run[str(row["run_id"])].append(row)
    for row in gpu:
        gpu_by_run[str(row["run_id"])].append(row)
    run_ids = [str(row["run_id"]) for row in trajectories]
    channels = {
        "direct": "direct_predicted_e2e_ms",
        "overhead": "overhead_predicted_e2e_ms",
        "legacy": "legacy_predicted_e2e_ms",
    }
    result: dict[str, Any] = {}
    eligible = set(run_ids) - incomplete - source_incoherent
    ineligible = incomplete | source_incoherent
    def passes(row: Mapping[str, Any]) -> bool:
        return abs(float(row["predicted_ms"]) - float(row["observed_ms"])) / float(row["observed_ms"]) * 100.0 <= GATE_PERCENT
    for channel, prediction in channels.items():
        all_scored_pass: dict[str, bool] = {}
        events_only_pass: dict[str, bool] = {}
        for row in trajectories:
            run_id = str(row["run_id"])
            cpu_pass = bool(tools_by_run[run_id]) and all(passes(item) for item in tools_by_run[run_id])
            gpu_pass = bool(gpu_by_run[run_id]) and all(passes(item) for item in gpu_by_run[run_id])
            e2e_pass = abs(float(row[prediction]) - float(row["observed_e2e_ms"])) / float(row["observed_e2e_ms"]) * 100.0 <= GATE_PERCENT
            events_only_pass[run_id] = cpu_pass and gpu_pass
            all_scored_pass[run_id] = cpu_pass and gpu_pass and e2e_pass
        scored_n = len(run_ids)
        events_only_scored_pass = sum(events_only_pass.values())
        events_only_required_pass = sum(events_only_pass.get(run_id, False) for run_id in eligible)
        scored_pass = sum(all_scored_pass.values())
        required_pass = sum(all_scored_pass.get(run_id, False) for run_id in eligible)
        result[channel] = {
            "events_only_cpu_and_gpu": {
                "all_scored": {
                    "passing_runs": events_only_scored_pass,
                    "denominator_runs": scored_n,
                    "rate": events_only_scored_pass / scored_n if scored_n else None,
                },
                "all_required": {
                    "passing_runs": events_only_required_pass,
                    "eligible_runs": len(eligible),
                    "denominator_retained_runs": scored_n,
                    "rate_over_retained": events_only_required_pass / scored_n if scored_n else None,
                    "rate_over_eligible": events_only_required_pass / len(eligible) if eligible else None,
                    "incomplete_run_ids": sorted(incomplete),
                    "source_incoherent_run_ids": sorted(source_incoherent),
                    "coverage_ineligible_union_run_ids": sorted(ineligible),
                },
            },
            "all_scored": {
                "passing_runs": scored_pass,
                "denominator_runs": scored_n,
                "rate": scored_pass / scored_n if scored_n else None,
            },
            "all_required": {
                "passing_runs": required_pass,
                "eligible_runs": len(eligible),
                "denominator_retained_runs": scored_n,
                "rate_over_retained": required_pass / scored_n if scored_n else None,
                "rate_over_eligible": required_pass / len(eligible) if eligible else None,
                "incomplete_run_ids": sorted(incomplete),
                "source_incoherent_run_ids": sorted(source_incoherent),
                "coverage_ineligible_union_run_ids": sorted(ineligible),
            },
        }
    return result


def _source_incoherent_runs(diagnostics: Mapping[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    rows = diagnostics["source_conservation"]["closure_rows"]
    details = []
    runs: set[str] = set()
    for row in rows:
        delta = abs(float(row["observed_tool_sum_ms"]) - float(row["protocol_tool_ms"]))
        if delta > 0.001:
            run_id = str(row["run_id"])
            runs.add(run_id)
            details.append({"run_id": run_id, "delta_ms": delta})
    return runs, details


def _candidate_report(
    payload: Mapping[str, Any],
    incomplete: set[str],
    source_incoherent: set[str],
    bootstrap: Mapping[str, Any] | None,
) -> dict[str, Any]:
    tools = list(payload["tool_events"])
    gpu = list(payload["gpu_events"])
    trajectories = list(payload["trajectories"])
    cpu = _metrics(_pairs(tools, "predicted_ms", "observed_ms"))
    return {
        "cpu": {
            "overall": _metric_view(cpu),
            "original_class": _class_metrics(tools, "original_class"),
            "semantic_class": _class_metrics(tools, "semantic_class") if any(row.get("semantic_class") for row in tools) else {},
        },
        "tool_sum_per_trajectory": _tool_sum_metrics(trajectories),
        "gpu": _metric_view(_metrics(_pairs(gpu, "predicted_ms", "observed_ms"))),
        "e2e": {name: _metric_view(metric) for name, metric in _e2e_metrics(trajectories).items()},
        "joint_gates": _joint_gates(tools, gpu, trajectories, incomplete, source_incoherent),
        "source_consistent_e2e": {
            name: _metric_view(metric)
            for name, metric in _e2e_metrics(
                [row for row in trajectories if str(row["run_id"]) not in source_incoherent]
            ).items()
        },
        "paired_bootstrap_tool_within25_delta_vs_baseline": dict(bootstrap or {}),
    }


def build_report(center: str) -> tuple[str, dict[str, Any]]:
    if center not in CENTER_CANDIDATE:
        raise ValueError("center must be median or gate")
    summary = _read("comparison_summary.json")
    diagnostics = _read("cohort_diagnostics.json")
    coverage = summary["coverage_audit"]
    incomplete = {str(run_id) for run_id in coverage["incomplete_run_ids"]}
    source_incoherent, incoherent_details = _source_incoherent_runs(diagnostics)
    protocols: dict[str, Any] = {}
    primary_gate_minus_median: dict[str, Any] | None = None
    for protocol in PROTOCOLS:
        summary_protocol = summary["protocols"][protocol]
        available = [
            label
            for label, candidate in CANDIDATE_FILES.items()
            if candidate in summary_protocol["candidates"]
        ]
        if not available:
            raise ValueError(f"no candidate outputs for protocol {protocol}")
        loaded = _load_protocol(protocol, available)
        if protocol == "instance_id_grouped":
            if "repo_gate" not in loaded or "repo_median" not in loaded:
                raise ValueError(
                    "primary instance-grouped outputs must include repo_gate and repo_median"
                )
            primary_gate_minus_median = _paired_gate_minus_median_bootstrap(
                loaded["repo_gate"]["tool_events"],
                loaded["repo_median"]["tool_events"],
            )
        bootstrap = summary_protocol.get("paired_cluster_bootstrap_tool_within25_delta_vs_baseline", {})
        protocols[protocol] = {
            "available_candidates": available,
            "candidates": {
                label: _candidate_report(
                    loaded[label], incomplete, source_incoherent, bootstrap.get(CANDIDATE_FILES[label])
                )
                for label in available
            },
            "selected_center_candidate": CENTER_CANDIDATE[center]
            if CENTER_CANDIDATE[center] in available
            else None,
        }
    historical = {}
    for protocol in PROTOCOLS:
        notes = summary["protocols"][protocol].get("notes", {})
        if notes:
            historical[protocol] = notes.get("historical_baseline_reference")
    report = {
        "schema_version": "assignment.d9-cpu-review-report.v1",
        "chosen_center": center,
        "chosen_center_source": "root-selected; reporter does not select a model",
        "chosen_candidate": CENTER_CANDIDATE[center],
        "cohort": summary.get("cohort"),
        "coverage_audit": {
            "incomplete_run_ids": sorted(incomplete),
            "coverage_ineligible_union_run_ids": sorted(incomplete | source_incoherent),
            "n_retained_runs": coverage["n_retained_runs"],
            "eligible_run_ids_from_comparison_summary": sorted(
                str(run_id) for run_id in coverage["eligible_run_ids"]
            ),
        },
        "source_incoherent30_runs": {
            "threshold_ms": 0.001,
            "run_ids": sorted(source_incoherent),
            "details": incoherent_details,
            "coverage_aware_gate_ineligible": True,
        },
        "historical_73_2_reference": historical,
        "ancillary_decision_evidence": {
            "primary_instance_id_grouped_gate_minus_median_tool_within25": primary_gate_minus_median,
            "description": (
                "Paired gate-minus-median bootstrap on primary instance clusters; "
                "ancillary evidence only, not a model search or selection criterion."
            ),
        },
        "protocols": protocols,
        "artifact_links": {
            "systems_review": str(ROOT / "docs/D9_CPU_SYSTEMS_REVIEW.md"),
            "comparison_summary": "comparison_summary.json",
            "cohort_diagnostics": "cohort_diagnostics.json",
            "prediction_files": sorted(
                f"predictions_{protocol}_{CANDIDATE_FILES[candidate]}.json"
                for protocol, detail in protocols.items()
                for candidate in detail["available_candidates"]
            ),
        },
        "scientific_decision_boundary": "Scientific model selection and holdout recommendation are supplied by Astra/root; this reporter makes neither decision.",
    }
    return _markdown(report), report


def _pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100.0:.2f}%"


def _ape_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}%"


def _metric_cell(metric: Mapping[str, Any]) -> str:
    return "/".join(
        [_pct(metric.get("within_25_rate"))]
        + [_ape_pct(metric.get(field)) for field in ("mean_ape", "median_ape", "p95_ape", "max_ape")]
    )


def _gate_compact(value: Mapping[str, Any]) -> dict[str, Any]:
    """Render gate counts/rates without placing long run-ID lists in Markdown."""

    return {
        key: value.get(key)
        for key in (
            "passing_runs",
            "denominator_runs",
            "eligible_runs",
            "denominator_retained_runs",
            "rate",
            "rate_over_retained",
            "rate_over_eligible",
        )
        if key in value
    }


def _markdown(report: Mapping[str, Any]) -> str:
    center = report["chosen_center"]
    chosen = report["chosen_candidate"]
    lines = [
        f"# D9 retained-cohort CPU report — chosen center: root-selected `{center}`",
        "",
        "No model winner or holdout recommendation is made here; both are supplied by Astra/root.",
        "",
        "## Candidate comparison",
        "",
        "Metric cells are within25% / mean APE% / median APE% / p95 APE% / max APE%; rates use the fixed 25% gate.",
        "",
        "| Protocol | Candidate | CPU events | Tool-sum trajectories | GPU events | Direct E2E | Overhead E2E | Legacy E2E |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for protocol, detail in report["protocols"].items():
        for candidate in detail["available_candidates"]:
            item = detail["candidates"][candidate]
            lines.append(
                f"| {protocol} | {candidate} | {_metric_cell(item['cpu']['overall'])} | {_metric_cell(item['tool_sum_per_trajectory'])} | {_metric_cell(item['gpu'])} | {_metric_cell(item['e2e']['direct'])} | {_metric_cell(item['e2e']['overhead'])} | {_metric_cell(item['e2e']['legacy'])} |"
            )
    selected = report["protocols"]["instance_id_grouped"]["candidates"].get(chosen)
    lines += ["", "## Root-selected center: instance-grouped", ""]
    if selected is None:
        lines.append(f"Selected candidate `{chosen}` was not available in the instance-grouped outputs.")
    else:
        lines += [
            f"Candidate: `{chosen}`; this is a root-selected center comparison, not an automatic winner.",
            "",
            "### Original-class CPU metrics",
            "",
            "| Class | n | within25% | mean APE% | median APE% | p95 APE% | max APE% |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for cls, metric in selected["cpu"]["original_class"].items():
            lines.append(
                f"| {cls} | {metric['n']} | {_pct(metric['within_25_rate'])} | {_ape_pct(metric['mean_ape'])} | {_ape_pct(metric['median_ape'])} | {_ape_pct(metric['p95_ape'])} | {_ape_pct(metric['max_ape'])} |"
            )
        lines += [
            "",
            "Semantic-class CPU metrics:",
            "",
            "| Class | n | within25% | mean APE% | median APE% | p95 APE% | max APE% |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for cls, metric in selected["cpu"]["semantic_class"].items():
            lines.append(
                f"| {cls} | {metric['n']} | {_pct(metric['within_25_rate'])} | {_ape_pct(metric['mean_ape'])} | {_ape_pct(metric['median_ape'])} | {_ape_pct(metric['p95_ape'])} | {_ape_pct(metric['max_ape'])} |"
            )
        lines += [
            "",
            "### Tool-sum, GPU, and E2E",
            "",
            f"Tool-sum: `{json.dumps(selected['tool_sum_per_trajectory'], sort_keys=True)}`; signed bias is total predicted−observed ms, absolute signed aggregate bias is its absolute value / observed total, and WAPE is sum absolute errors / sum observed.",
            f"Paired bootstrap tool within25 delta versus baseline: `{json.dumps(selected['paired_bootstrap_tool_within25_delta_vs_baseline'], sort_keys=True)}`",
            f"Ancillary primary instance-grouped gate-minus-median bootstrap (703 clusters, 1,000 repetitions, seed 20260908): `{json.dumps(report['ancillary_decision_evidence']['primary_instance_id_grouped_gate_minus_median_tool_within25'], sort_keys=True)}`",
            f"GPU: `{json.dumps(selected['gpu'], sort_keys=True)}`",
            f"E2E direct: `{json.dumps(selected['e2e']['direct'], sort_keys=True)}`",
            f"E2E overhead-aware: `{json.dumps(selected['e2e']['overhead'], sort_keys=True)}`",
            f"E2E legacy: `{json.dumps(selected['e2e']['legacy'], sort_keys=True)}`",
            "",
            "### Independently recomputed per-run joint gates",
            "",
            "The conjunction is computed per run (CPU all-events AND GPU all-events AND channel E2E), never as a product of marginal rates.",
            "",
        ]
        for channel, gate in selected["joint_gates"].items():
            lines.append(
                f"- `{channel}` CPU+GPU events only — all-scored: "
                f"`{json.dumps(_gate_compact(gate['events_only_cpu_and_gpu']['all_scored']), sort_keys=True)}`; "
                f"all-required: `{json.dumps(_gate_compact(gate['events_only_cpu_and_gpu']['all_required']), sort_keys=True)}` "
                "(run-ID details are in the JSON sidecar)."
            )
            lines.append(
                f"- `{channel}` CPU+GPU events plus E2E — all-scored: "
                f"`{json.dumps(_gate_compact(gate['all_scored']), sort_keys=True)}`; "
                f"all-required: `{json.dumps(_gate_compact(gate['all_required']), sort_keys=True)}` "
                "(run-ID details are in the JSON sidecar)."
            )
        lines += [
            "",
            "### Source-consistent E2E sensitivity",
            "",
            "No refitting is performed; these are the same predictions restricted to runs where observed tool sum and protocol tool time agree within 0.001 ms.",
            "",
        ]
        for channel, metric in selected["source_consistent_e2e"].items():
            lines.append(f"- `{channel}`: `{json.dumps(metric, sort_keys=True)}`")
    lines += [
        "",
        "## Coverage and provenance",
        "",
        f"Coverage audit: {len(report['coverage_audit']['incomplete_run_ids'])} incomplete runs; {len(report['source_incoherent30_runs']['run_ids'])} source-incoherent runs; {len(report['coverage_audit']['coverage_ineligible_union_run_ids'])} ineligible in their union. Full IDs are in the JSON sidecar.",
        "Historical 73.2% and matched-cohort baseline are retained as references; candidate comparisons are not independent confirmation after model selection.",
        "",
        f"Systems review: [{report['artifact_links']['systems_review']}]({report['artifact_links']['systems_review']}); artifacts: `{OUT}` ({report['artifact_links']['comparison_summary']}, {report['artifact_links']['cohort_diagnostics']}, {len(report['artifact_links']['prediction_files'])} prediction files).",
        "",
        "Scientific model selection and holdout recommendation are supplied by Astra/root; this report does not choose either.",
        "",
        "No holdout/GPU work was performed by this reporter; prior artifacts are preserved.",
    ]
    return "\n".join(lines) + "\n"


def _write_with_sha256(path: Path, content: str) -> None:
    """Write a UTF-8 artifact and its adjacent checksum sidecar."""

    import hashlib

    payload = content.encode("utf-8")
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    path.with_name(path.name + ".sha256").write_text(
        f"{digest}  {path.name}\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--center", choices=("median", "gate"), required=True)
    parser.add_argument("--output", type=Path, required=True, help="Markdown report path")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing report and sidecar")
    args = parser.parse_args(argv)
    sidecar_path = args.output.with_suffix(".json")
    checksum_paths = (
        args.output.with_name(args.output.name + ".sha256"),
        sidecar_path.with_name(sidecar_path.name + ".sha256"),
    )
    if not args.overwrite and (
        args.output.exists()
        or sidecar_path.exists()
        or any(path.exists() for path in checksum_paths)
    ):
        raise FileExistsError(f"refusing to overwrite report; pass --overwrite: {args.output}")
    markdown, sidecar = build_report(args.center)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write_with_sha256(args.output, markdown)
    sidecar_text = json.dumps(sidecar, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_with_sha256(sidecar_path, sidecar_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
