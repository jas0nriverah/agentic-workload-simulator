"""Opt-in activation for v2 hooks in the pinned SWE-agent subprocess.

The direct runner passes an explicit telemetry directory and identity through
the environment only for the reviewed SWE-agent project.  Importing this
module without that opt-in is a no-op.  A requested install that cannot load
the pinned hook interfaces raises so a purported v2 run cannot silently fall
back to the legacy stream.
"""

from __future__ import annotations

import atexit
import functools
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .clock import utc_now


_RECORDER: Any | None = None
_INSTALLED = False
_PENDING_AGENT_HOOK: Any | None = None
_OWNER_ENV = "ASSIGNMENT_TELEMETRY_V2_OWNER_PID"


def _requested() -> bool:
    return os.environ.get("ASSIGNMENT_TELEMETRY_V2_AUTO", "") == "1"


def _activation_owned_by_another_process() -> bool:
    """Keep inherited opt-in scoped to the process that claimed the marker.

    The reviewed agent deliberately passes ``AUTO=1`` to its descendants so
    sitecustomize can load before the agent imports its classes.  Those
    descendants must then remain ordinary helper processes.  The owner PID is
    exported only after the intended process claims the marker; the durable
    marker is also checked so a helper that starts after a process restart
    cannot steal the path.
    """

    owner = os.environ.get(_OWNER_ENV)
    if owner:
        try:
            owner_pid = int(owner)
        except ValueError as exc:
            raise RuntimeError("v2 activation owner PID is invalid") from exc
        if owner_pid <= 0:
            raise RuntimeError("v2 activation owner PID is invalid")
        if owner_pid != os.getpid():
            return True
    raw_path = os.environ.get("ASSIGNMENT_TELEMETRY_V2_READY")
    if not raw_path:
        return False
    path = Path(raw_path)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"v2 activation handshake must be a regular file: {path}")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"v2 activation handshake is not valid JSON: {path}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError("v2 activation handshake is not a JSON object")
    marker_pid = loaded.get("pid")
    if isinstance(marker_pid, bool) or not isinstance(marker_pid, int) or marker_pid <= 0:
        raise RuntimeError("v2 activation handshake has no valid owner PID")
    return marker_pid != os.getpid()


def _claim_activation_marker() -> bool:
    """Atomically reserve the ready path for this process before patching.

    A short ``installing`` reservation closes the startup race where a
    Python helper is launched before the final handshake is written.  The
    runner removes stale reservations together with stale final markers before
    each launch.
    """

    raw_path = os.environ.get("ASSIGNMENT_TELEMETRY_V2_READY")
    if not raw_path:
        raise RuntimeError("v2 auto instrumentation requires an activation handshake path")
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"v2 activation handshake must be a regular file: {path}")
    reservation = {
        "schema_version": "assignment.telemetry.v2.activation",
        "state": "installing",
        "pid": os.getpid(),
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"v2 activation handshake must be a regular file: {path}")
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"v2 activation handshake is not valid JSON: {path}") from exc
        if not isinstance(existing, dict):
            raise RuntimeError("v2 activation handshake is not a JSON object")
        owner_pid = existing.get("pid")
        if isinstance(owner_pid, bool) or not isinstance(owner_pid, int) or owner_pid <= 0:
            raise RuntimeError("v2 activation handshake has no valid owner PID")
        if owner_pid != os.getpid():
            return False
        os.environ[_OWNER_ENV] = str(os.getpid())
        return True
    try:
        payload = (json.dumps(reservation, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    os.environ[_OWNER_ENV] = str(os.getpid())
    return True


def _recorder() -> Any:
    global _RECORDER
    if _RECORDER is not None:
        return _RECORDER
    from .v2 import TelemetryV2

    output_dir = os.environ.get("ASSIGNMENT_TELEMETRY_V2_DIR")
    run_id = os.environ.get("ASSIGNMENT_TELEMETRY_V2_RUN_ID")
    if not output_dir or not run_id:
        raise RuntimeError("v2 auto instrumentation requires telemetry directory and run id")
    model_hardware: dict[str, Any] = {
        "cpu_frequency_hz": None,
        "gpu_memory_bandwidth_bytes_per_s": None,
        "gpu_compute_tflops": None,
        "availability": {
            "cpu_frequency_hz": "unavailable",
            "gpu_memory_bandwidth_bytes_per_s": "unavailable",
            "gpu_compute_tflops": "unavailable",
        },
    }
    raw_hardware: dict[str, Any] = {}
    profile_path = os.environ.get("ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH")
    profile_hash = os.environ.get("ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256")
    if profile_hash and not profile_path and os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRED") == "1":
        raise RuntimeError("v2 required hardware profile path is missing")
    if profile_path:
        path = Path(profile_path).expanduser().resolve()
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"v2 remote hardware profile is unavailable: {path}")
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if profile_hash and digest != profile_hash.lower():
            raise RuntimeError("v2 remote hardware profile hash does not match the manifest")
        profile_hash = profile_hash or digest
        try:
            profile = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"v2 remote hardware profile is not valid JSON: {exc}") from exc
        if not isinstance(profile, dict):
            raise RuntimeError("v2 remote hardware profile must be a JSON object")
        raw_hardware = {
            "remote_profile": profile,
            "remote_profile_sha256": digest,
        }
    raw_model = os.environ.get("ASSIGNMENT_TELEMETRY_V2_MODEL_HARDWARE_JSON")
    if raw_model:
        try:
            loaded_model = json.loads(raw_model)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"v2 model hardware projection is not valid JSON: {exc}") from exc
        if not isinstance(loaded_model, dict):
            raise RuntimeError("v2 model hardware projection must be a JSON object")
        model_hardware = loaded_model
    _RECORDER = TelemetryV2(
        Path(output_dir),
        run_id=run_id,
        attempt_id=os.environ.get("ASSIGNMENT_TELEMETRY_V2_ATTEMPT_ID", "attempt-001"),
        case_id=os.environ.get("ASSIGNMENT_CASE_ID"),
        instance_id=os.environ.get("ASSIGNMENT_INSTANCE_ID"),
        model=os.environ.get("ASSIGNMENT_MODEL"),
        model_revision=os.environ.get("ASSIGNMENT_MODEL_REVISION"),
        hardware=raw_hardware,
        model_hardware=model_hardware,
        hardware_profile_sha256=profile_hash,
        writer_role="sweagent",
    )
    return _RECORDER


def _attach_agent(agent: Any) -> None:
    global _PENDING_AGENT_HOOK
    if getattr(agent, "_assignment_v2_hook", None) is not None:
        return
    from .sweagent_hooks import SWEAgentTelemetryHook

    # The direct runner owns the one outer SWE-agent interval.  The child
    # process contributes only the native setup/client/model/tool/teardown
    # spans, which are joined to that parent interval by the shared clock
    # domain and output directory.
    hook = SWEAgentTelemetryHook(_recorder(), owns_outer=False)
    agent.add_hook(hook)
    agent._assignment_v2_hook = hook
    # In the pinned batch path the environment is constructed after the
    # agent, but it is started before ``DefaultAgent.setup``.  Keep the hook
    # available for the environment hook to bind before its first shell
    # command; the environment hook consumes this once per agent instance.
    _PENDING_AGENT_HOOK = hook


def _attach_environment(env: Any) -> None:
    global _PENDING_AGENT_HOOK
    if getattr(env, "_assignment_v2_hook", None) is not None:
        return
    from .sweagent_hooks import SWEAgentEnvironmentTelemetryHook

    hook = SWEAgentEnvironmentTelemetryHook(_recorder(), agent_hook=_PENDING_AGENT_HOOK)
    env.add_hook(hook)
    env._assignment_v2_hook = hook
    _PENDING_AGENT_HOOK = None


def _finish_at_exit() -> None:
    recorder = _RECORDER
    if recorder is None:
        return
    outer = getattr(recorder, "_outer", None)
    if outer is not None and not outer.closed:
        # Process exit is a durable boundary, but without a parent callback it
        # cannot assert an application success result.  Reconciliation keeps
        # any residual explicit and the terminal status unavailable.
        recorder.finish_outer(status="unavailable")
        recorder.reconcile_e2e()


def _write_activation_handshake() -> None:
    """Publish a durable marker after both pinned hook classes are patched.

    ``sitecustomize`` errors are reported by Python and execution can still
    continue.  The parent runner therefore requires this marker before it
    accepts a reviewed SWE-agent subprocess as instrumented.  The marker is
    written atomically and contains the immutable run identity so a stale
    file cannot satisfy a later attempt.
    """

    raw_path = os.environ.get("ASSIGNMENT_TELEMETRY_V2_READY")
    if not raw_path:
        raise RuntimeError("v2 auto instrumentation requires an activation handshake path")
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"v2 activation handshake must not be a symlink: {path}")
    if not path.is_file():
        raise RuntimeError(f"v2 activation handshake reservation is missing: {path}")
    try:
        reservation = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"v2 activation handshake reservation is invalid: {path}") from exc
    if not isinstance(reservation, dict) or reservation.get("pid") != os.getpid():
        raise RuntimeError("v2 activation handshake is owned by another process")
    payload = {
        "schema_version": "assignment.telemetry.v2.activation",
        "instrumentation_version": "telemetry-v2-20260908",
        "run_id": os.environ.get("ASSIGNMENT_TELEMETRY_V2_RUN_ID"),
        "attempt_id": os.environ.get("ASSIGNMENT_TELEMETRY_V2_ATTEMPT_ID"),
        "handshake_nonce": os.environ.get("ASSIGNMENT_TELEMETRY_V2_HANDSHAKE_NONCE"),
        "writer_role": "sweagent",
        "pid": os.getpid(),
        "hardware_profile_sha256": os.environ.get("ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256"),
        "raw_request_bodies_required": os.environ.get("ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES") == "1",
        "cpu_collector_configured": bool(os.environ.get("ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG")),
        "installed_at_utc": utc_now(),
        "hooks": [
            "agentic_sim.telemetry.sweagent_hooks.SWEAgentTelemetryHook",
            "agentic_sim.telemetry.sweagent_hooks.SWEAgentEnvironmentTelemetryHook",
        ],
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.is_symlink():
        raise RuntimeError(f"v2 activation temporary path must not be a symlink: {temporary}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def install_from_env() -> bool:
    """Install hooks into the pinned classes when explicit opt-in is set."""

    global _INSTALLED
    if _INSTALLED or not _requested():
        return _INSTALLED
    if _activation_owned_by_another_process():
        # AUTO is inherited by runtime helpers.  They intentionally continue
        # without hooks and must not replace the agent's marker or journal
        # owner.  The agent process itself has either no marker yet or owns it.
        return False
    try:
        from sweagent.agent.agents import DefaultAgent
        from sweagent.environment.swe_env import SWEEnv
    except ImportError as exc:
        raise RuntimeError("v2 instrumentation was requested but pinned SWE-agent is unavailable") from exc

    if not _claim_activation_marker():
        return False

    original_agent_init = DefaultAgent.__init__
    if not getattr(original_agent_init, "_assignment_v2_wrapped", False):
        def agent_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_agent_init(self, *args, **kwargs)
            _attach_agent(self)

        agent_init._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        DefaultAgent.__init__ = agent_init  # type: ignore[method-assign]

    original_env_init = SWEEnv.__init__
    if not getattr(original_env_init, "_assignment_v2_wrapped", False):
        def env_init(self: Any, *args: Any, **kwargs: Any) -> None:
            original_env_init(self, *args, **kwargs)
            _attach_environment(self)

        env_init._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        SWEEnv.__init__ = env_init  # type: ignore[method-assign]

    # The pinned CombinedAgentHook forwards most callbacks but its
    # ``on_setup_done`` implementation delegates to ``super()`` and drops the
    # registered hooks.  Close setup/startup around the real setup method so a
    # successful run cannot leave those spans open until process exit.
    original_setup = DefaultAgent.setup
    if not getattr(original_setup, "_assignment_v2_wrapped", False):
        @functools.wraps(original_setup)
        def setup(self: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                result = original_setup(self, *args, **kwargs)
            except BaseException as exc:
                hook = getattr(self, "_assignment_v2_hook", None)
                if hook is not None:
                    hook.finish_setup(
                        status="timeout" if "timeout" in type(exc).__name__.lower() else "failure",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                    )
                raise
            else:
                hook = getattr(self, "_assignment_v2_hook", None)
                if hook is not None:
                    hook.finish_setup()
                return result

        setup._assignment_v2_wrapped = True  # type: ignore[attr-defined]
        DefaultAgent.setup = setup  # type: ignore[method-assign]

    atexit.register(_finish_at_exit)
    _INSTALLED = True
    _write_activation_handshake()
    return True


__all__ = ["install_from_env"]
