#!/usr/bin/env python3
"""Read-only, fail-independent A100 setup diagnostics.

This command is intentionally separate from the production A100 entrypoints.
It only inspects local metadata, command availability, runtime state, pinned
cache paths, and disk state.  It never starts a container or server, invokes
Nsight tracing, sends a request, reads validation rows, or creates experiment
directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs/a100_final_validation.json"
DEFAULT_MANIFEST = Path("/mnt/eic-work/a100-startup.env")
DEFAULT_JSON_OUT = Path("/tmp/a100-diagnostic.json")
EXPECTED_BRANCH = "parallel-h100-shards"
# The release SHA is intentionally bound by the external manifest. Keeping a
# hard-coded SHA here would make every legitimate repository fix stale the
# diagnostic before the next sealed run.
EXPECTED_COMMIT = "<manifest-bound-release-commit>"
EXPECTED_PROTOCOL_SHA256 = "109c825bc799f7f25ffecb1136e1414d894f5a8f758fb242037f5250f4b50788"
EXPECTED_IMAGE = (
    "vllm/vllm-openai:v0.10.0@"
    "sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
)
EXPECTED_MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
EXPECTED_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
EXPECTED_NSYS_CONTAINER_PATH = "/host-cuda/bin/nsys"
EXPECTED_NSYS_HOST_PATH = "/usr/local/cuda/bin/nsys"
EXPECTED_NSYS_VERSION_PREFIX = "2025.1.3"
EXPECTED_MAX_WALL_SECONDS = 14400
DEFAULT_MIN_FREE_BYTES = 20 * 1024 * 1024 * 1024
H100_ARTIFACT_ROOT = ROOT / "artifacts/h100_final_validation"


class DiagnosticFailure(Exception):
    """A check failure with user-actionable remediation."""

    def __init__(self, detail: str, remediation: str, evidence: Optional[Mapping[str, Any]] = None):
        super().__init__(detail)
        self.detail = detail
        self.remediation = remediation
        self.evidence = dict(evidence or {})


@dataclass
class CheckResult:
    check_id: str
    category: str
    status: str
    detail: str
    remediation: str
    evidence: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
            "remediation": self.remediation,
            "evidence": self.evidence,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagnosticFailure(
            f"cannot read protocol JSON: {path} ({exc})",
            "Restore the tracked A100 protocol file and verify it is valid JSON.",
        ) from exc
    if not isinstance(value, dict):
        raise DiagnosticFailure(
            f"protocol JSON root is not an object: {path}",
            "Restore the tracked A100 protocol file from the required commit.",
        )
    return value


def _read_manifest(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DiagnosticFailure(
            f"external startup manifest is unavailable: {path}",
            f"Copy cloud/gcp/a100_startup_manifest.env.example to {path} outside Git and replace REQUIRED_COMMIT with the exact current pushed SHA.",
        ) from exc
    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise DiagnosticFailure(
                f"manifest line {number} is not KEY=VALUE",
                "Repair the external manifest without sourcing it; keep one KEY=VALUE entry per line.",
            )
        key, value = line.split("=", 1)
        if not key or key in values or any(char.isspace() for char in key):
            raise DiagnosticFailure(
                f"manifest key is duplicated or invalid at line {number}",
                "Remove duplicate/invalid manifest keys and keep the manifest outside the checkout.",
            )
        values[key] = value
    return values


def _run(command: List[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise DiagnosticFailure(
            f"required command is unavailable: {command[0]}",
            f"Install or expose {command[0]} on PATH, then rerun this diagnostic.",
        ) from exc
    except subprocess.CalledProcessError as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        raise DiagnosticFailure(
            f"command failed ({' '.join(command)}): {output.strip() or exc}",
            f"Resolve the reported {command[0]} failure and rerun this diagnostic; do not bypass the production guard.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise DiagnosticFailure(
            f"command timed out: {' '.join(command)}",
            f"Restore a responsive {command[0]} service and rerun this diagnostic.",
        ) from exc


def _manifest(args: argparse.Namespace) -> Dict[str, str]:
    path = args.manifest
    if path is None:
        raise DiagnosticFailure(
            "no external startup manifest was supplied",
            f"Create {DEFAULT_MANIFEST} outside Git from cloud/gcp/a100_startup_manifest.env.example and replace REQUIRED_COMMIT with the exact current pushed SHA.",
        )
    return _read_manifest(path)


def _protocol(args: argparse.Namespace) -> Dict[str, Any]:
    return _read_json(args.config)


def _require_manifest_value(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not value:
        raise DiagnosticFailure(
            f"manifest value {key} is missing",
            f"Add the pinned {key} value from cloud/gcp/a100_startup_manifest.env.example; do not source the manifest.",
        )
    return value


def _existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _outside_checkout(path: Path) -> bool:
    resolved = Path(os.path.abspath(os.path.expanduser(str(path))))
    root = Path(os.path.abspath(str(ROOT)))
    h100 = Path(os.path.abspath(str(H100_ARTIFACT_ROOT)))
    return resolved != root and root not in resolved.parents and h100 not in resolved.parents


def _writable_without_creation(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise DiagnosticFailure(
            f"external target does not exist: {path}",
            f"Create/mount the external target {path} before execution; the diagnostic will not create canonical directories.",
            {"path": str(path), "exists": False},
        )
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise DiagnosticFailure(
            f"external target is not a writable directory: {path}",
            f"Grant write and search access to the existing external target {path}; the diagnostic will not create or repair it.",
            {"path": str(path), "exists": True, "is_dir": path.is_dir()},
        )
    return {"path": str(path), "target_exists": True, "target_writable": True}


class A100Diagnostic:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.results: List[CheckResult] = []

    def check(self, check_id: str, category: str, remediation: str, probe: Callable[[], Any]) -> None:
        try:
            value = probe()
        except DiagnosticFailure as exc:
            self.results.append(CheckResult(check_id, category, "fail", exc.detail, exc.remediation, exc.evidence))
        except Exception as exc:  # pragma: no cover - defensive boundary for independent checks
            self.results.append(
                CheckResult(
                    check_id,
                    category,
                    "fail",
                    f"unexpected diagnostic error: {type(exc).__name__}: {exc}",
                    remediation,
                    {},
                )
            )
        else:
            evidence = value if isinstance(value, dict) else {"value": value}
            self.results.append(CheckResult(check_id, category, "pass", "check passed", remediation, evidence))

    def run(self) -> List[CheckResult]:
        # Every entry is deliberately a separate probe. A missing manifest or
        # unavailable GPU must not prevent Docker, Nsight, disk, or Git results.
        self.check(
            "branch",
            "branch_sha_protocol",
            f"Check out {EXPECTED_BRANCH} and rerun the diagnostic.",
            self._branch,
        )
        self.check(
            "commit",
            "branch_sha_protocol",
            "Use the exact commit recorded in the external manifest without modifying tracked files.",
            self._commit,
        )
        self.check(
            "clean_checkout",
            "branch_sha_protocol",
            "Commit or safely remove unrelated working-tree changes before any A100 execution.",
            self._clean_checkout,
        )
        self.check(
            "protocol_hash",
            "branch_sha_protocol",
            f"Restore configs/a100_final_validation.json from the required commit; expected SHA-256 is {EXPECTED_PROTOCOL_SHA256}.",
            self._protocol_hash,
        )
        self.check(
            "manifest_binding",
            "branch_sha_protocol",
            "Create the external manifest from the example and set REQUIRED_COMMIT to the exact current pushed SHA; never source it.",
            self._manifest_binding,
        )
        self.check(
            "nvidia_smi",
            "hardware_detected",
            "Install the NVIDIA driver/toolkit and expose nvidia-smi before A100 execution.",
            self._nvidia_smi,
        )
        self.check(
            "a100_identity",
            "hardware_detected",
            "Use exactly one NVIDIA A100 80GB with Ampere compute capability 8.0; stop for any mismatch.",
            self._a100_identity,
        )
        self.check(
            "gpu_isolation",
            "hardware_detected",
            "Stop unrelated GPU workloads and rerun until nvidia-smi reports no compute processes.",
            self._gpu_isolation,
        )
        self.check(
            "docker_command",
            "docker_nvidia_runtime",
            "Install Docker and expose the docker client on PATH.",
            self._docker_command,
        )
        self.check(
            "docker_daemon",
            "docker_nvidia_runtime",
            "Start/fix the Docker daemon, then rerun this read-only diagnostic.",
            self._docker_daemon,
        )
        self.check(
            "nvidia_runtime",
            "docker_nvidia_runtime",
            "Install/configure nvidia-container-toolkit and register the NVIDIA Docker runtime.",
            self._nvidia_runtime,
        )
        self.check(
            "pinned_image",
            "pinned_image",
            "Pull the exact pinned image digest with the approved runtime setup; never substitute a tag or image.",
            self._pinned_image,
        )
        self.check(
            "model_cache",
            "model_tokenizer",
            "Populate the exact offline model cache and snapshot revision from the external manifest.",
            self._model_cache,
        )
        self.check(
            "tokenizer_cache",
            "model_tokenizer",
            "Populate config.json and tokenizer files in the exact pinned snapshot; do not download during validation.",
            self._tokenizer_cache,
        )
        self.check(
            "nsight_discovery",
            "nsight",
            "Install Nsight Systems 2025.1.3 and expose nsys on PATH or /usr/local/cuda/bin/nsys.",
            self._nsight_discovery,
        )
        self.check(
            "nsight_required_path",
            "nsight",
            f"Set A100_NSYS_BIN to the container path {EXPECTED_NSYS_CONTAINER_PATH}; do not use a host-only or substituted path.",
            self._nsight_required_path,
        )
        self.check(
            "nsight_host_mapping",
            "nsight",
            f"Provide the required host Nsight mapping at {EXPECTED_NSYS_HOST_PATH} with version {EXPECTED_NSYS_VERSION_PREFIX}; /opt/nvidia candidates are informational only.",
            self._nsight_host_mapping,
        )
        self.check(
            "external_roots",
            "external_artifact_root",
            "Mount external A100 work/artifact/recovery roots outside Git and ensure their nearest parents are writable.",
            self._external_roots,
        )
        self.check(
            "artifact_collision",
            "external_artifact_root",
            "Use a fresh, empty canonical A100 artifact root; preserve and investigate any existing run before execution.",
            self._artifact_collision,
        )
        self.check(
            "disk_space",
            "disk_deadline",
            f"Free at least {self.args.min_free_bytes} bytes on the external A100 work filesystem before execution.",
            self._disk_space,
        )
        self.check(
            "deadline",
            "disk_deadline",
            "Keep the production hard wall exactly 14,400 seconds and set its deadline before the first calibration request.",
            self._deadline,
        )
        return self.results

    def _branch(self) -> Dict[str, Any]:
        result = _run(["git", "branch", "--show-current"])
        branch = result.stdout.strip()
        if branch != EXPECTED_BRANCH:
            raise DiagnosticFailure(f"wrong branch: {branch or '<detached>'}", f"Check out {EXPECTED_BRANCH}.", {"actual": branch})
        return {"branch": branch}

    def _commit(self) -> Dict[str, Any]:
        result = _run(["git", "rev-parse", "HEAD"])
        commit = result.stdout.strip()
        expected = None
        if self.args.manifest is not None and Path(self.args.manifest).is_file():
            expected = _read_manifest(self.args.manifest).get("REQUIRED_COMMIT")
        if expected and commit != expected:
            raise DiagnosticFailure(
                f"Git SHA does not match the external manifest: {commit}",
                f"Set REQUIRED_COMMIT={commit} only after pushing this exact clean checkout, then rerun.",
                {"actual": commit, "manifest_required_commit": expected},
            )
        return {"commit": commit, "manifest_required_commit": expected}

    def _clean_checkout(self) -> Dict[str, Any]:
        result = _run(["git", "status", "--porcelain"])
        changes = [line for line in result.stdout.splitlines() if line.strip()]
        if changes:
            raise DiagnosticFailure("working tree is dirty", "Commit or remove unrelated changes before execution.", {"changes": changes})
        return {"changes": []}

    def _protocol_hash(self) -> Dict[str, Any]:
        actual = _sha256(self.args.config)
        if actual != EXPECTED_PROTOCOL_SHA256:
            raise DiagnosticFailure(f"protocol SHA-256 mismatch: {actual}", f"Restore the exact protocol; expected {EXPECTED_PROTOCOL_SHA256}.", {"actual": actual, "expected": EXPECTED_PROTOCOL_SHA256})
        protocol = _protocol(self.args)
        if protocol.get("schema_version") != "a100-final-validation.v1" or protocol.get("safety", {}).get("max_total_wall_clock_seconds") != EXPECTED_MAX_WALL_SECONDS:
            raise DiagnosticFailure("protocol schema or safety wall is not the sealed A100 contract", "Restore configs/a100_final_validation.json from the required commit.")
        return {"sha256": actual, "schema_version": protocol.get("schema_version")}

    def _manifest_binding(self) -> Dict[str, Any]:
        values = _manifest(self.args)
        actual = values.get("REQUIRED_COMMIT")
        protocol_hash = values.get("PROTOCOL_SHA256")
        if values.get("REQUIRED_BRANCH") != EXPECTED_BRANCH:
            raise DiagnosticFailure("manifest REQUIRED_BRANCH mismatch", f"Set REQUIRED_BRANCH={EXPECTED_BRANCH}.", {"actual": values.get("REQUIRED_BRANCH")})
        current = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
        if actual != current:
            raise DiagnosticFailure(
                "manifest REQUIRED_COMMIT does not match the current checkout",
                f"Set REQUIRED_COMMIT={current} only after pushing this exact clean checkout.",
                {"actual": actual, "current_commit": current},
            )
        if protocol_hash != EXPECTED_PROTOCOL_SHA256:
            raise DiagnosticFailure("manifest PROTOCOL_SHA256 mismatch", f"Set PROTOCOL_SHA256={EXPECTED_PROTOCOL_SHA256}.", {"actual": protocol_hash})
        if not _outside_checkout(Path(values.get("ARTIFACT_ROOT", ""))) or not _outside_checkout(Path(values.get("RECOVERY_ROOT", ""))):
            raise DiagnosticFailure("manifest artifact/recovery path enters the checkout or H100 artifact root", "Use distinct external paths outside Git and outside the frozen H100 artifact root.")
        return {"manifest": str(self.args.manifest), "required_commit": actual, "protocol_sha256": protocol_hash}

    def _nvidia_smi(self) -> Dict[str, Any]:
        if shutil.which("nvidia-smi") is None:
            raise DiagnosticFailure("nvidia-smi is unavailable", "Install the NVIDIA driver/toolkit and expose nvidia-smi on PATH.")
        result = _run(["nvidia-smi", "--version"])
        return {"version": (result.stdout + result.stderr).strip()}

    def _gpu_query(self) -> List[str]:
        result = _run(["nvidia-smi", "--query-gpu=name,memory.total,compute_cap", "--format=csv,noheader,nounits"])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _a100_identity(self) -> Dict[str, Any]:
        rows = self._gpu_query()
        if len(rows) != 1:
            raise DiagnosticFailure(f"expected one GPU, found {len(rows)}", "Attach exactly one A100 80GB GPU to this VM.", {"rows": rows})
        parts = [part.strip() for part in rows[0].split(",")]
        if len(parts) != 3:
            raise DiagnosticFailure(f"unexpected nvidia-smi GPU row: {rows[0]}", "Use a driver supporting name, memory.total, and compute_cap queries.")
        name, memory, compute = parts
        try:
            memory_mib = int(float(memory))
        except ValueError as exc:
            raise DiagnosticFailure(f"GPU memory is not numeric: {memory}", "Fix the NVIDIA driver query output before execution.") from exc
        if name not in {"NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-80GB"} or memory_mib < 80000 or compute != "8.0":
            raise DiagnosticFailure(
                f"GPU is not an allowlisted A100 80GB Ampere device: {name}, {memory_mib} MiB, compute {compute}",
                "Use exactly one NVIDIA A100 80GB with compute capability 8.0.",
                {"gpu_name": name, "memory_total_mib": memory_mib, "compute_capability": compute, "architecture": "Ampere" if "A100" in name else "unknown"},
            )
        return {"gpu_name": name, "memory_total_mib": memory_mib, "compute_capability": compute, "architecture": "Ampere"}

    def _gpu_isolation(self) -> Dict[str, Any]:
        result = _run(["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"])
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if pids:
            raise DiagnosticFailure(f"competing GPU processes detected: {', '.join(pids)}", "Stop unrelated GPU workloads and rerun until the process list is empty.", {"pids": pids})
        return {"pids": []}

    def _docker_command(self) -> Dict[str, Any]:
        path = shutil.which("docker")
        if path is None:
            raise DiagnosticFailure("docker is unavailable", "Install Docker and expose the docker client on PATH.")
        return {"path": path}

    def _docker_daemon(self) -> Dict[str, Any]:
        result = _run(["docker", "info", "--format", "{{json .Runtimes}}"])
        return {"runtime_output": (result.stdout + result.stderr).strip()}

    def _nvidia_runtime(self) -> Dict[str, Any]:
        result = _run(["docker", "info", "--format", "{{json .Runtimes}}"])
        output = (result.stdout + result.stderr).lower()
        if "nvidia" not in output:
            raise DiagnosticFailure("Docker NVIDIA runtime is not registered", "Install nvidia-container-toolkit and configure Docker's NVIDIA runtime.", {"runtime_output": output})
        return {"nvidia_runtime_registered": True}

    def _pinned_image(self) -> Dict[str, Any]:
        values = _manifest(self.args)
        image = _require_manifest_value(values, "VLLM_IMAGE")
        if image != EXPECTED_IMAGE or "@sha256:" not in image:
            raise DiagnosticFailure(f"vLLM image pin mismatch: {image}", f"Set VLLM_IMAGE to the exact digest-pinned image {EXPECTED_IMAGE}.", {"actual": image})
        platform = values.get("VLLM_IMAGE_PLATFORM", "")
        if platform != "linux/amd64":
            raise DiagnosticFailure(f"vLLM image platform mismatch: {platform}", "Set VLLM_IMAGE_PLATFORM=linux/amd64.", {"actual": platform})
        inspect = _run(["docker", "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}\n{{join .RepoDigests \"\\n\"}}"])
        lines = [line.strip() for line in inspect.stdout.splitlines() if line.strip()]
        if not lines or lines[0] != platform or EXPECTED_IMAGE.split("@", 1)[1] not in "\n".join(lines[1:]):
            raise DiagnosticFailure("pinned vLLM image digest or platform is not available locally", "Make the exact digest-pinned linux/amd64 image available locally; do not pull a mutable tag.", {"inspect": lines, "expected_platform": platform})
        return {"image": image, "platform": lines[0], "digest": EXPECTED_IMAGE.split("@", 1)[1]}

    def _model_paths(self) -> Dict[str, Any]:
        values = _manifest(self.args)
        cache = Path(_require_manifest_value(values, "MODEL_CACHE")).expanduser()
        snapshot = Path(_require_manifest_value(values, "MODEL_SNAPSHOT")).expanduser()
        revision = _require_manifest_value(values, "VLLM_MODEL_REVISION")
        if values.get("VLLM_MODEL") != EXPECTED_MODEL or revision != EXPECTED_REVISION:
            raise DiagnosticFailure("model or revision pin differs from the sealed Qwen model", f"Set VLLM_MODEL={EXPECTED_MODEL} and VLLM_MODEL_REVISION={EXPECTED_REVISION}.")
        if not cache.is_dir() or not snapshot.is_dir():
            raise DiagnosticFailure(f"model cache or snapshot is missing: cache={cache}, snapshot={snapshot}", "Populate the exact pinned model cache offline before execution; the diagnostic does not download it.")
        try:
            snapshot.resolve().relative_to(cache.resolve())
        except ValueError as exc:
            raise DiagnosticFailure("model snapshot is outside MODEL_CACHE", "Place the exact revision snapshot beneath MODEL_CACHE.") from exc
        if snapshot.name != revision:
            raise DiagnosticFailure(f"snapshot basename is not the pinned revision: {snapshot.name}", f"Use snapshot directory {revision}.")
        return {"model": values.get("VLLM_MODEL"), "revision": revision, "cache": str(cache), "snapshot": str(snapshot)}

    def _model_cache(self) -> Dict[str, Any]:
        paths = self._model_paths()
        snapshot = Path(paths["snapshot"])
        index_paths = [snapshot / "model.safetensors.index.json", snapshot / "pytorch_model.bin.index.json"]
        weight_paths = sorted(snapshot.glob("*.safetensors"))
        existing_indexes = [path for path in index_paths if path.is_file()]
        if not existing_indexes and not weight_paths:
            raise DiagnosticFailure(
                "pinned model snapshot has no safetensors weights or weight index",
                "Restore the exact model.safetensors shards or model.safetensors.index.json in the pinned snapshot; do not download or substitute weights during validation.",
                {"snapshot": str(snapshot), "expected_indexes": [path.name for path in index_paths]},
            )
        indexed_weights: List[str] = []
        if existing_indexes:
            index = existing_indexes[0]
            try:
                index_data = json.loads(index.read_text(encoding="utf-8"))
                weight_map = index_data.get("weight_map", {})
                indexed_weights = sorted(set(weight_map.values())) if isinstance(weight_map, dict) else []
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                raise DiagnosticFailure(
                    f"model weight index is unreadable: {index}",
                    "Restore a valid JSON weight index for the pinned model snapshot.",
                ) from exc
            missing = [name for name in indexed_weights if not (snapshot / name).is_file()]
            if not indexed_weights or missing:
                raise DiagnosticFailure(
                    f"model weight index does not resolve to local shards: {', '.join(missing) or 'empty weight_map'}",
                    "Restore every weight shard referenced by the pinned index; do not continue with partial model weights.",
                    {"index": str(index), "missing_shards": missing},
                )
        return {
            **paths,
            "weight_indexes": [path.name for path in existing_indexes],
            "weight_files": [path.name for path in weight_paths],
            "indexed_weight_files": indexed_weights,
        }

    def _tokenizer_cache(self) -> Dict[str, Any]:
        paths = self._model_paths()
        snapshot = Path(paths["snapshot"])
        required = [snapshot / "config.json"]
        tokenizer_candidates = [snapshot / "tokenizer.json", snapshot / "tokenizer.model", snapshot / "tokenizer_config.json"]
        missing = [str(path.name) for path in required if not path.is_file()]
        if not any(path.is_file() for path in tokenizer_candidates):
            missing.append("tokenizer.json|tokenizer.model|tokenizer_config.json")
        if missing:
            raise DiagnosticFailure(f"model/tokenizer cache files are missing: {', '.join(missing)}", "Restore the pinned snapshot's config and tokenizer files; do not use a network download during validation.", {"snapshot": str(snapshot), "missing": missing})
        return {"snapshot": str(snapshot), "config": "config.json", "tokenizer": [path.name for path in tokenizer_candidates if path.is_file()]}

    def _discover_nsys(self) -> Path:
        candidates = self._nsys_candidates()
        if not candidates:
            raise DiagnosticFailure("Nsight Systems executable was not discovered", "Install Nsight Systems 2025.1.3 and expose nsys on PATH or /usr/local/cuda/bin/nsys.")
        return candidates[0]

    def _nsys_candidates(self) -> List[Path]:
        candidates: List[Path] = []
        path_candidate = shutil.which("nsys")
        if path_candidate:
            candidates.append(Path(path_candidate))
        for candidate in (Path(EXPECTED_NSYS_HOST_PATH),):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                candidates.append(candidate)
        opt_root = Path("/opt/nvidia")
        if opt_root.is_dir():
            try:
                candidates.extend(path for path in opt_root.rglob("nsys") if path.is_file() and os.access(path, os.X_OK))
            except OSError:
                pass
        unique: List[Path] = []
        seen = set()
        for candidate in candidates:
            resolved = str(candidate.resolve())
            if resolved not in seen:
                seen.add(resolved)
                unique.append(Path(resolved))
        return unique

    def _nsight_discovery(self) -> Dict[str, Any]:
        candidates = self._nsys_candidates()
        if not candidates:
            raise DiagnosticFailure("Nsight Systems executable was not discovered", "Install Nsight Systems 2025.1.3 and expose nsys on PATH, /usr/local/cuda/bin/nsys, or under /opt/nvidia.")
        reports = []
        for candidate in candidates:
            try:
                result = _run([str(candidate), "--version"])
                version = (result.stdout + result.stderr).strip()
                reports.append({"path": str(candidate), "version": version, "required_version": EXPECTED_NSYS_VERSION_PREFIX in version})
            except DiagnosticFailure as exc:
                reports.append({"path": str(candidate), "version_error": exc.detail, "required_version": False})
        if not any(report.get("required_version") for report in reports):
            raise DiagnosticFailure(
                f"discovered Nsight candidates do not report required version {EXPECTED_NSYS_VERSION_PREFIX}",
                f"Install Nsight Systems {EXPECTED_NSYS_VERSION_PREFIX}; candidates under /opt/nvidia do not satisfy the required production mapping by themselves.",
                {"candidates": reports},
            )
        return {"candidates": reports}

    def _nsight_host_mapping(self) -> Dict[str, Any]:
        required = Path(EXPECTED_NSYS_HOST_PATH)
        if not required.is_file() or not os.access(required, os.X_OK):
            raise DiagnosticFailure(
                f"required host Nsight mapping is missing or not executable: {required}",
                f"Map the approved Nsight Systems installation to {required}; an /opt/nvidia candidate is not a substitute for the production bind mount.",
                {"required_host_path": str(required), "candidates": [str(path) for path in self._nsys_candidates()]},
            )
        result = _run([str(required), "--version"])
        version = (result.stdout + result.stderr).strip()
        if EXPECTED_NSYS_VERSION_PREFIX not in version:
            raise DiagnosticFailure(
                f"required host Nsight mapping has wrong version: {version}",
                f"Install Nsight Systems {EXPECTED_NSYS_VERSION_PREFIX} at {required}; do not substitute an /opt/nvidia candidate.",
                {"required_host_path": str(required), "version": version},
            )
        return {"required_host_path": str(required), "version": version}

    def _nsight_required_path(self) -> Dict[str, Any]:
        values = _manifest(self.args)
        actual = _require_manifest_value(values, "A100_NSYS_BIN")
        if actual != EXPECTED_NSYS_CONTAINER_PATH:
            raise DiagnosticFailure(f"required Nsight container path mismatch: {actual}", f"Set A100_NSYS_BIN={EXPECTED_NSYS_CONTAINER_PATH}.", {"actual": actual, "expected": EXPECTED_NSYS_CONTAINER_PATH})
        return {"manifest_path": actual, "expected_container_path": EXPECTED_NSYS_CONTAINER_PATH}

    def _external_values(self) -> Dict[str, Path]:
        values = _manifest(self.args)
        keys = ("WORK_ROOT", "PYTHON_ENV_ROOT", "MODEL_CACHE", "MODEL_SNAPSHOT", "TRACE_ROOT", "ARTIFACT_ROOT", "RECOVERY_ROOT")
        paths: Dict[str, Path] = {}
        for key in keys:
            raw = _require_manifest_value(values, key)
            path = Path(raw).expanduser()
            if not _outside_checkout(path):
                raise DiagnosticFailure(f"{key} enters the checkout or H100 artifact root: {path}", "Use an external path outside Git and outside artifacts/h100_final_validation.")
            paths[key] = path
        if paths["ARTIFACT_ROOT"].resolve() == paths["RECOVERY_ROOT"].resolve():
            raise DiagnosticFailure("ARTIFACT_ROOT and RECOVERY_ROOT are identical", "Use distinct external artifact and recovery roots.")
        return paths

    def _external_roots(self) -> Dict[str, Any]:
        paths = self._external_values()
        evidence: Dict[str, Any] = {}
        for key in ("WORK_ROOT", "ARTIFACT_ROOT", "RECOVERY_ROOT", "TRACE_ROOT"):
            evidence[key] = _writable_without_creation(paths[key])
        return evidence

    def _artifact_collision(self) -> Dict[str, Any]:
        paths = self._external_values()
        artifact = paths["ARTIFACT_ROOT"]
        if artifact.exists():
            if not artifact.is_dir():
                raise DiagnosticFailure(f"ARTIFACT_ROOT is not a directory: {artifact}", "Move the conflicting path aside and provide a fresh external artifact directory.")
            try:
                entries = [entry.name for entry in os.scandir(artifact)]
            except OSError as exc:
                raise DiagnosticFailure(f"cannot inspect ARTIFACT_ROOT: {artifact}", "Grant read access to the external artifact root or provide a fresh one.") from exc
            if entries:
                raise DiagnosticFailure(f"artifact collision: ARTIFACT_ROOT is not empty ({len(entries)} entries)", "Preserve the existing run and choose a fresh empty canonical artifact root; never overwrite it.", {"entries": entries[:20], "entry_count": len(entries)})
        return {"artifact_root": str(artifact), "exists": artifact.exists(), "empty": True}

    def _disk_space(self) -> Dict[str, Any]:
        paths = self._external_values()
        target = _existing_parent(paths["WORK_ROOT"])
        if not target.exists():
            raise DiagnosticFailure(f"no existing filesystem for WORK_ROOT: {paths['WORK_ROOT']}", "Mount the external A100 work filesystem before execution.")
        usage = shutil.disk_usage(target)
        if usage.free < self.args.min_free_bytes:
            raise DiagnosticFailure(f"insufficient free disk space: {usage.free} bytes", f"Free at least {self.args.min_free_bytes} bytes on {target}.", {"path": str(target), "free_bytes": usage.free, "required_bytes": self.args.min_free_bytes})
        return {"path": str(target), "free_bytes": usage.free, "required_bytes": self.args.min_free_bytes}

    def _deadline(self) -> Dict[str, Any]:
        protocol = _protocol(self.args)
        configured = protocol.get("safety", {}).get("max_total_wall_clock_seconds")
        if configured != EXPECTED_MAX_WALL_SECONDS:
            raise DiagnosticFailure(f"protocol deadline is {configured}, not {EXPECTED_MAX_WALL_SECONDS}", "Restore the sealed 14,400-second production deadline.", {"configured_seconds": configured, "required_seconds": EXPECTED_MAX_WALL_SECONDS})
        if self.args.deadline_epoch is not None:
            remaining = self.args.deadline_epoch - time.time()
            if remaining <= 0:
                raise DiagnosticFailure("supplied deadline has expired", "Do not start or continue A100 execution after the deadline; establish a fresh authorized window.", {"deadline_epoch": self.args.deadline_epoch, "remaining_seconds": remaining})
            return {"configured_seconds": configured, "deadline_epoch": self.args.deadline_epoch, "remaining_seconds": remaining}
        return {"configured_seconds": configured, "live_deadline": "not started by diagnostic"}


def _report(args: argparse.Namespace, results: List[CheckResult]) -> Dict[str, Any]:
    failures = [result for result in results if result.status == "fail"]
    return {
        "schema_version": "a100-diagnostic.v1",
        "mode": "diagnostic_only",
        "generated_by": str(Path(__file__).resolve()),
        "config": str(args.config),
        "manifest": str(args.manifest) if args.manifest else None,
        "canonical_artifacts_created": False,
        "measurements_started": False,
        "holdout_accessed": False,
        "production_services_started": False,
        "status": "PASS" if not failures else "NOT_READY",
        "summary": {"checks": len(results), "passed": len(results) - len(failures), "failed": len(failures)},
        "checks": [result.as_dict() for result in results],
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    resolved = Path(os.path.abspath(str(path)))
    root = Path(os.path.abspath(str(ROOT)))
    if resolved == root or root in resolved.parents:
        raise ValueError("JSON report must be outside the Git checkout")
    manifest_path = report.get("manifest")
    if manifest_path:
        try:
            values = _read_manifest(Path(str(manifest_path)))
        except DiagnosticFailure:
            values = {}
        for key in ("ARTIFACT_ROOT", "RECOVERY_ROOT", "TRACE_ROOT"):
            raw_root = values.get(key)
            if raw_root:
                runtime_root = Path(os.path.abspath(os.path.expanduser(raw_root)))
                if resolved == runtime_root or runtime_root in resolved.parents:
                    raise ValueError(f"JSON report must not be written inside {key}: {runtime_root}")
    if not resolved.parent.is_dir() or not os.access(resolved.parent, os.W_OK):
        raise ValueError(f"JSON report parent must already exist and be writable: {resolved.parent}")
    resolved.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest", type=Path, default=None, help=f"external manifest (default is not assumed; example target: {DEFAULT_MANIFEST})")
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT, help="non-canonical path for the machine-readable report")
    parser.add_argument("--deadline-epoch", type=float, default=None, help="optional existing deadline to validate; diagnostic never creates one")
    parser.add_argument("--min-free-bytes", type=int, default=DEFAULT_MIN_FREE_BYTES)
    args = parser.parse_args(argv)
    if args.min_free_bytes < 0:
        parser.error("--min-free-bytes must be non-negative")

    diagnostic = A100Diagnostic(args)
    results = diagnostic.run()
    report = _report(args, results)
    try:
        _write_report(args.json_out, report)
    except (OSError, ValueError) as exc:
        print(f"NOT_READY: could not write JSON report: {exc}", file=sys.stderr)
        return 2

    print("A100 DIAGNOSTIC (read-only; no vLLM, tracing, measurements, holdouts, or canonical artifacts)")
    for result in results:
        marker = "PASS" if result.status == "pass" else "FAIL"
        print(f"[{marker}] {result.category}/{result.check_id}: {result.detail}")
        if result.status == "fail":
            print(f"       remediation: {result.remediation}")
    print(f"JSON report: {args.json_out}")
    print(f"Summary: {report['summary']['passed']} passed, {report['summary']['failed']} failed")
    print("READY_FOR_A100_EXECUTION" if report["status"] == "PASS" else "NOT_READY_FOR_A100_EXECUTION")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
