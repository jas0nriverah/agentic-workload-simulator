#!/usr/bin/env python3
"""Recompute v2 identity and timing coverage from merged on-disk journals.

This is an evidence audit, not model fitting. It never reads evaluator labels.
The caller must also compare physical attempts with serving/client witnesses;
an internally consistent journal alone cannot prove no event was omitted.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from agentic_sim.telemetry.features import build_tool_features, build_model_features, canonical_json, canonical_sha256, tool_model_vector, model_vector


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result


def audit(rows):
    errors = []
    if not rows:
        raise ValueError("No v2 event rows")
    identities = {(r.get("run_id"), r.get("attempt_id"), r.get("case_id")) for r in rows}
    if len(identities) != 1 or any(not isinstance(x, str) or not x.strip() for x in next(iter(identities))):
        raise ValueError("Audit one exact run/attempt/case at a time")
    clocks = {(r.get("clock", {}).get("hostname"), r.get("clock", {}).get("boot_id"), r.get("clock", {}).get("clock_id")) for r in rows}
    if len(clocks) != 1 or any(not isinstance(x, str) or not x.strip() for x in next(iter(clocks))):
        raise ValueError("Journals lack one proven host/boot/clock domain")
    duplicates = sum(n - 1 for n in Counter(r.get("event_id") for r in rows).values() if n > 1)
    if any(not r.get("event_id") for r in rows):
        errors.append("Missing event identity")
    spans = defaultdict(list)
    for row in rows:
        spans[row.get("span_id")].append(row)
    starts = [r for r in rows if r.get("terminal") is False]
    terminal = [r for r in rows if r.get("terminal") is True]
    missing_tool = missing_request = missing_pre = malformed = 0
    parity = future = 0
    for group in spans.values():
        first = [r for r in group if r.get("terminal") is False]
        last = [r for r in group if r.get("terminal") is True]
        phase = group[0].get("phase")
        if phase in {"unknown_residual", "e2e_reconciliation", "hardware_snapshot"} or group[0].get("event_kind") in {"hardware_snapshot", "tool_intent", "failure"}:
            continue
        if len(first) != 1 or len(last) != 1:
            errors.append(f"Span lacks exactly one start/terminal pair: {group[0].get('span_id')}")
            if phase == "tool_execution":
                missing_tool += len(first) if not last else 0
                missing_pre += len(last) if not first else 0
            if phase == "model_request":
                missing_request += len(first) if not last else 0
        if len(first) == len(last) == 1:
            if first[0].get("start_mono_ns") != last[0].get("start_mono_ns"):
                errors.append("Start boundary changed at terminal record")
            identity_fields = ("action_id", "request_id", "logical_request_id", "logical_operation_id", "action_sha256", "features", "feature_vector", "feature_vector_sha256", "feature_sha256")
            if first[0].get("event_kind") == "runtime_command_start":
                identity_fields += ("runtime_command", "runtime_command_sha256", "cpu_action_required")
            for key in identity_fields:
                if first[0].get(key) != last[0].get(key):
                    errors.append(f"Identity changed at terminal record: {key}")
    for row in terminal:
        start, end = row.get("start_mono_ns"), row.get("end_mono_ns")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end)) or end < start:
            malformed += 1
            continue
        duration = row.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or abs(duration - (end - start) / 1e6) > 1e-6:
            errors.append("Duration differs from monotonic endpoints")
    for row in starts:
        if row.get("phase") not in {"tool_execution", "model_request"}:
            continue
        if row.get("event_kind") == "runtime_command_start":
            # These are exact physical framework commands (setup/state/reset),
            # not semantic agent tools. They have an independent byte/hash
            # contract and BPF coverage, not an invented tool-feature vector.
            command = row.get("runtime_command")
            if not isinstance(command, str) or "\x00" in command:
                errors.append("Runtime command text is missing or invalid")
            elif hashlib.sha256(command.encode()).hexdigest() != row.get("runtime_command_sha256"):
                errors.append("Runtime command text/hash mismatch")
            if row.get("cpu_action_required") is not True:
                errors.append("Runtime command cannot disable required CPU capture")
            continue
        if row.get("event_kind") == "bash_interrupt_control_start":
            # A measured runtime interrupt is a control action, not a shell
            # command. Its start/terminal and timing checks still apply above.
            continue
        features = row.get("features")
        if not isinstance(features, dict):
            parity += 1
            continue
        try:
            if row["phase"] == "tool_execution":
                observed_at = features.get("script_state", {}).get("observed_at_mono_ns")
                if observed_at is not None and observed_at > row["start_mono_ns"]:
                    future += 1
                rebuilt = build_tool_features(row["action"], script_state=features.get("script_state"), action_id=features.get("action_id"))
                if hashlib.sha256(row["action"].encode()).hexdigest() != row.get("action_sha256"):
                    errors.append("Pre-action text/hash mismatch")
            else:
                rebuilt = build_model_features({key: features.get(key) for key in ("input_tokens", "context_tokens", "max_output_tokens", "temperature", "top_p", "seed", "model", "model_revision", "tokenizer_revision", "request_sha256", "mode")}, hardware=features.get("hardware"))
            if canonical_json(rebuilt) != canonical_json(features):
                parity += 1
            vector = tool_model_vector(rebuilt) if row["phase"] == "tool_execution" else model_vector(rebuilt)
            if "feature_vector" in row and (canonical_json(vector) != canonical_json(row["feature_vector"]) or canonical_sha256(vector) != row.get("feature_vector_sha256")):
                parity += 1
        except (ValueError, KeyError, TypeError):
            future += 1
    physical = [r for r in terminal if r.get("phase") == "model_request" and r.get("event_kind") == "model_request"]
    request_ids = {r.get("request_id") for r in physical}
    if len(request_ids) != len(physical) or any(not isinstance(x, str) or not x.strip() for x in request_ids):
        errors.append("Physical request identities are missing or reused")
    requests_by_id = {r.get("request_id"): r for r in physical}
    unlinked = 0
    for row in physical:
        retry = row.get("retry_index")
        if not isinstance(row.get("logical_request_id"), str) or not row["logical_request_id"].strip():
            unlinked += 1
        if not isinstance(retry, int) or isinstance(retry, bool) or retry < 0:
            unlinked += 1
        elif retry > 0 and row.get("retry_of") not in request_ids:
            unlinked += 1
        elif retry == 0 and row.get("retry_of") is not None:
            unlinked += 1
        elif retry > 0:
            previous = requests_by_id[row["retry_of"]]
            if (previous.get("logical_request_id") != row.get("logical_request_id")
                    or previous.get("retry_index") != retry - 1
                    or previous["start_mono_ns"] >= row["start_mono_ns"]):
                unlinked += 1
    outer = [r for r in terminal if r.get("phase") == "outer_swe_agent"]
    if len(outer) != 1 or malformed:
        raise ValueError("Requires exactly one valid closed outer interval and valid endpoints")
    start, end = outer[0]["start_mono_ns"], outer[0]["end_mono_ns"]
    excluded = {"outer_swe_agent", "generic_wrapper", "unknown_residual", "e2e_reconciliation"}
    attributed = [r for r in terminal if r.get("phase") not in excluded and r.get("availability") == "measured" and r.get("event_kind") != "hardware_snapshot"]
    intervals = [(max(start, r["start_mono_ns"]), min(end, r["end_mono_ns"])) for r in attributed if r["end_mono_ns"] > start and r["start_mono_ns"] < end]
    union = merge(intervals)
    covered = sum(b - a for a, b in union)
    physical_intervals = [(max(start, r["start_mono_ns"]), min(end, r["end_mono_ns"]))
                          for r in attributed if r.get("phase") in {"tool_execution", "model_request"}
                          and r["end_mono_ns"] > start and r["start_mono_ns"] < end]
    physical_covered = sum(b - a for a, b in merge(physical_intervals))
    phase_unions = {}
    for phase in sorted({r["phase"] for r in attributed}):
        values = [(max(start, r["start_mono_ns"]), min(end, r["end_mono_ns"])) for r in attributed
                  if r["phase"] == phase and r["end_mono_ns"] > start and r["start_mono_ns"] < end]
        phase_unions[phase] = sum(b-a for a,b in merge(values)) / 1e6
    cursor = start
    complement = []
    for left, right in union:
        if left > cursor:
            complement.append([cursor, left])
        cursor = right
    if cursor < end:
        complement.append([cursor, end])
    unknown = sum(b - a for a, b in complement)
    run, attempt, case = next(iter(identities))
    return {"schema_version": "assignment.v2-journal-audit.v1", "run_id": run, "attempt_id": attempt,
            "case_id": case, "execution_status": "completed" if outer[0].get("status") == "success" else "incomplete",
            "tool_events": sum(r.get("phase") == "tool_execution" and r.get("event_kind") not in {"tool_intent", "runtime_command", "bash_interrupt_control"} for r in terminal),
            "runtime_commands": sum(r.get("event_kind") == "runtime_command" for r in terminal),
            "physical_requests": len(physical), "missing_pre_actions": missing_pre,
            "missing_terminal_tools": missing_tool, "missing_terminal_requests": missing_request,
            "duplicate_ids": duplicates, "negative_intervals": malformed, "unlinked_retries": unlinked,
            "feature_parity_mismatches": parity, "future_feature_violations": future,
            "outer_wall_ms": (end-start)/1e6, "attributed_union_ms": covered/1e6,
            "tool_request_union_ms": physical_covered/1e6,
            "newly_attributed_union_ms": (covered-physical_covered)/1e6,
            "phase_union_ms": phase_unions,
            "phase_union_note": "Per-phase unions may overlap; do not add them. These are wall intervals, not CPU busy or GPU kernel time.",
            "unknown_wall_ms": unknown/1e6, "closure_error_ms": (covered+unknown-(end-start))/1e6,
            "unknown_intervals_mono_ns": complement,
            "attribution_excludes_unknown_and_outer_wrappers": True,
            "request_mutations": None,
            "request_mutation_note": "Must be verified against client payload/forwarding hashes separately",
            "internal_consistency_errors": errors,
            "status": "pass" if not errors and not any((missing_pre, missing_tool, missing_request, duplicates, malformed, parity, future, unlinked)) else "fail",
            "limitation": "Internal consistency cannot prove external event completeness; compare native action/client/server witnesses."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directories", type=Path, nargs="+")
    args = parser.parse_args()
    rows, bindings = [], []
    for directory in args.directories:
        for path in sorted(directory.rglob("*.jsonl")):
            if path.name not in {"lifecycle_events.jsonl", "tool_events.jsonl", "model_events.jsonl", "hardware_snapshots.jsonl"}:
                continue
            payload = path.read_bytes()
            bindings.append({"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()})
            for line in payload.decode().splitlines():
                if line.strip():
                    row = json.loads(line)
                    if str(row.get("schema_version", "")).startswith("assignment.telemetry.v2"):
                        rows.append(row)
    result = audit(rows)
    result["artifact_bindings"] = bindings
    print(json.dumps(result, sort_keys=True, indent=2))
    raise SystemExit(0 if result["status"] == "pass" else 1)
