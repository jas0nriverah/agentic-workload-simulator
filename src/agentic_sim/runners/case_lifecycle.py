"""Bounded process and case-deadline helpers.

The assignment runner is intentionally small, but the processes it starts are
not necessarily small process trees.  SWE-agent and the official evaluator
may create a worker which calls ``setsid()`` or which outlives its direct
parent after that parent receives ``SIGTERM``.  A process-group kill alone is
therefore insufficient.  This module keeps a start-time-bound registry of a
single owned process tree and, on Linux, uses a dedicated child subreaper so
orphaned descendants remain observable until they can be reaped.

No process is selected by a name, an instance ID, or a system-wide PID
substring.  Every signal is guarded by the ``/proc/<pid>/stat`` start time
captured for that process. Only the dedicated supervisor adopts unknown
orphans; it starts exactly one workload and has no unrelated subprocesses.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping, Sequence


# Public API shared by the outer matrix, case runner, and evaluator.  The
# value is an absolute reading of CLOCK_MONOTONIC (nanoseconds), not a duration
# and not wall-clock epoch seconds.
CASE_DEADLINE_ENV = "ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS"
# Stable owner identity shared by the case runner, SWE-ReX Docker arguments,
# and evaluator cleanup.  The outer case runner supplies a fresh value for
# each case/attempt; this helper only defines the wire name.
CASE_OWNER_ENV = "ASSIGNMENT_CASE_OWNER"
# The runner's supervisor is itself a Python process.  Keep the opt-in v2
# sitecustomize hook out of that helper and strip this marker before spawning
# the actual workload, where the activation handshake is required.
TELEMETRY_V2_SUPERVISOR_ENV = "ASSIGNMENT_TELEMETRY_V2_SUPERVISOR"
_DEDICATED_SUPERVISOR = False
_STOP_SIGNAL: int | None = None


class LifecycleError(ValueError):
    """A deadline or process-ownership value cannot be trusted."""


@dataclass(frozen=True)
class _ProcIdentity:
    pid: int
    starttime: int


@dataclass(frozen=True)
class ProcessOutcome:
    """Result of one owned subprocess and its bounded cleanup."""

    returncode: int
    timed_out: bool
    deadline_mono_ns: int | None
    started_mono_ns: int
    ended_mono_ns: int
    cleanup: Mapping[str, Any]


def parse_deadline(value: Any, *, label: str = CASE_DEADLINE_ENV) -> int:
    """Parse a positive absolute monotonic deadline without coercion."""

    if isinstance(value, bool):
        raise LifecycleError(f"{label} must be a positive integer")
    if isinstance(value, int):
        deadline = value
    elif isinstance(value, str) and value.isdecimal():
        deadline = int(value)
    else:
        raise LifecycleError(f"{label} must be a positive integer")
    if deadline <= 0:
        raise LifecycleError(f"{label} must be a positive integer")
    return deadline


def deadline_from_env(
    env: Mapping[str, str] | None = None,
    *,
    required: bool = False,
) -> int | None:
    """Read the inherited case deadline, optionally requiring propagation."""

    values = os.environ if env is None else env
    raw = values.get(CASE_DEADLINE_ENV)
    if raw is None:
        if required:
            raise LifecycleError(f"{CASE_DEADLINE_ENV} is required")
        return None
    return parse_deadline(raw)


def remaining_seconds(deadline_mono_ns: int, *, now_mono_ns: int | None = None) -> float:
    """Return non-negative seconds remaining until an absolute deadline."""

    deadline = parse_deadline(deadline_mono_ns, label="deadline_mono_ns")
    now = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
    if not isinstance(now, int) or isinstance(now, bool):
        raise LifecycleError("now_mono_ns must be an integer")
    return max(0.0, (deadline - now) / 1_000_000_000)


def deadline_with_timeout(timeout_seconds: float | int) -> int:
    """Create an absolute deadline for compatibility-only local callers."""

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise LifecycleError("timeout_seconds must be positive")
    if timeout_seconds <= 0:
        raise LifecycleError("timeout_seconds must be positive")
    return time.monotonic_ns() + int(float(timeout_seconds) * 1_000_000_000)


@contextmanager
def deadline_environment(
    deadline_mono_ns: int,
    env: MutableMapping[str, str] | None = None,
) -> Iterator[MutableMapping[str, str]]:
    """Temporarily bind ``CASE_DEADLINE_ENV`` in a mutable environment.

    The matrix uses this around exactly one child launch.  Restoring the
    process environment matters for tests and for any later non-case helper
    invoked by the same matrix process.
    """

    target = os.environ if env is None else env
    deadline = parse_deadline(deadline_mono_ns, label="deadline_mono_ns")
    had_value = CASE_DEADLINE_ENV in target
    previous = target.get(CASE_DEADLINE_ENV)
    target[CASE_DEADLINE_ENV] = str(deadline)
    try:
        yield target
    finally:
        if had_value:
            assert previous is not None
            target[CASE_DEADLINE_ENV] = previous
        else:
            target.pop(CASE_DEADLINE_ENV, None)


_SUBREAPER_ATTEMPTED = False
_SUBREAPER_ENABLED = False


def ensure_linux_subreaper() -> bool:
    """Request Linux child-subreaper semantics once per supervisor process."""

    global _SUBREAPER_ATTEMPTED, _SUBREAPER_ENABLED
    if _SUBREAPER_ATTEMPTED:
        return _SUBREAPER_ENABLED
    _SUBREAPER_ATTEMPTED = True
    if not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        # PR_SET_CHILD_SUBREAPER is 36 on Linux.  Keeping the constant local
        # avoids a dependency on a platform-specific Python package.
        if prctl(36, 1, 0, 0, 0) == 0:
            _SUBREAPER_ENABLED = True
    except (AttributeError, OSError, TypeError):
        _SUBREAPER_ENABLED = False
    return _SUBREAPER_ENABLED


def _read_proc_identity(pid: int) -> tuple[_ProcIdentity, int, int] | None:
    """Read ``(identity, parent_pid, process_group_id)`` for one PID."""

    if pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return None
    # comm may contain spaces and parentheses; the final ')' is the end of
    # comm for the kernel's stat representation.
    marker = text.rfind(")")
    if marker < 0:
        return None
    fields = text[marker + 2 :].split()
    # fields[0] is state (field 3); ppid is field 4, pgrp field 5, and
    # starttime field 22 -> index 19 in this suffix.
    if len(fields) <= 19:
        return None
    try:
        return (
            _ProcIdentity(pid, int(fields[19])),
            int(fields[1]),
            int(fields[2]),
        )
    except (TypeError, ValueError):
        return None


def _proc_is_zombie(pid: int) -> bool:
    """Return whether a still-listed process has already exited."""

    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return False
    marker = text.rfind(")")
    if marker < 0:
        return False
    fields = text[marker + 2 :].split()
    return bool(fields) and fields[0] == "Z"


def _read_children(pid: int) -> list[tuple[_ProcIdentity, int, int]]:
    """Read direct children from Linux's targeted children file."""

    if not sys.platform.startswith("linux"):
        return []
    try:
        raw = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    except (FileNotFoundError, PermissionError, OSError, UnicodeError):
        return []
    children: list[tuple[_ProcIdentity, int, int]] = []
    for token in raw.split():
        try:
            child_pid = int(token)
        except ValueError:
            continue
        record = _read_proc_identity(child_pid)
        if record is not None:
            children.append(record)
    return children


class OwnedProcessTree:
    """Track and terminate descendants of one explicitly spawned process."""

    def __init__(self, process: subprocess.Popen[Any], *, started_mono_ns: int | None = None) -> None:
        self.process = process
        root_record = _read_proc_identity(int(process.pid))
        if root_record is None:
            # A process can exit between Popen and /proc lookup.  Its PID is
            # still retained as a waitable root, but no unverified descendants
            # will ever be signalled.
            root = _ProcIdentity(int(process.pid), -1)
        else:
            root = root_record[0]
        self.root = root
        self.supervisor_pid = os.getpid()
        self.started_mono_ns = time.monotonic_ns() if started_mono_ns is None else started_mono_ns
        self._known: dict[int, _ProcIdentity] = {root.pid: root}
        self._preexisting_supervisor_children = set() if _DEDICATED_SUPERVISOR else {
            identity
            for identity, _, _ in _read_children(self.supervisor_pid)
        }
        self._observed = False

    @property
    def known(self) -> tuple[_ProcIdentity, ...]:
        return tuple(self._known.values())

    def _record(self, identity: _ProcIdentity) -> bool:
        prior = self._known.get(identity.pid)
        if prior is not None and prior.starttime != identity.starttime:
            return False
        self._known[identity.pid] = identity
        return prior is None

    def observe(self) -> tuple[_ProcIdentity, ...]:
        """Discover descendants through targeted parent-child relationships.

        If an owned parent exits on TERM, Linux reparents its descendants to
        this subreaper.  Newly adopted children are accepted only when they
        were not supervisor children before launch and their kernel start time
        is no earlier than the owned root.  This is an explicit launch-time
        ownership proof, not a system-wide name or PID scan.
        """

        if not sys.platform.startswith("linux"):
            return self.known
        queue = list(self._known.values())
        visited: set[tuple[int, int]] = set()
        while queue:
            parent = queue.pop()
            marker = (parent.pid, parent.starttime)
            if marker in visited:
                continue
            visited.add(marker)
            current = _read_proc_identity(parent.pid)
            if current is None or current[0].starttime != parent.starttime:
                continue
            for child, child_ppid, _ in _read_children(parent.pid):
                if child_ppid != parent.pid:
                    continue
                if self._record(child):
                    queue.append(child)
                elif self._known.get(child.pid) == child:
                    queue.append(child)

        # Descendants whose parent exited are direct children of this
        # subreaper.  Only identities observed in the owned ancestry are
        # eligible here.  We deliberately do not claim every new child of the
        # supervisor: doing so would make a concurrent, unrelated helper
        # indistinguishable from an orphaned grandchild.  Callers observe at
        # launch and throughout cleanup, while the owned parent is still
        # present, so descendants that become adopted on TERM are registered
        # before they lose their ancestry.
        for child, child_ppid, _ in _read_children(self.supervisor_pid):
            if child_ppid != self.supervisor_pid or child in self._preexisting_supervisor_children:
                continue
            if child.pid == self.root.pid and child.starttime == self.root.starttime:
                self._record(child)
                continue
            # The root's direct child is already registered by the recursive
            # walk above.  A different supervisor child is intentionally not
            # adopted unless it was observed as a descendant earlier.
            if _DEDICATED_SUPERVISOR:
                # This executable launches exactly one workload. Every child
                # adopted by its subreaper therefore belongs to that workload,
                # including a double-fork completed before our first scan.
                self._record(child)
                queue.append(child)
            elif self._known.get(child.pid) == child:
                queue.append(child)

        # Follow any newly adopted children and their own descendants.
        while queue:
            parent = queue.pop()
            marker = (parent.pid, parent.starttime)
            if marker in visited:
                continue
            visited.add(marker)
            current = _read_proc_identity(parent.pid)
            if current is None or current[0].starttime != parent.starttime:
                continue
            for child, child_ppid, _ in _read_children(parent.pid):
                if child_ppid == parent.pid and self._record(child):
                    queue.append(child)
        self._observed = True
        return self.known

    def _alive(self, identity: _ProcIdentity) -> bool:
        current = _read_proc_identity(identity.pid)
        return (
            current is not None
            and current[0].starttime == identity.starttime
            and not _proc_is_zombie(identity.pid)
        )

    def alive(self) -> tuple[_ProcIdentity, ...]:
        self.observe()
        return tuple(identity for identity in self._known.values() if self._alive(identity))

    def signal(self, signum: signal.Signals) -> dict[str, int]:
        """Signal only start-time-matching owned identities."""

        self.observe()
        attempted = 0
        sent = 0
        for identity in self.known:
            if not self._alive(identity):
                continue
            attempted += 1
            descriptor: int | None = None
            try:
                # Pin the process before the final start-time check: a PID
                # recycled between /proc lookup and signal cannot be targeted.
                descriptor = os.pidfd_open(identity.pid)
                if not self._alive(identity):
                    continue
                signal.pidfd_send_signal(descriptor, signum)
                sent += 1
            except (AttributeError, ProcessLookupError, PermissionError, OSError):
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        return {"attempted": attempted, "sent": sent}

    def _reap_known(self) -> int:
        reaped = 0
        for identity in self.known:
            # Popen owns wait/status bookkeeping for the direct root.  Calling
            # os.waitpid on it first would make a later Popen.wait() observe a
            # synthetic zero status on some Python versions.  Reap only
            # descendants adopted by this subreaper here.
            if identity.pid == self.root.pid and identity.starttime == self.root.starttime:
                continue
            try:
                waited, _ = os.waitpid(identity.pid, os.WNOHANG)
            except (ChildProcessError, ProcessLookupError, PermissionError, OSError):
                continue
            if waited == identity.pid:
                reaped += 1
        return reaped

    def terminate(
        self,
        *,
        term_grace_seconds: float = 2.0,
        kill_grace_seconds: float = 2.0,
    ) -> Mapping[str, Any]:
        """Bound cleanup with TERM, then KILL, retaining survivor evidence."""

        if term_grace_seconds < 0 or kill_grace_seconds < 0:
            raise LifecycleError("cleanup grace periods must be non-negative")
        self.observe()
        term = self.signal(signal.SIGTERM)
        term_deadline = time.monotonic() + term_grace_seconds
        while time.monotonic() < term_deadline:
            self.observe()
            self._reap_known()
            if not self.alive():
                break
            time.sleep(min(0.05, max(0.0, term_deadline - time.monotonic())))
        self.observe()
        survivors_after_term = self.alive()
        kill = {"attempted": 0, "sent": 0}
        if survivors_after_term:
            kill = self.signal(signal.SIGKILL)
            kill_deadline = time.monotonic() + kill_grace_seconds
            while time.monotonic() < kill_deadline:
                self.observe()
                self._reap_known()
                if not self.alive():
                    break
                time.sleep(min(0.05, max(0.0, kill_deadline - time.monotonic())))
        self._reap_known()
        survivors = self.alive()
        # Ensure the Popen child is reaped even if /proc disappeared first.
        try:
            self.process.wait(timeout=max(0.0, kill_grace_seconds))
        except subprocess.TimeoutExpired:
            pass
        return {
            "subreaper": _SUBREAPER_ENABLED,
            "known_processes": len(self._known),
            "term_attempted": term["attempted"],
            "term_sent": term["sent"],
            "kill_attempted": kill["attempted"],
            "kill_sent": kill["sent"],
            "survivors": [identity.pid for identity in survivors],
            "survivor_starttimes": [identity.starttime for identity in survivors],
            "cleanup_complete": not survivors,
        }


def _run_owned_process_inner(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: Any = None,
    stderr: Any = None,
    timeout_seconds: float | int | None = None,
    deadline_mono_ns: int | None = None,
    term_grace_seconds: float = 2.0,
    kill_grace_seconds: float = 2.0,
) -> ProcessOutcome:
    """Run one process and clean its owned tree by an absolute deadline.

    ``deadline_mono_ns`` is authoritative when supplied.  ``timeout_seconds``
    remains a compatibility cap for direct callers and can never extend that
    inherited deadline.
    """

    if deadline_mono_ns is not None:
        deadline = parse_deadline(deadline_mono_ns, label="deadline_mono_ns")
    elif timeout_seconds is not None:
        deadline = deadline_with_timeout(timeout_seconds)
    else:
        raise LifecycleError("an absolute deadline or timeout_seconds is required")
    if timeout_seconds is not None:
        local_deadline = deadline_with_timeout(timeout_seconds)
        # A local cap is relative to launch, while the inherited case deadline
        # remains the single upper bound visible to nested children.
        deadline = min(deadline, local_deadline)

    if not ensure_linux_subreaper():
        raise LifecycleError("owned process supervision requires Linux child-subreaper support")
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise LifecycleError("owned process supervision requires pidfd signal support")
    launch = time.monotonic_ns()
    process = subprocess.Popen(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
    )
    tree = OwnedProcessTree(process, started_mono_ns=launch)
    tree.observe()
    timed_out = False
    remaining = remaining_seconds(deadline)
    if remaining <= 0:
        timed_out = True
        cleanup = tree.terminate(term_grace_seconds=term_grace_seconds, kill_grace_seconds=kill_grace_seconds)
    else:
        try:
            while True:
                tree.observe()
                remaining = remaining_seconds(deadline)
                if remaining <= 0 or _STOP_SIGNAL is not None:
                    raise subprocess.TimeoutExpired(argv, remaining)
                try:
                    process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup = tree.terminate(term_grace_seconds=term_grace_seconds, kill_grace_seconds=kill_grace_seconds)
        except BaseException:
            tree.terminate(term_grace_seconds=term_grace_seconds, kill_grace_seconds=kill_grace_seconds)
            raise
        else:
            # A normally exiting parent can leave a detached worker behind;
            # clean it as well, but do not re-signal an empty tree.
            tree.observe()
            if tree.alive():
                cleanup = tree.terminate(term_grace_seconds=term_grace_seconds, kill_grace_seconds=kill_grace_seconds)
            else:
                cleanup = {
                    "subreaper": _SUBREAPER_ENABLED,
                    "known_processes": len(tree.known),
                    "term_attempted": 0,
                    "term_sent": 0,
                    "kill_attempted": 0,
                    "kill_sent": 0,
                    "survivors": [],
                    "survivor_starttimes": [],
                    "cleanup_complete": True,
                }
    # ``process.wait`` may have been interrupted by a child signal; always
    # return the actual code when available while preserving timeout status.
    returncode = process.returncode
    if returncode is None:
        try:
            returncode = process.wait(timeout=max(0.0, kill_grace_seconds))
        except subprocess.TimeoutExpired:
            returncode = -signal.SIGKILL if timed_out else 1
    cleanup = {**cleanup, "termination_signal": _STOP_SIGNAL,
               "workload_returncode": process.returncode}
    if not cleanup.get("cleanup_complete") and returncode == 0:
        returncode = 1
    return ProcessOutcome(
        returncode=int(returncode),
        timed_out=timed_out,
        deadline_mono_ns=deadline,
        started_mono_ns=launch,
        ended_mono_ns=time.monotonic_ns(),
        cleanup=cleanup,
    )


def run_owned_process(
    argv: Sequence[str], *, cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None, stdout: Any = None, stderr: Any = None,
    timeout_seconds: float | int | None = None, deadline_mono_ns: int | None = None,
    term_grace_seconds: float = 2.0, kill_grace_seconds: float = 2.0,
) -> ProcessOutcome:
    """Launch a dedicated subreaper; the caller's other children are never owned.

    The private status descriptor carries only lifecycle metadata, not argv or
    environment values. Workload stdout/stderr go directly to the caller's logs.
    """
    deadline = deadline_mono_ns if deadline_mono_ns is not None else deadline_from_env(env)
    if timeout_seconds is not None:
        cap = deadline_with_timeout(timeout_seconds)
        deadline = min(parse_deadline(deadline), cap) if deadline is not None else cap
    if deadline is None:
        raise LifecycleError("an absolute deadline or timeout_seconds is required")
    deadline = parse_deadline(deadline)
    if min(term_grace_seconds, kill_grace_seconds) < 0:
        raise LifecycleError("cleanup grace periods must be non-negative")
    started = time.monotonic_ns()
    if remaining_seconds(deadline) <= 0:
        return ProcessOutcome(124, True, deadline, started, time.monotonic_ns(), {
            "cleanup_complete": True, "survivors": [], "known_processes": 0,
            "reason": "deadline expired before launch", "launched": False,
        })
    child_env = dict(os.environ if env is None else env)
    child_env[CASE_DEADLINE_ENV] = str(deadline)
    if child_env.get("ASSIGNMENT_TELEMETRY_V2_AUTO") == "1":
        child_env[TELEMETRY_V2_SUPERVISOR_ENV] = "1"
    with tempfile.TemporaryFile(mode="w+b") as status:
        command = [sys.executable, str(Path(__file__).resolve()), "--supervise",
                   str(status.fileno()), str(deadline), str(term_grace_seconds),
                   str(kill_grace_seconds), *map(str, argv)]
        supervisor = subprocess.Popen(command, cwd=cwd, env=child_env,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, pass_fds=(status.fileno(),))
        try:
            supervisor.wait(timeout=remaining_seconds(deadline) + term_grace_seconds + 2 * kill_grace_seconds + 5)
        except subprocess.TimeoutExpired:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=term_grace_seconds + kill_grace_seconds + 1)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait()
        except BaseException:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=term_grace_seconds + 2 * kill_grace_seconds + 2)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait()
            raise
        status.seek(0)
        try:
            payload = json.load(status)
            return ProcessOutcome(**payload)
        except (ValueError, TypeError):
            # A supervisor crash must never be represented as successful
            # cleanup. Its diagnostic stderr remains in the caller's log.
            return ProcessOutcome(supervisor.returncode or 1,
                remaining_seconds(deadline) <= 0, deadline, started, time.monotonic_ns(),
                {"cleanup_complete": False, "survivors": [], "known_processes": 0,
                 "reason": "supervisor outcome unavailable", "survivors_unknown": True})


def _supervisor_main() -> None:
    global _DEDICATED_SUPERVISOR, _STOP_SIGNAL
    _DEDICATED_SUPERVISOR = True
    def stop(signum: int, _frame: Any) -> None:
        global _STOP_SIGNAL
        _STOP_SIGNAL = signum
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    descriptor = int(sys.argv[2])
    try:
        workload_env = dict(os.environ)
        workload_env.pop(TELEMETRY_V2_SUPERVISOR_ENV, None)
        outcome = _run_owned_process_inner(sys.argv[6:],
            deadline_mono_ns=int(sys.argv[3]), term_grace_seconds=float(sys.argv[4]),
            kill_grace_seconds=float(sys.argv[5]), env=workload_env)
    except Exception as exc:
        now = time.monotonic_ns()
        outcome = ProcessOutcome(127, False, int(sys.argv[3]), now, now,
            {"cleanup_complete": False, "survivors": [], "survivors_unknown": True,
             "reason": str(exc), "error_type": type(exc).__name__})
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(asdict(outcome), handle)
        handle.flush()
        os.fsync(handle.fileno())


__all__ = [
    "CASE_OWNER_ENV",
    "CASE_DEADLINE_ENV",
    "LifecycleError",
    "OwnedProcessTree",
    "ProcessOutcome",
    "deadline_environment",
    "deadline_from_env",
    "deadline_with_timeout",
    "ensure_linux_subreaper",
    "parse_deadline",
    "remaining_seconds",
    "run_owned_process",
]

if __name__ == "__main__":
    if len(sys.argv) < 7 or sys.argv[1] != "--supervise":
        raise SystemExit("internal supervisor invocation required")
    _supervisor_main()
