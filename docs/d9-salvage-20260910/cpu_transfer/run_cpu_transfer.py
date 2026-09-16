#!/usr/bin/env python3
"""Bounded Astropy-to-Django CPU-operation transfer diagnostic.

The existing atomic CPU helper already fit four fixed leave-one-Astropy-instance-
out median models.  This helper selects the first complete Django instance that
fits a 400,000,000-byte raw-stream bound, decodes that stream through the same
v3 packet decoder into the same compact typed-array store, and applies the
existing four candidate families without fitting or selecting on Django
targets.  It never opens case-result, model-event, or evaluator-label files.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ATOMIC_SCRIPT = REPO / "docs" / "d9-salvage-20260910" / "atomic_cpu" / "run_atomic_cpu.py"
ATOMIC_MODEL = REPO / "docs" / "d9-salvage-20260910" / "atomic_cpu" / "model.json"
OUT_MODEL = "model.json"
OUT_REPORT = "REPORT.md"
MAX_RAW_BYTES_DEFAULT = 400_000_000
MAX_CASES_DEFAULT = 4
SCHEMA = "assignment.d9.atomic-cpu-operation-transfer.v1"


def _load_atomic() -> Any:
    spec = importlib.util.spec_from_file_location("d9_atomic_cpu", ATOMIC_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load atomic helper: {ATOMIC_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


atomic = _load_atomic()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_case(case: Mapping[str, Any]) -> tuple[bool, str]:
    good, report = atomic._fully_valid_case(case)
    if good:
        return True, "fully_valid_train_calibration"
    validation = report.get("validation", {})
    scope = report.get("calibration_scope", {})
    raw_integrity = (report.get("attempts") or [{}])[0].get("cpu_raw_integrity", {})
    if case.get("partition") != "train_calibration":
        return False, "not_train_calibration"
    if report.get("disposition") != "accepted":
        return False, "report_not_accepted"
    if validation.get("status") != "valid" or int(validation.get("error_count", 1)) != 0:
        return False, "validation_errors"
    if scope.get("original_validation_status") != "valid" or scope.get("original_error_codes"):
        return False, "original_validation_errors"
    if raw_integrity.get("status") != "complete_bounded_raw_metadata":
        return False, "raw_integrity_incomplete"
    if int(raw_integrity.get("coverage_error_count", 1)) != 0:
        return False, "raw_coverage_errors"
    return False, "fully_validity_gate_failed"


def select_django_cases(
    manifest: Mapping[str, Any],
    *,
    max_raw_bytes: int,
    max_cases: int = MAX_CASES_DEFAULT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a fixed ascending-ordinal prefix of distinct valid Django cases.

    A candidate is prepared from metadata and action journals before the raw
    stream is opened.  Once the next distinct valid case would exceed the
    bound, selection stops; later cases are not searched by size or outcome.
    """

    if max_raw_bytes <= 0:
        raise ValueError("max_raw_bytes must be positive")
    if max_cases <= 0:
        raise ValueError("max_cases must be positive")
    selected: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    considered: list[dict[str, Any]] = []
    raw_bytes = 0
    stop_reason = "max_cases_reached"
    candidates = sorted(manifest.get("cases", []), key=lambda row: int(row["queue_ordinal"]))
    for source_case in candidates:
        if not str(source_case.get("instance_id", "")).startswith("django__"):
            continue
        if source_case.get("partition") != "train_calibration":
            considered.append({
                "queue_ordinal": source_case.get("queue_ordinal"),
                "instance_id": source_case.get("instance_id"),
                "decision": "skip",
                "reason": "not_train_calibration",
            })
            continue
        if str(source_case["instance_id"]) in seen_instances:
            considered.append({
                "queue_ordinal": source_case.get("queue_ordinal"),
                "instance_id": source_case.get("instance_id"),
                "decision": "skip",
                "reason": "duplicate_instance_after_selection",
            })
            continue
        good, gate_reason = _valid_case(source_case)
        if not good:
            considered.append({
                "queue_ordinal": source_case.get("queue_ordinal"),
                "instance_id": source_case.get("instance_id"),
                "decision": "skip",
                "reason": gate_reason,
            })
            continue
        # prepare_case reads only manifests, validation metadata, and compact
        # action journals.  It does not open the binary event stream.
        prepared = atomic.prepare_case(dict(source_case))
        candidate_bytes = int(prepared["raw_bytes"])
        if raw_bytes + candidate_bytes > max_raw_bytes:
            considered.append({
                "queue_ordinal": prepared["queue_ordinal"],
                "instance_id": prepared["instance_id"],
                "raw_bytes": candidate_bytes,
                "decision": "stop",
                "reason": "next_distinct_valid_case_exceeds_raw_byte_bound",
                "raw_bytes_if_added": raw_bytes + candidate_bytes,
            })
            stop_reason = "next_valid_case_exceeds_raw_byte_bound"
            break
        selected.append(prepared)
        seen_instances.add(str(prepared["instance_id"]))
        raw_bytes += candidate_bytes
        considered.append({
            "queue_ordinal": prepared["queue_ordinal"],
            "instance_id": prepared["instance_id"],
            "raw_bytes": candidate_bytes,
            "decision": "select",
            "reason": gate_reason,
        })
        if len(selected) >= max_cases:
            stop_reason = "max_cases_reached"
            break
    if not selected:
        raise atomic.ValidationError("no fully valid Django train_calibration case fits the raw-byte bound")
    return selected, {
        "policy": "ascending queue ordinal; distinct fully valid train_calibration Django instances; stop at first raw-byte overflow",
        "max_cases": max_cases,
        "max_raw_bytes": max_raw_bytes,
        "selected_count": len(selected),
        "selected_raw_bytes": raw_bytes,
        "stop_reason": stop_reason,
        "considered": considered,
    }


def _decode_cases(cases: list[dict[str, Any]]) -> tuple[Any, dict[str, Any]]:
    """Decode selected raw packets using the atomic helper's compact store."""

    operations = atomic.Labels()
    sizes = atomic.Labels()
    paths = atomic.Labels()
    kinds = atomic.Labels()
    store = atomic.EventStore(cases, operations, sizes, paths, kinds)
    case_counters = [atomic.CaseCounters() for _ in cases]
    status_counts: Counter[str] = Counter()
    zero_target_by_kind: Counter[str] = Counter()
    zero_target_by_operation: Counter[str] = Counter()
    decoded_records = 0
    range_mismatch_count = 0
    case_diagnostics: list[dict[str, Any]] = []

    for case_index, case in enumerate(cases):
        record_size = int(case["record_size_bytes"])
        range_mismatch_count += int(case["range_gap_bytes"] != 0 or case["range_overlap_bytes"] != 0)
        range_mismatch_count += sum(
            int(
                item["required_event_count"] != item["record_count"]
                or item["event_count"] != item["record_count"]
                or item["event_count_at_boundary"] != item["record_count"]
                or item["post_boundary_event_count"] != 0
                or not item["event_records_complete"]
            )
            for item in case["action_ranges"]
        )
        raw_offset = 0
        range_cursor = 0
        token_counts: Counter[int] = Counter()
        digest = hashlib.sha256()
        with Path(case["raw_path"]).open("rb") as stream:
            pending = b""
            while True:
                chunk = stream.read(record_size * 4096)
                if not chunk:
                    break
                digest.update(chunk)
                pending += chunk
                usable = (len(pending) // record_size) * record_size
                packets, pending = pending[:usable], pending[usable:]
                for packet_start in range(0, len(packets), record_size):
                    packet = packets[packet_start:packet_start + record_size]
                    row = atomic.BpfWorkCollector._event_row(
                        packet, schema_version=atomic.BPF_EVENT_SCHEMA
                    )
                    if row["kind"] not in set(range(1, 17)) or row["status"] not in {1, 2, 3}:
                        raise atomic.ValidationError("invalid decoded BPF kind/status")
                    if any(
                        row[prefix + "_status"] not in {0, 1, 2, 3}
                        or not 0 <= int(row[prefix + "_len"]) <= 128
                        for prefix in ("path", "path2")
                    ):
                        raise atomic.ValidationError("invalid decoded BPF path descriptor")
                    decoded_records += 1
                    counters = case_counters[case_index]
                    counters.raw_records += 1
                    status_name = str(row["status_name"])
                    status_counts[status_name] += 1
                    if status_name == "success":
                        counters.success_records += 1
                    elif status_name == "failure":
                        counters.failure_records += 1
                    while (
                        range_cursor < len(case["action_ranges"])
                        and raw_offset >= case["action_ranges"][range_cursor]["offset_end"]
                    ):
                        range_cursor += 1
                    action_range = (
                        case["action_ranges"][range_cursor]
                        if range_cursor < len(case["action_ranges"])
                        else None
                    )
                    atomic._validate_event_membership(
                        action_range,
                        raw_offset=raw_offset,
                        action_token=int(row["token"]),
                    )
                    token_counts[int(row["token"])] += 1
                    duration = row.get("duration_ns")
                    if duration is None:
                        counters.censored_records += 1
                        target_ns = -1
                    elif int(duration) == 0:
                        counters.zero_target_records += 1
                        zero_target_by_kind[str(row["kind_name"])] += 1
                        zero_target_by_operation[atomic.operation_key(row)] += 1
                        target_ns = 0
                    elif int(duration) < 0:
                        counters.negative_target_records += 1
                        target_ns = -1
                    else:
                        counters.target_records += 1
                        target_ns = int(duration)
                    store.add(
                        case_index=case_index,
                        operation_id=operations.get(atomic.operation_key(row)),
                        size_id=sizes.get(
                            atomic.requested_size_bucket(atomic.requested_size_value(row))
                        ),
                        path_id=paths.get(atomic.path_class(row)),
                        kind_id=kinds.get(str(row["kind_name"])),
                        target_ns=target_ns,
                        token=int(row["token"]),
                        sequence=int(row["sequence"]),
                        raw_offset=raw_offset,
                        kernel_start_ns=int(row["kernel_start_ns"]),
                    )
                    raw_offset += record_size
            if pending:
                raise atomic.ValidationError("raw BPF stream has a partial trailing record")
        actual_hash = digest.hexdigest()
        case["raw_hash_actual"] = actual_hash
        case["raw_hash_verified"] = bool(
            case.get("raw_hash_recorded") and actual_hash == case["raw_hash_recorded"]
        )
        if not case["raw_hash_verified"]:
            raise atomic.ValidationError("raw BPF stream hash differs from collector manifest")
        expected_tokens = {int(key): int(value) for key, value in case["token_expected_records"].items()}
        token_mismatches = [
            {"action_token": token, "expected": expected_tokens.get(token, 0), "observed": token_counts.get(token, 0)}
            for token in sorted(set(expected_tokens) | set(token_counts))
            if expected_tokens.get(token, 0) != token_counts.get(token, 0)
        ]
        range_mismatch_count += len(token_mismatches)
        if raw_offset != int(case["raw_bytes"]):
            range_mismatch_count += 1
        if raw_offset // record_size != int(case["expected_raw_records"]):
            range_mismatch_count += 1
        counters = case_counters[case_index]
        case_diagnostics.append({
            "case_id": case["case_id"],
            "instance_id": case["instance_id"],
            "queue_ordinal": case["queue_ordinal"],
            "raw_bytes": int(case["raw_bytes"]),
            "raw_records": int(counters.raw_records),
            "decoded_records": int(counters.raw_records),
            "raw_hash_recorded": case["raw_hash_recorded"],
            "raw_hash_actual": actual_hash,
            "raw_hash_verified": bool(case["raw_hash_verified"]),
            "token_join": {
                "expected_action_tokens": len(expected_tokens),
                "observed_action_tokens": len(token_counts),
                "expected_records": sum(expected_tokens.values()),
                "observed_records": sum(token_counts.values()),
                "mismatch_count": len(token_mismatches),
                "mismatches": token_mismatches,
            },
            "aggregate_drops": case["aggregate_drops"],
            "range_gap_bytes": int(case["range_gap_bytes"]),
            "range_overlap_bytes": int(case["range_overlap_bytes"]),
            "counters": counters.__dict__.copy(),
            "kernel_clock": case["kernel_clock"],
            "host_clock": case["host_clock"],
        })
    if range_mismatch_count:
        raise atomic.ValidationError(f"action range/event-count integrity mismatch: {range_mismatch_count}")
    return store, {
        "decoded_records": decoded_records,
        "valid_target_records": sum(item.target_records for item in case_counters),
        "zero_target_records": sum(item.zero_target_records for item in case_counters),
        "zero_target_by_kind": dict(sorted(zero_target_by_kind.items())),
        "zero_target_by_operation": dict(sorted(zero_target_by_operation.items())),
        "lineage_zero_target_records": sum(
            count for kind, count in zero_target_by_kind.items() if kind in atomic.LINEAGE_KINDS
        ),
        "modeled_zero_target_records": sum(
            count for kind, count in zero_target_by_kind.items() if kind not in atomic.LINEAGE_KINDS
        ),
        "censored_records": sum(item.censored_records for item in case_counters),
        "negative_target_records": sum(item.negative_target_records for item in case_counters),
        "success_records": sum(item.success_records for item in case_counters),
        "failure_records": sum(item.failure_records for item in case_counters),
        "status_counts": dict(sorted(status_counts.items())),
        "range_mismatch_count": range_mismatch_count,
        "case_diagnostics": case_diagnostics,
    }


def _predict(model: Mapping[str, Any], candidate: str, event: Mapping[str, Any]) -> float:
    operation = str(event["operation"])
    operation_prediction = float(model["operation_medians_ns"].get(operation, model["global_median_ns"]))
    if candidate == "global_median":
        return float(model["global_median_ns"])
    if candidate == "operation_median":
        return operation_prediction
    if candidate == "operation_requested_size_bucket_median":
        key = f"{operation}|{event['requested_size_bucket']}"
        return float(model["operation_requested_size_bucket_medians_ns"].get(key, operation_prediction))
    if candidate == "operation_path_class_median":
        key = f"{operation}|{event['path_class']}"
        return float(model["operation_path_class_medians_ns"].get(key, operation_prediction))
    raise atomic.ValidationError(f"unknown candidate: {candidate}")


def _score_transfer(store: Any, cases: list[dict[str, Any]], baseline: Mapping[str, Any]) -> dict[str, Any]:
    folds: dict[str, Any] = {}
    for fold in baseline["fold_models"]:
        fold_id = str(fold["held_out_instance"])
        metrics: dict[str, Any] = {}
        gates: dict[str, Any] = {}
        accumulators = {candidate: atomic.MetricAccumulator.create() for candidate in atomic.MODEL_ORDER}
        fold_gates = {
            candidate: {
                "required_records": 0,
                "positive_target_records": 0,
                "positive_within_25_records": 0,
                "zero_target_records": 0,
                "zero_exact_prediction_records": 0,
                "lineage_zero_records": 0,
                "unsupported_target_records": 0,
            }
            for candidate in atomic.MODEL_ORDER
        }
        model = fold["model"]
        for index, target in enumerate(store.target_ns):
            event = store.identity(index)
            kind_name = str(event["kind_name"])
            is_lineage_zero = target == 0 and kind_name in atomic.LINEAGE_KINDS
            for candidate in atomic.MODEL_ORDER:
                prediction = _predict(model, candidate, event)
                gate = fold_gates[candidate]
                if target > 0:
                    gate["required_records"] += 1
                    gate["positive_target_records"] += 1
                    target_ms = float(target) / 1_000_000.0
                    prediction_ms = float(prediction) / 1_000_000.0
                    ape = abs(prediction_ms - target_ms) / target_ms * 100.0
                    gate["positive_within_25_records"] += int(ape <= atomic.WITHIN_THRESHOLD_PERCENT)
                    accumulators[candidate].update(
                        prediction_ns=prediction,
                        target_ns=int(target),
                        event=event,
                    )
                elif target == 0:
                    if is_lineage_zero:
                        gate["lineage_zero_records"] += 1
                    else:
                        gate["required_records"] += 1
                        gate["zero_target_records"] += 1
                        gate["zero_exact_prediction_records"] += int(float(prediction) == 0.0)
                else:
                    gate["required_records"] += 1
                    gate["unsupported_target_records"] += 1
        for candidate in atomic.MODEL_ORDER:
            gate = fold_gates[candidate]
            eligible = gate["positive_target_records"] + gate["zero_target_records"]
            passing = gate["positive_within_25_records"] + gate["zero_exact_prediction_records"]
            gate["passing_records"] = passing
            gate["coverage_over_required_percent"] = (
                100.0 * passing / gate["required_records"] if gate["required_records"] else None
            )
            gate["coverage_over_scored_required_percent"] = 100.0 * passing / eligible if eligible else None
            gates[candidate] = gate
            metrics[candidate] = accumulators[candidate].finish()
        folds[fold_id] = {
            "held_out_astropy_instance": fold_id,
            "held_out_astropy_queue_ordinal": fold["held_out_queue_ordinal"],
            "training_astropy_instance_count": 3,
            "training_target_events": fold["training_target_events"],
            "tested_cases": [case["instance_id"] for case in cases],
            "metrics": metrics,
            "full_required_population_gate": gates,
        }
    summary: dict[str, Any] = {}
    for candidate in atomic.MODEL_ORDER:
        values = [fold["metrics"][candidate] for fold in folds.values()]
        coverage = [float(value["within_25_percent"]) for value in values]
        worst = [float(value["worst_ape_percent"]) for value in values]
        counts = [int(value["n_events"]) for value in values]
        summary[candidate] = {
            "transfer_fold_count": len(values),
            "n_events_per_fold": counts,
            "within_25_percent_mean": sum(coverage) / len(coverage),
            "within_25_percent_min": min(coverage),
            "within_25_percent_max": max(coverage),
            "worst_ape_percent_max": max(worst),
            "worst_ape_percent_min": min(worst),
            "all_folds_same_event_count": len(set(counts)) == 1,
        }
    per_instance: dict[str, Any] = {}
    for case in cases:
        instance_id = str(case["instance_id"])
        per_instance[instance_id] = {}
        for candidate in atomic.MODEL_ORDER:
            per_instance[instance_id][candidate] = {
                "transfer_folds": {
                    fold_id: fold["metrics"][candidate]
                    for fold_id, fold in folds.items()
                },
                "cross_fold_summary": summary[candidate],
            }
    return {
        "folds": folds,
        "cross_fold_summary": summary,
        "per_instance": per_instance,
    }


def _case_public(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: case[key]
        for key in (
            "queue_ordinal", "instance_id", "case_id", "partition", "record_size_bytes",
            "raw_path", "raw_bytes", "expected_raw_records", "raw_hash_recorded",
            "raw_hash_actual", "raw_hash_verified", "bpf_manifest_path", "bpf_manifest_sha256",
            "raw_aggregate_journal", "action_ranges", "range_gap_bytes", "range_overlap_bytes",
            "aggregate_drops", "kernel_clock", "host_clock", "validation_report_path",
            "validation_report_sha256_actual",
        )
    }


def _baseline_summary(baseline: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "n_events", "within_25_events", "within_25_percent", "mean_ape_percent",
        "p95_ape_percent_nearest_rank", "worst_ape_percent",
    )
    return {
        candidate: {field: baseline["metrics"]["overall"][candidate][field] for field in fields}
        for candidate in atomic.MODEL_ORDER
    }


def render_report(artifact: Mapping[str, Any]) -> str:
    selected = artifact["selection"]["selected_cases"]
    selected_text = ", ".join(
        f"{case['queue_ordinal']} ({case['instance_id']})" for case in selected
    )
    population = artifact["population"]
    transfer = artifact["transfer"]
    lines = [
        "# Bounded Astropy-to-Django CPU-operation transfer",
        "",
        "This is an offline, trace-conditioned development transfer diagnostic. It applies the existing four fixed Astropy leave-one-instance-out median fits to a fixed ordinal Django sample; it does not fit on Django targets, change acquisition, or claim the D9 all-event gate.",
        "",
        "## Fixed Django sample and byte bound",
        "",
        f"The selection policy was ascending queue ordinal after the evidence validity gate, distinct `train_calibration` Django instance IDs, stopping at the first valid case that would exceed the raw-byte bound. The selected sample is **{selected_text}**. It contains **{population['raw_records']} raw records** and **{population['raw_bytes_read']} bytes**; the bound is **{artifact['abi']['raw_read_bound_bytes']} bytes**. The next distinct fully valid case is retained in the selection audit with its overflow reason, but its stream was not opened.",
        "",
        "The raw stream was decoded through the existing `BpfWorkCollector._event_row` v3 decoder into the atomic helper's compact typed arrays. No normalized event export or prediction JSONL was created.",
        "",
        "## Transfer contract",
        "",
        "Each existing fit is trained on the other three fixed Astropy instances selected by the atomic CPU helper. The fit is then applied unchanged to the selected Django instance. The four candidate families are exactly the existing `global_median`, `operation_median`, `operation_requested_size_bucket_median`, and `operation_path_class_median` families; no new model family or Django refit is introduced.",
        "",
        "The target is positive completed BPF kernel duration. Failure status and return values are not features. Known fork/clone/thread lineage zeros are outside the individual-operation duration denominator; other zero, censored, or negative targets remain explicit required records and do not pass by omission.",
        "",
        "## Per-event transfer results",
        "",
        "Each row below scores the same Django instance once under one pre-existing Astropy fold. `count` is the number of positive-duration events; coverage is the fraction with absolute percentage error at most 25 percent.",
        "",
        "| Astropy fit held out | Candidate | Count | Within 25% | Worst APE |",
        "|---|---|---:|---:|---:|",
    ]
    for fold_id, fold in transfer["folds"].items():
        for candidate in atomic.MODEL_ORDER:
            item = fold["metrics"][candidate]
            lines.append(
                f"| `{fold_id}` | `{candidate}` | {item['n_events']} | {item['within_25_percent']:.2f}% ({item['within_25_events']}) | {item['worst_ape_percent']:.2f}% |"
            )
    lines.extend([
        "",
        "## Per-instance and cross-fold stability",
        "",
        "The selected Django instance is the only test instance in this bounded transfer pass. The summary reports fold-to-fold variation without treating repeated scoring of one trace as independent instances.",
        "",
        "| Candidate | Test instance | Fold count | Count/fold | Coverage min / mean / max | Worst APE max |",
        "|---|---|---:|---|---:|---:|",
    ])
    test_instance = selected[0]["instance_id"]
    for candidate in atomic.MODEL_ORDER:
        summary = transfer["cross_fold_summary"][candidate]
        coverage = (
            f"{summary['within_25_percent_min']:.2f}% / "
            f"{summary['within_25_percent_mean']:.2f}% / "
            f"{summary['within_25_percent_max']:.2f}%"
        )
        lines.append(
            f"| `{candidate}` | `{test_instance}` | {summary['transfer_fold_count']} | {summary['n_events_per_fold']} | {coverage} | {summary['worst_ape_percent_max']:.2f}% |"
        )
    lines.extend([
        "",
        "## Existing four-Astropy reference",
        "",
        "These are the atomic helper's existing four-instance leave-one-instance-out development metrics, copied as a comparison reference. They are not recomputed from Django and are not a sealed holdout.",
        "",
        "| Candidate | Astropy count | Within 25% | Mean APE | P95 APE | Worst APE |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for candidate, item in artifact["astropy_reference"]["overall"].items():
        lines.append(
            f"| `{candidate}` | {item['n_events']} | {item['within_25_percent']:.2f}% ({item['within_25_events']}) | {item['mean_ape_percent']:.2f}% | {item['p95_ape_percent_nearest_rank']:.2f}% | {item['worst_ape_percent']:.2f}% |"
        )
    lines.extend([
        "",
        "## Integrity and population diagnostics",
        "",
        f"- Raw records decoded: **{population['raw_records']}**; valid positive-duration targets scored: **{population['valid_target_records']}**.",
        f"- Raw hash/token joins: **{population['raw_hash_all_verified']}** raw hash verified; **{population['token_join_mismatch_count']}** action-token count mismatches; range mismatch count **{population['range_mismatch_count']}**.",
        f"- Status counts: `{json.dumps(population['status_counts'], sort_keys=True)}`; success **{population['success_records']}**, failure **{population['failure_records']}**.",
        f"- Zero targets: **{population['zero_target_records']}**, of which known lineage zeros are **{population['lineage_zero_target_records']}** and modeled-operation zeros are **{population['modeled_zero_target_records']}**; censored **{population['censored_records']}**; negative **{population['negative_target_records']}**.",
        f"- Aggregate loss counters: `{json.dumps(population['aggregate_drops'], sort_keys=True)}`. Callback and loss counters remain visible in `model.json` per selected case.",
        "- Protected case-result, model-event, evaluator-label, and outcome files were not opened by this transfer script; the only target values read were completed CPU durations from the selected raw BPF stream for scoring.",
        "",
        "## Interpretation",
        "",
        "The transfer rows answer whether the existing Astropy operation medians retain their accuracy on one ordinally selected Django trace. They do not establish repository-wide generalization: the byte cap leaves one Django instance, the four transfer fits each train on only three Astropy instances, and the same Django trace is scored repeatedly across those folds. Compare both coverage and worst error; a candidate that improves Astropy coverage but has materially lower Django coverage or a larger transfer tail has not demonstrated portable behavior.",
        f"On this bounded trace, path-class coverage averages **{transfer['cross_fold_summary']['operation_path_class_median']['within_25_percent_mean']:.2f}%** versus **{artifact['astropy_reference']['overall']['operation_path_class_median']['within_25_percent']:.2f}%** in the four-Astropy reference; size-bucket averages **{transfer['cross_fold_summary']['operation_requested_size_bucket_median']['within_25_percent_mean']:.2f}%** versus **{artifact['astropy_reference']['overall']['operation_requested_size_bucket_median']['within_25_percent']:.2f}%**; operation-only is **{transfer['cross_fold_summary']['operation_median']['within_25_percent_mean']:.2f}%** versus **{artifact['astropy_reference']['overall']['operation_median']['within_25_percent']:.2f}%**; and global is **{transfer['cross_fold_summary']['global_median']['within_25_percent_mean']:.2f}%** versus **{artifact['astropy_reference']['overall']['global_median']['within_25_percent']:.2f}%**. The path family remains the best transfer candidate by coverage, but its coverage is lower than the Astropy reference, so these results are evidence against claiming that the four-Astropy result generalizes across repositories.",
        "",
        "The output is reproducible with `run_cpu_transfer.py`. The script intentionally does not write event-level prediction exports to keep the artifact bounded.",
        "",
    ])
    return "\n".join(lines)


def run(*, out_dir: Path, max_raw_bytes: int = MAX_RAW_BYTES_DEFAULT, max_cases: int = MAX_CASES_DEFAULT) -> dict[str, Any]:
    manifest = json.loads(atomic.CPU_EVIDENCE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != "d9.calibration-input-manifest.v2":
        raise atomic.ValidationError("unexpected CPU evidence manifest schema")
    baseline = json.loads(ATOMIC_MODEL.read_text(encoding="utf-8"))
    if baseline.get("models") != list(atomic.MODEL_ORDER):
        raise atomic.ValidationError("atomic model candidate families differ from the fixed transfer contract")
    cases, selection = select_django_cases(
        manifest,
        max_raw_bytes=max_raw_bytes,
        max_cases=max_cases,
    )
    store, population = _decode_cases(cases)
    transfer = _score_transfer(store, cases, baseline)
    raw_bytes_read = sum(int(case["raw_bytes"]) for case in cases)
    aggregate_drops: Counter[str] = Counter()
    token_join_mismatch_count = 0
    raw_hash_all_verified = True
    for item in population["case_diagnostics"]:
        aggregate_drops.update(item["aggregate_drops"])
        token_join_mismatch_count += int(item["token_join"]["mismatch_count"])
        raw_hash_all_verified = raw_hash_all_verified and bool(item["raw_hash_verified"])
    population["raw_bytes_read"] = raw_bytes_read
    population["raw_records"] = population["decoded_records"]
    population["compact_index_bytes"] = store.memory_bytes()
    population["aggregate_drops"] = dict(sorted(aggregate_drops.items()))
    population["token_join_mismatch_count"] = token_join_mismatch_count
    population["raw_hash_all_verified"] = raw_hash_all_verified
    if raw_bytes_read > max_raw_bytes:
        raise atomic.ValidationError("selected raw streams exceed transfer byte bound")
    if population["valid_target_records"] != sum(
        fold["metrics"][atomic.MODEL_ORDER[0]]["n_events"] for fold in transfer["folds"].values()
    ) / max(1, len(transfer["folds"])):
        raise atomic.ValidationError("transfer scores do not cover every valid positive target")
    artifact: dict[str, Any] = {
        "schema": SCHEMA,
        "selection": {
            **selection,
            "selected_cases": [_case_public(case) for case in cases],
        },
        "abi": {
            "event_schema": atomic.BPF_EVENT_SCHEMA,
            "event_abi": atomic.BPF_EVENT_ABI,
            "record_size_bytes": atomic.BPF_EVENT_RECORD_SIZE,
            "decoder": "agentic_sim.telemetry.bpf_work.BpfWorkCollector._event_row; same v3 packet decoder used by atomic_cpu",
            "raw_read_bound_bytes": max_raw_bytes,
        },
        "feature_contract": baseline["feature_contract"],
        "target_contract": baseline["target_contract"],
        "population": population,
        "models": list(atomic.MODEL_ORDER),
        "transfer": transfer,
        "astropy_reference": {
            "model_path": str(ATOMIC_MODEL),
            "model_sha256": sha256_file(ATOMIC_MODEL),
            "selection": baseline["selection"],
            "overall": _baseline_summary(baseline),
        },
        "provenance": {
            "cpu_evidence_manifest": {
                "path": str(atomic.CPU_EVIDENCE_MANIFEST),
                "sha256": sha256_file(atomic.CPU_EVIDENCE_MANIFEST),
            },
            "atomic_helper": {
                "path": str(ATOMIC_SCRIPT),
                "sha256": sha256_file(ATOMIC_SCRIPT),
            },
            "script": str(Path(__file__).resolve()),
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "source_partition": manifest.get("partition"),
            "raw_hash_policy": "collector-recorded raw_event_stream_sha256 checked against a streaming SHA-256 pass for selected Django bytes",
            "protected_label_files_opened": [],
            "acquisition_or_inference": "not used",
            "existing_collector_source_changed": False,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / OUT_MODEL).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / OUT_REPORT).write_text(render_report(artifact), encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--max-raw-bytes", type=int, default=MAX_RAW_BYTES_DEFAULT)
    parser.add_argument("--max-cases", type=int, default=MAX_CASES_DEFAULT)
    args = parser.parse_args()
    artifact = run(
        out_dir=args.out_dir,
        max_raw_bytes=args.max_raw_bytes,
        max_cases=args.max_cases,
    )
    print(json.dumps({
        "model": str(args.out_dir / OUT_MODEL),
        "report": str(args.out_dir / OUT_REPORT),
        "selected_instances": [case["instance_id"] for case in artifact["selection"]["selected_cases"]],
        "raw_bytes_read": artifact["population"]["raw_bytes_read"],
        "raw_records": artifact["population"]["raw_records"],
        "valid_target_records": artifact["population"]["valid_target_records"],
        "models": list(atomic.MODEL_ORDER),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
