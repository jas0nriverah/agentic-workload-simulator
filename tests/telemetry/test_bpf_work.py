"""Focused offline contract tests for the privileged BCC work candidate."""

from __future__ import annotations

import ctypes as ct
import hashlib
import io
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path

from agentic_sim.telemetry.bpf_work import (
    BPF_EVENT_SCHEMA,
    BPF_EVENT_SCHEMA_LEGACY,
    BPF_EVENT_ABI,
    BPF_EVENT_RECORD_SIZE,
    BPF_EVENT_RECORD_SIZE_LEGACY,
    BPF_PATH_CAP,
    BPF_PERF_BUFFER_PAGES_PER_CPU,
    BPF_PROGRAM,
    BPF_RAW_SCHEMA,
    BPF_SOCKET_SCHEMA,
    BpfAttachError,
    BpfWorkClient,
    BpfWorkCollector,
    BpfWorkService,
    ProcessIdentity,
    ProcessTarget,
    ActionBoundary,
    _AGGREGATE_FIELDS,
    _CActionState,
    _CAggregate,
    _CProcAction,
    _CWorkEvent,
    _CWorkEventLegacy,
    _raw_path,
    _run_bash_action_unmodified,
    _spawn_bash_fixture,
    _stop_bash_fixture,
    _token_for,
    iter_bpf_events,
)


class BpfWorkContractTests(unittest.TestCase):
    def _collector_for_start(self, journal):
        collector = object.__new__(BpfWorkCollector)
        collector._closed = False
        collector._active = {}
        collector._active_loss_baseline = {}
        collector._active_stream_baseline = {}
        collector._perf_lost_total = 0
        collector._perf_callback_errors = []
        collector._raw_event_offset = 0
        collector._ordinal = 0
        collector._event_lock = threading.Condition()
        collector._assert_open = lambda: None
        collector._sync_native_stats = lambda: None
        collector._set_zero_aggregate = lambda _token: None
        collector._set_action = lambda *_args, **_kwargs: None
        collector._set_root_mapping = lambda _token: None
        collector._container_resources = lambda: {"status": "unavailable"}
        collector.boundary_journal = journal
        collector.session_id = "empty-command-test-session"
        return collector

    def _end_collector(
        self,
        *,
        sync_failure: BaseException | None = None,
        journal_failure: BaseException | None = None,
    ):
        collector = object.__new__(BpfWorkCollector)
        collector._closed = False
        collector._active = {"event": 42}
        collector._active_loss_baseline = {"event": (0, 0)}
        collector._active_stream_baseline = {"event": BPF_EVENT_RECORD_SIZE}
        collector._event_seen_by_token = {42: 2}
        collector._events_by_token = {}
        collector._deferred_tokens = {}
        collector._completed = []
        collector._event_lock = threading.Condition()
        collector._perf_lost_total = 0
        collector._perf_callback_errors = []
        collector._defer_event_derivation = True
        collector._raw_event_path = Path("raw_events.bin")
        collector._assert_open = lambda: None
        # This fixture isolates fsync ordering, without a live target process.
        collector._container_resources = lambda: {"status": "unavailable", "scope": "ordering-fixture"}

        class Table:
            def __init__(self):
                self.values = {}

            def __setitem__(self, key, value):
                self.values[int(getattr(key, "value", key))] = value

        collector._tables = {"closed_actions": Table()}
        collector._table = lambda name: collector._tables[name]
        state = _CActionState(root_pid=1, closing=0, in_flight=0, started_ns=1)
        collector._read_action_state = lambda _token: state
        aggregate = {field: 0 for field in _AGGREGATE_FIELDS}
        aggregate["required_event_count"] = 2
        collector._snapshot_aggregate = lambda _token: (dict(aggregate), False)
        collector._drain_perf_events = lambda *_args, **_kwargs: None
        capture_calls = []
        collector._capture_event_boundary = (
            lambda _token, *, fsync=True: (capture_calls.append(fsync) or (800, 2))
        )
        collector._snapshot_paths = lambda _token: []
        collector._events_for_token = lambda _token: []
        collector._count_token_descendants = lambda _token: 0
        cleanup_calls = []
        collector._delete_action_maps = lambda token: (
            cleanup_calls.append(token)
            or {"active": 1, "aggregate": 1, "process": 1, "fd": 0, "pending": 0, "clone": 0}
        )
        raw_rows = []
        collector._write_raw_action = lambda *args, **kwargs: (
            raw_rows.append(kwargs),
            {"boundary": {"event_id": "event"}, "action_token": 42},
        )[1]
        journal_started = threading.Event()
        sync_started = threading.Event()
        sync_finished = threading.Event()
        journal_calls = []
        command = "printf test"
        boundary = ActionBoundary(
            event_id="event",
            command=command,
            command_sha256=hashlib.sha256(command.encode()).hexdigest(),
            start_wall_ns=1,
            start_mono_ns=2,
            end_wall_ns=3,
            end_mono_ns=4,
            status="success",
        )

        def start_sync():
            errors = []

            def sync():
                sync_started.set()
                journal_started.wait(timeout=1)
                if sync_failure is not None:
                    errors.append(sync_failure)
                sync_finished.set()

            thread = threading.Thread(target=sync, daemon=True)
            thread.start()
            return thread, errors

        collector._start_raw_event_sync = start_sync

        class Journal:
            def end(self, *_args, **_kwargs):
                self.assert_sync_started = sync_started.wait(timeout=1)
                journal_calls.append("end")
                self.assert_sync_pending = not sync_finished.is_set()
                journal_started.set()
                if journal_failure is not None:
                    raise journal_failure
                return boundary

        collector.boundary_journal = Journal()
        collector._test_state = {
            "capture_calls": capture_calls,
            "cleanup_calls": cleanup_calls,
            "journal_calls": journal_calls,
            "raw_rows": raw_rows,
            "sync_started": sync_started,
            "sync_finished": sync_finished,
        }
        return collector

    def test_program_requires_individual_perf_records_and_lineage(self) -> None:
        self.assertIn("BPF_PERF_OUTPUT(work_events)", BPF_PROGRAM)
        # v3 work events are 400 bytes; keeping the reusable payload in a
        # per-CPU map avoids exceeding the 512-byte tracepoint stack limit.
        self.assertIn("BPF_PERCPU_ARRAY(work_event_scratch, struct work_event, 1)", BPF_PROGRAM)
        self.assertNotIn("struct work_event event = {}", BPF_PROGRAM)
        self.assertGreaterEqual(BPF_PROGRAM.count("work_event_scratch.lookup(&scratch_key)"), 2)
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
        self.assertIn("raw_args[6]", BPF_PROGRAM)
        self.assertIn("assignment.linux-bpf-work-scalar-args.v1", BPF_EVENT_ABI)
        self.assertNotIn("TRACEPOINT_PROBE(syscalls, sys_enter_read)", BPF_PROGRAM)

    def test_python_bcc_fallback_records_actual_burst_buffer_capacity(self) -> None:
        class FakeTable:
            def __init__(self):
                self.bpf = SimpleNamespace(perf_buffers={11: object(), 12: object()})
                self.called = None

            def open_perf_buffer(self, callback, *, page_cnt, lost_cb):
                self.called = (callback, page_cnt, lost_cb)

        with tempfile.TemporaryDirectory() as directory:
            collector = object.__new__(BpfWorkCollector)
            collector._raw_event_path = Path(directory) / "raw_events.bin"
            collector._event_table = FakeTable()
            collector._open_python_perf_buffer()
            try:
                self.assertEqual(collector._event_table.called[1], BPF_PERF_BUFFER_PAGES_PER_CPU)
                self.assertEqual(BPF_PERF_BUFFER_PAGES_PER_CPU, 512)
                descriptor = collector._perf_buffer_descriptor
                self.assertEqual(descriptor["implementation"], "python_bcc_callback")
                self.assertEqual(descriptor["perf_pages_per_cpu_actual"], 512)
                self.assertEqual(descriptor["perf_cpu_count"], 2)
                self.assertEqual(
                    descriptor["perf_data_bytes_per_cpu"],
                    512 * descriptor["perf_page_size_bytes"],
                )
                self.assertEqual(
                    descriptor["perf_mmap_bytes_total"],
                    2 * 513 * descriptor["perf_page_size_bytes"],
                )
            finally:
                collector._raw_event_stream.close()

    def test_ctypes_records_match_declared_kernel_layout(self) -> None:
        # The event keeps two fixed path payloads so rename source/destination
        # evidence cannot be conflated.  The aggregate appends kind counters
        # and latency sums without changing earlier field offsets.
        self.assertEqual(ct.sizeof(_CWorkEvent), BPF_EVENT_RECORD_SIZE)
        self.assertEqual(ct.sizeof(_CWorkEventLegacy), BPF_EVENT_RECORD_SIZE_LEGACY)
        self.assertEqual(BPF_EVENT_RECORD_SIZE, 400)
        self.assertEqual(BPF_EVENT_RECORD_SIZE_LEGACY, 352)
        self.assertEqual(_CWorkEvent.path.offset, 84)
        self.assertEqual(ct.sizeof(_CProcAction), 24)
        self.assertEqual(ct.sizeof(_CAggregate), 328)
        self.assertEqual(dict(_CWorkEvent._fields_)["path2"]._length_, BPF_PATH_CAP)
        self.assertEqual(dict(_CWorkEvent._fields_)["raw_args"]._length_, 6)

    def test_raw_path_preserves_bytes_and_explicit_empty(self) -> None:
        value = SimpleNamespace(path=(ct.c_char * BPF_PATH_CAP)(*b"/tmp/a"), path_len=6)
        path, encoded = _raw_path(value, 6)
        self.assertEqual(path, "/tmp/a")
        self.assertEqual(encoded, b"/tmp/a".hex())
        empty = SimpleNamespace(path=(ct.c_char * BPF_PATH_CAP)(), path_len=0)
        self.assertEqual(_raw_path(empty, 0), (None, None))

    def test_start_action_preserves_empty_reset_command_and_hash(self) -> None:
        calls = []

        class Journal:
            def start(self, event_id, command, **kwargs):
                calls.append((event_id, command, kwargs))
                return SimpleNamespace(event_id=event_id, command=command)

        collector = self._collector_for_start(Journal())
        boundary = collector.start_action("reset-empty", "", start_wall_ns=1, start_mono_ns=2)

        self.assertEqual(boundary.command, "")
        self.assertEqual(calls[0][1], "")
        self.assertEqual(calls[0][2]["start_mono_ns"], 2)
        self.assertEqual(
            collector._active["reset-empty"],
            _token_for(
                collector.session_id,
                "reset-empty",
                hashlib.sha256(b"").hexdigest(),
                1,
            ),
        )

    def test_start_action_rejects_missing_non_text_and_nul_commands(self) -> None:
        class Journal:
            def start(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("invalid command reached the boundary journal")

        for command in (None, 123, "reset\x00command"):
            journal = Journal()
            collector = self._collector_for_start(journal)
            with self.assertRaises(BpfAttachError):
                collector.start_action("invalid", command, start_wall_ns=1, start_mono_ns=2)
            self.assertFalse(getattr(journal, "called", False))

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
                value.raw_args[0] = 4
                value.raw_args[2] = 4
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
        packet = collector._event_table.event(None)
        collector._on_perf_event(0, ct.addressof(packet), ct.sizeof(packet))
        collector._on_perf_lost(3)
        rows = collector._events_for_token(123)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], BPF_EVENT_SCHEMA)
        self.assertEqual(rows[0]["kind_name"], "read")
        self.assertEqual(rows[0]["duration_ns"], 30)
        self.assertEqual(rows[0]["path"], "/tmp/a")
        self.assertEqual(rows[0]["path2"], "/tmp/b")
        self.assertEqual(rows[0]["event_abi"], BPF_EVENT_ABI)
        self.assertEqual(rows[0]["scalar_args"]["syscall_name"], "read")
        self.assertEqual(rows[0]["scalar_args"]["fd"], 4)
        self.assertEqual(collector._perf_lost_total, 3)
        self.assertEqual(collector._perf_callback_errors, [])

    def test_binary_records_filter_interleaved_tokens_and_reject_truncation(self) -> None:
        packets = []
        for token in (10, 11, 10):
            event = _CWorkEvent()
            event.token = token
            event.kind = 1
            event.status = 1
            event.kernel_start_ns = 100
            event.kernel_end_ns = 110
            packets.append(bytes(event))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw_events.bin"
            path.write_bytes(b"".join(packets))
            self.assertEqual(len(list(iter_bpf_events(path, token=10))), 2)
            self.assertEqual(len(list(iter_bpf_events(path, offset_start=BPF_EVENT_RECORD_SIZE, token=10))), 1)
            with self.assertRaises(ValueError):
                list(iter_bpf_events(path, offset_start=1))
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                list(iter_bpf_events(path))

    def test_legacy_binary_decoder_preserves_v2_and_marks_scalar_args_unavailable(self) -> None:
        event = _CWorkEventLegacy()
        event.token = 19
        event.kind = 1
        event.status = 1
        event.syscall_nr = 0
        event.kernel_start_ns = 100
        event.kernel_end_ns = 110
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "historical-v2.bin"
            path.write_bytes(bytes(event))
            rows = list(
                iter_bpf_events(
                    path,
                    schema_version=BPF_EVENT_SCHEMA_LEGACY,
                    record_size_bytes=BPF_EVENT_RECORD_SIZE_LEGACY,
                )
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], BPF_EVENT_SCHEMA_LEGACY)
        self.assertIsNone(rows[0]["event_abi"])
        self.assertIsNone(rows[0]["raw_scalar_args"])
        self.assertEqual(rows[0]["scalar_args"]["status"], "unavailable")

    def test_scalar_projection_covers_acquisition_critical_arguments(self) -> None:
        cases = [
            (0, [3, 0x1000, 4096, 0, 0, 0], {"fd": 3, "requested_size": 4096}),
            (17, [4, 0x1000, 128, 8192, 0, 0], {"fd": 4, "requested_size": 128, "offset": 8192}),
            (9, [0, 0x2000, 3, 5, 5, 4096], {"length": 0x2000, "prot": 3, "flags": 5, "fd": 5, "offset": 4096}),
            (76, [0x1000, 777, 0, 0, 0, 0], {"length": 777}),
            (257, [7, 0x1000, 0x42, 0, 0, 0], {"dirfd": 7, "open_flags": 0x42}),
            (316, [8, 0x1000, 9, 0x2000, 0x200, 0], {"old_dirfd": 8, "new_dirfd": 9, "flags": 0x200}),
        ]
        for syscall_nr, raw_args, expected in cases:
            event = _CWorkEvent()
            event.token = syscall_nr + 1
            event.kind = 1
            event.status = 1
            event.syscall_nr = syscall_nr
            event.kernel_start_ns = 100
            event.kernel_end_ns = 110
            for index, value in enumerate(raw_args):
                event.raw_args[index] = value
            row = BpfWorkCollector._event_row(bytes(event))
            self.assertEqual(row["event_abi"], BPF_EVENT_ABI)
            self.assertEqual(row["raw_scalar_args"], raw_args)
            for field, value in expected.items():
                self.assertEqual(row["scalar_args"][field], value, (syscall_nr, field))

    def test_process_exit_preserves_censor_without_duration_or_return(self) -> None:
        event = _CWorkEvent()
        event.kind = 1
        event.status = 3
        event.kernel_start_ns = 100
        event.kernel_end_ns = 150
        row = BpfWorkCollector._event_row(bytes(event))
        self.assertEqual(row["censor_boundary_ns"], 150)
        self.assertIsNone(row["kernel_end_ns"])
        self.assertIsNone(row["duration_ns"])
        self.assertIsNone(row["ret"])

    def test_acknowledged_count_does_not_wait_for_idle_poll_generation(self) -> None:
        collector = object.__new__(BpfWorkCollector)
        collector.bpf = object()
        collector._event_condition = threading.Condition()
        collector._perf_generation = 1
        collector._event_seen_by_token = {42: 7}
        collector._events_by_token = {}
        collector._drain_perf_events(1.0, token=42, expected=7)

    def test_end_overlaps_journal_fsync_and_joins_before_raw_commit(self) -> None:
        collector = self._end_collector()

        boundary = collector.end_action(
            "event", status="success", end_wall_ns=3, end_mono_ns=4
        )

        state = collector._test_state
        self.assertEqual(boundary.event_id, "event")
        self.assertEqual(state["capture_calls"], [False])
        self.assertEqual(state["journal_calls"], ["end"])
        self.assertTrue(collector.boundary_journal.assert_sync_started)
        self.assertTrue(collector.boundary_journal.assert_sync_pending)
        self.assertTrue(state["sync_finished"].is_set())
        self.assertEqual(len(state["raw_rows"]), 1)
        stream = state["raw_rows"][0]["event_stream"]
        self.assertEqual(stream["offset_start"], BPF_EVENT_RECORD_SIZE)
        self.assertEqual(stream["offset_end"], BPF_EVENT_RECORD_SIZE * 2)
        self.assertEqual(stream["record_count"], 2)
        self.assertTrue(stream["durable_at_boundary"])

    def test_failed_raw_sync_skips_raw_commit_and_cleans_action(self) -> None:
        collector = self._end_collector(sync_failure=OSError("fsync failed"))

        with self.assertRaises(BpfAttachError):
            collector.end_action(
                "event", status="success", end_wall_ns=3, end_mono_ns=4
            )

        state = collector._test_state
        self.assertTrue(collector.boundary_journal.assert_sync_started)
        self.assertTrue(state["sync_finished"].is_set())
        self.assertEqual(state["raw_rows"], [])
        self.assertEqual(state["cleanup_calls"], [42])
        self.assertNotIn("event", collector._active)
        self.assertNotIn("event", collector._active_stream_baseline)
        self.assertNotIn(42, collector._event_seen_by_token)

    def test_failed_boundary_journal_still_joins_raw_sync_and_cleans_action(self) -> None:
        collector = self._end_collector(journal_failure=OSError("journal fsync failed"))

        with self.assertRaises(OSError):
            collector.end_action(
                "event", status="success", end_wall_ns=3, end_mono_ns=4
            )

        state = collector._test_state
        self.assertTrue(collector.boundary_journal.assert_sync_started)
        self.assertTrue(state["sync_finished"].is_set())
        self.assertEqual(state["raw_rows"], [])
        self.assertEqual(state["cleanup_calls"], [42])
        self.assertNotIn("event", collector._active)

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
