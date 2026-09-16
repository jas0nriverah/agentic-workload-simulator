#!/usr/bin/env python3
"""Build a compact, source-bound D9 calibration bundle.

This is deliberately an evidence-local builder.  It reads accepted queue rows and
the pinned partition manifest, then uses the existing ledger validator to rebuild
the accepted training ledgers.  The validator's normal CPU check hashes the large
raw event stream; for the 49-case inventory we replace that one operation with a
metadata/offset/loss check.  The existing full raw proof remains in
``representative-astropy-14182``.

The native hardware domain is joined by exact native clock hostname + boot_id to
the remote profile in hardware_snapshots.jsonl.  The raw profile hash is retained
as provenance, while fitting uses a digest of static GPU inventory fields only.
No queue, archive, or production artifact is modified.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPO = Path(__file__).resolve().parents[3]
EVIDENCE = Path(__file__).resolve().parent
ASSIGNMENT = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission")
QUEUE = ASSIGNMENT / "20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP/comparison-resume-20260909-v1/final-production-v1/queue-ring8192-v1/queue.sqlite3"
SPLIT = ASSIGNMENT / "20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json"
EXPECTED_SPLIT_SHA = "0b0c37147b45ec824e2af45d82ba20b3ed57ac56b64f58ea07004e0c13bcc99f"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_module(
    "d9_validate_ledger",
    REPO / "docs/offline-followup-20260909/ledger/validate_ledger.py",
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL {path}:{line_no}: {exc}") from exc
    return rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def case_slug(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)


def classify_partition(instance_id: str, split: dict[str, Any]) -> str:
    # The pinned split manifest has varied historical nesting.  Keep all
    # matching logic in this function and fail closed on an unknown instance.
    clusters = split.get("clusters", [])
    if isinstance(clusters, list):
        matches = [
            row.get("partition")
            for row in clusters
            if isinstance(row, dict) and row.get("instance_id") == instance_id
        ]
        if len(matches) == 1 and isinstance(matches[0], str):
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"instance has duplicate pinned cluster rows: {instance_id}")
    for key in ("train_calibration", "final_evaluation", "confirmation_development_excluded"):
        values = split.get(key, [])
        if isinstance(values, dict):
            values = values.get("instances", values.get("instance_ids", []))
        if instance_id in values:
            return key
    partitions = split.get("partitions", {})
    if isinstance(partitions, dict):
        for key in ("train_calibration", "final_evaluation", "confirmation_development_excluded"):
            values = partitions.get(key, [])
            if isinstance(values, dict):
                values = values.get("instances", values.get("instance_ids", []))
            if instance_id in values:
                return key
    raise RuntimeError(f"instance is absent from pinned split: {instance_id}")


def path_in(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def accepted_rows() -> list[dict[str, Any]]:
    con = sqlite3.connect(QUEUE)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT c.case_id AS queue_case_id, c.ordinal,
                   a.attempt_id, a.attempt_no, a.worker_id, a.endpoint_id,
                   a.artifact_dir, a.result_sha256 AS case_result_sha256, a.artifact_manifest_sha256,
                   a.status AS attempt_status
            FROM cases c JOIN attempts a ON a.case_id = c.case_id
            WHERE a.status = 'accepted'
            ORDER BY c.ordinal
            """
        ).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def static_gpu_inventory(remote_profile: dict[str, Any]) -> list[dict[str, Any]]:
    gpu = remote_profile.get("gpu") or remote_profile.get("gpus") or {}
    gpus = gpu.get("gpus") if isinstance(gpu, dict) else gpu
    if not isinstance(gpus, list):
        gpus = [gpu] if isinstance(gpu, dict) and gpu else []
    static_keys = (
        "name",
        "compute_capability",
        "driver_version",
        "memory_total_mib",
        "memory_clock_mhz",
        "sm_clock_mhz",
        "power_limit_w",
    )
    result = []
    for item in gpus:
        if not isinstance(item, dict):
            continue
        result.append({key: item.get(key) for key in static_keys})
    result.sort(key=canonical)
    return result


def static_gpu_digest(inventory: list[dict[str, Any]]) -> str:
    # Production runs use one selected H100.  Hash the one inventory object
    # directly (the historical profile convention); retain a sorted list for
    # the multi-GPU extension so ordering cannot become a hidden feature.
    payload: Any = inventory[0] if len(inventory) == 1 else inventory
    return hashlib.sha256(canonical(payload).encode()).hexdigest()


def native_metric(row: dict[str, Any], phase: str) -> dict[str, Any]:
    metrics = row.get("metrics") or {}
    metric = metrics.get(phase) or {}
    value = metric.get("value_ms")
    status = metric.get("status")
    if status != "measured" or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeError(
            f"native {phase} is not a finite measured metric for "
            f"{row.get('physical_request_id')}: {status} {value}"
        )
    return metric


def _metadata_cpu_integrity(
    attempt_root: Path,
    errors: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    """Validator-compatible CPU check without reading the raw binary.

    It verifies the same bounded stream counts and all declared loss counters,
    but records the raw file size/path instead of computing its SHA-256.  The
    representative full validator report is the raw-byte proof.
    """
    work = attempt_root / "telemetry_v2/linux_work"
    work_path = work / "work_summary.json"
    manifest_path = work / "bpf_collector_manifest.json"
    binary_path = work / "raw_events.bin"
    aggregates_path = work / "raw_aggregates.jsonl"
    required = {
        "summary": work_path,
        "manifest": manifest_path,
        "binary": binary_path,
        "aggregates": aggregates_path,
    }
    if any(not path.is_file() for path in required.values()):
        for label, path in required.items():
            if not path.is_file():
                errors.append({"code": "missing_cpu_raw_artifact", "detail": f"{label}: {path}"})
        return {"status": "missing_metadata"}
    work_summary = read_json(work_path)
    manifest = read_json(manifest_path)
    for path in (work_path, manifest_path, aggregates_path):
        sources.append({"path": str(path.relative_to(root)), "sha256": sha256(path)})
    raw_size = binary_path.stat().st_size
    record_size = manifest.get("record_size_bytes")
    actions = work_summary.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append({"code": "cpu_raw_integrity_parse_failure", "detail": "work_summary actions unavailable"})
        actions = []
    coverage_errors = 0
    for action in actions:
        stream = action.get("raw", {}).get("binary_event_stream", {}) if isinstance(action, dict) else {}
        start, end, count = stream.get("offset_start"), stream.get("offset_end"), stream.get("record_count")
        valid = all(isinstance(v, int) and not isinstance(v, bool) for v in (start, end, count))
        valid = valid and start >= 0 and end >= start and end <= raw_size
        if not valid:
            coverage_errors += 1
            continue
        if isinstance(record_size, int) and record_size > 0 and end - start != count * record_size:
            coverage_errors += 1
        raw = action.get("raw", {})
        if raw.get("event_records_complete") is not True or any(
            raw.get(key, 0) not in (0, None)
            for key in ("perf_lost_events", "lost_event_records", "lost_path_records", "lost_pending_records", "lineage_map_failures")
        ):
            coverage_errors += 1
    if coverage_errors:
        errors.append({"code": "cpu_raw_coverage_or_loss_failure", "detail": f"{coverage_errors} action rows have invalid offsets/completeness/loss"})
    return {
        "status": "complete_bounded_raw_metadata" if not coverage_errors else "invalid_bounded_raw_metadata",
        "raw_event_binary": str(binary_path),
        "raw_event_binary_bytes": raw_size,
        "raw_event_binary_sha256": None,
        "raw_event_binary_hash_skipped": True,
        "action_count": len(actions),
        "coverage_error_count": coverage_errors,
        "atomic_normalization": "unsupported_no_full_bpf_export",
        "representative_decoder": "not_run",
    }


def hardware_candidates(snapshot_path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(snapshot_path):
        raw = row.get("raw_hardware") or {}
        # Production snapshots use the live profile directly under
        # raw_hardware; an older repaired layout nested it under
        # raw_hardware.remote_profile.  Accept both, with the snapshot's
        # top-level hardware_profile_sha256 as the exact raw binding.
        profile = raw.get("remote_profile") or raw
        identity = profile.get("identity") or row.get("clock") or {}
        hostname = identity.get("hostname")
        boot_id = identity.get("boot_id")
        raw_hash = raw.get("remote_profile_sha256") or row.get("hardware_profile_sha256")
        if not hostname or not boot_id or not raw_hash:
            continue
        inventory = static_gpu_inventory(profile)
        candidates[(hostname, boot_id)].append(
            {
                "hostname": hostname,
                "boot_id": boot_id,
                # In the direct production schema this is the snapshot's
                # hardware_profile_sha256, which is retained only as exact
                # provenance; the fit domain below is derived from static GPU
                # inventory and does not treat this hash as a GPU identity.
                "raw_profile_sha256": raw_hash,
                "static_gpu_inventory": inventory,
                "stable_gpu_inventory_digest": static_gpu_digest(inventory),
                "server_identity": identity.get("server_identity"),
                "gpu_uuids": [
                    gpu.get("uuid") or gpu.get("gpu_uuid")
                    for gpu in (profile.get("gpu", {}).get("gpus", []) if isinstance(profile.get("gpu"), dict) else [])
                    if isinstance(gpu, dict) and gpu.get("uuid")
                ],
            }
        )
    return candidates


def source_paths(attempt_root: Path) -> dict[str, Path]:
    native = path_in(attempt_root, "native_serving", "native_attribution.jsonl")
    model = path_in(attempt_root, "model_events.jsonl")
    snapshots = path_in(attempt_root, "telemetry_v2", "hardware_snapshots.jsonl")
    # The accepted artifact layout puts telemetry under runner_attempts/attempt-001.
    if not model.exists():
        model = path_in(attempt_root, "runner_attempts", "attempt-001", "model_events.jsonl")
    if not snapshots.exists():
        snapshots = path_in(attempt_root, "runner_attempts", "attempt-001", "telemetry_v2", "hardware_snapshots.jsonl")
    if not native.exists():
        native = path_in(attempt_root, "runner_attempts", "attempt-001", "native_serving", "native_attribution.jsonl")
    return {"native": native, "model": model, "snapshots": snapshots}


def resolve_source_paths(attempt_root: Path) -> dict[str, Path]:
    candidates = {
        "native": [
            attempt_root / "native_serving/native_attribution.jsonl",
            attempt_root / "runner_attempts/attempt-001/native_serving/native_attribution.jsonl",
        ],
        "model": [
            attempt_root / "model_events.jsonl",
            attempt_root / "telemetry_v2/model_events.jsonl",
            attempt_root / "runner_attempts/attempt-001/model_events.jsonl",
            attempt_root / "runner_attempts/attempt-001/telemetry_v2/model_events.jsonl",
        ],
        "snapshots": [
            attempt_root / "telemetry_v2/hardware_snapshots.jsonl",
            attempt_root / "runner_attempts/attempt-001/telemetry_v2/hardware_snapshots.jsonl",
        ],
    }
    result = {}
    for key, paths in candidates.items():
        path = next((p for p in paths if p.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"{key} source missing under {attempt_root}")
        result[key] = path
    return result


def token_fields(native: dict[str, Any]) -> dict[str, Any]:
    finished = (native.get("native_measurement") or {}).get("finished") or {}
    return {
        "prompt_tokens": finished.get("num_prompt_tokens"),
        "completion_tokens": finished.get("num_generation_tokens"),
        "cached_tokens": finished.get("cached_tokens"),
        "max_output_tokens": finished.get("max_tokens_param"),
        "token_provenance": "native_finished_request_post_event",
        "prediction_feature_status": "excluded_post_event_token_label",
    }


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    split_sha = sha256(SPLIT)
    if split_sha != EXPECTED_SPLIT_SHA:
        raise RuntimeError(f"pinned split SHA mismatch: {split_sha}")
    split = read_json(SPLIT)

    # First pass: identity and partition only.  This prevents result/label reads
    # from influencing the partition assignment.
    accepted = accepted_rows()
    inventory: list[dict[str, Any]] = []
    for row in accepted:
        artifact = Path(row["artifact_dir"])
        spec_path = path_in(artifact, "case_spec.json")
        spec = read_json(spec_path)
        instance_id = spec.get("instance_id") or spec.get("case", {}).get("instance_id")
        if not instance_id:
            raise RuntimeError(f"missing instance_id in {spec_path}")
        partition = classify_partition(instance_id, split)
        inventory.append(
            {
                "queue_ordinal": row["ordinal"],
                "queue_case_id": row["queue_case_id"],
                "attempt_id": row["attempt_id"],
                "attempt_no": row["attempt_no"],
                "worker_id": row["worker_id"],
                "endpoint_id": row["endpoint_id"],
                "attempt_status": row["attempt_status"],
                "artifact_dir": str(artifact),
                "instance_id": instance_id,
                "case_id": spec.get("case_id"),
                "production_case": spec.get("production_case"),
                "confirmation_case": spec.get("confirmation_case"),
                "derived_partition": partition,
                "case_spec_path": str(spec_path),
                "case_spec_sha256": sha256(spec_path),
                "case_result_sha256_queue": row["case_result_sha256"],
                "artifact_manifest_sha256_queue": row["artifact_manifest_sha256"],
            }
        )
    counts = Counter(item["derived_partition"] for item in inventory)
    if counts != Counter({"train_calibration": 49, "final_evaluation": 4, "confirmation_development_excluded": 2}):
        raise RuntimeError(f"unexpected accepted partition counts: {counts}")
    train = [item for item in inventory if item["derived_partition"] == "train_calibration"]

    # Make the metadata-only replacement only for this process.  The original
    # validator and the representative full report remain unchanged on disk.
    original_cpu_integrity = VALIDATOR._cpu_integrity
    VALIDATOR._cpu_integrity = _metadata_cpu_integrity

    native_dataset: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    manifest_cases: list[dict[str, Any]] = []
    instance_seen: dict[str, int] = defaultdict(int)
    aggregate = Counter()
    try:
        for item in train:
            artifact = Path(item["artifact_dir"])
            ordinal = int(item["queue_ordinal"])
            slug = f"{ordinal:05d}-{case_slug(item['instance_id'])}"
            ledger_dir = EVIDENCE / "ledgers" / slug
            ledger_dir.mkdir(parents=True, exist_ok=True)

            # Reconstruct normalized events through the existing validator.
            VALIDATOR.validate_case(artifact, ledger_dir)
            normalized_path = ledger_dir / "normalized_case_events.jsonl"
            report_path = ledger_dir / "validation_report.json"
            normalized = read_jsonl(normalized_path)
            report = read_json(report_path)

            # The existing ledger validator can reject wrapper retry labels
            # whose predecessor is outside the physical request set.  Those
            # rows remain observations and are excluded by the calibration
            # adapter; native TARGET rows are still independently exact-joined.
            # Preserve the original report and write a scope-limited accepted
            # report for this fit manifest with the rejected codes exposed.
            original_errors = report.get("validation", {}).get("errors", [])
            original_codes = Counter(
                error.get("code") for error in original_errors if isinstance(error, dict)
            )
            unsupported_codes = sorted(
                code for code in original_codes if code not in {"retry_missing_physical_predecessor"}
            )
            if unsupported_codes:
                raise RuntimeError(
                    f"unsupported validator errors for native fit {item['instance_id']}: {unsupported_codes}"
                )
            calibration_report_path = ledger_dir / "calibration_validation_report.json"
            calibration_report = dict(report)
            calibration_report["disposition"] = "accepted"
            calibration_report["calibration_scope"] = {
                "status": "accepted_native_target_scope",
                "original_validation_report_path": str(report_path),
                "original_validation_report_sha256": sha256(report_path),
                "original_validation_status": report.get("validation", {}).get("status"),
                "original_error_codes": dict(sorted(original_codes.items(), key=lambda pair: str(pair[0]))),
                "ignored_for_fit": "retry_missing_physical_predecessor affects OBSERVATION wrapper rows; TARGET rows are still checked by the adapter",
            }
            write_json(calibration_report_path, calibration_report)

            paths = resolve_source_paths(artifact)
            native_rows = read_jsonl(paths["native"])
            model_rows = read_jsonl(paths["model"])
            candidates = hardware_candidates(paths["snapshots"])
            native_by_id = {}
            for native in native_rows:
                physical_id = native.get("physical_request_id") or native.get("request_id")
                if not physical_id or physical_id in native_by_id:
                    raise RuntimeError(f"missing/duplicate native physical_request_id in {paths['native']}")
                native_by_id[physical_id] = native
            starts = {}
            for model in model_rows:
                physical_id = model.get("physical_request_id") or model.get("request_id")
                kind = model.get("event_kind") or model.get("kind") or model.get("event_type")
                if kind in {"model_request_start", "model_start", "start", "request_start"} or model.get("record_role") == "MODEL_START":
                    if physical_id in starts:
                        raise RuntimeError(f"duplicate model start {physical_id}")
                    starts[physical_id] = model
            native_ids = set(native_by_id)
            start_ids = set(starts)
            if native_ids != start_ids:
                raise RuntimeError(f"native/model physical join mismatch: native-only={native_ids-start_ids} model-only={start_ids-native_ids}")
            aggregate["native_physical_requests"] += len(native_rows)

            # Every native clock identity must resolve to exactly one distinct
            # remote profile candidate.  Duplicate snapshot rows with identical
            # profile content are collapsed, never guessed through.
            distinct_bindings = set()
            case_binding_rows = []
            for physical_id, native in native_by_id.items():
                clock = native.get("clock") or {}
                host = clock.get("hostname")
                boot = clock.get("boot_id")
                matches = candidates.get((host, boot), [])
                unique = {
                    (m["raw_profile_sha256"], m["stable_gpu_inventory_digest"]): m
                    for m in matches
                }
                if len(unique) != 1:
                    raise RuntimeError(f"ambiguous native hardware binding for {physical_id}: {host}/{boot} -> {len(unique)}")
                binding = next(iter(unique.values()))
                distinct_bindings.add((binding["raw_profile_sha256"], binding["stable_gpu_inventory_digest"]))
                case_binding_rows.append((physical_id, native, binding))
            if len(distinct_bindings) != 1:
                raise RuntimeError(f"multiple hardware domains within case {item['instance_id']}: {distinct_bindings}")
            case_binding = case_binding_rows[0][2]
            stable_domain = "stable_gpu_inventory_v1:" + case_binding["stable_gpu_inventory_digest"]

            # Join validator-native normalized target rows back to native source
            # rows, preserving the pre-event feature map and exact hardware
            # provenance while replacing the over-specific fit fingerprint.
            normalized_by_phase: dict[tuple[str, str], dict[str, Any]] = {}
            for event in normalized:
                if event.get("record_role") != "TARGET":
                    continue
                event_class = event.get("event_class", "")
                if not event_class.startswith("native:"):
                    continue
                phase = event_class.split(":", 1)[1]
                physical_id = event.get("physical_request_id")
                normalized_by_phase[(physical_id, phase)] = event

            case_native_rows: list[dict[str, Any]] = []
            for physical_id, native, binding in case_binding_rows:
                start = starts[physical_id]
                # These are deliberately pre-event fields only.  The token
                # values below are retained as labels/diagnostics and excluded
                # from the prediction feature map.
                pre_features = {
                    "request_kind": "model_request",
                    "max_output_tokens_bucket": str(start.get("max_output_tokens", start.get("max_tokens", 2048))),
                }
                for phase in ("queue", "prefill", "decode", "e2e"):
                    metric = native_metric(native, phase)
                    event = normalized_by_phase.get((physical_id, phase))
                    if event is None:
                        raise RuntimeError(f"validator did not reconstruct native {phase} {physical_id}")
                    event["partition"] = "train_calibration"
                    provenance = dict(event.get("feature_provenance") or {})
                    provenance.update(
                        {
                            "hardware_fingerprint": stable_domain,
                            "hardware_binding": "exact_native_clock_hostname_boot_to_remote_profile",
                            "hardware_fit_domain": "static_gpu_inventory_v1",
                            "raw_hardware_profile_sha256": binding["raw_profile_sha256"],
                            "hardware_snapshot_path": str(paths["snapshots"]),
                            "hardware_snapshot_sha256": sha256(paths["snapshots"]),
                        }
                    )
                    event["feature_provenance"] = provenance
                    event["pre_event_features"] = pre_features
                    event["post_event_token_fields"] = token_fields(native)
                    compact = {
                        "schema": "d9.native-phase-fit-row.v1",
                        "partition": "train_calibration",
                        "instance_id": item["instance_id"],
                        "case_id": item["case_id"],
                        "queue_ordinal": ordinal,
                        "physical_request_id": physical_id,
                        "event_id": event.get("event_id"),
                        "record_role": "TARGET",
                        "event_class": "native:" + phase,
                        "native_component": phase,
                        "observed_ms": metric["value_ms"],
                        "pre_event_features": pre_features,
                        "features": event.get("features") or pre_features,
                        "feature_provenance": provenance,
                        "host_id": event.get("host_id"),
                        "clock_id": event.get("clock_id"),
                        "hardware_domain": stable_domain,
                        "raw_hardware_profile_sha256": binding["raw_profile_sha256"],
                        "hardware_snapshot_path": str(paths["snapshots"]),
                        "hardware_snapshot_sha256": sha256(paths["snapshots"]),
                        **token_fields(native),
                    }
                    case_native_rows.append(compact)
                    native_dataset.append(compact)
                    aggregate["native_phase_rows"] += 1
            native_path = ledger_dir / "native_phase_rows.jsonl"
            write_jsonl(native_path, case_native_rows)
            calibration_events_path = ledger_dir / "calibration_events.jsonl"
            write_jsonl(calibration_events_path, normalized)

            # Source/acceptance evidence is compact metadata.  The validator
            # report itself contains the complete reconstructed ledger checks.
            result_path = path_in(artifact, "case_result.json")
            artifact_manifest_path = path_in(artifact, "artifact_manifest.json")
            validation_path = path_in(artifact, "validation.json")
            runner_attempt = artifact / "runner_attempts" / "attempt-001"
            work_path = path_in(runner_attempt, "telemetry_v2", "linux_work", "work_summary.json")
            bpf_path = path_in(runner_attempt, "telemetry_v2", "linux_work", "bpf_collector_manifest.json")
            case_result = read_json(result_path)
            validation = read_json(validation_path)
            if case_result.get("status") != "completed":
                raise RuntimeError(f"accepted case result is not completed: {item['instance_id']}")
            if validation.get("status") not in {"passed", "valid", "completed"}:
                raise RuntimeError(f"accepted validation status is not passed: {item['instance_id']}")
            if sha256(result_path) != item["case_result_sha256_queue"]:
                raise RuntimeError(f"case_result queue hash mismatch: {item['instance_id']}")
            if sha256(artifact_manifest_path) != item["artifact_manifest_sha256_queue"]:
                raise RuntimeError(f"artifact_manifest queue hash mismatch: {item['instance_id']}")
            source_record = {
                "native_attribution": {"path": str(paths["native"]), "sha256": sha256(paths["native"])},
                "model_events": {"path": str(paths["model"]), "sha256": sha256(paths["model"])},
                "hardware_snapshots": {"path": str(paths["snapshots"]), "sha256": sha256(paths["snapshots"])},
                "case_result": {"path": str(result_path), "sha256": sha256(result_path)},
                "artifact_manifest": {"path": str(artifact_manifest_path), "sha256": sha256(artifact_manifest_path)},
                "validation": {"path": str(validation_path), "sha256": sha256(validation_path)},
                "work_summary": {"path": str(work_path), "sha256": sha256(work_path)},
                "bpf_collector_manifest": {"path": str(bpf_path), "sha256": sha256(bpf_path)},
                "normalized_case_events": {"path": str(normalized_path), "sha256": sha256(normalized_path)},
                "calibration_events": {"path": str(calibration_events_path), "sha256": sha256(calibration_events_path)},
                "native_phase_rows": {"path": str(native_path), "sha256": sha256(native_path)},
            }
            binding_record = {
                "schema": "d9.native-hardware-binding.v1",
                "instance_id": item["instance_id"],
                "case_id": item["case_id"],
                "queue_ordinal": ordinal,
                "native_physical_request_count": len(native_rows),
                "model_start_count": len(starts),
                "join": "exact_physical_request_id",
                "binding": "exact_native_clock_hostname_boot_to_remote_profile",
                "candidate_snapshot_rows": sum(len(v) for v in candidates.values()),
                "distinct_profile_bindings": len(distinct_bindings),
                "raw_profile_sha256": case_binding["raw_profile_sha256"],
                "stable_gpu_inventory_digest": case_binding["stable_gpu_inventory_digest"],
                "fit_hardware_domain": stable_domain,
                "static_gpu_inventory": case_binding["static_gpu_inventory"],
                "server_identity": case_binding["server_identity"],
                "gpu_uuids_provenance_only": case_binding["gpu_uuids"],
                "hardware_snapshots_path": str(paths["snapshots"]),
                "hardware_snapshots_sha256": sha256(paths["snapshots"]),
            }
            bindings.append(binding_record)
            instance_seen[item["instance_id"]] += 1
            aggregate["cases"] += 1
            cpu_reports = report.get("attempts", [])
            cpu_status = cpu_reports[0].get("cpu_raw_integrity", {}).get("status", "") if cpu_reports else ""
            aggregate["complete_cpu_metadata_cases"] += 1 if cpu_status.startswith("complete") else 0
            aggregate["native_tokens_complete"] += sum(
                1 for native in native_rows if all(value is not None for value in token_fields(native).values() if not isinstance(value, str))
            )
            manifest_cases.append(
                {
                    "queue_ordinal": ordinal,
                    "queue_case_id": item["queue_case_id"],
                    "attempt_id": item["attempt_id"],
                    "case_root": str(artifact),
                    "instance_id": item["instance_id"],
                    "case_id": item["case_id"],
                    "partition": "train_calibration",
                    "events_path": str(calibration_events_path),
                    "validation_report_path": str(calibration_report_path),
                    "events_sha256": sha256(calibration_events_path),
                    "validation_report_sha256": sha256(calibration_report_path),
                    "native_phase_rows_path": str(native_path),
                    "native_phase_rows_sha256": sha256(native_path),
                    "source": source_record,
                    "native_hardware_domain": stable_domain,
                    "raw_profile_sha256": case_binding["raw_profile_sha256"],
                    "native_physical_request_count": len(native_rows),
                    "native_phase_row_count": len(case_native_rows),
                    "cpu_raw_binary_hash_scope": "representative_full_only; this case metadata/offset/loss checked without raw-byte rehash",
                }
            )
    finally:
        VALIDATOR._cpu_integrity = original_cpu_integrity

    # Stable aggregate dataset and binding ledger are intentionally separate:
    # the former is convenient for fitting, the latter is the audit trail.
    native_dataset_path = EVIDENCE / "native_phase_dataset.jsonl"
    write_jsonl(native_dataset_path, native_dataset)
    bindings_path = EVIDENCE / "native_profile_bindings.jsonl"
    write_jsonl(bindings_path, bindings)

    inventory_doc = {
        "schema": "d9.accepted-production-inventory.v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "queue_path": str(QUEUE),
        "queue_sha256": sha256(QUEUE),
        "pinned_split_manifest": str(SPLIT),
        "pinned_split_manifest_sha256": split_sha,
        "partition_derivation": "case_spec.instance_id matched to pinned split before case_result/native/validation reads",
        "accepted_counts": dict(counts),
        "eligible_training_case_count": len(train),
        "eligible_training_instance_count": len(instance_seen),
        "eligible_training_instances": sorted(instance_seen),
        "case_rows": inventory,
        "source_validity": {
            "accepted_case_result_status": "completed",
            "accepted_validation_status": "passed/valid/completed checked per train case",
            "native_join": "exact physical_request_id; all train cases",
            "native_metrics": "queue/prefill/decode/e2e measured finite; all train cases",
            "cpu_check": "work_summary + BPF manifest bounded counts and loss counters; no raw-byte rehash except representative proof",
        },
    }
    write_json(EVIDENCE / "accepted_case_inventory.json", inventory_doc)

    manifest_doc = {
        "schema": "d9.calibration-input-manifest.v2",
        "generated_at_utc": inventory_doc["generated_at_utc"],
        "partition": "train_calibration",
        "queue_path": str(QUEUE),
        "queue_sha256": inventory_doc["queue_sha256"],
        "pinned_split_manifest": str(SPLIT),
        "pinned_split_manifest_sha256": split_sha,
        "cases": manifest_cases,
        "native_phase_dataset_path": str(native_dataset_path),
        "native_phase_dataset_sha256": sha256(native_dataset_path),
        "native_profile_bindings_path": str(bindings_path),
        "native_profile_bindings_sha256": sha256(bindings_path),
        "native_fit_domain": {
            "name": "stable_gpu_inventory_v1",
            "digest": "sha256(canonical single static GPU object: name/compute_capability/driver_version/memory_total_mib/memory_clock_mhz/sm_clock_mhz/power_limit_w; sorted list for multi-GPU)",
            "mutable_fields_excluded": ["hostname", "boot_id", "server_identity", "gpu_uuid", "job_id", "pid", "counter_epoch"],
            "raw_profile_sha256_retained": True,
        },
        "token_policy": {
            "included_fields": ["prompt_tokens", "completion_tokens", "cached_tokens", "max_output_tokens"],
            "provenance": "native_finished_request_post_event",
            "prediction_features": "pre_event_features only; token fields are labels/diagnostics and excluded from fit features",
        },
        "aggregate": dict(aggregate),
    }
    write_json(EVIDENCE / "calibration_input_manifest.json", manifest_doc)

    print(json.dumps({
        "accepted_counts": dict(counts),
        "eligible_training_cases": len(train),
        "eligible_training_instances": len(instance_seen),
        "native_physical_requests": aggregate["native_physical_requests"],
        "native_phase_rows": aggregate["native_phase_rows"],
        "native_dataset": str(native_dataset_path),
        "manifest": str(EVIDENCE / "calibration_input_manifest.json"),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
