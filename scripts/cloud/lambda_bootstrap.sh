#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"
WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"
LOG_DIR="${WORK_ROOT}/logs/bootstrap"
STATE="${WORK_ROOT}/state/bootstrap"
REPOS="${WORK_ROOT}/repos"
VENV="${WORK_ROOT}/venv"
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

for key in WORK_ROOT SWE_AGENT_REVISION SWE_BENCH_REVISION VLLM_REVISION VLLM_VERSION VLLM_IMAGE VLLM_IMAGE_PLATFORM PYTHON_LOCK_SHA256 EVALUATOR_LITE_FIRST_IMAGE EVALUATOR_LITE_FIRST_DIGEST EVALUATOR_LITE_GOLD_IMAGE EVALUATOR_LITE_GOLD_DIGEST EVALUATOR_VERIFIED_GOLD_IMAGE EVALUATOR_VERIFIED_GOLD_DIGEST; do
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
    PYTHON_LOCK_SHA256) PYTHON_LOCK_SHA256="$value";;
    EVALUATOR_LITE_FIRST_IMAGE) EVALUATOR_LITE_FIRST_IMAGE="$value";;
    EVALUATOR_LITE_FIRST_DIGEST) EVALUATOR_LITE_FIRST_DIGEST="$value";;
    EVALUATOR_LITE_GOLD_IMAGE) EVALUATOR_LITE_GOLD_IMAGE="$value";;
    EVALUATOR_LITE_GOLD_DIGEST) EVALUATOR_LITE_GOLD_DIGEST="$value";;
    EVALUATOR_VERIFIED_GOLD_IMAGE) EVALUATOR_VERIFIED_GOLD_IMAGE="$value";;
    EVALUATOR_VERIFIED_GOLD_DIGEST) EVALUATOR_VERIFIED_GOLD_DIGEST="$value";;
  esac
done
LOG_DIR="${LOG_DIR:-$WORK_ROOT/logs/bootstrap}"
STATE="$WORK_ROOT/state/bootstrap"; REPOS="$WORK_ROOT/repos"; VENV="$WORK_ROOT/venv"

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

if (( DRY )); then
  cat <<EOF
DRY-RUN: no mutation, downloads, installs, containers, or paid calls.
DRY-RUN: preflight Linux x86-64/H100/Docker/disk/network, install missing utilities, create $VENV and caches.
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
  if (( RESUME )) && [[ -f "$marker" ]] && eval "$validator"; then
    echo "stage $name already validated"
    return 0
  fi
  rm -f -- "$marker"
  start="$(date -u +%FT%TZ)"; echo "stage $name start $start"
  set +e; "$@"; rc=$?; set -e
  end="$(date -u +%FT%TZ)"; echo "stage $name end $end exit=$rc"
  (( rc == 0 )) || return "$rc"
  eval "$validator"
  printf 'validated_at_utc=%s\n' "$end" > "$marker"
}

preflight() { "$ROOT/scripts/cloud/lambda_preflight.sh" --manifest "$MANIFEST" --output "$WORK_ROOT/artifacts/manifests/lambda_preflight.json"; }
utilities() {
  local missing=() tool
  for tool in git curl jq rsync tmux tar zstd python3; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  python3 -m venv --help >/dev/null 2>&1 || missing+=(python3-venv)
  ((${#missing[@]} == 0)) && return 0
  command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1 || { echo "missing utilities: ${missing[*]}" >&2; return 1; }
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}
directories() { mkdir -p -- "$REPOS" "$VENV" "$WORK_ROOT/cache/huggingface" "$WORK_ROOT/cache/pip" "$WORK_ROOT/cache/uv" "$WORK_ROOT/artifacts/manifests" "$WORK_ROOT/datasets"; }
python_environment() {
  [[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --only-binary=:all: 'pip==24.3.1' 'setuptools==75.6.0' 'wheel==0.45.1'
}
clone_pinned() {
  local url="$1" rev="$2" dst="$3"
  if [[ -d "$dst/.git" ]]; then git -C "$dst" fetch --depth 1 origin "$rev"; else git clone --filter=blob:none "$url" "$dst"; fi
  git -C "$dst" checkout --detach "$rev"
  [[ "$(git -C "$dst" rev-parse HEAD)" == "$rev" ]]
}
repositories() { clone_pinned "$SWE_AGENT_URL" "$SWE_AGENT_REVISION" "$REPOS/SWE-agent"; clone_pinned "$SWE_BENCH_URL" "$SWE_BENCH_REVISION" "$REPOS/SWE-bench"; }
python_packages() {
  "$VENV/bin/pip" install --disable-pip-version-check --no-input --require-hashes -r "$PYTHON_LOCK"
  "$VENV/bin/pip" install --disable-pip-version-check --no-input --no-deps -e "$REPOS/SWE-agent" -e "$REPOS/SWE-bench" -e "$ROOT"
  "$VENV/bin/python" -c 'import sweagent, swebench, agentic_sim; print("pinned Python packages import")'
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
    "lite": ("SWE-bench/SWE-bench_Lite", "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e", 300, {"astropy__astropy-12907", "astropy__astropy-14182"}),
    "verified": ("SWE-bench/SWE-bench_Verified", "91aa3ed51b709be6457e12d00300a6a596d4c6a3", 500, {"astropy__astropy-14365"}),
}
for name, (repo, revision, rows, ids) in expected.items():
    section = datasets.get(name)
    if not isinstance(section, dict) or section.get("repo") != repo or section.get("revision") != revision or section.get("split") != "test" or section.get("rows") != rows:
        raise SystemExit(f"dataset manifest does not match the frozen {name} revision")
    selected = {item.get("instance_id") for item in section.get("selected", [])}
    if selected != ids:
        raise SystemExit(f"dataset manifest selected IDs do not match the frozen {name} set")
print("validated pinned model and dataset manifests")
PY
}
model_and_datasets() { (( SKIP_MODEL )) || "$ROOT/scripts/cloud/lambda_download_assets.sh" --manifest "$MANIFEST" --work-root "$WORK_ROOT"; }
local_tests() {
  PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m unittest discover -s "$ROOT/tests" -v
  PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m compileall -q "$ROOT/src" "$ROOT/scripts" "$ROOT/tests"
}

stage preflight 'python3 - "$WORK_ROOT/artifacts/manifests/lambda_preflight.json" <<"PY"
import json,sys
assert json.load(open(sys.argv[1], encoding="utf-8"))["status"] == "PASS"
PY' preflight
stage utilities 'for t in git curl jq rsync tmux tar zstd python3; do command -v "$t" >/dev/null || exit 1; done; python3 -m venv --help >/dev/null' utilities
stage directories 'test -d "$REPOS" && test -d "$VENV" && test -d "$WORK_ROOT/datasets"' directories
stage python_environment 'test -x "$VENV/bin/python" && "$VENV/bin/python" -c "import pip"' python_environment
stage pinned_repositories 'test "$(git -C "$REPOS/SWE-agent" rev-parse HEAD)" = "$SWE_AGENT_REVISION" && test "$(git -C "$REPOS/SWE-bench" rev-parse HEAD)" = "$SWE_BENCH_REVISION"' repositories
stage python_packages '"$VENV/bin/python" -c "import sweagent, swebench, agentic_sim" && test -s "$PYTHON_LOCK"' python_packages
stage runtime 'validate_runtime' runtime
stage evaluator_images 'validate_evaluator_images' pull_evaluator_images
stage model_and_datasets 'validate_model_and_datasets' model_and_datasets
stage tests 'PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m unittest discover -s "$ROOT/tests" >/dev/null' local_tests

python3 - "$WORK_ROOT/artifacts/manifests/bootstrap.json" "$VLLM_VERSION" "$VLLM_REVISION" "$VLLM_IMAGE" "$SWE_AGENT_REVISION" "$SWE_BENCH_REVISION" <<'PY'
import json,pathlib,sys,time
out,*vals=sys.argv[1:]
obj={"schema_version":"lambda-bootstrap.v3","completed_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"vllm_version":vals[0],"vllm_revision":vals[1],"vllm_image":vals[2],"swe_agent_revision":vals[3],"swe_bench_revision":vals[4],"work_root":str(pathlib.Path(out).parents[2])}
p=pathlib.Path(out); tmp=p.with_name(p.name+'.tmp'); tmp.write_text(json.dumps(obj,indent=2)+"\n"); tmp.replace(p)
PY
echo "Bootstrap complete. Next command: $ROOT/scripts/cloud/lambda_start_vllm.sh --manifest $MANIFEST"
