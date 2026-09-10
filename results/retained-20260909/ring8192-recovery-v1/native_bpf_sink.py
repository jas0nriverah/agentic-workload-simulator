"""Native buffering for BCC perf records; no Python callback per syscall."""
from __future__ import annotations

import ctypes as ct
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

PERF_WAKEUP_EVENTS = 128
# A v3 work packet is 400 bytes.  The previous 64-page ring (256 KiB of
# data per CPU) was not enough headroom for the largest observed bursts.  The
# collector records the successfully-created page count in its manifest; a
# failure to create this larger ring remains fatal rather than silently
# reducing capture capacity.
DEFAULT_PERF_BUFFER_PAGES_PER_CPU = 8192
# Keep this in sync with the v3 ``struct work_event`` packet in bpf_work.py.
# The sink is intentionally standalone C code so it cannot import the BPF
# module at compile time; the descriptor records the ABI alongside this size.
NATIVE_RECORD_SIZE_BYTES = 400
NATIVE_EVENT_SCHEMA = "assignment.linux-bpf-work-event.v3"
NATIVE_EVENT_ABI = "assignment.linux-bpf-work-scalar-args.v1"


def _system_page_size_bytes() -> int:
    try:
        value = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, ValueError):
        value = 4096
    return value if value > 0 else 4096


def perf_buffer_descriptor(
    *,
    implementation: str,
    page_count: int,
    cpu_count: int | None,
    page_count_source: str,
) -> dict[str, int | str | None]:
    """Describe the perf mmap capacity that was actually opened.

    ``page_count`` is the data-page count passed to BCC's perf reader.  The
    kernel also maps one metadata page per CPU, so both the data and complete
    mmap byte counts are retained for resource/provenance accounting.
    """

    page_count = int(page_count)
    if page_count <= 0:
        raise ValueError("perf buffer page count must be positive")
    page_size = _system_page_size_bytes()
    cpu_count = None if cpu_count is None else int(cpu_count)
    descriptor: dict[str, int | str | None] = {
        "implementation": implementation,
        "perf_pages_per_cpu": page_count,
        "perf_pages_per_cpu_actual": page_count,
        "perf_page_size_bytes": page_size,
        "perf_data_bytes_per_cpu": page_count * page_size,
        "perf_mmap_bytes_per_cpu": (page_count + 1) * page_size,
        "perf_cpu_count": cpu_count,
        "perf_page_count_source": page_count_source,
    }
    descriptor["perf_mmap_bytes_total"] = (
        None if cpu_count is None else (page_count + 1) * page_size * cpu_count
    )
    return descriptor


class SinkStats(ct.Structure):
    _fields_ = [(name, ct.c_uint64) for name in (
        "offset_bytes", "total_records", "token_records", "lost", "errors"
    )]


class NativeBpfSink:
    def __init__(self, path: Path):
        source = Path(__file__).with_suffix(".c")
        library = path.parent / "native_bpf_sink.so"
        snapshot = path.parent / "native_bpf_sink.c"
        if library.exists() or library.is_symlink() or snapshot.exists() or snapshot.is_symlink():
            raise ValueError("refusing to overwrite a native collector library")
        source_bytes = source.read_bytes()
        with snapshot.open("xb") as output:
            output.write(source_bytes)
            output.flush()
            os.fsync(output.fileno())
        compiler = shutil.which("cc")
        if compiler is None:
            raise OSError("C compiler is required for native BPF capture")
        subprocess.run(
            [compiler, "-std=c11", "-O2", "-shared", "-fPIC", "-pthread",
             str(snapshot), "-o", str(library)],
            check=True, capture_output=True, timeout=30,
        )
        with library.open("rb") as compiled:
            os.fsync(compiled.fileno())
        self.descriptor = {
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "source_path": str(snapshot),
            "compiler_path": str(Path(compiler).resolve()),
            "compiler_sha256": hashlib.sha256(Path(compiler).read_bytes()).hexdigest(),
            "compiler_flags": ["-std=c11", "-O2", "-shared", "-fPIC", "-pthread"],
            "library_sha256": hashlib.sha256(library.read_bytes()).hexdigest(),
            "library_path": str(library),
            "record_size_bytes": NATIVE_RECORD_SIZE_BYTES,
            "event_schema_version": NATIVE_EVENT_SCHEMA,
            "event_abi": NATIVE_EVENT_ABI,
        }
        self.lib = ct.CDLL(str(library), use_errno=True)
        self.lib.sink_open.argtypes = [ct.c_char_p]
        self.lib.sink_open.restype = ct.c_void_p
        self.lib.sink_stats.argtypes = [ct.c_void_p, ct.c_uint64, ct.POINTER(SinkStats)]
        self.lib.sink_stats.restype = ct.c_int
        self.lib.sink_flush.argtypes = [ct.c_void_p, ct.c_int]
        self.lib.sink_flush.restype = ct.c_int
        self.lib.sink_boundary.argtypes = [ct.c_void_p, ct.c_uint64, ct.c_int, ct.POINTER(SinkStats)]
        self.lib.sink_boundary.restype = ct.c_int
        self.lib.sink_close.argtypes = [ct.c_void_p]
        self.lib.sink_close.restype = ct.c_int
        self.handle = self.lib.sink_open(str(path).encode())
        if not self.handle:
            raise OSError(ct.get_errno(), "native BPF sink open failed")

    def open_perf_buffers(
        self,
        table,
        *,
        page_count: int = DEFAULT_PERF_BUFFER_PAGES_PER_CPU,
    ):
        from bcc import lib
        from bcc.table import _RAW_CB_TYPE, _LOST_CB_TYPE
        from bcc.utils import get_online_cpus

        callback = ct.cast(self.lib.sink_event, _RAW_CB_TYPE)
        lost_callback = ct.cast(self.lib.sink_lost, _LOST_CB_TYPE)
        # BCC 0.18 hardcodes a wakeup per packet. Build the same reader with
        # batched wakeups; the poller also drains short tails every 5ms.
        # Use void* ctx: BCC's Python binding instead takes a Python object.
        new_reader = ct.CFUNCTYPE(
            ct.c_void_p, _RAW_CB_TYPE, _LOST_CB_TYPE, ct.c_void_p, ct.c_int,
        )(("perf_reader_new", lib))
        set_fd = ct.CFUNCTYPE(None, ct.c_void_p, ct.c_int)(("perf_reader_set_fd", lib))
        mmap_reader = ct.CFUNCTYPE(ct.c_int, ct.c_void_p)(("perf_reader_mmap", lib))
        free_reader = ct.CFUNCTYPE(None, ct.c_void_p)(("perf_reader_free", lib))
        self.lib.sink_perf_event_open.argtypes = [ct.c_int, ct.c_uint]
        self.lib.sink_perf_event_open.restype = ct.c_int
        self.lib.sink_perf_event_enable.argtypes = [ct.c_int]
        self.lib.sink_perf_event_enable.restype = ct.c_int
        cpus = list(get_online_cpus())
        self.descriptor["perf_wakeup_events"] = PERF_WAKEUP_EVENTS
        for cpu in cpus:
            reader = new_reader(callback, lost_callback, self.handle, page_count)
            if not reader:
                raise OSError("native BPF perf reader open failed")
            fd = self.lib.sink_perf_event_open(cpu, PERF_WAKEUP_EVENTS)
            if fd < 0:
                free_reader(reader)
                raise OSError(ct.get_errno(), "native perf event open failed")
            set_fd(reader, fd)
            if mmap_reader(reader) < 0 or self.lib.sink_perf_event_enable(fd) < 0:
                free_reader(reader)
                raise OSError(ct.get_errno(), "native perf reader setup failed")
            # Use BCC's reader registry so its normal polling and reader
            # cleanup own exactly the same descriptors as the Python path.
            table.bpf.perf_buffers[(id(table), cpu)] = reader
            table._cbs[cpu] = (callback, lost_callback)
            table._open_key_fds[cpu] = -1
            table[table.Key(cpu)] = table.Leaf(lib.perf_reader_fd(reader))
        # Reaching this point means every requested CPU reader was mmap'd and
        # enabled.  Record the actual successful capacity, including the
        # kernel metadata page that is part of each perf mmap.
        self.descriptor.update(
            perf_buffer_descriptor(
                implementation="native_c_bcc_reader",
                page_count=page_count,
                cpu_count=len(cpus),
                page_count_source="successful_perf_reader_mmap",
            )
        )

    def drain_perf_buffers(self, table):
        """Called only by the perf poll thread; drain below-threshold tails."""
        from bcc import lib
        read = ct.CFUNCTYPE(None, ct.c_void_p)(("perf_reader_event_read", lib))
        for (table_id, _cpu), reader in list(table.bpf.perf_buffers.items()):
            if table_id == id(table):
                read(reader)

    def stats(self, token: int = 0) -> SinkStats:
        result = SinkStats()
        if not self.handle or self.lib.sink_stats(self.handle, token, ct.byref(result)):
            raise OSError("native BPF sink stats failed")
        return result

    def flush(self, *, fsync: bool) -> SinkStats:
        if not self.handle or self.lib.sink_flush(self.handle, int(fsync)):
            raise OSError("native BPF sink flush failed")
        return self.stats()

    def boundary(self, token: int = 0, *, fsync: bool = True) -> SinkStats:
        result = SinkStats()
        if not self.handle or self.lib.sink_boundary(self.handle, token, int(fsync), ct.byref(result)):
            raise OSError("native BPF sink boundary failed")
        return result

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            if self.lib.sink_close(handle):
                raise OSError("native BPF sink close failed")
