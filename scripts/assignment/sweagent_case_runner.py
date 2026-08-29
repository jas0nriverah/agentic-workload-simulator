#!/usr/bin/env python3
"""Provider-neutral, fail-closed SWE-agent case runner for the assignment matrix.

The matrix executor owns ordering and resume state.  This adapter owns one case:
it validates an external JSON runtime manifest, builds the repository's reviewed
SWE-agent command, and delegates execution to ``agentic_sim.runners``.  It does
not source shell files, expand shell syntax, or silently substitute missing
dependencies.  Without ``--execute`` it performs validation only.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.sweagent_runner import (  # noqa: E402
    RunnerConfig,
    RunnerContractError,
    build_command,
    command_hash,
    run_sweagent,
)
from agentic_sim.assignment.event_simulator import HardwareProfile  # noqa: E402
from scripts.assignment.adaptive_event_protocol import (  # noqa: E402
    FrozenCalibrationModel,
    verify_trajectory_prediction,
)


MANIFEST_SCHEMA = "assignment-runtime-manifest.v1"
CASE_SCHEMA = "assignment-steps-1-3-plan.v1"
RESULT_SCHEMA = "assignment-case-result.v1"
STATE_SCHEMA = "assignment-case-runner-state.v1"
OFFICIAL_EVALUATOR_SCHEMA = "assignment-official-evaluator.v1"
SHA256_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
SUITES = {"lite", "verified"}
SETTINGS = {"call_limit", "max_output_tokens", "observation_length", "temperature"}
TOP_LEVEL = {
    "schema_version", "required_branch", "required_commit", "repository_root",
    "integrity", "pins", "datasets", "model", "runner", "evaluator", "hardware", "deadlines",
}
PIN_KEYS = {"model_revision", "tokenizer_revision", "swe_agent_revision", "swe_bench_revision", "vllm_version"}


class CaseRunnerError(ValueError):
    """A manifest, case, environment, or output contract is unsafe."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise CaseRunnerError(message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise CaseRunnerError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_sidecar(path: Path, label: str) -> str:
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    digest = sha256_file(path)
    sidecar = Path(str(path) + ".sha256")
    _fail(sidecar.is_file() and not sidecar.is_symlink(), f"{label} SHA-256 sidecar is missing: {sidecar}")
    try:
        actual = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseRunnerError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    _fail(actual == f"{digest}  {path.name}\n", f"{label} or SHA-256 sidecar was tampered with")
    return digest


def _verify_recognized_sidecar(path: Path, label: str) -> str:
    """Verify exactly one of the two repository sidecar conventions."""
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    digest = sha256_file(path)
    candidates = list(dict.fromkeys((Path(str(path) + ".sha256"), path.with_suffix(".sha256"))))
    existing = [candidate for candidate in candidates if candidate.exists()]
    _fail(len(existing) == 1, f"{label} requires exactly one recognized SHA-256 sidecar")
    sidecar = existing[0]
    _fail(
        sidecar.is_file() and not sidecar.is_symlink(),
        f"{label} sidecar must be a regular file",
    )
    _fail(
        sidecar.read_text(encoding="utf-8") == f"{digest}  {path.name}\n",
        f"{label} or SHA-256 sidecar was tampered with",
    )
    return digest


def _verify_manifest_sidecar(path: Path) -> str:
    return _verify_sidecar(path, "runtime manifest")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot read {label} {path}: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value


def _string(value: Any, label: str, *, nonempty: bool = True) -> str:
    _fail(isinstance(value, str), f"{label} must be a string")
    if nonempty:
        _fail(bool(value.strip()), f"{label} must not be empty")
    return value


def _immutable_commit(value: Any, label: str) -> str:
    result = _string(value, label).lower()
    _fail(bool(SHA256_RE.fullmatch(result)), f"{label} must be a 40-character lowercase commit SHA")
    _fail(set(result) != {"0"}, f"{label} is still the zero placeholder")
    return result


def _relative_or_absolute(value: Any, label: str) -> str:
    result = _string(value, label)
    _fail("\x00" not in result, f"{label} contains a NUL")
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_json(path, "runtime manifest")
    _fail(set(manifest) == TOP_LEVEL, "runtime manifest has missing or unknown top-level fields")
    _fail(manifest.get("schema_version") == MANIFEST_SCHEMA, "unsupported runtime manifest schema")
    _string(manifest["required_branch"], "required_branch")
    _immutable_commit(manifest["required_commit"], "required_commit")
    _relative_or_absolute(manifest["repository_root"], "repository_root")

    integrity = manifest["integrity"]
    integrity_keys = {
        "case_runner_path", "case_runner_sha256", "evaluator_adapter_path",
        "evaluator_adapter_sha256", "request_config_path", "request_config_sha256",
        "request_proxy_path", "request_proxy_sha256",
        "adaptive_runner_path", "adaptive_runner_sha256",
        "adaptive_runtime_path", "adaptive_runtime_sha256",
        "adaptive_protocol_path", "adaptive_protocol_sha256",
        "event_simulator_path", "event_simulator_sha256",
    }
    _fail(isinstance(integrity, dict) and set(integrity) == integrity_keys, "integrity has invalid fields")
    for key in (
        "case_runner_path", "evaluator_adapter_path", "request_config_path", "request_proxy_path",
        "adaptive_runner_path", "adaptive_runtime_path", "adaptive_protocol_path", "event_simulator_path",
    ):
        _relative_or_absolute(integrity[key], f"integrity.{key}")
    for key in (
        "case_runner_sha256", "evaluator_adapter_sha256", "request_config_sha256", "request_proxy_sha256",
        "adaptive_runner_sha256", "adaptive_runtime_sha256", "adaptive_protocol_sha256", "event_simulator_sha256",
    ):
        digest = _string(integrity[key], f"integrity.{key}").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"integrity.{key} must be SHA-256")
        _fail(set(digest) != {"0"}, f"integrity.{key} is still the zero placeholder")

    pins = manifest["pins"]
    _fail(isinstance(pins, dict) and set(pins) == PIN_KEYS, "pins must contain exactly the required pin fields")
    for key in PIN_KEYS - {"vllm_version"}:
        _immutable_commit(pins[key], f"pins.{key}")
    _string(pins["vllm_version"], "pins.vllm_version")

    datasets = manifest["datasets"]
    _fail(isinstance(datasets, dict) and set(datasets) == SUITES, "datasets must contain exactly lite and verified")
    for suite in sorted(SUITES):
        dataset = datasets[suite]
        _fail(isinstance(dataset, dict) and set(dataset) == {"name", "revision", "instances_path", "sha256"}, f"datasets.{suite} has invalid fields")
        _string(dataset["name"], f"datasets.{suite}.name")
        _immutable_commit(dataset["revision"], f"datasets.{suite}.revision")
        _relative_or_absolute(dataset["instances_path"], f"datasets.{suite}.instances_path")
        digest = _string(dataset["sha256"], f"datasets.{suite}.sha256").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"datasets.{suite}.sha256 must be SHA-256")

    model = manifest["model"]
    _fail(isinstance(model, dict) and set(model) == {"name", "revision", "api_base", "api_key"}, "model has invalid fields")
    _string(model["name"], "model.name")
    _immutable_commit(model["revision"], "model.revision")
    _string(model["api_base"], "model.api_base")
    parsed = urlparse(model["api_base"])
    _fail(parsed.scheme in {"http", "https"} and bool(parsed.netloc), "model.api_base must be an HTTP(S) URL")
    _string(model["api_key"], "model.api_key")
    _fail(model["api_key"] in {"EMPTY", "$VLLM_API_KEY", "${VLLM_API_KEY}"}, "model.api_key must be EMPTY or the literal VLLM_API_KEY marker")
    _fail(model["revision"] == pins["model_revision"], "model.revision disagrees with pins.model_revision")

    runner = manifest["runner"]
    runner_fields = {"executable", "project", "config_path", "request_config_path", "working_directory", "extra_args"}
    _fail(isinstance(runner, dict) and set(runner) == runner_fields, "runner has invalid fields")
    for key in runner_fields - {"extra_args"}:
        _relative_or_absolute(runner[key], f"runner.{key}")
    _fail(isinstance(runner["extra_args"], list) and all(isinstance(item, str) for item in runner["extra_args"]), "runner.extra_args must be a string list")
    _fail(all("\x00" not in item for item in runner["extra_args"]), "runner.extra_args contains a NUL")

    evaluator = manifest["evaluator"]
    evaluator_fields = {"command", "project", "result_path", "resolved_field", "submitted_field"}
    _fail(isinstance(evaluator, dict) and set(evaluator) == evaluator_fields, "evaluator has invalid fields")
    _fail(isinstance(evaluator["command"], list) and bool(evaluator["command"]) and all(isinstance(item, str) and item for item in evaluator["command"]), "evaluator.command must be a non-empty argv list")
    _relative_or_absolute(evaluator["project"], "evaluator.project")
    _relative_or_absolute(evaluator["result_path"], "evaluator.result_path")
    _string(evaluator["resolved_field"], "evaluator.resolved_field")
    _string(evaluator["submitted_field"], "evaluator.submitted_field")

    hardware = manifest["hardware"]
    hardware_fields = {"gpu_names", "minimum_memory_mib", "compute_capability", "one_gpu_only", "probe_command"}
    _fail(isinstance(hardware, dict) and set(hardware) == hardware_fields, "hardware has invalid fields")
    _fail(isinstance(hardware["gpu_names"], list) and bool(hardware["gpu_names"]) and all(isinstance(item, str) and item for item in hardware["gpu_names"]), "hardware.gpu_names must be a non-empty string list")
    _fail(isinstance(hardware["minimum_memory_mib"], int) and not isinstance(hardware["minimum_memory_mib"], bool) and hardware["minimum_memory_mib"] > 0, "hardware.minimum_memory_mib must be positive")
    _string(hardware["compute_capability"], "hardware.compute_capability")
    _fail(hardware["one_gpu_only"] is True, "hardware.one_gpu_only must be true")
    _fail(isinstance(hardware["probe_command"], list) and bool(hardware["probe_command"]) and all(isinstance(item, str) and item for item in hardware["probe_command"]), "hardware.probe_command must be a non-empty argv list")

    deadlines = manifest["deadlines"]
    _fail(isinstance(deadlines, dict) and set(deadlines) == {"per_case_seconds", "global_seconds"}, "deadlines has invalid fields")
    for key in deadlines:
        _fail(isinstance(deadlines[key], int) and not isinstance(deadlines[key], bool) and deadlines[key] > 0, f"deadlines.{key} must be a positive integer")
    _fail(deadlines["global_seconds"] >= deadlines["per_case_seconds"], "global deadline must cover one case")
    return manifest


def _verify_execution_integrity(manifest: Mapping[str, Any], repo: Path) -> dict[str, str]:
    integrity = manifest["integrity"]
    paths = {
        "case_runner": _strict_path(integrity["case_runner_path"], "integrity.case_runner_path"),
        "evaluator_adapter": _strict_path(integrity["evaluator_adapter_path"], "integrity.evaluator_adapter_path"),
        "request_config": _strict_path(integrity["request_config_path"], "integrity.request_config_path"),
        "request_proxy": _strict_path(integrity["request_proxy_path"], "integrity.request_proxy_path"),
        "adaptive_runner": _strict_path(integrity["adaptive_runner_path"], "integrity.adaptive_runner_path"),
        "adaptive_runtime": _strict_path(integrity["adaptive_runtime_path"], "integrity.adaptive_runtime_path"),
        "adaptive_protocol": _strict_path(integrity["adaptive_protocol_path"], "integrity.adaptive_protocol_path"),
        "event_simulator": _strict_path(integrity["event_simulator_path"], "integrity.event_simulator_path"),
    }
    expected_paths = {
        "case_runner": Path(__file__).resolve(),
        "evaluator_adapter": (repo / "scripts/assignment/evaluate_swebench_case.py").resolve(),
        "request_config": _resolve(repo, str(manifest["runner"]["request_config_path"])),
        "request_proxy": (repo / "scripts/observability/request_proxy.py").resolve(),
        "adaptive_runner": (repo / "scripts/assignment/sweagent_adaptive_runner.py").resolve(),
        "adaptive_runtime": (repo / "scripts/assignment/adaptive_runtime.py").resolve(),
        "adaptive_protocol": (repo / "scripts/assignment/adaptive_event_protocol.py").resolve(),
        "event_simulator": (repo / "src/agentic_sim/assignment/event_simulator.py").resolve(),
    }
    result: dict[str, str] = {}
    for name, path in paths.items():
        _fail(path == expected_paths[name], f"integrity.{name}_path does not match the reviewed execution path")
        _fail(path.is_file() and not path.is_symlink(), f"reviewed {name} is not a regular file")
        digest = sha256_file(path)
        _fail(digest == integrity[f"{name}_sha256"], f"reviewed {name} SHA-256 mismatch")
        result[f"{name}_sha256"] = digest
    evaluator_command = manifest["evaluator"]["command"]
    _fail(len(evaluator_command) >= 2, "evaluator command must name the reviewed adapter")
    _fail(Path(evaluator_command[1]).expanduser().resolve() == paths["evaluator_adapter"], "evaluator command does not invoke the reviewed adapter")
    return result


def load_case(path: Path) -> dict[str, Any]:
    case = _read_json(path, "case specification")
    required = {"record_type", "schema_version", "plan_id", "steps", "roles", "suite", "instance_id", "repository", "task_sha256", "source_manifest_sha256", "cell_id", "settings", "variation", "concurrency", "per_case_deadline_seconds", "resume_key"}
    _fail(set(case) == required, "case specification has missing or unknown fields")
    _fail(case["record_type"] == "case" and case["schema_version"] == CASE_SCHEMA, "unsupported case specification")
    _fail(case["suite"] in SUITES, "case suite must be lite or verified")
    for key in ("plan_id", "instance_id", "repository", "cell_id", "resume_key"):
        _string(case[key], f"case.{key}")
    for key in ("task_sha256", "source_manifest_sha256"):
        value = _string(case[key], f"case.{key}").lower()
        _fail(bool(HEX64_RE.fullmatch(value)), f"case.{key} must be SHA-256")
    _fail(isinstance(case["settings"], dict) and set(case["settings"]) == SETTINGS, "case.settings must contain exactly the four assignment knobs")
    settings = case["settings"]
    for key in ("call_limit", "max_output_tokens", "observation_length"):
        _fail(isinstance(settings[key], int) and not isinstance(settings[key], bool) and settings[key] > 0, f"case.settings.{key} must be positive")
    _fail(isinstance(settings["temperature"], (int, float)) and not isinstance(settings["temperature"], bool) and 0 <= settings["temperature"] <= 2, "case.settings.temperature is invalid")
    _fail(case["concurrency"] == 1, "case concurrency must be 1")
    _fail(isinstance(case["per_case_deadline_seconds"], int) and not isinstance(case["per_case_deadline_seconds"], bool) and case["per_case_deadline_seconds"] > 0, "case deadline must be a positive integer")
    return case


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _git(repo: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaseRunnerError(f"git preflight failed: {exc}") from exc
    if result.returncode != 0:
        raise CaseRunnerError(f"git preflight failed for {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def validate_checkout(manifest: Mapping[str, Any]) -> tuple[Path, dict[str, str]]:
    repo = Path(str(manifest["repository_root"])).expanduser()
    if not repo.is_absolute():
        repo = (Path.cwd() / repo).resolve()
    _fail(repo.is_dir() and (repo / ".git").exists(), f"repository_root is not a Git checkout: {repo}")
    branch = _git(repo, ["branch", "--show-current"])
    commit = _git(repo, ["rev-parse", "HEAD"]).lower()
    dirty = _git(repo, ["status", "--porcelain", "--untracked-files=all"])
    _fail(branch == manifest["required_branch"], f"wrong Git branch: expected {manifest['required_branch']}, got {branch}")
    _fail(commit == manifest["required_commit"], f"wrong Git commit: expected {manifest['required_commit']}, got {commit}")
    _fail(not dirty, "working tree is dirty; commit or remove tracked and untracked changes before execution")
    return repo, {"branch": branch, "commit": commit}


def validate_static_environment(manifest: Mapping[str, Any], case: Mapping[str, Any], repo: Path) -> dict[str, Any]:
    _fail(case["per_case_deadline_seconds"] == manifest["deadlines"]["per_case_seconds"], "case deadline disagrees with runtime manifest")
    datasets = manifest["datasets"][case["suite"]]
    for key in ("config_path", "request_config_path"):
        path = _resolve(repo, str(manifest["runner"][key]))
        _fail(path.is_file(), f"runner.{key} is unavailable: {path}")
    project_path = _resolve(repo, str(manifest["runner"]["project"]))
    _fail(project_path.is_dir(), f"runner.project is unavailable: {project_path}")
    _fail((project_path / ".git").exists(), f"runner.project is not a Git checkout: {project_path}")
    runner_revision = _git(project_path, ["rev-parse", "HEAD"]).lower()
    _fail(runner_revision == manifest["pins"]["swe_agent_revision"], "runner.project SWE-agent revision mismatch")
    _fail(not _git(project_path, ["status", "--porcelain", "--untracked-files=all"]), "runner.project SWE-agent checkout is dirty")
    evaluator_project = _resolve(repo, str(manifest["evaluator"]["project"]))
    _fail(evaluator_project.is_dir() and (evaluator_project / ".git").exists(), f"evaluator.project is not a Git checkout: {evaluator_project}")
    evaluator_revision = _git(evaluator_project, ["rev-parse", "HEAD"]).lower()
    _fail(evaluator_revision == manifest["pins"]["swe_bench_revision"], "evaluator.project SWE-bench revision mismatch")
    _fail(not _git(evaluator_project, ["status", "--porcelain", "--untracked-files=all"]), "evaluator.project SWE-bench checkout is dirty")
    executable = str(manifest["runner"]["executable"])
    if not manifest["runner"]["project"] and ("/" in executable or executable.startswith(".")):
        _fail(_resolve(repo, executable).is_file(), f"runner.executable is unavailable: {executable}")
    elif not manifest["runner"]["project"]:
        _fail(shutil.which(executable) is not None, f"runner.executable is not on PATH: {executable}")
    instances_path = _resolve(repo, str(datasets["instances_path"]))
    _fail(instances_path.is_file(), f"dataset instances_path is unavailable: {instances_path}")
    _fail(sha256_file(instances_path) == datasets["sha256"], "dataset instances_path SHA-256 mismatch")
    for line_number, line in enumerate(instances_path.read_text(encoding="utf-8").splitlines(), 1):
        _fail(bool(line.strip()), f"dataset has a blank line at {line_number}: {instances_path}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseRunnerError(f"dataset is not JSONL at line {line_number}: {exc}") from exc
        _fail(isinstance(row, dict), f"dataset line {line_number} is not an object")
    return {
        "instances_path": str(instances_path),
        "dataset_revision": datasets["revision"],
        "dataset_name": datasets["name"],
        "dataset_sha256": datasets["sha256"],
        "swe_agent_revision": runner_revision,
        "swe_bench_revision": evaluator_revision,
    }


def _evaluator_image(instance_id: str) -> str:
    """Return the Docker-compatible SWE-bench image name for an instance."""
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def _materialize_runner_instances(*, source: Path, instance_id: str, output_dir: Path) -> tuple[Path, str]:
    """Create the one-row SWE-agent input while preserving the raw dataset binding.

    The pinned SWE-agent ``InstancesFromFile`` loader consumes its already-
    normalized ``SimpleBatchInstance`` schema and requires ``image_name``.
    Pinned SWE-bench JSONL rows intentionally omit that derived runtime field.
    Keep the source dataset unchanged for provenance/evaluation, and write an
    auditable one-row runtime view for SWE-agent instead of weakening either
    contract or relying on an implicit loader conversion.
    """
    try:
        rows = [
            json.loads(line)
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CaseRunnerError(f"cannot materialize SWE-agent dataset view: {source}: {exc}") from exc
    _fail(all(isinstance(row, dict) for row in rows), "SWE-bench dataset rows must be JSON objects")
    matches = [dict(row) for row in rows if row.get("instance_id") == instance_id]
    _fail(len(matches) == 1, f"dataset must contain exactly one row for {instance_id}; found {len(matches)}")
    row = matches[0]
    expected_image = _evaluator_image(instance_id)
    _fail(
        row.get("image_name") in (None, expected_image),
        f"dataset image_name conflicts for {instance_id}",
    )
    row["image_name"] = expected_image
    row["repo_name"] = "testbed"
    path = output_dir / "runner_inputs" / "sweagent_instances.json"
    _atomic_json(path, [row])
    return path, sha256_file(path)


def _probe_hardware(manifest: Mapping[str, Any]) -> dict[str, Any]:
    command = [str(item) for item in manifest["hardware"]["probe_command"]]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaseRunnerError(f"hardware probe failed: {exc}") from exc
    _fail(result.returncode == 0, f"hardware probe failed: {result.stderr.strip()}")
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    _fail(rows, "hardware probe returned no GPUs")
    parsed: list[dict[str, Any]] = []
    for row in rows:
        fields = [part.strip() for part in row.split(",")]
        _fail(len(fields) >= 3, "hardware probe output must be name,memory_mib,compute_capability CSV")
        try:
            memory = int(float(fields[1]))
        except ValueError as exc:
            raise CaseRunnerError(f"hardware probe memory is invalid: {fields[1]}") from exc
        parsed.append({"name": fields[0], "memory_mib": memory, "compute_capability": fields[2]})
    hardware = manifest["hardware"]
    _fail(len(parsed) == 1, "hardware isolation failed: exactly one GPU must be visible")
    gpu = parsed[0]
    _fail(gpu["name"] in hardware["gpu_names"], f"wrong GPU: {gpu['name']}")
    _fail(gpu["memory_mib"] >= hardware["minimum_memory_mib"], "GPU memory is below the manifest requirement")
    _fail(gpu["compute_capability"] == hardware["compute_capability"], "GPU compute capability does not match the manifest")
    return {"command": command, "gpus": parsed}


def _format_argv(argv: Sequence[str], values: Mapping[str, str]) -> list[str]:
    result: list[str] = []
    allowed = set(values)
    for token in argv:
        _fail("\x00" not in token, "command contains a NUL")
        # Explicitly reject unknown braces so a typo cannot silently reach a shell-like tool.
        fields = set(re.findall(r"\{([A-Za-z0-9_]+)\}", token))
        _fail(fields.issubset(allowed), f"command contains an unknown placeholder: {sorted(fields - allowed)}")
        result.append(token.format(**values))
    return result


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _artifact_kind(path: Path) -> str:
    name = path.name.lower()
    if "trajectory" in name:
        return "trajectory"
    if "request" in name or "proxy" in name:
        return "request"
    if "eval" in name or "result" in name:
        return "evaluator"
    return "runner"


def _strict_path(value: Any, label: str) -> Path:
    """Return a resolved path only after rejecting non-string path claims."""
    return Path(_string(value, label)).expanduser().resolve()


def _validate_adaptive_config(
    path: Path,
    *,
    manifest_path: Path,
    manifest_sha256: str,
    run_id: str,
    repo: Path,
    output_dir: Path,
) -> dict[str, Any]:
    path = path.expanduser().resolve()
    _fail(not _inside(path, repo), "adaptive runtime config must live outside the repository")
    digest = _verify_sidecar(path, "adaptive runtime config")
    value = _read_json(path, "adaptive runtime config")
    required = {
        "schema_version", "run_id", "protocol_root", "calibration_model_path",
        "split_manifest_path", "runtime_manifest_path", "hardware_profile_path",
        "bindings", "tokenizer", "pre_trajectory_e2e",
    }
    _fail(set(value) == required, "adaptive runtime config has missing or unknown fields")
    _fail(value.get("schema_version") == "assignment.adaptive-runtime-config.v1", "unsupported adaptive runtime config")
    _fail(value.get("run_id") == run_id, "adaptive runtime config run_id does not match this case")
    claimed_manifest = _strict_path(value.get("runtime_manifest_path"), "adaptive runtime runtime_manifest_path")
    _fail(claimed_manifest == manifest_path, "adaptive runtime config does not bind the active runtime manifest")
    bindings = value.get("bindings")
    _fail(isinstance(bindings, dict), "adaptive runtime config bindings are missing")
    _fail(bindings.get("runtime_manifest_sha256") == manifest_sha256, "adaptive runtime manifest SHA-256 binding mismatch")
    protocol_root = _strict_path(value.get("protocol_root"), "adaptive runtime protocol_root")
    _fail(not _inside(protocol_root, repo), "adaptive protocol root must live outside the repository")
    _fail(_inside(protocol_root, output_dir), "adaptive protocol root must stay inside this case output-dir")
    pre = value.get("pre_trajectory_e2e")
    _fail(
        isinstance(pre, dict)
        and set(pre) == {"predicted_ms", "prediction_artifact_path", "prediction_artifact_sha256"},
        "adaptive runtime requires a hash-bound pre-reveal E2E prediction",
    )
    predicted = pre.get("predicted_ms")
    _fail(isinstance(predicted, (int, float)) and not isinstance(predicted, bool) and predicted > 0, "adaptive E2E prediction must be positive")
    prediction_path = _strict_path(
        pre.get("prediction_artifact_path"),
        "adaptive runtime prediction_artifact_path",
    )
    _fail(not _inside(prediction_path, repo), "adaptive E2E prediction must live outside the repository")
    prediction_sha = _verify_recognized_sidecar(
        prediction_path,
        "adaptive E2E prediction artifact",
    )
    _fail(
        pre.get("prediction_artifact_sha256") == prediction_sha,
        "adaptive E2E prediction artifact hash does not match its config binding",
    )

    split_path = _strict_path(value.get("split_manifest_path"), "adaptive split_manifest_path")
    hardware_path = _strict_path(value.get("hardware_profile_path"), "adaptive hardware_profile_path")
    model_path = _strict_path(value.get("calibration_model_path"), "adaptive calibration_model_path")
    runtime_path = _strict_path(value.get("runtime_manifest_path"), "adaptive runtime_manifest_path")
    _fail(runtime_path == manifest_path, "adaptive runtime manifest path does not match the active manifest")
    runtime_sha = _verify_recognized_sidecar(runtime_path, "adaptive runtime manifest")
    _fail(runtime_sha == manifest_sha256, "adaptive runtime manifest hash binding mismatch")
    split_sha = _verify_recognized_sidecar(split_path, "adaptive split manifest")
    hardware_sha = _verify_recognized_sidecar(hardware_path, "adaptive hardware profile")
    model_sha = _verify_recognized_sidecar(model_path, "adaptive calibration model")
    _fail(
        set(bindings) == {
            "split_manifest_sha256",
            "runtime_manifest_sha256",
            "hardware_profile_sha256",
            "model_revision_sha256",
        },
        "adaptive runtime bindings are incomplete",
    )
    _fail(bindings.get("split_manifest_sha256") == split_sha, "adaptive split manifest hash binding mismatch")
    _fail(bindings.get("runtime_manifest_sha256") == runtime_sha, "adaptive runtime manifest hash binding mismatch")
    _fail(bindings.get("hardware_profile_sha256") == hardware_sha, "adaptive hardware profile hash binding mismatch")
    try:
        hardware = HardwareProfile.from_mapping(_read_json(hardware_path, "adaptive hardware profile"))
        calibration_model = FrozenCalibrationModel.load(model_path)
        runtime_manifest = _read_json(runtime_path, "adaptive runtime manifest")
    except (CaseRunnerError, RunnerContractError, ValueError, OSError) as exc:
        raise CaseRunnerError(f"adaptive dependency validation failed: {exc}") from exc
    _fail(
        runtime_manifest.get("schema_version") == MANIFEST_SCHEMA,
        "adaptive runtime manifest schema is invalid",
    )
    model_bindings = calibration_model.bindings()
    _fail(
        model_bindings == {
            "split_manifest_sha256": split_sha,
            "runtime_manifest_sha256": runtime_sha,
            "hardware_profile_sha256": hardware_sha,
            "model_revision_sha256": hashlib.sha256(
                str(runtime_manifest.get("model", {}).get("revision", "")).encode("utf-8")
            ).hexdigest(),
        },
        "adaptive calibration model bindings are inconsistent",
    )
    tokenizer = value.get("tokenizer")
    _fail(
        isinstance(tokenizer, dict)
        and set(tokenizer) == {"snapshot_path", "revision", "required_files_sha256"},
        "adaptive tokenizer binding is incomplete",
    )
    snapshot = _strict_path(tokenizer.get("snapshot_path"), "adaptive tokenizer snapshot")
    _fail(snapshot.is_dir(), "adaptive tokenizer snapshot must be a directory")
    file_hashes = tokenizer.get("required_files_sha256")
    _fail(isinstance(file_hashes, dict) and file_hashes, "adaptive tokenizer file hashes are missing")
    for name, expected_hash in sorted(file_hashes.items()):
        _fail(isinstance(name, str) and Path(name).name == name, "adaptive tokenizer file name is unsafe")
        candidate = snapshot / name
        sidecars = list(
            dict.fromkeys((Path(str(candidate) + ".sha256"), candidate.with_suffix(".sha256")))
        )
        existing_sidecars = [sidecar for sidecar in sidecars if sidecar.exists()]
        actual_hash = (
            _verify_recognized_sidecar(candidate, f"adaptive tokenizer file {name}")
            if existing_sidecars
            else sha256_file(candidate)
        )
        _fail(actual_hash == expected_hash, f"adaptive tokenizer file hash mismatch: {name}")
    try:
        prediction, verified_prediction_sha = verify_trajectory_prediction(
            prediction_path,
            calibration_model,
            run_id=run_id,
            hardware=hardware.to_mapping(),
        )
    except ValueError as exc:
        raise CaseRunnerError(f"adaptive E2E prediction validation failed: {exc}") from exc
    _fail(verified_prediction_sha == prediction_sha, "adaptive E2E prediction hash changed during validation")
    _fail(
        float(prediction["predicted_ms"]) == float(predicted),
        "adaptive E2E prediction does not match its config value",
    )
    return {
        "config_path": str(path),
        "config_sha256": digest,
        "protocol_root": str(protocol_root),
        "pre_trajectory_e2e_ms": float(predicted),
        "prediction_artifact_path": str(prediction_path),
        "prediction_artifact_sha256": prediction_sha,
        "calibration_model_sha256": model_sha,
    }


def _validate_official_evaluator_result(
    *,
    path: Path,
    output_dir: Path,
    instance_id: str,
    run_id: str,
    dataset_path: Path,
    dataset_sha256: str,
    predictions_path: Path,
) -> dict[str, Any]:
    """Validate the evaluator's complete provenance before accepting a case.

    A zero exit code only establishes that an evaluator process returned.  This
    verifier binds the result to the exact one-instance dataset and the exact
    prediction file emitted by the preceding runner attempt, and prevents a
    stale or external report from being credited to this case.
    """
    _fail(path.is_file() and not path.is_symlink(), "official evaluator result must be a regular file")
    _fail(_inside(path.resolve(), output_dir), "official evaluator result must stay inside output-dir")
    official = _read_json(path, "official evaluator result")
    required = {
        "schema_version", "official_resolved", "submitted", "instance_id", "run_id",
        "report_path", "report_sha256", "dataset_path", "dataset_sha256",
        "predictions_path", "predictions_sha256", "evaluator_dataset_sha256",
        "evaluator_predictions_sha256", "command_sha256", "counts", "evaluator_python",
        "timeout_seconds",
    }
    _fail(required.issubset(official), "official evaluator result omits required provenance")
    _fail(official["schema_version"] == OFFICIAL_EVALUATOR_SCHEMA, "unsupported official evaluator result schema")
    _fail(official["instance_id"] == instance_id, "official evaluator instance_id does not match case")
    _fail(official["run_id"] == run_id, "official evaluator run_id does not match deterministic case run_id")
    _fail(official["submitted"] is True, "official evaluator did not submit the case")
    _fail(isinstance(official["official_resolved"], bool), "official evaluator official_resolved must be boolean")

    claimed_dataset = _strict_path(official["dataset_path"], "official evaluator dataset_path")
    _fail(claimed_dataset == dataset_path.resolve(), "official evaluator dataset_path does not match preflight dataset")
    _fail(_string(official["dataset_sha256"], "official evaluator dataset_sha256").lower() == dataset_sha256, "official evaluator dataset hash does not match preflight dataset")
    _fail(sha256_file(dataset_path) == dataset_sha256, "preflight dataset changed before evaluator verification")

    _fail(predictions_path.is_file() and not predictions_path.is_symlink(), "runner did not produce a regular preds.json")
    claimed_predictions = _strict_path(official["predictions_path"], "official evaluator predictions_path")
    _fail(claimed_predictions == predictions_path.resolve(), "official evaluator predictions_path does not match runner preds.json")
    predictions_sha256 = sha256_file(predictions_path)
    _fail(_string(official["predictions_sha256"], "official evaluator predictions_sha256").lower() == predictions_sha256, "official evaluator prediction hash does not match runner preds.json")

    report_path = _strict_path(official["report_path"], "official evaluator report_path")
    _fail(_inside(report_path, output_dir), "official evaluator report_path must stay inside output-dir")
    _fail(report_path.is_file() and not report_path.is_symlink(), "official evaluator report_path must be a regular file")
    report_sha256 = _string(official["report_sha256"], "official evaluator report_sha256").lower()
    _fail(bool(HEX64_RE.fullmatch(report_sha256)), "official evaluator report_sha256 must be SHA-256")
    _fail(sha256_file(report_path) == report_sha256, "official evaluator report hash does not match report_path")

    for field in ("evaluator_dataset_sha256", "evaluator_predictions_sha256", "command_sha256"):
        digest = _string(official[field], f"official evaluator {field}").lower()
        _fail(bool(HEX64_RE.fullmatch(digest)), f"official evaluator {field} must be SHA-256")
    _string(official["evaluator_python"], "official evaluator evaluator_python")
    _fail(isinstance(official["timeout_seconds"], int) and not isinstance(official["timeout_seconds"], bool) and official["timeout_seconds"] > 0, "official evaluator timeout_seconds must be positive")

    counts = official["counts"]
    count_keys = {
        "total_instances", "submitted_instances", "completed_instances",
        "resolved_instances", "unresolved_instances", "error_instances",
    }
    _fail(isinstance(counts, dict) and set(counts) == count_keys, "official evaluator counts are incomplete")
    for key in count_keys:
        _fail(isinstance(counts[key], int) and not isinstance(counts[key], bool) and counts[key] >= 0, f"official evaluator counts.{key} must be a non-negative integer")
    _fail(counts["total_instances"] == counts["submitted_instances"] == counts["completed_instances"] == 1, "official evaluator counts must describe exactly one submitted, completed instance")
    _fail(counts["error_instances"] == 0, "official evaluator counts contain errors")
    _fail(counts["resolved_instances"] + counts["unresolved_instances"] == 1, "official evaluator counts must contain exactly one resolved or unresolved instance")
    _fail(counts["resolved_instances"] == (1 if official["official_resolved"] else 0), "official evaluator resolved count disagrees with official_resolved")
    _fail(counts["unresolved_instances"] == (0 if official["official_resolved"] else 1), "official evaluator unresolved count disagrees with official_resolved")
    return official


def inventory_artifacts(root: Path, output_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not root.exists():
        return records
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        records.append({"kind": _artifact_kind(path), "path": str(path.relative_to(output_dir)), "sha256": sha256_file(path), "size": path.stat().st_size})
    return records


def _single_artifact(
    records: Sequence[Mapping[str, Any]], *, label: str, predicate: Any
) -> str:
    matches = [str(item["path"]) for item in records if predicate(Path(str(item["path"])))]
    _fail(len(matches) == 1, f"completed assignment case requires exactly one {label}; found {len(matches)}")
    return matches[0]


def _result(case: Mapping[str, Any], status: str, *, reason: str | None = None, **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"schema_version": RESULT_SCHEMA, "resume_key": case["resume_key"], "status": status}
    if reason:
        value["reason"] = reason
    value.update(extra)
    return value


def _upstream_endpoint(api_base: str) -> tuple[str, int, str]:
    parsed = urlparse(api_base)
    _fail(parsed.scheme == "http", "request proxy requires an http model.api_base; TLS termination is outside the reviewed proxy")
    _fail(bool(parsed.hostname), "model.api_base must include an upstream host")
    try:
        port = parsed.port or 80
    except ValueError as exc:
        raise CaseRunnerError(f"model.api_base has an invalid port: {api_base}") from exc
    _fail(1 <= port <= 65535, "model.api_base port is outside the valid range")
    return str(parsed.hostname), port, parsed.path.rstrip("/")


def _proxy_api_base(path: str, port: int) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _candidate_proxy_ports(run_id: str) -> list[int]:
    base = 20000 + (int(hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8], 16) % 20000)
    return [20000 + ((base - 20000 + offset) % 20000) for offset in range(128)]


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _terminate_proxy(process: subprocess.Popen[str] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        process.terminate()
    try:
        return process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def _start_request_proxy(
    *,
    manifest: Mapping[str, Any],
    repo: Path,
    output_dir: Path,
    run_id: str,
    deadline_seconds: int,
    adaptive_config_path: Path | None = None,
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    proxy_path = _strict_path(manifest["integrity"]["request_proxy_path"], "integrity.request_proxy_path")
    upstream_host, upstream_port, upstream_path = _upstream_endpoint(str(manifest["model"]["api_base"]))
    events_path = output_dir / "request_proxy.jsonl"
    stdout_path = output_dir / "request_proxy.stdout.log"
    stderr_path = output_dir / "request_proxy.stderr.log"
    command_hash_value: str | None = None
    process: subprocess.Popen[str] | None = None
    command: list[str] = []
    for port in _candidate_proxy_ports(run_id):
        if not _port_is_free(port):
            continue
        command = [
            sys.executable, str(proxy_path),
            "--listen-host", "127.0.0.1", "--listen-port", str(port),
            "--upstream-host", upstream_host, "--upstream-port", str(upstream_port),
            "--events", str(events_path), "--timeout-seconds", str(float(deadline_seconds)),
        ]
        if adaptive_config_path is not None:
            command.extend(["--adaptive-runtime-config", str(adaptive_config_path)])
        command_hash_value = command_hash(command)
        env = os.environ.copy()
        source_root = repo / "src"
        if source_root.is_dir():
            env["PYTHONPATH"] = str(source_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(repo),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                text=True,
                shell=False,
                close_fds=True,
                env=env,
            )
            ready_line = ""
            expected = f"request proxy listening on 127.0.0.1:{port}; upstream={upstream_host}:{upstream_port}"
            deadline = time.monotonic() + min(15.0, max(1.0, float(deadline_seconds)))
            assert process.stdout is not None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                readable, _, _ = select.select([process.stdout], [], [], min(0.25, max(0.0, deadline - time.monotonic())))
                if not readable:
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                stdout_handle.write(line)
                stdout_handle.flush()
                ready_line = line.strip()
                if ready_line == expected:
                    break
            _fail(ready_line == expected, "request proxy did not report readiness on the reviewed endpoint")
            _fail(process.poll() is None, "request proxy exited during startup")
            metadata = {
                "schema_version": "assignment-request-proxy-run.v1",
                "script_path": str(proxy_path),
                "script_sha256": sha256_file(proxy_path),
                "command": command,
                "command_sha256": command_hash_value,
                "upstream_api_base": str(manifest["model"]["api_base"]),
                "listen_api_base": _proxy_api_base(upstream_path, port),
                "events_path": str(events_path.relative_to(output_dir)),
                "stdout_path": str(stdout_path.relative_to(output_dir)),
                "stderr_path": str(stderr_path.relative_to(output_dir)),
                "readiness": "stdout_exact_match_without_model_request",
                "adaptive_runtime_enabled": adaptive_config_path is not None,
            }
            _atomic_json(output_dir / "request_proxy_provenance.json", metadata)
            return process, metadata
        except BaseException:
            _terminate_proxy(process)
            process = None
            raise
        finally:
            stdout_handle.close()
            stderr_handle.close()
    _fail(False, "no deterministic free loopback endpoint was available for the request proxy")
    raise AssertionError("unreachable")


def _validate_proxy_events(path: Path, *, adaptive_required: bool = False) -> dict[str, Any]:
    _fail(path.is_file() and not path.is_symlink(), f"request proxy events are missing: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CaseRunnerError(f"cannot read request proxy events: {exc}") from exc
    _fail(bool(lines), "request proxy events are empty")
    seen: set[str] = set()
    for index, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseRunnerError(f"request proxy event {index} is not JSON: {exc}") from exc
        _fail(isinstance(row, dict), f"request proxy event {index} is not an object")
        _fail(row.get("schema_version") == "observability.request-proxy.v1", f"request proxy event {index} has an unsupported schema")
        _fail(row.get("event_type") == "model_request_boundary", f"request proxy event {index} has an invalid event type")
        request_id = row.get("request_id")
        _fail(isinstance(request_id, str) and request_id and request_id not in seen, f"request proxy event {index} has a duplicate or missing request_id")
        seen.add(request_id)
        _fail(row.get("provenance") == "measured" and row.get("request_mutation") is False, f"request proxy event {index} lacks measured immutable provenance")
        _fail(isinstance(row.get("status_code"), int) and not isinstance(row["status_code"], bool) and 200 <= row["status_code"] < 300 and row.get("error") is None, f"request proxy event {index} is unsuccessful")
        _fail(isinstance(row.get("start_mono_ns"), int) and not isinstance(row["start_mono_ns"], bool) and isinstance(row.get("end_mono_ns"), int) and not isinstance(row["end_mono_ns"], bool) and row["end_mono_ns"] > row["start_mono_ns"], f"request proxy event {index} has invalid monotonic bounds")
        _fail(isinstance(row.get("duration_ms"), (int, float)) and not isinstance(row["duration_ms"], bool) and row["duration_ms"] > 0, f"request proxy event {index} has invalid duration")
        _fail(
            math.isclose(
                float(row["duration_ms"]),
                (row["end_mono_ns"] - row["start_mono_ns"]) / 1_000_000.0,
                rel_tol=1e-6,
                abs_tol=1e-6,
            ),
            f"request proxy event {index} duration disagrees with monotonic bounds",
        )
        for key in ("request_sha256", "response_sha256"):
            _fail(isinstance(row.get(key), str) and bool(HEX64_RE.fullmatch(row[key])), f"request proxy event {index} has invalid {key}")
        if adaptive_required:
            for key in ("adaptive_prediction_record_sha256", "adaptive_label_record_sha256"):
                _fail(isinstance(row.get(key), str) and bool(HEX64_RE.fullmatch(row[key])), f"request proxy event {index} has invalid {key}")
            _fail(row.get("prediction_durable_before_upstream") is True, f"request proxy event {index} lacks pre-dispatch prediction proof")
    return {"event_count": len(lines), "sha256": sha256_file(path)}


def _validate_adaptive_outputs(metadata: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(_string(metadata.get("protocol_root"), "adaptive protocol root")).resolve()
    manifest = root / "adaptive_prediction_manifest.json"
    score_path = root / "adaptive_score_report.json"
    journal = root / "adaptive_events.jsonl"
    manifest_sha = _verify_sidecar(manifest, "adaptive prediction manifest")
    score_sha = _verify_sidecar(score_path, "adaptive score report")
    _fail(journal.is_file() and not journal.is_symlink(), "adaptive journal is missing")
    score = _read_json(score_path, "adaptive score report")
    _fail(score.get("schema_version") == "assignment.adaptive-event-score.v1", "unsupported adaptive score report")
    _fail(score.get("prediction_manifest_sha256") == manifest_sha, "adaptive score does not bind the frozen prediction manifest")
    _fail(isinstance(score.get("passed"), bool), "adaptive score report lacks a boolean acceptance result")
    _fail(isinstance(score.get("event_scores"), list) and bool(score["event_scores"]), "adaptive score report has no event scores")
    return {
        **dict(metadata),
        "prediction_manifest_path": str(manifest),
        "prediction_manifest_sha256": manifest_sha,
        "journal_path": str(journal),
        "journal_sha256": sha256_file(journal),
        "score_path": str(score_path),
        "score_sha256": score_sha,
        "passed": score["passed"],
        "unavailable_event_count": score.get("unavailable_event_count"),
        "event_count": len(score["event_scores"]),
        "trajectory_score": score.get("trajectory_score"),
    }


def execute(args: argparse.Namespace) -> int:
    manifest_path = args.runtime_manifest or (Path(os.environ["ASSIGNMENT_RUNTIME_MANIFEST"]) if os.environ.get("ASSIGNMENT_RUNTIME_MANIFEST") else None)
    _fail(manifest_path is not None, "runtime manifest is required via --runtime-manifest or ASSIGNMENT_RUNTIME_MANIFEST")
    manifest_path = Path(manifest_path).resolve()
    manifest_digest = _verify_manifest_sidecar(manifest_path)
    manifest = load_manifest(manifest_path)
    case_path = Path(args.case_spec).resolve()
    output_dir = Path(args.output_dir).resolve()
    _fail(case_path.is_file(), f"case specification is unavailable: {case_path}")
    case = load_case(case_path)
    run_id = f"assignment-{hashlib.sha256(case['resume_key'].encode()).hexdigest()[:16]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    _fail(_inside(case_path, output_dir), "case specification must be inside output-dir")
    lock_path = output_dir / ".case-runner.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CaseRunnerError("another case-runner process holds the output lock") from exc
        result_path = output_dir / "case_result.json"
        _fail(not result_path.exists(), f"output collision: existing case result must be handled by the matrix resume layer: {result_path}")
        repo, git_state = validate_checkout(manifest)
        integrity = _verify_execution_integrity(manifest, repo)
        static = validate_static_environment(manifest, case, repo)
        adaptive: dict[str, Any] | None = None
        if args.adaptive_runtime_config is not None:
            adaptive = _validate_adaptive_config(
                Path(args.adaptive_runtime_config),
                manifest_path=manifest_path,
                manifest_sha256=manifest_digest,
                run_id=run_id,
                repo=repo,
                output_dir=output_dir,
            )
        validation = {"schema_version": "assignment-case-validation.v1", "status": "passed", "case_sha256": sha256_file(case_path), "manifest_sha256": manifest_digest, "integrity": integrity, "git": git_state, "suite": case["suite"], "instance_id": case["instance_id"], "settings": case["settings"], "dataset": static, "adaptive_runtime": adaptive}
        _atomic_json(output_dir / "validation.json", validation)
        if not args.execute:
            print(json.dumps(validation, sort_keys=True))
            return 0

        if manifest["runner"]["project"]:
            _fail(shutil.which("uv") is not None, "uv is required for the reviewed project runner")
        hardware = _probe_hardware(manifest)
        state_path = output_dir / "runner_state.json"
        attempt = 1
        if state_path.exists():
            previous = _read_json(state_path, "runner state")
            _fail(previous.get("status") != "completed", "successful runner state already exists")
            attempt = int(previous.get("attempt", 0)) + 1
        state = {"schema_version": STATE_SCHEMA, "status": "starting", "attempt": attempt, "case_sha256": validation["case_sha256"], "manifest_sha256": validation["manifest_sha256"], "git": git_state, "hardware": hardware}
        _atomic_json(state_path, state)
        runner_output = output_dir / "runner_attempts" / f"attempt-{attempt:03d}"
        _fail(not runner_output.exists(), f"output collision: runner output already exists: {runner_output}")
        runner_output.mkdir(parents=True)
        runner_instances_path, runner_instances_sha256 = _materialize_runner_instances(
            source=Path(static["instances_path"]),
            instance_id=case["instance_id"],
            output_dir=runner_output,
        )

        settings = case["settings"]
        runner_manifest = manifest["runner"]
        proxy_process: subprocess.Popen[str] | None = None
        proxy_metadata: dict[str, Any] = {}
        proxy_exit_before_stop: int | None = None
        runner_command: list[str] = []
        runner_result = None
        try:
            proxy_process, proxy_metadata = _start_request_proxy(
                manifest=manifest,
                repo=repo,
                output_dir=runner_output,
                run_id=f"assignment-{hashlib.sha256(case['resume_key'].encode()).hexdigest()[:16]}",
                deadline_seconds=case["per_case_deadline_seconds"],
                adaptive_config_path=Path(adaptive["config_path"]) if adaptive is not None else None,
            )
            proxy_api_base = str(proxy_metadata["listen_api_base"])
            reviewed_executable = (
                str(_strict_path(manifest["integrity"]["adaptive_runner_path"], "integrity.adaptive_runner_path"))
                if adaptive is not None
                else str(runner_manifest["executable"])
            )
            runner_command = build_command(
                executable=reviewed_executable,
                project=_resolve(repo, str(runner_manifest["project"])),
                config_path=_resolve(repo, str(runner_manifest["config_path"])),
                request_config_path=_resolve(repo, str(runner_manifest["request_config_path"])),
                instances_path=runner_instances_path,
                model=manifest["model"]["name"],
                model_revision=manifest["model"]["revision"],
                api_base=proxy_api_base,
                api_key=manifest["model"]["api_key"],
                instance_id=case["instance_id"],
                output_dir=runner_output,
                max_output_tokens=settings["max_output_tokens"],
                max_observation_length=settings["observation_length"],
                temperature=float(settings["temperature"]),
                per_instance_call_limit=settings["call_limit"],
                num_workers=1,
                extra_args=runner_manifest["extra_args"],
            )
            _fail("--num_workers" in runner_command and runner_command[runner_command.index("--num_workers") + 1] == "1", "runner command does not enforce concurrency=1")
            result_template = str(manifest["evaluator"]["result_path"])
            gpu_identity = hardware["gpus"][0]
            normalization_spec_path = output_dir / "normalization_spec.json"
            normalization_spec = {
                "run_id": run_id,
                "suite": case["suite"],
                "repository": case["repository"],
                "category": case["repository"],
                "instance_id": case["instance_id"],
                "config_id": case["cell_id"],
                "repeat_id": "r0",
                "hardware_id": hashlib.sha256(_canonical(gpu_identity).encode("utf-8")).hexdigest(),
                "model_revision": manifest["pins"]["model_revision"],
                "swe_agent_revision": manifest["pins"]["swe_agent_revision"],
                "swe_bench_revision": manifest["pins"]["swe_bench_revision"],
                "source_dataset_path": static["instances_path"],
                "source_dataset_sha256": static["dataset_sha256"],
                "runner_instances_path": str(runner_instances_path.relative_to(output_dir)),
                "runner_instances_sha256": runner_instances_sha256,
                "command_sha256": command_hash(runner_command),
                "settings": settings,
                "sweep_parameter": case["variation"]["knob"] if case["variation"] else None,
                "sweep_value": case["variation"]["value"] if case["variation"] else None,
            }
            _atomic_json(normalization_spec_path, normalization_spec)
            result_values = {
                "case_spec": str(case_path),
                "output_dir": str(output_dir),
                "runner_output_dir": str(runner_output),
                "dataset_path": static["instances_path"],
                "predictions_path": str(runner_output / "preds.json"),
                "instance_id": case["instance_id"],
                "suite": case["suite"],
                "report_dir": str(output_dir / "official_evaluator"),
                "run_id": run_id,
            }
            result_fields = set(re.findall(r"\{([A-Za-z0-9_]+)\}", result_template))
            _fail(result_fields.issubset(result_values), "evaluator result path contains an unknown placeholder")
            evaluator_result_path = Path(result_template.format(**result_values))
            if not evaluator_result_path.is_absolute():
                evaluator_result_path = (output_dir / evaluator_result_path).resolve()
            else:
                evaluator_result_path = evaluator_result_path.resolve()
            evaluator_values = {**result_values, "evaluator_result": str(evaluator_result_path)}
            _fail(_inside(evaluator_result_path, output_dir), "evaluator result path must stay inside output-dir")
            evaluator_command = _format_argv(manifest["evaluator"]["command"], evaluator_values)
            state.update({"status": "running", "runner_command_hash": command_hash(runner_command), "runner_output_dir": str(runner_output.relative_to(output_dir)), "evaluator_command_hash": command_hash(evaluator_command), "proxy": proxy_metadata})
            _atomic_json(state_path, state)
            runner_config = RunnerConfig(
                command=runner_command,
                experiment_id=run_id,
                instance_id=case["instance_id"],
                dataset=case["suite"],
                work_root=runner_output / "reviewed_runner_artifacts",
                attempt_id=f"attempt-{attempt:03d}",
                cwd=_resolve(repo, str(runner_manifest["working_directory"])),
                timeout_seconds=case["per_case_deadline_seconds"],
                model_revision=manifest["pins"]["model_revision"],
                swe_agent_revision=manifest["pins"]["swe_agent_revision"],
                swe_bench_revision=manifest["pins"]["swe_bench_revision"],
                config={"assignment_case": case["resume_key"], "dataset_revision": static["dataset_revision"], "request_proxy": proxy_metadata, "adaptive_runtime": adaptive},
                environment=(
                    {"ASSIGNMENT_ADAPTIVE_RUNTIME_CONFIG": str(adaptive["config_path"])}
                    if adaptive is not None
                    else {}
                ),
            )
            runner_result = run_sweagent(runner_config, evaluator_command=evaluator_command)
        finally:
            proxy_exit_before_stop = proxy_process.poll() if proxy_process is not None else None
            _terminate_proxy(proxy_process)
        _fail(runner_result is not None, "reviewed SWE-agent runner produced no result")
        _fail(proxy_exit_before_stop is None, "request proxy exited before the SWE-agent runner completed")
        proxy_events = runner_output / "request_proxy.jsonl"
        proxy_event_summary = _validate_proxy_events(proxy_events, adaptive_required=adaptive is not None)
        proxy_metadata.update({
            "event_count": proxy_event_summary["event_count"],
            "events_sha256": proxy_event_summary["sha256"],
            "exit_code_before_stop": proxy_exit_before_stop,
        })
        _atomic_json(runner_output / "request_proxy_provenance.json", proxy_metadata)
        eval_metadata: dict[str, Any] = {}
        eval_files = sorted(runner_result.output_dir.rglob("eval.json"))
        if eval_files:
            eval_metadata = _read_json(eval_files[-1], "runner evaluator status")
            _fail(eval_metadata.get("status") == "completed", "official evaluator command did not complete successfully")
        adaptive_result = _validate_adaptive_outputs(adaptive) if adaptive is not None else None
        if adaptive_result is not None:
            _fail(
                adaptive_result["passed"] is True,
                "adaptive event/E2E prediction did not pass the 25% acceptance gate",
            )
        refs = inventory_artifacts(runner_output, output_dir)
        if adaptive is not None:
            for item in inventory_artifacts(Path(adaptive["protocol_root"]), output_dir):
                if not any(existing["path"] == item["path"] for existing in refs):
                    refs.append(item)
        eval_status = "missing"
        official: dict[str, Any] = {}
        predictions_path = runner_output / "preds.json"
        if evaluator_result_path.is_file() and not evaluator_result_path.is_symlink():
            official = _validate_official_evaluator_result(
                path=evaluator_result_path,
                output_dir=output_dir,
                instance_id=case["instance_id"],
                run_id=run_id,
                dataset_path=Path(static["instances_path"]),
                dataset_sha256=static["dataset_sha256"],
                predictions_path=predictions_path,
            )
            _fail(official[manifest["evaluator"]["resolved_field"]] == official["official_resolved"], "runtime manifest resolved field disagrees with evaluator schema")
            _fail(official[manifest["evaluator"]["submitted_field"]] is True, "runtime manifest submitted field is not true")
            eval_status = "completed"
        else:
            eval_status = "missing"
        evaluator_ref = str(evaluator_result_path.relative_to(output_dir)) if evaluator_result_path.is_relative_to(output_dir) else None
        if evaluator_ref and evaluator_result_path.is_file() and not any(item["path"] == evaluator_ref for item in refs):
            refs.append({"kind": "evaluator", "path": evaluator_ref, "sha256": sha256_file(evaluator_result_path), "size": evaluator_result_path.stat().st_size})
        for item in inventory_artifacts(output_dir / "official_evaluator", output_dir):
            if not any(existing["path"] == item["path"] for existing in refs):
                refs.append(item)
        for source, kind in ((case_path, "case_spec"), (output_dir / "validation.json", "validation")):
            reference = str(source.relative_to(output_dir))
            if not any(existing["path"] == reference for existing in refs):
                refs.append({"kind": kind, "path": reference, "sha256": sha256_file(source), "size": source.stat().st_size})
        normalization_reference = str(normalization_spec_path.relative_to(output_dir))
        refs.append({"kind": "normalization_spec", "path": normalization_reference, "sha256": sha256_file(normalization_spec_path), "size": normalization_spec_path.stat().st_size})
        refs.sort(key=lambda item: item["path"])
        completed = runner_result.status == "completed" and eval_status == "completed"
        normalization_sources: dict[str, str | None] = {
            "run_spec": normalization_reference,
            "trajectory": None,
            "model_events": None,
            "runner_summary": None,
            "official_evaluator_result": evaluator_ref,
        }
        if completed:
            normalization_sources.update({
                "trajectory": _single_artifact(
                    refs,
                    label="SWE-agent trajectory",
                    predicate=lambda path: path.name in {"trajectory.json", "trajectory.traj"} or path.suffix == ".traj",
                ),
                "model_events": str(proxy_events.relative_to(output_dir)),
                "runner_summary": _single_artifact(
                    refs,
                    label="reviewed runner summary",
                    predicate=lambda path: path.name == "summary.json" and "reviewed_runner_artifacts" in path.parts,
                ),
            })
        status = "completed" if completed else ("timeout" if runner_result.status == "timeout" else "failed")
        reason = None if completed else ("missing_official_evaluator_result" if eval_status != "completed" else f"runner_{runner_result.status}")
        final = _result(
            case,
            status,
            reason=reason,
            run_id=run_id,
            case_sha256=validation["case_sha256"],
            manifest_sha256=validation["manifest_sha256"],
            integrity=integrity,
            git=git_state,
            hardware=hardware,
            runner={"status": runner_result.status, "returncode": runner_result.returncode, "output_dir": str(runner_output.relative_to(output_dir)), "command_hash": runner_result.command_hash},
            proxy=proxy_metadata,
            evaluator={"status": eval_status, "runner_status": eval_metadata.get("status"), "result_path": evaluator_ref, "official_resolved": official.get(manifest["evaluator"]["resolved_field"]), "submitted": official.get(manifest["evaluator"]["submitted_field"])},
            adaptive_runtime=adaptive_result,
            normalization_sources=normalization_sources,
            artifacts=refs,
        )
        _atomic_json(result_path, final)
        result_digest = sha256_file(result_path)
        Path(str(result_path) + ".sha256").write_text(
            f"{result_digest}  {result_path.name}\n", encoding="utf-8"
        )
        state.update({"status": status, "ended_epoch": int(time.time()), "result_sha256": result_digest, "artifact_count": len(refs)})
        _atomic_json(state_path, state)
        print(json.dumps(final, sort_keys=True))
        return 0 if completed else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--case-spec", required=True, type=Path)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--runtime-manifest", type=Path)
    result.add_argument(
        "--adaptive-runtime-config",
        type=Path,
        help="sealed per-run adaptive predictor configuration; enables predict-before-reveal event scoring",
    )
    result.add_argument("--execute", action="store_true", help="run the reviewed SWE-agent and official evaluator")
    result.add_argument("--validate-only", action="store_true", help="validate and launch nothing (the default)")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute and args.validate_only:
        print("NOT_READY: --execute and --validate-only are mutually exclusive", file=sys.stderr)
        return 1
    try:
        return execute(args)
    except (CaseRunnerError, RunnerContractError, OSError, ValueError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
