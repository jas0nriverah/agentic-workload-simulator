from __future__ import annotations

import ctypes as ct
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.telemetry.bpf_work import BPF_EVENT_SCHEMA, BPF_RAW_SCHEMA, _CWorkEvent


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/sweagent_case_runner.py"
SPEC = importlib.util.spec_from_file_location("assignment_bpf_case_audit", SCRIPT)
assert SPEC and SPEC.loader
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


def _event_packet(token: int, sequence: int, *, status: int = 1, kind: int = 16) -> bytes:
    event = _CWorkEvent()
    event.token = token
    event.sequence = sequence
    event.kernel_start_ns = 100 + sequence
    event.kernel_end_ns = 120 + sequence
    event.ret = 1
    event.syscall_nr = 0
    event.tgid = 10
    event.tid = 11
    event.parent_tgid = 10
    event.kind = kind
    event.status = status
    event.path_status = 0
    event.path_len = 0
    event.child_pid = 0
    event.fd = -1
    event.path2_status = 0
    event.path2_len = 0
    if status == 3:
        event.kernel_end_ns = 140 + sequence
        event.ret = 0
    return ct.string_at(ct.addressof(event), ct.sizeof(event))


class BpfCaseAuditTests(unittest.TestCase):
    def _identity(self) -> tuple[dict[str, object], str]:
        identity = {
            "pid": 123,
            "start_ticks": 456,
            "boot_id": "boot",
            "pid_namespace_inode": 789,
            "run_id": "run",
            "attempt_id": "attempt",
            "case_id": "case",
            "instance_id": "instance",
            "container_pid": 1,
            "pid_namespace": "pidns",
            "mapping_source": "test",
        }
        digest = hashlib.sha256(ADAPTER._canonical(identity).encode()).hexdigest()
        return identity, digest

    def _build(self, root: Path, *, deferred: bool = False) -> tuple[dict, list[dict]]:
        identity, binding = self._identity()
        token = 99
        command = "printf audit"
        command_sha = hashlib.sha256(command.encode()).hexdigest()
        packet_one = _event_packet(token, 1)
        packet_two = _event_packet(token, 2, status=3)
        binary = root / "raw_events.bin"
        binary.write_bytes(packet_one + (packet_two if deferred else b""))
        raw_path = root / "raw_aggregates.jsonl"
        def stream(end: int, count: int) -> dict:
            return {
                "path": str(binary),
                "schema_version": BPF_EVENT_SCHEMA,
                "record_size_bytes": ct.sizeof(_CWorkEvent),
                "offset_start": 0,
                "offset_end": end,
                "byte_length": end,
                "record_count": count,
                "durable_at_boundary": True,
            }
        def row(*, final: bool = False) -> dict:
            count = 2 if final else 1
            value = {
                "schema_version": BPF_RAW_SCHEMA,
                **({"record_type": "action_finalization"} if final else {}),
                "backend": "bcc",
                "program_sha256": "a" * 64,
                "identity": identity,
                "identity_binding_digest": binding,
                "boundary": {
                    "phase": "complete",
                    "event_id": "action-1",
                    "command": command,
                    "command_sha256": command_sha,
                    "start_wall_ns": 1,
                    "start_mono_ns": 2,
                    "end_wall_ns": 3,
                    "end_mono_ns": 4,
                    "status": "success",
                    "identity": identity,
                },
                "action_token": token,
                "command_sha256": command_sha,
                "raw_aggregate": {
                    "lost_event_records": 0,
                    "lost_path_records": 0,
                    "lost_pending_records": 0,
                    "lineage_map_failures": 0,
                    "censored_pending_records": 0,
                },
                "event_schema_version": BPF_EVENT_SCHEMA,
                "events": [],
                "event_storage": "binary",
                "event_count": count,
                "required_event_count": count,
                "perf_lost_events": 0,
                "event_records_complete": True,
                "event_callback_errors": [],
                "binary_event_stream": stream(count * ct.sizeof(_CWorkEvent), count),
                "path_records": [],
                "aggregate_missing": False,
                "in_flight_at_flush_timeout": 1 if deferred else 0,
                "deferred_quiescence": deferred,
                "censored_pending": [],
            }
            if final:
                value["censor_boundary"] = {
                    "clock_id": "CLOCK_MONOTONIC",
                    "censor_boundary_ns": 200,
                    "pending_count": 0,
                    "in_flight_at_stop": 0,
                    "pending_witness_gap": 0,
                }
            return value
        boundary = row()
        final = row(final=True) if deferred else None
        rows = [boundary] + ([final] if final else [])
        raw_path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")
        raw_stream = {
            "path": str(binary),
            "schema_version": BPF_EVENT_SCHEMA,
            "record_size_bytes": ct.sizeof(_CWorkEvent),
            "records_written": 2 if deferred else 1,
            "bytes_written": binary.stat().st_size,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
        summary = {
            "schema_version": ADAPTER.BPF_WORK_SUMMARY_SCHEMA,
            "identity": identity,
            "identity_binding_digest": binding,
            "program_sha256": "a" * 64,
            "raw_aggregate_journal": str(raw_path),
            "raw_event_stream": raw_stream,
            "actions": [{"event_id": "action-1", "action_token": token, "raw": boundary}],
            "action_finalizations": ([final] if final else []),
        }
        expected = [{
            "event_id": "action-1",
            "actual_action": command,
            "actual_action_sha256": command_sha,
        }]
        return summary, expected

    def test_binary_packets_are_the_count_source_and_status_three_is_faithful(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, expected = self._build(Path(directory), deferred=True)
            result = ADAPTER._audit_bpf_work_evidence(
                work_dir=Path(directory),
                summary=summary,
                expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                expected_tool_rows=expected,
            )
            self.assertEqual(result["raw_action_count"], 1)
            self.assertEqual(result["action_finalization_count"], 1)
            self.assertEqual(result["individual_operation_count"], 1)
            self.assertEqual(result["finalized_individual_operation_count"], 2)

    def test_binary_hash_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, expected = self._build(root)
            (root / "raw_events.bin").write_bytes((root / "raw_events.bin").read_bytes()[:-1] + b"x")
            with self.assertRaises(ADAPTER.CaseRunnerError):
                ADAPTER._audit_bpf_work_evidence(
                    work_dir=root,
                    summary=summary,
                    expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                    expected_tool_rows=expected,
                )

    def test_nondeferred_binary_action_requires_complete_capture(self):
        for complete in (False, True):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                summary, expected = self._build(root)
                raw = summary["actions"][0]["raw"]
                raw["event_records_complete"] = complete
                (root / "raw_aggregates.jsonl").write_text(json.dumps(raw) + "\n", encoding="utf-8")
                kwargs = dict(work_dir=root, summary=summary,
                              expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                              expected_tool_rows=expected)
                if complete:
                    self.assertEqual(ADAPTER._audit_bpf_work_evidence(**kwargs)["raw_action_count"], 1)
                else:
                    with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "complete individual-event capture"):
                        ADAPTER._audit_bpf_work_evidence(**kwargs)

    def test_incomplete_deferred_boundary_is_accepted_with_complete_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, expected = self._build(root, deferred=True)
            raw = summary["actions"][0]["raw"]
            raw["event_records_complete"] = False
            (root / "raw_aggregates.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in [raw, *summary["action_finalizations"]]),
                encoding="utf-8",
            )
            result = ADAPTER._audit_bpf_work_evidence(
                work_dir=root, summary=summary,
                expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                expected_tool_rows=expected,
            )
            self.assertEqual(result["action_finalization_count"], 1)

    def test_missing_executed_action_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            summary, expected = self._build(Path(directory))
            with self.assertRaises(ADAPTER.CaseRunnerError):
                ADAPTER._audit_bpf_work_evidence(
                    work_dir=Path(directory),
                    summary=summary,
                    expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                    expected_tool_rows=[],
                )

    def test_empty_framework_command_is_not_replaced_by_missing_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary, expected = self._build(root)
            raw = summary["actions"][0]["raw"]
            empty_hash = hashlib.sha256(b"").hexdigest()
            raw["boundary"].update(command="", command_sha256=empty_hash)
            raw["command_sha256"] = empty_hash
            (root / "raw_aggregates.jsonl").write_text(json.dumps(raw) + "\n", encoding="utf-8")
            expected[0].update(actual_action="", actual_action_sha256=empty_hash,
                               event_kind="runtime_command_start")
            kwargs = dict(work_dir=root, summary=summary,
                          expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
                          expected_tool_rows=expected)
            self.assertEqual(ADAPTER._audit_bpf_work_evidence(**kwargs)["raw_action_count"], 1)
            expected[0].pop("actual_action")
            with self.assertRaisesRegex(ADAPTER.CaseRunnerError, "command text"):
                ADAPTER._audit_bpf_work_evidence(**kwargs)


if __name__ == "__main__":
    unittest.main()
