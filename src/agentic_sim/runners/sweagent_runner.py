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
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentic_sim.artifacts.contract import ArtifactLayout, attempt_layout, initialize_attempt, inventory
from agentic_sim.artifacts.json import atomic_json_dump
from agentic_sim.telemetry.clock import clock_metadata
from agentic_sim.telemetry.thin import ThinTelemetry, monotonic_ns, utc_now


class RunnerContractError(ValueError):
    """A run would violate a frozen command or artifact contract."""


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

    @property
    def duration_ms(self) -> float:
        return (self.end_mono_ns - self.start_mono_ns) / 1_000_000


def _argv(command: Sequence[str] | str) -> list[str]:
    return shlex.split(command) if isinstance(command, str) else [str(item) for item in command]


def command_hash(command: Sequence[str] | str) -> str:
    return hashlib.sha256("\0".join(_argv(command)).encode("utf-8")).hexdigest()


def build_command(*, executable: str = "sweagent", project: str | Path | None = None, config_path: str | Path | None = None, request_config_path: str | Path | None = "cloud/lambda/sweagent_request.yaml", instances_path: str | Path, model: str, model_revision: str, api_base: str = "http://127.0.0.1:8000/v1", api_key: str = "$VLLM_API_KEY", instance_id: str, output_dir: str | Path, max_steps: int = 30, max_input_tokens: int = 32768, max_output_tokens: int = 2048, max_observation_length: int = 100_000, temperature: float = 0.0, seed: int = 0, per_instance_call_limit: int = 30, num_workers: int = 1, extra_args: Sequence[str] = ()) -> list[str]:
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
    args += ["--instances.type", "file", "--instances.path", str(instances_path), "--instances.filter", f"^{instance_id}$", "--agent.model.name", model, "--agent.model.api_base", api_base, "--agent.model.api_key", api_key, "--agent.model.total_cost_limit", "0", "--agent.model.per_instance_cost_limit", "0", "--agent.model.per_instance_call_limit", str(per_instance_call_limit), "--agent.model.temperature", str(temperature), "--agent.model.max_input_tokens", str(max_input_tokens), "--agent.model.max_output_tokens", str(max_output_tokens), "--agent.templates.max_observation_length", str(max_observation_length), "--output_dir", str(output_dir), "--num_workers", str(num_workers)]
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
    return dict(completion), path, hashlib.sha256(path.read_bytes()).hexdigest()


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
            "seed": int((completion or {}).get("seed")),
        }
    except (TypeError, ValueError) as exc:
        raise RunnerContractError(f"experimental setting is not numeric: {exc}") from None
    if resolved["max_output_tokens"] != resolved["max_output_tokens_guard"]:
        raise RunnerContractError("provider max_tokens and SWE-agent output guard disagree")
    if resolved["per_instance_call_limit"] <= 0 or resolved["max_output_tokens"] <= 0 or resolved["max_observation_length"] <= 0 or resolved["max_input_tokens"] <= 0:
        raise RunnerContractError("call, token, and observation settings must be positive")
    if not 0.0 <= resolved["temperature"] <= 2.0 or resolved["seed"] < 0:
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


def run_sweagent(config: RunnerConfig, *, evaluator_command: Sequence[str] | str | None = None, telemetry: ThinTelemetry | None = None, check: bool = False) -> RunnerResult:
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
    if telemetry:
        telemetry.run_start(config_hash=resolved["command_hash"])
    started_utc = utc_now()
    start = monotonic_ns()
    timed_out = False
    returncode = 1
    try:
        with stdout_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
            process = subprocess.Popen(argv, cwd=str(config.cwd) if config.cwd else None, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                returncode = process.wait(timeout=config.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                # SWE-agent may leave Docker/worker descendants behind. Kill
                # the isolated process group so a timeout cannot keep billing.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=30)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                returncode = process.wait()
    except OSError as exc:
        stderr_log.write_text(f"runner could not start: {exc}\n", encoding="utf-8")
        returncode = 127
    end = monotonic_ns()
    status = "completed" if returncode == 0 and not timed_out else ("timeout" if timed_out else "failed")
    ended_utc = utc_now()
    if telemetry:
        telemetry.run_end(status=status, exit_code=returncode)
    # Preserve evaluator handoff separately: evaluator runtime is not E2E.
    evaluator_status = {"status": "unavailable", "provenance": "unavailable", "reason": "not run by runner"}
    if evaluator_command is not None and status == "completed":
        eval_log = layout.directory / "evaluator.stdout.log"
        eval_err = layout.directory / "evaluator.stderr.log"
        eval_argv = _argv(evaluator_command)
        eval_started_utc = utc_now()
        eval_start = monotonic_ns()
        with eval_log.open("ab") as out, eval_err.open("ab") as err:
            eval_process = subprocess.Popen(eval_argv, cwd=str(config.cwd) if config.cwd else None, env=env, stdout=out, stderr=err, start_new_session=True)
            try:
                eval_rc = eval_process.wait(timeout=config.timeout_seconds)
                eval_timed_out = False
            except subprocess.TimeoutExpired:
                eval_timed_out = True
                try:
                    os.killpg(eval_process.pid, signal.SIGTERM)
                    eval_process.wait(timeout=30)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(eval_process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                eval_rc = eval_process.wait()
        eval_ended_utc = utc_now()
        eval_end = monotonic_ns()
        evaluator_status = {"status": "timeout" if eval_timed_out else ("completed" if eval_rc == 0 else "failed"), "provenance": "measured", "returncode": eval_rc, "command_hash": command_hash(eval_argv), "started_at_utc": eval_started_utc, "ended_at_utc": eval_ended_utc, "start_mono_ns": eval_start, "end_mono_ns": eval_end, "clock": clock_metadata(), "runtime_excluded_from_trajectory": True}
    if config.prediction_path and config.prediction_path.is_file():
        # Copying the evaluator input is explicit and only happens to the new attempt.
        layout.path("prediction.json").write_bytes(config.prediction_path.read_bytes())
    else:
        atomic_json_dump(layout.path("prediction.json"), {"schema_version": "cr6.prediction.v1", "status": "unavailable", "provenance": "unavailable", "reason": "SWE-agent did not produce a configured prediction path"})
    atomic_json_dump(layout.path("eval.json"), evaluator_status)
    result = RunnerResult(config.experiment_id, config.attempt_id, config.mode, returncode, status, started_utc, ended_utc, start, end, command_hash(argv), layout.directory, stdout_log, stderr_log)
    atomic_json_dump(summary_path, {"schema_version": "cr6.summary.v2", "artifact_contract_version": 2, "run_id": config.experiment_id, "instance_id": config.instance_id, "dataset": config.dataset, "attempt_id": config.attempt_id, "mode": config.mode, "status": status, "failure_class": _failure_class(returncode, timed_out), "returncode": returncode, "started_at_utc": started_utc, "ended_at_utc": ended_utc, "start_mono_ns": start, "end_mono_ns": end, "duration_ms": result.duration_ms, "clock": clock_metadata(), "command_hash": result.command_hash, "stdout_log": str(stdout_log), "stderr_log": str(stderr_log), "evaluator": evaluator_status, "artifacts": inventory(layout)})
    if check and returncode != 0:
        raise subprocess.CalledProcessError(returncode, argv)
    return result
