#!/usr/bin/env bash
# The stage runner stores deferred commands in single-quoted strings so they
# expand only when the stage executes; SC2016 is intentional here.
# shellcheck disable=SC2016
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"
WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"
LOG_DIR="${WORK_ROOT}/logs/bootstrap"
STATE="${WORK_ROOT}/state/bootstrap"
REPOS="${WORK_ROOT}/repos"
VENV="${WORK_ROOT}/venv"
PYTHON_ENV_MODE="${PYTHON_ENV_MODE:-venv}"
PYTHON_ENV_ROOT="${PYTHON_ENV_ROOT:-}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
PYTHON_VERSION_EXACT="${PYTHON_VERSION_EXACT:-}"
PYTHON_LOCK_PATH="${PYTHON_LOCK_PATH:-}"
PYTHON_LOCK="$ROOT/cloud/lambda/requirements-linux-x86_64.txt"
PYTHON_LOCK_SHA256="7e1177bf4c0b4efe4d64895f39b340413336b77e02d2f72bbf5aad387accc9cc"
DRY=0
RESUME=0
SKIP_MODEL=0

SWE_AGENT_URL="${SWE_AGENT_REPO_URL:-https://github.com/SWE-agent/SWE-agent.git}"
SWE_BENCH_URL="${SWE_BENCH_REPO_URL:-https://github.com/SWE-bench/SWE-bench.git}"
SWE_AGENT_REVISION="${SWE_AGENT_REVISION:-0f3acafacabc0def8cc76b4e48acb4b6cf302cb9}"
SWE_BENCH_REVISION="${SWE_BENCH_REVISION:-726c5461e2ef52d83cf1ea2107870a8bb3328d57}"
VLLM_REVISION="${VLLM_REVISION:-6d8d0a24c02bfd84d46b3016b865a44f048ae84b}"
VLLM_VERSION="${VLLM_VERSION:-0.10.0}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271}"
VLLM_IMAGE_PLATFORM="${VLLM_IMAGE_PLATFORM:-linux/amd64}"

usage() {
  echo 'Usage: lambda_bootstrap.sh [--manifest FILE] [--dry-run] [--resume] [--skip-model-download] [--log-dir DIR]'
}

while (($#)); do
  arg="$1"; shift
  case "$arg" in
    --dry-run) DRY=1;;
    --resume) RESUME=1;;
    --skip-model-download) SKIP_MODEL=1;;
    --manifest) [[ $# -gt 0 ]] || { echo '--manifest requires a file' >&2; exit 2; }; MANIFEST="$1"; shift;;
    --manifest=*) MANIFEST="${arg#*=}";;
    --log-dir) [[ $# -gt 0 ]] || { echo '--log-dir requires a directory' >&2; exit 2; }; LOG_DIR="$1"; shift;;
    --log-dir=*) LOG_DIR="${arg#*=}";;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $arg" >&2; exit 2;;
  esac
done

# Read only the immutable, non-secret keys. Never source the manifest.
manifest_value() {
  local wanted="$1" line key value
  [[ -f "$MANIFEST" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; value="${line#*=}"
    [[ "$key" == "$wanted" ]] && { printf '%s' "$value"; return 0; }
  done < "$MANIFEST"
  return 0
}

for key in WORK_ROOT SWE_AGENT_REVISION SWE_BENCH_REVISION VLLM_REVISION VLLM_VERSION VLLM_IMAGE VLLM_IMAGE_PLATFORM PYTHON_VERSION PYTHON_VERSION_EXACT PYTHON_ENV_MODE PYTHON_ENV_ROOT PYTHON_LOCK_PATH PYTHON_LOCK_SHA256 EVALUATOR_LITE_FIRST_IMAGE EVALUATOR_LITE_FIRST_DIGEST EVALUATOR_LITE_GOLD_IMAGE EVALUATOR_LITE_GOLD_DIGEST EVALUATOR_VERIFIED_GOLD_IMAGE EVALUATOR_VERIFIED_GOLD_DIGEST; do
  value="$(manifest_value "$key")"
  [[ -n "$value" ]] || continue
  case "$key" in
    WORK_ROOT) WORK_ROOT="$value";;
    SWE_AGENT_REVISION) SWE_AGENT_REVISION="$value";;
    SWE_BENCH_REVISION) SWE_BENCH_REVISION="$value";;
    VLLM_REVISION) VLLM_REVISION="$value";;
    VLLM_VERSION) VLLM_VERSION="$value";;
    VLLM_IMAGE) VLLM_IMAGE="$value";;
    VLLM_IMAGE_PLATFORM) VLLM_IMAGE_PLATFORM="$value";;
    PYTHON_VERSION) PYTHON_VERSION="$value";;
    PYTHON_VERSION_EXACT) PYTHON_VERSION_EXACT="$value";;
    PYTHON_ENV_MODE) PYTHON_ENV_MODE="$value";;
    PYTHON_ENV_ROOT) PYTHON_ENV_ROOT="$value";;
    PYTHON_LOCK_PATH) PYTHON_LOCK_PATH="$value";;
    PYTHON_LOCK_SHA256) PYTHON_LOCK_SHA256="$value";;
    EVALUATOR_LITE_FIRST_IMAGE) EVALUATOR_LITE_FIRST_IMAGE="$value";;
    EVALUATOR_LITE_FIRST_DIGEST) EVALUATOR_LITE_FIRST_DIGEST="$value";;
    EVALUATOR_LITE_GOLD_IMAGE) EVALUATOR_LITE_GOLD_IMAGE="$value";;
    EVALUATOR_LITE_GOLD_DIGEST) EVALUATOR_LITE_GOLD_DIGEST="$value";;
    EVALUATOR_VERIFIED_GOLD_IMAGE) EVALUATOR_VERIFIED_GOLD_IMAGE="$value";;
    EVALUATOR_VERIFIED_GOLD_DIGEST) EVALUATOR_VERIFIED_GOLD_DIGEST="$value";;
  esac
done
case "$PYTHON_ENV_MODE" in
  venv)
    VENV="${PYTHON_ENV_ROOT:-$WORK_ROOT/venv}"
    ;;
  managed)
    if [[ -z "$PYTHON_ENV_ROOT" ]]; then
      PYTHON_ENV_ROOT="$(python3 -c 'import sys; print(sys.prefix)')"
    fi
    VENV="$PYTHON_ENV_ROOT"
    ;;
  *) echo "PYTHON_ENV_MODE must be managed or venv: $PYTHON_ENV_MODE" >&2; exit 1;;
esac
LOG_DIR="${LOG_DIR:-$WORK_ROOT/logs/bootstrap}"
STATE="$WORK_ROOT/state/bootstrap"; REPOS="$WORK_ROOT/repos"

if [[ -n "$PYTHON_LOCK_PATH" ]]; then
  if [[ -f "$PYTHON_LOCK_PATH" ]]; then
    PYTHON_LOCK="$PYTHON_LOCK_PATH"
  elif [[ "$PYTHON_LOCK_PATH" == */cloud/lambda/requirements-linux-x86_64*.txt ]]; then
    # A local dry-run may still use the example's archive-time path. Resolve
    # that reviewed suffix against the checked-out project without accepting
    # an arbitrary missing lock path.
    PYTHON_LOCK="$ROOT/cloud/lambda/${PYTHON_LOCK_PATH##*/cloud/lambda/}"
  else
    echo "pinned Python lock path is missing: $PYTHON_LOCK_PATH" >&2
    exit 1
  fi
fi

pin() { [[ "$2" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "$1 must be a 40-hex immutable commit" >&2; return 1; }; }
pin SWE_AGENT_REVISION "$SWE_AGENT_REVISION"
pin SWE_BENCH_REVISION "$SWE_BENCH_REVISION"
pin VLLM_REVISION "$VLLM_REVISION"
[[ "$VLLM_IMAGE" == *@sha256:* ]] || { echo 'VLLM_IMAGE must include an immutable digest' >&2; exit 1; }
[[ "$VLLM_IMAGE_PLATFORM" == linux/amd64 ]] || { echo 'VLLM_IMAGE_PLATFORM must be linux/amd64' >&2; exit 1; }
[[ -s "$PYTHON_LOCK" ]] || { echo "pinned Linux Python lock is missing: $PYTHON_LOCK" >&2; exit 1; }
[[ "$PYTHON_LOCK_SHA256" =~ ^[0-9a-fA-F]{64}$ ]] || { echo 'PYTHON_LOCK_SHA256 must be a 64-hex digest' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then lock_digest="$(sha256sum -- "$PYTHON_LOCK" | awk '{print $1}')"; else lock_digest="$(shasum -a 256 -- "$PYTHON_LOCK" | awk '{print $1}')"; fi
[[ "$lock_digest" == "$PYTHON_LOCK_SHA256" ]] || { echo 'Linux Python lock digest mismatch' >&2; exit 1; }
grep -Fq -- "--python-version $PYTHON_VERSION" <(head -n 5 "$PYTHON_LOCK") || {
  echo "Python lock was not resolved for Python $PYTHON_VERSION: $PYTHON_LOCK" >&2
  exit 1
}
grep -Fq -- '--python-platform x86_64-manylinux2014' <(head -n 5 "$PYTHON_LOCK") || {
  echo "Python lock is not the reviewed Linux x86-64 resolution: $PYTHON_LOCK" >&2
  exit 1
}

REPOSITORY_REVISION="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'no-git')"
BOOTSTRAP_FINGERPRINT="$(python3 - "$MANIFEST" "$ROOT/scripts/cloud/lambda_bootstrap.sh" "$ROOT/pyproject.toml" "$PYTHON_LOCK" "$lock_digest" "$REPOSITORY_REVISION" "$PYTHON_ENV_MODE" "$VENV" "$PYTHON_VERSION" "$PYTHON_VERSION_EXACT" "$WORK_ROOT" <<'PY'
import hashlib
import pathlib
import sys

manifest, script, project, lock, lock_digest, *values = sys.argv[1:]
parts = []
for path in (manifest, script, project, lock):
    p = pathlib.Path(path)
    digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "missing"
    parts.append(f"{p}={digest}")
parts.extend(values)
print(hashlib.sha256("\n".join(parts).encode()).hexdigest())
PY
)"

if (( DRY )); then
  cat <<EOF
DRY-RUN: no mutation, downloads, installs, containers, or paid calls.
DRY-RUN: preflight Linux x86-64/H100/Docker/disk/network, install missing utilities, use $PYTHON_ENV_MODE Python environment $VENV and create caches.
DRY-RUN: resolved Python $PYTHON_VERSION lock $PYTHON_LOCK (sha256 $lock_digest); bootstrap fingerprint $BOOTSTRAP_FINGERPRINT.
DRY-RUN: checkout SWE-agent@$SWE_AGENT_REVISION and SWE-bench@$SWE_BENCH_REVISION; install the pinned Linux Python lock $PYTHON_LOCK, then install both detached source trees and this project with --no-deps.
DRY-RUN: pull and verify vLLM image $VLLM_IMAGE for $VLLM_IMAGE_PLATFORM (source target $VLLM_REVISION), plus the three selected evaluator images.
DRY-RUN: download model revision and the exact Lite/Verified rows, generate local task/evaluator JSON, run all local tests, and write validated stage markers.
DRY-RUN: next command after success: $ROOT/scripts/cloud/lambda_start_vllm.sh --manifest $MANIFEST
EOF
  exit 0
fi

mkdir -p -- "$LOG_DIR" "$STATE" "$WORK_ROOT/artifacts/manifests"
exec > >(tee -a "$LOG_DIR/bootstrap.log") 2>&1

stage() {
  local name="$1" validator="$2"; shift 2
  local marker="$STATE/$name.ok" start end rc
  if (( RESUME )) && [[ -f "$marker" ]] && grep -Fqx "bootstrap_fingerprint=$BOOTSTRAP_FINGERPRINT" "$marker"; then
    # Run resume validation in a fresh errexit/pipelinefail context. An eval
    # used directly as an `if` condition disables errexit inside called
    # functions and can otherwise turn a failed audit into a false PASS.
    if ( set -Eeuo pipefail; eval "$validator" ); then
      echo "stage $name already validated"
      return 0
    fi
  fi
  rm -f -- "$marker"
  start="$(date -u +%FT%TZ)"; echo "stage $name start $start"
  # Keep the outer script alive long enough to record a stage failure, but run
  # the stage itself with errexit so a failed install cannot be masked by a
  # later successful command in the same function.
  set +e
  ( set -Eeuo pipefail; "$@" )
  rc=$?
  set -e
  end="$(date -u +%FT%TZ)"; echo "stage $name end $end exit=$rc"
  (( rc == 0 )) || return "$rc"
  eval "$validator"
  printf 'bootstrap_fingerprint=%s\nvalidated_at_utc=%s\n' "$BOOTSTRAP_FINGERPRINT" "$end" > "$marker"
}

preflight() { "$ROOT/scripts/cloud/lambda_preflight.sh" --manifest "$MANIFEST" --output "$WORK_ROOT/artifacts/manifests/lambda_preflight.json"; }
utilities() {
  local missing=() tool
  for tool in git curl jq rsync tmux tar zstd python3; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  if [[ "$PYTHON_ENV_MODE" != managed ]]; then
    python3 -m venv --help >/dev/null 2>&1 || missing+=(python3-venv)
  fi
  ((${#missing[@]} == 0)) && return 0
  if ! command -v sudo >/dev/null 2>&1 || ! command -v apt-get >/dev/null 2>&1; then
    echo "missing utilities: ${missing[*]}" >&2
    return 1
  fi
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}
directories() {
  mkdir -p -- "$REPOS" "$WORK_ROOT/cache/huggingface" "$WORK_ROOT/cache/pip" "$WORK_ROOT/cache/uv" "$WORK_ROOT/artifacts/manifests" "$WORK_ROOT/datasets"
  [[ "$PYTHON_ENV_MODE" == managed ]] || mkdir -p -- "$VENV"
}
python_environment() {
  if [[ "$PYTHON_ENV_MODE" == managed ]]; then
    [[ -x "$VENV/bin/python" ]] || { echo "managed Python environment is unavailable: $VENV" >&2; return 1; }
    [[ "$("$VENV/bin/python" -c 'import sys; print(sys.prefix)')" == "$VENV" ]] || {
      echo "managed Python prefix mismatch: expected $VENV" >&2
      return 1
    }
  else
    [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
  fi
  actual_python_version="$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  actual_python_exact="$("$VENV/bin/python" -c 'import platform; print(platform.python_version())')"
  [[ "$actual_python_version" == "$PYTHON_VERSION" ]] || {
    echo "Python version mismatch: manifest=$PYTHON_VERSION actual=$actual_python_version" >&2
    return 1
  }
  if [[ -n "$PYTHON_VERSION_EXACT" && "$actual_python_exact" != "$PYTHON_VERSION_EXACT" ]]; then
    echo "Python exact version mismatch: manifest=$PYTHON_VERSION_EXACT actual=$actual_python_exact" >&2
    return 1
  fi
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --only-binary=:all: 'pip==24.3.1' 'setuptools==75.6.0' 'wheel==0.45.1'
}
validate_python_environment() {
  [[ -x "$VENV/bin/python" ]] || return 1
  [[ "$("$VENV/bin/python" -c 'import sys; print(sys.prefix)')" == "$VENV" ]] || return 1
  actual_python_version="$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  [[ "$actual_python_version" == "$PYTHON_VERSION" ]] || return 1
  if [[ -n "$PYTHON_VERSION_EXACT" ]]; then
    actual_python_exact="$("$VENV/bin/python" -c 'import platform; print(platform.python_version())')"
    [[ "$actual_python_exact" == "$PYTHON_VERSION_EXACT" ]] || return 1
  fi
  "$VENV/bin/python" -c 'import pip' || return 1
}
pip_check_with_managed_allowlist() {
  local report="$WORK_ROOT/artifacts/manifests/python-pip-check.txt"
  local audit="$WORK_ROOT/artifacts/manifests/python-pip-check.json"
  local raw_tmp audit_tmp pip_status classify_status
  mkdir -p -- "$(dirname -- "$report")"
  raw_tmp="$(mktemp "$report.tmp.XXXXXX")" || return 1
  if PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_COLOR=1 LC_ALL=C \
    "$VENV/bin/python" -m pip check >"$raw_tmp" 2>&1; then
    pip_status=0
  else
    pip_status=$?
  fi
  if ! mv -f -- "$raw_tmp" "$report"; then
    rm -f -- "$raw_tmp"
    return 1
  fi
  audit_tmp="$(mktemp "$audit.tmp.XXXXXX")" || return 1
  if "$VENV/bin/python" - "$report" "$audit_tmp" "$PYTHON_LOCK" "$PYTHON_ENV_MODE" "$pip_status" "$VENV/bin/python" <<'PY'
import hashlib
import importlib.metadata as metadata
import json
import pathlib
import re
import sys
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

report_path, audit_path, lock_path, environment_mode, status_text, python_executable = sys.argv[1:]
pip_status = int(status_text)
report = pathlib.Path(report_path)
audit = pathlib.Path(audit_path)
lock = pathlib.Path(lock_path)
raw = report.read_text(encoding="utf-8", errors="replace")
raw_lines = raw.splitlines()
lines = list(raw_lines)
allowed_records = {
    ("matplotlib", "3.8.2", "numpy", "<2,>=1.21", "numpy", "2.4.6"),
    ("scikit-learn", "1.3.2", "numpy", "<2.0,>=1.17.3", "numpy", "2.4.6"),
    ("scipy", "1.11.4", "numpy", "<1.28.0,>=1.21.6", "numpy", "2.4.6"),
    ("lightning-sdk", "2026.6.8", "urllib3", "<=2.5.0", "urllib3", "2.7.0"),
}
exact_lines = {
    f"{owner} {owner_version} has requirement {dependency}{requirement}, but you have {installed} {installed_version}."
    for owner, owner_version, dependency, requirement, installed, installed_version in allowed_records
}
locked_versions = {
    canonicalize_name(match.group(1)): match.group(2)
    for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ \t\\]+)", lock.read_text(encoding="utf-8"), re.MULTILINE)
}
lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
base = {
    "schema_version": "python-pip-check.v1",
    "status": "FAIL",
    "python_env_mode": environment_mode,
    "python_executable": python_executable,
    "python_version": sys.version.split()[0],
    "pip_version": metadata.version("pip"),
    "raw_report": str(report),
    "raw_output_sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
    "lock_path": str(lock),
    "lock_sha256": lock_digest,
    "pip_check_exit_code": pip_status,
    "conflicts": [],
    "unexpected_conflicts": [],
}

def finish(status, conflicts=None, unexpected=None):
    base["status"] = status
    base["conflicts"] = conflicts or []
    base["unexpected_conflicts"] = unexpected or []
    audit.write_text(json.dumps(base, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if status in {"PASS_CLEAN", "PASS_MANAGED_BASE_ALLOWLIST"}:
        print(f"pip check: {status}")
        return 0
    for item in base["unexpected_conflicts"] or ["pip check classification failed"]:
        print(f"pip check blocking conflict: {item}", file=sys.stderr)
    return 1

if pip_status == 0:
    raise SystemExit(finish("PASS_CLEAN") if raw == "" else finish("FAIL", unexpected=["non-canonical output for a successful pip check", *lines]))
if pip_status != 1:
    raise SystemExit(finish("FAIL", unexpected=[f"pip check exited with unsupported status {pip_status}", *lines]))
if environment_mode != "managed":
    raise SystemExit(finish("FAIL", unexpected=["nonzero pip check is not allowlisted for a venv", *lines]))
if not lines or any(not line or line != line.strip() or "\x1b" in line for line in lines):
    raise SystemExit(finish("FAIL", unexpected=["blank, whitespace-padded, or ANSI pip check output", *lines]))

pattern = re.compile(r"^(?P<owner>[A-Za-z0-9_.-]+) (?P<owner_version>\S+) has requirement (?P<requirement>.+), but you have (?P<dependency>[A-Za-z0-9_.-]+) (?P<installed_version>\S+)\.$")
records = []
errors = []
for line in lines:
    if line not in exact_lines:
        errors.append(f"unrecognized pip check line: {line}")
        continue
    match = pattern.fullmatch(line)
    if not match:
        errors.append(f"pip check line did not match the reviewed grammar: {line}")
        continue
    owner = canonicalize_name(match.group("owner"))
    dependency = canonicalize_name(match.group("dependency"))
    requirement_text = match.group("requirement")
    try:
        requirement = Requirement(requirement_text)
    except Exception as exc:
        errors.append(f"invalid requirement in pip check line ({exc}): {line}")
        continue
    requirement_specifier = str(requirement.specifier)
    record = (owner, match.group("owner_version"), dependency, requirement_specifier, dependency, match.group("installed_version"))
    records.append(record)
    if record not in allowed_records:
        errors.append(f"pip check tuple is outside the reviewed allowlist: {line}")
        continue
    if canonicalize_name(requirement.name) != dependency or requirement.marker or requirement.extras:
        errors.append(f"requirement name/extras/marker mismatch: {line}")
        continue
    if requirement.specifier.contains(Version(match.group("installed_version")), prereleases=True):
        errors.append(f"allowlisted requirement does not reject installed version: {line}")
        continue
    try:
        owner_actual = metadata.version(owner)
        dependency_actual = metadata.version(dependency)
    except metadata.PackageNotFoundError as exc:
        errors.append(f"pip check package metadata is missing ({exc}): {line}")
        continue
    if owner_actual != match.group("owner_version") or dependency_actual != match.group("installed_version"):
        errors.append(f"pip check metadata version mismatch: {line}")
        continue
    owner_dist = metadata.distribution(owner)
    metadata_match = False
    for declared in owner_dist.requires or []:
        try:
            declared_requirement = Requirement(declared)
        except Exception:
            continue
        if canonicalize_name(declared_requirement.name) == dependency and declared_requirement.specifier == requirement.specifier:
            metadata_match = True
            break
    if not metadata_match:
        errors.append(f"active Requires-Dist metadata does not match: {line}")
        continue
    if owner in locked_versions:
        errors.append(f"allowlisted requiring distribution is present in the workload lock: {owner}")
        continue
    if locked_versions.get(dependency) != match.group("installed_version"):
        errors.append(f"dependency is not pinned to the observed version in the workload lock: {dependency}")
        continue
    base["conflicts"].append({
        "requiring_distribution": owner,
        "requiring_version": match.group("owner_version"),
        "requirement": requirement_text,
        "installed_distribution": dependency,
        "installed_version": match.group("installed_version"),
        "classification": "managed_base_allowlisted",
    })
if len(records) != len(set(records)):
    errors.append("duplicate pip check conflict record")
if errors:
    raise SystemExit(finish("FAIL", unexpected=errors))
raise SystemExit(finish("PASS_MANAGED_BASE_ALLOWLIST", conflicts=base["conflicts"]))
PY
  then
    classify_status=0
  else
    classify_status=$?
  fi
  if ! mv -f -- "$audit_tmp" "$audit"; then
    rm -f -- "$audit_tmp"
    return 1
  fi
  (( classify_status == 0 )) || return 1
}
validate_python_inventory() {
  validate_python_environment || return 1
  pip_check_with_managed_allowlist || return 1
  local current_freeze="$WORK_ROOT/artifacts/manifests/python-freeze.current.txt"
  "$VENV/bin/python" -m pip freeze --all > "$current_freeze" || {
    rm -f -- "$current_freeze"
    return 1
  }
  if ! cmp -s "$current_freeze" "$WORK_ROOT/artifacts/manifests/python-freeze.txt"; then
    rm -f -- "$current_freeze"
    return 1
  fi
  rm -f -- "$current_freeze"
}
clone_pinned() {
  local url="$1" rev="$2" dst="$3"
  if [[ -d "$dst/.git" ]]; then git -C "$dst" fetch --depth 1 origin "$rev"; else git clone --filter=blob:none "$url" "$dst"; fi
  git -C "$dst" checkout --detach "$rev"
  [[ "$(git -C "$dst" rev-parse HEAD)" == "$rev" ]]
}
repositories() { clone_pinned "$SWE_AGENT_URL" "$SWE_AGENT_REVISION" "$REPOS/SWE-agent"; clone_pinned "$SWE_BENCH_URL" "$SWE_BENCH_REVISION" "$REPOS/SWE-bench"; }
python_packages() {
  local reinstall=()
  [[ "$PYTHON_ENV_MODE" == managed ]] && reinstall+=(--force-reinstall)
  "$VENV/bin/pip" install --disable-pip-version-check --no-input "${reinstall[@]}" --only-binary=:all: --require-hashes -r "$PYTHON_LOCK" || return 1
  "$VENV/bin/pip" install --disable-pip-version-check --no-input --no-deps -e "$REPOS/SWE-agent" -e "$REPOS/SWE-bench" -e "$ROOT" || return 1
  pip_check_with_managed_allowlist || return 1
  "$VENV/bin/python" -m pip freeze --all > "$WORK_ROOT/artifacts/manifests/python-freeze.txt" || return 1
  "$VENV/bin/python" -c 'import agentic_sim, datasets, docker, numpy, pandas, requests, sweagent, swebench, urllib3; print("pinned workload packages import")' || return 1
}
runtime() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
  command -v tmux >/dev/null 2>&1
  docker pull "$VLLM_IMAGE" >/dev/null
  validate_runtime
  "$VENV/bin/sweagent" --help >/dev/null
  "$VENV/bin/python" -m swebench.harness.run_evaluation --help >/dev/null
}
validate_runtime() {
  local platform image_ref repo digest repo_digests
  image_ref="${VLLM_IMAGE%@*}"
  repo="${image_ref%%:*}"
  digest="${VLLM_IMAGE##*@}"
  repo_digests="$(docker image inspect "$VLLM_IMAGE" --format '{{join .RepoDigests "\n"}}')"
  grep -Fqx "$repo@$digest" <<<"$repo_digests" || { echo "vLLM image digest mismatch: expected $repo@$digest" >&2; return 1; }
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$VLLM_IMAGE")"
  [[ "$platform" == "$VLLM_IMAGE_PLATFORM" ]] || { echo "vLLM image platform mismatch: $platform" >&2; return 1; }
  "$VENV/bin/sweagent" --help >/dev/null
  "$VENV/bin/python" -m swebench.harness.run_evaluation --help >/dev/null
}
pull_evaluator_images() {
  local image digest pair
  for pair in \
    "${EVALUATOR_LITE_FIRST_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest}|${EVALUATOR_LITE_FIRST_DIGEST:-sha256:483f26c8c89a879560ed3f2e47e470343a5a0b8bf5e08d8fe3ec7eac9201df88}" \
    "${EVALUATOR_LITE_GOLD_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-14182:latest}|${EVALUATOR_LITE_GOLD_DIGEST:-sha256:1caa6363958e49791e9dc4c838fbfd8e8e134b7992e20e90def10072cb920c25}" \
    "${EVALUATOR_VERIFIED_GOLD_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-14365:latest}|${EVALUATOR_VERIFIED_GOLD_DIGEST:-sha256:ac22529003ab4df5a84eb0e6be4b269b691c0f3b4aca582161bdfb581e1e9305}"; do
    image="${pair%%|*}"; digest="${pair#*|}"
    [[ "$digest" =~ ^sha256:[0-9a-fA-F]{64}$ ]] || { echo "invalid evaluator digest: $digest" >&2; return 1; }
    docker pull "${image%%:*}@${digest}" >/dev/null
    docker tag "${image%%:*}@${digest}" "$image"
    docker image inspect "$image" --format '{{json .RepoDigests}}' | grep -Fq "$digest"
    [[ "$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")" == linux/amd64 ]]
  done
}
validate_evaluator_images() {
  local image digest pair repo_digests platform
  for pair in \
    "${EVALUATOR_LITE_FIRST_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest}|${EVALUATOR_LITE_FIRST_DIGEST:-sha256:483f26c8c89a879560ed3f2e47e470343a5a0b8bf5e08d8fe3ec7eac9201df88}" \
    "${EVALUATOR_LITE_GOLD_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-14182:latest}|${EVALUATOR_LITE_GOLD_DIGEST:-sha256:1caa6363958e49791e9dc4c838fbfd8e8e134b7992e20e90def10072cb920c25}" \
    "${EVALUATOR_VERIFIED_GOLD_IMAGE:-swebench/sweb.eval.x86_64.astropy_1776_astropy-14365:latest}|${EVALUATOR_VERIFIED_GOLD_DIGEST:-sha256:ac22529003ab4df5a84eb0e6be4b269b691c0f3b4aca582161bdfb581e1e9305}"; do
    image="${pair%%|*}"; digest="${pair#*|}"
    repo_digests="$(docker image inspect "$image" --format '{{join .RepoDigests "\n"}}')"
    grep -Fq "$digest" <<<"$repo_digests"
    platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image")"
    [[ "$platform" == linux/amd64 ]] || { echo "evaluator image platform mismatch: $image -> $platform" >&2; return 1; }
  done
}
validate_model_and_datasets() {
  local model_out="$WORK_ROOT/artifacts/manifests/model_download.json"
  local dataset_out="$WORK_ROOT/artifacts/manifests/datasets.json"
  "$VENV/bin/python" - "$model_out" "$dataset_out" <<'PY'
import json
import hashlib
import pathlib
import sys

model_path, dataset_path = map(pathlib.Path, sys.argv[1:])
model = json.loads(model_path.read_text(encoding="utf-8"))
if model.get("model") != "Qwen/Qwen3-Coder-30B-A3B-Instruct" or model.get("revision") != "b2cff646eb4bb1d68355c01b18ae02e7cf42d120":
    raise SystemExit("model download manifest does not match the frozen model revision")
snapshot = pathlib.Path(model.get("snapshot", ""))
if not snapshot.is_dir() or not (snapshot / "config.json").is_file() or not list(snapshot.glob("*.safetensors")):
    raise SystemExit("model snapshot is incomplete")
datasets = json.loads(dataset_path.read_text(encoding="utf-8"))
expected = {
    "lite": ("SWE-bench/SWE-bench_Lite", "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e", 300, "f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b", {
        "astropy__astropy-12907": "e117000983a3aabba8f43fb52e155d0cc6529b900ed476f59dc6cc065e970faa",
        "astropy__astropy-14182": "87118fdd9b83e959aa533ea57a70557e95a7027fbce92b14879a98468f5a263b",
    }),
    "verified": ("SWE-bench/SWE-bench_Verified", "91aa3ed51b709be6457e12d00300a6a596d4c6a3", 500, "43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21", {
        "astropy__astropy-14365": "4d0d91079bd056ff5d1940614ad71f025dd96f0498ceaf9ab71757efde87f5e3",
    }),
}
for name, (repo, revision, rows, expected_source_hash, expected_selected) in expected.items():
    section = datasets.get(name)
    if not isinstance(section, dict) or section.get("repo") != repo or section.get("revision") != revision or section.get("split") != "test" or section.get("rows") != rows:
        raise SystemExit(f"dataset manifest does not match the frozen {name} revision")
    source = pathlib.Path(section.get("source_file", ""))
    if source.suffix != ".parquet" or not source.is_file():
        raise SystemExit(f"dataset {name} source Parquet is missing")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if section.get("source_file_sha256") != expected_source_hash or digest != expected_source_hash:
        raise SystemExit(f"dataset {name} source Parquet hash does not match the measured pin")
    if section.get("provenance") != "measured" or section.get("reader") != "huggingface_hub+pyarrow.parquet":
        raise SystemExit(f"dataset {name} manifest lacks measured pinned-reader provenance")
    selected = {item.get("instance_id"): item for item in section.get("selected", [])}
    if set(selected) != set(expected_selected):
        raise SystemExit(f"dataset manifest selected IDs do not match the frozen {name} set")
    for instance_id, expected_hash in expected_selected.items():
        if selected[instance_id].get("sha256") != expected_hash:
            raise SystemExit(f"dataset manifest selected hash does not match the frozen {instance_id} row")
        selected_path = pathlib.Path(selected[instance_id].get("path", ""))
        if not selected_path.is_file():
            raise SystemExit(f"selected dataset file is missing for {instance_id}")
        values = json.loads(selected_path.read_text(encoding="utf-8"))
        if not isinstance(values, list) or len(values) != 1 or values[0].get("instance_id") != instance_id:
            raise SystemExit(f"selected dataset file is not the canonical one-row asset for {instance_id}")
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if hashlib.sha256(canonical).hexdigest() != expected_hash:
            raise SystemExit(f"selected dataset file hash does not match the measured {instance_id} row")
print("validated pinned model and dataset manifests")
PY
}
model_and_datasets() { (( SKIP_MODEL )) || "$ROOT/scripts/cloud/lambda_download_assets.sh" --manifest "$MANIFEST" --work-root "$WORK_ROOT"; }
local_tests() {
  PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m unittest discover -s "$ROOT/tests" -v
  PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m compileall -q "$ROOT/src" "$ROOT/scripts" "$ROOT/tests"
}

stage utilities 'for t in git curl jq rsync tmux tar zstd python3; do command -v "$t" >/dev/null || exit 1; done; if [[ "$PYTHON_ENV_MODE" == managed ]]; then test -x "$VENV/bin/python"; else python3 -m venv --help >/dev/null; fi' utilities
stage preflight 'python3 - "$WORK_ROOT/artifacts/manifests/lambda_preflight.json" <<"PY"
import json,sys
assert json.load(open(sys.argv[1], encoding="utf-8"))["status"] == "PASS"
PY' preflight
stage directories 'test -d "$REPOS" && test -d "$VENV" && test -d "$WORK_ROOT/datasets"' directories
stage python_environment 'validate_python_environment' python_environment
stage pinned_repositories 'test "$(git -C "$REPOS/SWE-agent" rev-parse HEAD)" = "$SWE_AGENT_REVISION" && test "$(git -C "$REPOS/SWE-bench" rev-parse HEAD)" = "$SWE_BENCH_REVISION"' repositories
stage python_packages 'validate_python_inventory && "$VENV/bin/python" -c "import agentic_sim, datasets, docker, numpy, pandas, requests, sweagent, swebench, urllib3" && test -s "$PYTHON_LOCK" && test -s "$WORK_ROOT/artifacts/manifests/python-freeze.txt" && test -s "$WORK_ROOT/artifacts/manifests/python-pip-check.json"' python_packages
stage runtime 'validate_runtime' runtime
stage evaluator_images 'validate_evaluator_images' pull_evaluator_images
stage model_and_datasets 'validate_model_and_datasets' model_and_datasets
stage tests 'PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m unittest discover -s "$ROOT/tests" >/dev/null' local_tests

if [[ -z "$PYTHON_VERSION_EXACT" && -x "$VENV/bin/python" ]]; then
  PYTHON_VERSION_EXACT="$("$VENV/bin/python" -c 'import platform; print(platform.python_version())')"
fi
python3 - "$WORK_ROOT/artifacts/manifests/bootstrap.json" "$VLLM_VERSION" "$VLLM_REVISION" "$VLLM_IMAGE" "$SWE_AGENT_REVISION" "$SWE_BENCH_REVISION" "$PYTHON_ENV_MODE" "$VENV" "$PYTHON_VERSION" "$PYTHON_VERSION_EXACT" "$PYTHON_LOCK" "$lock_digest" "$BOOTSTRAP_FINGERPRINT" <<'PY'
import json,pathlib,sys,time
out,*vals=sys.argv[1:]
obj={"schema_version":"lambda-bootstrap.v5","completed_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"vllm_version":vals[0],"vllm_revision":vals[1],"vllm_image":vals[2],"swe_agent_revision":vals[3],"swe_bench_revision":vals[4],"python_env_mode":vals[5],"python_env_root":vals[6],"python_version":vals[7],"python_version_exact":vals[8] or None,"python_lock_path":vals[9],"python_lock_sha256":vals[10],"bootstrap_fingerprint":vals[11],"work_root":str(pathlib.Path(out).parents[2])}
p=pathlib.Path(out); tmp=p.with_name(p.name+'.tmp'); tmp.write_text(json.dumps(obj,indent=2)+"\n"); tmp.replace(p)
PY
echo "Bootstrap complete. Next command: $ROOT/scripts/cloud/lambda_start_vllm.sh --manifest $MANIFEST"
