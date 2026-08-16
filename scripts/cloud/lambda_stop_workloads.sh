#!/usr/bin/env bash
# Stop only this project's recorded workloads. Never terminates a Lambda VM.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
WORK_ROOT=${WORK_ROOT:-"$PROJECT_ROOT/work"}
SERVER_MANIFEST=${SERVER_MANIFEST:-"$WORK_ROOT/artifacts/manifests/vllm_server.json"}
SESSION=${VLLM_TMUX_SESSION:-vllm-agentic}
GPU_LOCK_DIR=${VLLM_GPU_LOCK_DIR:-"$WORK_ROOT/locks/gpu-0.lock"}
DRY_RUN=0

usage() { echo "Usage: lambda_stop_workloads.sh [--work-root DIR] [--server-manifest FILE] [--session NAME] [--gpu-lock-dir DIR] [--dry-run]"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --work-root) [[ $# -ge 2 ]] || die '--work-root requires a path'; WORK_ROOT=$2; shift 2 ;;
    --server-manifest) [[ $# -ge 2 ]] || die '--server-manifest requires a path'; SERVER_MANIFEST=$2; shift 2 ;;
    --session) [[ $# -ge 2 ]] || die '--session requires a name'; SESSION=$2; shift 2 ;;
    --gpu-lock-dir) [[ $# -ge 2 ]] || die '--gpu-lock-dir requires a path'; GPU_LOCK_DIR=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if (( DRY_RUN )); then
  printf 'DRY-RUN: stop recorded project PIDs under %s and tmux session %s\n' "$WORK_ROOT" "$SESSION"
  printf 'DRY-RUN: release the project GPU lease at %s\n' "$GPU_LOCK_DIR"
  printf 'DRY-RUN: Lambda instance remains running; terminate it separately in the provider console.\n'
  exit 0
fi

mkdir -p -- "$WORK_ROOT/artifacts/manifests"
stopped=()
recorded_server_pid=''
stop_pid_file() {
  local pid_file=$1 pid command
  [[ -f "$pid_file" ]] || return 0
  pid=$(tr -d '[:space:]' <"$pid_file")
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    command=$(ps -p "$pid" -o command= 2>/dev/null || true)
    case "$command" in
      *"$PROJECT_ROOT"*|*"$WORK_ROOT"*|*vllm*|*sweagent*|*swe-agent*)
        kill "$pid" 2>/dev/null || true
        stopped+=("$pid")
        ;;
      *) printf 'refusing unrelated PID %s: %s\n' "$pid" "$command" >&2 ;;
    esac
  fi
}

if [[ -f "$SERVER_MANIFEST" ]]; then
  pid=$(python3 - "$SERVER_MANIFEST" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("pid", ""))
except (OSError, ValueError):
    print("")
PY
)
  recorded_server_pid=$pid
  [[ "$pid" =~ ^[0-9]+$ ]] && printf '%s\n' "$pid" >"$WORK_ROOT/.vllm.pid"
fi

for pid_file in "$WORK_ROOT"/.vllm.pid "$WORK_ROOT"/.sweagent.pid "$WORK_ROOT"/.evaluator.pid; do
  stop_pid_file "$pid_file"
done

if command -v tmux >/dev/null 2>&1 && tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux kill-session -t "$SESSION"
  stopped+=("tmux:$SESSION")
fi

if [[ "$recorded_server_pid" =~ ^[0-9]+$ ]]; then
  for _ in {1..10}; do
    kill -0 "$recorded_server_pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$recorded_server_pid" 2>/dev/null; then
    printf 'refusing to release GPU lease while recorded server PID %s is still alive\n' "$recorded_server_pid" >&2
    exit 1
  fi
fi

if [[ -z "$recorded_server_pid" && -f "$GPU_LOCK_DIR/pid" ]]; then
  lease_pid=$(tr -d '[:space:]' <"$GPU_LOCK_DIR/pid")
  if [[ "$lease_pid" =~ ^[0-9]+$ ]] && kill -0 "$lease_pid" 2>/dev/null; then
    printf 'refusing to release GPU lease while lease PID %s is still alive\n' "$lease_pid" >&2
    exit 1
  fi
fi

if [[ -d "$GPU_LOCK_DIR" ]]; then
  rm -f -- "$GPU_LOCK_DIR/pid" "$GPU_LOCK_DIR/owner.json"
  rmdir -- "$GPU_LOCK_DIR" 2>/dev/null || printf 'GPU lease directory is not empty; inspect: %s\n' "$GPU_LOCK_DIR" >&2
  stopped+=("gpu-lock:$GPU_LOCK_DIR")
fi

receipt="$WORK_ROOT/artifacts/manifests/workload_stop_receipt.json"
STOPPED_JSON=$(printf '%s\n' "${stopped[@]:-}" | python3 -c 'import json,sys; print(json.dumps([x for x in sys.stdin.read().splitlines() if x]))')
python3 - "$receipt" "$STOPPED_JSON" <<'PY'
import json, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "schema_version": "lambda-stop.v1",
    "stopped_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "stopped": json.loads(sys.argv[2]),
    "provider_instance_terminated": False,
    "termination_note": "Stop workloads does not stop Lambda billing; terminate in the provider console.",
}, indent=2) + "\n", encoding="utf-8")
PY
printf 'Stopped project workloads. Receipt: %s\n' "$receipt"
printf 'Lambda billing continues until the instance is terminated in the provider console.\n'
