"""Focused offline contract tests for the privileged BCC work candidate."""

from __future__ import annotations

import ctypes as ct
import io
import tempfile
import threading
import unittest
from types import SimpleNamespace

from agentic_sim.telemetry.bpf_work import (
    BPF_EVENT_SCHEMA,
    BPF_PATH_CAP,
    BPF_PROGRAM,
    BPF_RAW_SCHEMA,
    BPF_SOCKET_SCHEMA,
    BpfWorkClient,
    BpfWorkCollector,
    BpfWorkService,
    ProcessIdentity,
    ProcessTarget,
    ActionBoundary,
    _CAggregate,
    _CProcAction,
    _CWorkEvent,
    _raw_path,
    _run_bash_action_unmodified,
    _spawn_bash_fixture,
    _stop_bash_fixture,
)


class BpfWorkContractTests(unittest.TestCase):
    def test_program_requires_individual_perf_records_and_lineage(self) -> None:
        self.assertIn("BPF_PERF_OUTPUT(work_events)", BPF_PROGRAM)
        self.assertIn("TRACEPOINT_PROBE(raw_syscalls, sys_enter)", BPF_PROGRAM)
        self.assertIn("TRACEPOINT_PROBE(raw_syscalls, sys_exit)", BPF_PROGRAM)
        self.assertIn("int on_fork(struct fork_tracepoint_args *args)", BPF_PROGRAM)
        self.assertIn("lost_pending_records", BPF_PROGRAM)
        self.assertIn("censored_pending_records", BPF_PROGRAM)
        self.assertIn("required_event_count", BPF_PROGRAM)
        self.assertIn("K_MUTATION", BPF_PROGRAM)
        self.assertIn("K_RENAME", BPF_PROGRAM)
        self.assertIn("__NR_mmap", BPF_PROGRAM)
        self.assertIn("__NR_msync", BPF_PROGRAM)
        self.assertIn("path2", BPF_PROGRAM)
        self.assertNotIn("TRACEPOINT_PROBE(syscalls, sys_enter_read)", BPF_PROGRAM)

    def test_ctypes_records_match_declared_kernel_layout(self) -> None:
        # The event keeps two fixed path payloads so rename source/destination
        # evidence cannot be conflated.  The aggregate appends kind counters
        # and latency sums without changing earlier field offsets.
        self.assertEqual(ct.sizeof(_CWorkEvent), 352)
        self.assertEqual(_CWorkEvent.path.offset, 84)
        self.assertEqual(ct.sizeof(_CProcAction), 24)
        self.assertEqual(ct.sizeof(_CAggregate), 328)
        self.assertEqual(_CWorkEvent._fields_[-1][1]._length_, BPF_PATH_CAP)

    def test_raw_path_preserves_bytes_and_explicit_empty(self) -> None:
        value = SimpleNamespace(path=(ct.c_char * BPF_PATH_CAP)(*b"/tmp/a"), path_len=6)
        path, encoded = _raw_path(value, 6)
        self.assertEqual(path, "/tmp/a")
        self.assertEqual(encoded, b"/tmp/a".hex())
        empty = SimpleNamespace(path=(ct.c_char * BPF_PATH_CAP)(), path_len=0)
        self.assertEqual(_raw_path(empty, 0), (None, None))

    def test_perf_callback_decodes_event_and_loss_is_recorded(self) -> None:
        class FakeTable:
            def event(self, _data):
                value = _CWorkEvent()
                value.token = 123
                value.sequence = 7
                value.kernel_start_ns = 100
                value.kernel_end_ns = 130
                value.ret = 4
                value.syscall_nr = 0
                value.tgid = 10
                value.tid = 11
                value.parent_tgid = 9
                value.kind = 1
                value.status = 1
                value.path_status = 1
                value.path_len = 6
                value.path2_status = 1
                value.path2_len = 6
                value.fd = 4
                value.child_pid = 0
                value.path = b"/tmp/a"
                value.path2 = b"/tmp/b"
                return value

        collector = object.__new__(BpfWorkCollector)
        collector._event_table = FakeTable()
        collector._events_by_token = {}
        collector._event_condition = threading.Condition()
        collector._event_lock = collector._event_condition
        collector._deferred_tokens = {}
        collector._event_seen_by_token = {}
        collector._raw_event_stream = io.BytesIO()
        collector._raw_event_offset = 0
        collector._raw_event_records = 0
        collector._perf_lost_total = 0
        collector._perf_callback_errors = []
        collector._on_perf_event(0, object(), 216)
        collector._on_perf_lost(3)
        rows = collector._events_for_token(123)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], BPF_EVENT_SCHEMA)
        self.assertEqual(rows[0]["kind_name"], "read")
        self.assertEqual(rows[0]["duration_ns"], 30)
        self.assertEqual(rows[0]["path"], "/tmp/a")
        self.assertEqual(rows[0]["path2"], "/tmp/b")
        self.assertEqual(collector._perf_lost_total, 3)
        self.assertEqual(collector._perf_callback_errors, [])

    def test_raw_schema_is_versioned_for_full_event_journal(self) -> None:
        self.assertEqual(BPF_RAW_SCHEMA, "assignment.linux-bpf-work-raw.v2")

    def test_negative_kernel_duration_is_corruption(self) -> None:
        value = _CWorkEvent()
        value.kernel_start_ns = 20
        value.kernel_end_ns = 19
        with self.assertRaises(ValueError):
            BpfWorkCollector._event_row(bytes(value))

    def test_unmodified_shell_fixture_returns_to_stdin_read(self) -> None:
        process = _spawn_bash_fixture()
        try:
            _run_bash_action_unmodified(
                process,
                "printf 'fixture-work\\n' >/dev/null",
                "__UNMODIFIED_TEST__",
                2.0,
            )
            # No stopped helper or synthetic wait owns the shell here.  It is
            # alive and has returned to its ordinary persistent stdin read.
            self.assertIsNone(process.poll())
        finally:
            _stop_bash_fixture(process)

    def test_socket_service_roundtrip_binds_identity_and_stops(self) -> None:
        target = ProcessTarget(
            pid=1,
            run_id="run",
            attempt_id="attempt",
            case_id="case",
            mapping_source="test-persistent-shell",
        )
        identity = ProcessIdentity(
            pid=1,
            start_ticks=1,
            boot_id="boot",
            pid_namespace_inode=1,
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            case_id=target.case_id,
            instance_id=None,
            container_pid=None,
            pid_namespace=None,
            mapping_source=target.mapping_source,
        )

        class FakeCollector:
            startup_wall_ms = 0.1

            def __init__(self) -> None:
                self.identity = identity
                self.closed = False

            def start_action(self, event_id: str, command: str, **_: object) -> ActionBoundary:
                return ActionBoundary(
                    event_id=event_id,
                    command=command,
                    command_sha256=__import__("hashlib").sha256(command.encode()).hexdigest(),
                    start_wall_ns=1,
                    start_mono_ns=1,
                )

            def end_action(self, event_id: str, **kwargs: object) -> ActionBoundary:
                command = "printf ok"
                return ActionBoundary(
                    event_id=event_id,
                    command=command,
                    command_sha256=__import__("hashlib").sha256(command.encode()).hexdigest(),
                    start_wall_ns=1,
                    start_mono_ns=1,
                    end_wall_ns=2,
                    end_mono_ns=2,
                    status=str(kwargs.get("status", "success")),
                )

            def close(self) -> dict[str, object]:
                self.closed = True
                return {"schema_version": "summary", "status": "closed"}

        with tempfile.TemporaryDirectory(prefix="bpf-service-test-") as directory:
            socket_path = __import__("pathlib").Path(directory) / "collector.sock"
            service = BpfWorkService(FakeCollector(), socket_path)
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            client = BpfWorkClient(socket_path, identity=identity.to_mapping())
            for _ in range(100):
                try:
                    ping = client.ping()
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    __import__("time").sleep(0.005)
            else:
                self.fail("BPF socket service did not become ready")
            self.assertEqual(ping["schema_version"], BPF_SOCKET_SCHEMA)
            client.start_action("event", "printf ok", start_mono_ns=1)
            client.end_action("event", status="success", end_mono_ns=2)
            summary = client.stop()
            self.assertEqual(summary["schema_version"], "summary")
            self.assertEqual(summary["action_count"], 0)
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
