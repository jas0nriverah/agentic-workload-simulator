#!/usr/bin/env bash
# shellcheck disable=SC2015
set -Eeuo pipefail

# Read-only setup gate for the sealed H100 experiment. This script never
# starts Docker, vLLM, Nsight, a trace provider, or a cloud allocation.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="$ROOT/configs/h100_final_validation.json"
MANIFEST="$ROOT/cloud/lambda/instance_manifest.env"
RUNNER="$ROOT/scripts/cloud/h100_case_runner.py"
OFFLINE=0
CHECK_SERVER=0
REQUIRED_BRANCH="parallel-h100-shards"
FAILURES=0

usage() {
  cat <<'EOF'
Usage: h100_setup_doctor.sh [options]

Read-only validation of the sealed H100 runtime contract.

Options:
  --config FILE       Sealed protocol JSON (default: configs/h100_final_validation.json)
  --manifest FILE     Untracked vLLM instance manifest
  --runner FILE       Reviewed case runner
  --offline           Skip host, Docker, model, provider, and server checks
  --check-server      Also check the already-running vLLM health endpoints
  -h, --help          Show this help

The normal VM command is:
  scripts/cloud/h100_setup_doctor.sh --manifest cloud/lambda/instance_manifest.env

After the pinned server is started, add --check-server. This command never
starts a service and never allocates cloud resources.
EOF
}

pass() { printf 'PASS  %s\n' "$1"; }
warn() { printf 'WARN  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAILURES=$((FAILURES + 1)); }

manifest_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted { print substr($0, index($0, "=") + 1); exit }' "$MANIFEST"
}

while (($#)); do
  case "$1" in
    --config) (($# >= 2)) || { echo '--config requires a path' >&2; exit 2; }; CONFIG="$2"; shift 2;;
    --manifest) (($# >= 2)) || { echo '--manifest requires a path' >&2; exit 2; }; MANIFEST="$2"; shift 2;;
    --runner) (($# >= 2)) || { echo '--runner requires a path' >&2; exit 2; }; RUNNER="$2"; shift 2;;
    --offline) OFFLINE=1; shift;;
    --check-server) CHECK_SERVER=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

cd "$ROOT"
printf 'H100 setup doctor\n'
printf 'repository: %s\n' "$ROOT"
if [[ "$OFFLINE" -eq 1 ]]; then
  printf 'mode: offline\n'
else
  printf 'mode: VM-preflight%s\n' "$([[ "$CHECK_SERVER" -eq 1 ]] && echo ' + server-check' || true)"
fi

if command -v git >/dev/null 2>&1; then
  branch="$(git branch --show-current 2>/dev/null || true)"
  [[ "$branch" == "$REQUIRED_BRANCH" ]] && pass "branch $branch" || fail "required branch $REQUIRED_BRANCH (found ${branch:-detached})"
  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    [[ "$OFFLINE" -eq 1 ]] && warn 'working tree is dirty (offline mode; execution remains blocked)' || fail 'working tree is not clean'
  else
    pass 'working tree clean'
  fi
else
  fail 'git is unavailable'
fi

for required in "$CONFIG" "$RUNNER" "$ROOT/scripts/cloud/run_h100_final_validation.sh" "$ROOT/scripts/cloud/lambda_start_vllm.sh" "$ROOT/scripts/cloud/lambda_healthcheck.sh"; do
  [[ -f "$required" ]] && pass "file present: ${required#"$ROOT"/}" || fail "missing file: ${required#"$ROOT"/}"
done
[[ -x "$RUNNER" ]] && pass 'reviewed runner is executable' || fail "runner is not executable: ${RUNNER#"$ROOT"/}"

if command -v python3 >/dev/null 2>&1 && [[ -f "$CONFIG" ]]; then
  if python3 - "$CONFIG" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
assert data["status"] == "sealed_scaffold_not_launched"
assert data["launch_authorized"] is False
assert data["hardware"]["required_compute_capability"] == "9.0"
assert data["request_protocol"]["concurrency"] == 1
assert data["request_protocol"]["warmup_requests"] == 2
assert data["request_protocol"]["measured_repetitions_per_case"] == 3
assert len(data["calibration_configs"]) == 24
assert len(data["sealed_holdouts"]) == 12
assert data["leakage_boundaries"]["seal_before_run"] is True
assert data["fit_specification"]["prediction_artifact_required_before_holdout_join"] is True
PY
  then
    pass 'sealed protocol invariants'
  else
    fail 'sealed protocol invariants'
  fi
else
  fail 'python3 is required to validate the sealed protocol'
fi

if [[ "$OFFLINE" -eq 1 ]]; then
  if ((FAILURES)); then
    printf 'NOT_READY: %d offline checks failed\n' "$FAILURES"
    exit 1
  fi
  echo 'READY_FOR_VM_PREFLIGHT: offline checks passed; no runtime or GPU checks performed'
  exit 0
fi

if [[ ! -f "$MANIFEST" ]]; then
  fail "missing untracked instance manifest: ${MANIFEST#"$ROOT"/}"
else
  pass 'instance manifest present (values not printed)'
  expected_revision="$(python3 - "$CONFIG" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["frozen_software"]["model_revision"])
PY
)"
  manifest_revision="$(manifest_value VLLM_MODEL_REVISION)"
  [[ "$manifest_revision" == "$expected_revision" ]] && pass 'manifest model revision matches sealed protocol' || fail 'manifest model revision mismatch'
  manifest_image="$(manifest_value VLLM_IMAGE)"
  expected_image="$(python3 - "$CONFIG" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["frozen_software"]["vllm_image"])
PY
)"
  [[ "$manifest_image" == "$expected_image" ]] && pass 'manifest vLLM image matches sealed protocol' || fail 'manifest vLLM image mismatch'
fi

for command_name in curl docker nvidia-smi; do
  command -v "$command_name" >/dev/null 2>&1 && pass "command available: $command_name" || fail "command unavailable: $command_name"
done

if command -v nvidia-smi >/dev/null 2>&1; then
  gpu_lines="$(nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits 2>/dev/null || true)"
  gpu_count="$(printf '%s\n' "$gpu_lines" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
  [[ "$gpu_count" == 1 ]] && pass 'exactly one visible GPU' || fail "expected one visible GPU (found $gpu_count)"
  if [[ "$gpu_count" == 1 ]]; then
    gpu_name="$(printf '%s\n' "$gpu_lines" | cut -d, -f1 | sed 's/[[:space:]]*$//')"
    gpu_memory="$(printf '%s\n' "$gpu_lines" | cut -d, -f2 | tr -d ' ')"
    gpu_compute="$(printf '%s\n' "$gpu_lines" | cut -d, -f3 | tr -d ' ')"
    [[ "$gpu_name" == *H100* ]] && pass "allowlisted H100 GPU: $gpu_name" || fail "GPU is not H100: $gpu_name"
    [[ "$gpu_memory" =~ ^[0-9]+$ && "$gpu_memory" -ge 80000 ]] && pass 'GPU memory is at least 80000 MiB' || fail "GPU memory below 80000 MiB: $gpu_memory"
    [[ "$gpu_compute" == 9.0 ]] && pass 'GPU compute capability 9.0' || fail "GPU compute capability is $gpu_compute"
  fi
  process_count="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
  if [[ "$CHECK_SERVER" -eq 0 && "$process_count" == 0 ]]; then
    pass 'no concurrent GPU compute process'
  elif [[ "$CHECK_SERVER" -eq 0 ]]; then
    fail "concurrent GPU compute process detected ($process_count)"
  else
    warn "GPU compute process count is $process_count (server-check mode)"
  fi
fi

if command -v docker >/dev/null 2>&1; then
  docker info >/dev/null 2>&1 && pass 'Docker daemon reachable' || fail 'Docker daemon unavailable'
  if [[ -f "$MANIFEST" ]]; then
    image="$(manifest_value VLLM_IMAGE)"
    if [[ -n "$image" ]] && docker image inspect "$image" >/dev/null 2>&1; then
      pass 'pinned vLLM image present locally'
    else
      fail 'pinned vLLM image is missing or not inspectable'
    fi
  fi
fi

snapshot="${H100_MODEL_SNAPSHOT:-}"
provider="${H100_TRACE_PROVIDER:-}"
if [[ -d "$snapshot" ]]; then
  expected_revision="$(python3 - "$CONFIG" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["frozen_software"]["model_revision"])
PY
)"
  [[ "$(basename "$snapshot")" == "$expected_revision" ]] && pass 'model snapshot path matches sealed revision' || fail 'model snapshot basename does not match sealed revision'
else
  fail 'H100_MODEL_SNAPSHOT is unset or not a directory'
fi
if [[ -x "$provider" ]]; then
  [[ "$provider" != *"/tests/fixtures/"* ]] && pass 'production trace provider is executable' || fail 'test fixture cannot be used as production trace provider'
else
  fail 'H100_TRACE_PROVIDER is unset or not executable'
fi
[[ "${H100_RUNNER_TEST_MODE:-}" != 1 ]] && pass 'runner test mode disabled' || fail 'H100_RUNNER_TEST_MODE=1 is forbidden for production'

if [[ "$CHECK_SERVER" -eq 1 ]]; then
  base_url="${H100_VLLM_BASE_URL:-http://127.0.0.1:8000}"
  for endpoint in /health /v1/models /metrics; do
    curl -fsS --max-time 10 "$base_url$endpoint" >/dev/null && pass "vLLM endpoint reachable: $endpoint" || fail "vLLM endpoint unavailable: $endpoint"
  done
fi

if ((FAILURES)); then
  printf 'NOT_READY: %d checks failed\n' "$FAILURES"
  exit 1
fi
echo 'READY_FOR_PREFLIGHT: all requested checks passed; calibration has not started'
