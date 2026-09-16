"""Contract tests for the native BCC perf-record sink."""

from __future__ import annotations

import ctypes as ct
import errno
import struct
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock
from pathlib import Path

from agentic_sim.telemetry.native_bpf_sink import (
    DEFAULT_PERF_BUFFER_PAGES_PER_CPU,
    NATIVE_RECORD_SIZE_BYTES,
    PERF_WAKEUP_EVENTS,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src/agentic_sim/telemetry/native_bpf_sink.c"
RECORD_SIZE = NATIVE_RECORD_SIZE_BYTES


class SinkStats(ct.Structure):
    _fields_ = [
        ("offset_bytes", ct.c_uint64),
        ("total_records", ct.c_uint64),
        ("token_records", ct.c_uint64),
        ("lost", ct.c_uint64),
        ("errors", ct.c_uint64),
    ]


def packet(token: int, marker: int) -> bytes:
    body = bytes((marker + index) % 256 for index in range(RECORD_SIZE - 8))
    return struct.pack("<Q", token) + body


class NativeBpfSinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_dir = tempfile.TemporaryDirectory(prefix="native-bpf-sink-build-")
        cls.library_path = Path(cls.build_dir.name) / "native_bpf_sink.so"
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-shared",
                "-fPIC",
                "-pthread",
                "-O2",
                str(SOURCE),
                "-o",
                str(cls.library_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        cls.library = ct.CDLL(str(cls.library_path), use_errno=True)
        cls.library.sink_open.argtypes = [ct.c_char_p]
        cls.library.sink_open.restype = ct.c_void_p
        cls.library.sink_event.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_int]
        cls.library.sink_event.restype = None
        cls.library.sink_lost.argtypes = [ct.c_void_p, ct.c_uint64]
        cls.library.sink_lost.restype = None
        cls.library.sink_stats.argtypes = [ct.c_void_p, ct.c_uint64, ct.POINTER(SinkStats)]
        cls.library.sink_stats.restype = ct.c_int
        cls.library.sink_flush.argtypes = [ct.c_void_p, ct.c_int]
        cls.library.sink_flush.restype = ct.c_int
        cls.library.sink_boundary.argtypes = [ct.c_void_p, ct.c_uint64, ct.c_int, ct.POINTER(SinkStats)]
        cls.library.sink_boundary.restype = ct.c_int
        cls.library.sink_close.argtypes = [ct.c_void_p]
        cls.library.sink_close.restype = ct.c_int
        cls.library.sink_perf_event_open.argtypes = [ct.c_int, ct.c_uint]
        cls.library.sink_perf_event_open.restype = ct.c_int
        cls.library.sink_perf_event_enable.argtypes = [ct.c_int]
        cls.library.sink_perf_event_enable.restype = ct.c_int

    @classmethod
    def tearDownClass(cls):
        cls.build_dir.cleanup()

    @classmethod
    def open_sink(cls, path: Path):
        handle = cls.library.sink_open(str(path).encode("utf-8"))
        if not handle:
            raise OSError(ct.get_errno(), "sink_open failed")
        return handle

    @classmethod
    def send_event(cls, handle, payload: bytes) -> None:
        buffer = (ct.c_ubyte * len(payload)).from_buffer_copy(payload)
        cls.library.sink_event(handle, ct.cast(buffer, ct.c_void_p), len(payload))

    @classmethod
    def read_stats(cls, handle, token: int = 0) -> SinkStats:
        result = SinkStats()
        if cls.library.sink_stats(handle, token, ct.byref(result)) != 0:
            raise AssertionError("sink_stats returned failure")
        return result

    @classmethod
    def read_boundary(cls, handle, token: int = 0, fsync: bool = True) -> SinkStats:
        result = SinkStats()
        if cls.library.sink_boundary(handle, token, int(fsync), ct.byref(result)) != 0:
            raise AssertionError("sink_boundary returned failure")
        return result

    @classmethod
    def close_sink(cls, handle) -> None:
        if cls.library.sink_close(handle) != 0:
            raise AssertionError("sink_close returned failure")

    def test_perf_helpers_reject_invalid_parameters(self):
        self.assertEqual(self.library.sink_perf_event_enable(-1), -1)
        self.assertEqual(ct.get_errno(), errno.EINVAL)
        # CPU -2 is outside the perf_event_open sentinel range. The wakeup
        # The named wrapper constant is forwarded to the native helper even
        # on this unprivileged local test host.
        self.assertEqual(self.library.sink_perf_event_open(-2, PERF_WAKEUP_EVENTS), -1)
        self.assertIn(ct.get_errno(), {errno.EINVAL, errno.EPERM, errno.EACCES})

    def test_default_perf_buffer_capacity_is_burst_sized(self):
        self.assertEqual(DEFAULT_PERF_BUFFER_PAGES_PER_CPU, 512)

    def test_open_refuses_existing_file_and_symlink_without_truncating_target(self):
        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-exclusive-") as temporary:
            root = Path(temporary)
            existing = root / "existing.bin"
            existing.write_bytes(b"keep this raw stream")
            self.assertFalse(self.library.sink_open(str(existing).encode("utf-8")))
            self.assertEqual(ct.get_errno(), errno.EEXIST)
            self.assertEqual(existing.read_bytes(), b"keep this raw stream")

            target = root / "target.bin"
            target.write_bytes(b"keep symlink target")
            link = root / "link.bin"
            link.symlink_to(target)
            self.assertFalse(self.library.sink_open(str(link).encode("utf-8")))
            self.assertEqual(ct.get_errno(), errno.EEXIST)
            self.assertEqual(target.read_bytes(), b"keep symlink target")

    def test_wrapper_binds_bcc_context_as_native_void_pointer(self):
        from agentic_sim.telemetry import native_bpf_sink as wrapper

        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-wrapper-") as temporary:
            path = Path(temporary) / "raw_events.bin"
            sink = wrapper.NativeBpfSink(path)
            native_handle = sink.handle
            self.assertEqual(sink.descriptor["record_size_bytes"], RECORD_SIZE)
            self.assertEqual(
                sink.descriptor["event_schema_version"],
                "assignment.linux-bpf-work-event.v3",
            )
            calls = []
            perf_open_calls = []
            signatures = []

            class FakeLib:
                @staticmethod
                def perf_reader_fd(reader):
                    return 123

            fake_lib = FakeLib()

            def fake_open_reader(callback, lost_callback, context, page_count):
                calls.append((callback, lost_callback, context, page_count))
                return 456

            def fake_set_fd(reader, fd):
                calls.append(("set_fd", reader, fd))

            def fake_mmap(reader):
                calls.append(("mmap", reader))
                return 0

            def fake_free(reader):
                calls.append(("free", reader))

            def fake_perf_event_open(cpu, wakeup_events):
                perf_open_calls.append((cpu, wakeup_events))
                return 789

            def fake_cfunctype(*signature):
                signatures.append(signature)
                if signature[0] is ct.c_void_p:
                    return lambda _symbol: fake_open_reader
                if signature[0] is None and len(signature) == 3:
                    return lambda _symbol: fake_set_fd
                if signature[0] is ct.c_int:
                    return lambda _symbol: fake_mmap
                return lambda _symbol: fake_free

            class FakeTable:
                def __init__(self):
                    self.bpf = types.SimpleNamespace(perf_buffers={})
                    self._cbs = {}
                    self._open_key_fds = {}
                    self.values = {}

                @staticmethod
                def Key(cpu):
                    return cpu

                @staticmethod
                def Leaf(fd):
                    return fd

                def __setitem__(self, key, value):
                    self.values[key] = value

            fake_bcc = types.ModuleType("bcc")
            fake_bcc.lib = fake_lib
            fake_bcc_table = types.ModuleType("bcc.table")
            fake_bcc_table._RAW_CB_TYPE = ct.CFUNCTYPE(None, ct.c_void_p, ct.c_void_p, ct.c_int)
            fake_bcc_table._LOST_CB_TYPE = ct.CFUNCTYPE(None, ct.c_void_p, ct.c_uint64)
            fake_bcc_utils = types.ModuleType("bcc.utils")
            fake_bcc_utils.get_online_cpus = lambda: [2]
            try:
                with mock.patch.dict(
                    sys.modules,
                    {"bcc": fake_bcc, "bcc.table": fake_bcc_table, "bcc.utils": fake_bcc_utils},
                ), mock.patch.object(wrapper.ct, "CFUNCTYPE", side_effect=fake_cfunctype):
                    with mock.patch.object(sink.lib, "sink_perf_event_open", side_effect=fake_perf_event_open), mock.patch.object(sink.lib, "sink_perf_event_enable", return_value=0):
                        table = FakeTable()
                        sink.open_perf_buffers(table, page_count=64)
            finally:
                sink.close()

            self.assertEqual(len(signatures), 4)
            self.assertEqual(signatures[0][3], ct.c_void_p)
            self.assertEqual(calls[0][2], native_handle)
            self.assertEqual(calls[0][3], 64)
            self.assertEqual(perf_open_calls, [(2, wrapper.PERF_WAKEUP_EVENTS)])
            self.assertEqual(table.bpf.perf_buffers[(id(table), 2)], 456)
            self.assertEqual(sink.descriptor["perf_pages_per_cpu"], 64)
            self.assertEqual(sink.descriptor["perf_pages_per_cpu_actual"], 64)
            self.assertEqual(sink.descriptor["perf_cpu_count"], 1)
            self.assertEqual(
                sink.descriptor["perf_data_bytes_per_cpu"],
                64 * sink.descriptor["perf_page_size_bytes"],
            )
            self.assertEqual(
                sink.descriptor["perf_mmap_bytes_total"],
                65 * sink.descriptor["perf_page_size_bytes"],
            )

    def test_interleaved_tokens_ignore_perf_padding_and_preserve_packet_bytes(self):
        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-raw-") as temporary:
            path = Path(temporary) / "raw_events.bin"
            handle = self.open_sink(path)
            records = [
                packet(11, 0x10),
                packet(22, 0x20),
                packet(11, 0x30),
                packet(33, 0x40),
                packet(22, 0x50),
            ]
            try:
                for index, record in enumerate(records):
                    self.send_event(handle, record + bytes([0xA5]) * (index % 8))
                boundary = self.read_boundary(handle, token=11, fsync=True)
                self.assertEqual(boundary.offset_bytes, RECORD_SIZE * len(records))
                self.assertEqual(boundary.total_records, len(records))
                self.assertEqual(boundary.token_records, 2)
                self.assertEqual(boundary.lost, 0)
                self.assertEqual(boundary.errors, 0)
                self.assertEqual(self.read_stats(handle, token=22).token_records, 2)
                self.assertEqual(self.read_stats(handle, token=0).token_records, 0)
            finally:
                self.close_sink(handle)
            self.assertEqual(path.read_bytes(), b"".join(records))

    def test_truncated_records_and_perf_loss_are_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-errors-") as temporary:
            path = Path(temporary) / "raw_events.bin"
            handle = self.open_sink(path)
            valid = packet(7, 0x71)
            try:
                self.send_event(handle, valid)
                self.send_event(handle, valid[:-1])
                self.send_event(handle, valid + b"padding8")
                self.library.sink_lost(handle, 7)
                self.assertEqual(self.library.sink_flush(handle, 0), 0)
                before_boundary = self.read_stats(handle, token=7)
                self.assertEqual(before_boundary.offset_bytes, RECORD_SIZE)
                self.assertEqual(before_boundary.total_records, 1)
                self.assertEqual(before_boundary.token_records, 1)
                self.assertEqual(before_boundary.lost, 7)
                self.assertGreaterEqual(before_boundary.errors, 2)
                boundary = self.read_boundary(handle, token=0, fsync=True)
                self.assertEqual(boundary.offset_bytes, RECORD_SIZE)
                self.assertEqual(boundary.total_records, 1)
                self.assertEqual(boundary.token_records, 0)
                self.assertEqual(boundary.lost, 7)
                self.assertGreaterEqual(boundary.errors, 2)
            finally:
                self.close_sink(handle)
            self.assertEqual(path.read_bytes(), valid)

    def test_periodic_flush_occurs_at_the_declared_one_mib_record_boundary(self):
        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-periodic-") as temporary:
            path = Path(temporary) / "raw_events.bin"
            handle = self.open_sink(path)
            record_count = (1024 * 1024) // RECORD_SIZE
            record = packet(91, 0x91)
            try:
                for _ in range(record_count):
                    self.send_event(handle, record)
                # The native sink explicitly flushes every floor(1 MiB/400)
                # complete records. The remaining 576 bytes stay buffered
                # until the boundary/close flush.
                self.assertEqual(path.stat().st_size, record_count * RECORD_SIZE)
                stats = self.read_stats(handle, token=91)
                self.assertEqual(stats.offset_bytes, record_count * RECORD_SIZE)
                self.assertEqual(stats.total_records, record_count)
                self.assertEqual(stats.token_records, record_count)
                self.assertEqual(stats.errors, 0)
            finally:
                self.close_sink(handle)
            self.assertEqual(path.stat().st_size, record_count * RECORD_SIZE)
            self.assertEqual(path.read_bytes(), record * record_count)

    def test_callbacks_stats_flush_and_boundaries_are_thread_safe(self):
        with tempfile.TemporaryDirectory(prefix="native-bpf-sink-threaded-") as temporary:
            path = Path(temporary) / "raw_events.bin"
            handle = self.open_sink(path)
            thread_count = 6
            records_per_thread = 300
            expected: list[bytes] = []
            expected_lock = threading.Lock()
            observer_done = threading.Event()
            failures: list[str] = []

            def produce(thread_index: int) -> None:
                token = thread_index + 1
                local = []
                for sequence in range(records_per_thread):
                    record_bytes = bytearray(packet(token, sequence % 256))
                    record_bytes[8:16] = struct.pack("<Q", sequence)
                    record = bytes(record_bytes)
                    local.append(record)
                    self.send_event(handle, record + bytes([0x5A]) * (sequence % 8))
                with expected_lock:
                    expected.extend(local)

            def observe() -> None:
                try:
                    while not observer_done.is_set():
                        stats = SinkStats()
                        if self.library.sink_stats(handle, 0, ct.byref(stats)) != 0:
                            failures.append("sink_stats returned failure")
                            return
                        if stats.token_records != 0 or stats.offset_bytes != stats.total_records * RECORD_SIZE:
                            failures.append("inconsistent stats snapshot")
                            return
                        boundary = SinkStats()
                        if self.library.sink_boundary(handle, 1, 0, ct.byref(boundary)) != 0:
                            failures.append("sink_boundary returned failure")
                            return
                        if boundary.offset_bytes != boundary.total_records * RECORD_SIZE:
                            failures.append("inconsistent boundary snapshot")
                            return
                        if self.library.sink_flush(handle, 0) != 0:
                            failures.append("sink_flush returned failure")
                            return
                except Exception as exc:  # pragma: no cover - diagnostic path
                    failures.append(str(exc))

            observer = threading.Thread(target=observe, name="native-sink-observer")
            producers = [threading.Thread(target=produce, args=(index,)) for index in range(thread_count)]
            observer.start()
            for producer_thread in producers:
                producer_thread.start()
            for producer_thread in producers:
                producer_thread.join()
            observer_done.set()
            observer.join(timeout=5)
            self.assertFalse(observer.is_alive())
            self.assertEqual(failures, [])

            try:
                final = self.read_boundary(handle, token=1, fsync=True)
                expected_count = thread_count * records_per_thread
                self.assertEqual(final.offset_bytes, expected_count * RECORD_SIZE)
                self.assertEqual(final.total_records, expected_count)
                self.assertEqual(final.token_records, records_per_thread)
                self.assertEqual(final.lost, 0)
                self.assertEqual(final.errors, 0)
                for token in range(1, thread_count + 1):
                    self.assertEqual(self.read_stats(handle, token=token).token_records, records_per_thread)
                self.assertEqual(self.read_stats(handle, token=0).token_records, 0)
            finally:
                self.close_sink(handle)

            raw = path.read_bytes()
            with expected_lock:
                expected_set = set(expected)
                expected_count = len(expected)
            self.assertEqual(len(raw), expected_count * RECORD_SIZE)
            chunks = [raw[offset : offset + RECORD_SIZE] for offset in range(0, len(raw), RECORD_SIZE)]
            self.assertEqual(len(set(chunks)), expected_count)
            self.assertTrue(all(chunk in expected_set for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
