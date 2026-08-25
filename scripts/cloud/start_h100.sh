#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# Canonical, fail-closed H100 VM startup.  This script starts or reuses only
# the reviewed profiled server.  It never invokes the calibration driver,
# opens holdout artifacts, changes pins, or deletes artifacts.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
EXPECTED_BRANCH='parallel-h100-shards'
CONFIG="$ROOT/configs/h100_final_validation.json"
PYTHON_LOCK="$ROOT/cloud/lambda/requirements-linux-x86_64.txt"
DIRECT_REQUIREMENTS="$ROOT/cloud/gcp/h100_direct_requirements.txt"
SYSTEM_LOCK="$ROOT/cloud/gcp/h100_system_packages_ubuntu22.04-amd64.lock"
LAUNCHER="$ROOT/scripts/cloud/start_h100_vllm_nsight.sh"
TRACE_PROVIDER="$ROOT/scripts/cloud/h100_nsight_trace_provider.py"
MANIFEST="${H100_STARTUP_MANIFEST:-$ROOT/../h100-startup.env}"
MANIFEST_EXPLICIT=0
[[ -n "${H100_STARTUP_MANIFEST:-}" ]] && MANIFEST_EXPLICIT=1
STATE_ROOT=""
BACKEND="${BACKEND:-auto}"
DRY_RUN=0
SETUP=0
DRY_RUN_DEFAULT_MANIFEST=0

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'USAGE'
Usage: scripts/cloud/start_h100.sh --manifest FILE [--backend auto|docker|direct] [--setup] [--dry-run]

The manifest is an external, non-secret pin/path file.  It is never sourced.
This entrypoint starts or reuses the pinned profiled vLLM server only; it does
not start calibration, holdout, fitting, scoring, or cloud allocation.

--setup is direct-only. It installs the pinned Python/vLLM closure and verifies
the pinned model/tokenizer snapshot, then exits without starting a server.
USAGE
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 -- "$1" | awk '{print $1}'
  else
    die 'sha256sum or shasum is required'
  fi
}

while (($#)); do
  case "$1" in
    --backend)
      (($# >= 2)) || die '--backend requires auto, docker, or direct'
      BACKEND="$2"
      shift 2
      ;;
    --backend=*) BACKEND="${1#*=}"; shift ;;
    --manifest)
      (($# >= 2)) || die '--manifest requires a file'
      MANIFEST="$2"
      MANIFEST_EXPLICIT=1
      shift 2
      ;;
    --manifest=*) MANIFEST="${1#*=}"; MANIFEST_EXPLICIT=1; shift ;;
    --setup) SETUP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$BACKEND" == auto || "$BACKEND" == docker || "$BACKEND" == direct ]] || die 'backend must be auto, docker, or direct'
if (( SETUP )) && [[ "$BACKEND" != direct ]]; then
  die '--setup requires --backend direct; no backend fallback is attempted'
fi

if (( DRY_RUN && MANIFEST_EXPLICIT == 0 )); then
  MANIFEST="$ROOT/cloud/gcp/h100_startup_manifest.env.example"
  DRY_RUN_DEFAULT_MANIFEST=1
fi

[[ -f "$MANIFEST" ]] || die "startup manifest is missing: $MANIFEST"
MANIFEST="$(cd -- "$(dirname -- "$MANIFEST")" && pwd -P)/$(basename -- "$MANIFEST")"
if (( ! DRY_RUN )); then
  case "$MANIFEST" in
    "$ROOT"/*) die 'startup manifest must be outside the checkout so git remains clean' ;;
  esac
fi

# Read only an explicit allowlist.  In particular, do not source the file:
# API keys, tokens, and arbitrary shell text are never evaluated or logged.
manifest_value() {
  local wanted="$1" line key value found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" == "$wanted" ]] || continue
    found=$((found + 1))
    ((found == 1)) || die "duplicate startup manifest key: $wanted"
    printf '%s' "$value"
  done < "$MANIFEST"
}

required_manifest_value() {
  local key="$1" value
  value="$(manifest_value "$key")"
  [[ -n "$value" ]] || die "startup manifest key is missing: $key"
  printf '%s' "$value"
}

REQUIRED_BRANCH="$(required_manifest_value REQUIRED_BRANCH)"
REQUIRED_COMMIT="$(required_manifest_value REQUIRED_COMMIT)"
PROTOCOL_SHA256="$(required_manifest_value PROTOCOL_SHA256)"
PYTHON_LOCK_SHA256="$(required_manifest_value PYTHON_LOCK_SHA256)"
SYSTEM_LOCK_SHA256="$(required_manifest_value SYSTEM_LOCK_SHA256)"
WORK_ROOT="$(required_manifest_value WORK_ROOT)"
PYTHON_ENV_ROOT="$(required_manifest_value PYTHON_ENV_ROOT)"
PYTHON_BOOTSTRAP_BIN="$(required_manifest_value PYTHON_BOOTSTRAP_BIN)"
MODEL_CACHE="$(required_manifest_value MODEL_CACHE)"
MODEL_SNAPSHOT="$(required_manifest_value MODEL_SNAPSHOT)"
TRACE_ROOT="$(required_manifest_value TRACE_ROOT)"
MANIFEST_MODEL="$(required_manifest_value VLLM_MODEL)"
MANIFEST_REVISION="$(required_manifest_value VLLM_MODEL_REVISION)"
MANIFEST_IMAGE="$(required_manifest_value VLLM_IMAGE)"
MANIFEST_PLATFORM="$(required_manifest_value VLLM_IMAGE_PLATFORM)"
MANIFEST_PARSER="$(required_manifest_value VLLM_TOOL_PARSER)"
MANIFEST_MAX_MODEL_LEN="$(required_manifest_value VLLM_MAX_MODEL_LEN)"
MANIFEST_TP="$(required_manifest_value VLLM_TENSOR_PARALLEL_SIZE)"
PORT="$(required_manifest_value VLLM_PORT)"
GPU_MEM_UTIL="$(required_manifest_value VLLM_GPU_MEMORY_UTILIZATION)"
CONTAINER="$(required_manifest_value H100_CONTAINER)"
SESSION="$(required_manifest_value H100_NSYS_SESSION)"
CUDA_VERSION="$(required_manifest_value CUDA_VERSION)"
NSYS_VERSION_PREFIX="$(required_manifest_value NSYS_VERSION_PREFIX)"
MANIFEST_DIRECT_REQUIREMENTS_SHA256="$(manifest_value DIRECT_REQUIREMENTS_SHA256)"
STATE_ROOT="${H100_STARTUP_STATE_ROOT:-$WORK_ROOT/state/h100-startup}"

if (( DRY_RUN )) && [[ "$REQUIRED_COMMIT" == '<40-hex-pushed-commit>' ]]; then
  REQUIRED_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
fi

reject_checkout_path() {
  local label="$1" path="$2" resolved
  [[ "$path" == /* ]] || die "$label must be an absolute path"
  if resolved="$(readlink -m -- "$path" 2>/dev/null)"; then
    :
  else
    command -v python3 >/dev/null 2>&1 || die 'python3 is required to resolve startup paths'
    resolved="$(python3 -c 'import os, sys; print(os.path.abspath(os.path.normpath(sys.argv[1])))' "$path")"
  fi
  case "$resolved/" in
    "$ROOT/"*|"$ROOT") die "$label must be outside the checkout and its artifacts" ;;
  esac
}
reject_checkout_path WORK_ROOT "$WORK_ROOT"
reject_checkout_path PYTHON_ENV_ROOT "$PYTHON_ENV_ROOT"
reject_checkout_path MODEL_CACHE "$MODEL_CACHE"
reject_checkout_path MODEL_SNAPSHOT "$MODEL_SNAPSHOT"
reject_checkout_path TRACE_ROOT "$TRACE_ROOT"
reject_checkout_path STATE_ROOT "$STATE_ROOT"

[[ "$REQUIRED_BRANCH" == "$EXPECTED_BRANCH" ]] || die "required branch is not $EXPECTED_BRANCH"
[[ "$REQUIRED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || die 'REQUIRED_COMMIT must be a 40-hex commit'
[[ "$PROTOCOL_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || die 'PROTOCOL_SHA256 must be a 64-hex digest'
[[ "$PYTHON_LOCK_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || die 'PYTHON_LOCK_SHA256 must be a 64-hex digest'
[[ "$SYSTEM_LOCK_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || die 'SYSTEM_LOCK_SHA256 must be a 64-hex digest'
[[ "$MANIFEST_MODEL" == 'Qwen/Qwen3-Coder-30B-A3B-Instruct' ]] || die 'manifest model pin changed'
[[ "$MANIFEST_REVISION" == 'b2cff646eb4bb1d68355c01b18ae02e7cf42d120' ]] || die 'manifest model revision changed'
[[ "$MANIFEST_IMAGE" == 'vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271' ]] || die 'manifest vLLM image pin changed'
[[ "$MANIFEST_PLATFORM" == 'linux/amd64' ]] || die 'manifest vLLM platform changed'
[[ "$MANIFEST_PARSER" == 'qwen3_coder' ]] || die 'manifest tool parser pin changed'
[[ "$MANIFEST_MAX_MODEL_LEN" == '32768' && "$MANIFEST_TP" == '1' ]] || die 'manifest context or tensor-parallel pin changed'
[[ "$PORT" == '8000' && "$CONTAINER" == 'h100-final-vllm' && "$SESSION" == 'h100-final-validation' ]] || die 'manifest server identity changed'
[[ "$GPU_MEM_UTIL" == '0.90' ]] || die 'manifest GPU memory pin changed'
[[ "$MODEL_SNAPSHOT" == */b2cff646eb4bb1d68355c01b18ae02e7cf42d120 ]] || die 'model snapshot path is not the pinned revision'

[[ -f "$CONFIG" ]] || die "sealed config is missing: $CONFIG"
[[ -f "$PYTHON_LOCK" ]] || die "Python lock is missing: $PYTHON_LOCK"
[[ -f "$DIRECT_REQUIREMENTS" ]] || die "direct runtime lock is missing: $DIRECT_REQUIREMENTS"
[[ -f "$SYSTEM_LOCK" ]] || die "system lock is missing: $SYSTEM_LOCK"
[[ -x "$LAUNCHER" ]] || die "reviewed vLLM launcher is missing or not executable: $LAUNCHER"
[[ -x "$TRACE_PROVIDER" ]] || die "real production trace provider is missing or not executable: $TRACE_PROVIDER"
[[ "$TRACE_PROVIDER" != *h100_fake_trace_provider.py ]] || die 'test trace fixture cannot be used for production startup'
[[ "$TRACE_PROVIDER" == "$ROOT/scripts/cloud/h100_nsight_trace_provider.py" ]] || die 'trace provider must be the canonical production provider'

ACTUAL_PYTHON_LOCK_SHA256="$(sha256_file "$PYTHON_LOCK")"
[[ "$ACTUAL_PYTHON_LOCK_SHA256" == "$PYTHON_LOCK_SHA256" ]] || die 'Python lock hash mismatch'
DIRECT_REQUIREMENTS_SHA256="$(sha256_file "$DIRECT_REQUIREMENTS")"
if [[ -n "$MANIFEST_DIRECT_REQUIREMENTS_SHA256" && "$MANIFEST_DIRECT_REQUIREMENTS_SHA256" != "$DIRECT_REQUIREMENTS_SHA256" ]]; then
  die 'direct runtime lock hash mismatch'
fi
ACTUAL_SYSTEM_LOCK_SHA256="$(sha256_file "$SYSTEM_LOCK")"
[[ "$ACTUAL_SYSTEM_LOCK_SHA256" == "$SYSTEM_LOCK_SHA256" ]] || die 'system package lock hash mismatch'
CONFIG_ACTUAL_SHA256="$(sha256_file "$CONFIG")"
[[ "$CONFIG_ACTUAL_SHA256" == "$PROTOCOL_SHA256" ]] || die 'sealed config hash mismatch'

if (( ! DRY_RUN_DEFAULT_MANIFEST && ! SETUP )); then
  case "$MODEL_SNAPSHOT/" in
    "$MODEL_CACHE"/*) ;;
    *) die 'model snapshot must be inside MODEL_CACHE' ;;
  esac
  [[ -d "$MODEL_CACHE" ]] || die "model cache is missing: $MODEL_CACHE"
  [[ -d "$MODEL_SNAPSHOT" ]] || die "exact model snapshot is missing: $MODEL_SNAPSHOT"
  [[ "$(basename -- "$MODEL_SNAPSHOT")" == "$MANIFEST_REVISION" ]] || die 'model snapshot directory is not the pinned revision'
  [[ -s "$MODEL_SNAPSHOT/config.json" && -s "$MODEL_SNAPSHOT/tokenizer.json" ]] || die 'model snapshot metadata is incomplete'
  MODEL_WEIGHTS_FOUND=0
  for MODEL_WEIGHT in "$MODEL_SNAPSHOT"/*.safetensors "$MODEL_SNAPSHOT"/*.safetensors.index.json; do
    if [[ -f "$MODEL_WEIGHT" ]]; then MODEL_WEIGHTS_FOUND=1; break; fi
  done
  ((MODEL_WEIGHTS_FOUND == 1)) || die 'model snapshot has no safetensors weights/index'
fi

if (( DRY_RUN )); then
  command -v git >/dev/null 2>&1 || die 'git is required for dry-run verification'
  command -v python3 >/dev/null 2>&1 || die 'python3 is required for dry-run verification'
  current_branch="$(git -C "$ROOT" branch --show-current)"
  [[ -z "$current_branch" || "$current_branch" == "$EXPECTED_BRANCH" ]] || die "checkout is not on $EXPECTED_BRANCH"
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$REQUIRED_COMMIT" ]] || die 'checkout commit does not match REQUIRED_COMMIT'
  [[ -z "$(git -C "$ROOT" status --porcelain)" ]] || die 'checkout is dirty; refusing startup'
  python3 - "$CONFIG" <<'PY'
import json
import os
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
software = d.get("frozen_software", {})
request = d.get("request_protocol", {})
if d.get("schema_version") != "h100-final-validation.v1": raise SystemExit("protocol schema mismatch")
if d.get("launch_authorized") is not False: raise SystemExit("sealed protocol launch guard changed")
if d.get("status") != "sealed_scaffold_not_launched": raise SystemExit("protocol status changed")
if software.get("model_revision") != "b2cff646eb4bb1d68355c01b18ae02e7cf42d120": raise SystemExit("model revision mismatch")
if software.get("vllm_image") != "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271": raise SystemExit("image mismatch")
if software.get("vllm_tool_parser") != "qwen3_coder" or software.get("context_tokens") != 32768: raise SystemExit("vLLM parser/context mismatch")
if request.get("concurrency") != 1 or request.get("warmup_requests") != 2 or request.get("measured_repetitions_per_case") != 3: raise SystemExit("request protocol mismatch")
PY
  printf 'DRY-RUN: checkout branch=%s commit=%s clean; sealed config=%s; manifest=%s\n' "${current_branch:-detached}" "$REQUIRED_COMMIT" "$CONFIG_ACTUAL_SHA256" "$MANIFEST"
  printf 'DRY-RUN: pinned system lock=%s; Python lock=%s; no installs, Docker, GPU, server, or artifact access\n' "$SYSTEM_LOCK_SHA256" "$PYTHON_LOCK_SHA256"
  printf 'DRY-RUN: requested BACKEND=%s; auto probes Docker/NVIDIA/GPU and otherwise selects direct; explicit docker never falls back\n' "$BACKEND"
  printf 'DRY-RUN: would verify the selected backend with the same pinned model/revision/parser/request limits and trace provider; Docker container=%s\n' "$CONTAINER"
  exit 0
fi

verify_checkout_identity() {
  command -v git >/dev/null 2>&1 || die 'git is required'
  [[ "$(git -C "$ROOT" branch --show-current)" == "$EXPECTED_BRANCH" ]] || die "checkout is not on $EXPECTED_BRANCH"
  [[ "$(git -C "$ROOT" rev-parse HEAD)" == "$REQUIRED_COMMIT" ]] || die 'checkout commit does not match REQUIRED_COMMIT'
  if (( $# == 0 )); then
    [[ -z "$(git -C "$ROOT" status --porcelain)" ]] || die 'checkout is dirty; refusing startup'
  fi
}

locked_system_package_version() {
  local package="$1"
  awk -F= -v wanted="$package" '$1 == wanted { print $2; exit }' "$SYSTEM_LOCK"
}

direct_apt() {
  if (( EUID == 0 )); then
    env DEBIAN_FRONTEND=noninteractive apt-get "$@"
  else
    command -v sudo >/dev/null 2>&1 || die 'sudo is required to install the pinned direct Python bootstrap'
    sudo -n true >/dev/null 2>&1 || die 'non-interactive sudo is required to install the pinned direct Python bootstrap'
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "$@"
  fi
}

ensure_direct_python() {
  local bootstrap_bin python_version venv_version
  bootstrap_bin="$(command -v "$PYTHON_BOOTSTRAP_BIN" 2>/dev/null || true)"
  python_version="$(locked_system_package_version python3.11)"
  venv_version="$(locked_system_package_version python3.11-venv)"
  [[ -n "$python_version" && -n "$venv_version" ]] || die 'direct Python pins are missing from the reviewed system lock'
  local package
  for package in python3.11 python3.11-venv; do
    local version
    version="$(locked_system_package_version "$package")"
    if [[ "$(dpkg-query -W -f='${Status} ${Version}' "$package" 2>/dev/null || true)" != *" ok installed $version" ]]; then
      printf 'Installing pinned direct Python package: %s=%s\n' "$package" "$version"
      direct_apt update
      direct_apt --allow-downgrades install -y --no-install-recommends \
        "python3.11=$python_version" "python3.11-venv=$venv_version"
      break
    fi
  done
  bootstrap_bin="$(command -v "$PYTHON_BOOTSTRAP_BIN" 2>/dev/null || true)"
  [[ -x "$bootstrap_bin" ]] || die "direct Python bootstrap binary is unavailable: $PYTHON_BOOTSTRAP_BIN"
  [[ "$("$bootstrap_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == '3.11' ]] \
    || die 'direct Python bootstrap must be Python 3.11'
  DIRECT_BOOTSTRAP_BIN="$bootstrap_bin"
}

check_direct_python_lock() {
  local python_bin="$1" lock_file="$2"
  "$python_bin" - "$lock_file" <<'PY'
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

lock = Path(sys.argv[1]).read_text(encoding="utf-8")
for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ \t\\]+)", lock, re.MULTILINE):
    name = re.sub(r"[-_.]+", "-", match.group(1).lower())
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise SystemExit(f"missing locked package: {name}") from exc
    if actual != match.group(2):
        raise SystemExit(f"locked package mismatch: {name} expected {match.group(2)} got {actual}")
PY
}

setup_direct_runtime() {
  [[ "$BACKEND" == direct ]] || die 'direct setup requires BACKEND=direct'
  # Setup writes only external runtime roots; launch still requires a clean checkout.
  verify_checkout_identity allow-dirty
  command -v python3 >/dev/null 2>&1 || die 'python3 is required to verify the checkout'
  command -v sha256sum >/dev/null 2>&1 || die 'sha256sum is required for direct setup'
  local direct_pip_cache="$WORK_ROOT/cache/pip"
  local pip_cache_from_environment
  pip_cache_from_environment="$(printenv PIP_CACHE_DIR 2>/dev/null || true)"
  [[ -n "$pip_cache_from_environment" ]] && direct_pip_cache="$pip_cache_from_environment"
  mkdir -p -- "$WORK_ROOT" "$MODEL_CACHE" "$TRACE_ROOT" "$STATE_ROOT" "$direct_pip_cache"

  local bootstrap_bin direct_python
  ensure_direct_python
  bootstrap_bin="$DIRECT_BOOTSTRAP_BIN"
  direct_python="$PYTHON_ENV_ROOT/bin/python"
  if [[ -e "$PYTHON_ENV_ROOT" && ! -x "$direct_python" ]]; then
    die "direct Python environment exists but is not a usable venv: $PYTHON_ENV_ROOT"
  fi
  if [[ -x "$direct_python" ]]; then
    [[ "$("$direct_python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == '3.11' ]] \
      || die "direct Python environment is not Python 3.11: $direct_python"
  else
    mkdir -p -- "$(dirname -- "$PYTHON_ENV_ROOT")"
    "$bootstrap_bin" -m venv "$PYTHON_ENV_ROOT"
  fi
  [[ -x "$direct_python" ]] || die "direct Python environment was not created: $direct_python"

  PIP_CACHE_DIR="$direct_pip_cache" "$direct_python" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: --upgrade \
    'pip==24.3.1' 'setuptools==75.6.0' 'wheel==0.45.1'
  PIP_CACHE_DIR="$direct_pip_cache" "$direct_python" -m pip install \
    --disable-pip-version-check --no-input --only-binary=:all: --require-hashes --upgrade \
    -r "$DIRECT_REQUIREMENTS"
  check_direct_python_lock "$direct_python" "$DIRECT_REQUIREMENTS" || die 'direct Python environment does not match the pinned vLLM closure'
  "$direct_python" -m pip check >/dev/null 2>&1 || die 'direct Python environment failed pip check'
  "$direct_python" -c 'import importlib.metadata; expected="0.10.0"; actual=importlib.metadata.version("vllm"); raise SystemExit(0 if actual == expected else f"vLLM version mismatch: {actual}")' \
    || die 'direct setup did not install pinned vLLM 0.10.0'

  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$direct_python" "$ROOT/scripts/cloud/h100_direct_setup.py" \
    --model "$MANIFEST_MODEL" \
    --revision "$MANIFEST_REVISION" \
    --model-cache "$MODEL_CACHE" \
    --model-snapshot "$MODEL_SNAPSHOT" \
    --state-output "$STATE_ROOT/direct_model.json" \
    || die 'direct setup could not verify or download the pinned model/tokenizer snapshot'

  "$direct_python" - "$STATE_ROOT/direct_setup.json" "$direct_python" "$DIRECT_REQUIREMENTS_SHA256" "$MANIFEST_MODEL" "$MANIFEST_REVISION" "$MODEL_CACHE" "$MODEL_SNAPSHOT" <<'PY'
import json
import os
import pathlib
import sys
import time

output, python_bin, lock_sha, model, revision, cache, snapshot = map(pathlib.Path, sys.argv[1:])
state = {
    "schema_version": "h100-direct-setup.v1",
    "provenance": "setup",
    "backend": "direct",
    "python": {"executable": str(python_bin), "version": "3.11"},
    "vllm": {"version": "0.10.0"},
    "direct_requirements_sha256": str(lock_sha),
    "model": {"id": str(model), "revision": str(revision), "cache": str(cache), "snapshot": str(snapshot)},
    "host": {"hostname": os.uname().nodename},
    "completed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
output.parent.mkdir(parents=True, exist_ok=True)
temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(output)
PY
  printf 'Direct setup complete: Python=%s vLLM=0.10.0 model=%s revision=%s\n' \
    "$direct_python" "$MANIFEST_MODEL" "$MANIFEST_REVISION"
  printf 'Direct setup metadata: %s; no server, calibration, holdout, or measurement started\n' "$STATE_ROOT/direct_setup.json"
}

if (( SETUP )); then
  setup_direct_runtime
  exit 0
fi

select_runtime_backend() {
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$BACKEND" "$MANIFEST" <<'PY'
import sys

from agentic_sim.runtime.backend import resolve_backend, select_backend
from agentic_sim.runtime.vllm_config import resolve_vllm_config

try:
    requested = select_backend(sys.argv[1], manifest_path=sys.argv[2])
    config = resolve_vllm_config(sys.argv[2], environ={})
    print(resolve_backend(requested, config))
except Exception as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

SELECTED_BACKEND="$(select_runtime_backend)" || die 'runtime backend selection failed; no backend fallback was attempted'
printf 'Selected runtime backend: %s (requested %s)\n' "$SELECTED_BACKEND" "$BACKEND"
export BACKEND="$SELECTED_BACKEND"
RUNTIME_TRACE_ROOT="$TRACE_ROOT/$SELECTED_BACKEND"
mkdir -p -- "$RUNTIME_TRACE_ROOT"

if [[ "$SELECTED_BACKEND" == direct ]]; then
  # Direct mode is for an already-prepared VM/GPU container.  It must not
  # install Docker or invoke any Docker command; the shared backend module
  # owns the pinned command, health lifecycle, state, provenance, and cleanup.
  DIRECT_PYTHON="$PYTHON_ENV_ROOT/bin/python"
  [[ -x "$DIRECT_PYTHON" ]] || die "direct backend requires a prepared Python environment: $DIRECT_PYTHON"
  command -v nvidia-smi >/dev/null 2>&1 || die 'direct backend requires nvidia-smi for H100 identity validation'
  DIRECT_GPU_ROWS="$(nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits 2>/dev/null || true)"
  [[ "$(printf '%s\n' "$DIRECT_GPU_ROWS" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')" == 1 ]] || die 'direct backend requires exactly one visible GPU'
  IFS=',' read -r DIRECT_GPU_NAME DIRECT_GPU_MEMORY DIRECT_GPU_COMPUTE <<< "$DIRECT_GPU_ROWS"
  DIRECT_GPU_NAME="$(sed 's/^ *//;s/ *$//' <<< "$DIRECT_GPU_NAME")"
  DIRECT_GPU_MEMORY="$(sed 's/^ *//;s/ *$//' <<< "$DIRECT_GPU_MEMORY")"
  DIRECT_GPU_COMPUTE="$(sed 's/^ *//;s/ *$//' <<< "$DIRECT_GPU_COMPUTE")"
  [[ "$DIRECT_GPU_NAME" == *H100* && "$DIRECT_GPU_NAME" != *A100* && "$DIRECT_GPU_NAME" != *H200* ]] || die "direct GPU is not H100: $DIRECT_GPU_NAME"
  [[ "$DIRECT_GPU_MEMORY" =~ ^[0-9]+$ && "$DIRECT_GPU_MEMORY" -ge 80000 ]] || die 'direct H100 memory is below 80000 MiB'
  [[ "$DIRECT_GPU_COMPUTE" == 9.0 || "$DIRECT_GPU_COMPUTE" == 9.0* ]] || die 'direct H100 compute capability is not 9.0'
  DIRECT_EXISTING_GPU_PIDS="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
  [[ -z "$DIRECT_EXISTING_GPU_PIDS" ]] || die "direct GPU already has compute processes: $DIRECT_EXISTING_GPU_PIDS"
  DIRECT_NVIDIA_SUMMARY="$(nvidia-smi 2>/dev/null || true)"
  grep -Fq "CUDA Version: $CUDA_VERSION" <<< "$DIRECT_NVIDIA_SUMMARY" || die 'direct CUDA version is not the pinned version'
  H100_NSYS_BIN="${H100_NSYS_BIN:-/usr/local/cuda/bin/nsys}"
  [[ -x "$H100_NSYS_BIN" ]] || die "direct backend requires the reviewed tracing binary: $H100_NSYS_BIN"
  verify_checkout_identity
  DIRECT_PYTHON_VERSION="$($DIRECT_PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [[ "$DIRECT_PYTHON_VERSION" == '3.11' ]] || die "direct backend requires Python 3.11, found $DIRECT_PYTHON_VERSION"
  check_direct_python_lock "$DIRECT_PYTHON" "$DIRECT_REQUIREMENTS" \
    || die 'direct backend Python environment does not match the pinned vLLM closure'
  "$DIRECT_PYTHON" -m pip check >/dev/null 2>&1 || die 'direct backend Python environment failed pip check'
  "$DIRECT_PYTHON" -c 'import importlib.metadata; expected="0.10.0"; actual=importlib.metadata.version("vllm"); raise SystemExit(0 if actual == expected else f"vLLM version mismatch: {actual}")' \
    || die 'direct backend vLLM package is not the pinned 0.10.0 release'
  export H100_MODEL_CACHE="$MODEL_CACHE"
  export H100_MODEL_SNAPSHOT="$MODEL_SNAPSHOT"
  export H100_TRACE_MOUNT_ROOT="$RUNTIME_TRACE_ROOT"
  export H100_TRACE_PROVIDER="$TRACE_PROVIDER"
  export H100_NSYS_BIN
  export H100_VLLM_BASE_URL="http://127.0.0.1:$PORT"
  export VLLM_MODEL="$MANIFEST_MODEL"
  export VLLM_MODEL_REVISION="$MANIFEST_REVISION"
  export VLLM_IMAGE="$MANIFEST_IMAGE"
  export VLLM_PORT="$PORT"
  export VLLM_MAX_MODEL_LEN="$MANIFEST_MAX_MODEL_LEN"
  export VLLM_GPU_MEMORY_UTILIZATION="$GPU_MEM_UTIL"
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$DIRECT_PYTHON" -m agentic_sim.runtime.backend \
    --backend direct \
    --manifest "$MANIFEST" \
    --artifact-root "$WORK_ROOT/runtime" \
    --protected-root "$ROOT/artifacts/h100_final_validation" \
    --protected-root "$ROOT/project" \
    --model-cache "$MODEL_CACHE" \
    --model-path "$MODEL_SNAPSHOT" \
    --python "$DIRECT_PYTHON" \
    --trace-binary "$H100_NSYS_BIN" \
    --trace-session "$SESSION" \
    --protocol-sha256 "$PROTOCOL_SHA256" \
    --health-timeout 180 \
    || die 'direct vLLM runtime failed; state and cleanup metadata were preserved'
  DIRECT_RUNTIME_STATE="$WORK_ROOT/runtime/direct/run_state.json"
  DIRECT_GPU_PIDS="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$DIRECT_PYTHON" - "$DIRECT_RUNTIME_STATE" "$DIRECT_GPU_PIDS" "$H100_NSYS_BIN" "$SESSION" <<'PY'
import json
import pathlib
import sys

state_path = pathlib.Path(sys.argv[1])
state = json.loads(state_path.read_text(encoding="utf-8"))
if state.get("backend") != "direct" or state.get("status") != "healthy":
    raise SystemExit("direct runtime did not produce healthy state")
pids = sorted(
    {
        line.split(",", 1)[0].strip()
        for line in sys.argv[2].splitlines()
        if line.strip() and line.split(",", 1)[0].strip().isdigit()
    }
)
if not pids:
    raise SystemExit("direct runtime has no visible GPU process")
state["runtime_pids"] = [int(pid) for pid in pids]
state["trace_binary"] = sys.argv[3]
state["trace_session"] = sys.argv[4]
temporary = state_path.with_name(state_path.name + ".tmp")
temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(state_path)
PY
  printf 'Direct H100 server ready: model=%s revision=%s artifact_root=%s/direct\n' "$MANIFEST_MODEL" "$MANIFEST_REVISION" "$WORK_ROOT/runtime"
  exit 0
fi

ensure_system_packages() {
  local marker="$STATE_ROOT/system.lock.sha256" marker_hash='' line package version
  local -a specs=()
  while IFS='=' read -r package version || [[ -n "$package" ]]; do
    [[ -z "$package" || "$package" == \#* ]] && continue
    [[ "$package" =~ ^[a-z0-9][a-z0-9+.-]*$ && "$version" != '' ]] || die "malformed system lock line"
    specs+=("$package=$version")
  done < "$SYSTEM_LOCK"
  ((${#specs[@]} > 0)) || die 'system package lock is empty'
  if [[ -f "$marker" ]]; then marker_hash="$(awk 'NF {print $1; exit}' "$marker")"; fi
  local packages_ok=1
  for spec in "${specs[@]}"; do
    package="${spec%%=*}"
    version="${spec#*=}"
    if [[ "$(dpkg-query -W -f='${Status} ${Version}' "$package" 2>/dev/null || true)" != *" ok installed $version" ]]; then
      packages_ok=0
      break
    fi
  done
  if [[ "$marker_hash" == "$SYSTEM_LOCK_SHA256" && "$packages_ok" == 1 ]]; then
    printf 'System packages already validated: %s\n' "$SYSTEM_LOCK_SHA256"
    return 0
  fi
  command -v apt-get >/dev/null 2>&1 || die 'apt-get is required to install the pinned system lock'
  if (( EUID == 0 )); then
    root_apt() { env DEBIAN_FRONTEND=noninteractive apt-get "$@"; }
  else
    command -v sudo >/dev/null 2>&1 || die 'sudo is required for pinned system package installation'
    sudo -n true >/dev/null 2>&1 || die 'non-interactive sudo is required for pinned system package installation'
    root_apt() { sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "$@"; }
  fi
  printf 'Installing reviewed system lock (offline-first): %s\n' "$SYSTEM_LOCK_SHA256"
  if ! root_apt --no-download --allow-downgrades install -y --no-install-recommends "${specs[@]}"; then
    root_apt --allow-downgrades install -y --no-install-recommends "${specs[@]}"
  fi
  for spec in "${specs[@]}"; do
    package="${spec%%=*}"
    version="${spec#*=}"
    [[ "$(dpkg-query -W -f='${Status} ${Version}' "$package" 2>/dev/null || true)" == *" ok installed $version" ]] || die "system package did not match lock: $package"
  done
  mkdir -p -- "$STATE_ROOT"
  printf '%s  %s\n' "$SYSTEM_LOCK_SHA256" "$SYSTEM_LOCK" > "$marker.tmp"
  mv -f -- "$marker.tmp" "$marker"
}

ensure_python_packages() {
  local marker="$STATE_ROOT/python.lock.sha256" marker_hash='' python_bin="$PYTHON_ENV_ROOT/bin/python"
  [[ -x "$PYTHON_BOOTSTRAP_BIN" || -n "$(command -v "$PYTHON_BOOTSTRAP_BIN" 2>/dev/null || true)" ]] || die "Python bootstrap binary is missing: $PYTHON_BOOTSTRAP_BIN"
  if [[ ! -x "$python_bin" ]]; then
    mkdir -p -- "$PYTHON_ENV_ROOT"
    "$PYTHON_BOOTSTRAP_BIN" -m venv "$PYTHON_ENV_ROOT"
  fi
  [[ -x "$python_bin" ]] || die "Python environment was not created: $PYTHON_ENV_ROOT"
  local python_version
  python_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [[ "$python_version" == '3.11' ]] || die "Python lock requires 3.11, found $python_version"
  if [[ -f "$marker" ]]; then marker_hash="$(awk 'NF {print $1; exit}' "$marker")"; fi
  local packages_ok=1
  if [[ "$marker_hash" == "$PYTHON_LOCK_SHA256" ]]; then
    "$python_bin" - "$PYTHON_LOCK" <<'PY' >/dev/null 2>&1 || packages_ok=0
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

lock = Path(sys.argv[1]).read_text(encoding="utf-8")
expected = {}
for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ \t\\]+)", lock, re.MULTILINE):
    expected[re.sub(r"[-_.]+", "-", match.group(1).lower())] = match.group(2)
for name, version in expected.items():
    try:
        actual = metadata.version(name)
    except metadata.PackageNotFoundError:
        raise SystemExit(1)
    if actual != version:
        raise SystemExit(1)
PY
    "$python_bin" -m pip check >/dev/null 2>&1 || packages_ok=0
  else
    packages_ok=0
  fi
  if [[ "$marker_hash" == "$PYTHON_LOCK_SHA256" && "$packages_ok" == 1 ]]; then
    printf 'Python packages already validated: %s\n' "$PYTHON_LOCK_SHA256"
    return 0
  fi
  mkdir -p -- "$WORK_ROOT/cache/pip"
  local -a pip_args=(--disable-pip-version-check --no-input --only-binary=:all: --require-hashes --upgrade -r "$PYTHON_LOCK")
  printf 'Installing reviewed Python lock (offline-first): %s\n' "$PYTHON_LOCK_SHA256"
  if ! PIP_CACHE_DIR="$WORK_ROOT/cache/pip" "$python_bin" -m pip install --no-index "${pip_args[@]}"; then
    PIP_CACHE_DIR="$WORK_ROOT/cache/pip" "$python_bin" -m pip install "${pip_args[@]}"
  fi
  "$python_bin" -m pip check >/dev/null 2>&1 || die 'pinned Python environment failed pip check'
  "$python_bin" - "$PYTHON_LOCK" <<'PY'
import importlib.metadata as metadata
import re
import sys
from pathlib import Path

lock = Path(sys.argv[1]).read_text(encoding="utf-8")
for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ \t\\]+)", lock, re.MULTILINE):
    name = re.sub(r"[-_.]+", "-", match.group(1).lower())
    if metadata.version(name) != match.group(2):
        raise SystemExit(f"locked package mismatch: {name}")
PY
  mkdir -p -- "$STATE_ROOT"
  printf '%s  %s\n' "$PYTHON_LOCK_SHA256" "$PYTHON_LOCK" > "$marker.tmp"
  mv -f -- "$marker.tmp" "$marker"
  PYTHON_BIN="$python_bin"
}

ensure_system_packages
command -v git >/dev/null 2>&1 || die 'git is required'
command -v python3 >/dev/null 2>&1 || die 'python3 is required'
PYTHON_BIN="$PYTHON_ENV_ROOT/bin/python"
ensure_python_packages
export PATH="$PYTHON_ENV_ROOT/bin:$PATH"

verify_checkout_identity

CONFIG_VALUES="$("$PYTHON_BIN" - "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

d = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
software = d.get("frozen_software", {})
hardware = d.get("hardware", {})
request = d.get("request_protocol", {})
if d.get("schema_version") != "h100-final-validation.v1": raise SystemExit("protocol schema mismatch")
if d.get("launch_authorized") is not False: raise SystemExit("sealed protocol launch guard changed")
if d.get("status") != "sealed_scaffold_not_launched": raise SystemExit("protocol status changed")
if software.get("model") != "Qwen/Qwen3-Coder-30B-A3B-Instruct": raise SystemExit("model mismatch")
if software.get("model_revision") != "b2cff646eb4bb1d68355c01b18ae02e7cf42d120": raise SystemExit("model revision mismatch")
if software.get("vllm_image") != "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271": raise SystemExit("image mismatch")
if software.get("vllm_tool_parser") != "qwen3_coder" or software.get("context_tokens") != 32768: raise SystemExit("vLLM parser/context mismatch")
if software.get("tensor_parallel_size") != 1 or software.get("precision") != "bfloat16": raise SystemExit("vLLM runtime pin mismatch")
if request.get("concurrency") != 1 or request.get("warmup_requests") != 2 or request.get("measured_repetitions_per_case") != 3: raise SystemExit("request protocol mismatch")
if hardware.get("gpu_family") != "H100" or not hardware.get("one_gpu_only"): raise SystemExit("hardware protocol mismatch")
print("\t".join((software["model"], software["model_revision"], software["vllm_image"], software["vllm_tool_parser"], str(software["context_tokens"]), str(software["tensor_parallel_size"]))))
PY
)"
IFS=$'\t' read -r MODEL REVISION VLLM_IMAGE PARSER MAX_MODEL_LEN TENSOR_PARALLEL_SIZE <<< "$CONFIG_VALUES"
[[ "$MODEL" == "$MANIFEST_MODEL" && "$REVISION" == "$MANIFEST_REVISION" ]] || die 'manifest and sealed config model pins differ'
[[ "$VLLM_IMAGE" == "$MANIFEST_IMAGE" && "$PARSER" == "$MANIFEST_PARSER" ]] || die 'manifest and sealed config vLLM pins differ'
[[ "$MAX_MODEL_LEN" == "$MANIFEST_MAX_MODEL_LEN" && "$TENSOR_PARALLEL_SIZE" == "$MANIFEST_TP" ]] || die 'manifest and sealed config runtime pins differ'

command -v docker >/dev/null 2>&1 || die 'docker is required'
command -v nvidia-smi >/dev/null 2>&1 || die 'nvidia-smi is required'
command -v curl >/dev/null 2>&1 || die 'curl is required'
[[ -x /usr/local/cuda/bin/nsys ]] || die 'Nsight Systems is missing at /usr/local/cuda/bin/nsys'
NSYS_VERSION="$('/usr/local/cuda/bin/nsys' --version 2>&1)"
[[ "$NSYS_VERSION" == *"NVIDIA Nsight Systems version $NSYS_VERSION_PREFIX"* ]] || die 'Nsight Systems version is not the pinned reviewed version'
docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
DOCKER_RUNTIMES="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
grep -Eq '(^|[^A-Za-z])nvidia([^A-Za-z]|$)' <<< "$DOCKER_RUNTIMES" || die 'Docker NVIDIA runtime is unavailable'

GPU_ROWS="$(nvidia-smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>/dev/null || true)"
[[ "$(printf '%s\n' "$GPU_ROWS" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')" == 1 ]] || die 'exactly one visible GPU is required'
IFS=',' read -r GPU_NAME GPU_MEMORY GPU_COMPUTE GPU_DRIVER <<< "$GPU_ROWS"
: "$GPU_DRIVER"
GPU_NAME="$(sed 's/^ *//;s/ *$//' <<< "$GPU_NAME")"
GPU_MEMORY="$(sed 's/^ *//;s/ *$//' <<< "$GPU_MEMORY")"
GPU_COMPUTE="$(sed 's/^ *//;s/ *$//' <<< "$GPU_COMPUTE")"
[[ "$GPU_NAME" == 'NVIDIA H100 80GB HBM3' || "$GPU_NAME" == 'NVIDIA H100 PCIe 80GB' ]] || die "visible GPU is not an allowlisted H100: $GPU_NAME"
[[ "$GPU_MEMORY" =~ ^[0-9]+$ && "$GPU_MEMORY" -ge 80000 ]] || die 'H100 memory is below 80000 MiB'
[[ "$GPU_COMPUTE" == 9.0 || "$GPU_COMPUTE" == 9.0* ]] || die 'H100 compute capability is not 9.0'
NVIDIA_SUMMARY="$(nvidia-smi 2>/dev/null || true)"
grep -Fq "CUDA Version: $CUDA_VERSION" <<< "$NVIDIA_SUMMARY" || die 'host CUDA version is not the pinned version'

docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 || docker pull --platform linux/amd64 "$VLLM_IMAGE" >/dev/null
IMAGE_REF="${VLLM_IMAGE%@*}"
IMAGE_REPO="${IMAGE_REF%%:*}"
IMAGE_DIGEST="${VLLM_IMAGE##*@}"
REPO_DIGESTS="$(docker image inspect "$VLLM_IMAGE" --format '{{join .RepoDigests "\n"}}' 2>/dev/null || true)"
grep -Fqx "$IMAGE_REPO@$IMAGE_DIGEST" <<< "$REPO_DIGESTS" || die 'pinned vLLM image digest mismatch'
[[ "$(docker image inspect "$VLLM_IMAGE" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true)" == "$MANIFEST_PLATFORM" ]] || die 'pinned vLLM image platform mismatch'

docker run --rm --pull=never --network none --gpus device=0 --entrypoint python3 "$VLLM_IMAGE" -c 'import torch; assert torch.cuda.is_available(); assert torch.cuda.device_count() == 1; assert "H100" in torch.cuda.get_device_name(0)' >/dev/null 2>&1 || die 'Docker NVIDIA/CUDA runtime did not expose exactly one H100'

port_in_use() {
  if command -v ss >/dev/null 2>&1; then ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .
  elif command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else die 'ss or lsof is required to check the server port'; fi
}
container_exists() { docker container inspect "$CONTAINER" >/dev/null 2>&1; }
container_running() { [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" == true ]]; }
container_matches() {
  local command_json devices mounts required
  [[ "$(docker inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || true)" == "$VLLM_IMAGE" ]] || return 1
  [[ "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER" 2>/dev/null || true)" == host ]] || return 1
  [[ "$(docker inspect --format '{{.HostConfig.IpcMode}}' "$CONTAINER" 2>/dev/null || true)" == host ]] || return 1
  [[ "$(docker inspect --format '{{.HostConfig.Runtime}}' "$CONTAINER" 2>/dev/null || true)" == nvidia ]] || return 1
  [[ "$(docker inspect --format '{{.HostConfig.ShmSize}}' "$CONTAINER" 2>/dev/null || true)" == 17179869184 ]] || return 1
  command_json="$(docker inspect --format '{{json .Config.Cmd}}' "$CONTAINER" 2>/dev/null || true)"
  for required in "--session-new=$SESSION" '--trace=cuda,osrt' '--cuda-event-trace=false' 'vllm.entrypoints.openai.api_server' '--revision' "$REVISION" '--served-model-name' "$MODEL" '--port' "$PORT" '--max-model-len' "$MAX_MODEL_LEN" '--tensor-parallel-size' "$TENSOR_PARALLEL_SIZE" '--tool-call-parser' "$PARSER"; do
    grep -Fq -- "$required" <<< "$command_json" || return 1
  done
  devices="$(docker inspect --format '{{json .HostConfig.DeviceRequests}}' "$CONTAINER" 2>/dev/null || true)"
  grep -Fq '"DeviceIDs":["0"]' <<< "$devices" || return 1
  grep -Fq '"Capabilities":[["gpu"]]' <<< "$devices" || return 1
  mounts="$(docker inspect --format '{{json .Mounts}}' "$CONTAINER" 2>/dev/null || true)"
  grep -Fq '"Destination":"/root/.cache/huggingface"' <<< "$mounts" || return 1
  grep -Fq '"Destination":"/host-cuda"' <<< "$mounts" || return 1
  grep -Fq '"Destination":"/trace"' <<< "$mounts" || return 1
}
gpu_processes="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
container_pids() { docker top "$CONTAINER" -eo pid 2>/dev/null | awk 'NR > 1 {print $1}'; }
gpu_processes_are_expected() {
  local pid expected
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    expected="$(container_pids)"
    grep -Eq "(^|[[:space:]])${pid}([[:space:]]|$)" <<< "$expected" || return 1
  done <<< "$gpu_processes"
}
duplicate_container_check() {
  local id name image command_json
  while IFS= read -r id; do
    [[ -n "$id" ]] || continue
    name="$(docker inspect --format '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##')"
    [[ "$name" == "$CONTAINER" ]] && continue
    image="$(docker inspect --format '{{.Config.Image}}' "$id" 2>/dev/null || true)"
    command_json="$(docker inspect --format '{{json .Config.Cmd}}' "$id" 2>/dev/null || true)"
    if [[ "$image" == "$VLLM_IMAGE" ]] || grep -Fq 'vllm.entrypoints.openai.api_server' <<< "$command_json" || grep -Fq -- "--port\",\"$PORT" <<< "$command_json"; then
      die "duplicate or conflicting running container: $name"
    fi
  done < <(docker ps -q)
}

health_check() {
  local base="http://127.0.0.1:$PORT" models normal tool metrics logs
  curl -fsS --connect-timeout 5 --max-time 10 "$base/health" >/dev/null || die 'health check failed: /health'
  models="$(curl -fsS --connect-timeout 5 --max-time 10 "$base/v1/models")" || die 'health check failed: /v1/models'
  "$PYTHON_BIN" - "$models" "$MODEL" <<'PY' || die 'served model ID does not match the sealed model'
import json
import sys
d = json.loads(sys.argv[1])
if not any(item.get("id") == sys.argv[2] for item in d.get("data", [])):
    raise SystemExit(1)
PY
  normal='{"model":"'"$MODEL"'","prompt":"Reply READY.","max_tokens":8,"temperature":0,"top_p":1,"seed":0}'
  curl -fsS --connect-timeout 5 --max-time 60 -H 'Content-Type: application/json' -d "$normal" "$base/v1/completions" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); assert d.get("choices")' >/dev/null || die 'health check failed: normal completion'
  tool='{"model":"'"$MODEL"'","messages":[{"role":"user","content":"Call ping."}],"tools":[{"type":"function","function":{"name":"ping","description":"Return pong","parameters":{"type":"object","properties":{}}}}],"tool_choice":{"type":"function","function":{"name":"ping"}},"max_tokens":32,"temperature":0,"top_p":1,"seed":0}'
  curl -fsS --connect-timeout 5 --max-time 60 -H 'Content-Type: application/json' -d "$tool" "$base/v1/chat/completions" | "$PYTHON_BIN" -c 'import json,sys; d=json.load(sys.stdin); m=d.get("choices",[{}])[0].get("message",{}); assert any(x.get("function",{}).get("name") == "ping" for x in m.get("tool_calls",[]))' >/dev/null || die 'health check failed: qwen3_coder tool parsing'
  metrics="$(curl -fsS --connect-timeout 5 --max-time 10 "$base/metrics")" || die 'health check failed: /metrics'
  for metric in 'vllm:request_success_total' 'vllm:prompt_tokens_total' 'vllm:generation_tokens_total' 'vllm:e2e_request_latency_seconds_'; do grep -Eq "^${metric}" <<< "$metrics" || die "health check failed: missing metric $metric"; done
  logs="$(docker logs --tail 240 "$CONTAINER" 2>&1 || true)"
  grep -Eiq 'vllm|api_server|Started server process' <<< "$logs" || die 'health check failed: server log has no startup record'
  if grep -Eiq 'out of memory|cuda error|fatal error|traceback' <<< "$logs"; then die 'health check failed: fatal text in server logs'; fi
  printf 'Health checks passed: /health /v1/models completion tool-parser /metrics logs\n'
}

record_docker_runtime_metadata() {
  local runtime_root="$WORK_ROOT/runtime/docker"
  mkdir -p -- "$runtime_root"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" - "$runtime_root" "$MANIFEST" "$CONTAINER" "$SESSION" "$TRACE_PROVIDER" "$H100_NSYS_BIN" "$MODEL_CACHE" "$MODEL_SNAPSHOT" "$RUNTIME_TRACE_ROOT" "$PROTOCOL_SHA256" <<'PY'
import json
import pathlib
import sys

from agentic_sim.runtime.backend import build_docker_command, build_run_metadata, deterministic_manifest
from agentic_sim.runtime.vllm_config import resolve_vllm_config

root = pathlib.Path(sys.argv[1])
manifest = pathlib.Path(sys.argv[2])
container, session, provider, trace_binary, model_cache, model_snapshot, trace_root, protocol_sha = sys.argv[3:]
config = resolve_vllm_config(manifest, environ={})
cache_path = pathlib.Path(model_cache).resolve()
snapshot_path = pathlib.Path(model_snapshot).resolve()
model_path = "/root/.cache/huggingface/" + snapshot_path.relative_to(cache_path).as_posix()
command = build_docker_command(
    config,
    container_name=container,
    model_cache=model_cache,
    model_path=model_path,
    detach=True,
    remove=False,
    trace_binary="/host-cuda/bin/nsys",
    trace_session=session,
    trace_root=trace_root,
)
metadata = build_run_metadata(
    backend="docker",
    config=config,
    command=command,
    artifact_root=root,
    protocol_sha256=protocol_sha,
    phase="calibration",
    observed_environment={
        "trace_provider": provider,
        "trace_binary": trace_binary,
        "trace_root": trace_root,
        "trace_session": session,
        "container_name": container,
        "nsight_session": session,
        "model_cache": model_cache,
        "model_path": model_path,
    },
)
metadata["runtime_launch"] = {
    "launcher": "scripts/cloud/start_h100_vllm_nsight.sh",
    "container_name": container,
    "nsight_session": session,
    "trace_provider": provider,
    "trace_binary": trace_binary,
}
payload = deterministic_manifest(metadata)
path = root / "run_metadata.json"
if path.exists() and path.read_bytes() != payload:
    raise SystemExit(f"runtime metadata changed; refusing overwrite: {path}")
if not path.exists():
    path.write_bytes(payload)
state = {
    "schema_version": "runtime-run-state.v1",
    "backend": "docker",
    "backend_contract": "runtime-backend.v1",
    "artifact_root": str(root.resolve()),
    "protocol_sha256": protocol_sha,
    "split_manifest_sha256": None,
    "prediction_manifest_sha256": None,
    "command_sha256": metadata["command_sha256"],
    "status": "healthy",
    "identifier": container,
    "metadata": "run_metadata.json",
}
state_path = root / "run_state.json"
state_payload = deterministic_manifest(state)
if state_path.exists() and state_path.read_bytes() != state_payload:
    raise SystemExit(f"runtime state changed; refusing overwrite: {state_path}")
if not state_path.exists():
    state_path.write_bytes(state_payload)
PY
}

if container_exists; then
  container_running || die "stale stopped container exists; inspect before recovery: $CONTAINER"
  container_matches || die "running container is stale or pin-mismatched: $CONTAINER"
  duplicate_container_check
  [[ -n "$gpu_processes" ]] || die 'expected running server has no GPU process'
  gpu_processes_are_expected || die 'GPU process is outside the expected server container'
  docker exec "$CONTAINER" /host-cuda/bin/nsys sessions list 2>/dev/null | grep -Fq "$SESSION" || die 'expected Nsight session is not registered'
  health_check
  record_docker_runtime_metadata
  printf 'H100 server reused: container=%s session=%s\n' "$CONTAINER" "$SESSION"
  exit 0
fi

duplicate_container_check
[[ -z "$gpu_processes" ]] || die "GPU already has compute processes: $gpu_processes"
port_in_use && die "port $PORT is occupied by a non-reviewed process"
export H100_MODEL_CACHE="$MODEL_CACHE"
export H100_MODEL_SNAPSHOT="$MODEL_SNAPSHOT"
export H100_TRACE_MOUNT_ROOT="$RUNTIME_TRACE_ROOT"
export H100_TRACE_PROVIDER="$TRACE_PROVIDER"
export H100_EXPECTED_SERVER_CONTAINER="$CONTAINER"
export H100_NSYS_CONTAINER="$CONTAINER"
export H100_NSYS_SESSION="$SESSION"
export H100_NSYS_BIN=/host-cuda/bin/nsys
export VLLM_MODEL="$MODEL"
export VLLM_MODEL_REVISION="$REVISION"
export VLLM_IMAGE
export VLLM_PORT="$PORT"
export VLLM_MAX_MODEL_LEN="$MAX_MODEL_LEN"
export VLLM_GPU_MEMORY_UTILIZATION="$GPU_MEM_UTIL"
printf 'Starting pinned profiled vLLM server through reviewed launcher...\n'
"$LAUNCHER"
container_matches || die 'launcher returned but the server container does not match the sealed contract'
gpu_processes="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
gpu_processes_are_expected || die 'new server GPU process is outside the expected container'
health_check
record_docker_runtime_metadata
printf 'H100 server ready: container=%s session=%s model=%s revision=%s\n' "$CONTAINER" "$SESSION" "$MODEL" "$REVISION"
