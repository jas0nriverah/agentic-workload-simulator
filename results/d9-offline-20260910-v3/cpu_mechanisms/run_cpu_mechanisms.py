#!/usr/bin/env python3
"""Bounded mechanism comparison for the retained atomic CPU-operation traces.

The atomic CPU helper owns the evidence selection, ABI validation, packet
decoder, and compact typed-array store.  This module reuses those pieces and
adds a small set of entry-only mechanism comparisons:

* global and operation medians;
* operation + requested-size and operation + path medians;
* a hierarchical operation + requested-size + path median; and
* a training-only per-operation representative chosen to maximize the number
  of training targets inside its +/-25% interval.

All scores are fixed leave-one-instance-out scores over the same four cases as
``atomic_cpu/run_atomic_cpu.py``.  The raw streams are read once and no
normalized event JSON or prediction dump is produced by default.
"""
from __future__ import annotations

import argparse
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ATOMIC_DIR = HERE.parent / "atomic_cpu"
if str(ATOMIC_DIR) not in sys.path:
    sys.path.insert(0, str(ATOMIC_DIR))

import run_atomic_cpu as atomic  # noqa: E402


RESULT_SCHEMA = "assignment.d9.atomic-cpu-mechanism-comparison.v1"
RESULT_NAME = "model.json"
REPORT_NAME = "REPORT.md"
SELECTED_CASE_COUNT = atomic.SELECTED_CASE_COUNT
WITHIN_THRESHOLD_PERCENT = atomic.WITHIN_THRESHOLD_PERCENT
# The four selected streams total 390,973,200 bytes.  Keep this hard default
# below 400 MB as requested; callers may use a smaller bound for a fail-fast
# contract check, but never enlarge it silently in a saved result.
MAX_RAW_BYTES_DEFAULT = 400_000_000

CANDIDATES = (
    "global_median",
    "operation_median",
    "operation_requested_size_bucket_median",
    "operation_path_class_median",
    "operation_requested_size_path_median",
    "operation_coverage_representative",
)
LINEAGE_KINDS = atomic.LINEAGE_KINDS


class ValidationError(ValueError):
    """Raised when the retained evidence cannot satisfy this contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class DecodeResult:
    cases: list[dict[str, Any]]
    store: atomic.EventStore
    diagnostics: dict[str, Any]


def _decode_store(max_raw_bytes: int) -> DecodeResult:
    """Stream the selected raw packets into the existing compact store.

    This is intentionally the same framing and membership validation used by
    the atomic helper.  It avoids reading normalized event JSON and keeps only
    the entry features, target, and compact event identity fields.
    """

    if max_raw_bytes > MAX_RAW_BYTES_DEFAULT:
        raise ValidationError(
            f"raw bound {max_raw_bytes} exceeds the fixed 400 MB mechanism bound"
        )
    manifest = json.loads(atomic.CPU_EVIDENCE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != "d9.calibration-input-manifest.v2":
        raise ValidationError("unexpected CPU evidence manifest schema")
    selected = [
        atomic.prepare_case(case)
        for case in atomic.select_cases(manifest, count=SELECTED_CASE_COUNT)
    ]
    raw_bytes = sum(int(case["raw_bytes"]) for case in selected)
    if raw_bytes > max_raw_bytes:
        raise ValidationError(
            f"selected raw input is {raw_bytes} bytes, over bound {max_raw_bytes}"
        )

    operations = atomic.Labels()
    sizes = atomic.Labels()
    paths = atomic.Labels()
    kinds = atomic.Labels()
    store = atomic.EventStore(selected, operations, sizes, paths, kinds)
    case_counters = [atomic.CaseCounters() for _ in selected]
    status_counts: Counter[str] = Counter()
    zero_target_by_kind: Counter[str] = Counter()
    zero_target_by_operation: Counter[str] = Counter()
    decoded_records = 0
    range_mismatch_count = sum(
        int(case["range_gap_bytes"] != 0 or case["range_overlap_bytes"] != 0)
        + sum(
            int(
                item["required_event_count"] != item["record_count"]
                or item["event_count"] != item["record_count"]
                or item["event_count_at_boundary"] != item["record_count"]
                or item["post_boundary_event_count"] != 0
                or not item["event_records_complete"]
            )
            for item in case["action_ranges"]
        )
        for case in selected
    )
    valid_kinds = set(range(1, 17))
    valid_statuses = {1, 2, 3}

    for case_index, case in enumerate(selected):
        raw_offset = 0
        record_size = int(case["record_size_bytes"])
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
                    packet = packets[packet_start : packet_start + record_size]
                    row = atomic.BpfWorkCollector._event_row(
                        packet, schema_version=atomic.BPF_EVENT_SCHEMA
                    )
                    if row["kind"] not in valid_kinds or row["status"] not in valid_statuses:
                        raise ValidationError("invalid decoded BPF kind/status")
                    if any(
                        row[prefix + "_status"] not in {0, 1, 2, 3}
                        or not 0 <= int(row[prefix + "_len"]) <= 128
                        for prefix in ("path", "path2")
                    ):
                        raise ValidationError("invalid decoded BPF path descriptor")

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

                    operation_id = operations.get(atomic.operation_key(row))
                    size_id = sizes.get(
                        atomic.requested_size_bucket(atomic.requested_size_value(row))
                    )
                    path_id = paths.get(atomic.path_class(row))
                    kind_id = kinds.get(str(row["kind_name"]))
                    store.add(
                        case_index=case_index,
                        operation_id=operation_id,
                        size_id=size_id,
                        path_id=path_id,
                        kind_id=kind_id,
                        target_ns=target_ns,
                        token=int(row["token"]),
                        sequence=int(row["sequence"]),
                        raw_offset=raw_offset,
                        kernel_start_ns=int(row["kernel_start_ns"]),
                    )
                    raw_offset += record_size
            if pending:
                raise ValidationError("raw BPF stream has a partial trailing record")

        actual_raw_hash = digest.hexdigest()
        case["raw_hash_actual"] = actual_raw_hash
        case["raw_hash_verified"] = bool(
            case["raw_hash_recorded"] and actual_raw_hash == case["raw_hash_recorded"]
        )
        if not case["raw_hash_verified"]:
            raise ValidationError("raw BPF stream hash differs from collector manifest")
        for token, expected in case["token_expected_records"].items():
            if token_counts[token] != expected:
                range_mismatch_count += 1
        if raw_offset != int(case["raw_bytes"]):
            range_mismatch_count += 1
        if raw_offset // int(case["record_size_bytes"]) != int(case["expected_raw_records"]):
            range_mismatch_count += 1

    if range_mismatch_count:
        raise ValidationError(
            f"action range/event-count integrity mismatch: {range_mismatch_count}"
        )

    diagnostics = {
        "decoded_records": decoded_records,
        "valid_target_records": sum(case.target_records for case in case_counters),
        "zero_target_records": sum(case.zero_target_records for case in case_counters),
        "zero_target_by_kind": dict(sorted(zero_target_by_kind.items())),
        "zero_target_by_operation": dict(sorted(zero_target_by_operation.items())),
        "lineage_zero_target_records": sum(
            count for kind, count in zero_target_by_kind.items() if kind in LINEAGE_KINDS
        ),
        "modeled_zero_target_records": sum(
            count for kind, count in zero_target_by_kind.items() if kind not in LINEAGE_KINDS
        ),
        "censored_records": sum(case.censored_records for case in case_counters),
        "negative_target_records": sum(case.negative_target_records for case in case_counters),
        "success_records": sum(case.success_records for case in case_counters),
        "failure_records": sum(case.failure_records for case in case_counters),
        "status_counts": dict(status_counts),
        "perf_lost_events": sum(
            int(case["aggregate_drops"]["perf_lost_events"]) for case in selected
        ),
        "event_callback_error_count": sum(
            int(case["aggregate_drops"]["event_callback_error_count"]) for case in selected
        ),
        "censored_pending_count": sum(
            int(case["aggregate_drops"]["censored_pending_count"]) for case in selected
        ),
        "lost_event_records": sum(
            int(case["aggregate_drops"]["lost_event_records"]) for case in selected
        ),
        "lost_path_records": sum(
            int(case["aggregate_drops"]["lost_path_records"]) for case in selected
        ),
        "lost_pending_records": sum(
            int(case["aggregate_drops"]["lost_pending_records"]) for case in selected
        ),
        "range_mismatch_count": range_mismatch_count,
        "compact_index_bytes": store.memory_bytes(),
        "raw_bytes_read": raw_bytes,
        "case_counters": {
            selected[index]["instance_id"]: case_counters[index].__dict__
            for index in range(len(selected))
        },
    }
    if diagnostics["valid_target_records"] != sum(target > 0 for target in store.target_ns):
        raise ValidationError("target counter does not match compact store")
    return DecodeResult(selected, store, diagnostics)


def _median(values: Iterable[int]) -> float:
    materialized = list(values)
    if not materialized:
        raise ValidationError("cannot compute an empty median")
    return float(statistics.median(materialized))


def _coverage_representative(values: Iterable[int]) -> float:
    """Choose a training-only prediction maximizing +/-25% interval coverage.

    For a target ``y``, a prediction ``p`` passes exactly when
    ``0.75*y <= p <= 1.25*y``.  A sorted two-pointer window therefore finds
    the largest jointly coverable target set whenever
    ``y_max / y_min <= 5/3``.  The returned value may lie between observed
    targets: it is clamped to the feasible interval and, among equal-size
    windows, chosen closest to the training median.  No held-out target is
    consulted.
    """

    ordered = sorted(int(value) for value in values if int(value) > 0)
    if not ordered:
        raise ValidationError("cannot choose a representative from no targets")
    training_median = float(statistics.median(ordered))
    best_count = 0
    best_value = training_median
    best_distance = float("inf")
    left = 0
    for right, maximum in enumerate(ordered):
        while left <= right and maximum * 3 > ordered[left] * 5:
            left += 1
        count = right - left + 1
        minimum = ordered[left]
        feasible_lower = 0.75 * maximum
        feasible_upper = 1.25 * minimum
        candidate = min(max(training_median, feasible_lower), feasible_upper)
        distance = abs(math.log(candidate / training_median))
        if count > best_count or (
            count == best_count
            and (
                distance < best_distance
                or (distance == best_distance and candidate < best_value)
            )
        ):
            best_count = count
            best_value = candidate
            best_distance = distance
    return float(best_value)


@dataclass
class MechanismFit:
    global_ns: float
    operation_ns: dict[int, float]
    operation_size_ns: dict[tuple[int, int], float]
    operation_path_ns: dict[tuple[int, int], float]
    operation_size_path_ns: dict[tuple[int, int, int], float]
    global_coverage_ns: float
    operation_coverage_ns: dict[int, float]
    training_target_events: int
    training_operation_groups: int
    training_size_groups: int
    training_path_groups: int
    training_size_path_groups: int


def fit_mechanism(store: atomic.EventStore, held_case: int) -> MechanismFit:
    """Fit all candidate maps from positive targets outside the held-out case."""

    global_values: list[int] = []
    operations: dict[int, list[int]] = defaultdict(list)
    operation_sizes: dict[tuple[int, int], list[int]] = defaultdict(list)
    operation_paths: dict[tuple[int, int], list[int]] = defaultdict(list)
    operation_size_paths: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for index, target in enumerate(store.target_ns):
        if target <= 0 or int(store.instance_index[index]) == held_case:
            continue
        value = int(target)
        operation = int(store.operation_id[index])
        size = int(store.size_id[index])
        path = int(store.path_id[index])
        global_values.append(value)
        operations[operation].append(value)
        operation_sizes[(operation, size)].append(value)
        operation_paths[(operation, path)].append(value)
        operation_size_paths[(operation, size, path)].append(value)

    operation_medians = {key: _median(values) for key, values in operations.items()}
    operation_size_medians = {
        key: _median(values) for key, values in operation_sizes.items()
    }
    operation_path_medians = {
        key: _median(values) for key, values in operation_paths.items()
    }
    operation_size_path_medians = {
        key: _median(values) for key, values in operation_size_paths.items()
    }
    return MechanismFit(
        global_ns=_median(global_values),
        operation_ns=operation_medians,
        operation_size_ns=operation_size_medians,
        operation_path_ns=operation_path_medians,
        operation_size_path_ns=operation_size_path_medians,
        global_coverage_ns=_coverage_representative(global_values),
        operation_coverage_ns={
            key: _coverage_representative(values)
            for key, values in operations.items()
        },
        training_target_events=len(global_values),
        training_operation_groups=len(operations),
        training_size_groups=len(operation_sizes),
        training_path_groups=len(operation_paths),
        training_size_path_groups=len(operation_size_paths),
    )


def predict(
    fitted: MechanismFit,
    candidate: str,
    operation: int,
    size: int,
    path: int,
) -> float:
    operation_prediction = fitted.operation_ns.get(operation, fitted.global_ns)
    if candidate == "global_median":
        return fitted.global_ns
    if candidate == "operation_median":
        return operation_prediction
    if candidate == "operation_requested_size_bucket_median":
        return fitted.operation_size_ns.get((operation, size), operation_prediction)
    if candidate == "operation_path_class_median":
        return fitted.operation_path_ns.get((operation, path), operation_prediction)
    if candidate == "operation_requested_size_path_median":
        return fitted.operation_size_path_ns.get(
            (operation, size, path),
            fitted.operation_size_ns.get(
                (operation, size),
                fitted.operation_path_ns.get((operation, path), operation_prediction),
            ),
        )
    if candidate == "operation_coverage_representative":
        return fitted.operation_coverage_ns.get(operation, fitted.global_coverage_ns)
    raise ValidationError(f"unknown candidate: {candidate}")


@dataclass
class ErrorSummary:
    keep_apes: bool = False
    apes: array | None = None
    n_events: int = 0
    within_25_events: int = 0
    sum_ape: float = 0.0
    sum_abs_ms: float = 0.0
    sum_signed_ms: float = 0.0
    worst_ape: float = -1.0
    worst_event: dict[str, Any] | None = None

    @classmethod
    def create(cls, *, keep_apes: bool = False) -> "ErrorSummary":
        return cls(keep_apes=keep_apes, apes=array("d") if keep_apes else None)

    def update(
        self,
        *,
        prediction_ns: float,
        target_ns: int,
        event: Mapping[str, Any],
    ) -> None:
        target_ms = float(target_ns) / 1_000_000.0
        prediction_ms = float(prediction_ns) / 1_000_000.0
        ape = abs(prediction_ms - target_ms) / target_ms * 100.0
        if self.apes is not None:
            self.apes.append(ape)
        self.n_events += 1
        self.within_25_events += int(ape <= WITHIN_THRESHOLD_PERCENT)
        self.sum_ape += ape
        self.sum_abs_ms += abs(prediction_ms - target_ms)
        self.sum_signed_ms += prediction_ms - target_ms
        if ape > self.worst_ape:
            self.worst_ape = ape
            self.worst_event = {
                **dict(event),
                "observed_duration_ns": int(target_ns),
                "predicted_duration_ns": float(prediction_ns),
                "ape_percent": ape,
            }

    def finish(self, *, include_p95: bool = False) -> dict[str, Any]:
        if not self.n_events:
            return {
                "n_events": 0,
                "within_25_events": 0,
                "coverage_percent": None,
                "mean_ape_percent": None,
                "p95_ape_percent_nearest_rank": None,
                "worst_ape_percent": None,
                "absolute_error_ms": 0.0,
                "signed_bias_ms": 0.0,
                "worst_event": None,
            }
        p95 = None
        if include_p95:
            if self.apes is None:
                raise ValidationError("p95 requested from an accumulator without APEs")
            ordered = sorted(self.apes)
            rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
            p95 = ordered[rank]
        return {
            "n_events": self.n_events,
            "within_25_events": self.within_25_events,
            "coverage_percent": 100.0 * self.within_25_events / self.n_events,
            "mean_ape_percent": self.sum_ape / self.n_events,
            "p95_ape_percent_nearest_rank": p95,
            "worst_ape_percent": self.worst_ape,
            "absolute_error_ms": self.sum_abs_ms,
            "signed_bias_ms": self.sum_signed_ms,
            "worst_event": self.worst_event,
        }


def _new_metric_tables(
    store: atomic.EventStore,
    cases: list[dict[str, Any]],
) -> tuple[
    dict[str, ErrorSummary],
    dict[str, dict[int, ErrorSummary]],
    dict[str, dict[int, ErrorSummary]],
]:
    overall = {candidate: ErrorSummary.create(keep_apes=True) for candidate in CANDIDATES}
    per_instance = {
        candidate: {
            case_index: ErrorSummary.create() for case_index in range(len(cases))
        }
        for candidate in CANDIDATES
    }
    per_operation = {
        candidate: {
            operation: ErrorSummary.create()
            for operation in range(len(store.operations.values))
        }
        for candidate in CANDIDATES
    }
    return overall, per_instance, per_operation


def _fold_fit_public(fitted: MechanismFit, store: atomic.EventStore) -> dict[str, Any]:
    """Serialize only the two new family fits for transfer diagnostics.

    The maps are keyed by the same stable operation/size/path labels retained
    in the atomic artifact, so a separate transfer checker can apply these
    folds without a prediction dump or a second Astropy fit.  They contain
    training-only values; no held-out target is serialized into a fit.
    """

    def operation_map(values: Mapping[int, float]) -> dict[str, float]:
        return {
            store.operations.values[key]: value
            for key, value in sorted(values.items())
        }

    def pair_map(values: Mapping[tuple[int, int], float], labels: atomic.Labels) -> dict[str, float]:
        return {
            f"{store.operations.values[operation]}|{labels.values[group]}": value
            for (operation, group), value in sorted(values.items())
        }

    def triple_map(values: Mapping[tuple[int, int, int], float]) -> dict[str, float]:
        return {
            f"{store.operations.values[operation]}|{store.sizes.values[size]}|"
            f"{store.paths.values[path]}": value
            for (operation, size, path), value in sorted(values.items())
        }

    return {
        "operation_requested_size_path_median": {
            "global_median_ns": fitted.global_ns,
            "operation_medians_ns": operation_map(fitted.operation_ns),
            "operation_requested_size_bucket_medians_ns": pair_map(
                fitted.operation_size_ns, store.sizes
            ),
            "operation_path_class_medians_ns": pair_map(
                fitted.operation_path_ns, store.paths
            ),
            "operation_requested_size_path_medians_ns": triple_map(
                fitted.operation_size_path_ns
            ),
            "fallback": "exact operation+size+path, then operation+size, then operation+path, then operation, then global",
        },
        "operation_coverage_representative": {
            "global_coverage_representative_ns": fitted.global_coverage_ns,
            "operation_coverage_representative_ns": operation_map(
                fitted.operation_coverage_ns
            ),
            "selection": "training observed positive target maximizing count in [p/1.25, p/0.75]; ties nearest training median then lower p",
        },
    }


def _compact_event(store: atomic.EventStore, index: int) -> dict[str, Any]:
    event = store.identity(index)
    # Drop fields that are useful to the raw decoder but do not identify an
    # error location; this keeps each retained worst-event explanation small.
    return {
        key: event[key]
        for key in (
            "event_id",
            "case_id",
            "instance_id",
            "queue_ordinal",
            "action_token",
            "sequence",
            "raw_offset_start",
            "raw_offset_end",
            "kernel_start_ns",
            "operation",
            "requested_size_bucket",
            "path_class",
            "kind_name",
        )
    }


def score(
    store: atomic.EventStore,
    cases: list[dict[str, Any]],
) -> tuple[
    dict[str, ErrorSummary],
    dict[str, dict[int, ErrorSummary]],
    dict[str, dict[int, ErrorSummary]],
    dict[str, dict[str, int]],
    list[dict[str, Any]],
    int,
]:
    overall, per_instance, per_operation = _new_metric_tables(store, cases)
    gate = {
        candidate: {
            "required_records": 0,
            "positive_target_records": 0,
            "positive_within_25_records": 0,
            "zero_target_records": 0,
            "zero_exact_prediction_records": 0,
            "lineage_zero_records": 0,
            "unsupported_target_records": 0,
        }
        for candidate in CANDIDATES
    }
    fold_summaries: list[dict[str, Any]] = []
    scored = 0

    for held_case in range(len(cases)):
        fitted = fit_mechanism(store, held_case)
        fold_summaries.append(
            {
                "held_out_instance": cases[held_case]["instance_id"],
                "held_out_queue_ordinal": cases[held_case]["queue_ordinal"],
                "training_target_events": fitted.training_target_events,
                "training_operation_groups": fitted.training_operation_groups,
                "training_operation_size_groups": fitted.training_size_groups,
                "training_operation_path_groups": fitted.training_path_groups,
                "training_operation_size_path_groups": fitted.training_size_path_groups,
                "new_family_fits": _fold_fit_public(fitted, store),
            }
        )
        for index, target in enumerate(store.target_ns):
            if int(store.instance_index[index]) != held_case:
                continue
            operation = int(store.operation_id[index])
            size = int(store.size_id[index])
            path = int(store.path_id[index])
            kind_name = store.kinds.values[int(store.kind_id[index])]
            if target > 0:
                event = _compact_event(store, index)
                scored += 1
                for candidate in CANDIDATES:
                    prediction = predict(fitted, candidate, operation, size, path)
                    overall[candidate].update(
                        prediction_ns=prediction, target_ns=int(target), event=event
                    )
                    per_instance[candidate][held_case].update(
                        prediction_ns=prediction, target_ns=int(target), event=event
                    )
                    per_operation[candidate][operation].update(
                        prediction_ns=prediction, target_ns=int(target), event=event
                    )
                    gate[candidate]["required_records"] += 1
                    gate[candidate]["positive_target_records"] += 1
                    target_ms = float(target) / 1_000_000.0
                    prediction_ms = float(prediction) / 1_000_000.0
                    gate[candidate]["positive_within_25_records"] += int(
                        abs(prediction_ms - target_ms) / target_ms * 100.0
                        <= WITHIN_THRESHOLD_PERCENT
                    )
            elif target == 0:
                is_lineage_zero = kind_name in LINEAGE_KINDS
                for candidate in CANDIDATES:
                    if is_lineage_zero:
                        gate[candidate]["lineage_zero_records"] += 1
                    else:
                        gate[candidate]["required_records"] += 1
                        gate[candidate]["zero_target_records"] += 1
                        prediction = predict(fitted, candidate, operation, size, path)
                        gate[candidate]["zero_exact_prediction_records"] += int(
                            float(prediction) == 0.0
                        )
            else:
                for candidate in CANDIDATES:
                    gate[candidate]["required_records"] += 1
                    gate[candidate]["unsupported_target_records"] += 1

    expected_scored = sum(target > 0 for target in store.target_ns)
    if scored != expected_scored:
        raise ValidationError(
            f"leave-one-instance-out scoring covered {scored}, expected {expected_scored}"
        )
    for values in gate.values():
        values["passing_records"] = (
            values["positive_within_25_records"]
            + values["zero_exact_prediction_records"]
        )
        values["coverage_over_required_percent"] = (
            100.0 * values["passing_records"] / values["required_records"]
            if values["required_records"]
            else None
        )
        eligible = values["positive_target_records"] + values["zero_target_records"]
        values["coverage_over_scored_required_percent"] = (
            100.0 * values["passing_records"] / eligible if eligible else None
        )
    return overall, per_instance, per_operation, gate, fold_summaries, scored


def _quantile(values: list[int], probability: float) -> int:
    if not values:
        raise ValidationError("quantile requested from an empty group")
    rank = max(0, math.ceil(probability * len(values)) - 1)
    return int(values[rank])


def _distribution_stats(values: Iterable[int]) -> dict[str, Any]:
    ordered = sorted(int(value) for value in values if int(value) > 0)
    if not ordered:
        return {"n_events": 0}
    q01 = _quantile(ordered, 0.01)
    q10 = _quantile(ordered, 0.10)
    q25 = _quantile(ordered, 0.25)
    q50 = _quantile(ordered, 0.50)
    q75 = _quantile(ordered, 0.75)
    q90 = _quantile(ordered, 0.90)
    q95 = _quantile(ordered, 0.95)
    q99 = _quantile(ordered, 0.99)
    return {
        "n_events": len(ordered),
        "min_ns": ordered[0],
        "q01_ns": q01,
        "q10_ns": q10,
        "q25_ns": q25,
        "q50_ns": q50,
        "q75_ns": q75,
        "q90_ns": q90,
        "q95_ns": q95,
        "q99_ns": q99,
        "max_ns": ordered[-1],
        "q90_to_q10": float(q90 / q10) if q10 else None,
        "q99_to_q50": float(q99 / q50) if q50 else None,
        "iqr_to_median": float((q75 - q25) / q50) if q50 else None,
    }


def _timing_distributions(store: atomic.EventStore) -> dict[str, dict[str, dict[str, Any]]]:
    """Return positive-target quantiles for operation and entry-feature groups."""

    families: dict[str, dict[tuple[int, ...], array]] = {
        "operation": defaultdict(lambda: array("Q")),
        "operation_requested_size": defaultdict(lambda: array("Q")),
        "operation_path": defaultdict(lambda: array("Q")),
        "operation_requested_size_path": defaultdict(lambda: array("Q")),
    }
    for index, target in enumerate(store.target_ns):
        if target <= 0:
            continue
        operation = int(store.operation_id[index])
        size = int(store.size_id[index])
        path = int(store.path_id[index])
        families["operation"][(operation,)].append(int(target))
        families["operation_requested_size"][(operation, size)].append(int(target))
        families["operation_path"][(operation, path)].append(int(target))
        families["operation_requested_size_path"][(operation, size, path)].append(int(target))

    output: dict[str, dict[str, dict[str, Any]]] = {}
    for family, groups in families.items():
        family_output: dict[str, dict[str, Any]] = {}
        for key, values in sorted(groups.items()):
            operation = key[0]
            if family == "operation":
                label = store.operations.values[operation]
            elif family == "operation_requested_size":
                label = (
                    f"{store.operations.values[operation]}|"
                    f"{store.sizes.values[key[1]]}"
                )
            elif family == "operation_path":
                label = (
                    f"{store.operations.values[operation]}|"
                    f"{store.paths.values[key[1]]}"
                )
            else:
                label = (
                    f"{store.operations.values[operation]}|"
                    f"{store.sizes.values[key[1]]}|"
                    f"{store.paths.values[key[2]]}"
                )
            family_output[label] = _distribution_stats(values)
        output[family] = family_output
    return output


def _public_case(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: case[key]
        for key in (
            "queue_ordinal",
            "instance_id",
            "case_id",
            "partition",
            "record_size_bytes",
            "raw_path",
            "raw_bytes",
            "expected_raw_records",
            "raw_hash_recorded",
            "raw_hash_actual",
            "raw_hash_verified",
            "bpf_manifest_path",
            "raw_aggregate_journal",
            "action_ranges",
            "kernel_clock",
            "host_clock",
            "validation_report_path",
        )
    }


def _finish_metrics(
    overall: dict[str, ErrorSummary],
    per_instance: dict[str, dict[int, ErrorSummary]],
    per_operation: dict[str, dict[int, ErrorSummary]],
    cases: list[dict[str, Any]],
    store: atomic.EventStore,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    overall_json = {
        candidate: summary.finish(include_p95=True)
        for candidate, summary in overall.items()
    }
    per_instance_json: dict[str, Any] = {}
    equal_instance: dict[str, Any] = {}
    for candidate, summaries in per_instance.items():
        rows = {
            cases[case_index]["instance_id"]: summary.finish()
            for case_index, summary in sorted(summaries.items())
        }
        per_instance_json[candidate] = rows
        valid = [row for row in rows.values() if row["n_events"]]
        equal_instance[candidate] = {
            "instances": len(valid),
            "macro_mean_coverage_percent": statistics.mean(
                row["coverage_percent"] for row in valid
            ),
            "macro_mean_ape_percent": statistics.mean(
                row["mean_ape_percent"] for row in valid
            ),
            "macro_mean_worst_ape_percent": statistics.mean(
                row["worst_ape_percent"] for row in valid
            ),
        }

    per_operation_json: dict[str, Any] = {}
    for candidate, summaries in per_operation.items():
        per_operation_json[candidate] = {
            store.operations.values[operation]: summary.finish()
            for operation, summary in sorted(summaries.items())
            if summary.n_events
        }
    return overall_json, per_instance_json, equal_instance, per_operation_json


def _feature_spread_summary(distributions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Compact family summary plus widest groups for the human report."""

    output: dict[str, Any] = {}
    for family, groups in distributions.items():
        spread_rows = [
            (float(row["q90_to_q10"] or 0.0), label, row["n_events"])
            for label, row in groups.items()
            if row.get("n_events", 0) and row.get("q90_to_q10") is not None
        ]
        spread_rows.sort(reverse=True)
        output[family] = {
            "groups": len(groups),
            "widest_groups_by_q90_to_q10": [
                {"label": label, "n_events": count, "q90_to_q10": spread}
                for spread, label, count in spread_rows[:10]
            ],
        }
    return output


def render_report(artifact: Mapping[str, Any]) -> str:
    population = artifact["population"]
    diagnostics = artifact["diagnostics"]
    overall = artifact["metrics"]["overall"]
    equal_instance = artifact["metrics"]["equal_instance"]
    distributions = artifact["timing_distributions"]
    lines = [
        "# Bounded D9 CPU mechanism comparison",
        "",
        "This is an offline, trace-conditioned diagnostic on the same four fixed fully valid `train_calibration` instances as the atomic CPU comparison. It does not change the collector, acquire data, run inference, or establish a complete D9 pass.",
        "",
        "## Fixed population and feature contract",
        "",
        f"Cases were selected by ascending queue ordinal after the evidence validity gate, taking the first four distinct instances: {', '.join(str(c['queue_ordinal']) + ' (' + c['instance_id'] + ')' for c in artifact['selection']['selected_cases'])}. Selection did not inspect durations, outcomes, or model errors.",
        f"The decoder streamed **{population['raw_records']} raw records** ({population['raw_bytes_read']} bytes) through the existing v3 `{atomic.BPF_EVENT_RECORD_SIZE}`-byte packet decoder. The compact typed-array store used **{population['compact_index_bytes']} bytes** ({population['compact_index_bytes'] / (1024 * 1024):.2f} MiB); no full normalized event export was created.",
        "Features are limited to syscall number/name/kind, decoder-proven requested-size buckets, and lexical classes from bounded syscall-entry path bytes. The decoder also exposes open/openat flags, mmap length/prot/flags, and pread/pwrite offsets, but those descriptors are intentionally ignored in this bounded comparison; requested-size/path values can instead be unavailable or unknown, and openat2 flag/pointer details are opaque under the retained ABI. Return values, status, completed latency, residuals, future events, filesystem lookups, and tool wall time are excluded. Positive durations are targets; failure-status events remain valid targets.",
        "",
        "## Leave-one-instance-out results",
        "",
        "| Candidate | Events | Within 25% | Mean APE | P95 APE | Worst APE | Equal-instance mean coverage | Equal-instance mean APE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for candidate in CANDIDATES:
        metric = overall[candidate]
        macro = equal_instance[candidate]
        lines.append(
            f"| `{candidate}` | {metric['n_events']} | {metric['coverage_percent']:.2f}% ({metric['within_25_events']}) | {metric['mean_ape_percent']:.2f}% | {metric['p95_ape_percent_nearest_rank']:.2f}% | {metric['worst_ape_percent']:.2f}% | {macro['macro_mean_coverage_percent']:.2f}% | {macro['macro_mean_ape_percent']:.2f}% |"
        )

    primary = "operation_path_class_median"
    tail_candidate = "operation_coverage_representative"
    path_baseline = overall[primary]["coverage_percent"]
    robust_delta = overall[tail_candidate]["coverage_percent"] - path_baseline
    lines += [
        "",
        f"Recommended primary candidate: `{primary}`. It reaches {overall[primary]['coverage_percent']:.2f}% held-out within-25% coverage with {overall[primary]['mean_ape_percent']:.2f}% mean APE. The hierarchical size+path interaction reaches {overall['operation_requested_size_path_median']['coverage_percent']:.2f}% coverage ({overall['operation_requested_size_path_median']['coverage_percent'] - path_baseline:+.2f} points) but raises mean APE to {overall['operation_requested_size_path_median']['mean_ape_percent']:.2f}% and does not improve the long tail. The secondary tail candidate `{tail_candidate}` trades {abs(robust_delta):.2f} percentage points of coverage ({robust_delta:+.2f}) for {overall[tail_candidate]['mean_ape_percent']:.2f}% mean APE, {overall[tail_candidate]['p95_ape_percent_nearest_rank']:.2f}% P95 APE, and {overall[tail_candidate]['worst_ape_percent']:.2f}% worst APE. These are candidates for new fixed held-out validation, not a D9PASS claim.",
        "",
        "## Per-operation coverage, count, and worst error",
        "",
        "Each row is pooled across held-out folds. The prediction maps were fit without that operation's held-out instance; `worst event` is an identity and not a case-selection rule.",
    ]
    for candidate in CANDIDATES:
        lines += [
            "",
            f"### `{candidate}`",
            "",
            "| Operation | Count | Within 25% | Mean APE | Worst APE | Worst event |",
            "|---|---:|---:|---:|---:|---|",
        ]
        for operation, metric in artifact["metrics"]["per_operation"][candidate].items():
            worst_event = metric["worst_event"] or {}
            lines.append(
                f"| `{operation}` | {metric['n_events']} | {metric['coverage_percent']:.2f}% ({metric['within_25_events']}) | {metric['mean_ape_percent']:.2f}% | {metric['worst_ape_percent']:.2f}% | `{worst_event.get('event_id', '')}` |"
            )

    lines += [
        "",
        "## Equal-instance results",
        "",
        "These are macro averages across the four held-out instances, so the largest trace does not dominate the comparison. Detailed rows are retained in `model.json`.",
        "",
        "| Candidate | Macro coverage | Macro mean APE | Macro worst APE |",
        "|---|---:|---:|---:|",
    ]
    for candidate in CANDIDATES:
        macro = equal_instance[candidate]
        lines.append(
            f"| `{candidate}` | {macro['macro_mean_coverage_percent']:.2f}% | {macro['macro_mean_ape_percent']:.2f}% | {macro['macro_mean_worst_ape_percent']:.2f}% |"
        )
    lines += [
        "",
        "## Timing distributions and same-feature spread",
        "",
        "Quantiles are nearest-rank positive-duration values pooled over the four traces. `q90/q10` reports within-feature timing spread; these distributions describe the target and are not used to choose cases.",
        "",
        "| Operation | Count | q01 (ns) | q10 | q50 | q90 | q99 | q90/q10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for operation, row in distributions["operation"].items():
        lines.append(
            f"| `{operation}` | {row['n_events']} | {row['q01_ns']} | {row['q10_ns']} | {row['q50_ns']} | {row['q90_ns']} | {row['q99_ns']} | {row['q90_to_q10']:.2f} |"
        )
    lines += [
        "",
        "The full operation+size, operation+path, and operation+size+path timing tables are in `model.json`; the ten widest groups per family are summarized below.",
        "",
        "| Feature family | Groups | Widest groups by q90/q10 |",
        "|---|---:|---|",
    ]
    for family, summary in artifact["feature_spread_summary"].items():
        widest = "; ".join(
            f"`{row['label']}` ({row['n_events']}, {row['q90_to_q10']:.2f}x)"
            for row in summary["widest_groups_by_q90_to_q10"][:5]
        )
        lines.append(f"| `{family}` | {summary['groups']} | {widest} |")

    lines += [
        "",
        "## Population gate and limitations",
        "",
        f"Positive duration targets scored: **{diagnostics['valid_target_records']}**; zero targets: **{diagnostics['zero_target_records']}** ({diagnostics['lineage_zero_target_records']} known fork/clone/thread lineage records); censored: **{diagnostics['censored_records']}**; negative: **{diagnostics['negative_target_records']}**.",
        f"Status counts were success **{diagnostics['success_records']}** and failure **{diagnostics['failure_records']}**; status was not a feature. Perf-buffer losses, callback errors, pending losses, and range mismatches were **{diagnostics['perf_lost_events']} / {diagnostics['event_callback_error_count']} / {diagnostics['lost_pending_records']} / {diagnostics['range_mismatch_count']}**.",
        "The sample is an ABI, leakage, and mechanism comparison on four fixed traces. It does not establish all-production CPU accuracy, cross-hardware transfer, a prospective pre-execution predictor, or the assignment's complete CPU+GPU+E2E acceptance gate. The robust representative is optimized only on each fold's training targets and may overfit their within-25% count; it is a candidate for additional fixed held-out validation.",
        "",
        "`run_cpu_mechanisms.py` is the reproducible helper. Its default raw-read bound is 400,000,000 bytes and it does not write a prediction JSONL stream.",
    ]
    return "\n".join(lines) + "\n"


def run(*, out_dir: Path, max_raw_bytes: int = MAX_RAW_BYTES_DEFAULT) -> dict[str, Any]:
    decoded = _decode_store(max_raw_bytes)
    cases = decoded.cases
    store = decoded.store
    overall, per_instance, per_operation, gate, folds, scored = score(store, cases)
    overall_json, per_instance_json, equal_instance, per_operation_json = _finish_metrics(
        overall, per_instance, per_operation, cases, store
    )
    distributions = _timing_distributions(store)
    raw_bytes = int(decoded.diagnostics["raw_bytes_read"])
    primary = overall_json["operation_path_class_median"]
    combined = overall_json["operation_requested_size_path_median"]
    tail = overall_json["operation_coverage_representative"]
    coverage_delta = tail["coverage_percent"] - primary["coverage_percent"]
    recommendation = {
        "primary_candidate": "operation_path_class_median",
        "primary_reason": (
            f"best balanced entry-only median: {primary['coverage_percent']:.2f}% "
            f"held-out within-25% coverage with {primary['mean_ape_percent']:.2f}% "
            f"mean APE; the size+path interaction adds "
            f"{combined['coverage_percent'] - primary['coverage_percent']:.2f} "
            f"percentage points of coverage but raises mean APE to "
            f"{combined['mean_ape_percent']:.2f}% and does not improve the long tail"
        ),
        "tail_candidate": "operation_coverage_representative",
        "tail_reason": (
            f"{tail['coverage_percent']:.2f}% held-out within-25% coverage, "
            f"{tail['mean_ape_percent']:.2f}% mean APE, "
            f"{tail['p95_ape_percent_nearest_rank']:.2f}% P95 APE, and "
            f"{tail['worst_ape_percent']:.2f}% worst APE; this trades "
            f"{abs(coverage_delta):.2f} percentage points of coverage for "
            "materially lower average and tail error"
        ),
        "status": "offline candidate only; requires new fixed held-out instances before any production or D9 gate claim",
    }
    artifact: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "selection": {
            "policy": "ascending queue ordinal, evidence-valid train_calibration cases, first four distinct instance IDs; no target/outcome/error ranking",
            "selected_cases": [_public_case(case) for case in cases],
        },
        "abi": {
            "event_schema": atomic.BPF_EVENT_SCHEMA,
            "event_abi": atomic.BPF_EVENT_ABI,
            "record_size_bytes": atomic.BPF_EVENT_RECORD_SIZE,
            "decoder": "agentic_sim.telemetry.bpf_work.BpfWorkCollector._event_row; reused from atomic_cpu/run_atomic_cpu.py",
            "raw_read_bound_bytes": max_raw_bytes,
        },
        "feature_contract": {
            "entry_features": [
                "syscall number/name/kind",
                "requested-size bucket from decoder scalar_args.requested_size",
                "lexical path class from bounded sys_enter path bytes",
            ],
            "entry_descriptors_ignored_in_this_bound": [
                "open/openat open_flags",
                "mmap length/prot/flags",
                "pread/pwrite offset",
            ],
            "entry_descriptors_unavailable_or_opaque": [
                "requested size on operations without a decoder-proven size word",
                "path bytes when the decoder marks the bounded path unknown",
                "openat2 flag/pointer details not projected by the ABI",
            ],
            "forbidden_features": [
                "ret/return bytes",
                "status/success_failure",
                "duration_ns/latency/kernel_end_ns",
                "residual/error/held-out target",
                "post-event result or future event",
                "filesystem lookup",
                "tool wall time",
            ],
            "combination_fallback": "size+path exact pair, then operation+size, then operation+path, then operation median, then global median",
        },
        "target_contract": {
            "target": "positive completed BPF kernel duration_ns per event",
            "positive_score": "absolute percentage error and within 25 percent",
            "zero_or_censored_policy": "known fork/clone/thread lineage zeros are reported separately; individual zeros remain required and only exact-zero predictions pass; censored/negative targets remain unsupported required records; no imputation",
        },
        "candidates": list(CANDIDATES),
        "population": {
            "instances": len(cases),
            "raw_records": decoded.diagnostics["decoded_records"],
            "raw_bytes_read": raw_bytes,
            "valid_target_records": scored,
            "compact_index_bytes": store.memory_bytes(),
            "operations": len(store.operations.values),
            "requested_size_buckets": len(store.sizes.values),
            "path_classes": len(store.paths.values),
            "kinds": len(store.kinds.values),
            "case_counters": decoded.diagnostics["case_counters"],
        },
        "folds": folds,
        "metrics": {
            "overall": overall_json,
            "per_instance": per_instance_json,
            "equal_instance": equal_instance,
            "per_operation": per_operation_json,
            "full_required_population_gate": gate,
        },
        "timing_distributions": distributions,
        "feature_spread_summary": _feature_spread_summary(distributions),
        "recommendation": recommendation,
        "diagnostics": decoded.diagnostics,
        "provenance": {
            "cpu_evidence_manifest": {
                "path": str(atomic.CPU_EVIDENCE_MANIFEST),
                "sha256": sha256_file(atomic.CPU_EVIDENCE_MANIFEST),
            },
            "atomic_helper": {
                "path": str(ATOMIC_DIR / "run_atomic_cpu.py"),
                "sha256": sha256_file(ATOMIC_DIR / "run_atomic_cpu.py"),
            },
            "script": str(Path(__file__).resolve()),
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "source_partition": "train_calibration",
            "acquisition_or_inference": "not used",
            "existing_collector_source_changed": False,
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / RESULT_NAME
    report_path = out_dir / REPORT_NAME
    # Render after adding the result-independent summaries; JSON is the only
    # machine artifact and contains no raw event stream or predictions.
    result_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(render_report(artifact), encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--max-raw-bytes", type=int, default=MAX_RAW_BYTES_DEFAULT)
    args = parser.parse_args()
    artifact = run(out_dir=args.out_dir, max_raw_bytes=args.max_raw_bytes)
    print(
        json.dumps(
            {
                "model": str(args.out_dir / RESULT_NAME),
                "report": str(args.out_dir / REPORT_NAME),
                "raw_bytes_read": artifact["population"]["raw_bytes_read"],
                "raw_records": artifact["population"]["raw_records"],
                "valid_target_records": artifact["population"]["valid_target_records"],
                "candidates": list(CANDIDATES),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
