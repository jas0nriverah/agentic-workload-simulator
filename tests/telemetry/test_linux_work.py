"""Focused contract tests for identity-bound Linux work collection."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentic_sim.telemetry.linux_work import (
    ActionBoundary,
    BoundaryJournal,
    CollectorAttachError,
    IdentityBindingError,
    LinuxWorkError,
    LinuxWorkCollector,
    ProcessIdentity,
    ProcessTarget,
    _aggregate_syscalls,
    _append_jsonl,
    _assert_pid_binding,
    _capture_pid_binding,
    _proc_delta,
    _parse_proc_stat_text,
    measure_probe_overhead,
    parse_strace_file,
    parse_strace_lines,
    stop_session,
    summarize_trace,
)


class DurableAppendTests(unittest.TestCase):
    def test_short_writes_preserve_complete_record(self):
        real_write = os.write
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.jsonl"
            with mock.patch(
                "agentic_sim.telemetry.linux_work.os.write",
                side_effect=lambda fd, data: real_write(fd, data[:7]),
            ):
                _append_jsonl(path, {"event_id": "a", "command": "printf café"})
                _append_jsonl(path, {"event_id": "b"})
            self.assertEqual(
                [json.loads(line) for line in path.read_text().splitlines()],
                [{"event_id": "a", "command": "printf café"}, {"event_id": "b"}],
            )

    def test_zero_write_fails_without_claiming_durability(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("agentic_sim.telemetry.linux_work.os.write", return_value=0), mock.patch(
                "agentic_sim.telemetry.linux_work.os.fsync"
            ) as sync:
                with self.assertRaisesRegex(OSError, "no progress"):
                    _append_jsonl(Path(directory) / "journal.jsonl", {"event_id": "a"})
                sync.assert_not_called()


def _identity(pid: int = 321) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        start_ticks=9876,
        boot_id="boot-test",
        pid_namespace_inode=54321,
        run_id="run-test",
        attempt_id="attempt-test",
        case_id="case-test",
        instance_id="instance-test",
        container_pid=7,
        pid_namespace="ns-test",
        mapping_source="unit-test-explicit",
    )


class LinuxWorkParserTests(unittest.TestCase):
    def test_parser_aggregates_fd_classes_messages_and_two_sided_copy(self) -> None:
        lines = """\
100.000000 openat(AT_FDCWD</work>, "/work/a", O_RDONLY) = 3</work/a> <0.000100>
100.001000 read(3</work/a>, "abc", 3) = 3 <0.000010>
100.002000 write(4<pipe:[1]>, "xy", 2) = 2 <0.000005>
100.003000 send(5<socket:[2]>, "xyz", 3, 0) = 3 <0.000005>
100.004000 getdents64(6</work>, /* 1 entries */, 10) = 10 <0.000005>
100.005000 newfstatat(AT_FDCWD</work>, "/work/a", {}, 0) = 0 <0.000005>
100.006000 clone(child_stack=NULL, flags=CLONE_THREAD|SIGCHLD) = 123 <0.000005>
100.007000 fork() = 124 <0.000005>
100.008000 execve("/bin/true", [], []) = 0 <0.000005>
100.009000 recvmmsg(5<socket:[2]>, [], 1, 0, NULL) = 2 <0.000005>
100.010000 sendmmsg(5<socket:[2]>, [], 1, 0) = 3 <0.000005>
100.011000 splice(3</work/a>, NULL, 4<pipe:[1]>, NULL, 12, 0) = 4 <0.000005>
100.012000 copy_file_range(3</work/a>, NULL, 4</work/b>, NULL, 12, 0) = 5 <0.000005>
100.013000 stat("/work/no", {}) = -1 ENOENT (No such file or directory) <0.000005>
100.014000 read(3</work/a>, "x", 1) = ? ERESTARTSYS (To be restarted) <0.000005>
100.015000 read(3</work/a>, "x", 1 <unfinished ...>
100.016000 <... read resumed> "x", 1) = 1 <0.000010>
""".splitlines(True)
        parsed = parse_strace_lines(lines, default_pid=321, source_file="strace.321")
        self.assertEqual(parsed.diagnostics, [])
        self.assertEqual(len(parsed.records), 16)
        aggregate = _aggregate_syscalls(parsed.records)

        self.assertEqual(aggregate["open_count"], 1)
        self.assertEqual(aggregate["stat_count"], 2)
        self.assertEqual(aggregate["getdents_bytes"], 10)
        self.assertEqual(aggregate["fork_count"], 1)
        self.assertEqual(aggregate["thread_count"], 1)
        self.assertEqual(aggregate["exec_count"], 1)
        self.assertEqual(aggregate["received_message_count"], 2)
        self.assertEqual(aggregate["sent_message_count"], 3)
        self.assertEqual(aggregate["pipe_write_bytes"], 6)
        self.assertEqual(aggregate["socket_write_bytes"], 3)
        self.assertEqual(aggregate["path_backed_read_bytes"], 13)
        self.assertEqual(aggregate["path_backed_write_bytes"], 5)
        self.assertIsNone(aggregate["regular_file_read_bytes"])
        self.assertIsNone(aggregate["regular_file_write_bytes"])
        self.assertIn("/work/a", aggregate["distinct_observed_paths"])
        self.assertIn("/work/b", aggregate["path_backed_paths"])
        self.assertIn("source and destination", aggregate["work_semantics"]["splice_copy_file_range"])
        self.assertEqual(aggregate["timeout_syscall_count"], 1)
        self.assertEqual(aggregate["resumed_syscall_count"], 1)

    def test_unfinished_trace_is_retained_as_unfinished(self) -> None:
        parsed = parse_strace_lines(
            ["200.000000 read(3</work/a>, \"x\", 1 <unfinished ...>\n"],
            default_pid=9,
        )
        self.assertEqual(len(parsed.records), 1)
        self.assertEqual(parsed.records[0].state, "unfinished")
        self.assertEqual(parsed.records[0].name, "read")
        self.assertEqual(_aggregate_syscalls(parsed.records)["unfinished_syscall_count"], 1)

    def test_file_parser_uses_pid_suffix_and_replays_raw_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "strace.321"
            path.write_text("100.000000 getpid() = 321 <0.000001>\n", encoding="utf-8")
            parsed = parse_strace_file(path)
        self.assertEqual(parsed.records[0].pid, 321)
        self.assertEqual(parsed.records[0].return_value, 321)


class LinuxWorkIdentityTests(unittest.TestCase):
    def test_proc_stat_parser_handles_comm_with_closing_parenthesis(self) -> None:
        fields = ["0"] * 20
        fields[0] = "S"
        fields[1] = "2"
        fields[11] = "7"
        fields[12] = "8"
        fields[13] = "9"
        fields[14] = "10"
        fields[17] = "3"
        fields[19] = "1234"
        parsed = _parse_proc_stat_text("321 (worker)with) S " + " ".join(fields[1:]), pid=321)
        self.assertEqual(parsed["comm"], "worker)with")
        self.assertEqual(parsed["ppid"], 2)
        self.assertEqual(parsed["utime_ticks"], 7)
        self.assertEqual(parsed["cstime_ticks"], 10)
        self.assertEqual(parsed["num_threads"], 3)
        self.assertEqual(parsed["start_ticks"], 1234)

    def test_current_identity_rejects_reused_pid(self) -> None:
        identity = _identity()
        with (
            mock.patch(
                "agentic_sim.telemetry.linux_work._read_proc_stat",
                return_value={"start_ticks": identity.start_ticks + 1},
            ),
            mock.patch("agentic_sim.telemetry.linux_work._read_boot_id", return_value=identity.boot_id),
            mock.patch(
                "agentic_sim.telemetry.linux_work._pid_namespace_inode",
                return_value=identity.pid_namespace_inode,
            ),
        ):
            with self.assertRaisesRegex(IdentityBindingError, "reused"):
                identity.assert_current()

    def test_stale_tracer_binding_is_rejected_before_signal(self) -> None:
        binding = _capture_pid_binding(os.getpid())
        binding["start_ticks"] = int(binding["start_ticks"]) + 1
        with self.assertRaisesRegex(IdentityBindingError, "reused"):
            _assert_pid_binding(binding, role="strace")

    def test_manifest_tracer_pid_mismatch_is_rejected_before_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity()
            manifest = {
                "schema_version": "assignment.linux-work-collector.v1",
                "trace_dir": str(root),
                "trace_prefix": str(root / "strace"),
                "boundary_journal": str(root / "action_boundaries.jsonl"),
                "identity": identity.to_mapping(),
                "strace_pid": 123,
                "strace_pid_binding": {
                    "pid": 456,
                    "start_ticks": 1,
                    "boot_id": "boot-test",
                    "pid_namespace_inode": 54321,
                },
            }
            (root / "collector_manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaisesRegex(CollectorAttachError, "does not match"):
                stop_session(root / "collector_manifest.json")

    def test_attach_bad_executable_fails_before_any_trace_claim(self) -> None:
        target = ProcessTarget(
            pid=os.getpid(), run_id="run", attempt_id="attempt", case_id="case"
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CollectorAttachError):
                LinuxWorkCollector.attach(
                    target,
                    Path(temporary) / "collector",
                    strace_path="/definitely/not-an-executable",
                )

    def test_attach_timeout_reaps_owned_tracer_and_closes_stderr(self) -> None:
        class FakeProcess:
            pid = 456

            def __init__(self) -> None:
                self.stderr = io.StringIO("permission denied")
                self.returncode = None
                self.wait_calls = 0

            def poll(self):
                return None

            def send_signal(self, _signal):
                self.returncode = -2

            def wait(self, timeout=None):
                self.wait_calls += 1
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        fake = FakeProcess()
        target = ProcessTarget(
            pid=os.getpid(), run_id="run", attempt_id="attempt", case_id="case"
        )
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch(
                    "agentic_sim.telemetry.linux_work.ProcessIdentity.capture",
                    return_value=_identity(pid=os.getpid()),
                ),
                mock.patch(
                    "agentic_sim.telemetry.linux_work.subprocess.Popen",
                    return_value=fake,
                ),
                mock.patch(
                    "agentic_sim.telemetry.linux_work.ProcessIdentity.assert_current",
                    return_value={},
                ),
                mock.patch(
                    "agentic_sim.telemetry.linux_work._read_tracer_pid",
                    return_value=None,
                ),
            ):
                with self.assertRaises(CollectorAttachError):
                    LinuxWorkCollector.attach(
                        target,
                        Path(temporary) / "collector",
                        attach_timeout_s=0.001,
                        strace_path=sys.executable,
                    )
        self.assertTrue(fake.stderr.closed)
        self.assertGreaterEqual(fake.wait_calls, 1)


class LinuxWorkBoundaryTests(unittest.TestCase):
    def test_boundary_journal_pairs_durable_start_and_end(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "action_boundaries.jsonl"
            journal = BoundaryJournal(path, identity)
            journal.start("event-1", "cat /work/a", start_wall_ns=100_000_000_000, start_mono_ns=50)
            journal.end("event-1", status="success", end_wall_ns=101_000_000_000, end_mono_ns=60)
            paired = BoundaryJournal(path, identity).boundaries()
            self.assertEqual(len(paired), 1)
            self.assertEqual(paired[0].event_id, "event-1")
            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(payload["command_sha256"], hashlib.sha256(b"cat /work/a").hexdigest())

    def test_boundary_journal_round_trips_empty_reset_command(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "action_boundaries.jsonl"
            journal = BoundaryJournal(path, identity)
            journal.start("reset-empty", "", start_wall_ns=100_000_000_000, start_mono_ns=50)
            journal.end("reset-empty", status="success", end_wall_ns=101_000_000_000, end_mono_ns=60)
            paired = BoundaryJournal(path, identity).boundaries()
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0].command, "")
        self.assertEqual(paired[0].command_sha256, hashlib.sha256(b"").hexdigest())

    def test_action_command_validation_keeps_nul_and_nontext_rejected(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            journal = BoundaryJournal(Path(temporary) / "action_boundaries.jsonl", identity)
            for command in (None, 123, "reset\x00command"):
                with self.assertRaises(LinuxWorkError):
                    journal.start("invalid", command, start_wall_ns=100_000_000_000, start_mono_ns=50)

    def test_summary_keeps_proc_cpu_io_semantics_separate(self) -> None:
        identity = _identity()
        start_proc = {
            "start_ticks": 10,
            "utime_ticks": 2,
            "stime_ticks": 3,
            "cutime_ticks": 4,
            "cstime_ticks": 5,
            "num_threads": 1,
            "io": {"rchar": 100, "wchar": 20, "read_bytes": 7, "write_bytes": 8, "syscr": 2, "syscw": 1},
        }
        end_proc = {
            "start_ticks": 10,
            "utime_ticks": 5,
            "stime_ticks": 4,
            "cutime_ticks": 6,
            "cstime_ticks": 7,
            "num_threads": 2,
            "io": {"rchar": 140, "wchar": 30, "read_bytes": 9, "write_bytes": 11, "syscr": 4, "syscw": 2},
        }
        boundary = ActionBoundary(
            event_id="event-1",
            command="python -c pass",
            command_sha256=hashlib.sha256(b"python -c pass").hexdigest(),
            start_wall_ns=100_000_000_000,
            start_mono_ns=1,
            end_wall_ns=102_000_000_000,
            end_mono_ns=2,
            status="success",
            start_snapshot={"processes": {"321": start_proc}},
            end_snapshot={"processes": {"321": end_proc}},
        )
        parsed = parse_strace_lines(
            ["101.000000 read(3</work/a>, \"x\", 4) = 4 <0.000001>\n"],
            default_pid=321,
        )
        with tempfile.TemporaryDirectory() as temporary:
            raw = Path(temporary) / "strace.321"
            raw.write_text("101.000000 read(3</work/a>, \"x\", 4) = 4 <0.000001>\n", encoding="utf-8")
            summary = summarize_trace(
                parsed.records,
                [boundary],
                identity=identity,
                raw_trace_files=[raw],
            )
        action = summary["actions"][0]
        self.assertEqual(action["work"]["path_backed_read_bytes"], 4)
        self.assertEqual(action["proc"]["totals"]["user_cpu_ticks"], 3)
        self.assertEqual(action["proc"]["io_totals"]["rchar"], 40)
        self.assertEqual(action["proc"]["io_totals"]["read_bytes"], 2)
        self.assertIn("physical storage", action["proc"]["io_semantics"]["read_bytes_write_bytes"])

    def test_proc_totals_are_unavailable_when_a_started_child_exits(self) -> None:
        start = {
            "processes": {
                "321": {"start_ticks": 10, "utime_ticks": 2, "stime_ticks": 3, "io": {"rchar": 10}},
                "322": {"start_ticks": 11, "utime_ticks": 4, "stime_ticks": 5, "io": {"rchar": 20}},
            }
        }
        end = {
            "processes": {
                "321": {"start_ticks": 10, "utime_ticks": 5, "stime_ticks": 4, "io": {"rchar": 15}},
            }
        }
        delta = _proc_delta(start, end)
        self.assertEqual(delta["availability"], "measured")
        self.assertEqual(delta["processes_missing_at_end"], ["322"])
        self.assertIsNone(delta["totals"]["user_cpu_ticks"])
        self.assertIsNone(delta["io_totals"]["rchar"])


@unittest.skipUnless(shutil.which("strace"), "strace is not installed")
class LinuxWorkOverheadTests(unittest.TestCase):
    def test_paired_fixture_is_ready_gated_and_fail_closed_without_ptrace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = measure_probe_overhead(
                [sys.executable, "-c", "import time; time.sleep(0.01)"],
                repeats=1,
                output_dir=Path(temporary),
                timeout_s=5.0,
            )
        self.assertIn(result["status"], {"measured", "unavailable"})
        pair = result["paired"][0]
        self.assertIsNotNone(pair["control_execution_wall_ms"])
        if result["status"] == "unavailable":
            self.assertIsNone(pair["instrumented_wall_ms"])
            self.assertIsNone(pair["setup_delta_ms"])
            self.assertTrue(result["instrumented_attach_failures"])
        else:
            self.assertIsNotNone(pair["instrumented_execution_wall_ms"])
            self.assertIsNotNone(pair["setup_delta_ms"])


if __name__ == "__main__":
    unittest.main()
