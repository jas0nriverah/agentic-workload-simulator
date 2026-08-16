#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"
OUT="${PREFLIGHT_OUTPUT:-$ROOT/../agentic-work/artifacts/manifests/lambda_preflight.json}"
PORT="${VLLM_PORT:-8000}"; MIN_FREE_GIB="${MIN_FREE_GIB:-120}"; DRY=0
usage() { echo "Usage: lambda_preflight.sh [--manifest FILE] [--output FILE] [--port PORT] [--min-free-gib N] [--dry-run]"; }
prev=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1;; --manifest|--output|--port|--min-free-gib) :;;
    --manifest=*) MANIFEST="${arg#*=}";; --output=*) OUT="${arg#*=}";;
    --port=*) PORT="${arg#*=}";; --min-free-gib=*) MIN_FREE_GIB="${arg#*=}";;
    -h|--help) usage; exit 0;;
    *) case "$prev" in --manifest) MANIFEST="$arg";; --output) OUT="$arg";; --port) PORT="$arg";; --min-free-gib) MIN_FREE_GIB="$arg";; *) echo "unknown argument: $arg" >&2; exit 2;; esac;;
  esac
  prev="$arg"
done
# Never source the env file: it may contain secrets or shell metacharacters.
if [[ -f "$MANIFEST" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; [[ "$line" == *=* ]] || continue; key="${line%%=*}"; value="${line#*=}"
    case "$key" in VLLM_PORT) PORT="$value";; MIN_FREE_GIB) MIN_FREE_GIB="$value";; esac
  done < "$MANIFEST"
fi
if (( DRY )); then
  cat <<EOF
DRY-RUN: read-only Lambda preflight (no installs, downloads, or service starts)
DRY-RUN: verify Ubuntu/kernel, Linux x86-64, sudo, nvidia-smi (one H100-class GPU, >=80 GiB), driver/CUDA, CPU/RAM, filesystem (>=${MIN_FREE_GIB} GiB free), Docker, git/curl/jq/rsync/tmux/tar/zstd, port ${PORT}, registries, UTC clock, and GPU process isolation
DRY-RUN: write JSON report only to $OUT
EOF
  exit 0
fi
failures=(); warnings=(); add_failure() { failures+=("$1"); }; add_warning() { warnings+=("$1"); }
os_release="unknown"; [[ -r /etc/os-release ]] && os_release="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release | sed 's/^"//;s/"$//' | head -n1)"
arch="$(uname -m 2>/dev/null || echo unknown)"; [[ "$arch" == x86_64 ]] || add_failure "unsupported architecture: $arch (expected x86_64)"
user_name="$(id -un 2>/dev/null || echo unknown)"; sudo_state="unavailable"; command -v sudo >/dev/null 2>&1 && sudo_state="available"; command -v sudo >/dev/null 2>&1 || add_warning "sudo is unavailable"
gpu="unknown"; gpu_mem=0; gpu_count=0; compute="unknown"; driver="unknown"; cuda="unknown"
if command -v nvidia-smi >/dev/null 2>&1; then
  gpu_count="$(nvidia-smi --query-gpu=count --format=csv,noheader 2>/dev/null | head -n1 | tr -cd '0-9' || true)"; [[ "$gpu_count" =~ ^[0-9]+$ ]] || gpu_count=0
  gpu="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1 | sed 's/[[:space:]]*$//' || true)"
  gpu_mem="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -cd '0-9' || true)"; [[ "$gpu_mem" =~ ^[0-9]+$ ]] || gpu_mem=0
  compute="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ' || true)"; [[ -n "$compute" ]] || compute=unknown
  driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 | tr -d ' ' || true)"; [[ -n "$driver" ]] || driver=unknown; cuda="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version:[[:space:]]*\([^[:space:]]*\).*/\1/p' | head -n1 || true)"; [[ -n "$cuda" ]] || cuda=unknown
else add_failure "nvidia-smi is unavailable"; fi
(( gpu_count == 1 )) || add_failure "expected exactly one isolated GPU; found $gpu_count"; (( gpu_mem >= 80000 )) || add_failure "GPU memory is ${gpu_mem} MiB; at least 80000 MiB is required"
if [[ "$compute" =~ ^[0-9]+\.[0-9]+$ ]]; then awk "BEGIN {exit !($compute >= 9.0)}" || add_failure "GPU compute capability $compute is below required H100-class 9.0"; else add_failure "GPU compute capability is unavailable"; fi
cpu_cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 0)"; [[ "$cpu_cores" =~ ^[0-9]+$ ]] || cpu_cores=0; memory_bytes="$(awk '/^MemTotal:/ {print $2 * 1024; exit}' /proc/meminfo 2>/dev/null || echo 0)"; [[ "$memory_bytes" =~ ^[0-9]+$ ]] || memory_bytes=0
free_kib="$(df -Pk "${WORK_ROOT:-$ROOT/..}" 2>/dev/null | awk 'NR==2 {print $4}' || true)"; [[ "$free_kib" =~ ^[0-9]+$ ]] || free_kib=0; min_kib=$(( MIN_FREE_GIB * 1024 * 1024 )); (( free_kib >= min_kib )) || add_failure "less than ${MIN_FREE_GIB} GiB free on work filesystem"; filesystem="$(df -P "${WORK_ROOT:-$ROOT/..}" 2>/dev/null | awk 'NR==2 {print $6}' || echo unknown)"
docker_state="unavailable"; if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then docker_state="available"; else add_failure "Docker daemon is unavailable"; fi
missing=(); for tool in git curl jq rsync tmux tar zstd; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done; ((${#missing[@]} == 0)) || add_failure "missing required utilities: ${missing[*]}"
if command -v ss >/dev/null 2>&1; then ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q . && add_failure "port $PORT is occupied"; elif command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && add_failure "port $PORT is occupied"; else add_warning "neither ss nor lsof is available for port check"; fi
network_state="PASS"; for endpoint in https://github.com https://huggingface.co https://pypi.org https://registry-1.docker.io; do if ! curl --connect-timeout 5 --max-time 10 -fsSI "$endpoint" >/dev/null 2>&1; then network_state="BLOCKED"; add_failure "endpoint unreachable: $endpoint"; fi; done
gpu_processes=""; if command -v nvidia-smi >/dev/null 2>&1; then gpu_processes="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' | tr '\n' ' ' || true)"; fi; [[ -z "$gpu_processes" ]] || add_failure "unexpected GPU processes: $gpu_processes"
status=PASS; ((${#failures[@]} == 0)) || status=BLOCKED; mkdir -p -- "$(dirname -- "$OUT")"
python3 - "$OUT" "$status" "$os_release" "$arch" "$user_name" "$sudo_state" "$gpu" "$gpu_mem" "$gpu_count" "$compute" "$driver" "$cuda" "$docker_state" "$network_state" "$PORT" "$filesystem" "$cpu_cores" "$memory_bytes" "${failures[*]-}" "${warnings[*]-}" <<'PY'
import json, pathlib, sys, time, platform
(out, status, os_release, arch, user, sudo, gpu, mem, count, compute, driver, cuda, docker, network, port, filesystem, cpu_cores, memory_bytes, failures, warnings) = sys.argv[1:]
obj={"schema_version":"lambda-preflight.v2","status":status,"checked_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"os_release":os_release,"kernel":platform.release(),"architecture":arch,"user":user,"sudo":sudo,"cpu_cores":int(cpu_cores),"memory_bytes":int(memory_bytes),"gpu":{"name":gpu,"memory_mib":int(mem),"count":int(count),"compute_capability":compute,"driver":driver,"cuda":cuda},"docker":docker,"network":network,"port":int(port),"filesystem":filesystem,"blocking_failures":[x for x in failures.split("\n") if x],"warnings":[x for x in warnings.split("\n") if x]}
tmp=pathlib.Path(out).with_name(pathlib.Path(out).name+".tmp"); tmp.write_text(json.dumps(obj,indent=2)+"\n",encoding="utf-8"); tmp.replace(out)
PY
echo "lambda preflight: $status (report $OUT)"; for item in "${failures[@]}"; do echo "blocking: $item" >&2; done; for item in "${warnings[@]}"; do echo "warning: $item" >&2; done; [[ "$status" == PASS ]]
