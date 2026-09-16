#!/usr/bin/env python3
"""Rebuild compact, saved-byte evidence from the frozen combined v8 fixture.

This is intentionally a bounded audit helper.  It hashes the raw BPF binary,
decodes exactly one action range, stops the separate path search at the first
inline pathname, and reconstructs the comparatively small native/host journal
joins.  It does not emit a decoded BPF export or make a production claim.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_helper(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("saved_reconstruction_v2", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.evidence_root.resolve(strict=True)
    output = args.output.resolve()
    if output.exists():
        raise RuntimeError(f"refusing to overwrite: {output}")
    source = root / "source-v8"
    import sys

    sys.path.insert(0, str(source / "src"))
    from agentic_sim.telemetry.bpf_work import iter_bpf_events

    helper = load_helper(root / "reconstruction-tools-v2/reconstruct_saved_evidence_v2.py")
    case = root / "combined-case-v8"
    work = case / "runner_attempts/attempt-001/telemetry_v2/linux_work"
    summary = json.loads((work / "work_summary.json").read_text())
    action = summary["actions"][0]
    stream = action["raw"]["binary_event_stream"]
    binary = work / "raw_events.bin"
    action_rows = list(
        iter_bpf_events(
            binary,
            offset_start=stream["offset_start"],
            offset_end=stream["offset_end"],
            token=action["action_token"],
            schema_version=stream["schema_version"],
            record_size_bytes=stream["record_size_bytes"],
        )
    )
    path_row = None
    scanned = 0
    for row in iter_bpf_events(
        binary,
        schema_version=stream["schema_version"],
        record_size_bytes=stream["record_size_bytes"],
    ):
        scanned += 1
        if row.get("path"):
            path_row = {
                key: row.get(key)
                for key in ("token", "sequence", "kind", "status", "ret", "path", "path2", "kernel_start_ns", "kernel_end_ns", "scalar_args")
            }
            break
    operation_examples: dict[str, Any] = {}
    # Select each bounded action range from its saved aggregate rather than
    # walking the whole 846,533-record binary again.  K_READ=1, K_WRITE=2,
    # K_GETDENTS=5 in the retained v3 ABI.
    wanted = {
        "read": ("read_syscall_count", 1),
        "write": ("write_syscall_count", 2),
        "getdents": ("getdents_count", 5),
    }
    for name, (aggregate_field, kind) in wanted.items():
        candidate = next(
            (item for item in summary["actions"] if item["raw"]["raw_aggregate"].get(aggregate_field, 0) > 0),
            None,
        )
        if candidate is None:
            operation_examples[name] = {"status": "unavailable_no_saved_example"}
            continue
        candidate_stream = candidate["raw"]["binary_event_stream"]
        event = next(
            (
                item
                for item in iter_bpf_events(
                    binary,
                    offset_start=candidate_stream["offset_start"],
                    offset_end=candidate_stream["offset_end"],
                    token=candidate["action_token"],
                    schema_version=candidate_stream["schema_version"],
                    record_size_bytes=candidate_stream["record_size_bytes"],
                )
                if item["kind"] == kind
            ),
            None,
        )
        operation_examples[name] = {
            "status": "measured" if event is not None else "invalid_aggregate_no_matching_packet",
            "action_event_id": candidate["event_id"],
            "action_token": candidate["action_token"],
            "aggregate_count": candidate["raw"]["raw_aggregate"].get(aggregate_field),
            "event": None if event is None else {
                key: event.get(key)
                for key in ("kind", "status", "ret", "path", "path2", "kernel_start_ns", "kernel_end_ns", "scalar_args")
            },
            "kernel_duration_ns": None if event is None else event["kernel_end_ns"] - event["kernel_start_ns"],
        }
    identity = helper.find_identity(case, helper.collect_telemetry_identity(case))["fields"]
    model_summary, model_rows, _ = helper.collect_models(case, identity)
    host_summary, _ = helper.collect_host_clock_intervals(case, identity)
    clock = host_summary["e2e"]
    native_sums = {
        field: sum(
            row["native_finished"]["measurement"]["finished"]["phase_ms"][field]
            for row in model_rows
        )
        for field in ("prefill_time", "decode_time", "queued_time", "e2e_latency")
    }
    spec = json.loads((case / "case_spec.json").read_text())
    result = json.loads((case / "case_result.json").read_text())
    prior = json.loads((root / "reconstruction-v8/representative-deliverable-input.json").read_text())["sample_figure_input"]
    merged = json.loads((root / "reconstruction-v8/merged-v2-proof.json").read_text())
    fresh = {
        "case_id": spec["case_id"],
        "instance_id": spec["instance_id"],
        "suite": spec["suite"],
        "repository": spec["repository"],
        "settings": spec["settings"],
        "official_resolved": result["evaluator"]["official_resolved"],
        "e2e_wall_ms": merged["outer_wall_ms"],
        "tool_execution_union_ms": merged["phase_union_ms"]["tool_execution"],
        "unknown_wall_ms": merged["unknown_wall_ms"],
        "native_sums_ms": {
            "prefill": native_sums["prefill_time"],
            "decode": native_sums["decode_time"],
            "queue": native_sums["queued_time"],
            "e2e": native_sums["e2e_latency"],
        },
    }
    fresh["ratio"] = fresh["tool_execution_union_ms"] / (
        fresh["native_sums_ms"]["prefill"] + fresh["native_sums_ms"]["decode"]
    )
    expected = {
        "e2e_wall_ms": prior["e2e_wall_ms"],
        "tool_execution_union_ms": prior["semantic_tool_wall_ms"],
        "unknown_wall_ms": prior["unknown_wall_ms"],
        "native_sums_ms": prior["native_phase_sums_ms"],
        "ratio": prior["semantic_tool_to_native_inference_wall_ratio"],
    }
    proof = {
        "scope": "closed combined-case-v8 integration fixture only; not production data",
        "bpf": {
            "full_binary_sha256": sha256(binary),
            "manifest_binary_sha256": json.loads((work / "bpf_collector_manifest.json").read_text())["raw_event_stream_sha256"],
            "first_action": {
                "event_id": action["event_id"],
                "token": action["action_token"],
                "range": {key: stream[key] for key in ("offset_start", "offset_end", "record_count", "record_size_bytes", "schema_version")},
                "decoded_token_filtered_count": len(action_rows),
                "aggregate_required_count": action["raw"]["required_event_count"],
            },
            "inline_path_search": {"records_scanned_before_first_path": scanned, "first_path_record": path_row},
            "operation_examples": operation_examples,
            "full_fixture_audit": {
                "action_rows": len(summary["actions"]),
                "complete_action_rows": sum(row["raw"]["event_records_complete"] for row in summary["actions"]),
                "required_event_count": sum(row["raw"]["required_event_count"] for row in summary["actions"]),
                "raw_event_count": sum(row["raw"]["event_count"] for row in summary["actions"]),
                "loss_totals": {
                    "perf_lost_events": sum(row["raw"].get("perf_lost_events", 0) for row in summary["actions"]),
                    **{field: sum(row["raw"]["raw_aggregate"].get(field, 0) for row in summary["actions"])
                       for field in ("lost_event_records", "lost_path_records", "lost_pending_records", "lineage_map_failures")},
                },
                "deferred_finalization_rows": len(summary["action_finalizations"]),
            },
        },
        "native": {
            "physical_request_count": model_summary["physical_request_count"],
            "native_finished_count": model_summary["native_finished_count"],
            "native_http_terminal_count": model_summary["native_http_terminal_count"],
            "native_hash_mismatch_count": model_summary["native_hash_mismatch_count"],
            "unmatched_finished_count": model_summary["native_unmatched_finished_count"],
            "join_rule": model_summary["join_rule"],
        },
        "host_e2e": {
            key: clock[key]
            for key in ("status", "outer_e2e_ms", "measured_union_ms", "unknown_residual_ms", "closure_error_ns", "cross_clock_bpf_union_performed")
        },
        "representative_d1_d6": {"fresh": fresh, "matches_retained_row": {key: fresh[key] == value for key, value in expected.items()}},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
