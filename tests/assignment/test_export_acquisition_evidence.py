import ctypes as ct
import hashlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agentic_sim.telemetry.bpf_work import _CWorkEvent, _CWorkEventLegacy
from scripts.validation.export_acquisition_evidence import (
    EXPORT_SCHEMA,
    ExportError,
    export_acquisition_evidence,
    validate_export,
)


IDENTITY = {
    "run_id": "run-fixture",
    "attempt_id": "attempt-fixture",
    "case_id": "case-fixture",
}
CLOCK = {
    "hostname": "host-fixture",
    "boot_id": "boot-fixture",
    "clock_id": "CLOCK_MONOTONIC_RAW",
}


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def lifecycle_pair(span_id, phase, start, end, *, event_kind=None):
    base = {
        "schema_version": "assignment.telemetry.v2.lifecycle",
        **IDENTITY,
        "clock": CLOCK,
        "span_id": span_id,
        "phase": phase,
        "event_kind": event_kind or phase,
        "provenance": "measured",
        "availability": "measured",
    }
    return [
        {
            **base,
            "event_id": f"{span_id}-start",
            "terminal": False,
            "status": "pending",
            "start_mono_ns": start,
            "end_mono_ns": None,
            "duration_ms": None,
        },
        {
            **base,
            "event_id": f"{span_id}-end",
            "terminal": True,
            "status": "success",
            "start_mono_ns": start,
            "end_mono_ns": end,
            "duration_ms": (end - start) / 1_000_000,
        },
    ]


def make_source(tmp_path: Path, *, v3: bool = False) -> Path:
    root = tmp_path / "source"
    telemetry = root / "telemetry"
    linux = root / "linux_work"
    payloads = telemetry / "request_payloads"
    telemetry.mkdir(parents=True)
    linux.mkdir(parents=True)
    payloads.mkdir(parents=True)
    write_json(
        telemetry / "telemetry_manifest.json",
        {
            "schema_version": "assignment.telemetry.v2.manifest",
            **IDENTITY,
            "clock": CLOCK,
            "streams": {
                "lifecycle": "lifecycle_events.jsonl",
                "tool": "tool_events.jsonl",
                "model": "model_events.jsonl",
                "hardware": "hardware_snapshots.jsonl",
            },
            "raw_hardware_inventory": {"cpu": "fixture"},
        },
    )
    lifecycle = lifecycle_pair("outer", "outer_swe_agent", 0, 1_000_000_000) + lifecycle_pair(
        "nested", "tool_execution", 100_000_000, 400_000_000
    )
    write_jsonl(telemetry / "lifecycle_events.jsonl", lifecycle)
    tool_rows = [
        {
            "schema_version": "assignment.telemetry.v2.tool",
            **IDENTITY,
            "clock": CLOCK,
            "event_id": "tool-start",
            "span_id": "tool-span",
            "event_kind": "tool_event_start",
            "phase": "tool_execution",
            "terminal": False,
            "status": "pending",
            "start_mono_ns": 100_000_000,
            "end_mono_ns": None,
            "duration_ms": None,
            "provenance": "measured",
            "availability": "measured",
        },
        {
            "schema_version": "assignment.telemetry.v2.tool",
            **IDENTITY,
            "clock": CLOCK,
            "event_id": "tool-end",
            "span_id": "tool-span",
            "event_kind": "tool_event",
            "phase": "tool_execution",
            "terminal": True,
            "status": "success",
            "start_mono_ns": 100_000_000,
            "end_mono_ns": 400_000_000,
            "duration_ms": 300.0,
            "provenance": "measured",
            "availability": "measured",
            "action_id": "action-1",
        },
    ]
    write_jsonl(telemetry / "tool_events.jsonl", tool_rows)
    request = {"prompt": "fixture", "max_tokens": 2}
    response = {"choices": [{"text": "ok"}]}
    request_bytes = json.dumps(request, sort_keys=True).encode()
    response_bytes = json.dumps(response, sort_keys=True).encode()
    request_path = payloads / "physical-1.request.bin"
    response_path = payloads / "physical-1.response.bin"
    request_path.write_bytes(request_bytes)
    response_path.write_bytes(response_bytes)
    request_hash = hashlib.sha256(request_bytes).hexdigest()
    response_hash = hashlib.sha256(response_bytes).hexdigest()
    model_start = {
        "schema_version": "assignment.telemetry.v2.model",
        **IDENTITY,
        "clock": CLOCK,
        "event_id": "model-start",
        "span_id": "model-span",
        "event_kind": "model_request_start",
        "phase": "model_request",
        "terminal": False,
        "status": "pending",
        "start_mono_ns": 500_000_000,
        "end_mono_ns": None,
        "duration_ms": None,
        "provenance": "measured",
        "availability": "measured",
        "physical_request_id": "physical-1",
        "logical_request_id": "logical-1",
        "retry_index": 0,
        "retry_of": None,
    }
    model_end = {
        **model_start,
        "event_id": "model-end",
        "event_kind": "model_request",
        "terminal": True,
        "status": "success",
        "end_mono_ns": 550_000_000,
        "duration_ms": 50.0,
        "request_body_sha256": request_hash,
        "response_body_sha256": response_hash,
        "request_payload_artifact": {
            "physical_request_id": "physical-1",
            "request": {
                "artifact_path": "request_payloads/physical-1.request.bin",
                "bytes": len(request_bytes),
                "complete": True,
                "sha256": request_hash,
            },
            "response": {
                "artifact_path": "request_payloads/physical-1.response.bin",
                "bytes": len(response_bytes),
                "complete": True,
                "sha256": response_hash,
            },
        },
    }
    write_jsonl(telemetry / "model_events.jsonl", [model_start, model_end])
    write_jsonl(telemetry / "hardware_snapshots.jsonl", [])

    event_type = _CWorkEvent if v3 else _CWorkEventLegacy
    event = event_type()
    event.token = 123
    event.sequence = 0
    event.kernel_start_ns = 120_000_000
    event.kernel_end_ns = 130_000_000
    event.ret = 4
    event.syscall_nr = 0
    event.tgid = 7
    event.tid = 7
    event.parent_tgid = 7
    event.kind = 1
    event.status = 1
    event.path_status = 0
    event.path_len = 0
    event.path2_status = 0
    event.path2_len = 0
    if v3:
        event.raw_args[0] = 1234
        event.raw_args[1] = 5678
    binary = bytes(event)
    (linux / "raw_events.bin").write_bytes(binary)
    write_json(
        linux / "bpf_collector_manifest.json",
        {
            "schema_version": "assignment.linux-bpf-work-collector.v1",
            "identity": {**IDENTITY, "boot_id": "boot-fixture"},
            "raw_event_stream": {
                "record_size_bytes": ct.sizeof(event_type),
                "schema_version": "assignment.linux-bpf-work-event.v3" if v3 else "assignment.linux-bpf-work-event.v2",
            },
        },
    )
    write_jsonl(
        linux / "raw_aggregates.jsonl",
        [
            {
                "schema_version": "assignment.linux-bpf-work-raw.v2",
                "action_token": 123,
                "command_sha256": "a" * 64,
                "identity": {**IDENTITY, "boot_id": "boot-fixture"},
                "boundary": {
                    "event_id": "tool-start",
                    "start_mono_ns": 100_000_000,
                    "end_mono_ns": 400_000_000,
                    "status": "success",
                },
                "raw_aggregate": {field: 0 for field in ("lost_event_records", "lost_path_records", "lost_pending_records", "lineage_map_failures")},
                "required_event_count": 1,
                "event_records_complete": True,
                "aggregate_missing": False,
                "binary_event_stream": {"offset_start": 0, "offset_end": len(binary), "record_count": 1},
                "events": [],
            }
        ],
    )
    return root


def test_export_roundtrip_is_saved_only_and_source_local(tmp_path):
    source = make_source(tmp_path)
    output = tmp_path / "export"
    manifest = export_acquisition_evidence([source], output)
    assert manifest["schema_version"] == EXPORT_SCHEMA
    assert manifest["status"] == "pass"
    assert manifest["counts"]["bpf_operations"] == 1
    assert manifest["counts"]["model_payload_rows"] == 2
    assert validate_export(output)["status"] == "pass"
    operation = json.loads((output / "bpf_operations.jsonl").read_text().splitlines()[0])
    assert operation["action_token"] == 123
    assert operation["record_index"] == 0
    assert operation["action_record_index"] == 0
    assert operation["syscall_args_provenance"] == "unavailable_not_in_saved_record"
    assert len(list((output / "source_artifacts").rglob("*"))) >= 1
    assert any(row["binding_kind"] == "saved_source_artifact" for row in (json.loads(line) for line in (output / "source_bindings.jsonl").read_text().splitlines()))


def test_v3_scalar_arguments_and_timing_are_exported(tmp_path):
    source = make_source(tmp_path, v3=True)
    output = tmp_path / "export-v3"
    export_acquisition_evidence([source], output)
    assert validate_export(output)["status"] == "pass"
    operation = json.loads((output / "bpf_operations.jsonl").read_text().splitlines()[0])
    assert operation["syscall_args_provenance"] == "measured_raw_scalar_args"
    assert operation["syscall_args"]["raw_scalar_args"][:2] == [1234, 5678]
    assert operation["status_name"] == "success"
    assert operation["censored"] is False
    assert operation["kernel_start_ns"] == 120_000_000
    assert operation["kernel_end_ns"] == 130_000_000
    assert operation["duration_ns"] == 10_000_000


def test_exact_hash_manifest_references_are_copied(tmp_path):
    source = make_source(tmp_path)
    config = source / "configs" / "production.json"
    config.parent.mkdir(parents=True)
    config.write_text('{"worker_count": 2}\n', encoding="utf-8")
    write_json(source / "artifact_hashes.json", {
        "configs/production.json": hashlib.sha256(config.read_bytes()).hexdigest(),
    })
    output = tmp_path / "export-manifest"
    manifest = export_acquisition_evidence([source], output)
    assert manifest["status"] == "pass"
    copied = output / "source_artifacts" / next(iter(manifest["sources"])) / "configs/production.json"
    assert copied.read_bytes() == config.read_bytes()
    source_row = json.loads((output / "sources.jsonl").read_text().splitlines()[0])
    assert all(row["status"] == "pass" for row in source_row["declared_hash_audit"])
    assert validate_export(output)["status"] == "pass"


def test_missing_operation_row_fails_saved_only_validation(tmp_path):
    source = make_source(tmp_path)
    output = tmp_path / "export"
    export_acquisition_evidence([source], output)
    (output / "bpf_operations.jsonl").write_text("", encoding="utf-8")
    result = validate_export(output)
    assert result["status"] == "fail"
    (output / "bpf_operations.jsonl").write_text(
        json.dumps({"source_id": "source", "action_token": 123, "record_index": 1, "action_record_index": 1, "syscall_args_provenance": "unavailable"}) + "\n",
        encoding="utf-8",
    )
    assert validate_export(output)["status"] == "fail"


def test_wrong_attempt_identity_fails_saved_only_validation(tmp_path):
    source = make_source(tmp_path)
    output = tmp_path / "export"
    export_acquisition_evidence([source], output)
    rows = [json.loads(line) for line in (output / "bpf_operations.jsonl").read_text().splitlines()]
    rows[0]["attempt_id"] = "wrong-attempt"
    (output / "bpf_operations.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )
    result = validate_export(output)
    assert result["status"] == "fail"
    assert any("attempt_id" in error for error in result["errors"])


def test_aggregate_only_is_explicit_and_never_emitted_as_operation(tmp_path):
    source = make_source(tmp_path)
    (source / "linux_work/raw_events.bin").unlink()
    output = tmp_path / "export"
    manifest = export_acquisition_evidence([source], output)
    assert manifest["counts"]["bpf_operations"] == 0
    action = json.loads((output / "bpf_actions.jsonl").read_text().splitlines()[0])
    assert action["aggregate_only"] is True
    assert "no_binary_stream" in action["operation_evidence_status"]
    assert json.loads((output / "unknown_joins.jsonl").read_text().splitlines()[0])["status"] == "unknown"


def test_binary_stream_is_not_dropped_when_aggregate_journal_is_empty(tmp_path):
    source = make_source(tmp_path)
    (source / "linux_work/raw_aggregates.jsonl").unlink()
    output = tmp_path / "export-no-aggregate"
    manifest = export_acquisition_evidence([source], output)
    assert manifest["counts"]["bpf_operations"] == 1
    operation = json.loads((output / "bpf_operations.jsonl").read_text().splitlines()[0])
    assert operation["action_join_status"] == "unknown_action_token"
    assert operation["provenance"] == "measured"
    joins = [json.loads(line) for line in (output / "unknown_joins.jsonl").read_text().splitlines()]
    assert any("has no rows" in row.get("reason", "") for row in joins)
    assert validate_export(output)["status"] == "pass"


def test_saved_binary_packet_hash_roundtrip_rejects_mutation(tmp_path):
    source = make_source(tmp_path, v3=True)
    output = tmp_path / "export-mutated-binary"
    export_acquisition_evidence([source], output)
    bindings = [json.loads(line) for line in (output / "source_bindings.jsonl").read_text().splitlines()]
    binary = next(row for row in bindings if row.get("binding_kind") == "saved_source_artifact" and row.get("source_relative_path") == "linux_work/raw_events.bin")
    path = output / binary["export_relative_path"]
    data = bytearray(path.read_bytes())
    data[0] ^= 0x01
    path.write_bytes(data)
    result = validate_export(output)
    assert result["status"] == "fail"
    assert any("raw_events.bin" in error or "BPF packet hash" in error for error in result["errors"])
