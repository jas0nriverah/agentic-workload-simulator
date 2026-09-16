#!/usr/bin/env python3
"""Deterministically validate and normalize an offline telemetry case.

The validator reads frozen journals only.  It never treats a producer's
``status=proven`` as an interval proof, never aligns clock domains, and never
uses a residual as a target.  It is intentionally usable by downstream
calibration as an importable ``validate_case(case_root, output_dir)`` function.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


class LedgerError(ValueError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LedgerError(f"{path}: expected JSON object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise LedgerError(f"{path}:{number}: expected JSON object")
            row = dict(row)
            row["_source_path"] = str(path)
            row["_line"] = number
            records.append(row)
    return records


def _case_and_attempts(case_root: Path, requested_attempt: str | None = None) -> tuple[Path, list[Path], list[str]]:
    root = case_root.resolve(strict=True)
    if (root / "telemetry_v2").is_dir():
        # An attempt directory: find its owning case specification.
        for parent in (root, *root.parents):
            if (parent / "case_spec.json").is_file():
                return parent, [root], []
        raise LedgerError("attempt root has no ancestor case_spec.json")
    if not (root / "case_spec.json").is_file():
        raise LedgerError(f"{root}: case_spec.json is required before journals are read")
    attempts = sorted(path for path in (root / "runner_attempts").glob("*/") if (path / "telemetry_v2").is_dir())
    if not attempts:
        raise LedgerError(f"{root}: no runner_attempts/*/telemetry_v2 directory")
    if requested_attempt:
        selected = [path for path in attempts if path.name == requested_attempt]
        if not selected:
            raise LedgerError(f"requested attempt does not exist: {requested_attempt}")
        return root, selected, [path.name for path in attempts if path not in selected]
    if len(attempts) == 1:
        return root, attempts, []
    # A case result may explicitly bind an accepted attempt.  Do not silently
    # choose a lexical/latest journal when a case holds retries.
    result_path = root / "case_result.json"
    result = _read_json(result_path) if result_path.is_file() else {}
    accepted = result.get("accepted_attempt_id")
    bound = [path for path in attempts if path.name == accepted]
    if len(bound) != 1:
        raise LedgerError("multiple attempts require --attempt-id or an unambiguous case_result binding")
    return root, bound, [path.name for path in attempts if path not in bound]


def _cpu_integrity(attempt: Path, errors: list[dict[str, Any]], sources: list[dict[str, Any]], root: Path) -> dict[str, Any]:
    """Prove bounded raw BPF coverage; never infer it from semantic tool rows."""
    work = attempt / "telemetry_v2/linux_work"
    required = {"summary": work / "work_summary.json", "manifest": work / "bpf_collector_manifest.json", "binary": work / "raw_events.bin", "aggregates": work / "raw_aggregates.jsonl"}
    if any(not path.is_file() for path in required.values()):
        for label, path in required.items():
            if not path.is_file():
                _record_error(errors, "missing_cpu_raw_artifact", label + ": " + str(path))
        return {"status": "incomplete_missing_raw_artifact", "atomic_normalization": "unsupported_no_full_bpf_export"}
    for path in required.values():
        sources.append({"path": str(path.relative_to(root)), "sha256": _sha256(path)})
    try:
        summary, manifest = _read_json(required["summary"]), _read_json(required["manifest"])
        binary_size = required["binary"].stat().st_size
        actions = summary.get("actions")
        if not isinstance(actions, list) or not actions:
            raise LedgerError("work_summary actions unavailable")
        coverage_errors = 0
        record_size = manifest.get("record_size_bytes")
        for action in actions:
            stream = action.get("raw", {}).get("binary_event_stream", {}) if isinstance(action, Mapping) else {}
            start, end, count = stream.get("offset_start"), stream.get("offset_end"), stream.get("record_count")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, count)) or start < 0 or end < start or end > binary_size:
                coverage_errors += 1
                continue
            if isinstance(record_size, int) and record_size > 0 and end - start != count * record_size:
                coverage_errors += 1
            raw = action.get("raw", {})
            if raw.get("event_records_complete") is not True or any(raw.get(key, 0) not in (0, None) for key in ("perf_lost_events", "lost_event_records", "lost_path_records", "lost_pending_records", "lineage_map_failures")):
                coverage_errors += 1
        if coverage_errors:
            _record_error(errors, "cpu_raw_coverage_or_loss_failure", f"{coverage_errors} action rows have invalid offsets/completeness/loss")
        return {"status": "complete_bounded_raw_coverage" if not coverage_errors else "incomplete_raw_coverage_or_loss", "action_count": len(actions), "raw_binary_bytes": binary_size, "atomic_normalization": "unsupported_no_full_bpf_export", "representative_decoder": "not_run"}
    except (LedgerError, ValueError, TypeError, KeyError) as exc:
        _record_error(errors, "cpu_raw_integrity_parse_failure", str(exc))
        return {"status": "incomplete_raw_integrity_parse_failure", "atomic_normalization": "unsupported_no_full_bpf_export"}


def _clock_domain(row: Mapping[str, Any]) -> tuple[str, str] | None:
    clock = row.get("clock")
    if not isinstance(clock, Mapping):
        return None
    host = clock.get("hostname")
    clock_id = clock.get("clock_id")
    boot = clock.get("boot_id")
    if not isinstance(host, str) or not host or not isinstance(clock_id, str) or not clock_id:
        return None
    # A monotonic clock id is insufficient across reboot; boot id is part of
    # the domain when the producer supplies it.
    return host, f"{clock_id}|boot={boot if isinstance(boot, str) and boot else 'unknown'}"


def _identity_mismatches(row: Mapping[str, Any], expected: Mapping[str, str]) -> list[str]:
    failures: list[str] = []
    values: Mapping[str, Any] = row
    if isinstance(row.get("target_request"), Mapping):
        values = row["target_request"]  # native attribution carries request identity here.
    for name, expected_value in expected.items():
        value = values.get(name)
        if value is not None and value != expected_value:
            failures.append(f"{name}={value!r}, expected {expected_value!r}")
    return failures


def _physical_id(row: Mapping[str, Any]) -> str | None:
    for name in ("physical_request_id", "request_id"):
        value = row.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _interval(row: Mapping[str, Any]) -> tuple[int, int] | None:
    start, end = row.get("start_mono_ns"), row.get("end_mono_ns")
    if isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool):
        return start, end
    # Native attribution has different host clocks and explicit request bounds.
    target = row.get("target_request")
    if isinstance(target, Mapping):
        start, end = target.get("started_monotonic_ns"), target.get("terminal_monotonic_ns")
        if isinstance(start, int) and isinstance(end, int):
            return start, end
    return None


def _union(intervals: Iterable[tuple[int, int]]) -> tuple[int, list[tuple[int, int]]]:
    ordered = sorted(intervals)
    merged: list[list[int]] = []
    for start, end in ordered:
        if end < start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    compact = [(start, end) for start, end in merged]
    return sum(end - start for start, end in compact), compact


def _event_class(row: Mapping[str, Any], source: str) -> str:
    if source == "native":
        return "native_physical_request_terminal"
    kind = str(row.get("event_kind") or "unknown")
    if kind == "model_request_start":
        return "model_request"
    if kind == "model_request":
        return "model_request"
    if kind in {"model_client_call", "model_client_call_start"}:
        return "model_client_call"
    if kind == "tool_event":
        return "semantic_action"
    if kind == "runtime_command":
        return "runtime_command"
    if kind == "tool_intent":
        return "tool_intent"
    if source == "lifecycle" and row.get("terminal") is True:
        return "lifecycle:" + kind
    if kind.endswith("_start"):
        return "evidence_observation"
    return "evidence_observation"


def _target_boundary(row: Mapping[str, Any], source: str) -> str:
    if source == "native":
        return "native_per_request_duration"
    phase = row.get("phase")
    if isinstance(phase, str) and phase:
        return phase
    return str(row.get("event_kind") or "unknown")


def _prospective_features(row: Mapping[str, Any], event_class: str) -> tuple[dict[str, Any] | None, str]:
    # Only starts/intents expose a feature payload.  Terminal labels are never
    # copied into a training row, even where raw journals repeat the payload.
    kind = str(row.get("event_kind") or "")
    if not kind.endswith("_start"):
        return None, "not_pre_event"
    # Emit only the repaired calibration allowlist, synthesized solely from
    # declared pre-dispatch fields.  Do not pass the rich raw feature payload.
    features: dict[str, Any] = {}
    if kind == "model_request_start":
        features["request_kind"] = "model_request"
        value = row.get("max_output_tokens")
        if isinstance(value, int) and value >= 0:
            features["max_output_tokens_bucket"] = str(value)
    elif kind == "tool_event_start":
        features["semantic_class"] = "tool"
        for output, source in (("operation_class", "operation_class"), ("operation", "tool_name"), ("execution_mode", "traversal_mode")):
            value = row.get(source)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value):
                features[output] = str(value)
    else:
        # A matched lifecycle/runtime start is a safe intercept-only path.
        features["semantic_class"] = "lifecycle"
        features["operation"] = kind[:-6]
    return features, "declared_pre_event"


def _normalized(row: Mapping[str, Any], source: str, identity: Mapping[str, str], partition: Any, joined_features: tuple[dict[str, Any] | None, str] | None = None, metric: str | None = None) -> dict[str, Any]:
    klass = _event_class(row, source)
    features, provenance = joined_features if joined_features is not None else _prospective_features(row, klass)
    interval = _interval(row)
    domain = _clock_domain(row)
    event_id = row.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        # Native journal records lack ordinary event ids.  Source content ID is
        # deterministic and avoids pretending their sequence is a host event id.
        event_id = "native:" + hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    observed_ms = None if interval is None else (interval[1] - interval[0]) / 1_000_000
    if source == "native" and isinstance(row.get("metrics"), Mapping):
        observed_ms = row["metrics"].get(metric or "e2e", {}).get("value_ms") if isinstance(row["metrics"].get(metric or "e2e"), Mapping) else observed_ms
        klass = "native:" + str(metric or "e2e")
    is_target = klass in {"semantic_action", "runtime_command"} or klass.startswith("native:") or (klass.startswith("lifecycle:") and klass != "lifecycle:unknown_residual")
    eligible = bool(features is not None and is_target)
    return {
        "instance_id": identity["instance_id"], "case_id": identity["case_id"], "attempt_id": str(row.get("attempt_id") or identity["attempt_id"]),
        "event_id": event_id if metric is None else event_id + ":" + metric, "event_class": klass, "target_boundary": ("native_per_request_" + metric if source == "native" and metric else _target_boundary(row, source)), "observed_ms": observed_ms,
        "features": features, "feature_provenance": {"availability": provenance, "feature_contract": "pre_event_only" if features is not None else "none", "source_event_id": row.get("event_id") if row.get("event_kind") == "model_request_start" else None}, "host_id": None if domain is None else domain[0],
        "clock_id": None if domain is None else domain[1], "physical_request_id": _physical_id(row), "partition": partition,
        "start_mono_ns": None if interval is None else interval[0], "end_mono_ns": None if interval is None else interval[1],
        "pre_event_id": row.get("event_id") if row.get("event_kind") == "model_request_start" else None,
        "model_eligible": eligible, "model_eligibility_reason": "matched_pre_event_features" if eligible else "wrapper_bookkeeping_or_missing_pre_event_features",
        "record_role": "TARGET" if eligible and isinstance(observed_ms, (int, float)) and observed_ms >= 0 else "OBSERVATION",
    }


def _record_error(errors: list[dict[str, Any]], code: str, detail: str, row: Mapping[str, Any] | None = None) -> None:
    item: dict[str, Any] = {"code": code, "detail": detail}
    if row is not None:
        item["source"] = row.get("_source_path")
        item["line"] = row.get("_line")
        item["event_id"] = row.get("event_id")
    errors.append(item)


def validate_case(case_root: Path | str, output_dir: Path | str, attempt_id: str | None = None) -> dict[str, Any]:
    """Validate a frozen case and write deterministic report and JSONL ledger.

    Validation faults are reported in the returned report instead of causing
    partial output to disappear. Structural read errors still raise LedgerError.
    """
    case, attempts, excluded_attempts = _case_and_attempts(Path(case_root), attempt_id)
    spec = _read_json(case / "case_spec.json")  # Mandatory before all journals.
    if not isinstance(spec.get("instance_id"), str) or not isinstance(spec.get("case_id"), str):
        raise LedgerError("case_spec.json lacks instance_id or case_id")
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    errors: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = [{"path": "case_spec.json", "sha256": _sha256(case / "case_spec.json")}]
    result_path = case / "case_result.json"
    case_result: dict[str, Any] | None = None
    if result_path.is_file():
        sources.append({"path": "case_result.json", "sha256": _sha256(result_path)})
        case_result = _read_json(result_path)
    normalized_rows: list[dict[str, Any]] = []
    attempts_report: list[dict[str, Any]] = []
    partition = spec.get("partition")  # supplied, never inferred here
    confirmation = bool(spec.get("outcome_blind")) or "confirmation" in str(spec.get("namespace", ""))
    hardware_bindings: dict[str, Any] = {"cpu": "unavailable", "native_gpu": "unavailable"}
    native_fingerprint: str | None = None
    native_profile_host: str | None = None
    native_profile_boot: str | None = None
    inputs = case / "execution_inputs"
    for name, role in (("remote_hardware_profile.json", "native_gpu"), ("serving_fingerprint.json", "native_gpu")):
        path = inputs / name
        if path.is_file():
            sources.append({"path": str(path.relative_to(case)), "sha256": _sha256(path)})
            hardware_bindings[role] = {"path": str(path.relative_to(case)), "sha256": _sha256(path), "binding": "remote serving profile; distinct native host domain"}
            if name == "remote_hardware_profile.json":
                profile = _read_json(path)
                fingerprint = profile.get("serving_fingerprint_sha256")
                if isinstance(fingerprint, str) and isinstance(profile.get("identity"), Mapping) and profile["identity"].get("hostname"):
                    native_fingerprint = _sha256(path)
                    native_profile_host = str(profile["identity"]["hostname"])
                    native_profile_boot = profile["identity"].get("boot_id")

    for attempt in attempts:
        attempt_id = attempt.name
        identity = {"instance_id": spec["instance_id"], "case_id": spec["case_id"], "attempt_id": attempt_id}
        raw: list[tuple[str, dict[str, Any]]] = []
        for source, relative in (("model", "telemetry_v2/model_events.jsonl"), ("tool", "telemetry_v2/tool_events.jsonl"), ("lifecycle", "telemetry_v2/lifecycle_events.jsonl"), ("native", "native_serving/native_attribution.jsonl")):
            path = attempt / relative
            if not path.is_file():
                _record_error(errors, "missing_required_journal", relative)
                continue
            sources.append({"path": str(path.relative_to(case)), "sha256": _sha256(path)})
            for row in _read_jsonl(path):
                raw.append((source, row))
                for mismatch in _identity_mismatches(row, identity):
                    _record_error(errors, "identity_mismatch", mismatch, row)
                if source != "native":
                    for name, expected_value in identity.items():
                        if row.get(name) is None:
                            _record_error(errors, "missing_required_identity", name, row)

        non_native = [row for source, row in raw if source != "native"]
        native = [row for source, row in raw if source == "native"]
        starts = [row for source, row in raw if source == "model" and row.get("event_kind") == "model_request_start"]
        terminals = [row for source, row in raw if source == "model" and row.get("event_kind") == "model_request" and row.get("terminal") is True]
        start_ids, terminal_ids, native_ids = Counter(_physical_id(r) for r in starts), Counter(_physical_id(r) for r in terminals), Counter(_physical_id(r) for r in native)
        for label, counts in (("model_start", start_ids), ("model_terminal", terminal_ids), ("native_terminal", native_ids)):
            for physical_id, count in counts.items():
                if not physical_id:
                    _record_error(errors, "missing_physical_request_id", label)
                elif count != 1:
                    _record_error(errors, "duplicate_physical_request_id", f"{label} {physical_id} occurs {count} times")
        if set(start_ids) != set(terminal_ids) or set(start_ids) != set(native_ids):
            _record_error(errors, "physical_native_bijection_failure", "model starts, model terminals, and native terminals must have identical physical ID sets")

        # Retry labels must identify distinct physical requests and point only at
        # another real physical request. Wrapper client calls cannot satisfy this.
        known_physical = {key for key in start_ids if key}
        pre_features: dict[str, tuple[dict[str, Any] | None, str]] = {}
        pre_event_ids: dict[str, str] = {}
        pre_hardware: dict[str, str | None] = {}
        for row in starts:
            pre_features[_physical_id(row) or ""] = _prospective_features(row, "model_physical_request_start")
            pre_hardware[_physical_id(row) or ""] = row.get("hardware_profile_sha256") if isinstance(row.get("hardware_profile_sha256"), str) else None
            if isinstance(row.get("event_id"), str):
                pre_event_ids[_physical_id(row) or ""] = row["event_id"]
        span_pre: dict[tuple[str, str, Any], tuple[dict[str, Any] | None, str, str | None]] = {}
        span_hardware: dict[tuple[str, str, Any], str | None] = {}
        for source, row in raw:
            kind = str(row.get("event_kind") or "")
            if not kind.endswith("_start") or row.get("terminal") is True:
                continue
            base = kind[:-6]
            key = _physical_id(row) if source == "model" and base == "model_request" else row.get("span_id")
            if key is not None:
                span_pre[(source, base, key)] = (*_prospective_features(row, _event_class(row, source)), row.get("event_id") if isinstance(row.get("event_id"), str) else None)
                span_hardware[(source, base, key)] = row.get("hardware_profile_sha256") if isinstance(row.get("hardware_profile_sha256"), str) else None
            retry_of = row.get("retry_of")
            if retry_of is not None and retry_of not in known_physical:
                _record_error(errors, "retry_missing_physical_predecessor", str(retry_of), row)
            if retry_of is not None and retry_of == _physical_id(row):
                _record_error(errors, "retry_self_reference", str(retry_of), row)

        event_ids = Counter(str(row.get("event_id")) for row in non_native if row.get("event_id"))
        start_event_ids = Counter(str(row.get("event_id")) for row in non_native if str(row.get("event_kind", "")).endswith("_start"))
        terminal_event_ids = Counter(str(row.get("event_id")) for row in non_native if row.get("terminal") is True)
        for label, counts in (("all", event_ids), ("starts", start_event_ids), ("terminals", terminal_event_ids)):
            for event_id, count in counts.items():
                if count > 1:
                    _record_error(errors, "duplicate_event_id", f"{label}: {event_id} occurs {count} times")

        intervals_by_domain: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        leaves_by_domain: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        for source, row in raw:
            interval, domain = _interval(row), _clock_domain(row)
            if interval is not None and interval[1] < interval[0]:
                _record_error(errors, "negative_interval", f"{interval[0]} > {interval[1]}", row)
            if interval is not None and domain is None:
                _record_error(errors, "missing_clock_domain", "timestamped interval lacks host/clock domain", row)
            excluded_union_kinds = {"outer_swe_agent", "runner_process_wrapper", "e2e_reconciliation", "unknown_residual"}
            if interval is not None and domain is not None and source != "native" and row.get("event_kind") not in excluded_union_kinds:
                intervals_by_domain[domain].append(interval)
            # Only explicitly declared leaves are tested for disjointness. The
            # normal nested wrapper intervals are legitimate and contribute by union.
            if row.get("lifecycle_leaf") is True:
                if source != "lifecycle" or interval is None or domain is None:
                    _record_error(errors, "invalid_lifecycle_leaf_declaration", "leaf requires lifecycle interval and domain", row)
                else:
                    leaves_by_domain[domain].append(interval)
        for domain, leaves in leaves_by_domain.items():
            total, merged = _union(leaves)
            if total != sum(end - start for start, end in leaves):
                _record_error(errors, "lifecycle_leaf_overlap", f"overlap in {domain[0]} / {domain[1]}")

        # Required start/terminal pairs are checked inside each journal using
        # its stable correlation key. Unknown/censored records remain ledger rows.
        pair_inventory: dict[str, dict[str, int]] = {}
        for source, rows in (("model", [r for s, r in raw if s == "model"]), ("tool", [r for s, r in raw if s == "tool"]), ("lifecycle", [r for s, r in raw if s == "lifecycle"])):
            source_starts = [r for r in rows if str(r.get("event_kind", "")).endswith("_start") and r.get("terminal") is not True]
            source_terminals = [r for r in rows if r.get("terminal") is True]
            pair_inventory[source] = {"starts": len(source_starts), "terminals": len(source_terminals)}
            for start in source_starts:
                base = str(start["event_kind"])[:-6]
                key = _physical_id(start) if source == "model" and base == "model_request" else start.get("span_id")
                candidates = [r for r in source_terminals if r.get("event_kind") == base and ((source == "model" and base == "model_request" and _physical_id(r) == key) or (not (source == "model" and base == "model_request") and r.get("span_id") == key))]
                if len(candidates) != 1:
                    _record_error(errors, "missing_or_ambiguous_terminal", f"{source}:{base} start has {len(candidates)} terminals", start)
                elif _clock_domain(start) != _clock_domain(candidates[0]):
                    _record_error(errors, "paired_span_cross_clock_domain", f"{source}:{base} start/terminal domains differ", start)
                elif start.get("start_mono_ns") != candidates[0].get("start_mono_ns"):
                    _record_error(errors, "paired_span_start_timestamp_mismatch", f"{source}:{base}", start)
                elif isinstance(start.get("start_mono_ns"), int) and isinstance(candidates[0].get("end_mono_ns"), int) and candidates[0]["end_mono_ns"] < start["start_mono_ns"]:
                    _record_error(errors, "paired_span_terminal_before_start", f"{source}:{base}", start)
            # Terminal-only failure records are preserved dispositions, not
            # failed span-pair evidence; all other non-failure terminals must
            # have a corresponding start when their kind declares one.
            bases = {str(r.get("event_kind"))[:-6] for r in source_starts} | {
                "outer_swe_agent", "runner_process_wrapper", "deployment_start", "persistent_shell_pid_discovery",
                "runtime_command", "startup", "setup", "get_state", "client_processing", "script_read", "teardown",
                "tool_event", "model_request", "model_client_call"}
            required_pair_kinds = {"model_request", "model_client_call", "tool_event", "runtime_command", "get_state", "client_processing", "deployment_start", "startup", "setup", "teardown", "persistent_shell_pid_discovery", "outer_swe_agent", "runner_process_wrapper", "script_read"}
            for terminal in source_terminals:
                kind = str(terminal.get("event_kind", ""))
                if kind in bases or kind in required_pair_kinds:
                    key = _physical_id(terminal) if source == "model" and kind == "model_request" else terminal.get("span_id")
                    matched = [r for r in source_starts if str(r.get("event_kind"))[:-6] == kind and ((_physical_id(r) == key) if source == "model" and kind == "model_request" else r.get("span_id") == key)]
                    if len(matched) != 1:
                        _record_error(errors, "orphan_terminal", f"{source}:{kind} has {len(matched)} starts", terminal)

        union_report: dict[str, Any] = {}
        for domain, intervals in sorted(intervals_by_domain.items()):
            duration, merged = _union(intervals)
            union_report[f"{domain[0]}|{domain[1]}"] = {"raw_interval_count": len(intervals), "disjoint_union_ms": duration / 1_000_000, "merged_interval_count": len(merged)}
        # Emit raw observations, plus terminal target rows joined only to their
        # matching pre-event physical request features.  The repeated wrappers
        # are preserved but have null features/are not additive fit targets.
        snapshots = attempt / "telemetry_v2/hardware_snapshots.jsonl"
        snapshot_rows = _read_jsonl(snapshots) if snapshots.is_file() else []
        for source, row in raw:
            physical = _physical_id(row) or ""
            joined = pre_features.get(physical) if source == "native" else None
            joined_event_id = pre_event_ids.get(physical) if source == "native" else None
            joined_hardware = None
            kind = str(row.get("event_kind") or "")
            if row.get("terminal") is True and source != "native":
                key = _physical_id(row) if source == "model" and kind == "model_request" else row.get("span_id")
                matched = span_pre.get((source, kind, key))
                if matched is not None:
                    joined, joined_event_id = (matched[0], matched[1]), matched[2]
                    joined_hardware = span_hardware.get((source, kind, key))
            if source == "native":
                metrics = row.get("metrics")
                for metric in ("queue", "prefill", "decode", "e2e"):
                    value = metrics.get(metric, {}).get("value_ms") if isinstance(metrics, Mapping) and isinstance(metrics.get(metric), Mapping) else None
                    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                        _record_error(errors, "invalid_or_missing_native_metric", metric, row)
                        continue
                    if isinstance(metrics, Mapping) and isinstance(metrics.get(metric), Mapping):
                        normalized = _normalized(row, source, identity, partition, joined, metric)
                        normalized["pre_event_id"] = joined_event_id
                        normalized["feature_provenance"]["source_event_id"] = joined_event_id
                        normalized["feature_provenance"]["cpu_pre_event_hardware_profile_sha256"] = pre_hardware.get(physical)
                        normalized["feature_provenance"]["hardware_binding"] = "cpu_profile_is_not_substituted_for_native_gpu_domain"
                        if native_fingerprint and normalized["host_id"] == native_profile_host and native_profile_boot and row.get("clock", {}).get("boot_id") == native_profile_boot:
                            normalized["feature_provenance"]["hardware_fingerprint"] = native_fingerprint
                            normalized["feature_provenance"]["hardware_binding"] = "matched_remote_native_host_profile"
                        normalized_rows.append(normalized)
                continue
            normalized = _normalized(row, source, identity, partition, joined)
            if joined is not None:
                normalized["pre_event_id"] = joined_event_id
                normalized["feature_provenance"]["source_event_id"] = joined_event_id
                normalized["feature_provenance"]["cpu_pre_event_hardware_profile_sha256"] = pre_hardware.get(physical)
                bound_snapshots = [s for s in snapshot_rows if s.get("hardware_profile_sha256") == joined_hardware
                                   and _clock_domain(s) == _clock_domain(row)
                                   and isinstance(s.get("end_mono_ns"), int)
                                   and isinstance(row.get("start_mono_ns"), int)
                                   and s["end_mono_ns"] <= row["start_mono_ns"]]
                if source != "native" and joined_hardware and bound_snapshots:
                    normalized["feature_provenance"]["hardware_fingerprint"] = joined_hardware
                    normalized["feature_provenance"]["hardware_binding"] = "same_host_clock_pre_event_snapshot"
            normalized_rows.append(normalized)
        cpu = _cpu_integrity(attempt, errors, sources, case)
        snapshots = attempt / "telemetry_v2/hardware_snapshots.jsonl"
        if snapshots.is_file():
            sources.append({"path": str(snapshots.relative_to(case)), "sha256": _sha256(snapshots)})
            snapshot_rows = _read_jsonl(snapshots)
            pre = [row for row in snapshot_rows if row.get("hardware_profile_sha256") and row.get("start_mono_ns") is not None]
            if pre:
                hardware_bindings["cpu"] = {"path": str(snapshots.relative_to(case)), "sha256": _sha256(snapshots), "hardware_profile_sha256": pre[0]["hardware_profile_sha256"], "binding": "pre-execution CPU-host snapshot only"}
        outer = [r for s, r in raw if s == "lifecycle" and r.get("event_kind") == "outer_swe_agent" and r.get("terminal") is True]
        outer_interval = _interval(outer[0]) if len(outer) == 1 else None
        if len(outer) != 1 or outer_interval is None:
            _record_error(errors, "missing_outer_e2e_boundary", "need exactly one terminal outer_swe_agent interval")
        attempts_report.append({"attempt_id": attempt_id, "raw_record_count": len(raw), "model_physical_starts": len(starts), "model_physical_terminals": len(terminals), "native_physical_terminals": len(native), "pair_inventory": pair_inventory, "cpu_raw_integrity": cpu, "host_clock_unions": union_report, "outer_e2e_boundary": None if outer_interval is None else {"event_kind": "outer_swe_agent", "observed_ms": (outer_interval[1] - outer_interval[0]) / 1_000_000}, "native_clock_policy": "native intervals and GPU durations are retained separately; never added to host timeline union"})

    normalized_rows.sort(key=lambda row: (row["attempt_id"], str(row["event_id"])))
    events_path = out / "normalized_case_events.jsonl"
    events_path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in normalized_rows), encoding="utf-8")
    metadata = {name: spec.get(name) for name in ("suite", "repository", "category", "settings") if name in spec}
    report = {"schema_version": "assignment.offline.ledger-validation.v1", "case_root": str(case), "identity": {"instance_id": spec["instance_id"], "case_id": spec["case_id"]}, "case_metadata": metadata, "hardware_bindings": hardware_bindings, "partition": partition, "partition_provenance": "case_spec_supplied_untrusted; downstream derives pinned partition before event read", "disposition": "confirmation_excluded" if confirmation else "unknown_not_fit_eligible_without_external_pinned_manifest", "excluded_attempt_inventory": excluded_attempts, "case_result": {"present": case_result is not None, "evaluator_outcome_provenance": "unproven_not_bound_by_ledger" if case_result else "missing"}, "source_hashes": sorted(sources, key=lambda value: value["path"]), "validation": {"status": "valid" if not errors else "invalid", "error_count": len(errors), "errors": errors}, "attempts": attempts_report, "case_event_records": {"path": str(events_path), "count": len(normalized_rows)}}
    report_path = out / "validation_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["validation_report_path"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--attempt-id", help="required when a case has multiple attempts without an accepted binding")
    args = parser.parse_args()
    report = validate_case(args.case_root, args.output_dir, args.attempt_id)
    print(json.dumps({"validation_report_path": report["validation_report_path"], "events_path": report["case_event_records"]["path"], "status": report["validation"]["status"]}, sort_keys=True))
    return 0 if report["validation"]["status"] == "valid" else 2


if __name__ == "__main__":
    raise SystemExit(main())
