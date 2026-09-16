"""Identity-bound Linux CPU work collection and ``strace`` trace replay.

The collector attaches to an already-running process.  It never wraps an
action in another shell, changes the command, or infers work from command
text.  A caller records the real action boundary with :meth:`start_action`
and :meth:`end_action`; the raw ``strace -ff -ttt -T -yy`` files are retained
and the summary is derived from those files later.

``strace -ttt`` timestamps are Unix epoch seconds, so trace/action slicing is
performed in ``CLOCK_REALTIME`` nanoseconds.  Monotonic timestamps are also
stored at every collector boundary for local lifecycle correlation, but are
never mixed with the trace clock.  Process identity is bound to PID,
``/proc/<pid>/stat`` start ticks, boot ID, and the caller's run/attempt/case
identity.  A reused PID therefore cannot silently receive another action's
trace.

The module deliberately keeps two I/O notions separate.  Returned syscall
bytes are split between path-backed descriptors, pipes/sockets, other known
descriptors, and unknown descriptors; ``-yy`` does not prove regular-file
inode type.  Linux ``/proc/<pid>/io`` ``rchar``/``wchar`` are syscall-facing
byte counters while ``read_bytes``/``write_bytes`` are physical-storage
counters when available; cached bytes are never relabelled as disk bytes.
``/proc`` CPU counters are reported with their tick resolution and child-exit
caveats.

The primary backend is strace because it is available on the target Linux
host.  An eBPF backend can consume the same boundary and summary contract in
the future without changing the hook API.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .clock import clock_fields


COLLECTOR_SCHEMA = "assignment.linux-work-collector.v1"
RAW_TRACE_SCHEMA = "assignment.linux-strace-raw.v1"
BOUNDARY_SCHEMA = "assignment.linux-work-boundary.v1"
SUMMARY_SCHEMA = "assignment.linux-work-summary.v1"
OVERHEAD_SCHEMA = "assignment.linux-work-overhead.v1"
TRACE_CLOCK_ID = "CLOCK_REALTIME"
TRACE_CLOCK_SOURCE = "strace -ttt Unix epoch seconds"
VALID_ACTION_STATUSES = frozenset(
    {"success", "failure", "timeout", "unavailable", "incomplete"}
)

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_PID_FILE_RE = re.compile(r"(?:^|[.])(?P<pid>[0-9]+)$")
_PID_PREFIX_RE = re.compile(r"^\[pid\s+(?P<pid>[0-9]+)\]\s+(?P<body>.*)$")
_TIMESTAMP_RE = re.compile(r"^(?P<timestamp>[0-9]+(?:\.[0-9]+)?)\s+(?P<body>.*)$")
_COMPLETE_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\)\s+=\s+"
    r"(?P<result>.*?)\s+<(?P<duration>[0-9]+(?:\.[0-9]+)?)>\s*$"
)
_UNFINISHED_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<args>.*)\s+<unfinished \.\.\.>\s*$"
)
_RESUMED_RE = re.compile(
    r"^<\.\.\.\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+resumed>\s+"
    r"(?P<tail>.*?)\s+=\s+(?P<result>.*?)\s+<(?P<duration>[0-9]+(?:\.[0-9]+)?)>\s*$"
)
_EXIT_RE = re.compile(r"^\+\+\+ exited with (?P<code>-?[0-9]+) \+\+\+\s*$")
_DETACHED_RE = re.compile(r"(?:detached|timed out|timeout)", re.IGNORECASE)
_QUOTED_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
_FD_TARGET_RE = re.compile(r"(?<![A-Za-z0-9_])(?P<fd>[0-9]+)<(?P<target>[^>]+)>")
_ERRNO_RE = re.compile(r"^-1\s+(?P<errno>[A-Z][A-Z0-9_]*)")
_INTEGER_RE = re.compile(r"^-?(?:0[xX][0-9a-fA-F]+|[0-9]+)")

_READ_SYSCALLS = frozenset(
    {
        "read",
        "pread",
        "pread64",
        "readv",
        "preadv",
        "preadv2",
        "recv",
        "recvfrom",
        "recvmsg",
        "splice",
        "copy_file_range",
    }
)
_WRITE_SYSCALLS = frozenset(
    {
        "write",
        "pwrite",
        "pwrite64",
        "writev",
        "pwritev",
        "pwritev2",
        "send",
        "sendto",
        "sendmsg",
        "splice",
        "copy_file_range",
    }
)
_MESSAGE_READ_SYSCALLS = frozenset({"recvmmsg"})
_MESSAGE_WRITE_SYSCALLS = frozenset({"sendmmsg"})
_OPEN_SYSCALLS = frozenset({"open", "openat", "openat2", "creat"})
_STAT_SYSCALLS = frozenset(
    {
        "stat",
        "stat64",
        "lstat",
        "lstat64",
        "fstat",
        "fstat64",
        "fstatat",
        "newfstatat",
        "statx",
    }
)
_GETDENTS_SYSCALLS = frozenset({"getdents", "getdents64"})
_PATH_SYSCALLS = frozenset(
    {
        *_OPEN_SYSCALLS,
        *_STAT_SYSCALLS,
        "access",
        "faccessat",
        "faccessat2",
        "readlink",
        "readlinkat",
        "execve",
        "execveat",
        "unlink",
        "unlinkat",
        "rename",
        "renameat",
        "renameat2",
        "mkdir",
        "mkdirat",
        "rmdir",
        "chdir",
        "fchdir",
        "link",
        "linkat",
        "symlink",
        "symlinkat",
    }
)
_FORK_SYSCALLS = frozenset({"fork", "vfork"})
_CLONE_SYSCALLS = frozenset({"clone", "clone3"})
_EXEC_SYSCALLS = frozenset({"execve", "execveat"})


class LinuxWorkError(ValueError):
    """Base error for invalid or unavailable Linux work evidence."""


class CollectorAttachError(LinuxWorkError):
    """Raised when an existing process cannot be attached safely."""


class IdentityBindingError(LinuxWorkError):
    """Raised when a process or action no longer matches its binding."""


class TraceParseError(LinuxWorkError):
    """Raised when a raw trace cannot be parsed as the declared format."""


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LinuxWorkError(f"{name} must be non-empty text without NUL")
    return value


def _require_action_command(value: Any, name: str) -> str:
    """Validate a physical action command, including an empty reset call."""

    if not isinstance(value, str) or "\x00" in value:
        raise LinuxWorkError(f"{name} must be text without NUL")
    return value


def _require_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LinuxWorkError(f"{name} must be a positive integer")
    return value


def _require_ns(value: Any, name: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LinuxWorkError(f"{name} must be an integer nanosecond timestamp")
    if value < 0 or (value == 0 and not allow_zero):
        raise LinuxWorkError(f"{name} must be a positive nanosecond timestamp")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_line(value: Mapping[str, Any]) -> bytes:
    return (_canonical_json(dict(value)) + "\n").encode("utf-8")


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    if path.is_symlink():
        raise LinuxWorkError(f"journal path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        pending = memoryview(_json_line(value))
        while pending:
            written = os.write(fd, pending)
            if written <= 0:
                raise OSError("journal append made no progress")
            pending = pending[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_boot_id(path: Path = Path("/proc/sys/kernel/random/boot_id")) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise CollectorAttachError(f"Linux boot ID is unavailable: {path}") from exc
    if not value:
        raise CollectorAttachError("Linux boot ID is empty")
    return value


def _pid_namespace_inode(pid: int) -> int:
    try:
        return os.stat(f"/proc/{pid}/ns/pid").st_ino
    except OSError as exc:
        raise CollectorAttachError(f"PID namespace identity is unavailable for PID {pid}") from exc


def _parse_proc_stat_text(text: str, *, pid: int) -> dict[str, Any]:
    """Parse ``/proc/<pid>/stat`` without splitting a parenthesized comm."""

    close = text.rfind(")")
    if close < 0:
        raise IdentityBindingError(f"/proc/{pid}/stat has no closing comm delimiter")
    prefix = text[:close]
    if "(" not in prefix:
        raise IdentityBindingError(f"/proc/{pid}/stat has no comm field")
    comm = prefix[prefix.find("(") + 1 :]
    fields = text[close + 2 :].split()
    if len(fields) < 20:
        raise IdentityBindingError(f"/proc/{pid}/stat is truncated")
    try:
        values = {
            "pid": pid,
            "comm": comm,
            "state": fields[0],
            "ppid": int(fields[1]),
            "utime_ticks": int(fields[11]),
            "stime_ticks": int(fields[12]),
            "cutime_ticks": int(fields[13]),
            "cstime_ticks": int(fields[14]),
            "num_threads": int(fields[17]),
            "start_ticks": int(fields[19]),
        }
    except (IndexError, TypeError, ValueError) as exc:
        raise IdentityBindingError(f"/proc/{pid}/stat has invalid numeric fields") from exc
    if values["start_ticks"] <= 0:
        raise IdentityBindingError(f"/proc/{pid}/stat has invalid start ticks")
    return values


def _read_proc_stat(pid: int) -> dict[str, Any]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise IdentityBindingError(f"cannot read /proc/{pid}/stat") from exc
    return _parse_proc_stat_text(text, pid=pid)


def _read_proc_io(pid: int) -> dict[str, int] | None:
    fields: dict[str, int] = {}
    try:
        lines = Path(f"/proc/{pid}/io").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        key, separator, value = line.partition(":")
        if not separator:
            continue
        try:
            number = int(value.strip())
        except ValueError:
            continue
        if number >= 0:
            fields[key.strip()] = number
    return fields or None


def _read_tracer_pid(pid: int) -> int | None:
    """Read the kernel's ptrace owner for *pid*.

    A live strace process is not proof that ``-p`` succeeded: strace can stay
    alive while an attach is pending or can have failed after the target was
    quiet.  ``TracerPid`` is the kernel acknowledgement used by attach().
    """

    try:
        lines = Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise IdentityBindingError(f"cannot read /proc/{pid}/status for tracer binding") from exc
    for line in lines:
        key, separator, value = line.partition(":")
        if key.strip() != "TracerPid" or not separator:
            continue
        try:
            tracer_pid = int(value.strip())
        except ValueError as exc:
            raise IdentityBindingError(f"invalid TracerPid in /proc/{pid}/status") from exc
        return tracer_pid or None
    raise IdentityBindingError(f"/proc/{pid}/status has no TracerPid")


def _capture_pid_binding(pid: int) -> dict[str, int | str]:
    """Capture the minimum identity needed before signaling a helper PID."""

    stat = _read_proc_stat(pid)
    return {
        "pid": pid,
        "start_ticks": int(stat["start_ticks"]),
        "boot_id": _read_boot_id(),
        "pid_namespace_inode": int(_pid_namespace_inode(pid)),
    }


def _assert_pid_binding(binding: Mapping[str, Any], *, role: str) -> None:
    try:
        pid = _require_positive_int(binding.get("pid"), f"{role}.pid")
        expected_start = _require_positive_int(binding.get("start_ticks"), f"{role}.start_ticks")
        expected_boot = _require_text(binding.get("boot_id"), f"{role}.boot_id")
        expected_namespace = _require_positive_int(
            binding.get("pid_namespace_inode"), f"{role}.pid_namespace_inode"
        )
        current = _read_proc_stat(pid)
        if int(current["start_ticks"]) != expected_start:
            raise IdentityBindingError(
                f"{role} PID {pid} was reused: expected start_ticks={expected_start}, "
                f"found {current['start_ticks']}"
            )
        if _read_boot_id() != expected_boot:
            raise IdentityBindingError(f"{role} boot ID changed")
        if _pid_namespace_inode(pid) != expected_namespace:
            raise IdentityBindingError(f"{role} PID namespace changed")
    except (CollectorAttachError, IdentityBindingError, LinuxWorkError):
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise IdentityBindingError(f"cannot validate {role} process binding") from exc


@dataclass(frozen=True)
class ProcessTarget:
    """Explicit process mapping supplied by the runtime hook or CLI."""

    pid: int
    run_id: str
    attempt_id: str
    case_id: str
    instance_id: str | None = None
    container_pid: int | None = None
    pid_namespace: str | None = None
    mapping_source: str = "explicit_pid_mapping"

    def __post_init__(self) -> None:
        _require_positive_int(self.pid, "pid")
        _require_text(self.run_id, "run_id")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.case_id, "case_id")
        if self.instance_id is not None:
            _require_text(self.instance_id, "instance_id")
        if self.container_pid is not None:
            _require_positive_int(self.container_pid, "container_pid")
        if not isinstance(self.mapping_source, str) or not self.mapping_source:
            raise LinuxWorkError("mapping_source must be non-empty text")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "container_pid": self.container_pid,
            "pid_namespace": self.pid_namespace,
            "mapping_source": self.mapping_source,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "instance_id": self.instance_id,
        }


@dataclass(frozen=True)
class ProcessIdentity:
    """Identity captured at attach time and checked at every action boundary."""

    pid: int
    start_ticks: int
    boot_id: str
    pid_namespace_inode: int
    run_id: str
    attempt_id: str
    case_id: str
    instance_id: str | None
    container_pid: int | None
    pid_namespace: str | None
    mapping_source: str

    def __post_init__(self) -> None:
        _require_positive_int(self.pid, "pid")
        _require_positive_int(self.start_ticks, "start_ticks")
        _require_text(self.boot_id, "boot_id")
        _require_positive_int(self.pid_namespace_inode, "pid_namespace_inode")
        _require_text(self.run_id, "run_id")
        _require_text(self.attempt_id, "attempt_id")
        _require_text(self.case_id, "case_id")

    @classmethod
    def capture(cls, target: ProcessTarget) -> "ProcessIdentity":
        stat = _read_proc_stat(target.pid)
        if stat["pid"] != target.pid:
            raise IdentityBindingError("/proc stat PID disagrees with target PID")
        return cls(
            pid=target.pid,
            start_ticks=stat["start_ticks"],
            boot_id=_read_boot_id(),
            pid_namespace_inode=_pid_namespace_inode(target.pid),
            run_id=target.run_id,
            attempt_id=target.attempt_id,
            case_id=target.case_id,
            instance_id=target.instance_id,
            container_pid=target.container_pid,
            pid_namespace=target.pid_namespace,
            mapping_source=target.mapping_source,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ProcessIdentity":
        if not isinstance(value, Mapping):
            raise IdentityBindingError("process identity must be a mapping")
        return cls(
            pid=_require_positive_int(value.get("pid"), "identity.pid"),
            start_ticks=_require_positive_int(value.get("start_ticks"), "identity.start_ticks"),
            boot_id=_require_text(value.get("boot_id"), "identity.boot_id"),
            pid_namespace_inode=_require_positive_int(
                value.get("pid_namespace_inode"), "identity.pid_namespace_inode"
            ),
            run_id=_require_text(value.get("run_id"), "identity.run_id"),
            attempt_id=_require_text(value.get("attempt_id"), "identity.attempt_id"),
            case_id=_require_text(value.get("case_id"), "identity.case_id"),
            instance_id=value.get("instance_id"),
            container_pid=value.get("container_pid"),
            pid_namespace=value.get("pid_namespace"),
            mapping_source=_require_text(value.get("mapping_source"), "identity.mapping_source"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "boot_id": self.boot_id,
            "pid_namespace_inode": self.pid_namespace_inode,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "case_id": self.case_id,
            "instance_id": self.instance_id,
            "container_pid": self.container_pid,
            "pid_namespace": self.pid_namespace,
            "mapping_source": self.mapping_source,
        }

    def binding_digest(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_mapping()).encode("utf-8")).hexdigest()

    def assert_current(self) -> dict[str, Any]:
        current = _read_proc_stat(self.pid)
        if current["start_ticks"] != self.start_ticks:
            raise IdentityBindingError(
                f"PID {self.pid} was reused: expected start_ticks={self.start_ticks}, "
                f"found {current['start_ticks']}"
            )
        current_boot = _read_boot_id()
        if current_boot != self.boot_id:
            raise IdentityBindingError("Linux boot ID changed during collection")
        current_namespace = _pid_namespace_inode(self.pid)
        if current_namespace != self.pid_namespace_inode:
            raise IdentityBindingError("PID namespace identity changed during collection")
        return current


def _wall_mono_fields() -> dict[str, Any]:
    return {
        "wall_clock": {
            "clock_id": TRACE_CLOCK_ID,
            "clock_source": "time.time_ns",
            "unit": "nanoseconds",
        },
        "monotonic_clock": dict(clock_fields()),
    }


def _snapshot_process_tree(identity: ProcessIdentity) -> dict[str, Any]:
    """Capture target/descendant ``/proc`` counters at one boundary."""

    identity.assert_current()
    processes: dict[int, dict[str, Any]] = {}
    try:
        candidates = [
            int(path.name)
            for path in Path("/proc").iterdir()
            if path.name.isdigit()
        ]
    except OSError as exc:
        raise IdentityBindingError("cannot enumerate /proc for process lineage") from exc
    for pid in candidates:
        try:
            stat = _read_proc_stat(pid)
        except IdentityBindingError:
            continue
        processes[pid] = {
            **stat,
            "io": _read_proc_io(pid),
        }
    if identity.pid not in processes:
        raise IdentityBindingError("target disappeared while capturing process lineage")
    selected: dict[int, dict[str, Any]] = {identity.pid: processes[identity.pid]}
    changed = True
    while changed:
        changed = False
        for pid, stat in processes.items():
            if pid in selected:
                continue
            if stat["ppid"] in selected:
                selected[pid] = stat
                changed = True
    return {
        "captured_wall_ns": time.time_ns(),
        "captured_mono_ns": time.monotonic_ns(),
        **_wall_mono_fields(),
        "target_pid": identity.pid,
        "target_start_ticks": identity.start_ticks,
        "processes": {str(pid): value for pid, value in sorted(selected.items())},
        "enumeration_scope": "target and descendants observed in /proc at boundary",
    }


@dataclass(frozen=True)
class ActionBoundary:
    """One durable pre/post action boundary pair."""

    event_id: str
    command: str
    command_sha256: str
    start_wall_ns: int
    start_mono_ns: int
    end_wall_ns: int | None = None
    end_mono_ns: int | None = None
    status: str = "incomplete"
    timeout: bool = False
    error: str | None = None
    start_snapshot: Mapping[str, Any] | None = None
    end_snapshot: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_action_command(self.command, "command")
        if not _HEX64.fullmatch(self.command_sha256):
            raise LinuxWorkError("command_sha256 must be a 64-character hex digest")
        _require_ns(self.start_wall_ns, "start_wall_ns")
        _require_ns(self.start_mono_ns, "start_mono_ns")
        if self.end_wall_ns is not None:
            _require_ns(self.end_wall_ns, "end_wall_ns")
            if self.end_wall_ns < self.start_wall_ns:
                raise LinuxWorkError("action wall interval is reversed")
        if self.end_mono_ns is not None:
            _require_ns(self.end_mono_ns, "end_mono_ns")
            if self.end_mono_ns < self.start_mono_ns:
                raise LinuxWorkError("action monotonic interval is reversed")
        if self.status not in VALID_ACTION_STATUSES:
            raise LinuxWorkError(f"unsupported action status: {self.status}")

    @property
    def complete(self) -> bool:
        return self.end_wall_ns is not None

    def to_mapping(self, identity: ProcessIdentity, *, phase: str = "complete") -> dict[str, Any]:
        if phase not in {"start", "end", "complete"}:
            raise LinuxWorkError("boundary phase must be start, end, or complete")
        return {
            "schema_version": BOUNDARY_SCHEMA,
            "phase": phase,
            "event_id": self.event_id,
            "command": self.command,
            "command_sha256": self.command_sha256,
            "start_wall_ns": self.start_wall_ns,
            "start_mono_ns": self.start_mono_ns,
            "end_wall_ns": self.end_wall_ns,
            "end_mono_ns": self.end_mono_ns,
            "status": self.status,
            "timeout": self.timeout,
            "error": self.error,
            "identity": identity.to_mapping(),
            "clock": _wall_mono_fields(),
            "start_snapshot": self.start_snapshot,
            "end_snapshot": self.end_snapshot,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ActionBoundary":
        if value.get("schema_version") != BOUNDARY_SCHEMA:
            raise LinuxWorkError("unsupported action boundary schema")
        command = _require_action_command(value.get("command"), "boundary.command")
        expected = _sha256_text(command)
        if value.get("command_sha256") != expected:
            raise IdentityBindingError("action boundary command hash does not match command")
        return cls(
            event_id=_require_text(value.get("event_id"), "boundary.event_id"),
            command=command,
            command_sha256=expected,
            start_wall_ns=_require_ns(value.get("start_wall_ns"), "boundary.start_wall_ns"),
            start_mono_ns=_require_ns(value.get("start_mono_ns"), "boundary.start_mono_ns"),
            end_wall_ns=value.get("end_wall_ns"),
            end_mono_ns=value.get("end_mono_ns"),
            status=value.get("status", "incomplete"),
            timeout=bool(value.get("timeout", False)),
            error=value.get("error"),
            start_snapshot=value.get("start_snapshot"),
            end_snapshot=value.get("end_snapshot"),
        )


class BoundaryJournal:
    """Append-only action boundary journal usable by a runtime hook or CLI."""

    def __init__(self, path: Path, identity: ProcessIdentity):
        self.path = Path(path)
        self.identity = identity
        if self.path.is_symlink():
            raise LinuxWorkError(f"boundary journal must not be a symlink: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._active: dict[str, ActionBoundary] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise LinuxWorkError(f"cannot read boundary journal: {self.path}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise LinuxWorkError(f"boundary journal has blank line {line_number}")
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LinuxWorkError(f"boundary journal line {line_number} is invalid JSON") from exc
            if not isinstance(value, Mapping):
                raise LinuxWorkError(f"boundary journal line {line_number} is not an object")
            identity = ProcessIdentity.from_mapping(value.get("identity", {}))
            self._assert_identity(identity)
            boundary = ActionBoundary.from_mapping(value)
            event_id = boundary.event_id
            phase = value.get("phase")
            if phase == "start":
                if event_id in self._active:
                    raise LinuxWorkError(f"duplicate action start: {event_id}")
                self._active[event_id] = boundary
            elif phase == "end":
                start = self._active.pop(event_id, None)
                if start is None:
                    raise LinuxWorkError(f"action end has no start: {event_id}")
                if boundary.command_sha256 != start.command_sha256:
                    raise IdentityBindingError(f"action hash changed between boundaries: {event_id}")
            else:
                raise LinuxWorkError(f"unsupported action boundary phase: {phase!r}")

    def _assert_identity(self, identity: ProcessIdentity) -> None:
        if identity.to_mapping() != self.identity.to_mapping():
            raise IdentityBindingError("boundary identity does not match collector identity")

    def start(
        self,
        event_id: str,
        command: str,
        *,
        start_wall_ns: int | None = None,
        start_mono_ns: int | None = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> ActionBoundary:
        event_id = _require_text(event_id, "event_id")
        command = _require_action_command(command, "command")
        if event_id in self._active:
            raise LinuxWorkError(f"action is already active: {event_id}")
        boundary = ActionBoundary(
            event_id=event_id,
            command=command,
            command_sha256=_sha256_text(command),
            start_wall_ns=_require_ns(start_wall_ns or time.time_ns(), "start_wall_ns"),
            start_mono_ns=_require_ns(start_mono_ns or time.monotonic_ns(), "start_mono_ns"),
            start_snapshot=snapshot,
        )
        _append_jsonl(self.path, boundary.to_mapping(self.identity, phase="start"))
        self._active[event_id] = boundary
        return boundary

    def end(
        self,
        event_id: str,
        *,
        status: str,
        end_wall_ns: int | None = None,
        end_mono_ns: int | None = None,
        timeout: bool = False,
        error: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
    ) -> ActionBoundary:
        event_id = _require_text(event_id, "event_id")
        start = self._active.pop(event_id, None)
        if start is None:
            raise LinuxWorkError(f"action has no active start: {event_id}")
        end_wall = _require_ns(end_wall_ns or time.time_ns(), "end_wall_ns")
        end_mono = _require_ns(end_mono_ns or time.monotonic_ns(), "end_mono_ns")
        boundary = ActionBoundary(
            event_id=start.event_id,
            command=start.command,
            command_sha256=start.command_sha256,
            start_wall_ns=start.start_wall_ns,
            start_mono_ns=start.start_mono_ns,
            end_wall_ns=end_wall,
            end_mono_ns=end_mono,
            status=status,
            timeout=timeout,
            error=error,
            start_snapshot=start.start_snapshot,
            end_snapshot=snapshot,
        )
        _append_jsonl(self.path, boundary.to_mapping(self.identity, phase="end"))
        return boundary

    @property
    def active_event_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._active))

    def boundaries(self) -> list[ActionBoundary]:
        """Return paired boundaries, rejecting incomplete or mismatched journals."""

        starts: dict[str, ActionBoundary] = {}
        ends: dict[str, ActionBoundary] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise LinuxWorkError(f"cannot read boundary journal: {self.path}") from exc
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                raise LinuxWorkError(f"boundary journal has blank line {line_number}")
            value = json.loads(line)
            identity = ProcessIdentity.from_mapping(value.get("identity", {}))
            self._assert_identity(identity)
            boundary = ActionBoundary.from_mapping(value)
            if value.get("phase") == "start":
                starts[boundary.event_id] = boundary
            elif value.get("phase") == "end":
                ends[boundary.event_id] = boundary
        if set(starts) != set(ends):
            raise LinuxWorkError(
                f"boundary journal has unpaired actions: starts={sorted(set(starts)-set(ends))}, "
                f"ends={sorted(set(ends)-set(starts))}"
            )
        result: list[ActionBoundary] = []
        for event_id in sorted(starts):
            start = starts[event_id]
            end = ends[event_id]
            if start.command_sha256 != end.command_sha256:
                raise IdentityBindingError(f"action hash changed between boundaries: {event_id}")
            result.append(end)
        return result


@dataclass(frozen=True)
class SyscallRecord:
    """One parsed syscall or process diagnostic from a raw strace line."""

    pid: int
    timestamp_ns: int
    name: str
    args: str
    result: str | None
    return_value: int | None
    duration_ns: int | None
    state: str
    source_file: str | None
    line_numbers: tuple[int, ...]
    raw_lines: tuple[str, ...]
    end_timestamp_ns: int | None = None
    errno: str | None = None

    @property
    def end_ns(self) -> int:
        if self.end_timestamp_ns is not None:
            return self.end_timestamp_ns
        if self.duration_ns is not None:
            return self.timestamp_ns + self.duration_ns
        return self.timestamp_ns

    @property
    def failed(self) -> bool:
        return self.state in {"failure", "timeout"} or (
            self.return_value is not None and self.return_value < 0
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "timestamp_ns": self.timestamp_ns,
            "end_timestamp_ns": self.end_timestamp_ns,
            "name": self.name,
            "args": self.args,
            "result": self.result,
            "return_value": self.return_value,
            "duration_ns": self.duration_ns,
            "state": self.state,
            "source_file": self.source_file,
            "line_numbers": list(self.line_numbers),
            "raw_lines": list(self.raw_lines),
            "errno": self.errno,
        }


@dataclass
class TraceParseResult:
    records: list[SyscallRecord] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    raw_line_count: int = 0
    parsed_line_count: int = 0

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": RAW_TRACE_SCHEMA,
            "clock": {
                "clock_id": TRACE_CLOCK_ID,
                "clock_source": TRACE_CLOCK_SOURCE,
                "unit": "nanoseconds after conversion from strace decimal seconds",
            },
            "raw_line_count": self.raw_line_count,
            "parsed_line_count": self.parsed_line_count,
            "diagnostics": self.diagnostics,
            "records": [record.to_mapping() for record in self.records],
        }


def _pid_from_trace_path(path: Path) -> int | None:
    match = _PID_FILE_RE.search(path.name)
    return int(match.group("pid")) if match else None


def _parse_timestamp(value: str) -> int:
    try:
        timestamp = float(value)
    except ValueError as exc:
        raise TraceParseError(f"invalid strace timestamp: {value!r}") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise TraceParseError(f"invalid strace timestamp: {value!r}")
    return int(round(timestamp * 1_000_000_000))


def _parse_return_value(value: str) -> int | None:
    match = _INTEGER_RE.match(value.strip())
    if not match:
        return None
    try:
        return int(match.group(0), 0)
    except ValueError:
        return None


def _parse_result(value: str) -> tuple[str, int | None, str | None, str]:
    result = value.strip()
    errno_match = _ERRNO_RE.match(result)
    errno = errno_match.group("errno") if errno_match else None
    return_value = _parse_return_value(result)
    lower = result.lower()
    if "?" in result or "ereSTART".lower() in lower or _DETACHED_RE.search(result):
        state = "timeout"
    elif errno is not None or (return_value is not None and return_value < 0):
        state = "failure"
    else:
        state = "completed"
    return result, return_value, errno, state


def _parse_trace_line(
    line: str,
    *,
    default_pid: int | None,
    source_file: str | None,
    line_number: int,
) -> tuple[int, int, str, str] | None:
    raw = line.rstrip("\n")
    pid = default_pid
    prefix = _PID_PREFIX_RE.match(raw)
    if prefix:
        pid = int(prefix.group("pid"))
        raw = prefix.group("body")
    timestamp = _TIMESTAMP_RE.match(raw)
    if timestamp is None:
        return None
    if pid is None:
        raise TraceParseError(
            f"trace line has no PID and source filename has no PID: {source_file}:{line_number}"
        )
    return pid, _parse_timestamp(timestamp.group("timestamp")), timestamp.group("body"), raw


def parse_strace_lines(
    lines: Iterable[str],
    *,
    default_pid: int | None = None,
    source_file: str | None = None,
) -> TraceParseResult:
    """Parse complete, unfinished, resumed, failed, and timeout strace rows."""

    result = TraceParseResult()
    pending: dict[tuple[int, str], tuple[int, str, int, str]] = {}
    for line_number, line in enumerate(lines, 1):
        result.raw_line_count += 1
        parsed = _parse_trace_line(
            line,
            default_pid=default_pid,
            source_file=source_file,
            line_number=line_number,
        )
        if parsed is None:
            text = line.rstrip("\n")
            if text.strip():
                result.diagnostics.append(
                    {
                        "line_number": line_number,
                        "source_file": source_file,
                        "text": text,
                        "kind": "unstructured",
                        "timeout": bool(_DETACHED_RE.search(text)),
                    }
                )
            continue
        pid, timestamp_ns, body, raw = parsed
        resumed = _RESUMED_RE.match(body)
        if resumed:
            name = resumed.group("name")
            pending_row = pending.pop((pid, name), None)
            if pending_row is None:
                result.diagnostics.append(
                    {
                        "line_number": line_number,
                        "source_file": source_file,
                        "text": raw,
                        "kind": "orphan_resumed",
                    }
                )
                continue
            start_ns, args, start_line, start_raw = pending_row
            result_text, return_value, errno, state = _parse_result(resumed.group("result"))
            duration_ns = int(round(float(resumed.group("duration")) * 1_000_000_000))
            result.records.append(
                SyscallRecord(
                    pid=pid,
                    timestamp_ns=start_ns,
                    end_timestamp_ns=timestamp_ns,
                    name=name,
                    args=args + resumed.group("tail"),
                    result=result_text,
                    return_value=return_value,
                    duration_ns=duration_ns,
                    state="resumed" if state == "completed" else state,
                    source_file=source_file,
                    line_numbers=(start_line, line_number),
                    raw_lines=(start_raw, raw),
                    errno=errno,
                )
            )
            result.parsed_line_count += 1
            continue
        unfinished = _UNFINISHED_RE.match(body)
        if unfinished:
            name = unfinished.group("name")
            key = (pid, name)
            if key in pending:
                result.records.append(
                    SyscallRecord(
                        pid=pid,
                        timestamp_ns=pending[key][0],
                        name=name,
                        args=pending[key][1],
                        result=None,
                        return_value=None,
                        duration_ns=None,
                        state="unfinished",
                        source_file=source_file,
                        line_numbers=(pending[key][2],),
                        raw_lines=(pending[key][3],),
                    )
                )
            pending[key] = (timestamp_ns, unfinished.group("args"), line_number, raw)
            result.parsed_line_count += 1
            continue
        exited = _EXIT_RE.match(body)
        if exited:
            code = int(exited.group("code"))
            result.records.append(
                SyscallRecord(
                    pid=pid,
                    timestamp_ns=timestamp_ns,
                    name="__process_exit__",
                    args="",
                    result=body,
                    return_value=code,
                    duration_ns=0,
                    state="process_exit" if code == 0 else "failure",
                    source_file=source_file,
                    line_numbers=(line_number,),
                    raw_lines=(raw,),
                )
            )
            result.parsed_line_count += 1
            continue
        complete = _COMPLETE_RE.match(body)
        if complete is None:
            result.diagnostics.append(
                {
                    "line_number": line_number,
                    "source_file": source_file,
                    "text": raw,
                    "kind": "unparsed_timestamped",
                }
            )
            continue
        result_text, return_value, errno, state = _parse_result(complete.group("result"))
        duration_ns = int(round(float(complete.group("duration")) * 1_000_000_000))
        result.records.append(
            SyscallRecord(
                pid=pid,
                timestamp_ns=timestamp_ns,
                name=complete.group("name"),
                args=complete.group("args"),
                result=result_text,
                return_value=return_value,
                duration_ns=duration_ns,
                state=state,
                source_file=source_file,
                line_numbers=(line_number,),
                raw_lines=(raw,),
                errno=errno,
            )
        )
        result.parsed_line_count += 1
    for (pid, name), (timestamp_ns, args, line_number, raw) in sorted(pending.items()):
        result.records.append(
            SyscallRecord(
                pid=pid,
                timestamp_ns=timestamp_ns,
                name=name,
                args=args,
                result=None,
                return_value=None,
                duration_ns=None,
                state="unfinished",
                source_file=source_file,
                line_numbers=(line_number,),
                raw_lines=(raw,),
            )
        )
    result.records.sort(key=lambda record: (record.timestamp_ns, record.pid, record.line_numbers))
    return result


def parse_strace_file(path: Path, *, default_pid: int | None = None) -> TraceParseResult:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise TraceParseError(f"raw trace is not a regular file: {path}")
    if default_pid is None:
        default_pid = _pid_from_trace_path(path)
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return parse_strace_lines(stream, default_pid=default_pid, source_file=str(path))
    except OSError as exc:
        raise TraceParseError(f"cannot read raw trace: {path}") from exc


def parse_strace_files(paths: Iterable[Path]) -> TraceParseResult:
    combined = TraceParseResult()
    for path in paths:
        parsed = parse_strace_file(Path(path))
        combined.records.extend(parsed.records)
        combined.diagnostics.extend(parsed.diagnostics)
        combined.raw_line_count += parsed.raw_line_count
        combined.parsed_line_count += parsed.parsed_line_count
    combined.records.sort(key=lambda record: (record.timestamp_ns, record.pid, record.line_numbers))
    return combined


def _decode_strace_quoted(value: str) -> str:
    try:
        return json.loads('"' + value + '"')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return value.replace('\\"', '"').replace('\\\\', '\\')


def _observed_paths(record: SyscallRecord) -> set[str]:
    paths: set[str] = set()
    for target in re.findall(r"<([^>]+)>", record.args):
        if target.startswith(("pipe:[", "socket:[", "anon_inode:")):
            continue
        if target and target not in {"unfinished ..."}:
            paths.add(target)
    if record.name in _PATH_SYSCALLS:
        for quoted in _QUOTED_RE.findall(record.args):
            value = _decode_strace_quoted(quoted)
            # Path arguments are observed kernel inputs.  Restrict this to
            # path-taking syscall classes so read/write payloads are not
            # mislabeled as files.
            if value and "\x00" not in value:
                paths.add(value)
    return paths


def _fd_targets(record: SyscallRecord) -> list[str]:
    """Return ``-yy`` descriptor targets in argument order.

    A descriptor annotation is evidence of the kernel object target, but a
    path string alone does not prove ``S_IFREG``.  The aggregate therefore
    calls path-backed bytes ``path_backed`` until a separate type probe exists.
    """

    return [target for _, target in _FD_TARGET_RE.findall(record.args)]


def _fd_kind_for_target(target: str | None) -> tuple[str, str | None]:
    if target is None:
        return "unknown", None
    if target.startswith("pipe:["):
        return "pipe", target
    if target.startswith("socket:[") or target.startswith("netlink:"):
        return "socket", target
    if target.startswith("anon_inode:"):
        return "other", target
    if target.startswith("/") or target.startswith("."):
        # strace -yy prints the resolved path, not the inode mode.  A proc,
        # device, tty, directory, or regular file can all look path-backed.
        return "path_backed", target
    return "other", target


def _fd_kind(record: SyscallRecord, *, ordinal: int = 0) -> tuple[str, str | None]:
    if record.name not in _READ_SYSCALLS and record.name not in _WRITE_SYSCALLS:
        return "none", None
    targets = _fd_targets(record)
    return _fd_kind_for_target(targets[ordinal] if len(targets) > ordinal else None)


def _nonnegative_return(record: SyscallRecord) -> int:
    return record.return_value if record.return_value is not None and record.return_value >= 0 else 0


def _records_for_interval(
    records: Iterable[SyscallRecord], start_ns: int, end_ns: int
) -> list[SyscallRecord]:
    selected: list[SyscallRecord] = []
    for record in records:
        record_start, record_end = record.timestamp_ns, record.end_ns
        if record_end >= start_ns and record_start <= end_ns:
            selected.append(record)
    return selected


def _delta(start: Mapping[str, Any] | None, end: Mapping[str, Any] | None, field_name: str) -> int | None:
    if start is None or end is None:
        return None
    try:
        value = int(end[field_name]) - int(start[field_name])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _proc_delta(start_snapshot: Mapping[str, Any] | None, end_snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(start_snapshot, Mapping) or not isinstance(end_snapshot, Mapping):
        return {
            "availability": "unavailable",
            "reason": "one or both /proc boundary snapshots are unavailable",
            "processes": {},
        }
    start_processes = start_snapshot.get("processes", {})
    end_processes = end_snapshot.get("processes", {})
    if not isinstance(start_processes, Mapping) or not isinstance(end_processes, Mapping):
        return {"availability": "unavailable", "reason": "malformed /proc snapshots", "processes": {}}
    per_process: dict[str, Any] = {}
    missing_end: list[str] = []
    for pid, start in start_processes.items():
        end = end_processes.get(pid)
        if not isinstance(start, Mapping) or not isinstance(end, Mapping):
            missing_end.append(str(pid))
            continue
        if start.get("start_ticks") != end.get("start_ticks"):
            missing_end.append(str(pid))
            continue
        per_process[str(pid)] = {
            "start_ticks": start.get("start_ticks"),
            "user_cpu_ticks": _delta(start, end, "utime_ticks"),
            "system_cpu_ticks": _delta(start, end, "stime_ticks"),
            "waited_children_user_cpu_ticks": _delta(start, end, "cutime_ticks"),
            "waited_children_system_cpu_ticks": _delta(start, end, "cstime_ticks"),
            "thread_count_start": start.get("num_threads"),
            "thread_count_end": end.get("num_threads"),
            "io": {
                name: _delta(start.get("io"), end.get("io"), name)
                for name in ("rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw")
            },
        }
    complete_tree = not missing_end and len(per_process) == len(start_processes)
    totals: dict[str, int | None] = {}
    for field_name in (
        "user_cpu_ticks",
        "system_cpu_ticks",
        "waited_children_user_cpu_ticks",
        "waited_children_system_cpu_ticks",
    ):
        values = [row[field_name] for row in per_process.values() if row.get(field_name) is not None]
        totals[field_name] = (
            sum(values)
            if complete_tree and len(values) == len(per_process) and per_process
            else None
        )
    io_totals: dict[str, int | None] = {}
    for field_name in ("rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"):
        values = [row["io"].get(field_name) for row in per_process.values()]
        io_totals[field_name] = (
            sum(values)
            if complete_tree
            and values
            and len(values) == len(per_process)
            and all(v is not None for v in values)
            else None
        )
    tick_hz = os.sysconf("SC_CLK_TCK")
    return {
        "availability": "measured" if per_process else "unavailable",
        "tick_hz": int(tick_hz),
        "cpu_tick_resolution_ms": 1000.0 / int(tick_hz),
        "processes": per_process,
        "process_count_start": len(start_processes),
        "process_count_end": len(end_processes),
        "processes_missing_at_end": sorted(missing_end),
        "totals": totals,
        "io_totals": io_totals,
        "io_semantics": {
            "rchar_wchar": "bytes passed through read/write syscalls, including cached files and pipes/sockets",
            "read_bytes_write_bytes": "physical storage bytes reported by /proc/<pid>/io when available",
        },
        "caveats": [
            "CPU counters have kernel tick resolution.",
            "Exited children absent at the end boundary cannot contribute per-process deltas.",
            "waited_children_* only cover children accounted by the target process; ongoing or un-waited children are not complete.",
            "Physical read/write counters are kernel observations and are separate from returned syscall bytes.",
        ],
    }


def _aggregate_syscalls(records: Sequence[SyscallRecord]) -> dict[str, Any]:
    observed_paths: set[str] = set()
    path_backed_paths: set[str] = set()
    directory_paths: set[str] = set()
    path_backed_read = path_backed_write = 0
    pipe_read = pipe_write = 0
    socket_read = socket_write = 0
    unknown_read = unknown_write = 0
    other_read = other_write = 0
    returned_read = returned_write = 0
    received_message_count = sent_message_count = 0
    getdents_bytes = 0
    getdents_count = open_count = stat_count = 0
    fork_count = exec_count = thread_count = 0
    failed = timeout = unfinished = resumed = 0
    syscall_counts: dict[str, int] = {}
    for record in records:
        syscall_counts[record.name] = syscall_counts.get(record.name, 0) + 1
        observed_paths.update(_observed_paths(record))
        if record.name in _OPEN_SYSCALLS:
            open_count += 1
        if record.name in _STAT_SYSCALLS:
            stat_count += 1
        if record.name in _GETDENTS_SYSCALLS:
            getdents_count += 1
            getdents_bytes += _nonnegative_return(record)
            kind, target = _fd_kind_for_target((_fd_targets(record) or [None])[0])
            if target:
                directory_paths.add(target)
        if record.name in _FORK_SYSCALLS:
            if record.return_value is not None and record.return_value >= 0:
                fork_count += 1
        if record.name in _CLONE_SYSCALLS:
            if record.return_value is not None and record.return_value >= 0:
                if "CLONE_THREAD" in record.args:
                    thread_count += 1
                else:
                    fork_count += 1
        if record.name in _EXEC_SYSCALLS and not record.failed:
            exec_count += 1
        if record.state == "failure":
            failed += 1
        elif record.state == "timeout":
            timeout += 1
        elif record.state == "unfinished":
            unfinished += 1
        elif record.state == "resumed":
            resumed += 1
        if record.name in _MESSAGE_READ_SYSCALLS and not record.failed:
            # recvmmsg returns a message count, not a byte count.
            received_message_count += _nonnegative_return(record)
        if record.name in _MESSAGE_WRITE_SYSCALLS and not record.failed:
            # sendmmsg returns a message count, not a byte count.
            sent_message_count += _nonnegative_return(record)
        if record.name in _READ_SYSCALLS:
            value = _nonnegative_return(record)
            returned_read += value
            ordinal = 0
            kind, target = _fd_kind(record, ordinal=ordinal)
            if record.name in {"splice", "copy_file_range"}:
                # These calls have distinct source and destination FDs.  The
                # first descriptor is the source/read side.
                kind, target = _fd_kind(record, ordinal=0)
            if target and kind == "path_backed":
                path_backed_read += value
                path_backed_paths.add(target)
            elif kind == "pipe":
                pipe_read += value
            elif kind == "socket":
                socket_read += value
            elif kind == "other":
                other_read += value
            else:
                unknown_read += value
        if record.name in _WRITE_SYSCALLS:
            value = _nonnegative_return(record)
            returned_write += value
            ordinal = 1 if record.name in {"splice", "copy_file_range"} else 0
            kind, target = _fd_kind(record, ordinal=ordinal)
            if target and kind == "path_backed":
                path_backed_write += value
                path_backed_paths.add(target)
            elif kind == "pipe":
                pipe_write += value
            elif kind == "socket":
                socket_write += value
            elif kind == "other":
                other_write += value
            else:
                unknown_write += value
    return {
        "syscall_count": len(records),
        "completed_syscall_count": sum(record.state in {"completed", "resumed", "failure", "timeout"} for record in records),
        "failed_syscall_count": failed,
        "timeout_syscall_count": timeout,
        "unfinished_syscall_count": unfinished,
        "resumed_syscall_count": resumed,
        "syscall_counts": dict(sorted(syscall_counts.items())),
        "returned_read_bytes": returned_read,
        "returned_write_bytes": returned_write,
        # A -yy path annotation does not contain inode mode evidence.  Keep
        # the legacy regular-file keys explicit and unavailable rather than
        # claiming that /dev, /proc, a tty, or a directory is a regular file.
        "regular_file_read_bytes": None,
        "regular_file_write_bytes": None,
        "path_backed_read_bytes": path_backed_read,
        "path_backed_write_bytes": path_backed_write,
        "pipe_read_bytes": pipe_read,
        "pipe_write_bytes": pipe_write,
        "socket_read_bytes": socket_read,
        "socket_write_bytes": socket_write,
        "other_descriptor_read_bytes": other_read,
        "other_descriptor_write_bytes": other_write,
        "unknown_descriptor_read_bytes": unknown_read,
        "unknown_descriptor_write_bytes": unknown_write,
        "received_message_count": received_message_count,
        "sent_message_count": sent_message_count,
        "getdents_bytes": getdents_bytes,
        "getdents_count": getdents_count,
        "open_count": open_count,
        "stat_count": stat_count,
        "fork_count": fork_count,
        "exec_count": exec_count,
        "thread_count": thread_count,
        "distinct_observed_paths": sorted(observed_paths),
        "path_backed_paths": sorted(path_backed_paths),
        "regular_file_paths": [],
        "directory_paths": sorted(directory_paths),
        "path_count": len(observed_paths),
        "regular_file_path_count": None,
        "path_backed_path_count": len(path_backed_paths),
        "directory_path_count": len(directory_paths),
        "work_semantics": {
            "returned_syscall_bytes": "kernel return values, split only when -yy identifies descriptor targets",
            "regular_file_bytes": "unavailable: -yy path annotations do not prove inode mode; no regular-file bytes are claimed",
            "path_backed_bytes": "read/write returns for path-backed descriptors; may include regular files, directories, devices, procfs, or ttys",
            "pipe_socket_bytes": "read/write returns for pipe/socket descriptors observed in -yy output",
            "unknown_descriptor_bytes": "returned bytes whose descriptor target was not identified; never assigned to disk",
            "message_syscalls": "recvmmsg/sendmmsg returns are message counts, kept separate from byte totals",
            "splice_copy_file_range": "source and destination descriptors are classified independently",
            "distinct_observed_paths": "paths present in traced syscall arguments or -yy descriptor annotations; no paths inferred from action text",
        },
    }


def _short_path_hash(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha256_path(path)}


def _load_boundaries(path: Path, identity: ProcessIdentity) -> list[ActionBoundary]:
    return BoundaryJournal(path, identity).boundaries()


def summarize_trace(
    records: Sequence[SyscallRecord],
    boundaries: Sequence[ActionBoundary],
    *,
    identity: ProcessIdentity,
    raw_trace_files: Sequence[Path] = (),
    parser_diagnostics: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Derive action-level summaries from preserved raw records and boundaries."""

    actions: list[dict[str, Any]] = []
    for boundary in boundaries:
        if boundary.end_wall_ns is None:
            raise LinuxWorkError(f"cannot summarize unclosed action: {boundary.event_id}")
        selected = _records_for_interval(records, boundary.start_wall_ns, boundary.end_wall_ns)
        work = _aggregate_syscalls(selected)
        work["trace_record_count"] = len(selected)
        work["trace_pids"] = sorted({record.pid for record in selected})
        work["interval_overlap_policy"] = "record interval intersects explicit action [start_wall_ns,end_wall_ns]"
        proc = _proc_delta(boundary.start_snapshot, boundary.end_snapshot)
        actions.append(
            {
                "event_id": boundary.event_id,
                "command": boundary.command,
                "command_sha256": boundary.command_sha256,
                "status": boundary.status,
                "timeout": boundary.timeout or boundary.status == "timeout",
                "error": boundary.error,
                "start_wall_ns": boundary.start_wall_ns,
                "end_wall_ns": boundary.end_wall_ns,
                "start_mono_ns": boundary.start_mono_ns,
                "end_mono_ns": boundary.end_mono_ns,
                "duration_ms": (boundary.end_wall_ns - boundary.start_wall_ns) / 1_000_000,
                "identity_binding": identity.to_mapping(),
                "work": work,
                "proc": proc,
            }
        )
    trace_hashes = [_short_path_hash(Path(path)) for path in raw_trace_files if Path(path).is_file()]
    return {
        "schema_version": SUMMARY_SCHEMA,
        "collector_schema": COLLECTOR_SCHEMA,
        "trace_format": "strace -ff -ttt -T -yy",
        "trace_clock": {
            "clock_id": TRACE_CLOCK_ID,
            "clock_source": TRACE_CLOCK_SOURCE,
            "unit": "nanoseconds",
        },
        "identity": identity.to_mapping(),
        "identity_binding_digest": identity.binding_digest(),
        "actions": actions,
        "action_count": len(actions),
        "raw_trace_files": trace_hashes,
        "parser_diagnostics": [dict(item) for item in parser_diagnostics],
        "limitations": [
            "strace attach can miss syscalls before attach and after a detached/failed collector; raw diagnostics are retained.",
            "-yy descriptor annotations distinguish regular paths from pipes/sockets only when the kernel/strace exposes them.",
            "returned syscall bytes are not physical disk bytes; /proc read_bytes/write_bytes are reported separately.",
            "process children that exit before an end snapshot may be absent from /proc deltas even though their raw strace files remain.",
        ],
    }


def _safe_output_dir(path: Path) -> Path:
    path = Path(path).expanduser()
    if path.is_symlink():
        raise LinuxWorkError(f"output directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise LinuxWorkError(f"output directory is not a regular directory: {path}")
    return path


def _reap_owned_process(
    process: subprocess.Popen[str],
    *,
    request_signal: bool,
    timeout_s: float = 1.0,
) -> str:
    """Stop, reap, and close an owned helper process after attach failure."""

    if request_signal and process.poll() is None:
        with contextlib.suppress(OSError):
            process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.terminate()
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=timeout_s)
    stderr = ""
    stream = process.stderr
    if stream is not None:
        try:
            with contextlib.suppress(Exception):
                stderr = stream.read()
        finally:
            with contextlib.suppress(Exception):
                stream.close()
    return stderr


class LinuxWorkCollector:
    """Attach strace to an existing target and own action boundaries."""

    def __init__(
        self,
        *,
        identity: ProcessIdentity,
        trace_dir: Path,
        strace_process: subprocess.Popen[str] | None,
        manifest_path: Path,
        boundary_journal: BoundaryJournal,
        trace_prefix: Path,
        attach_stderr: str = "",
    ):
        self.identity = identity
        self.trace_dir = Path(trace_dir)
        self.strace_process = strace_process
        self.manifest_path = Path(manifest_path)
        self.boundary_journal = boundary_journal
        self.trace_prefix = Path(trace_prefix)
        self.attach_stderr = attach_stderr
        self._closed = False

    @classmethod
    def attach(
        cls,
        target: ProcessTarget,
        trace_dir: Path,
        *,
        strace_path: str | None = None,
        attach_timeout_s: float = 2.0,
        force: bool = False,
        enable_perf: bool = False,
    ) -> "LinuxWorkCollector":
        """Attach to *target.pid* without starting a replacement shell."""

        if os.name != "posix" or sys.platform != "linux":
            raise CollectorAttachError("Linux work collector requires Linux")
        if not math.isfinite(float(attach_timeout_s)) or attach_timeout_s <= 0:
            raise CollectorAttachError("attach_timeout_s must be positive")
        trace_dir = _safe_output_dir(Path(trace_dir))
        manifest_path = trace_dir / "collector_manifest.json"
        boundary_path = trace_dir / "action_boundaries.jsonl"
        if not force and (manifest_path.exists() or boundary_path.exists()):
            raise CollectorAttachError(
                f"refusing to reuse collector directory without force: {trace_dir}"
            )
        identity = ProcessIdentity.capture(target)
        executable = strace_path or shutil.which("strace")
        if not executable:
            raise CollectorAttachError("strace executable is unavailable; no work is reported")
        if "\x00" in executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
            raise CollectorAttachError(f"strace executable is not executable: {executable}")
        trace_prefix = trace_dir / "strace"
        command = [
            executable,
            "-ff",
            "-ttt",
            "-T",
            "-yy",
            "-e",
            "trace=%file,%desc,%process",
            "-p",
            str(identity.pid),
            "-o",
            str(trace_prefix),
        ]
        try:
            process: subprocess.Popen[str] = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CollectorAttachError(f"cannot start strace attach: {exc}") from exc
        deadline = time.monotonic() + attach_timeout_s
        attached = False
        while time.monotonic() < deadline:
            code = process.poll()
            if code is not None:
                stderr = _reap_owned_process(process, request_signal=False)
                raise CollectorAttachError(
                    f"strace attach failed with exit code {code}: {stderr.strip() or 'no stderr'}"
                )
            try:
                identity.assert_current()
                tracer_pid = _read_tracer_pid(identity.pid)
            except IdentityBindingError as exc:
                _reap_owned_process(process, request_signal=True)
                raise CollectorAttachError(
                    f"target identity/status disappeared during strace attach: {exc}"
                ) from exc
            if tracer_pid == process.pid:
                attached = True
                break
            if tracer_pid is not None:
                _reap_owned_process(process, request_signal=True)
                raise CollectorAttachError(
                    f"target PID {identity.pid} is already traced by PID {tracer_pid}; refusing mixed trace"
                )
            time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))
        if not attached:
            stderr = _reap_owned_process(process, request_signal=True)
            raise CollectorAttachError(
                "strace attach did not receive kernel TracerPid acknowledgement within "
                f"{attach_timeout_s:.3f}s: {stderr.strip() or 'permission denied or ptrace unavailable'}"
            )
        try:
            boundary = BoundaryJournal(boundary_path, identity)
            session_id = hashlib.sha256(
                f"{identity.binding_digest()}\0{time.time_ns()}".encode("utf-8")
            ).hexdigest()[:32]
            manifest = {
                "schema_version": COLLECTOR_SCHEMA,
                "session_id": session_id,
                "status": "attached",
                "identity": identity.to_mapping(),
                "trace_format": "strace -ff -ttt -T -yy",
                "trace_clock": {
                    "clock_id": TRACE_CLOCK_ID,
                    "clock_source": TRACE_CLOCK_SOURCE,
                    "unit": "seconds in raw files; nanoseconds in boundaries/summary",
                },
                "trace_dir": str(trace_dir),
                "trace_prefix": str(trace_prefix),
                "boundary_journal": str(boundary_path),
                "strace_pid": process.pid,
                "strace_pid_binding": _capture_pid_binding(process.pid),
                "strace_command": command,
                "strace_command_sha256": _sha256_text(_canonical_json(command)),
                "attached_wall_ns": time.time_ns(),
                "attached_mono_ns": time.monotonic_ns(),
                **_wall_mono_fields(),
                "perf": {
                    "requested": bool(enable_perf),
                    "status": "not_started",
                    "reason": "optional perf collection is not enabled by default",
                },
            }
            if manifest_path.exists() and not force:
                raise CollectorAttachError(
                    f"refusing to overwrite collector manifest: {manifest_path}"
                )
            manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
        except Exception:
            _reap_owned_process(process, request_signal=True)
            raise
        return cls(
            identity=identity,
            trace_dir=trace_dir,
            strace_process=process,
            manifest_path=manifest_path,
            boundary_journal=boundary,
            trace_prefix=trace_prefix,
        )

    @classmethod
    def from_session(cls, session_path: Path) -> "LinuxWorkCollector":
        """Open a stopped/session manifest for boundary or parse operations."""

        session_path = Path(session_path)
        try:
            manifest = json.loads(session_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LinuxWorkError(f"cannot read collector manifest: {session_path}") from exc
        if manifest.get("schema_version") != COLLECTOR_SCHEMA:
            raise LinuxWorkError("unsupported collector manifest schema")
        identity = ProcessIdentity.from_mapping(manifest.get("identity", {}))
        trace_dir = Path(manifest["trace_dir"])
        boundary_path = Path(manifest["boundary_journal"])
        return cls(
            identity=identity,
            trace_dir=trace_dir,
            strace_process=None,
            manifest_path=session_path,
            boundary_journal=BoundaryJournal(boundary_path, identity),
            trace_prefix=Path(manifest["trace_prefix"]),
        )

    def _assert_open(self) -> None:
        if self._closed:
            raise LinuxWorkError("collector is closed")
        self.identity.assert_current()

    def start_action(
        self,
        event_id: str,
        command: str,
        *,
        start_wall_ns: int | None = None,
        start_mono_ns: int | None = None,
    ) -> ActionBoundary:
        self._assert_open()
        snapshot = _snapshot_process_tree(self.identity)
        return self.boundary_journal.start(
            event_id,
            command,
            start_wall_ns=start_wall_ns,
            start_mono_ns=start_mono_ns,
            snapshot=snapshot,
        )

    def end_action(
        self,
        event_id: str,
        *,
        status: str,
        end_wall_ns: int | None = None,
        end_mono_ns: int | None = None,
        timeout: bool = False,
        error: str | None = None,
    ) -> ActionBoundary:
        if self._closed:
            raise LinuxWorkError("collector is closed")
        snapshot: Mapping[str, Any] | None
        try:
            snapshot = _snapshot_process_tree(self.identity)
        except IdentityBindingError as exc:
            snapshot = {"availability": "unavailable", "reason": str(exc)}
            if status == "success":
                status = "unavailable"
                error = error or str(exc)
        return self.boundary_journal.end(
            event_id,
            status=status,
            end_wall_ns=end_wall_ns,
            end_mono_ns=end_mono_ns,
            timeout=timeout,
            error=error,
            snapshot=snapshot,
        )

    def _stop_strace(self, timeout_s: float = 3.0) -> dict[str, Any]:
        process = self.strace_process
        if process is None:
            return {"status": "already_detached", "returncode": None, "stderr": self.attach_stderr}
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        process.kill()
                    process.wait(timeout=1.0)
        stderr = ""
        if process.stderr is not None:
            with contextlib.suppress(Exception):
                stderr = process.stderr.read()
        return {
            "status": "detached" if process.returncode in (0, 130, -signal.SIGINT) else "failed",
            "returncode": process.returncode,
            "stderr": stderr,
            "detached_wall_ns": time.time_ns(),
            "detached_mono_ns": time.monotonic_ns(),
        }

    def _write_manifest_update(self, values: Mapping[str, Any]) -> None:
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LinuxWorkError(f"cannot update collector manifest: {self.manifest_path}") from exc
        manifest.update(dict(values))
        self.manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")

    def close(self, *, timeout_s: float = 3.0) -> dict[str, Any]:
        if self._closed:
            return self.summarize()
        for event_id in self.boundary_journal.active_event_ids:
            with contextlib.suppress(Exception):
                self.end_action(
                    event_id,
                    status="incomplete",
                    error="collector closed before an action terminal boundary",
                )
        stop = self._stop_strace(timeout_s=timeout_s)
        self._closed = True
        self._write_manifest_update({"status": "closed", "detach": stop})
        summary = self.summarize()
        summary_path = self.trace_dir / "work_summary.json"
        summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8")
        return summary

    def raw_trace_files(self) -> list[Path]:
        return sorted(
            path
            for path in self.trace_dir.glob("strace.*")
            if path.is_file() and not path.is_symlink()
        )

    def summarize(self) -> dict[str, Any]:
        trace_files = self.raw_trace_files()
        parsed = parse_strace_files(trace_files)
        boundaries = self.boundary_journal.boundaries()
        return summarize_trace(
            parsed.records,
            boundaries,
            identity=self.identity,
            raw_trace_files=trace_files,
            parser_diagnostics=parsed.diagnostics,
        )


class LinuxWorkHookAdapter:
    """Small callback adapter for instrumentation_resume's action hooks.

    The adapter intentionally accepts the actual command supplied by the
    runtime hook.  It never reconstructs a command from a plan or from a
    precomputed work row.
    """

    def __init__(self, collector: LinuxWorkCollector):
        self.collector = collector

    def on_action_started(
        self,
        *,
        event_id: str,
        command: str,
        start_wall_ns: int | None = None,
        start_mono_ns: int | None = None,
    ) -> ActionBoundary:
        return self.collector.start_action(
            event_id,
            command,
            start_wall_ns=start_wall_ns,
            start_mono_ns=start_mono_ns,
        )

    def on_action_executed(
        self,
        *,
        event_id: str,
        status: str,
        end_wall_ns: int | None = None,
        end_mono_ns: int | None = None,
        timeout: bool = False,
        error: str | None = None,
    ) -> ActionBoundary:
        return self.collector.end_action(
            event_id,
            status=status,
            end_wall_ns=end_wall_ns,
            end_mono_ns=end_mono_ns,
            timeout=timeout,
            error=error,
        )


@dataclass(frozen=True)
class FixtureMeasurement:
    mode: str
    repeat: int
    status: str
    returncode: int | None
    wall_ms: float | None
    cpu_user_ms: float | None
    cpu_system_ms: float | None
    work_summary: Mapping[str, Any] | None
    error: str | None = None
    setup_wall_ms: float | None = None
    execution_wall_ms: float | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "repeat": self.repeat,
            "status": self.status,
            "returncode": self.returncode,
            "wall_ms": self.wall_ms,
            "cpu_user_ms": self.cpu_user_ms,
            "cpu_system_ms": self.cpu_system_ms,
            "work_summary": self.work_summary,
            "error": self.error,
            "setup_wall_ms": self.setup_wall_ms,
            "execution_wall_ms": self.execution_wall_ms,
        }


def _child_rusage_delta(before: Any, after: Any) -> tuple[float, float]:
    return (
        max(0.0, (float(after.ru_utime) - float(before.ru_utime)) * 1000.0),
        max(0.0, (float(after.ru_stime) - float(before.ru_stime)) * 1000.0),
    )


_FIXTURE_GATE_CODE = (
    "import os,sys; "
    "os.write(int(os.environ['_LINUX_WORK_READY_FD']),b'R'); "
    "os.read(int(os.environ['_LINUX_WORK_GATE_FD']),1); "
    "os.execvp(sys.argv[1],sys.argv[1:])"
)


def _spawn_gated_fixture(command: Sequence[str]) -> tuple[subprocess.Popen[bytes], int, int]:
    """Start a direct-exec fixture paused until the observer has attached.

    The helper is a short-lived Python gate that ``execvp``s the requested
    argv in the same PID.  It gives the collector a real pre-action boundary
    while preserving the command's argv and avoiding a new shell wrapper.
    """

    ready_read, ready_write = os.pipe()
    gate_read, gate_write = os.pipe()
    environment = os.environ.copy()
    environment.update(
        {
            "_LINUX_WORK_READY_FD": str(ready_write),
            "_LINUX_WORK_GATE_FD": str(gate_read),
        }
    )
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", _FIXTURE_GATE_CODE, *command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            pass_fds=(ready_write, gate_read),
        )
    except (OSError, subprocess.SubprocessError):
        os.close(ready_read)
        os.close(ready_write)
        os.close(gate_read)
        os.close(gate_write)
        raise
    os.close(ready_write)
    os.close(gate_read)
    return process, ready_read, gate_write


def _await_fixture_ready(process: subprocess.Popen[bytes], ready_read: int, timeout_s: float) -> None:
    ready, _, _ = select.select([ready_read], [], [], min(timeout_s, 5.0))
    if not ready:
        raise LinuxWorkError("CPU fixture did not reach its pre-action ready barrier")
    value = os.read(ready_read, 1)
    if value != b"R":
        raise LinuxWorkError(
            f"CPU fixture exited before ready barrier (returncode={process.poll()!r})"
        )


def _release_fixture(gate_write: int) -> None:
    os.write(gate_write, b"G")
    os.close(gate_write)


def _run_fixture_once(
    command: Sequence[str],
    *,
    mode: str,
    repeat: int,
    trace_root: Path,
    strace_path: str | None,
    timeout_s: float,
) -> FixtureMeasurement:
    if not command or any(not isinstance(part, str) or not part or "\x00" in part for part in command):
        raise LinuxWorkError("fixture command must be a non-empty argv without NUL")
    import resource

    before_rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter_ns()
    process, ready_read, gate_write = _spawn_gated_fixture(command)
    collector: LinuxWorkCollector | None = None
    error: str | None = None
    summary: Mapping[str, Any] | None = None
    status = "success"
    ready_ns: int | None = None
    released_ns: int | None = None
    finished_ns: int | None = None
    try:
        try:
            _await_fixture_ready(process, ready_read, timeout_s)
            ready_ns = time.perf_counter_ns()
        except (LinuxWorkError, OSError) as exc:
            status = "unavailable"
            error = f"fixture_ready:{type(exc).__name__}:{exc}"
        if status == "success" and mode == "instrumented":
            target = ProcessTarget(
                pid=process.pid,
                run_id=f"fixture-{repeat}",
                attempt_id=f"{mode}-{repeat}",
                case_id=f"cpu-fixture-{repeat}",
                mapping_source="overhead_fixture_direct_pid",
            )
            try:
                collector = LinuxWorkCollector.attach(
                    target,
                    trace_root / f"{mode}-{repeat}",
                    strace_path=strace_path,
                )
                collector.start_action(
                    f"fixture-{repeat}",
                    shlex.join(list(command)),
                )
            except (LinuxWorkError, OSError) as exc:
                status = "unavailable"
                error = f"collector_attach:{type(exc).__name__}:{exc}"
        if status == "success":
            try:
                _release_fixture(gate_write)
                gate_write = -1
                released_ns = time.perf_counter_ns()
            except OSError as exc:
                status = "unavailable"
                error = f"fixture_release:{type(exc).__name__}:{exc}"
        elif gate_write >= 0:
            # Let a failed attach terminate/return naturally so the paired
            # row records the real fixture status instead of waiting on a
            # barrier that can no longer be observed.
            with contextlib.suppress(OSError):
                _release_fixture(gate_write)
            gate_write = -1
            released_ns = time.perf_counter_ns()
        try:
            process.wait(timeout=timeout_s)
            finished_ns = time.perf_counter_ns()
        except subprocess.TimeoutExpired:
            status = "timeout"
            error = error or "fixture_timeout"
            with contextlib.suppress(OSError):
                process.kill()
            process.wait(timeout=2.0)
            finished_ns = time.perf_counter_ns()
        if process.returncode != 0 and status == "success":
            status = "failure"
            error = f"fixture_exit:{process.returncode}"
        if collector is not None:
            if collector.boundary_journal.active_event_ids:
                with contextlib.suppress(LinuxWorkError):
                    collector.end_action(
                        f"fixture-{repeat}",
                        status=status,
                        timeout=status == "timeout",
                        error=error,
                    )
            summary = collector.close()
    finally:
        with contextlib.suppress(OSError):
            os.close(ready_read)
        if gate_write >= 0:
            with contextlib.suppress(OSError):
                _release_fixture(gate_write)
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
    after_rusage = resource.getrusage(resource.RUSAGE_CHILDREN)
    user_ms, system_ms = _child_rusage_delta(before_rusage, after_rusage)
    return FixtureMeasurement(
        mode=mode,
        repeat=repeat,
        status=status,
        returncode=process.returncode,
        wall_ms=(time.perf_counter_ns() - started) / 1_000_000,
        cpu_user_ms=user_ms,
        cpu_system_ms=system_ms,
        work_summary=summary,
        error=error,
        setup_wall_ms=(released_ns - ready_ns) / 1_000_000 if ready_ns is not None and released_ns is not None else None,
        execution_wall_ms=(finished_ns - released_ns) / 1_000_000 if finished_ns is not None and released_ns is not None else None,
    )


def measure_probe_overhead(
    command: Sequence[str],
    *,
    repeats: int = 3,
    output_dir: Path,
    strace_path: str | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Run paired control/instrumented CPU fixtures and measure probe overhead."""

    if isinstance(command, (str, bytes)) or not command:
        raise LinuxWorkError("overhead command must be an argv sequence")
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats <= 0:
        raise LinuxWorkError("repeats must be a positive integer")
    if not math.isfinite(float(timeout_s)) or timeout_s <= 0:
        raise LinuxWorkError("timeout_s must be positive")
    output_dir = _safe_output_dir(Path(output_dir))
    rows: list[FixtureMeasurement] = []
    for repeat in range(1, repeats + 1):
        rows.append(
            _run_fixture_once(
                command,
                mode="control",
                repeat=repeat,
                trace_root=output_dir,
                strace_path=strace_path,
                timeout_s=timeout_s,
            )
        )
        rows.append(
            _run_fixture_once(
                command,
                mode="instrumented",
                repeat=repeat,
                trace_root=output_dir,
                strace_path=strace_path,
                timeout_s=timeout_s,
            )
        )
    controls = [row for row in rows if row.mode == "control" and row.status == "success"]
    instrumented = [row for row in rows if row.mode == "instrumented" and row.status == "success"]
    paired: list[dict[str, Any]] = []
    for repeat in range(1, repeats + 1):
        control = next(row for row in rows if row.mode == "control" and row.repeat == repeat)
        measured = next(row for row in rows if row.mode == "instrumented" and row.repeat == repeat)
        paired.append(
            {
                "repeat": repeat,
                "status": "measured" if control.status == measured.status == "success" else "unavailable",
                "control_wall_ms": control.wall_ms if control.status == "success" else None,
                "instrumented_wall_ms": measured.wall_ms if measured.status == "success" else None,
                "control_setup_wall_ms": control.setup_wall_ms if control.status == "success" else None,
                "instrumented_setup_wall_ms": measured.setup_wall_ms if measured.status == "success" else None,
                "setup_delta_ms": (
                    measured.setup_wall_ms - control.setup_wall_ms
                    if control.setup_wall_ms is not None and measured.setup_wall_ms is not None
                    and measured.status == "success" and control.status == "success"
                    else None
                ),
                "control_execution_wall_ms": control.execution_wall_ms if control.status == "success" else None,
                "instrumented_execution_wall_ms": measured.execution_wall_ms if measured.status == "success" else None,
                "wall_delta_ms": (
                    measured.wall_ms - control.wall_ms
                    if control.wall_ms is not None and measured.wall_ms is not None and measured.status == "success" and control.status == "success"
                    else None
                ),
                "control": control.to_mapping(),
                "instrumented": measured.to_mapping(),
            }
        )
    return {
        "schema_version": OVERHEAD_SCHEMA,
        "fixture_command": list(command),
        "fixture_command_sha256": _sha256_text("\0".join(command)),
        "repeats": repeats,
        "paired": paired,
        "control_success_count": len(controls),
        "instrumented_success_count": len(instrumented),
        "instrumented_attach_failures": [row.error for row in rows if row.mode == "instrumented" and row.status == "unavailable"],
        "status": "measured" if len(controls) == repeats and len(instrumented) == repeats else "unavailable",
        "limitations": [
            "Control and instrumented fixtures are separate process invocations; compare paired distributions, not a single run.",
            "The ready barrier releases the action only after TracerPid acknowledgement; setup activity before the action boundary is outside the action trace, and a later detach or failure can leave gaps.",
            "No zero or synthetic instrumented value is emitted when attach permissions or strace are unavailable.",
        ],
    }


def _trace_paths(trace_dir: Path) -> list[Path]:
    return sorted(path for path in trace_dir.glob("strace.*") if path.is_file() and not path.is_symlink())


def stop_session(session_path: Path, *, timeout_s: float = 3.0) -> dict[str, Any]:
    """Detach a CLI-created session and write its derived summary."""

    collector = LinuxWorkCollector.from_session(session_path)
    try:
        manifest = json.loads(Path(session_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LinuxWorkError(f"cannot read session manifest: {session_path}") from exc
    strace_pid = manifest.get("strace_pid")
    stop: dict[str, Any]
    if isinstance(strace_pid, int) and not isinstance(strace_pid, bool) and strace_pid > 0:
        binding = manifest.get("strace_pid_binding")
        if not isinstance(binding, Mapping):
            raise CollectorAttachError(
                "manifest lacks strace PID identity binding; refusing to signal an unbound PID"
            )
        if binding.get("pid") != strace_pid:
            raise CollectorAttachError(
                "manifest strace PID does not match its persisted identity binding; "
                "refusing to signal an unbound PID"
            )
        try:
            _assert_pid_binding(binding, role="strace")
        except IdentityBindingError as exc:
            raise CollectorAttachError(
                f"strace PID identity validation failed; refusing to signal PID {strace_pid}: {exc}"
            ) from exc
        try:
            os.kill(strace_pid, signal.SIGINT)
        except ProcessLookupError:
            stop = {"status": "already_detached", "returncode": None}
        except PermissionError as exc:
            raise CollectorAttachError(
                f"permission denied while detaching strace PID {strace_pid}; raw evidence remains open"
            ) from exc
        else:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    os.kill(strace_pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError as exc:
                    raise CollectorAttachError(f"cannot verify strace PID ownership: {strace_pid}") from exc
                time.sleep(0.02)
            stop = {"status": "detached", "strace_pid": strace_pid, "detached_wall_ns": time.time_ns()}
    else:
        stop = {"status": "unavailable", "reason": "manifest lacks strace_pid"}
    manifest.update({"status": "closed", "detach": stop})
    Path(session_path).write_text(_canonical_json(manifest) + "\n", encoding="utf-8")
    boundaries = collector.boundary_journal.boundaries()
    parsed = parse_strace_files(_trace_paths(collector.trace_dir))
    summary = summarize_trace(
        parsed.records,
        boundaries,
        identity=collector.identity,
        raw_trace_files=_trace_paths(collector.trace_dir),
        parser_diagnostics=parsed.diagnostics,
    )
    summary_path = collector.trace_dir / "work_summary.json"
    summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8")
    return summary


def _session_target(args: argparse.Namespace) -> ProcessTarget:
    return ProcessTarget(
        pid=args.pid,
        run_id=args.run_id,
        attempt_id=args.attempt_id,
        case_id=args.case_id,
        instance_id=args.instance_id,
        container_pid=args.container_pid,
        pid_namespace=args.pid_namespace,
        mapping_source=args.mapping_source,
    )


def _load_manifest_identity(session_path: Path) -> tuple[dict[str, Any], ProcessIdentity]:
    try:
        manifest = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LinuxWorkError(f"cannot read collector manifest: {session_path}") from exc
    if manifest.get("schema_version") != COLLECTOR_SCHEMA:
        raise LinuxWorkError("unsupported collector manifest schema")
    return manifest, ProcessIdentity.from_mapping(manifest.get("identity", {}))


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="subcommand", required=True)
    attach = sub.add_parser("attach", help="attach strace to an existing persistent PID")
    attach.add_argument("--pid", type=int, required=True)
    attach.add_argument("--trace-dir", type=Path, required=True)
    attach.add_argument("--run-id", required=True)
    attach.add_argument("--attempt-id", required=True)
    attach.add_argument("--case-id", required=True)
    attach.add_argument("--instance-id")
    attach.add_argument("--container-pid", type=int)
    attach.add_argument("--pid-namespace")
    attach.add_argument("--mapping-source", default="explicit_cli_pid_mapping")
    attach.add_argument("--strace-path")
    attach.add_argument("--attach-timeout-s", type=float, default=2.0)
    attach.add_argument("--force", action="store_true")
    attach.add_argument("--enable-perf", action="store_true")

    for name in ("start-action", "end-action"):
        action = sub.add_parser(name)
        action.add_argument("--session", type=Path, required=True)
        action.add_argument("--event-id", required=True)
        action.add_argument("--wall-ns", type=int)
        action.add_argument("--mono-ns", type=int)
        if name == "start-action":
            action.add_argument("--command", required=True)
        else:
            action.add_argument("--status", choices=sorted(VALID_ACTION_STATUSES - {"incomplete"}), required=True)
            action.add_argument("--timeout", action="store_true")
            action.add_argument("--error")

    stop = sub.add_parser("stop", help="detach a session and derive work_summary.json")
    stop.add_argument("--session", type=Path, required=True)
    stop.add_argument("--timeout-s", type=float, default=3.0)

    parse = sub.add_parser("parse", help="parse retained raw trace and boundaries")
    parse.add_argument("--session", type=Path, required=True)
    parse.add_argument("--output", type=Path)

    overhead = sub.add_parser("overhead", help="measure paired CPU fixture probe overhead")
    overhead.add_argument("--output-dir", type=Path, required=True)
    overhead.add_argument("--repeats", type=int, default=3)
    overhead.add_argument("--timeout-s", type=float, default=60.0)
    overhead.add_argument("--strace-path")
    overhead.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "attach":
            collector = LinuxWorkCollector.attach(
                _session_target(args),
                args.trace_dir,
                strace_path=args.strace_path,
                attach_timeout_s=args.attach_timeout_s,
                force=args.force,
                enable_perf=args.enable_perf,
            )
            print(collector.manifest_path.read_text(encoding="utf-8"), end="")
            return 0
        if args.subcommand in {"start-action", "end-action"}:
            collector = LinuxWorkCollector.from_session(args.session)
            if args.subcommand == "start-action":
                row = collector.start_action(
                    args.event_id,
                    args.command,
                    start_wall_ns=args.wall_ns,
                    start_mono_ns=args.mono_ns,
                )
            else:
                row = collector.end_action(
                    args.event_id,
                    status=args.status,
                    end_wall_ns=args.wall_ns,
                    end_mono_ns=args.mono_ns,
                    timeout=args.timeout,
                    error=args.error,
                )
            print(_canonical_json(row.to_mapping(collector.identity)))
            return 0
        if args.subcommand == "stop":
            print(_canonical_json(stop_session(args.session, timeout_s=args.timeout_s)))
            return 0
        if args.subcommand == "parse":
            collector = LinuxWorkCollector.from_session(args.session)
            summary = collector.summarize()
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(_canonical_json(summary) + "\n", encoding="utf-8")
            print(_canonical_json(summary))
            return 0
        if args.subcommand == "overhead":
            command = list(args.command)
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                parser.error("overhead requires a fixture command after --")
            print(
                _canonical_json(
                    measure_probe_overhead(
                        command,
                        repeats=args.repeats,
                        output_dir=args.output_dir,
                        strace_path=args.strace_path,
                        timeout_s=args.timeout_s,
                    )
                )
            )
            return 0
    except (LinuxWorkError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    parser.error("unhandled subcommand")
    return 2


__all__ = [
    "ActionBoundary",
    "BOUNDARY_SCHEMA",
    "COLLECTOR_SCHEMA",
    "CollectorAttachError",
    "FixtureMeasurement",
    "IdentityBindingError",
    "LinuxWorkCollector",
    "LinuxWorkError",
    "LinuxWorkHookAdapter",
    "OVERHEAD_SCHEMA",
    "ProcessIdentity",
    "ProcessTarget",
    "RAW_TRACE_SCHEMA",
    "SUMMARY_SCHEMA",
    "SyscallRecord",
    "TraceParseError",
    "TraceParseResult",
    "measure_probe_overhead",
    "parse_strace_file",
    "parse_strace_files",
    "parse_strace_lines",
    "stop_session",
    "summarize_trace",
]


if __name__ == "__main__":  # pragma: no cover - CLI exercised in subprocess tests
    raise SystemExit(main())
