"""A narrow runner for the pinned SWE-agent command.

The control path calls SWE-agent directly with ``subprocess``.  Thin
telemetry, when requested by the caller, is an output observer and never a
prompt/request wrapper.  No SWE-agent, Hermes, or LangGraph package is
imported here, which keeps local tests deterministic and paid runs auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentic_sim.artifacts.contract import ArtifactLayout, attempt_layout, initialize_attempt, inventory
from agentic_sim.artifacts.json import atomic_json_dump
from agentic_sim.runners.case_lifecycle import (
    CASE_DEADLINE_ENV,
    deadline_from_env,
    deadline_with_timeout,
    remaining_seconds,
    run_owned_process,
)
from agentic_sim.telemetry.clock import clock_metadata
from agentic_sim.telemetry.thin import ThinTelemetry, monotonic_ns, utc_now
from agentic_sim.telemetry.v2 import TelemetryV2


# These variables are intentionally scoped to the reviewed SWE-agent child.
# In particular, they must not leak into the separately launched evaluator or
# into helper Python processes that are not the agent owner.
_V2_ACTIVATION_ENV_KEYS = (
    "ASSIGNMENT_TELEMETRY_V2_AUTO",
    "ASSIGNMENT_TELEMETRY_V2_READY",
    "ASSIGNMENT_TELEMETRY_V2_HANDSHAKE_NONCE",
    "ASSIGNMENT_TELEMETRY_V2_OWNER_PID",
    "ASSIGNMENT_TELEMETRY_V2_SUPERVISOR",
    "ASSIGNMENT_TELEMETRY_V2_REQUIRED",
    "ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES",
    "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH",
    "ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256",
    "ASSIGNMENT_TELEMETRY_V2_MODEL_HARDWARE_JSON",
    "ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG",
    "ASSIGNMENT_TELEMETRY_V2_DIR",
    "ASSIGNMENT_TELEMETRY_V2_RUN_ID",
    "ASSIGNMENT_TELEMETRY_V2_ATTEMPT_ID",
    "ASSIGNMENT_CASE_ID",
    "ASSIGNMENT_INSTANCE_ID",
    "ASSIGNMENT_MODEL",
    "ASSIGNMENT_MODEL_REVISION",
)


def _without_v2_activation(env: Mapping[str, str]) -> dict[str, str]:
    """Return an environment safe for a non-agent descendant.

    The evaluator runs after the agent in the same runner process.  Reusing
    the agent environment would make ``sitecustomize`` attempt to install
    hooks in evaluator/tokenizer helpers and could let one of those helpers
    replace the agent activation marker.  The evaluator still receives the
    ordinary case deadline and caller-provided settings; only v2 activation
    controls are stripped.
    """

    result = dict(env)
    for key in _V2_ACTIVATION_ENV_KEYS:
        result.pop(key, None)
    return result


class RunnerContractError(ValueError):
    """A run would violate a frozen command or artifact contract."""


def _validate_reviewed_telemetry_requirements(
    requirements: Mapping[str, Any],
    *,
    hardware_profile_path: Any,
    hardware_profile_sha256: Any,
) -> None:
    """Reject a reviewed launch that cannot produce complete v2 evidence."""

    if requirements.get("require_activation") is not True:
        raise RunnerContractError("reviewed SWE-agent requires v2 child activation")
    if requirements.get("require_raw_request_payloads") is not True:
        raise RunnerContractError("reviewed SWE-agent requires raw request payload capture")
    if requirements.get("require_cpu_work") is not True:
        raise RunnerContractError("reviewed SWE-agent requires CPU work capture")
    cpu = requirements.get("cpu_work")
    if not isinstance(cpu, Mapping):
        raise RunnerContractError("reviewed SWE-agent CPU collector configuration is missing")
    backend = cpu.get("backend")
    if backend not in {"bcc", "kernel_aggregate"}:
        raise RunnerContractError(
            "reviewed SWE-agent requires the reviewed BCC CPU collector backend"
        )
    trace_format = str(cpu.get("trace_format", "")).lower()
    if "raw" not in trace_format or "individual" not in trace_format:
        raise RunnerContractError(
            "reviewed SWE-agent CPU collector must retain individual raw records"
        )
    if cpu.get("attach_existing_process") is not True:
        raise RunnerContractError("reviewed SWE-agent CPU collector must attach an existing process")
    if cpu.get("require_persistent_runtime_pid") is not True:
        raise RunnerContractError("reviewed SWE-agent CPU collector requires a persistent runtime PID")
    if not isinstance(hardware_profile_path, str) or not hardware_profile_path.strip():
        raise RunnerContractError("reviewed SWE-agent requires a hardware profile path")
    profile = Path(hardware_profile_path).expanduser()
    if not profile.is_absolute() or profile.is_symlink() or not profile.is_file():
        raise RunnerContractError("reviewed SWE-agent hardware profile is unavailable")
    if (
        not isinstance(hardware_profile_sha256, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", hardware_profile_sha256)
        or set(hardware_profile_sha256.lower()) == {"0"}
    ):
        raise RunnerContractError("reviewed SWE-agent requires a non-zero hardware profile SHA-256")


@dataclass(frozen=True)
class RunnerConfig:
    command: Sequence[str] | str
    experiment_id: str
    instance_id: str
    dataset: str = "lite"
    work_root: Path = Path(".")
    attempt_id: str = "attempt-001"
    mode: str = "uninstrumented"
    environment: Mapping[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    timeout_seconds: int = 7200
    config: Mapping[str, Any] = field(default_factory=dict)
    prediction_path: Path | None = None
    model_revision: str | None = None
    swe_agent_revision: str | None = None
    swe_bench_revision: str | None = None
    observability_level: str | None = None
    profilers_enabled: Sequence[str] = ()
    instrumentation_version: str = "obs-1"
    vllm_metrics_available: Sequence[str] = ()
    dcgm_metrics_available: Sequence[str] = ()
    hardware_manifest: Mapping[str, Any] = field(default_factory=dict)
    telemetry_requirements: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in {"uninstrumented", "thin-telemetry"}:
            raise RunnerContractError("mode must be uninstrumented or thin-telemetry")
        if self.timeout_seconds <= 0:
            raise RunnerContractError("timeout_seconds must be positive")
        if isinstance(self.command, str) and not self.command.strip():
            raise RunnerContractError("command is empty")
        if isinstance(self.command, Sequence) and not isinstance(self.command, str) and not self.command:
            raise RunnerContractError("command is empty")
        level = self.observability_level or ("control" if self.mode == "uninstrumented" else "thin")
        if level not in {"control", "thin", "otel", "nsys", "syscall"}:
            raise RunnerContractError("unsupported observability level")
        if level == "control" and self.profilers_enabled:
            raise RunnerContractError("control runs cannot enable profilers")
        if self.mode == "uninstrumented" and level != "control":
            raise RunnerContractError("uninstrumented mode must use control observability level")
        if self.mode == "thin-telemetry" and level != "thin":
            raise RunnerContractError("thin-telemetry mode must use thin observability level")
        for label, revision in (("model", self.model_revision), ("SWE-agent", self.swe_agent_revision), ("SWE-bench", self.swe_bench_revision)):
            if revision is not None and not re.fullmatch(r"[0-9a-fA-F]{12,64}", revision):
                raise RunnerContractError(f"{label} revision must be an immutable hexadecimal commit")

    @property
    def layout(self) -> ArtifactLayout:
        return attempt_layout(self.work_root, self.experiment_id, self.dataset, self.instance_id, self.attempt_id)


@dataclass(frozen=True)
class RunnerResult:
    run_id: str
    attempt_id: str
    mode: str
    returncode: int
    status: str
    started_at_utc: str
    ended_at_utc: str
    start_mono_ns: int
    end_mono_ns: int
    command_hash: str
    output_dir: Path
    stdout_log: Path
    stderr_log: Path
    deadline_mono_ns: int | None = None
    timed_out: bool = False
    cleanup: Mapping[str, Any] = field(default_factory=dict)
    evaluator_cleanup: Mapping[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end_mono_ns - self.start_mono_ns) / 1_000_000


def _argv(command: Sequence[str] | str) -> list[str]:
    return shlex.split(command) if isinstance(command, str) else [str(item) for item in command]


def command_hash(command: Sequence[str] | str) -> str:
    return hashlib.sha256("\0".join(_argv(command)).encode("utf-8")).hexdigest()


def build_command(*, executable: str = "sweagent", project: str | Path | None = None, config_path: str | Path | None = None, request_config_path: str | Path | None = "cloud/lambda/sweagent_request.yaml", instances_path: str | Path, model: str, model_revision: str, api_base: str = "http://127.0.0.1:8000/v1", api_key: str = "$VLLM_API_KEY", instance_id: str | None, output_dir: str | Path, max_steps: int = 30, max_input_tokens: int = 32768, max_output_tokens: int = 2048, max_observation_length: int = 100_000, temperature: float = 0.0, seed: int = 0, per_instance_call_limit: int = 30, num_workers: int = 1, extra_args: Sequence[str] = ()) -> list[str]:
    """Construct SWE-agent v1.1.0's direct ``run-batch`` command.

    The local file path is intentional: passing a dataset name would silently
    resolve a floating Hugging Face revision. ``model_revision`` is required
    for the run manifest and pin validation, but is not passed as a made-up
    SWE-agent CLI flag; the vLLM server owns model revision selection.
    """
    if not model_revision or not re.fullmatch(r"[0-9a-fA-F]{12,64}", model_revision):
        raise RunnerContractError("an immutable model revision is required")
    if not instances_path or not model or not model_revision:
        raise RunnerContractError("instances_path, model, and immutable model_revision are required")
    args = [executable]
    if project:
        args[0:0] = ["uv", "run", "--project", str(project)]
    # Keep the historical API name as a compatibility alias, but never emit
    # it as an invented SWE-agent flag: the pinned implementation's real
    # control is per_instance_call_limit.
    if max_steps != 30:
        if per_instance_call_limit != 30 and per_instance_call_limit != max_steps:
            raise RunnerContractError("max_steps and per_instance_call_limit disagree")
        per_instance_call_limit = max_steps
    if per_instance_call_limit <= 0 or max_input_tokens <= 0 or max_output_tokens <= 0 or max_observation_length <= 0:
        raise RunnerContractError("call, token, and observation limits must be positive")
    if not 0.0 <= temperature <= 2.0:
        raise RunnerContractError("temperature must be between 0 and 2")
    if seed < 0:
        raise RunnerContractError("seed must be non-negative")
    # ``max_input_tokens`` remains a fixed context guard.  The assignment's
    # output sweep is the provider request field below; SWE-agent's generic
    # max_output_tokens metadata alone is not sufficient for OpenAI/vLLM.
    # SWE-agent v1.1.0 rejects nested completion_kwargs.* CLI flags, so the
    # request fields are carried in a second YAML fragment that is resolved by
    # the pinned parser and recorded in the run manifest.
    args += ["run-batch", "--config", str(config_path or "config/default.yaml")]
    if request_config_path is not None:
        args += ["--config", str(request_config_path)]
    args += ["--instances.type", "file", "--instances.path", str(instances_path)]
    if instance_id is not None:
        if not instance_id:
            raise RunnerContractError("instance_id must be non-empty when an instance filter is requested")
        args += ["--instances.filter", f"^{instance_id}$"]
    args += ["--agent.model.name", model, "--agent.model.api_base", api_base, "--agent.model.api_key", api_key, "--agent.model.total_cost_limit", "0", "--agent.model.per_instance_cost_limit", "0", "--agent.model.per_instance_call_limit", str(per_instance_call_limit), "--agent.model.temperature", str(temperature), "--agent.model.max_input_tokens", str(max_input_tokens), "--agent.model.max_output_tokens", str(max_output_tokens), "--agent.templates.max_observation_length", str(max_observation_length), "--output_dir", str(output_dir), "--num_workers", str(num_workers)]
    args.extend(str(item) for item in extra_args)
    return args


_EXPERIMENTAL_FLAGS = {
    "per_instance_call_limit": "--agent.model.per_instance_call_limit",
    "temperature": "--agent.model.temperature",
    "observation_budget": "--agent.templates.max_observation_length",
}


def _load_request_config(tokens: Sequence[str]) -> tuple[dict[str, Any], Path | None, str | None]:
    """Load the completion kwargs fragment referenced by a real command."""
    try:
        config_indices = [index for index, token in enumerate(tokens) if token == "--config"]
    except TypeError as exc:  # defensive for callers passing a non-sequence
        raise RunnerContractError("command tokens are not iterable") from exc
    obsolete_flags = ("--agent.model.completion_kwargs.max_tokens", "--agent.model.completion_kwargs.seed")
    if any(flag in tokens for flag in obsolete_flags):
        raise RunnerContractError("nested completion_kwargs CLI flags are rejected by SWE-agent v1.1.0; use request YAML")
    if len(config_indices) < 2:
        raise RunnerContractError("SWE-agent command must include a request YAML config fragment")
    path = Path(str(tokens[config_indices[-1] + 1])) if config_indices[-1] + 1 < len(tokens) else Path()
    if not path.is_file():
        raise RunnerContractError(f"request YAML config fragment is unavailable: {path}")
    raw_config = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(raw_config)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(raw_config)
        except Exception as exc:  # pragma: no cover - exercised on the pinned Linux path
            raise RunnerContractError(f"request YAML config fragment is not readable: {exc}") from exc
    try:
        completion = loaded["agent"]["model"]["completion_kwargs"]
    except (KeyError, TypeError):
        raise RunnerContractError("request YAML config fragment lacks agent.model.completion_kwargs") from None
    if not isinstance(completion, dict):
        raise RunnerContractError("agent.model.completion_kwargs must be a mapping")
    explicit_keywords = {"model", "messages", "temperature", "top_p", "api_version",
                         "api_key", "fallbacks", "n", "api_base", "tools"}
    duplicates = sorted(explicit_keywords.intersection(completion))
    if duplicates:
        raise RunnerContractError(
            f"completion_kwargs duplicates pinned SWE-agent explicit keywords: {duplicates}; "
            "use the first-class model configuration fields"
        )
    # Return resolved provider settings, not a second kwargs dictionary to be
    # expanded into LiteLLM. The actual archived request fragment stays intact.
    settings = dict(completion)
    settings["top_p"] = loaded["agent"]["model"].get("top_p", 1.0)
    return settings, path, hashlib.sha256(path.read_bytes()).hexdigest()


def resolved_experiment_settings(argv: Sequence[str] | str) -> dict[str, Any]:
    """Resolve the declared assignment knobs from a concrete argv list.

    This intentionally parses the command rather than importing SWE-agent so
    local/macOS validation can prove propagation without pretending to run the
    pinned Linux dependency stack.
    """
    tokens = _argv(argv)
    values: dict[str, str] = {}
    for name, flag in _EXPERIMENTAL_FLAGS.items():
        try:
            index = tokens.index(flag)
            values[name] = tokens[index + 1]
        except (ValueError, IndexError):
            raise RunnerContractError(f"command is missing concrete experimental setting: {flag}") from None
    try:
        guard = tokens[tokens.index("--agent.model.max_output_tokens") + 1]
        input_limit = tokens[tokens.index("--agent.model.max_input_tokens") + 1]
    except (ValueError, IndexError):
        raise RunnerContractError("command is missing fixed input/output token guards") from None
    completion: dict[str, Any] | None = None
    request_config_path: Path | None = None
    request_config_sha256: str | None = None
    if "run-batch" in tokens:
        completion, request_config_path, request_config_sha256 = _load_request_config(tokens)
    try:
        resolved = {
            "per_instance_call_limit": int(values["per_instance_call_limit"]),
            "temperature": float(values["temperature"]),
            "max_observation_length": int(values["observation_budget"]),
            "max_input_tokens": int(input_limit),
            "max_output_tokens_guard": int(guard),
            "max_output_tokens": int((completion or {}).get("max_tokens")),
            "seed": int((completion or {}).get("seed", 0)),
            "top_p": float((completion or {}).get("top_p", 1.0)),
        }
    except (TypeError, ValueError) as exc:
        raise RunnerContractError(f"experimental setting is not numeric: {exc}") from None
    if resolved["max_output_tokens"] != resolved["max_output_tokens_guard"]:
        raise RunnerContractError("provider max_tokens and SWE-agent output guard disagree")
    if resolved["per_instance_call_limit"] <= 0 or resolved["max_output_tokens"] <= 0 or resolved["max_observation_length"] <= 0 or resolved["max_input_tokens"] <= 0:
        raise RunnerContractError("call, token, and observation settings must be positive")
    if (
        not 0.0 <= resolved["temperature"] <= 2.0
        or resolved["seed"] < 0
        or not 0.0 <= resolved["top_p"] <= 1.0
    ):
        raise RunnerContractError("temperature or seed is outside the supported range")
    if request_config_path is not None:
        resolved["request_config_path"] = str(request_config_path)
        resolved["request_config_sha256"] = request_config_sha256
    return resolved


def validate_experiment_command(argv: Sequence[str] | str) -> dict[str, Any]:
    """Return the resolved knob map or raise a contract error."""
    resolved = resolved_experiment_settings(argv)
    tokens = _argv(argv)
    model_index = tokens.index("--agent.model.name") if "--agent.model.name" in tokens else -1
    if model_index < 0 or model_index + 1 >= len(tokens) or not tokens[model_index + 1]:
        raise RunnerContractError("command is missing a model name")
    resolved["model"] = tokens[model_index + 1]
    resolved["command_hash"] = command_hash(tokens)
    return resolved


def _resolved_config(config: RunnerConfig, layout: ArtifactLayout, argv: list[str]) -> dict[str, Any]:
    raw = dict(config.config)
    observability_level = config.observability_level or ("control" if config.mode == "uninstrumented" else "thin")
    # Fixture commands used by local runner tests are intentionally not
    # SWE-agent invocations.  Real run-batch commands must pass the complete
    # four-knob contract and receive a concrete resolved map.
    experiment_settings: dict[str, Any]
    if "run-batch" in argv:
        experiment_settings = validate_experiment_command(argv)
    else:
        experiment_settings = {"status": "not_applicable", "reason": "non-SWE-agent fixture command"}
    return {
        "schema_version": "cr6.run-config.v2",
        "artifact_contract_version": 2,
        "run_id": config.experiment_id,
        "instance_id": config.instance_id,
        "dataset": config.dataset,
        "attempt_id": config.attempt_id,
        "mode": config.mode,
        "command": argv,
        "command_hash": command_hash(argv),
        "output_dir": str(layout.directory),
        "settings": raw,
        "resolved_experiment": experiment_settings,
        "observability_level": observability_level,
        "profilers_enabled": list(config.profilers_enabled),
        "instrumentation_version": config.instrumentation_version,
        "vllm_metrics_available": list(config.vllm_metrics_available),
        "dcgm_metrics_available": list(config.dcgm_metrics_available),
        "hardware_manifest": dict(config.hardware_manifest),
        "telemetry_requirements": dict(config.telemetry_requirements),
        "clock": clock_metadata(),
        "revisions": {
            "model": config.model_revision,
            "swe_agent": config.swe_agent_revision,
            "swe_bench": config.swe_bench_revision,
        },
        "started_by": "agentic_sim.runners.sweagent_runner",
    }


def _failure_class(returncode: int, timed_out: bool) -> str:
    if timed_out:
        return "runner_timeout"
    if returncode == 0:
        return "none"
    return "runner_error"


def run_sweagent(config: RunnerConfig, *, evaluator_command: Sequence[str] | str | None = None, telemetry: ThinTelemetry | TelemetryV2 | None = None, check: bool = False) -> RunnerResult:
    """Run one attempt and preserve raw stdout/stderr, trajectory, and status."""
    layout = config.layout
    layout.ensure()
    argv = _argv(config.command)
    resolved = _resolved_config(config, layout, argv)
    initialize_attempt(layout, resolved)
    summary_path = layout.path("summary.json")
    if summary_path.exists():
        try:
            prior = json.loads(summary_path.read_text(encoding="utf-8"))
            if prior.get("status") == "completed":
                raise RunnerContractError("successful attempt exists; choose a new attempt_id")
        except json.JSONDecodeError:
            pass
    stdout_log = layout.directory / "agent.stdout.log"
    stderr_log = layout.directory / "agent.stderr.log"
    env = os.environ.copy()
    env.update({str(k): str(v) for k, v in config.environment.items()})
    # Establish one deadline even for direct library calls, then pass it to
    # both stages. Measurement uses MONOTONIC_RAW; deadline comparisons must
    # exclusively use the lifecycle helper's CLOCK_MONOTONIC.
    inherited_deadline = deadline_from_env(env) or deadline_with_timeout(config.timeout_seconds)
    env[CASE_DEADLINE_ENV] = str(inherited_deadline)
    process_deadline = inherited_deadline
    if process_deadline is None:
        process_deadline = deadline_with_timeout(config.timeout_seconds)
    started_utc = utc_now()
    start = monotonic_ns()
    v2_telemetry = telemetry if isinstance(telemetry, TelemetryV2) else None
    generic_wrapper = None
    telemetry_activation: dict[str, Any] = {
        "required": False,
        "ready": None,
        "marker": None,
        "reason": "subprocess is not the reviewed SWE-agent project",
    }
    if v2_telemetry is not None:
        # Never inherit a marker owner or activation controls from a parent
        # shell/matrix process.  The reviewed child below becomes the sole
        # owner for this launch.
        for key in _V2_ACTIVATION_ENV_KEYS:
            env.pop(key, None)
        env["ASSIGNMENT_TELEMETRY_V2_DIR"] = str(v2_telemetry.output_dir)
        env["ASSIGNMENT_TELEMETRY_V2_RUN_ID"] = v2_telemetry.run_id
        env["ASSIGNMENT_TELEMETRY_V2_ATTEMPT_ID"] = v2_telemetry.attempt_id
        requirements = dict(config.telemetry_requirements)
        require_activation = bool(requirements.get("require_activation", False))
        require_payloads = bool(requirements.get("require_raw_request_payloads", False))
        env["ASSIGNMENT_TELEMETRY_V2_REQUIRED"] = "1" if (require_activation or bool(requirements.get("require_cpu_work", False))) else "0"
        env["ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES"] = "1" if require_payloads else "0"
        hardware_hash = requirements.get("hardware_profile_sha256") or getattr(v2_telemetry, "hardware_profile_sha256", None)
        if hardware_hash:
            env["ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256"] = str(hardware_hash)
        profile_path = requirements.get("hardware_profile_path")
        if profile_path:
            env["ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_PATH"] = str(profile_path)
        model_hardware = requirements.get("model_hardware")
        if isinstance(model_hardware, Mapping):
            env["ASSIGNMENT_TELEMETRY_V2_MODEL_HARDWARE_JSON"] = json.dumps(
                dict(model_hardware), sort_keys=True, separators=(",", ":")
            )
        cpu_config = requirements.get("cpu_work")
        if isinstance(cpu_config, Mapping):
            env["ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG"] = json.dumps(
                dict(cpu_config), sort_keys=True, separators=(",", ":")
            )
        if v2_telemetry.case_id is not None:
            env["ASSIGNMENT_CASE_ID"] = str(v2_telemetry.case_id)
        if v2_telemetry.instance_id is not None:
            env["ASSIGNMENT_INSTANCE_ID"] = str(v2_telemetry.instance_id)
        if v2_telemetry.model is not None:
            env["ASSIGNMENT_MODEL"] = str(v2_telemetry.model)
        if v2_telemetry.model_revision is not None:
            env["ASSIGNMENT_MODEL_REVISION"] = str(v2_telemetry.model_revision)
        # Only the reviewed project gets subprocess hook activation.  Fixture
        # commands may use a v2 recorder for the runner boundary while lacking
        # the pinned SWE-agent package and therefore must not be mislabelled as
        # hook-complete runs.
        project_candidates = [config.cwd] if config.cwd is not None else []
        project_candidates.extend(Path(token) for token in argv if token and not token.startswith("-"))
        reviewed_project = next((candidate for candidate in project_candidates
            if candidate is not None and (candidate / "sweagent" / "agent" / "agents.py").is_file()), None)
        reviewed_sweagent = reviewed_project is not None
        if reviewed_sweagent:
            # Production dependencies are prepared and fingerprinted before
            # dispatch. Per-case uv resolution would mutate the shared lock/
            # environment and can change behavior between parallel workers.
            env["UV_NO_SYNC"] = "1"
            _validate_reviewed_telemetry_requirements(
                requirements,
                hardware_profile_path=profile_path,
                hardware_profile_sha256=hardware_hash,
            )
            # Reassert the reviewed-project requirement after the project
            # probe; direct library callers may omit a manifest descriptor,
            # but a real pinned subprocess always needs the handshake.
            env["ASSIGNMENT_TELEMETRY_V2_REQUIRED"] = "1"
        if reviewed_sweagent:
            marker = v2_telemetry.output_dir / "sweagent_hook_ready.json"
            # A marker is valid only for this process launch.  Remove a stale
            # regular file or link before setting the child environment; a
            # directory at this path is left in place and will fail closed.
            if marker.is_symlink() or marker.is_file():
                marker.unlink()
            telemetry_activation = {
                "required": True,
                "ready": False,
                "marker": str(marker),
                "reason": "waiting for child sitecustomize hook installation",
            }
            env["ASSIGNMENT_TELEMETRY_V2_READY"] = str(marker)
            env["ASSIGNMENT_TELEMETRY_V2_HANDSHAKE_NONCE"] = secrets.token_hex(16)
            env["ASSIGNMENT_TELEMETRY_V2_AUTO"] = "1"
            # This module lives at <repo>/src/agentic_sim/runners; parents[2]
            # is already the repository's source root.
            source_root = Path(__file__).resolve().parents[2]
            if source_root.is_dir():
                env["PYTHONPATH"] = str(source_root) + os.pathsep + str(reviewed_project.resolve()) + (
                    os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
                )
        outer = v2_telemetry.start_outer(start_mono_ns=start)
        generic_wrapper = v2_telemetry.start_phase(
            "generic_wrapper",
            start_mono_ns=start,
            parent_event_id=outer.pre_event_id,
            event_kind="runner_process_wrapper",
            reason="direct subprocess owner; excluded from useful phase coverage",
        )
    elif telemetry:
        telemetry.run_start(config_hash=resolved["command_hash"])
    timed_out = False
    returncode = 1
    agent_cleanup: Mapping[str, Any] = {}
    try:
        with stdout_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
            outcome = run_owned_process(
                argv,
                cwd=str(config.cwd) if config.cwd else None,
                env=env,
                stdout=stdout,
                stderr=stderr,
                deadline_mono_ns=process_deadline,
                # Do not create a second relative timeout when the matrix
                # deadline was inherited.  The compatibility cap is used only
                # for direct callers that have no outer deadline.
                timeout_seconds=None if inherited_deadline is not None else config.timeout_seconds,
            )
            returncode = outcome.returncode
            timed_out = outcome.timed_out or (
                remaining_seconds(inherited_deadline) <= 0
            )
            agent_cleanup = outcome.cleanup
    except OSError as exc:
        stderr_log.write_text(f"runner could not start: {exc}\n", encoding="utf-8")
        returncode = 127
    end = monotonic_ns()
    if telemetry_activation["required"]:
        marker_path = Path(str(telemetry_activation["marker"]))
        marker_error: str | None = None
        try:
            if marker_path.is_symlink() or not marker_path.is_file():
                raise ValueError("activation marker is missing or not a regular file")
            loaded = json.loads(marker_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("activation marker is not a JSON object")
            expected = {
                "schema_version": "assignment.telemetry.v2.activation",
                "run_id": v2_telemetry.run_id if v2_telemetry is not None else None,
                "attempt_id": v2_telemetry.attempt_id if v2_telemetry is not None else None,
                "handshake_nonce": env.get("ASSIGNMENT_TELEMETRY_V2_HANDSHAKE_NONCE"),
                "writer_role": "sweagent",
            }
            for key, value in expected.items():
                if loaded.get(key) != value:
                    raise ValueError(f"activation marker {key} does not match this run")
            if not isinstance(loaded.get("pid"), int) or loaded["pid"] <= 0:
                raise ValueError("activation marker has no valid child pid")
            expected_profile_hash = env.get("ASSIGNMENT_TELEMETRY_V2_HARDWARE_PROFILE_SHA256")
            if expected_profile_hash is not None and loaded.get("hardware_profile_sha256") != expected_profile_hash:
                raise ValueError("activation marker hardware profile hash is not bound to this run")
            expected_payload_capture = env.get("ASSIGNMENT_TELEMETRY_V2_REQUIRE_RAW_REQUEST_BODIES") == "1"
            if loaded.get("raw_request_bodies_required") is not expected_payload_capture:
                raise ValueError("activation marker raw request capture requirement is not bound")
            expected_cpu_configured = bool(env.get("ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG"))
            if loaded.get("cpu_collector_configured") is not expected_cpu_configured:
                raise ValueError("activation marker CPU collector configuration is not bound")
            telemetry_activation.update({"ready": True, "reason": "child hooks installed", "payload": loaded})
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            marker_error = str(exc)
            telemetry_activation.update({"ready": False, "reason": marker_error})
        if marker_error is not None:
            agent_cleanup = {
                **dict(agent_cleanup),
                "instrumentation_missing": True,
                "instrumentation_error": marker_error,
            }
            # Preserve an independently observed timeout.  A zero-exit child
            # without the required hook handshake is always a failed run so
            # callers cannot silently accept an uninstrumented result.
            if returncode == 0 and not timed_out:
                returncode = 126
    status = "completed" if returncode == 0 and not timed_out else ("timeout" if timed_out else "failed")
    ended_utc = utc_now()
    telemetry_v2_summary: Mapping[str, Any] | None = None
    if v2_telemetry is not None:
        v2_status = "success" if status == "completed" else ("timeout" if status == "timeout" else "failure")
        if generic_wrapper is not None and not generic_wrapper.closed:
            generic_wrapper.finish(status=v2_status, end_mono_ns=end)
        if getattr(v2_telemetry, "_outer", None) is not None and not v2_telemetry._outer.closed:
            v2_telemetry.finish_outer(status=v2_status, end_mono_ns=end)
            telemetry_v2_summary = v2_telemetry.reconcile_e2e()
    elif telemetry:
        telemetry.run_end(status=status, exit_code=returncode)
    # Preserve evaluator handoff separately: evaluator runtime is not E2E.
    evaluator_status = {"status": "unavailable", "provenance": "unavailable", "reason": "not run by runner"}
    evaluator_cleanup: Mapping[str, Any] = {}
    # If the agent consumed the case deadline, do not launch an evaluator that
    # would receive a fresh full timeout.  The unavailable status remains
    # explicit in eval.json and the case runner records the timeout result.
    if evaluator_command is not None and status == "completed" and (
        remaining_seconds(inherited_deadline) > 0
    ):
        eval_log = layout.directory / "evaluator.stdout.log"
        eval_err = layout.directory / "evaluator.stderr.log"
        eval_argv = _argv(evaluator_command)
        eval_started_utc = utc_now()
        eval_start = monotonic_ns()
        with eval_log.open("ab") as out, eval_err.open("ab") as err:
            eval_outcome = run_owned_process(
                eval_argv,
                cwd=str(config.cwd) if config.cwd else None,
                env=_without_v2_activation(env),
                stdout=out,
                stderr=err,
                deadline_mono_ns=inherited_deadline,
                timeout_seconds=None if inherited_deadline is not None else config.timeout_seconds,
            )
            eval_rc = eval_outcome.returncode
            eval_timed_out = eval_outcome.timed_out or (
                remaining_seconds(inherited_deadline) <= 0
            )
            evaluator_cleanup = eval_outcome.cleanup
        eval_ended_utc = utc_now()
        eval_end = monotonic_ns()
        evaluator_status = {"status": "timeout" if eval_timed_out else ("completed" if eval_rc == 0 else "failed"), "provenance": "measured", "returncode": eval_rc, "command_hash": command_hash(eval_argv), "started_at_utc": eval_started_utc, "ended_at_utc": eval_ended_utc, "start_mono_ns": eval_start, "end_mono_ns": eval_end, "clock": clock_metadata(), "runtime_excluded_from_trajectory": True, "deadline_mono_ns": inherited_deadline, "cleanup": dict(evaluator_cleanup)}
    elif evaluator_command is not None and remaining_seconds(inherited_deadline) <= 0:
        evaluator_status = {
            "status": "timeout",
            "provenance": "measured",
            "reason": "case deadline expired before evaluator launch",
            "deadline_mono_ns": inherited_deadline,
            "runtime_excluded_from_trajectory": True,
        }
    if config.prediction_path and config.prediction_path.is_file():
        # Copying the evaluator input is explicit and only happens to the new attempt.
        layout.path("prediction.json").write_bytes(config.prediction_path.read_bytes())
    else:
        atomic_json_dump(layout.path("prediction.json"), {"schema_version": "cr6.prediction.v1", "status": "unavailable", "provenance": "unavailable", "reason": "SWE-agent did not produce a configured prediction path"})
    atomic_json_dump(layout.path("eval.json"), evaluator_status)
    result = RunnerResult(config.experiment_id, config.attempt_id, config.mode, returncode, status, started_utc, ended_utc, start, end, command_hash(argv), layout.directory, stdout_log, stderr_log, process_deadline, timed_out, agent_cleanup, evaluator_cleanup)
    atomic_json_dump(summary_path, {"schema_version": "cr6.summary.v2", "artifact_contract_version": 2, "run_id": config.experiment_id, "instance_id": config.instance_id, "dataset": config.dataset, "attempt_id": config.attempt_id, "mode": config.mode, "status": status, "failure_class": _failure_class(returncode, timed_out), "returncode": returncode, "started_at_utc": started_utc, "ended_at_utc": ended_utc, "start_mono_ns": start, "end_mono_ns": end, "duration_ms": result.duration_ms, "clock": clock_metadata(), "command_hash": result.command_hash, "stdout_log": str(stdout_log), "stderr_log": str(stderr_log), "deadline_mono_ns": inherited_deadline, "lifecycle": {"agent": dict(agent_cleanup), "evaluator": dict(evaluator_cleanup)}, "evaluator": evaluator_status, "telemetry_activation": telemetry_activation, "telemetry_v2": telemetry_v2_summary, "artifacts": inventory(layout)})
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, argv)
    return result
