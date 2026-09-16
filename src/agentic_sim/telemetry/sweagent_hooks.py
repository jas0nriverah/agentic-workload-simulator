"""Hooks for the pinned SWE-agent agent/environment callback interfaces.

The hook is optional at import time so the local simulator and offline tests do
not need SWE-agent or SWE-ReX installed.  In a real run it can be added with
``agent.add_hook(SWEAgentTelemetryHook(recorder))`` and the environment hook
with ``env.add_hook(SWEAgentEnvironmentTelemetryHook(recorder))``.  The hook
only observes existing callbacks and wraps no prompt or tool payload.
"""

from __future__ import annotations

import atexit
import asyncio
import contextlib
import functools
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping

from .clock import monotonic_ns
from .features import _operator_segments
from .script_state import ScriptStateLedger
from .v2 import Span, TelemetryV2, stable_id
from .work import CommandProbeBinding, WorkMeasurement, measure_runtime_work


SCRIPT_CONTENT_MAX_BYTES = 256 * 1024
_PERSISTENT_SHELL_SENTINEL = "__ASSIGNMENT_V2_PERSISTENT_SHELL__"
_PID_NAMESPACE_RE = re.compile(r"^pid:\[[0-9]+\]$")
_SCRIPT_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
    }
)
_SCRIPT_SUFFIXES = frozenset(
    {".sh", ".bash", ".zsh", ".py", ".pl", ".pm", ".rb", ".js", ".mjs", ".cjs"}
)


def _is_bash_interrupt_action(action: Any) -> bool:
    """Recognize the pinned SWE-ReX interrupt control action exactly."""

    return (
        type(action).__name__ == "BashInterruptAction"
        and getattr(action, "action_type", None) == "bash_interrupt"
    )


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _script_path_candidates(command: str, *, depth: int = 0) -> list[str]:
    """Find explicit script files in one action without reading the host.

    The parser only identifies a path candidate.  Existence, contents, and
    hashes are obtained later through ``SWEEnv.read_file`` in the container.
    It intentionally ignores pytest/test-file arguments and Python ``-m``
    modules, which are workload descriptors rather than executable scripts.
    """

    if depth > 2:
        return []
    tokens = _shell_tokens(command)
    candidates: list[str] = []
    separators = {"&&", "||", ";", "|"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in separators:
            index += 1
            continue
        name = PurePosixPath(token).name.lower()
        if name in _SCRIPT_INTERPRETERS:
            child: str | None = None
            child_index = index + 1
            while child_index < len(tokens):
                value = tokens[child_index]
                if value in separators:
                    break
                if value in {"-c", "-lc", "-cl", "--command"}:
                    if child_index + 1 < len(tokens):
                        candidates.extend(
                            _script_path_candidates(tokens[child_index + 1], depth=depth + 1)
                        )
                    break
                if value == "-m":
                    # A module import is not a file path that can be read by
                    # the native container-file API at this boundary.
                    break
                if value.startswith("-"):
                    child_index += 1
                    continue
                child = value
                break
            if child is not None and (
                PurePosixPath(child).suffix.lower() in _SCRIPT_SUFFIXES
                or "/" in child
                or child.startswith((".", "~"))
            ):
                candidates.append(child)
            index = max(index + 1, child_index + 1)
            continue
        suffix = PurePosixPath(token).suffix.lower()
        # A directly invoked path is an executable script even if it has no
        # conventional suffix.  Bare ``tests/foo.py`` arguments are only
        # treated as scripts when they are the command word itself.
        if (
            index == 0
            and (token.startswith(("./", "../", "/")) or suffix in _SCRIPT_SUFFIXES)
        ):
            candidates.append(token)
        index += 1
    result: list[str] = []
    for candidate in candidates:
        if candidate not in result:
            result.append(candidate)
    return result

try:  # pragma: no cover - the pinned dependency is absent in most unit runs
    from sweagent.agent.hooks.abstract import AbstractAgentHook as _AgentHook
except ImportError:  # pragma: no cover - exercised by local import tests
    class _AgentHook:  # type: ignore[no-redef]
        pass

try:  # pragma: no cover
    from sweagent.environment.hooks.abstract import EnvHook as _EnvHook
except ImportError:  # pragma: no cover
    class _EnvHook:  # type: ignore[no-redef]
        pass


def _step_value(step: Any, name: str, default: Any = None) -> Any:
    if isinstance(step, Mapping):
        return step.get(name, default)
    return getattr(step, name, default)


def _pid_namespace_link(pid: int) -> str | None:
    """Read the host-visible PID namespace for one live process."""

    try:
        value = os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return None
    return value if _PID_NAMESPACE_RE.fullmatch(value) else None


def _proc_nspid_values(pid: int) -> tuple[int, ...]:
    """Return the kernel's PID values from the host to the deepest namespace."""

    try:
        lines = Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return ()
    for line in lines:
        if not line.startswith("NSpid:"):
            continue
        values: list[int] = []
        for item in line.split()[1:]:
            try:
                parsed = int(item)
            except ValueError:
                return ()
            if parsed <= 0:
                return ()
            values.append(parsed)
        return tuple(values)
    return ()


def _proc_cgroup_membership(pid: int) -> tuple[str, ...]:
    """Return the complete cgroup membership for one host-visible process."""

    try:
        lines = Path(f"/proc/{pid}/cgroup").read_text(
            encoding="ascii", errors="replace"
        ).splitlines()
    except OSError:
        return ()
    memberships = tuple(line.strip() for line in lines if line.strip())
    return memberships


def _has_specific_cgroup(memberships: tuple[str, ...]) -> bool:
    """Require a cgroup path that identifies a delegated container scope."""

    for line in memberships:
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[2].strip() not in {"", "/"}:
            return True
    return False


def _persistent_shell_observation(env: Any, *, session: str, timeout: float) -> tuple[int, str]:
    """Ask the existing SWE-ReX shell for its own PID and namespace.

    The command is read-only and runs through the runtime's persistent bash
    session.  It never uses the Docker CLI helper process as a substitute for
    the shell target.
    """

    command = (
        f"printf '{_PERSISTENT_SHELL_SENTINEL} pid=%s ns=%s\\n' "
        '"$$" "$(readlink /proc/$$/ns/pid)"'
    )
    communicate = getattr(env, "communicate", None)
    # SWEEnv.communicate is hard-wired to the default shell session.  Use it
    # only for that session; a named session must go through SWE-ReX's
    # run_in_session API so the PID witness cannot silently describe another
    # shell.
    if callable(communicate) and session == "default":
        try:
            output = communicate(command, timeout=timeout, check="ignore")
        except TypeError:
            output = communicate(command, timeout=timeout)
    else:
        runtime = getattr(getattr(env, "deployment", None), "runtime", None)
        method = getattr(runtime, "run_in_session", None)
        if not callable(method):
            raise RuntimeError("SWE-ReX runtime has no persistent shell execution API")
        try:
            from swerex.runtime.abstract import BashAction
        except ImportError as exc:
            raise RuntimeError("SWE-ReX BashAction is unavailable for PID discovery") from exc
        response = asyncio.run(
            method(
                BashAction(
                    command=command,
                    session=session,
                    timeout=timeout,
                    check="raise",
                )
            )
        )
        output = getattr(response, "output", None)
    if not isinstance(output, str):
        raise RuntimeError("persistent SWE-ReX shell PID query returned no text")
    matches = re.findall(
        rf"(?m)^{re.escape(_PERSISTENT_SHELL_SENTINEL)} pid=([1-9][0-9]*) ns=(pid:\[[0-9]+\])\s*$",
        output,
    )
    if len(matches) != 1:
        raise RuntimeError("persistent SWE-ReX shell PID query was missing or ambiguous")
    return int(matches[0][0]), matches[0][1]


def _map_container_pid_to_host(
    *,
    container_runtime: str,
    container_name: str,
    container_pid: int,
    pid_namespace: str,
) -> int:
    """Map a persistent container PID through the host's NSpid table."""

    if not isinstance(container_runtime, str) or not container_runtime.strip():
        raise RuntimeError("container runtime executable is unavailable for PID mapping")
    if not isinstance(container_name, str) or not container_name.strip():
        raise RuntimeError("SWE-ReX container name is unavailable for PID mapping")
    if not isinstance(container_pid, int) or isinstance(container_pid, bool) or container_pid <= 0:
        raise RuntimeError("persistent container shell PID is invalid")
    if not isinstance(pid_namespace, str) or not _PID_NAMESPACE_RE.fullmatch(pid_namespace):
        raise RuntimeError("persistent container shell PID namespace is invalid")
    try:
        inspected = subprocess.run(
            [container_runtime, "inspect", "--format", "{{.State.Pid}}", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot inspect SWE-ReX container for PID mapping: {exc}") from exc
    if inspected.returncode != 0:
        detail = inspected.stderr.strip()[-256:]
        raise RuntimeError(f"container PID inspection failed: {detail or inspected.returncode}")
    values = inspected.stdout.split()
    if len(values) != 1:
        raise RuntimeError("container PID inspection returned an ambiguous init PID")
    try:
        init_pid = int(values[0])
    except ValueError as exc:
        raise RuntimeError("container PID inspection returned a non-integer init PID") from exc
    if init_pid <= 0:
        raise RuntimeError("container is not running while mapping its persistent shell")
    init_nspids = _proc_nspid_values(init_pid)
    if len(init_nspids) < 2 or init_nspids[-1] != 1:
        raise RuntimeError("Docker State.Pid is not the container init process")
    init_cgroup = _proc_cgroup_membership(init_pid)
    if not _has_specific_cgroup(init_cgroup):
        raise RuntimeError("container init cgroup membership is not container-specific")
    host_namespace = _pid_namespace_link(init_pid)

    candidates: list[int] = []
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError as exc:
        raise RuntimeError(f"cannot enumerate host processes for PID mapping: {exc}") from exc
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        host_pid = int(entry.name)
        # Docker State.Pid is host-visible, but an unprivileged SWE-agent
        # process may be unable to read /proc/<pid>/ns/pid for the root-owned
        # container.  Cgroup membership is the stable container binding in
        # that case.  If the namespace symlink is readable, retain it as an
        # additional check.
        if _proc_cgroup_membership(host_pid) != init_cgroup:
            continue
        candidate_namespace = _pid_namespace_link(host_pid)
        if candidate_namespace is not None and candidate_namespace != pid_namespace:
            continue
        if host_namespace is not None and candidate_namespace != host_namespace:
            continue
        nspids = _proc_nspid_values(host_pid)
        if len(nspids) >= 2 and nspids[-1] == container_pid:
            candidates.append(host_pid)
    if len(candidates) != 1:
        raise RuntimeError(
            "persistent container shell PID did not map to exactly one host process"
        )
    return candidates[0]


class SWEAgentTelemetryHook(_AgentHook):
    """Observe actual SWE-agent lifecycle/model/action callbacks."""

    def __init__(
        self,
        telemetry: TelemetryV2,
        *,
        script_state: ScriptStateLedger | None = None,
        max_output_tokens: int | None = None,
        owns_outer: bool = True,
        work_collector: Any | None = None,
    ):
        self.telemetry = telemetry
        self.script_state = script_state or ScriptStateLedger()
        self.max_output_tokens = max_output_tokens
        self.owns_outer = owns_outer
        # Optional LinuxWorkCollector adapter.  The collector attaches to the
        # already-running runtime process; it never wraps or reconstructs the
        # command.  Keeping this dependency injected preserves the offline
        # import path while allowing the production runner to require a
        # concrete PID/container mapping.
        self.work_collector = work_collector
        self.agent: Any | None = None
        self._setup_span: Span | None = None
        self._startup_span: Span | None = None
        self._client_span: Span | None = None
        self._model_span: Span | None = None
        self._tool_span: Span | None = None
        self._pending_action: str | None = None
        self._pending_intent: dict[str, Any] | None = None
        self._step_id: int = 0
        self._request_retry_index = 0
        self._last_request_id: str | None = None
        self._last_request_end_ns: int | None = None
        self._action_retry_index = 0
        self._last_action_id: str | None = None
        self._step_active = False
        self._client_interval_index = 0
        self._actual_action: str | None = None
        self._runtime_command: str | None = None
        self._runtime_start_ns: int | None = None
        self._runtime_end_ns: int | None = None
        self._runtime_exit_code: int | None = None
        self._runtime_exit_code_observed = False
        self._runtime_timeout = False
        self._runtime_error: tuple[str, str] | None = None
        self._runtime_work = WorkMeasurement(
            bytes_read=None,
            bytes_written=None,
            files_touched=None,
            subprocess_count=None,
            availability={
                "bytes_read": "unavailable",
                "bytes_written": "unavailable",
                "files_touched": "unavailable",
                "subprocess_count": "unavailable",
            },
            source=None,
            probe=None,
        )
        self._runtime = None
        self._work_collector_event_id: str | None = None
        self._work_collector_error: str | None = None
        self._work_collector_status: str | None = None
        self._work_collector_by_span: dict[str, dict[str, Any]] = {}
        self._work_service: Any | None = None
        self._work_service_socket_dir: Any | None = None
        self._work_env: Any | None = None
        self._work_service_stop_registered = False
        self._aux_span: Span | None = None
        self._aux_runtime_values: dict[str, dict[str, Any]] = {}
        self._target_discovery_span: Span | None = None
        self._wrapped = False

    def _close(self, span: Span | None, *, status: str = "success", **kwargs: Any) -> None:
        if span is None or span.closed:
            return
        try:
            # Tool/model terminal rows must pass through their contract
            # helpers so null measurements and their availability maps remain
            # present even on failures and callback-order fallbacks.
            if span is self._tool_span:
                row = self.telemetry.end_tool(span, status=status, **kwargs)
            elif span is self._model_span:
                row = self.telemetry.end_request(span, status=status, **kwargs)
            else:
                auxiliary = self._aux_runtime_values.pop(span.span_id, {})
                auxiliary.update(kwargs)
                kwargs = auxiliary
                row = span.finish(status=status, **kwargs)
            if span is self._model_span and row.get("end_mono_ns") is not None:
                self._last_request_end_ns = int(row["end_mono_ns"])
        finally:
            if span is self._setup_span:
                self._setup_span = None
            if span is self._startup_span:
                self._startup_span = None
            if span is self._client_span:
                self._client_span = None
            if span is self._model_span:
                self._model_span = None
            if span is self._tool_span:
                self._tool_span = None
            if span is self._aux_span:
                self._aux_span = None

    def _client_parent(self) -> str | None:
        outer = getattr(self.telemetry, "_outer", None)
        return outer.pre_event_id if outer is not None else None

    def _pause_client_processing(self) -> None:
        self._close(self._client_span)

    def _resume_client_processing(self) -> None:
        if not self._step_active or self._client_span is not None:
            return
        self._client_interval_index += 1
        self._client_span = self.telemetry.start_phase(
            "client_processing",
            event_kind="client_processing",
            phase_id=stable_id(
                "client",
                self.telemetry.run_id,
                self.telemetry.attempt_id,
                self._step_id,
                self._client_interval_index,
            ),
            parent_event_id=self._client_parent(),
        )

    def finish_setup(
        self,
        *,
        status: str = "success",
        error_type: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """Close setup/startup around the real ``DefaultAgent.setup`` call.

        The pinned ``CombinedAgentHook.on_setup_done`` currently delegates to
        ``super()`` and does not forward to registered hooks.  The activation
        shim calls this method from a wrapper around the actual setup method,
        keeping its boundaries tied to work that really ran.
        """

        self._close(
            self._startup_span,
            status=status,
            error_type=error_type,
            error_message=error_message,
        )
        self._close(
            self._setup_span,
            status=status,
            error_type=error_type,
            error_message=error_message,
        )

    def _wrap_get_state(self, agent: Any) -> None:
        tools = getattr(agent, "tools", None)
        method = getattr(tools, "get_state", None)
        if tools is None or not callable(method) or getattr(method, "_assignment_v2_wrapped", False):
            return

        @functools.wraps(method)
        def get_state(*args: Any, **kwargs: Any) -> Any:
            had_client = self._client_span is not None
            parent_event_id = (
                self._tool_span.pre_event_id
                if self._tool_span is not None
                else self._client_span.pre_event_id if self._client_span is not None else None
            )
            self._pause_client_processing()
            span = self.telemetry.start_phase(
                "get_state",
                parent_event_id=parent_event_id,
                event_kind="get_state",
            )
            self._wrap_runtime(getattr(self.agent, "_env", None))
            self._aux_span = span
            try:
                result = method(*args, **kwargs)
            except TimeoutError as exc:
                self._close(span, status="timeout", error_type=type(exc).__name__, error_message=str(exc))
                if had_client:
                    self._resume_client_processing()
                raise
            except BaseException as exc:
                self._close(span, status="failure", error_type=type(exc).__name__, error_message=str(exc))
                if had_client:
                    self._resume_client_processing()
                raise
            else:
                self._close(span)
                if had_client:
                    self._resume_client_processing()
                return result
            finally:
                if self._aux_span is span:
                    self._aux_span = None

        get_state._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        tools.get_state = get_state

    def _wrap_model_query(self, agent: Any) -> None:
        model = getattr(agent, "model", None)
        method = getattr(model, "query", None)
        if model is None or not callable(method) or getattr(method, "_assignment_v2_wrapped", False):
            return

        @functools.wraps(method)
        def query(*args: Any, **kwargs: Any) -> Any:
            had_client = self._client_span is not None
            if self._model_span is None:
                self._begin_model_request()
            else:
                self._pause_client_processing()
            config = getattr(model, "config", None)
            completion_kwargs = getattr(config, "completion_kwargs", None)
            previous_headers: Any = None
            headers_installed = False
            if self._model_span is not None and isinstance(completion_kwargs, dict):
                previous_headers = completion_kwargs.get("extra_headers")
                request_headers = dict(previous_headers) if isinstance(previous_headers, Mapping) else {}
                request_headers.update(
                    {
                        "X-EIC-Logical-Request-ID": str(self._model_span.identity.get("logical_request_id")),
                        "X-EIC-Client-Span-ID": self._model_span.span_id,
                        "X-EIC-Parent-Event-ID": str(self._model_span.pre_event_id),
                        "X-EIC-Retry-Index": str(self._model_span.identity.get("retry_index", 0)),
                    }
                )
                retry_of = self._model_span.identity.get("retry_of")
                if retry_of:
                    request_headers["X-EIC-Retry-Of"] = str(retry_of)
                completion_kwargs["extra_headers"] = request_headers
                headers_installed = True
            try:
                result = method(*args, **kwargs)
            except TimeoutError as exc:
                self._close(self._model_span, status="timeout", error_type=type(exc).__name__, error_message=str(exc))
                if had_client or self._step_active:
                    self._resume_client_processing()
                raise
            except BaseException as exc:
                self._close(self._model_span, status="failure", error_type=type(exc).__name__, error_message=str(exc))
                if had_client or self._step_active:
                    self._resume_client_processing()
                raise
            else:
                # The query boundary ends when the model client returns.  Any
                # response parsing/action filtering is represented by the
                # surrounding client_processing span.
                self._close(self._model_span, status="success")
                if had_client or self._step_active:
                    self._resume_client_processing()
            finally:
                if headers_installed and isinstance(completion_kwargs, dict):
                    if previous_headers is None:
                        completion_kwargs.pop("extra_headers", None)
                    else:
                        completion_kwargs["extra_headers"] = previous_headers
            return result

        query._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        model.query = query

    def bind_environment(
        self,
        env: Any,
        *,
        startup_span: Span | None = None,
        runtime_ready: bool = True,
    ) -> None:
        """Bind the agent hook to the environment before its first shell call.

        ``SWEEnv.start`` creates the persistent session and executes its
        initial environment-variable command before ``DefaultAgent.setup``
        dispatches any agent callback.  The environment hook calls this
        method at that earlier boundary so the runtime wrapper and optional
        startup span are already owned by this hook.  It is deliberately
        synchronous; collector bootstrap may perform one measured native
        shell PID query, but never nests ``asyncio.run`` inside the async
        SWE-ReX runtime wrapper.
        """

        if startup_span is not None and self._aux_span is None:
            self._aux_span = startup_span
        # DockerDeployment exposes ``runtime`` only after ``start`` has
        # created the deployment.  The environment lifecycle callback starts
        # before that point, so it binds the owner/span now and defers the
        # property access until the first real persistent-shell command.
        if runtime_ready:
            self._wrap_runtime(env)

    def _wrap_runtime(self, env: Any) -> None:
        """Capture the guarded command's actual SWE-ReX observation.

        ``SWEEnv.communicate`` intentionally returns only stdout and discards
        ``BashObservation.exit_code``.  Wrapping the underlying async
        ``run_in_session`` keeps the command result at the real execution
        boundary and still lets the pinned environment retain its behavior.
        """

        runtime = getattr(getattr(env, "deployment", None), "runtime", None)
        method = getattr(runtime, "run_in_session", None)
        if runtime is None or not callable(method):
            return
        if getattr(method, "_assignment_v2_runtime_wrapper", False):
            self._runtime = runtime
            return

        @functools.wraps(method)
        async def run_in_session(action: Any, *args: Any, **kwargs: Any) -> Any:
            active = self._tool_span or self._aux_span
            command = getattr(action, "command", None)
            required = os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1"
            capture = isinstance(command, str) and (
                active is not None or self.work_collector is not None or required
            )
            collection_suspended = capture and active is not None and active is self._target_discovery_span
            control_span: Span | None = None
            if _is_bash_interrupt_action(action):
                control_span = self.telemetry.start_phase(
                    "tool_execution",
                    event_kind="bash_interrupt_control",
                    parent_event_id=active.pre_event_id if active is not None else self._client_parent(),
                    reason="measured SWE-ReX BashInterruptAction control boundary",
                )

            def close_control(status: str, *, error_type: str | None = None, error_message: str | None = None) -> None:
                if control_span is None:
                    return
                self._close(
                    control_span,
                    status=status,
                    error_type=error_type,
                    error_message=error_message,
                    control_action="bash_interrupt",
                    runtime_action_class=type(action).__name__,
                    runtime_action_type=getattr(action, "action_type", None),
                    session=getattr(action, "session", None),
                    command=None,
                    command_sha256=None,
                    command_identity_available=False,
                )

            started = monotonic_ns() if capture else None
            runtime_span: Span | None = None
            collection_span: Span | None = active
            if capture and not collection_suspended and (active is None or active is not self._tool_span):
                parent_event_id = active.pre_event_id if active is not None else self._client_parent()
                runtime_span = self.telemetry.begin_runtime_command(
                    command,
                    phase="tool_execution",
                    start_mono_ns=started,
                    parent_event_id=parent_event_id,
                    reason="measured auxiliary SWE-ReX command boundary",
                )
                collection_span = runtime_span

            def store_auxiliary(values: dict[str, Any]) -> None:
                span = runtime_span or active
                if span is None or span is self._tool_span:
                    return
                state = self._work_collector_by_span.get(span.span_id, {})
                values.update(
                    {
                        "work_collector_event_id": state.get("event_id"),
                        "work_collector_status": state.get("status"),
                        "work_collector_error": state.get("error"),
                    }
                )
                self._aux_runtime_values[span.span_id] = values

            def close_auxiliary(
                status: str,
                *,
                error_type: str | None = None,
                error_message: str | None = None,
            ) -> None:
                if runtime_span is None:
                    return
                state = self._work_collector_by_span.get(runtime_span.span_id, {})
                self._close(
                    runtime_span,
                    status=status,
                    error_type=error_type,
                    error_message=error_message,
                    work_collector_event_id=state.get("event_id"),
                    work_collector_status=state.get("status"),
                    work_collector_error=state.get("error"),
                )

            def end_collection(
                status: str,
                *,
                end_mono_ns: int | None,
                error: str | None,
            ) -> None:
                """End the collector and terminalize a child on end failure.

                In required mode ``_end_work_collection`` deliberately
                raises when the service cannot finalize an action.  The
                runtime child must nevertheless be closed with the original
                command identity and an explicit unavailable collector state;
                otherwise a saved journal contains an unjoinable pending
                start row and loses the setup command that caused the failure.
                """

                if collection_suspended:
                    return
                try:
                    self._end_work_collection(
                        span=collection_span,
                        status=status,
                        end_mono_ns=end_mono_ns,
                        error=error,
                    )
                except BaseException as exc:
                    if runtime_span is not None:
                        if runtime_span.span_id not in self._aux_runtime_values:
                            store_auxiliary(
                                {
                                    "runtime_command": command,
                                    "runtime_command_start_mono_ns": started,
                                    "runtime_command_end_mono_ns": end_mono_ns,
                                    "command_exit_code": self._runtime_exit_code,
                                    "command_exit_code_availability": (
                                        "measured"
                                        if self._runtime_exit_code_observed
                                        else "unavailable"
                                    ),
                                    "command_timeout": bool(self._runtime_timeout),
                                    "runtime_error": (
                                        self._runtime_error[0]
                                        if self._runtime_error
                                        else None
                                    ),
                                    "bytes_read": None,
                                    "bytes_written": None,
                                    "files_touched": None,
                                    "subprocess_count": None,
                                    "work_volume_source": None,
                                    "work_volume_probe": None,
                                    "work_volume_reason": (
                                        "collector action finalization failed"
                                    ),
                                    "work_probe_binding": None,
                                    "runtime_child_telemetry": False,
                                }
                            )
                        close_auxiliary(
                            "failure",
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    raise

            if capture:
                # Runtime calls are the authoritative command boundary.  The
                # callback can arrive before SWE-ReX has actually dispatched
                # the BashAction, so start the collector here too when an
                # auxiliary state/setup call was not announced by a tool
                # callback.  A per-span map makes this idempotent for normal
                # tool callbacks, which already start before communicate().
                if not collection_suspended:
                    try:
                        self._start_work_collection(collection_span, command, start_mono_ns=started)
                    except BaseException as exc:
                        close_auxiliary(
                            "failure",
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                        raise
                self._runtime_command = command
                self._runtime_start_ns = started
                self._runtime_end_ns = None
                self._runtime_exit_code = None
                self._runtime_exit_code_observed = False
                self._runtime_timeout = False
                self._runtime_error = None
                self._runtime_work = WorkMeasurement(
                    bytes_read=None,
                    bytes_written=None,
                    files_touched=None,
                    subprocess_count=None,
                    availability={
                        "bytes_read": "unavailable",
                        "bytes_written": "unavailable",
                        "files_touched": "unavailable",
                        "subprocess_count": "unavailable",
                    },
                    source=None,
                    probe=None,
                )
            try:
                result = await method(action, *args, **kwargs)
            except BaseException as exc:
                if capture:
                    self._runtime_end_ns = monotonic_ns()
                    if type(exc).__name__ == "CommandTimeoutError" or "timeout" in str(exc).lower():
                        self._runtime_timeout = True
                    else:
                        self._runtime_error = (type(exc).__name__, str(exc))
                    status = "timeout" if self._runtime_timeout else "failure"
                    if not collection_suspended:
                        end_collection(
                            status,
                            end_mono_ns=self._runtime_end_ns,
                            error=type(exc).__name__,
                        )
                    store_auxiliary({
                            "runtime_command": command,
                            "runtime_command_start_mono_ns": started,
                            "runtime_command_end_mono_ns": self._runtime_end_ns,
                            "command_exit_code": None,
                            "command_exit_code_availability": "unavailable",
                            "command_timeout": self._runtime_timeout,
                            "runtime_error": self._runtime_error[0] if self._runtime_error else type(exc).__name__,
                            "bytes_read": None,
                            "bytes_written": None,
                            "files_touched": None,
                            "subprocess_count": None,
                            "work_volume_source": None,
                            "work_volume_probe": None,
                            "work_volume_reason": "runtime child/work probe is unavailable",
                            "work_probe_binding": None,
                            "runtime_child_telemetry": False,
                            "work_collection_status": (
                                "suspended_for_target_discovery"
                                if collection_suspended
                                else None
                            ),
                        })
                    close_auxiliary(
                        status,
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                close_control(
                    "timeout" if type(exc).__name__ == "CommandTimeoutError" else "failure",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                raise
            else:
                if capture:
                    self._runtime_end_ns = monotonic_ns()
                    exit_code = getattr(result, "exit_code", None)
                    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                        self._runtime_exit_code = exit_code
                        self._runtime_exit_code_observed = True
                    if collection_suspended and active is not None and started is not None and self._runtime_end_ns is not None:
                        store_auxiliary({
                            "runtime_command": command,
                            "runtime_command_start_mono_ns": started,
                            "runtime_command_end_mono_ns": self._runtime_end_ns,
                            "command_exit_code": self._runtime_exit_code,
                            "command_exit_code_availability": "measured" if self._runtime_exit_code_observed else "unavailable",
                            "command_timeout": False,
                            "runtime_error": None,
                            "bytes_read": None,
                            "bytes_written": None,
                            "files_touched": None,
                            "subprocess_count": None,
                            "work_volume_source": None,
                            "work_volume_probe": None,
                            "work_volume_reason": "CPU collector suspended during target discovery",
                            "work_probe_binding": None,
                            "runtime_child_telemetry": False,
                            "work_collection_status": "suspended_for_target_discovery",
                        })
                    elif started is not None and self._runtime_end_ns is not None:
                        binding = CommandProbeBinding(
                            event_id=(collection_span.pre_event_id if collection_span is not None else self._client_parent()) or "unbound",
                            command=command,
                            start_mono_ns=started,
                            end_mono_ns=self._runtime_end_ns,
                        )
                        measurement = measure_runtime_work(
                            result,
                            runtime,
                            binding=binding,
                        )
                        # ``None is None`` is not a tool-span match: setup,
                        # startup, and post-startup environment commands use
                        # their own runtime child span and must retain the
                        # observed work fields on that lifecycle row.
                        if self._tool_span is not None and active is self._tool_span:
                            self._runtime_work = measurement
                        else:
                            store_auxiliary({
                                "runtime_command": command,
                                "runtime_command_start_mono_ns": started,
                                "runtime_command_end_mono_ns": self._runtime_end_ns,
                                "command_exit_code": self._runtime_exit_code,
                                "command_exit_code_availability": "measured" if self._runtime_exit_code_observed else "unavailable",
                                "command_timeout": False,
                                "bytes_read": measurement.bytes_read,
                                "bytes_written": measurement.bytes_written,
                                "files_touched": measurement.files_touched,
                                "subprocess_count": measurement.subprocess_count,
                                "work_volume_source": measurement.source,
                                "work_volume_probe": measurement.probe,
                                "work_volume_reason": measurement.reason,
                                "work_probe_binding": dict(measurement.binding) if measurement.binding else None,
                                "runtime_child_telemetry": measurement.source is not None,
                            })
                    status = "unavailable"
                    if self._runtime_timeout:
                        status = "timeout"
                    elif self._runtime_exit_code_observed:
                        status = "success" if self._runtime_exit_code == 0 else "failure"
                    if not collection_suspended:
                        end_collection(
                            status,
                            end_mono_ns=self._runtime_end_ns,
                            error=(
                                f"exit_code={self._runtime_exit_code}"
                                if self._runtime_exit_code_observed and self._runtime_exit_code != 0
                                else None
                            ),
                        )
                    close_auxiliary(status)
                close_control("success")
                return result

        run_in_session._assignment_v2_runtime_wrapper = True  # type: ignore[attr-defined]
        runtime.run_in_session = run_in_session
        self._runtime = runtime

    def _guarded_action(self, action: str) -> str:
        tools = getattr(self.agent, "tools", None)
        guard = getattr(tools, "guard_multiline_input", None)
        if callable(guard):
            try:
                guarded = guard(action)
            except BaseException:
                # The actual SWE-agent call will report the same guard error;
                # retain the original text and let the tool span close as an
                # unavailable command outcome.
                return action.strip()
            if isinstance(guarded, str):
                return guarded.strip()
        return action.strip()

    def _start_work_collection(
        self,
        span: Span,
        command: str,
        *,
        start_mono_ns: int | None = None,
    ) -> None:
        collector = self.work_collector
        if collector is None:
            if os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
                raise RuntimeError("required v2 CPU collector is not active at action start")
            return
        event_id = span.pre_event_id or span.span_id
        existing = self._work_collector_by_span.get(span.span_id)
        if existing is not None:
            # A tool callback and the underlying runtime wrapper observe the
            # same physical command.  Do not allocate two BPF tokens.
            self._work_collector_event_id = existing.get("event_id")
            self._work_collector_status = existing.get("status")
            self._work_collector_error = existing.get("error")
            return
        local_error: str | None = None
        try:
            collector.start_action(
                event_id=event_id,
                command=command,
                start_mono_ns=start_mono_ns if start_mono_ns is not None else span.start_mono_ns,
            )
        except TypeError:
            # The callback adapter in the standalone collector accepts the
            # same keyword contract; this fallback keeps compatibility with a
            # narrowly implemented injected adapter.
            try:
                collector.start_action(
                    event_id,
                    command,
                    start_mono_ns=start_mono_ns if start_mono_ns is not None else span.start_mono_ns,
                )
            except BaseException as exc:
                local_error = f"{type(exc).__name__}: {exc}"[:256]
        except BaseException as exc:
            local_error = f"{type(exc).__name__}: {exc}"[:256]
        if local_error is not None:
            self._work_collector_by_span[span.span_id] = {
                "event_id": None,
                "status": "unavailable",
                "error": local_error,
            }
            if span is self._tool_span:
                self._work_collector_event_id = None
                self._work_collector_status = "unavailable"
                self._work_collector_error = local_error
            if os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
                raise RuntimeError(f"required v2 CPU collector failed to start an action: {local_error}")
            return
        self._work_collector_by_span[span.span_id] = {
            "event_id": event_id,
            "status": "started",
            "error": None,
        }
        self._work_collector_event_id = event_id
        self._work_collector_status = "started"

    def _end_work_collection(
        self,
        *,
        span: Span | None = None,
        status: str,
        end_mono_ns: int | None,
        error: str | None,
    ) -> None:
        collector = self.work_collector
        span_id = span.span_id if span is not None else (self._tool_span.span_id if self._tool_span else None)
        state = self._work_collector_by_span.get(span_id) if span_id is not None else None
        if state is not None and state.get("status") in {"ended", "unavailable"}:
            if span is self._tool_span or span is None:
                self._work_collector_event_id = state.get("event_id")
                self._work_collector_status = state.get("status")
                self._work_collector_error = state.get("error")
            return
        event_id = state.get("event_id") if state is not None else self._work_collector_event_id
        if collector is None or event_id is None:
            if state is not None:
                state.update({"status": "unavailable", "error": state.get("error") or "collector event was not started"})
            if os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
                raise RuntimeError("required v2 CPU collector did not have a live action token at action end")
            return
        try:
            try:
                collector.end_action(
                    event_id=event_id,
                    status=status,
                    end_mono_ns=end_mono_ns,
                    timeout=status == "timeout",
                    error=error,
                )
            except TypeError:
                collector.end_action(
                    event_id,
                    status=status,
                    end_mono_ns=end_mono_ns,
                    timeout=status == "timeout",
                    error=error,
                )
            if state is None:
                state = {"event_id": event_id}
                if span_id is not None:
                    self._work_collector_by_span[span_id] = state
            state.update({"status": "ended", "error": None})
            if span is self._tool_span or span is None:
                self._work_collector_event_id = event_id
                self._work_collector_status = "ended"
                self._work_collector_error = None
        except BaseException as exc:
            message = f"{type(exc).__name__}: {exc}"[:256]
            if state is None:
                state = {"event_id": event_id}
                if span_id is not None:
                    self._work_collector_by_span[span_id] = state
            state.update({"status": "unavailable", "error": message})
            if span is self._tool_span or span is None:
                self._work_collector_status = "unavailable"
                self._work_collector_error = message
            if os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
                raise RuntimeError(f"required v2 CPU collector failed to end an action: {message}") from exc

    @staticmethod
    def _load_work_config() -> dict[str, Any] | None:
        raw = os.environ.get("ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG")
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"v2 CPU collector config is not valid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("v2 CPU collector config must be a JSON object")
        return value

    def _target_from_mapping(self, value: Mapping[str, Any], *, source: str) -> Any:
        from .linux_work import ProcessTarget

        host_pid = value.get("host_pid", value.get("pid"))
        if isinstance(host_pid, bool) or not isinstance(host_pid, int) or host_pid <= 0:
            raise RuntimeError(f"v2 CPU collector {source} has no positive host PID")
        container_pid = value.get("container_pid")
        if container_pid is not None and (
            isinstance(container_pid, bool) or not isinstance(container_pid, int) or container_pid <= 0
        ):
            raise RuntimeError(f"v2 CPU collector {source} container_pid is invalid")
        namespace = value.get("pid_namespace")
        if namespace is not None and not isinstance(namespace, str):
            raise RuntimeError(f"v2 CPU collector {source} pid_namespace is invalid")
        mapping_source = value.get("mapping_source", source)
        if not isinstance(mapping_source, str) or not mapping_source:
            raise RuntimeError(f"v2 CPU collector {source} mapping_source is invalid")
        return ProcessTarget(
            pid=host_pid,
            run_id=self.telemetry.run_id,
            attempt_id=self.telemetry.attempt_id,
            case_id=self.telemetry.case_id or "unknown-case",
            instance_id=self.telemetry.instance_id,
            container_pid=container_pid,
            pid_namespace=namespace,
            mapping_source=mapping_source,
        )

    def _process_target(
        self,
        *,
        host_pid: int,
        container_pid: int | None,
        pid_namespace: str | None,
        mapping_source: str,
    ) -> Any:
        from .linux_work import ProcessTarget

        return ProcessTarget(
            pid=host_pid,
            run_id=self.telemetry.run_id,
            attempt_id=self.telemetry.attempt_id,
            case_id=self.telemetry.case_id or "unknown-case",
            instance_id=self.telemetry.instance_id,
            container_pid=container_pid,
            pid_namespace=pid_namespace,
            mapping_source=mapping_source,
        )

    @staticmethod
    def _persist_process_target(env: Any, target: Any) -> None:
        """Make the resolved mapping visible to later hooks and finalizers."""

        to_mapping = getattr(target, "to_mapping", None)
        mapping = dict(to_mapping()) if callable(to_mapping) else {}
        mapping["host_pid"] = target.pid
        for owner in (env, getattr(env, "deployment", None)):
            if owner is not None:
                setattr(owner, "_assignment_v2_process_target", dict(mapping))

    @contextlib.contextmanager
    def _target_discovery_scope(self):
        """Measure the one internal shell query needed before collector boot."""

        parent_event_id = (
            self._aux_span.pre_event_id
            if self._aux_span is not None
            else self._client_parent()
        )
        span = self.telemetry.start_phase(
            "state_query",
            event_kind="persistent_shell_pid_discovery",
            parent_event_id=parent_event_id,
            reason="resolve the live persistent SWE-ReX shell target before required BPF startup",
        )
        previous_aux_span = self._aux_span
        previous_discovery_span = self._target_discovery_span
        self._aux_span = span
        self._target_discovery_span = span
        try:
            yield span
        except TimeoutError as exc:
            self._close(
                span,
                status="timeout",
                error_type=type(exc).__name__,
                error_message=str(exc),
                target_discovery=True,
                collection_suspended=True,
                cpu_action_required=False,
            )
            raise
        except BaseException as exc:
            self._close(
                span,
                status="failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
                target_discovery=True,
                collection_suspended=True,
                cpu_action_required=False,
            )
            raise
        else:
            self._close(
                span,
                status="success",
                target_discovery=True,
                collection_suspended=True,
                cpu_action_required=False,
            )
        finally:
            if self._target_discovery_span is span:
                self._target_discovery_span = previous_discovery_span
                if self._aux_span is None or self._aux_span is span:
                    self._aux_span = previous_aux_span

    def _resolve_work_target(self, config: Mapping[str, Any], env: Any) -> Any:
        explicit = config.get("target") or config.get("process_target")
        if isinstance(explicit, Mapping):
            target = self._target_from_mapping(explicit, source="config")
            self._persist_process_target(env, target)
            return target
        for name in ("_assignment_v2_process_target", "assignment_v2_process_target"):
            value = getattr(env, name, None)
            if isinstance(value, Mapping):
                target = self._target_from_mapping(value, source=name)
                self._persist_process_target(env, target)
                return target
        deployment = getattr(env, "deployment", None)
        for name in ("_assignment_v2_process_target", "assignment_v2_process_target"):
            value = getattr(deployment, name, None)
            if isinstance(value, Mapping):
                target = self._target_from_mapping(value, source=f"deployment.{name}")
                self._persist_process_target(env, target)
                return target

        runtime = getattr(deployment, "runtime", None)
        sessions = getattr(runtime, "sessions", None)
        if isinstance(sessions, Mapping):
            session_name = config.get("session", "default")
            session = sessions.get(session_name)
            shell = getattr(session, "shell", None) if session is not None else None
            try:
                pid = getattr(shell, "pid", None)
            except BaseException:
                pid = None
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                target = self._process_target(
                    host_pid=pid,
                    # LocalRuntime's BashSession is already a host process;
                    # do not falsely label its host PID as a container PID.
                    container_pid=None,
                    pid_namespace=_pid_namespace_link(pid),
                    mapping_source="swerex_local_persistent_bash_session",
                )
                self._persist_process_target(env, target)
                return target

        # RemoteRuntime intentionally does not expose its remote BashSession
        # object.  DockerDeployment does expose the running container name,
        # so query the persistent shell itself and translate its namespace PID
        # through the host's NSpid table.  The Docker CLI Popen is never a
        # target candidate.
        container_name = getattr(deployment, "container_name", None)
        if not isinstance(container_name, str) or not container_name.strip():
            container_name = getattr(deployment, "_container_name", None)
        if isinstance(container_name, str) and container_name.strip():
            session_name = config.get("session", "default")
            if not isinstance(session_name, str) or not session_name.strip():
                raise RuntimeError("v2 CPU collector SWE-ReX session name is invalid")
            with self._target_discovery_scope():
                try:
                    query_timeout = float(config.get("pid_query_timeout_s", 5.0))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("v2 CPU collector PID query timeout is invalid") from exc
                if not 0 < query_timeout <= 30:
                    raise RuntimeError("v2 CPU collector PID query timeout must be between 0 and 30 seconds")
                container_pid, namespace = _persistent_shell_observation(
                    env, session=session_name, timeout=query_timeout
                )
                runtime_config = getattr(deployment, "_config", None)
                container_runtime = getattr(runtime_config, "container_runtime", None)
                if container_runtime is None:
                    container_runtime = getattr(deployment, "container_runtime", None)
                host_pid = _map_container_pid_to_host(
                    container_runtime=str(container_runtime or ""),
                    container_name=container_name,
                    container_pid=container_pid,
                    pid_namespace=namespace,
                )
                target = self._process_target(
                    host_pid=host_pid,
                    container_pid=container_pid,
                    pid_namespace=namespace,
                    mapping_source="swerex_docker_persistent_bash_nspid",
                )
                self._persist_process_target(env, target)
                return target
        raise RuntimeError(
            "v2 CPU collector requires an explicit host PID mapping or a mapped persistent SWE-ReX bash session; refusing parent/docker-helper PID"
        )

    def _stop_work_service(self) -> None:
        service = self._work_service
        try:
            if service is not None:
                service.stop()
        finally:
            self._work_service = None
            if self._work_service_socket_dir is not None:
                self._work_service_socket_dir.cleanup()
                self._work_service_socket_dir = None
            self.work_collector = None
            self._work_collector_event_id = None
            self._work_collector_status = None
            self._work_collector_error = None
            self._work_collector_by_span.clear()
            env = self._work_env
            self._work_env = None
            if env is not None:
                for owner in (env, getattr(env, "deployment", None)):
                    if owner is None:
                        continue
                    for name in (
                        "_assignment_v2_bpf_service",
                        "_assignment_v2_stop_bpf_service",
                        "_assignment_v2_process_target",
                    ):
                        with contextlib.suppress(AttributeError):
                            delattr(owner, name)

    def _ensure_work_collector(self, env: Any) -> None:
        required = os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1"
        if self.work_collector is not None:
            if required and not all(
                callable(getattr(self.work_collector, name, None))
                for name in ("start_action", "end_action")
            ):
                raise RuntimeError("required v2 CPU collector does not implement the action boundary API")
            return
        config = self._load_work_config()
        if config is None:
            if required:
                raise RuntimeError("required v2 CPU collector configuration is missing")
            return
        backend = str(config.get("backend", "")).lower()
        if backend in {"", "none", "unavailable"}:
            if required:
                raise RuntimeError("v2 production requires a concrete CPU work collector backend")
            return
        if backend not in {"bcc", "kernel_aggregate"}:
            raise RuntimeError(
                f"unsupported v2 CPU collector backend: {backend}; only the reviewed BCC backend is activatable"
            )
        if required:
            if config.get("attach_existing_process") is not True:
                raise RuntimeError("required v2 CPU collector must attach an existing process")
            if config.get("require_persistent_runtime_pid") is not True:
                raise RuntimeError("required v2 CPU collector must require a persistent runtime PID")
            trace_format = str(config.get("trace_format", "")).lower()
            if "raw" not in trace_format or "individual" not in trace_format:
                raise RuntimeError("required v2 CPU collector must retain individual raw records")
        from .bpf_work import launch_bpf_work_service

        target = self._resolve_work_target(config, env)
        root = Path(str(config.get("output_dir") or (self.telemetry.output_dir / "linux_work"))).expanduser()
        socket_path = Path(str(config.get("socket_path") or (root / "collector.sock"))).expanduser()
        if not config.get("socket_path") and len(os.fsencode(socket_path)) >= 104:
            # The socket is transient IPC, not raw evidence. Keep its private
            # owned directory short even when the durable attempt path is long.
            self._work_service_socket_dir = tempfile.TemporaryDirectory(prefix="as-bpf-", dir="/tmp")
            socket_path = Path(self._work_service_socket_dir.name) / "collector.sock"
        try:
            service = launch_bpf_work_service(
                target,
                socket_path=socket_path,
                trace_dir=root,
                python_executable=config.get("python_executable") if isinstance(config.get("python_executable"), str) else None,
                cwd=Path(config["cwd"]) if isinstance(config.get("cwd"), str) else None,
                startup_timeout_s=float(config.get("startup_timeout_s", 15.0)),
                force=bool(config.get("force", False)),
            )
        except BaseException:
            if self._work_service_socket_dir is not None:
                self._work_service_socket_dir.cleanup()
                self._work_service_socket_dir = None
            raise
        if service is None or not callable(getattr(service, "stop", None)):
            raise RuntimeError("v2 CPU collector service did not return a stoppable handle")
        if not all(
            callable(getattr(service.client, name, None))
            for name in ("start_action", "end_action")
        ):
            with contextlib.suppress(BaseException):
                service.stop()
            raise RuntimeError("v2 CPU collector service returned an invalid client")
        self._work_service = service
        self.work_collector = service.client
        self._work_env = env
        setattr(env, "_assignment_v2_bpf_service", service)
        setattr(env, "_assignment_v2_stop_bpf_service", self._stop_work_service)
        if not self._work_service_stop_registered:
            atexit.register(self._stop_work_service)
            self._work_service_stop_registered = True

    def on_init(self, *, agent: Any):
        self.agent = agent
        self._wrap_get_state(agent)
        self._wrap_model_query(agent)
        self._wrap_runtime(getattr(agent, "_env", None))

    def on_run_start(self):
        if self.owns_outer and getattr(self.telemetry, "_outer", None) is None:
            self.telemetry.start_outer()

    def on_setup_attempt(self):
        self._setup_span = self.telemetry.start_phase("setup", event_kind="setup")
        self._aux_span = self._setup_span
        env = getattr(self.agent, "_env", None)
        self._wrap_runtime(env)
        # The persistent SWE-ReX session exists by the time the agent's real
        # setup begins.  Attach the owned collector here so setup/state-query
        # commands after this boundary and all later tool commands share the
        # same target identity.  Missing production mappings fail closed.
        if env is not None:
            self._ensure_work_collector(env)

    def on_tools_installation_started(self):
        if self._startup_span is None:
            self._startup_span = self.telemetry.start_phase("startup", event_kind="startup")
        self._aux_span = self._startup_span
        self._wrap_runtime(getattr(self.agent, "_env", None))

    def on_setup_done(self):
        self.finish_setup()

    def on_step_start(self):
        self._step_active = True
        self._step_id += 1
        # Request retry lineage is scoped to one logical agent step.  A fresh
        # step is a new logical call, even when it happens immediately after a
        # previous query.
        self._request_retry_index = 0
        self._last_request_id = None
        self._last_request_end_ns = None
        self._action_retry_index = 0
        self._last_action_id = None
        self._pending_action = None
        self._pending_intent = None
        self._client_interval_index = 0
        self._clear_runtime_result()
        self._wrap_runtime(getattr(self.agent, "_env", None))
        self._resume_client_processing()

    def _begin_model_request(self) -> None:
        client_parent_event_id = (
            self._client_span.pre_event_id if self._client_span is not None else self._client_parent()
        )
        self._pause_client_processing()
        if self._model_span is not None and not self._model_span.closed:
            self._close(self._model_span, status="failure", error_type="ModelQuerySuperseded", error_message="previous query did not produce an action")
        retry_of = self._last_request_id
        self._request_retry_index = self._request_retry_index + 1 if retry_of else 0
        if retry_of and self._last_request_end_ns is not None:
            retry = self.telemetry.start_phase(
                "retry",
                start_mono_ns=self._last_request_end_ns,
                parent_event_id=client_parent_event_id,
                event_kind="model_retry",
                reason="SWE-agent re-queried the model within one logical step",
            )
            retry.finish(status="success")
        request: dict[str, Any] = {
            "input_tokens": None,
            "context_tokens": None,
            "max_output_tokens": self.max_output_tokens,
        }
        model = getattr(getattr(self.agent, "model", None), "config", None)
        if model is not None:
            completion = getattr(model, "completion_kwargs", {}) or {}
            if request["max_output_tokens"] is None and isinstance(completion, Mapping):
                value = completion.get("max_tokens")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    request["max_output_tokens"] = value
            if isinstance(completion, Mapping):
                # Preserve the concrete request policy in the pre-action
                # feature envelope.  These are request controls, not inferred
                # measurements; defaults are explicit when the pinned client
                # omits a field.
                top_p = completion.get("top_p", 1.0)
                seed = completion.get("seed", 0)
                if isinstance(top_p, (int, float)) and not isinstance(top_p, bool):
                    request["top_p"] = float(top_p)
                if isinstance(seed, int) and not isinstance(seed, bool):
                    request["seed"] = seed
            name = getattr(model, "name", None)
            if isinstance(name, str):
                request["model"] = name
        self._model_span = self.telemetry.begin_request(
            request,
            logical_request_id=stable_id("logical-request", self.telemetry.run_id, self.telemetry.attempt_id, self._step_id),
            retry_index=self._request_retry_index,
            retry_of=retry_of,
            step_id=self._step_id,
            parent_event_id=client_parent_event_id,
            phase="model_client_call",
            event_kind="model_client_call",
        )
        self._last_request_id = str(self._model_span.identity.get("physical_request_id"))

    def on_model_query(self, *, messages: list[dict[str, str]], agent: str):
        del messages, agent
        self._begin_model_request()

    def on_actions_generated(self, *, step: Any):
        self._close(self._model_span)
        action = _step_value(step, "action", "")
        if not isinstance(action, str) or not action.strip():
            self.telemetry.record_unknown(
                start_mono_ns=monotonic_ns(),
                end_mono_ns=monotonic_ns(),
                reason="model output did not contain an executable action",
            )
            return
        # This callback is before blocklist/exit checks.  Keep the generated
        # intent durable, but defer the measured tool span until
        # ``on_action_started`` (the exact point before env.communicate).
        if self._pending_action is not None or self._last_action_id is not None:
            self._action_retry_index += 1
        intent = self.telemetry.record_tool_intent(
            action,
            logical_operation_id=stable_id("operation", self.telemetry.run_id, self.telemetry.attempt_id, self._step_id),
            retry_index=self._action_retry_index,
            step_id=self._step_id,
            script_state=self.script_state.current(),
        )
        self._pending_action = action
        self._pending_intent = intent

    def on_action_started(self, *, step: Any):
        action = _step_value(step, "action", "")
        if self._tool_span is None and isinstance(action, str) and action.strip():
            actual_action = self._guarded_action(action)
            state_parent_event_id = (
                self._client_span.pre_event_id
                if self._client_span is not None
                else self._client_parent()
            )
            self._refresh_script_state_before_action(
                actual_action=actual_action,
                parent_event_id=state_parent_event_id,
            )
            client_parent_event_id = (
                self._client_span.pre_event_id
                if self._client_span is not None
                else self._client_parent()
            )
            self._pause_client_processing()
            intent = self._pending_intent if self._pending_action == action else None
            logical = intent.get("logical_operation_id") if intent else stable_id(
                "operation", self.telemetry.run_id, self.telemetry.attempt_id, self._step_id
            )
            action_id = intent.get("action_id") if intent else None
            retry_index = int(intent.get("retry_index", self._action_retry_index)) if intent else self._action_retry_index
            self._tool_span = self.telemetry.begin_tool(
                action,
                action_id=action_id,
                logical_operation_id=logical,
                retry_index=retry_index,
                retry_of=self._last_action_id,
                step_id=self._step_id,
                parent_event_id=client_parent_event_id,
                script_state=self.script_state.current(),
                actual_action=actual_action,
            )
            self._last_action_id = str(self._tool_span.identity.get("action_id"))
            self._actual_action = actual_action
            self._start_work_collection(self._tool_span, actual_action)
            self._clear_runtime_result()
            env = getattr(self.agent, "_env", None) if self.agent is not None else None
            if env is not None:
                self._wrap_runtime(env)
            self._pending_action = None
            self._pending_intent = None

    def on_action_executed(self, *, step: Any):
        if self._tool_span is None:
            return
        operation = str(self._tool_span.identity.get("operation_class") or "")
        status, error = self._step_status(step)
        self._end_work_collection(
            status=status,
            end_mono_ns=self._runtime_end_ns or monotonic_ns(),
            error=error[1],
        )
        self._close(
            self._tool_span,
            status=status,
            error_type=error[0],
            error_message=error[1],
            **self._runtime_values(),
            work_collector_event_id=self._work_collector_event_id,
            work_collector_status=self._work_collector_status,
            work_collector_error=self._work_collector_error,
        )
        self._invalidate_after_action(operation)
        self._resume_client_processing()
        self._clear_runtime_result()

    def on_step_done(self, *, step: Any, info: Mapping[str, Any]):
        del info
        if self._tool_span is not None:
            operation = str(self._tool_span.identity.get("operation_class") or "")
            status, error = self._step_status(step)
            self._end_work_collection(
                status=status,
                end_mono_ns=self._runtime_end_ns or monotonic_ns(),
                error=error[1],
            )
            self._close(
                self._tool_span,
                status=status,
                error_type=error[0] or "StepNotTerminated",
                error_message=error[1] or "SWE-agent step ended without action-executed callback",
                **self._runtime_values(),
                work_collector_event_id=self._work_collector_event_id,
                work_collector_status=self._work_collector_status,
                work_collector_error=self._work_collector_error,
            )
            self._invalidate_after_action(operation)
        self._close(self._client_span)
        self._step_active = False
        self._clear_runtime_result()

    def on_query_message_added(self, **kwargs: Any):
        # Message content is intentionally ignored.  It is enough to retain a
        # client-processing interval and its bounded event identity.
        del kwargs

    def on_run_done(self, *, trajectory: Any, info: Mapping[str, Any]):
        del trajectory, info
        self._close(self._model_span, status="failure", error_type="AgentRunEnded", error_message="model request ended without terminal action")
        self._close(
            self._tool_span,
            status="failure",
            error_type="AgentRunEnded",
            error_message="tool action ended without terminal callback",
            **self._runtime_values(),
            work_collector_event_id=self._work_collector_event_id,
            work_collector_status=self._work_collector_status,
            work_collector_error=self._work_collector_error,
        )
        self._close(self._client_span)
        self._step_active = False
        self._clear_runtime_result()
        self._close(self._setup_span)
        self._close(self._startup_span)

    def _step_status(self, step: Any) -> tuple[str, tuple[str | None, str | None]]:
        if self._runtime_timeout:
            return "timeout", ("CommandTimeoutError", "SWE-ReX reported a command timeout")
        if self._runtime_error is not None:
            return "failure", self._runtime_error
        if self._runtime_exit_code_observed:
            if self._runtime_exit_code == 0:
                return "success", (None, None)
            return "failure", (
                "CommandExitError",
                f"SWE-ReX command exited with status {self._runtime_exit_code}",
            )
        exit_status = _step_value(step, "exit_status", None)
        text = str(exit_status or "").lower()
        if "timeout" in text or "cancel" in text:
            return "timeout", ("CommandTimeoutError", "SWE-agent reported a command timeout")
        if isinstance(exit_status, int) and not isinstance(exit_status, bool) and exit_status != 0:
            return "failure", ("CommandExitError", f"SWE-agent command exited with status {exit_status}")
        if any(word in text for word in ("error", "failure", "forfeit")):
            return "failure", ("CommandExecutionError", f"SWE-agent action exit status: {exit_status}")
        # Pinned SWE-agent catches CommandTimeoutError and still invokes the
        # action callback.  Its consecutive-timeout counter is the only
        # reliable signal available at this callback boundary.
        if self.agent is not None:
            timeout_count = getattr(self.agent, "_n_consecutive_timeouts", 0)
            if isinstance(timeout_count, int) and timeout_count > 0:
                return "timeout", ("CommandTimeoutError", "SWE-agent caught a command timeout before the hook callback")
        return "unavailable", (
            "CommandOutcomeUnavailable",
            "SWE-ReX did not expose an exit code for the executed command",
        )

    def _runtime_values(self) -> dict[str, Any]:
        return {
            "runtime_command": self._runtime_command,
            "runtime_command_start_mono_ns": self._runtime_start_ns,
            "runtime_command_end_mono_ns": self._runtime_end_ns,
            "command_exit_code": self._runtime_exit_code,
            "command_exit_code_availability": "measured" if self._runtime_exit_code_observed else "unavailable",
            "command_timeout": bool(self._runtime_timeout),
            "runtime_error": self._runtime_error[0] if self._runtime_error else None,
            "bytes_read": self._runtime_work.bytes_read,
            "bytes_written": self._runtime_work.bytes_written,
            "files_touched": self._runtime_work.files_touched,
            "subprocess_count": self._runtime_work.subprocess_count,
            "work_volume_source": self._runtime_work.source,
            "work_volume_probe": self._runtime_work.probe,
            "work_volume_reason": self._runtime_work.reason,
            "work_probe_binding": (
                dict(self._runtime_work.binding)
                if self._runtime_work.binding is not None
                else None
            ),
            "runtime_child_telemetry": self._runtime_work.source is not None,
        }

    def _clear_runtime_result(self) -> None:
        self._actual_action = None
        self._runtime_command = None
        self._runtime_start_ns = None
        self._runtime_end_ns = None
        self._runtime_exit_code = None
        self._runtime_exit_code_observed = False
        self._runtime_timeout = False
        self._runtime_error = None
        self._runtime_work = WorkMeasurement(
            bytes_read=None,
            bytes_written=None,
            files_touched=None,
            subprocess_count=None,
            availability={
                "bytes_read": "unavailable",
                "bytes_written": "unavailable",
                "files_touched": "unavailable",
                "subprocess_count": "unavailable",
            },
            source=None,
            probe=None,
        )

    @staticmethod
    def _canonical_container_cwd(value: Any) -> str | None:
        """Validate the single absolute path returned by a native ``pwd``.

        A repository name is not a working-directory witness: SWE-ReX keeps a
        persistent shell whose cwd can be changed by setup and prior actions.
        The only accepted base for a relative script path is therefore the
        cwd queried from that shell immediately before the action.
        """

        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return None
        if not isinstance(value, str) or "\x00" in value:
            return None
        lines = value.splitlines()
        if len(lines) != 1:
            return None
        text = lines[0]
        if not text or text != text.strip() or not text.startswith("/"):
            return None
        path = PurePosixPath(text)
        if not path.is_absolute() or ".." in path.parts or "~" in path.parts:
            return None
        return str(path)

    @staticmethod
    def _resolve_cwd_target(cwd: str, target: str) -> str | None:
        """Resolve a literal ``cd`` target without guessing shell expansion."""

        if not isinstance(target, str) or not target or target in {"-", "~"}:
            return None
        if any(marker in target for marker in ("$", "`", "*", "?", "(", ")", "{", "}")):
            return None
        if not isinstance(cwd, str) or not cwd.startswith("/"):
            return None
        parts = list(PurePosixPath(cwd).parts)
        if not parts or parts[0] != "/":
            return None
        target_parts = PurePosixPath(target).parts
        if target.startswith("/"):
            parts = ["/"]
        for part in target_parts:
            if part in {"", ".", "/"}:
                continue
            if part == "..":
                if len(parts) <= 1:
                    return None
                parts.pop()
            else:
                parts.append(part)
        return str(PurePosixPath(*parts))

    @staticmethod
    def _simple_cd_target(tokens: list[str]) -> str | None:
        if not tokens or tokens[0] != "cd":
            return None
        if len(tokens) == 2:
            return tokens[1]
        if len(tokens) == 3 and tokens[1] == "--":
            return tokens[2]
        return None

    @classmethod
    def _ordered_container_script_paths(
        cls,
        command: str,
        container_cwd: str,
    ) -> tuple[list[str], str | None]:
        """Resolve script paths only where shell ordering proves their cwd.

        The action is split at top-level operators.  A literal ``cd x &&``
        establishes ``x`` for the following segment.  A ``;``/``||`` edge
        cannot establish a cwd without observing the preceding exit status,
        and a ``cd`` in a pipeline runs in a subshell, so those cases remain
        unavailable.  This prevents a later ``cd`` token from being applied
        retroactively to an earlier script and prevents one action's edits
        from being presented as a pre-action state.
        """

        if cls._canonical_container_cwd(container_cwd) is None:
            return [], "native cwd witness is not one absolute path"
        current = str(PurePosixPath(container_cwd))
        known = True
        changed_previous = False
        unresolved = False
        paths: list[str] = []
        segments = _operator_segments(command)
        for index, (segment, operator_before) in enumerate(segments):
            if operator_before == "||" and changed_previous:
                # The prior cd may have succeeded (skipping this branch) or
                # failed (leaving the old cwd), so the branch's cwd is not
                # observed by this hook.
                known = False
            tokens = _shell_tokens(segment)
            if not tokens:
                continue
            segment_candidates = _script_path_candidates(segment)
            if segment_candidates:
                for token in segment_candidates:
                    # Absolute script paths remain resolvable even when an
                    # earlier conditional cd made the relative cwd unknown.
                    if not known and not token.startswith("/"):
                        unresolved = True
                        continue
                    path = cls._resolve_cwd_target(current if known else "/", token)
                    if path is None:
                        unresolved = True
                    elif path not in paths:
                        paths.append(path)

            target = cls._simple_cd_target(tokens)
            if tokens[0] == "cd" and target is None:
                # A non-literal cd may affect every later relative script.
                if segment_candidates:
                    unresolved = True
                known = False
                changed_previous = True
                continue
            if "cd" in tokens and tokens[0] != "cd":
                # ``if cd ...``, command substitutions, and shell groups need
                # a shell AST/exit observation that this boundary does not
                # have.  Do not infer their cwd effects.
                if segment_candidates:
                    unresolved = True
                known = False
                changed_previous = True

            if target is None:
                changed_previous = False
                continue

            next_operator = segments[index + 1][1] if index + 1 < len(segments) else None
            new_cwd = cls._resolve_cwd_target(current if known else "/", target)
            if operator_before == "|" or next_operator == "|":
                # A cd in a pipeline does not change the persistent shell's
                # cwd.  Keep the observed parent cwd for another stage.
                changed_previous = False
                continue
            if next_operator == "&&" and new_cwd is not None:
                current = new_cwd
                known = True
                changed_previous = True
            elif index + 1 < len(segments):
                # For ``;`` and ``||`` the next command may run with either
                # the old or new cwd because cd's exit status is unobserved.
                known = False
                changed_previous = True
            else:
                changed_previous = False

        if unresolved:
            return paths, "one or more executable script paths have unproven shell ordering or cwd"
        return paths, None

    @staticmethod
    def _resolve_container_script_path(
        token: str,
        container_cwd: str,
    ) -> str | None:
        """Resolve one already-ordered token against a native cwd witness."""

        if not isinstance(token, str) or not token or token.startswith("~"):
            return None
        return SWEAgentTelemetryHook._resolve_cwd_target(container_cwd, token)

    @staticmethod
    def _query_container_working_directory(
        env: Any, *, witness: dict[str, Any] | None = None, prefer_service: bool = True
    ) -> str | None:
        """Read live service-bound cwd, falling back to the native shell API.

        No cwd is cached. The privileged service proves the current path in
        the target's root namespace and the client validates freshness and
        identity. Retain that proof in the enclosing script-read journal.
        """

        witness = witness if witness is not None else {}
        witness.update(source="swerex_pwd", status="unavailable")
        service = getattr(env, "_assignment_v2_bpf_service", None)
        client = getattr(service, "client", None)
        query = getattr(client, "cwd_snapshot", None)
        if prefer_service and callable(query):
            try:
                snapshot = query()
                if not isinstance(snapshot, Mapping):
                    raise ValueError("cwd service returned no snapshot")
                witness["service_snapshot"] = dict(snapshot)
                if snapshot.get("status") == "measured":
                    cwd = SWEAgentTelemetryHook._canonical_container_cwd(snapshot.get("container_cwd"))
                    if cwd is None or snapshot.get("identity") != getattr(client, "identity", None):
                        raise ValueError("cwd service returned an invalid path or identity")
                    witness.update(source="bpf_service_procfs", status="measured")
                    return cwd
                witness["fallback_reason"] = str(snapshot.get("reason") or "service cwd unavailable")[:512]
            except Exception as exc:
                witness["fallback_reason"] = f"{type(exc).__name__}: {exc}"[:512]
        else:
            witness["fallback_reason"] = (
                "action cd requires native shell logical cwd"
                if not prefer_service else "bound service cwd operation unavailable"
            )
        communicate = getattr(env, "communicate", None)
        if not callable(communicate):
            witness["reason"] = "native shell cwd fallback unavailable"
            return None
        try:
            output = communicate("pwd", check="ignore")
        except TypeError:
            try:
                output = communicate("pwd")
            except BaseException as exc:
                witness["reason"] = f"{type(exc).__name__}: {exc}"[:512]
                return None
        except BaseException as exc:
            witness["reason"] = f"{type(exc).__name__}: {exc}"[:512]
            return None
        cwd = SWEAgentTelemetryHook._canonical_container_cwd(output)
        if cwd is not None:
            witness["status"] = "measured"
        else:
            witness["reason"] = "native shell pwd did not return one absolute path"
        return cwd

    def _native_script_snapshot(self, actual_action: str, *, parent_event_id: str | None) -> dict[str, Any] | None:
        """Read executable scripts through the pinned container API.

        ``SWEEnv.read_file`` is synchronous but performs the native
        SWE-ReX ``ReadFileRequest``.  The state-query span encloses the cwd
        witness and each real read.  The pinned API has no byte-limit argument,
        so ``SCRIPT_CONTENT_MAX_BYTES`` is a retention cap applied after the
        returned text is decoded; an oversized/undecodable result is kept
        explicitly unavailable rather than hashed or truncated as if it were
        complete.
        """

        env = getattr(self.agent, "_env", None) if self.agent is not None else None
        read_file = getattr(env, "read_file", None)
        candidates = _script_path_candidates(actual_action)
        if env is None or not callable(read_file) or not candidates:
            return None
        state_span = self.telemetry.start_phase(
            "state_query",
            event_kind="script_read",
            parent_event_id=parent_event_id,
            reason="native-container script read before executable action with retention cap",
        )
        previous_aux_span = self._aux_span
        self._aux_span = state_span

        def restore_aux_span() -> None:
            if self._aux_span is state_span:
                self._aux_span = previous_aux_span

        # SWE-ReX keeps one persistent shell session.  Query its actual cwd at
        # this boundary instead of deriving it from repo_name or applying all
        # ``cd`` tokens in the action retroactively.
        cwd_witness: dict[str, Any] = {}
        # procfs exposes physical cwd. Bash's default logical ``cd ..`` can
        # follow a different parent after a symlinked cd, so preserve native
        # pwd for compound actions whose own cd semantics need that witness.
        contains_cd = any(
            "cd" in _shell_tokens(segment)
            for segment, _operator in _operator_segments(actual_action)
        )
        container_cwd = self._query_container_working_directory(
            env, witness=cwd_witness, prefer_service=not contains_cd
        )
        if container_cwd is None:
            self._close(
                state_span,
                status="unavailable",
                error_type="WorkingDirectoryUnavailable",
                error_message="live persistent-shell cwd was unavailable from service and native fallback",
                script_read_retention_cap_bytes=SCRIPT_CONTENT_MAX_BYTES,
                script_read_count=0,
                script_cwd_witness=cwd_witness,
            )
            state = self.script_state.invalidate(
                "native persistent-shell working directory was not observable",
                source_event_id=state_span.pre_event_id,
            )
            restore_aux_span()
            return state

        ordered_paths, unresolved_reason = self._ordered_container_script_paths(
            actual_action,
            container_cwd,
        )
        if unresolved_reason is not None or not ordered_paths:
            reason = unresolved_reason or "no executable script path had a provable native cwd"
            self._close(
                state_span,
                status="unavailable",
                error_type="ScriptPathUnavailable",
                error_message=reason,
                script_read_retention_cap_bytes=SCRIPT_CONTENT_MAX_BYTES,
                script_read_count=0,
                script_container_cwd=container_cwd,
                script_cwd_witness=cwd_witness,
            )
            state = self.script_state.invalidate(reason, source_event_id=state_span.pre_event_id)
            restore_aux_span()
            return state

        next_generation = self.script_state.generation + 1
        descriptors: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        unavailable = False
        try:
            for path in ordered_paths:
                try:
                    try:
                        content = read_file(path, encoding="utf-8", errors="strict")
                    except TypeError:
                        # Small fixture environments may expose only read_file
                        # (path), while pinned SWEEnv supports encoding/errors.
                        content = read_file(path)
                    if isinstance(content, bytes):
                        content = content.decode("utf-8", errors="strict")
                    if not isinstance(content, str):
                        raise TypeError("native read_file did not return text")
                    encoded = content.encode("utf-8")
                    if len(encoded) > SCRIPT_CONTENT_MAX_BYTES:
                        unavailable = True
                        descriptors.append(
                            {
                                "path": path,
                                "sha256": None,
                                "size_bytes": len(encoded),
                                "content_artifact": {
                                    "artifact_path": None,
                                    "sha256": None,
                                    "encoding": "utf-8",
                                    "size_bytes": len(encoded),
                                    "truncated": True,
                                    "hash_basis": "decoded_text_utf8_reencoding",
                                    "byte_exact": False,
                                },
                            }
                        )
                        continue
                    digest = hashlib.sha256(encoded).hexdigest()
                    artifact = self.telemetry.record_script_artifact(
                        container_path=path,
                        content=encoded,
                        encoding="utf-8",
                        generation=next_generation,
                        # SWEEnv.read_file is implemented with Path.read_text
                        # in the pinned runtime.  The returned value is
                        # therefore a decoded text representation; re-encoding
                        # it as UTF-8 is deterministic but cannot claim the
                        # original file bytes (for example, newline
                        # translation may already have occurred).
                        hash_basis="decoded_text_utf8_reencoding",
                        byte_exact=False,
                    )
                    state_artifact = {
                        key: artifact[key]
                        for key in (
                            "artifact_path",
                            "sha256",
                            "encoding",
                            "size_bytes",
                            "truncated",
                            "hash_basis",
                            "byte_exact",
                        )
                    }
                    descriptors.append(
                        {"path": path, "sha256": digest, "size_bytes": len(encoded), "content_artifact": state_artifact}
                    )
                    artifacts.append(artifact)
                except BaseException as exc:
                    unavailable = True
                    descriptors.append(
                        {
                            "path": path,
                            "sha256": None,
                            "size_bytes": None,
                            "content_artifact": {
                                "artifact_path": None,
                                "sha256": None,
                                "encoding": "utf-8",
                                "size_bytes": None,
                                "truncated": False,
                                "hash_basis": "decoded_text_utf8_reencoding",
                                "byte_exact": False,
                            },
                        }
                    )
                    artifacts.append(
                        {
                            "container_path": path,
                            "artifact_path": None,
                            "sha256": None,
                            "encoding": "utf-8",
                            "size_bytes": None,
                            "truncated": False,
                            "hash_basis": "decoded_text_utf8_reencoding",
                            "byte_exact": False,
                            "generation": next_generation,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc)[:256],
                        }
                    )
            state = self.script_state.snapshot(
                descriptors,
                source_event_id=state_span.pre_event_id,
            )
            self._close(
                state_span,
                status="unavailable" if unavailable else "success",
                error_type="ScriptReadUnavailable" if unavailable else None,
                error_message="one or more script reads exceeded the cap or failed" if unavailable else None,
                script_state=state,
                script_artifacts=artifacts,
                script_read_retention_cap_bytes=SCRIPT_CONTENT_MAX_BYTES,
                script_read_count=len(ordered_paths),
                script_container_cwd=container_cwd,
                script_cwd_witness=cwd_witness,
            )
            restore_aux_span()
            return state
        except BaseException as exc:
            self._close(
                state_span,
                status="failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
                script_cwd_witness=cwd_witness,
            )
            self.script_state.invalidate(
                "native container script state snapshot could not be serialized",
                source_event_id=state_span.pre_event_id,
            )
            restore_aux_span()
            return self.script_state.current()

    def _refresh_script_state_before_action(
        self,
        *,
        actual_action: str | None = None,
        parent_event_id: str | None = None,
    ) -> None:
        current = self.script_state.current()
        parent = parent_event_id if parent_event_id is not None else self._client_parent()
        if actual_action:
            captured = self._native_script_snapshot(actual_action, parent_event_id=parent)
            if captured is not None:
                return
        paths = [
            entry.get("path")
            for entry in current.get("paths", [])
            if isinstance(entry, Mapping) and entry.get("path")
        ]
        if self.script_state.root is not None and paths:
            # This branch is retained for explicit local/offline callers which
            # supplied a trusted root.  Production container hooks use the
            # native read path above and never hash a host path as container
            # state.
            self.script_state.snapshot(paths, source_event_id=current.get("source_event_id"))
        elif current.get("status") == "known":
            self.script_state.invalidate("pre-action repository state is not locally observable")

    def _invalidate_after_action(self, operation: str) -> None:
        if operation in {"write", "patch", "shell", "test"}:
            self.script_state.invalidate("action may have mutated repository state")


class SWEAgentEnvironmentTelemetryHook(_EnvHook):
    """Observe the pinned SWEEnv lifecycle callbacks."""

    def __init__(self, telemetry: TelemetryV2, *, agent_hook: SWEAgentTelemetryHook | None = None):
        self.telemetry = telemetry
        self._agent_hook = agent_hook
        self._startup: Span | None = None
        self._teardown: Span | None = None
        self._env: Any | None = None
        self._close_wrapped = False
        self._communicate_wrapped = False
        self._collector_bootstrap_active = False

    def _finish(self, span: Span | None, status: str = "success", **kwargs: Any):
        if span is not None and not span.closed:
            span.finish(status=status, **kwargs)

    def on_init(self, *, env: Any):
        self._env = env
        self._wrap_close(env)
        self._wrap_communicate(env)

    def bind_agent_hook(self, agent_hook: SWEAgentTelemetryHook) -> None:
        """Share the already-installed agent owner with this early env hook."""

        self._agent_hook = agent_hook
        if self._env is not None:
            self._wrap_communicate(self._env)
            agent_hook.bind_environment(
                self._env,
                startup_span=self._startup,
                runtime_ready=False,
            )

    def _wrap_communicate(self, env: Any) -> None:
        method = getattr(env, "communicate", None)
        if not callable(method) or getattr(method, "_assignment_v2_env_wrapper", False):
            return

        @functools.wraps(method)
        def communicate(*args: Any, **kwargs: Any) -> Any:
            agent_hook = self._agent_hook
            if agent_hook is not None and not self._collector_bootstrap_active:
                agent_hook.bind_environment(env, startup_span=self._startup)
                if agent_hook.work_collector is None:
                    self._collector_bootstrap_active = True
                    try:
                        # This wrapper is synchronous and runs before the
                        # first outer communicate call.  If target discovery
                        # calls communicate recursively, the guard delegates
                        # directly to the original method, avoiding nested
                        # asyncio.run inside the async runtime wrapper.
                        agent_hook._ensure_work_collector(env)
                    finally:
                        self._collector_bootstrap_active = False
            elif agent_hook is None and os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
                raise RuntimeError("required v2 CPU collector agent hook is not bound before environment setup")
            return method(*args, **kwargs)

        communicate._assignment_v2_env_wrapper = True  # type: ignore[attr-defined]
        env.communicate = communicate
        self._communicate_wrapped = True

    def _wrap_close(self, env: Any) -> None:
        method = getattr(env, "close", None)
        if not callable(method) or getattr(method, "_assignment_v2_wrapped", False):
            return

        @functools.wraps(method)
        def close(*args: Any, **kwargs: Any) -> Any:
            if self._teardown is not None and not self._teardown.closed:
                return method(*args, **kwargs)
            self._teardown = self.telemetry.start_phase("teardown", event_kind="teardown")
            collector_error: BaseException | None = None
            try:
                # Pinned SWEEnv stops deployment before invoking on_close.
                # Stop the owned collector while the persistent target still
                # exists, but always let the environment perform its own
                # cleanup so a collector failure cannot strand a container.
                stop_collector = getattr(env, "_assignment_v2_stop_bpf_service", None)
                if callable(stop_collector):
                    try:
                        stop_collector()
                    except BaseException as exc:
                        collector_error = exc
                result = method(*args, **kwargs)
            except TimeoutError as exc:
                self._finish(self._teardown, status="timeout", error_type=type(exc).__name__, error_message=str(exc))
                self._teardown = None
                raise
            except BaseException as exc:
                self._finish(self._teardown, status="failure", error_type=type(exc).__name__, error_message=str(exc))
                self._teardown = None
                raise
            else:
                if collector_error is not None:
                    self._finish(
                        self._teardown,
                        status="failure",
                        error_type=type(collector_error).__name__,
                        error_message=str(collector_error),
                    )
                    self._teardown = None
                    raise collector_error
                self._finish(self._teardown)
                self._teardown = None
                return result

        close._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        env.close = close
        self._close_wrapped = True

    def on_start_deployment(self):
        if self._startup is None:
            self._startup = self.telemetry.start_phase("startup", event_kind="deployment_start")
        if self._agent_hook is not None and self._env is not None:
            self._agent_hook.bind_environment(
                self._env,
                startup_span=self._startup,
                runtime_ready=False,
            )

    def on_copy_repo_started(self, *, repo: Any):
        del repo
        if self._startup is None:
            self._startup = self.telemetry.start_phase("startup", event_kind="copy_repo")
        if self._agent_hook is not None and self._env is not None:
            self._agent_hook.bind_environment(self._env, startup_span=self._startup)

    def on_install_env_started(self):
        if self._startup is None:
            self._startup = self.telemetry.start_phase("startup", event_kind="install_environment")
        if self._agent_hook is not None and self._env is not None:
            self._agent_hook.bind_environment(self._env, startup_span=self._startup)

    def on_environment_startup(self):
        if self._startup is not None and self._agent_hook is not None:
            self._agent_hook._close(self._startup)
        else:
            self._finish(self._startup)
        self._startup = None

    def on_close(self):
        # In the pinned SWEEnv this callback is invoked after deployment.stop.
        # The enclosing close wrapper owns the measured teardown interval;
        # this callback only closes an incomplete startup span.
        if self._startup is not None and self._agent_hook is not None:
            self._agent_hook._close(
                self._startup,
                status="failure",
                error_type="EnvironmentClosed",
                error_message="environment closed before startup completion",
            )
        else:
            self._finish(self._startup, status="failure", error_type="EnvironmentClosed", error_message="environment closed before startup completion")
        self._startup = None


def attach_sweagent_telemetry(agent: Any, env: Any, telemetry: TelemetryV2, *, script_state: ScriptStateLedger | None = None) -> SWEAgentTelemetryHook:
    """Attach both actual pinned hook types and return the agent hook."""

    agent_hook = SWEAgentTelemetryHook(telemetry, script_state=script_state)
    agent.add_hook(agent_hook)
    env.add_hook(SWEAgentEnvironmentTelemetryHook(telemetry, agent_hook=agent_hook))
    return agent_hook


__all__ = [
    "SWEAgentEnvironmentTelemetryHook",
    "SWEAgentTelemetryHook",
    "attach_sweagent_telemetry",
]
