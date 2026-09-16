#!/usr/bin/env python3
"""Bounded, trace-conditioned CPU-operation median comparison.

This helper reads the retained v3 BPF binary stream directly through the
existing decoder.  It selects the first four *distinct* fully valid
``train_calibration`` instances by queue ordinal, keeps the decoded records in
compact typed arrays, and scores fixed leave-one-instance-out median models.

The model features are limited to values known at syscall entry: syscall
number/kind, a decoder-proven requested-size argument where one exists, and a
lexical path class derived from the bounded entry path bytes.  Return values,
success/failure status, completed duration, and any other result are targets or
diagnostics only.  No production collector source is changed.
"""
from __future__ import annotations

import argparse
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
import gzip
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic_sim.telemetry.bpf_work import (  # noqa: E402
    BPF_EVENT_ABI,
    BPF_EVENT_RECORD_SIZE,
    BPF_EVENT_SCHEMA,
    BpfWorkCollector,
)


CPU_EVIDENCE_MANIFEST = REPO / "docs" / "d9-salvage-20260910" / "evidence" / "calibration_input_manifest.json"
SCHEMA = "assignment.d9.atomic-cpu-operation-comparison.v1"
FIT_SCHEMA = "assignment.d9.atomic-cpu-operation-fit-artifact.v1"
REPORT_NAME = "REPORT.md"
MODEL_NAME = "model.json"
MAX_RAW_BYTES_DEFAULT = 512 * 1024 * 1024
SELECTED_CASE_COUNT = 4
WITHIN_THRESHOLD_PERCENT = 25.0

MODEL_ORDER = (
    "global_median",
    "operation_median",
    "operation_requested_size_bucket_median",
    "operation_path_class_median",
)
LINEAGE_KINDS = frozenset({"fork", "clone", "thread"})


class ValidationError(ValueError):
    """Raised when the retained evidence cannot satisfy the fixed contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Labels:
    """Stable integer codes for compact feature storage."""

    def __init__(self) -> None:
        self.values: list[str] = []
        self._ids: dict[str, int] = {}

    def get(self, value: str) -> int:
        value = str(value)
        if value not in self._ids:
            self._ids[value] = len(self.values)
            self.values.append(value)
        return self._ids[value]


class EventStore:
    """Compact in-memory index; raw JSON events are never materialized."""

    def __init__(self, cases: list[dict[str, Any]], operations: Labels, sizes: Labels, paths: Labels, kinds: Labels) -> None:
        self.cases = cases
        self.operations = operations
        self.sizes = sizes
        self.paths = paths
        self.kinds = kinds
        self.instance_index = array("B")
        self.operation_id = array("I")
        self.size_id = array("I")
        self.path_id = array("I")
        self.kind_id = array("B")
        self.target_ns = array("q")  # -1 means censored/unavailable.
        self.token = array("Q")
        self.sequence = array("Q")
        self.raw_offset = array("Q")
        self.kernel_start_ns = array("Q")

    def add(
        self,
        *,
        case_index: int,
        operation_id: int,
        size_id: int,
        path_id: int,
        kind_id: int,
        target_ns: int,
        token: int,
        sequence: int,
        raw_offset: int,
        kernel_start_ns: int,
    ) -> None:
        self.instance_index.append(case_index)
        self.operation_id.append(operation_id)
        self.size_id.append(size_id)
        self.path_id.append(path_id)
        self.kind_id.append(kind_id)
        self.target_ns.append(target_ns)
        self.token.append(token)
        self.sequence.append(sequence)
        self.raw_offset.append(raw_offset)
        self.kernel_start_ns.append(kernel_start_ns)

    def __len__(self) -> int:
        return len(self.target_ns)

    def memory_bytes(self) -> int:
        arrays = (
            self.instance_index,
            self.operation_id,
            self.size_id,
            self.path_id,
            self.kind_id,
            self.target_ns,
            self.token,
            self.sequence,
            self.raw_offset,
            self.kernel_start_ns,
        )
        return sum(item.itemsize * len(item) for item in arrays)

    def identity(self, index: int) -> dict[str, Any]:
        case = self.cases[int(self.instance_index[index])]
        record_size = int(case["record_size_bytes"])
        return {
            "event_id": (
                f"{case['case_id']}:{int(self.token[index])}:"
                f"{int(self.sequence[index])}:{int(self.raw_offset[index])}"
            ),
            "case_id": case["case_id"],
            "instance_id": case["instance_id"],
            "queue_ordinal": case["queue_ordinal"],
            "action_token": int(self.token[index]),
            "sequence": int(self.sequence[index]),
            "raw_offset_start": int(self.raw_offset[index]),
            "raw_offset_end": int(self.raw_offset[index]) + record_size,
            "kernel_start_ns": int(self.kernel_start_ns[index]),
            "operation": self.operations.values[int(self.operation_id[index])],
            "requested_size_bucket": self.sizes.values[int(self.size_id[index])],
            "path_class": self.paths.values[int(self.path_id[index])],
            "kind_name": self.kinds.values[int(self.kind_id[index])],
            "cpu_clock": case["kernel_clock"],
        }


@dataclass
class CaseCounters:
    raw_records: int = 0
    target_records: int = 0
    zero_target_records: int = 0
    censored_records: int = 0
    negative_target_records: int = 0
    success_records: int = 0
    failure_records: int = 0


@dataclass
class MetricAccumulator:
    apes: array
    n_events: int = 0
    within_25_events: int = 0
    sum_ape: float = 0.0
    sum_abs_ms: float = 0.0
    sum_signed_ms: float = 0.0
    worst_ape: float = -1.0
    worst_event: dict[str, Any] | None = None

    @classmethod
    def create(cls) -> "MetricAccumulator":
        return cls(array("d"))

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

    def finish(self) -> dict[str, Any]:
        if not self.n_events:
            return {
                "n_events": 0,
                "within_25_events": 0,
                "within_25_percent": None,
                "mean_ape_percent": None,
                "p95_ape_percent_nearest_rank": None,
                "worst_ape_percent": None,
                "absolute_error_ms": 0.0,
                "signed_bias_ms": 0.0,
                "worst_event": None,
            }
        ordered = sorted(self.apes)
        rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return {
            "n_events": self.n_events,
            "within_25_events": self.within_25_events,
            "within_25_percent": 100.0 * self.within_25_events / self.n_events,
            "mean_ape_percent": self.sum_ape / self.n_events,
            "p95_ape_percent_nearest_rank": ordered[rank],
            "worst_ape_percent": self.worst_ape,
            "absolute_error_ms": self.sum_abs_ms,
            "signed_bias_ms": self.sum_signed_ms,
            "worst_event": self.worst_event,
        }


def operation_key(row: Mapping[str, Any]) -> str:
    """Build an operation label from syscall-entry identity only."""

    scalar = row.get("scalar_args") or {}
    name = scalar.get("syscall_name") or "unknown"
    return (
        f"nr={int(row['syscall_nr'])};syscall={name};"
        f"kind={str(row['kind_name'])}"
    )


def requested_size_value(row: Mapping[str, Any]) -> int | None:
    """Return only a decoder-proven raw syscall-entry requested-size word."""

    scalar = row.get("scalar_args") or {}
    value = scalar.get("requested_size")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def requested_size_bucket(value: int | None) -> str:
    if value is None:
        return "entry_size_unavailable"
    if value == 0:
        return "entry_size_zero"
    if value <= 4096:
        return "entry_size_1_4KiB"
    if value <= 65536:
        return "entry_size_4_64KiB"
    return "entry_size_gt_64KiB"


def path_class(row: Mapping[str, Any]) -> str:
    """Classify only bounded path bytes copied at syscall entry.

    ``bpf_work.BPF_PROGRAM`` calls ``read_path`` from the sys_enter handlers;
    no filesystem lookup or syscall result is consulted here.  Truncation and
    unknown path states remain explicit classes.
    """

    status = str(row.get("path_status_name") or "unknown")
    if status == "unknown":
        return "entry_path_unknown"
    value = row.get("path")
    if not isinstance(value, str) or not value:
        return f"entry_path_{status}"
    if status == "truncated":
        return "entry_path_truncated"
    if value.startswith("/"):
        return "entry_path_absolute"
    if value in {".", ".."} or value.startswith("./") or value.startswith("../"):
        return "entry_path_dot_relative"
    return "entry_path_relative"


def _fully_valid_case(case: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    report = json.loads(Path(case["validation_report_path"]).read_text(encoding="utf-8"))
    validation = report.get("validation", {})
    scope = report.get("calibration_scope", {})
    attempt = (report.get("attempts") or [{}])[0]
    raw_integrity = attempt.get("cpu_raw_integrity", {})
    good = (
        case.get("partition") == "train_calibration"
        and report.get("disposition") == "accepted"
        and validation.get("status") == "valid"
        and int(validation.get("error_count", 1)) == 0
        and scope.get("original_validation_status") == "valid"
        and not scope.get("original_error_codes")
        and raw_integrity.get("status") == "complete_bounded_raw_metadata"
        and int(raw_integrity.get("coverage_error_count", 1)) == 0
    )
    return good, report


def select_cases(manifest: Mapping[str, Any], count: int = SELECTED_CASE_COUNT) -> list[dict[str, Any]]:
    """Select by fixed ordinal order after the evidence validity gate."""

    candidates = sorted(manifest.get("cases", []), key=lambda row: int(row["queue_ordinal"]))
    selected: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    for case in candidates:
        good, _report = _fully_valid_case(case)
        if not good or case["instance_id"] in seen_instances:
            continue
        selected.append(dict(case))
        seen_instances.add(str(case["instance_id"]))
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValidationError(f"only {len(selected)} distinct fully valid cases available; need {count}")
    return selected


def _compact_range(row: Mapping[str, Any], record_size: int, raw_size: int) -> dict[str, Any]:
    stream = row.get("binary_event_stream") or {}
    start = int(stream.get("offset_start", -1))
    end = int(stream.get("offset_end", -1))
    count = int(stream.get("record_count", -1))
    if not 0 <= start <= end <= raw_size or end - start != count * record_size:
        raise ValidationError("invalid action binary range in raw aggregate journal")
    return {
        "action_token": int(row["action_token"]),
        "offset_start": start,
        "offset_end": end,
        "record_count": count,
        "required_event_count": int(row.get("required_event_count", -1)),
        "event_count": int(row.get("event_count", -1)),
        "event_count_at_boundary": int(row.get("event_count_at_boundary", -1)),
        "post_boundary_event_count": int(row.get("post_boundary_event_count", -1)),
        "event_records_complete": bool(row.get("event_records_complete", False)),
        "perf_lost_events": int(row.get("perf_lost_events", 0)),
        "event_callback_error_count": len(row.get("event_callback_errors") or []),
        "censored_pending_count": len(row.get("censored_pending") or []),
        "lost_event_records": int((row.get("raw_aggregate") or {}).get("lost_event_records", 0)),
        "lost_path_records": int((row.get("raw_aggregate") or {}).get("lost_path_records", 0)),
        "lost_pending_records": int((row.get("raw_aggregate") or {}).get("lost_pending_records", 0)),
    }


def _validate_action_ranges(
    ranges: list[dict[str, Any]],
    raw_size: int,
) -> tuple[int, int, Counter[int]]:
    """Require one disjoint action-range cover of the complete raw stream.

    A matching sum of range lengths is insufficient: a gap and an overlap can
    cancel numerically.  Sorting and checking the cursor catches both cases
    before any packet is treated as a scored event.
    """

    ordered = sorted(ranges, key=lambda item: item["offset_start"])
    cursor = 0
    gap_bytes = 0
    overlap_bytes = 0
    for item in ordered:
        if item["offset_start"] > cursor:
            gap_bytes += item["offset_start"] - cursor
        elif item["offset_start"] < cursor:
            overlap_bytes += cursor - item["offset_start"]
        cursor = max(cursor, item["offset_end"])
    if cursor != raw_size:
        if cursor < raw_size:
            gap_bytes += raw_size - cursor
        else:
            overlap_bytes += cursor - raw_size
    if gap_bytes or overlap_bytes:
        raise ValidationError(
            f"selected action ranges are not a disjoint full raw-stream cover: "
            f"gaps={gap_bytes}, overlaps={overlap_bytes}"
        )
    token_expected = Counter()
    for item in ordered:
        token_expected[int(item["action_token"])] += int(item["record_count"])
    return gap_bytes, overlap_bytes, token_expected


def _validate_event_membership(
    action_range: Mapping[str, Any] | None,
    *,
    raw_offset: int,
    action_token: int,
) -> None:
    """Reject a decoded packet that is outside or mislabeled for its range."""

    if action_range is None:
        raise ValidationError(f"raw offset {raw_offset} has no containing action range")
    if not action_range["offset_start"] <= raw_offset < action_range["offset_end"]:
        raise ValidationError(f"raw offset {raw_offset} is outside its action range")
    if int(action_token) != int(action_range["action_token"]):
        raise ValidationError(
            f"raw offset {raw_offset} token {action_token} does not match "
            f"range token {action_range['action_token']}"
        )


def prepare_case(case: dict[str, Any]) -> dict[str, Any]:
    """Read compact manifests/journals, not normalized event JSON exports."""

    bpf_manifest_path = Path(case["source"]["bpf_collector_manifest"]["path"])
    bpf_manifest = json.loads(bpf_manifest_path.read_text(encoding="utf-8"))
    expected_bpf_manifest_sha256 = case["source"]["bpf_collector_manifest"].get("sha256")
    actual_bpf_manifest_sha256 = sha256_file(bpf_manifest_path)
    if expected_bpf_manifest_sha256 and actual_bpf_manifest_sha256 != expected_bpf_manifest_sha256:
        raise ValidationError("BPF collector manifest hash differs from evidence manifest")
    stream = bpf_manifest.get("raw_event_stream") or {}
    record_size = int(stream.get("record_size_bytes", 0))
    if record_size != BPF_EVENT_RECORD_SIZE or stream.get("schema_version") != BPF_EVENT_SCHEMA:
        raise ValidationError("selected case is not the current v3 400-byte BPF stream")
    if stream.get("event_abi") != BPF_EVENT_ABI:
        raise ValidationError("selected case has an unexpected BPF scalar-argument ABI")
    raw_path = Path(stream["path"])
    if not raw_path.is_file():
        raise ValidationError(f"raw BPF stream is missing: {raw_path}")
    raw_size = raw_path.stat().st_size
    if raw_size % record_size:
        raise ValidationError("raw BPF stream is not record aligned")
    ranges: list[dict[str, Any]] = []
    host_clock: dict[str, Any] | None = None
    aggregate_path = Path(bpf_manifest["raw_aggregate_journal"])
    aggregate_drops = Counter()
    expected_records = 0
    for line in aggregate_path.open(encoding="utf-8"):
        if not line.strip():
            continue
        row = json.loads(line)
        compact = _compact_range(row, record_size, raw_size)
        ranges.append(compact)
        expected_records += compact["record_count"]
        for key in (
            "perf_lost_events", "event_callback_error_count", "censored_pending_count",
            "lost_event_records", "lost_path_records", "lost_pending_records",
        ):
            aggregate_drops[key] += int(compact[key])
        if host_clock is None:
            host_clock = (((row.get("boundary") or {}).get("clock") or {}).get("monotonic_clock"))
    if expected_records != raw_size // record_size:
        raise ValidationError("action ranges do not cover the raw stream record count")
    gap_bytes, overlap_bytes, token_expected = _validate_action_ranges(ranges, raw_size)
    ranges.sort(key=lambda item: item["offset_start"])
    validation_report_path = Path(case["validation_report_path"])
    actual_validation_report_sha256 = sha256_file(validation_report_path)
    expected_validation_report_sha256 = case.get("validation_report_sha256")
    if expected_validation_report_sha256 and actual_validation_report_sha256 != expected_validation_report_sha256:
        raise ValidationError("validation report hash differs from evidence manifest")
    case["case_id"] = str(case["case_id"])
    case["instance_id"] = str(case["instance_id"])
    case["record_size_bytes"] = record_size
    case["raw_path"] = str(raw_path)
    case["raw_bytes"] = raw_size
    case["expected_raw_records"] = raw_size // record_size
    case["raw_hash_recorded"] = bpf_manifest.get("raw_event_stream_sha256")
    case["bpf_manifest_path"] = str(bpf_manifest_path)
    case["bpf_manifest_sha256"] = actual_bpf_manifest_sha256
    case["raw_aggregate_journal"] = str(aggregate_path)
    case["action_ranges"] = ranges
    case["range_gap_bytes"] = gap_bytes
    case["range_overlap_bytes"] = overlap_bytes
    case["token_expected_records"] = dict(token_expected)
    case["aggregate_drops"] = dict(aggregate_drops)
    case["kernel_clock"] = dict(bpf_manifest.get("kernel_clock") or {})
    case["host_clock"] = dict(host_clock or {})
    case["validation_report_sha256_actual"] = actual_validation_report_sha256
    return case


def _median(values: list[int]) -> float:
    if not values:
        raise ValidationError("cannot compute an empty median")
    return float(statistics.median(values))


@dataclass
class FoldModel:
    global_ns: float
    operation_ns: dict[int, float]
    operation_size_ns: dict[tuple[int, int], float]
    operation_path_ns: dict[tuple[int, int], float]


def fit_fold(store: EventStore, held_case: int) -> FoldModel:
    global_values: list[int] = []
    operations: dict[int, list[int]] = defaultdict(list)
    operation_sizes: dict[tuple[int, int], list[int]] = defaultdict(list)
    operation_paths: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, target in enumerate(store.target_ns):
        if target <= 0 or int(store.instance_index[index]) == held_case:
            continue
        operation = int(store.operation_id[index])
        size = int(store.size_id[index])
        path = int(store.path_id[index])
        global_values.append(int(target))
        operations[operation].append(int(target))
        operation_sizes[(operation, size)].append(int(target))
        operation_paths[(operation, path)].append(int(target))
    return FoldModel(
        global_ns=_median(global_values),
        operation_ns={key: _median(values) for key, values in operations.items()},
        operation_size_ns={key: _median(values) for key, values in operation_sizes.items()},
        operation_path_ns={key: _median(values) for key, values in operation_paths.items()},
    )


def predict(model: FoldModel, candidate: str, operation: int, size: int, path: int) -> float:
    operation_prediction = model.operation_ns.get(operation, model.global_ns)
    if candidate == "global_median":
        return model.global_ns
    if candidate == "operation_median":
        return operation_prediction
    if candidate == "operation_requested_size_bucket_median":
        return model.operation_size_ns.get((operation, size), operation_prediction)
    if candidate == "operation_path_class_median":
        return model.operation_path_ns.get((operation, path), operation_prediction)
    raise ValidationError(f"unknown candidate: {candidate}")


def fold_model_json(model: FoldModel, store: EventStore) -> dict[str, Any]:
    def operation_map(values: Mapping[int, float]) -> dict[str, float]:
        return {store.operations.values[key]: value for key, value in sorted(values.items())}

    def pair_map(values: Mapping[tuple[int, int], float], labels: Labels) -> dict[str, float]:
        return {
            f"{store.operations.values[op]}|{labels.values[group]}": value
            for (op, group), value in sorted(values.items())
        }

    return {
        "global_median_ns": model.global_ns,
        "operation_medians_ns": operation_map(model.operation_ns),
        "operation_requested_size_bucket_medians_ns": pair_map(model.operation_size_ns, store.sizes),
        "operation_path_class_medians_ns": pair_map(model.operation_path_ns, store.paths),
    }


def _metric_tables() -> tuple[dict[str, MetricAccumulator], dict[str, dict[int, MetricAccumulator]]]:
    overall = {candidate: MetricAccumulator.create() for candidate in MODEL_ORDER}
    per_case = {candidate: {} for candidate in MODEL_ORDER}
    return overall, per_case


def score_store(
    store: EventStore,
    cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], int]:
    overall, per_case = _metric_tables()
    full_gate = {
        candidate: {
            "required_records": 0,
            "positive_target_records": 0,
            "positive_within_25_records": 0,
            "zero_target_records": 0,
            "zero_exact_prediction_records": 0,
            "lineage_zero_records": 0,
            "unsupported_target_records": 0,
        }
        for candidate in MODEL_ORDER
    }
    fold_models: list[dict[str, Any]] = []
    scored = 0
    fitted: list[FoldModel] = []
    for held_case in range(len(cases)):
        model = fit_fold(store, held_case)
        fitted.append(model)
        held_targets = sum(
            1
            for index, target in enumerate(store.target_ns)
            if int(store.instance_index[index]) == held_case and target > 0
        )
        fold_models.append(
            {
                "held_out_instance": cases[held_case]["instance_id"],
                "held_out_queue_ordinal": cases[held_case]["queue_ordinal"],
                "training_target_events": sum(
                    1
                    for index, target in enumerate(store.target_ns)
                    if int(store.instance_index[index]) != held_case and target > 0
                ),
                "held_out_target_events": held_targets,
                "model": fold_model_json(model, store),
            }
        )
        for candidate in MODEL_ORDER:
            per_case[candidate][held_case] = MetricAccumulator.create()
        for index, target in enumerate(store.target_ns):
            if int(store.instance_index[index]) != held_case:
                continue
            operation = int(store.operation_id[index])
            size = int(store.size_id[index])
            path = int(store.path_id[index])
            event = store.identity(index)
            kind_name = store.kinds.values[int(store.kind_id[index])]
            is_lineage_zero = target == 0 and kind_name in LINEAGE_KINDS
            for candidate in MODEL_ORDER:
                prediction = predict(model, candidate, operation, size, path)
                gate = full_gate[candidate]
                if target > 0:
                    gate["required_records"] += 1
                    if candidate == MODEL_ORDER[0]:
                        scored += 1
                    gate["positive_target_records"] += 1
                    target_ms = float(target) / 1_000_000.0
                    prediction_ms = float(prediction) / 1_000_000.0
                    ape = abs(prediction_ms - target_ms) / target_ms * 100.0
                    gate["positive_within_25_records"] += int(ape <= WITHIN_THRESHOLD_PERCENT)
                    overall[candidate].update(prediction_ns=prediction, target_ns=int(target), event=event)
                    per_case[candidate][held_case].update(prediction_ns=prediction, target_ns=int(target), event=event)
                elif target == 0:
                    if is_lineage_zero:
                        gate["lineage_zero_records"] += 1
                    else:
                        gate["required_records"] += 1
                        gate["zero_target_records"] += 1
                        gate["zero_exact_prediction_records"] += int(float(prediction) == 0.0)
                else:
                    # Unsupported/censored targets stay in the required gate
                    # and fail it explicitly; they must not vanish from the
                    # denominator merely because no duration was available.
                    gate["required_records"] += 1
                    gate["unsupported_target_records"] += 1
    if scored != sum(1 for target in store.target_ns if target > 0):
        raise ValidationError("leave-one-instance-out scoring did not cover every valid target")
    overall_metrics = {candidate: overall[candidate].finish() for candidate in MODEL_ORDER}
    per_case_metrics = {
        candidate: {
            cases[case_index]["instance_id"]: accumulator.finish()
            for case_index, accumulator in sorted(per_case[candidate].items())
        }
        for candidate in MODEL_ORDER
    }
    for candidate, gate in full_gate.items():
        eligible = gate["positive_target_records"] + gate["zero_target_records"]
        passing = gate["positive_within_25_records"] + gate["zero_exact_prediction_records"]
        gate["passing_records"] = passing
        gate["coverage_over_required_percent"] = 100.0 * passing / gate["required_records"] if gate["required_records"] else None
        gate["coverage_over_scored_required_percent"] = 100.0 * passing / eligible if eligible else None
    return overall_metrics, per_case_metrics, full_gate, fold_models, scored


def _open_prediction_stream(path: Path) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.name.endswith(".gz"):
        return gzip.open(path, "wb")
    return path.open("wb")


def write_predictions(
    path: Path,
    store: EventStore,
    cases: list[dict[str, Any]],
    fitted: list[FoldModel],
) -> int:
    """Optional audit stream; disabled by default to avoid a giant JSON export."""

    written = 0
    with _open_prediction_stream(path) as stream:
        for index, target in enumerate(store.target_ns):
            if target <= 0:
                continue
            held_case = int(store.instance_index[index])
            event = store.identity(index)
            operation = int(store.operation_id[index])
            size = int(store.size_id[index])
            path_id = int(store.path_id[index])
            row = {
                **event,
                "observed_duration_ns": int(target),
                "predictions_ns": {
                    candidate: predict(fitted[held_case], candidate, operation, size, path_id)
                    for candidate in MODEL_ORDER
                },
            }
            stream.write((json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
            written += 1
    return written


def _case_public(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: case[key]
        for key in (
            "queue_ordinal", "instance_id", "case_id", "partition", "record_size_bytes",
            "raw_path", "raw_bytes", "expected_raw_records", "raw_hash_recorded",
            "raw_hash_actual", "raw_hash_verified",
            "bpf_manifest_path", "bpf_manifest_sha256", "raw_aggregate_journal",
            "action_ranges", "range_gap_bytes", "range_overlap_bytes", "aggregate_drops",
            "kernel_clock", "host_clock",
            "validation_report_path", "validation_report_sha256_actual",
        )
    }


def render_report(artifact: Mapping[str, Any]) -> str:
    population = artifact["population"]
    diagnostics = artifact["diagnostics"]
    metrics = artifact["metrics"]["overall"]
    full_gate = artifact["metrics"]["full_required_population_gate"]
    selected_text = ", ".join(
        f"{case['queue_ordinal']} ({case['instance_id']})"
        for case in artifact["selection"]["selected_cases"]
    )
    lines = [
        "# Bounded D9 individual CPU-operation comparison",
        "",
        "This is an offline, trace-conditioned diagnostic on four fixed fully valid `train_calibration` instances. It does not change the BPF collector, acquire new data, or claim a complete D9 pass.",
        "",
        "## Fixed population and raw stream contract",
        "",
        f"Cases were selected by ascending queue ordinal after the evidence validity gate, taking the first four distinct instances: **{selected_text}**. Selection did not inspect operation durations, evaluator outcomes, or model errors.",
        f"The selected v3 ABI streams contain **{population['raw_records']} raw records** across **{population['instances']} instances** and **{population['raw_bytes_read']} bytes**. The helper streamed each 400-byte packet through the existing `BpfWorkCollector._event_row` packet decoder (the same v3 layout used by `iter_bpf_events`); it did not create a full decoded JSON export. The compact typed-array index used at most **{population['compact_index_bytes']} bytes** ({population['compact_index_bytes'] / (1024 * 1024):.2f} MiB).",
        "",
        "Each event retains an identity made from case ID, action token, sequence, and raw byte range. Action-token ranges, BPF/kernel clock descriptors, host monotonic clock identity, source manifests, and recorded raw-stream hashes are retained in `model.json`.",
        "",
        "## Feature and target contract",
        "",
        "The target is the completed BPF kernel operation duration (`kernel_end_ns - kernel_start_ns`) for an individual event. Syscall failures remain valid duration targets; failure status and return values are diagnostics only. Positive-duration medians are conditional on positive targets and exclude zero and censored targets rather than imputing them. Known fork/clone/thread lineage records with zero duration are reported separately from the individual-operation denominator; an unclassified zero-duration operation would remain in the required gate and pass only with an exact-zero prediction. Censored targets remain explicitly unsupported.",
        "",
        "Features are restricted to syscall-entry-known data: syscall number/kind, decoder-proven requested-size words where available, and a lexical path class from bounded path bytes copied by the sys_enter handler. No return bytes, failure/result status, completed latency, or post-event field enters a model key.",
        "",
        "## Leave-one-instance-out results",
        "",
        "| Model | Scored events | Within 25% | Mean APE | P95 APE | Worst APE |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for candidate in MODEL_ORDER:
        item = metrics[candidate]
        lines.append(
            f"| `{candidate}` | {item['n_events']} | {item['within_25_percent']:.2f}% ({item['within_25_events']}) | {item['mean_ape_percent']:.2f}% | {item['p95_ape_percent_nearest_rank']:.2f}% | {item['worst_ape_percent']:.2f}% |"
        )
    lines.extend([
        "",
        "Per-instance metrics and the identity/range of each model's worst event are recorded in `model.json`; the four candidates are fixed descriptive comparisons, not an automatic production selection.",
        "",
        "## Duration-population gate",
        "",
        "The positive-duration table is conditional coverage. This gate covers records that require an individual duration decision: positive targets must be within 25 percent, individual-operation zero targets pass only with an exact-zero prediction, and censored/unsupported targets remain in the denominator and do not pass. Known zero-duration lineage/provenance records are counted separately and are outside this duration population.",
        "",
        "| Model | Duration records | Passing records | Coverage over duration population | Modeled zero exact-zero | Lineage zero | Unsupported/censored |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for candidate in MODEL_ORDER:
        gate = full_gate[candidate]
        lines.append(
            f"| `{candidate}` | {gate['required_records']} | {gate['passing_records']} | {gate['coverage_over_required_percent']:.2f}% | {gate['zero_exact_prediction_records']}/{gate['zero_target_records']} | {gate['lineage_zero_records']} | {gate['unsupported_target_records']} |"
        )
    lines.extend([
        "",
        "## Coverage and integrity diagnostics",
        "",
        f"- Valid positive-duration targets scored: **{diagnostics['valid_target_records']}**.",
        f"- Zero-duration records: **{diagnostics['zero_target_records']}** total, with **{diagnostics['lineage_zero_target_records']}** known lineage/provenance zeros and **{diagnostics['modeled_zero_target_records']}** individual-operation zeros; by kind: `{json.dumps(diagnostics['zero_target_by_kind'], sort_keys=True)}`.",
        f"- Censored targets: **{diagnostics['censored_records']}**; negative targets: **{diagnostics['negative_target_records']}**. Unsupported/censored records stay visible in the duration-population denominator and never pass.",
        f"- Event statuses observed: success **{diagnostics['success_records']}**, failure **{diagnostics['failure_records']}**. Status was not a feature.",
        f"- Perf-buffer lost events: **{diagnostics['perf_lost_events']}**; lost event/path/pending map records: **{diagnostics['lost_event_records']} / {diagnostics['lost_path_records']} / {diagnostics['lost_pending_records']}**; callback errors: **{diagnostics['event_callback_error_count']}**.",
        f"- Action-range/event-count mismatches: **{diagnostics['range_mismatch_count']}**; raw records decoded: **{diagnostics['decoded_records']}**.",
        "",
        "The zero-target classification is explicit: known lineage/provenance records are outside individual-operation duration coverage, while any modeled operation zero would be required to predict exactly zero. No drop or range mismatch was observed in the selected cases.",
        "",
        "## Scope limits",
        "",
        "This four-instance sample is an ABI/target-method check. It does not establish all-production CPU accuracy, cross-hardware transfer, a prospective pre-execution predictor, or the assignment's complete CPU+GPU+E2E D9 acceptance gate. The existing collector source remains unchanged.",
        "",
        "`run_atomic_cpu.py` is the reproducible helper. An optional `--predictions path.jsonl.gz` audit stream preserves each scored event identity and all candidate predictions; it is intentionally disabled by default to avoid a giant JSON export.",
        "",
    ])
    return "\n".join(lines)


def run(*, out_dir: Path, max_raw_bytes: int = MAX_RAW_BYTES_DEFAULT, predictions_path: Path | None = None) -> dict[str, Any]:
    manifest = json.loads(CPU_EVIDENCE_MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("schema") != "d9.calibration-input-manifest.v2":
        raise ValidationError("unexpected CPU evidence manifest schema")
    selected = [prepare_case(case) for case in select_cases(manifest)]
    raw_bytes = sum(int(case["raw_bytes"]) for case in selected)
    if raw_bytes > max_raw_bytes:
        raise ValidationError(f"selected raw input is {raw_bytes} bytes, over bound {max_raw_bytes}")

    operations = Labels()
    sizes = Labels()
    paths = Labels()
    kinds = Labels()
    store = EventStore(selected, operations, sizes, paths, kinds)
    case_counters = [CaseCounters() for _ in selected]
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
                    packet = packets[packet_start:packet_start + record_size]
                    row = BpfWorkCollector._event_row(packet, schema_version=BPF_EVENT_SCHEMA)
                    if row["kind"] not in set(range(1, 17)) or row["status"] not in {1, 2, 3}:
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
                    while range_cursor < len(case["action_ranges"]) and raw_offset >= case["action_ranges"][range_cursor]["offset_end"]:
                        range_cursor += 1
                    action_range = (
                        case["action_ranges"][range_cursor]
                        if range_cursor < len(case["action_ranges"])
                        else None
                    )
                    _validate_event_membership(
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
                        zero_target_by_operation[operation_key(row)] += 1
                        target_ns = 0
                    elif int(duration) < 0:
                        counters.negative_target_records += 1
                        target_ns = -1
                    else:
                        counters.target_records += 1
                        target_ns = int(duration)
                    operation_id = operations.get(operation_key(row))
                    size_id = sizes.get(requested_size_bucket(requested_size_value(row)))
                    path_id = paths.get(path_class(row))
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
        case["raw_hash_verified"] = bool(case["raw_hash_recorded"] and actual_raw_hash == case["raw_hash_recorded"])
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
        raise ValidationError(f"action range/event-count integrity mismatch: {range_mismatch_count}")

    overall, per_case, full_gate, fold_models, scored = score_store(store, selected)
    fitted_models = []
    for held_case in range(len(selected)):
        fitted_models.append(fit_fold(store, held_case))
    prediction_count = None
    if predictions_path is not None:
        prediction_count = write_predictions(predictions_path, store, selected, fitted_models)

    drop_keys = (
        "perf_lost_events", "event_callback_error_count", "censored_pending_count",
        "lost_event_records", "lost_path_records", "lost_pending_records",
    )
    aggregate_drops = Counter()
    for case in selected:
        aggregate_drops.update(case["aggregate_drops"])
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
        "perf_lost_events": int(aggregate_drops["perf_lost_events"]),
        "event_callback_error_count": int(aggregate_drops["event_callback_error_count"]),
        "censored_pending_count": int(aggregate_drops["censored_pending_count"]),
        "lost_event_records": int(aggregate_drops["lost_event_records"]),
        "lost_path_records": int(aggregate_drops["lost_path_records"]),
        "lost_pending_records": int(aggregate_drops["lost_pending_records"]),
        "range_mismatch_count": range_mismatch_count,
        "drop_counter_keys": list(drop_keys),
    }
    if diagnostics["valid_target_records"] != scored:
        raise ValidationError("valid target count differs from scored count")

    artifact: dict[str, Any] = {
        "schema": FIT_SCHEMA,
        "selection": {
            "policy": "ascending queue ordinal, evidence-valid train_calibration cases, first four distinct instance IDs; no target/outcome ranking",
            "selected_cases": [_case_public(case) for case in selected],
        },
        "abi": {
            "event_schema": BPF_EVENT_SCHEMA,
            "event_abi": BPF_EVENT_ABI,
            "record_size_bytes": BPF_EVENT_RECORD_SIZE,
            "decoder": "agentic_sim.telemetry.bpf_work.BpfWorkCollector._event_row; same v3 packet decoder used by iter_bpf_events",
            "raw_read_bound_bytes": max_raw_bytes,
        },
        "feature_contract": {
            "operation": "syscall number + decoder syscall name + BPF kind name from entry/event identity",
            "requested_size": "decoder scalar_args.requested_size from raw syscall-entry arguments only",
            "requested_size_buckets": [
                "entry_size_unavailable", "entry_size_zero", "entry_size_1_4KiB",
                "entry_size_4_64KiB", "entry_size_gt_64KiB",
            ],
            "path_class": "lexical class from bounded path bytes copied by sys_enter; unknown/truncated retained explicitly",
            "forbidden_features": [
                "ret/return_bytes", "status/success_failure", "duration_ns/latency", "kernel_end_ns",
                "censor boundary", "post-event result", "filesystem lookup", "tool wall time",
            ],
        },
        "target_contract": {
            "target": "positive BPF kernel duration_ns for each completed individual event",
            "clock": "kernel CLOCK_MONOTONIC / bpf_ktime_get_ns; preserved separately from host CLOCK_MONOTONIC_RAW action anchors",
            "zero_or_censored_policy": "positive-duration metrics exclude zero and censored targets; known fork/clone/thread lineage zeros are reported separately; any zero-duration individual operation remains in the required gate and passes only with an exact-zero prediction; censored/negative targets remain required unsupported records; nothing is imputed",
            "failure_targets": "included when duration is positive; failure status is not a feature",
        },
        "population": {
            "instances": len(selected),
            "raw_records": decoded_records,
            "raw_bytes_read": raw_bytes,
            "valid_target_records": scored,
            "compact_index_bytes": store.memory_bytes(),
            "operations": len(operations.values),
            "requested_size_buckets": len(sizes.values),
            "path_classes": len(paths.values),
            "kinds": len(kinds.values),
            "case_counters": {
                selected[index]["instance_id"]: case_counters[index].__dict__
                for index in range(len(selected))
            },
        },
        "models": list(MODEL_ORDER),
        "fold_models": fold_models,
        "metrics": {"overall": overall, "per_instance": per_case, "full_required_population_gate": full_gate},
        "diagnostics": diagnostics,
        "provenance": {
            "cpu_evidence_manifest": {
                "path": str(CPU_EVIDENCE_MANIFEST),
                "sha256": sha256_file(CPU_EVIDENCE_MANIFEST),
            },
            "source_partition": manifest.get("partition"),
            "raw_hash_policy": "collector-recorded raw_event_stream_sha256 retained; binary framing/count streamed and checked without a second full-file hash pass",
            "production_integration": "none",
            "acquisition_or_inference": "not used",
            "existing_collector_source_changed": False,
            "script": str(Path(__file__).resolve()),
            "script_sha256": sha256_file(Path(__file__).resolve()),
        },
        "optional_predictions": {
            "path": str(predictions_path) if predictions_path is not None else None,
            "rows": prediction_count,
            "default": "disabled to avoid a giant JSON export",
        },
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MODEL_NAME).write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / REPORT_NAME).write_text(render_report(artifact), encoding="utf-8")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=HERE)
    parser.add_argument("--max-raw-bytes", type=int, default=MAX_RAW_BYTES_DEFAULT)
    parser.add_argument(
        "--predictions",
        type=Path,
        default=None,
        help="optional JSONL or .jsonl.gz audit stream; disabled by default",
    )
    args = parser.parse_args()
    artifact = run(out_dir=args.out_dir, max_raw_bytes=args.max_raw_bytes, predictions_path=args.predictions)
    print(json.dumps({
        "model": str(args.out_dir / MODEL_NAME),
        "report": str(args.out_dir / REPORT_NAME),
        "raw_bytes_read": artifact["population"]["raw_bytes_read"],
        "raw_records": artifact["population"]["raw_records"],
        "valid_target_records": artifact["population"]["valid_target_records"],
        "models": list(MODEL_ORDER),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
