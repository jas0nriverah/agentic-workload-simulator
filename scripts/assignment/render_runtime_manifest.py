#!/usr/bin/env python3
"""Render a release-bound, external assignment runtime manifest.

This command is deliberately offline.  It reads Git metadata and a checked-in
JSON template, then writes a manifest whose external dependencies live below a
caller-owned work root.  It never installs packages, probes a GPU, contacts a
provider, or starts a workload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from agentic_sim.telemetry.cpu_policy import WORKER_CPUS, policy_config  # noqa: E402

TEMPLATE_DEFAULT = ROOT / "configs/assignment_runtime_manifest.example.json"
SCHEMA = "assignment-runtime-manifest.v1"
ZERO_COMMIT = "0" * 40
COMMIT_LENGTH = 40
DATASET_HASHES = {
    "lite": "7f54792b83bf491c0a905770a00ce7fa28836552d37c7ea0e9e2bae4c53f33fb",
    "verified": "52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da",
}
DATASET_SOURCE_PARQUET_HASHES = {
    "lite": "f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b",
    "verified": "43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21",
}
DATASET_REVISIONS = {
    "lite": "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
    "verified": "91aa3ed51b709be6457e12d00300a6a596d4c6a3",
}
PINS = {
    "swe_agent_revision": "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
    "swe_bench_revision": "726c5461e2ef52d83cf1ea2107870a8bb3328d57",
}
HARDWARE = {
    "h100": {
        "gpu_names": ["NVIDIA H100 80GB HBM3", "NVIDIA H100 PCIe 80GB"],
        "minimum_memory_mib": 80000,
        "compute_capability": "9.0",
    },
    "a100": {
        "gpu_names": ["NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-80GB"],
        "minimum_memory_mib": 80000,
        "compute_capability": "8.0",
    },
}


class RenderError(ValueError):
    """The requested render would produce an unsafe or incomplete manifest."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise RenderError(message)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RenderError(f"cannot read {label} {path}: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must be a JSON object: {path}")
    return value


def _git(repo: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"Git command failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RenderError(f"Git command failed ({' '.join(args)}): {detail}")
    return result.stdout.strip()


def git_state(repo: Path, expected_branch: str | None) -> dict[str, str]:
    git_path = _git(repo, ["rev-parse", "--show-toplevel"])
    actual_repo = Path(git_path).resolve()
    _fail(actual_repo == repo, f"repo root is not the Git top level: {repo}")
    branch = _git(repo, ["branch", "--show-current"])
    commit = _git(repo, ["rev-parse", "HEAD"]).lower()
    dirty = _git(repo, ["status", "--porcelain", "--untracked-files=all"])
    _fail(bool(branch), "repository is in detached HEAD state")
    _fail(len(commit) == COMMIT_LENGTH and all(char in "0123456789abcdef" for char in commit), "Git HEAD is not a full commit SHA")
    _fail(not dirty, "working tree is dirty; render only from a clean repository")
    if expected_branch is not None:
        _fail(branch == expected_branch, f"wrong Git branch: expected {expected_branch}, got {branch}")
    return {"branch": branch, "commit": commit}


def _absolute_directory(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    _fail(path.is_absolute(), f"{label} must be an absolute path")
    _fail("\x00" not in str(path), f"{label} contains a NUL")
    return path.resolve()


def _validate_template(template: dict[str, Any]) -> None:
    _fail(template.get("schema_version") == SCHEMA, "template has an unsupported schema_version")
    _fail(isinstance(template.get("pins"), dict), "template pins are missing")
    _fail(template.get("pins", {}).get("swe_agent_revision") == PINS["swe_agent_revision"], "template SWE-agent pin is incorrect")
    _fail(template.get("pins", {}).get("swe_bench_revision") == PINS["swe_bench_revision"], "template SWE-bench pin is incorrect")
    _fail(isinstance(template.get("datasets"), dict), "template datasets are missing")
    for suite in ("lite", "verified"):
        dataset = template["datasets"].get(suite)
        _fail(isinstance(dataset, dict), f"template dataset {suite} is missing")
        _fail(dataset.get("revision") == DATASET_REVISIONS[suite], f"template dataset {suite} revision is incorrect")
        _fail(dataset.get("sha256") == DATASET_HASHES[suite], f"template dataset {suite} SHA-256 is incorrect")
        _fail(dataset.get("source_parquet_sha256") == DATASET_SOURCE_PARQUET_HASHES[suite],
              f"template dataset {suite} source Parquet SHA-256 is incorrect")


def _path(root: Path, *parts: str) -> str:
    return str((root.joinpath(*parts)).resolve())


def _file_sha256(path: Path, label: str) -> str:
    _fail(path.is_file() and not path.is_symlink(), f"{label} is not a regular file: {path}")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RenderError(f"cannot hash {label} {path}: {exc}") from exc


def render(template: dict[str, Any], *, repo: Path, work_root: Path, hardware: str, evaluator_python: Path, state: dict[str, str], remote_hardware_profile: Path | None = None, cpu_worker_id: str | None = None) -> dict[str, Any]:
    _validate_template(template)
    profile = HARDWARE[hardware]
    value = json.loads(json.dumps(template))

    value["required_branch"] = state["branch"]
    value["required_commit"] = state["commit"]
    value["repository_root"] = str(repo)
    case_runner = (repo / "scripts/assignment/sweagent_case_runner.py").resolve()
    evaluator_adapter = (repo / "scripts/assignment/evaluate_swebench_case.py").resolve()
    request_config = (repo / "cloud/lambda/sweagent_request.yaml").resolve()
    request_proxy = (repo / "scripts/observability/request_proxy.py").resolve()
    adaptive_runner = (repo / "scripts/assignment/sweagent_adaptive_runner.py").resolve()
    adaptive_runtime = (repo / "scripts/assignment/adaptive_runtime.py").resolve()
    adaptive_protocol = (repo / "scripts/assignment/adaptive_event_protocol.py").resolve()
    event_simulator = (repo / "src/agentic_sim/assignment/event_simulator.py").resolve()
    value["integrity"] = {
        "case_runner_path": str(case_runner),
        "case_runner_sha256": _file_sha256(case_runner, "assignment case runner"),
        "evaluator_adapter_path": str(evaluator_adapter),
        "evaluator_adapter_sha256": _file_sha256(evaluator_adapter, "official evaluator adapter"),
        "request_config_path": str(request_config),
        "request_config_sha256": _file_sha256(request_config, "SWE-agent request config"),
        "request_proxy_path": str(request_proxy),
        "request_proxy_sha256": _file_sha256(request_proxy, "request proxy"),
        "adaptive_runner_path": str(adaptive_runner),
        "adaptive_runner_sha256": _file_sha256(adaptive_runner, "adaptive SWE-agent wrapper"),
        "adaptive_runtime_path": str(adaptive_runtime),
        "adaptive_runtime_sha256": _file_sha256(adaptive_runtime, "adaptive runtime"),
        "adaptive_protocol_path": str(adaptive_protocol),
        "adaptive_protocol_sha256": _file_sha256(adaptive_protocol, "adaptive event protocol"),
        "event_simulator_path": str(event_simulator),
        "event_simulator_sha256": _file_sha256(event_simulator, "event simulator"),
    }
    value["pins"]["swe_agent_revision"] = PINS["swe_agent_revision"]
    value["pins"]["swe_bench_revision"] = PINS["swe_bench_revision"]
    for suite in ("lite", "verified"):
        value["datasets"][suite]["revision"] = DATASET_REVISIONS[suite]
        value["datasets"][suite]["sha256"] = DATASET_HASHES[suite]
        value["datasets"][suite]["source_parquet_sha256"] = DATASET_SOURCE_PARQUET_HASHES[suite]
        value["datasets"][suite]["instances_path"] = _path(work_root, "datasets", f"SWE-bench_{'Lite' if suite == 'lite' else 'Verified'}.jsonl")

    value["runner"] = {
        "executable": "sweagent",
        "project": _path(work_root, "repos", "SWE-agent"),
        "config_path": _path(work_root, "repos", "SWE-agent", "config", "default.yaml"),
        "request_config_path": str((repo / "cloud/lambda/sweagent_request.yaml").resolve()),
        "working_directory": _path(work_root, "repos", "SWE-agent"),
        "extra_args": [],
        "telemetry": value.get("runner", {}).get("telemetry"),
    }
    tool_runtime = template.get("runner", {}).get("tool_runtime")
    if tool_runtime is not None:
        # A configured isolated tool bundle must never be silently replaced
        # with the upstream default's task-Python-dependent installation.
        from agentic_sim.runners.tool_runtime import validate_tool_runtime_bundle

        _fail(isinstance(tool_runtime, dict) and set(tool_runtime) == {"manifest_path", "manifest_sha256", "config_sha256"},
              "template tool_runtime reference is malformed")
        tool_config = Path(template["runner"]["config_path"])
        tool_manifest = Path(tool_runtime["manifest_path"])
        _fail(tool_config.is_absolute() and tool_manifest.is_absolute(),
              "isolated tool runtime configuration and manifest must be absolute")
        _fail(_file_sha256(tool_config, "isolated tool runtime config") == tool_runtime["config_sha256"],
              "isolated tool runtime config SHA-256 mismatch")
        validate_tool_runtime_bundle(
            tool_manifest, tool_runtime["manifest_sha256"], expected_config_path=tool_config,
            expected_swe_agent_revision=value["pins"]["swe_agent_revision"],
        )
        value["runner"]["config_path"] = str(tool_config)
        value["runner"]["tool_runtime"] = dict(tool_runtime)
    telemetry = value["runner"]["telemetry"]
    if cpu_worker_id is None:
        _fail("cpu_policy" not in template.get("runner", {}),
              "template CPU policy requires explicit --cpu-worker-id; refusing to drop placement")
    else:
        value["runner"]["cpu_policy"] = policy_config(
            cpu_worker_id, repo / "src/agentic_sim/telemetry/cpu_policy.py",
        )
    if remote_hardware_profile is not None:
        profile_path = remote_hardware_profile.expanduser().resolve()
        profile_digest = _file_sha256(profile_path, "remote hardware profile")
        _fail(isinstance(telemetry, dict), "template runner.telemetry is missing")
        telemetry["remote_hardware_profile"] = {
            "path": str(profile_path),
            "sha256": profile_digest,
        }
    evaluator_script = str(evaluator_adapter)
    value["evaluator"] = {
        "command": [
            str(evaluator_python), evaluator_script,
            "--evaluator-project", _path(work_root, "repos", "SWE-bench"),
            "--evaluator-revision", value["pins"]["swe_bench_revision"],
            "--dataset", "{dataset_path}",
            "--predictions", "{predictions_path}",
            "--instance-id", "{instance_id}",
            "--report-dir", "{report_dir}",
            "--run-id", "{run_id}",
            "--result", "{evaluator_result}",
        ],
        "project": _path(work_root, "repos", "SWE-bench"),
        "result_path": "{output_dir}/evaluator_result.json",
        "resolved_field": "official_resolved",
        "submitted_field": "submitted",
    }
    value["hardware"] = {
        **profile,
        "one_gpu_only": True,
        "probe_command": ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"],
    }
    return value


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_secure(path: Path, payload: bytes, *, force: bool) -> None:
    sidecar = Path(str(path) + ".sha256")
    if not force:
        _fail(not path.exists(), f"refusing to overwrite existing manifest: {path}")
        _fail(not sidecar.exists(), f"refusing to overwrite existing sidecar: {sidecar}")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(payload).hexdigest()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        sidecar_payload = f"{digest}  {path.name}\n".encode("ascii")
        sidecar.write_bytes(sidecar_payload)
        os.chmod(sidecar, stat.S_IRUSR | stat.S_IWUSR)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="clean Git checkout to bind")
    parser.add_argument("--work-root", required=True, type=Path, help="absolute external work root")
    parser.add_argument("--hardware", choices=sorted(HARDWARE), required=True)
    parser.add_argument("--expected-branch", help="optional branch name to require")
    parser.add_argument("--evaluator-python", type=Path, help="evaluator interpreter; defaults to WORK_ROOT/venv/bin/python")
    parser.add_argument("--cpu-worker-id", choices=sorted(WORKER_CPUS),
                        help="bind the reviewed Sep 9 CPU placement (required for confirmation)")
    parser.add_argument(
        "--remote-hardware-profile",
        type=Path,
        help="sealed remote hardware descriptor; when supplied its path and SHA-256 are bound into runner.telemetry",
    )
    parser.add_argument("--template", type=Path, default=TEMPLATE_DEFAULT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--validation-only", action="store_true", help="print the rendered manifest without writing files")
    parser.add_argument("--force", action="store_true", help="replace an existing manifest and sidecar")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        repo = _absolute_directory(str(args.repo_root), "--repo-root")
        work_root = _absolute_directory(str(args.work_root), "--work-root")
        template_path = Path(args.template).expanduser().resolve()
        output = Path(args.output).expanduser().resolve()
        if args.evaluator_python:
            interpreter = args.evaluator_python.expanduser()
            # Resolve directory aliases while preserving the venv's executable
            # symlink. Resolving bin/python itself selects the base interpreter
            # and silently loses that environment's evaluator dependencies.
            _fail(interpreter.is_absolute() and "\x00" not in str(interpreter),
                  "--evaluator-python must be an absolute path without NUL")
            evaluator_python = interpreter.parent.resolve() / interpreter.name
        else:
            evaluator_python = work_root / "venv/bin/python"
        state = git_state(repo, args.expected_branch)
        rendered = render(_read_json(template_path, "runtime manifest template"), repo=repo, work_root=work_root, hardware=args.hardware, evaluator_python=evaluator_python, state=state, remote_hardware_profile=args.remote_hardware_profile, cpu_worker_id=args.cpu_worker_id)
        payload = _canonical_bytes(rendered)
        if args.validation_only:
            sys.stdout.buffer.write(payload)
            return 0
        _write_secure(output, payload, force=args.force)
        print(json.dumps({"manifest": str(output), "sha256": hashlib.sha256(payload).hexdigest(), "sidecar": str(output) + ".sha256", "mode": "0600"}, sort_keys=True))
        return 0
    except (RenderError, OSError, ValueError) as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
