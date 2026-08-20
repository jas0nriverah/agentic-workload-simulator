#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"; MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"; WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"; CACHE_ROOT="${CACHE_ROOT:-}"; MODEL="${VLLM_MODEL:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"; REVISION="${VLLM_MODEL_REVISION:-b2cff646eb4bb1d68355c01b18ae02e7cf42d120}"; VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271}"; PORT="${VLLM_PORT:-8000}"; SESSION="${VLLM_TMUX_SESSION:-vllm-agentic}"; LOCK_DIR="${VLLM_GPU_LOCK_DIR:-$WORK_ROOT/locks/gpu-0.lock}"; TASK_ID="${TASK_ID:-vllm-server}"; EXPERIMENT_ID="${EXPERIMENT_ID:-}"; MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"; GPU_MEM_UTIL="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"; DRY=0
usage(){ echo 'Usage: lambda_start_vllm.sh [--manifest FILE] [--config FILE] [--work-root DIR] [--dry-run]'; }; prev=""
for arg in "$@"; do case "$arg" in --dry-run) DRY=1;; --manifest|--config|--work-root) :;; --manifest=*) MANIFEST="${arg#*=}";; --config=*) MANIFEST="${arg#*=}";; --work-root=*) WORK_ROOT="${arg#*=}";; -h|--help) usage; exit 0;; *) case "$prev" in --manifest|--config) MANIFEST="$arg";; --work-root) WORK_ROOT="$arg";; *) echo "unknown argument: $arg" >&2; exit 2;; esac;; esac; prev="$arg"; done
if [[ -f "$MANIFEST" ]]; then while IFS= read -r line || [[ -n "$line" ]]; do line="${line%%#*}"; [[ "$line" == *=* ]] || continue; key="${line%%=*}"; value="${line#*=}"; case "$key" in WORK_ROOT) WORK_ROOT="$value";; CACHE_ROOT) CACHE_ROOT="$value";; VLLM_MODEL) MODEL="$value";; VLLM_MODEL_REVISION) REVISION="$value";; VLLM_IMAGE) VLLM_IMAGE="$value";; VLLM_PORT) PORT="$value";; VLLM_TMUX_SESSION) SESSION="$value";; VLLM_GPU_LOCK_DIR) LOCK_DIR="$value";; TASK_ID) TASK_ID="$value";; EXPERIMENT_ID) EXPERIMENT_ID="$value";; esac; done < "$MANIFEST"; fi
if (( ! DRY )) && [[ ! -f "$MANIFEST" ]]; then echo "instance manifest is required for a non-dry vLLM launch: $MANIFEST" >&2; exit 1; fi
resolved_json="$(PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -m agentic_sim.runtime.vllm_config --manifest "$MANIFEST" --format json)" || { echo 'invalid vLLM instance manifest' >&2; exit 1; }
resolved_value() { PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1" <<<"$resolved_json"; }
MODEL="$(resolved_value model)"; REVISION="$(resolved_value model_revision)"; VLLM_IMAGE="$(resolved_value image)"; PORT="$(resolved_value port)"; MAX_MODEL_LEN="$(resolved_value max_model_len)"; HEALTH_CONTEXT="$(resolved_value health_context)"; GPU_MEM_UTIL="$(resolved_value gpu_memory_utilization)"; TENSOR_PARALLEL_SIZE="$(resolved_value tensor_parallel_size)"; PARSER="$(resolved_value parser)"
LOG_DIR="$WORK_ROOT/logs/vllm"; SERVER_MANIFEST="$WORK_ROOT/artifacts/manifests/vllm_server.json"; SERVED_NAME="$MODEL"
HF_CACHE="${HF_HUB_CACHE:-${CACHE_ROOT:-$WORK_ROOT/cache}/huggingface}"; cmd=(docker run --rm --name "$SESSION" --gpus device=0 --network host --ipc=host -e HF_HOME=/root/.cache/huggingface -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -v "$HF_CACHE:/root/.cache/huggingface" --pull=never "$VLLM_IMAGE" --model "$MODEL" --revision "$REVISION" --served-model-name "$SERVED_NAME" --host 127.0.0.1 --port "$PORT" --dtype bfloat16 --max-model-len "$MAX_MODEL_LEN" --gpu-memory-utilization "$GPU_MEM_UTIL" --tensor-parallel-size "$TENSOR_PARALLEL_SIZE" --enable-auto-tool-choice --tool-call-parser "$PARSER")
if (( DRY )); then
  printf 'DRY-RUN: atomically acquire GPU-0 lease %s; start pinned container %s in disconnect-safe tmux session %s:' "$LOCK_DIR" "$VLLM_IMAGE" "$SESSION"; printf ' %q' "${cmd[@]}"; echo
  echo "DRY-RUN: record resolved config, model revision, parser=$PARSER, /metrics endpoint, port=$PORT, and log=$LOG_DIR/server.log; refuse conflicting port/GPU lease."
  exit 0
fi
command -v docker >/dev/null 2>&1 || { echo 'docker is unavailable' >&2; exit 1; }; command -v tmux >/dev/null 2>&1 || { echo 'tmux is required' >&2; exit 1; }; command -v python3 >/dev/null 2>&1 || { echo 'python3 is required' >&2; exit 1; }; docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 || { echo "pinned vLLM image unavailable: $VLLM_IMAGE" >&2; exit 1; }
image_ref="${VLLM_IMAGE%@*}"; image_repo="${image_ref%%:*}"; image_digest="${VLLM_IMAGE##*@}"
repo_digests="$(docker image inspect "$VLLM_IMAGE" --format '{{join .RepoDigests "\n"}}' 2>/dev/null || true)"
grep -Fqx "$image_repo@$image_digest" <<<"$repo_digests" || { echo "pinned vLLM image digest mismatch: expected $image_repo@$image_digest" >&2; exit 1; }
image_platform="$(docker image inspect "$VLLM_IMAGE" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true)"
[[ "$image_platform" == "linux/amd64" ]] || { echo "pinned vLLM image platform mismatch: $image_platform" >&2; exit 1; }
mkdir -p -- "$LOG_DIR" "$WORK_ROOT/artifacts/manifests" "$(dirname -- "$LOCK_DIR")"
port_in_use(){ if command -v ss >/dev/null 2>&1; then ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .; elif command -v lsof >/dev/null 2>&1; then lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; else return 1; fi; }
gpu_in_use(){ command -v nvidia-smi >/dev/null 2>&1 && [[ -n "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d')" ]]; }
if port_in_use; then echo "port $PORT is occupied" >&2; exit 1; fi
if tmux has-session -t "$SESSION" 2>/dev/null; then echo "tmux session already exists: $SESSION" >&2; exit 1; fi
if ! mkdir -- "$LOCK_DIR" 2>/dev/null; then
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$owner" =~ ^[0-9]+$ && -d "/proc/$owner" ]]; then echo "GPU 0 lease is active (pid $owner)" >&2; exit 1; fi
  if port_in_use || gpu_in_use; then echo "stale lease has active port/GPU process; inspect before clearing: $LOCK_DIR" >&2; exit 1; fi
  rm -f -- "$LOCK_DIR/pid" "$LOCK_DIR/owner.json"; rmdir -- "$LOCK_DIR" 2>/dev/null || { echo "cannot clear stale lease: $LOCK_DIR" >&2; exit 1; }; mkdir -- "$LOCK_DIR"
fi
lease_live=1; cleanup(){ if (( lease_live )); then rm -f -- "$LOCK_DIR/pid" "$LOCK_DIR/owner.json"; rmdir -- "$LOCK_DIR" 2>/dev/null || true; fi; }; trap cleanup EXIT
printf '%s\n' "$$" > "$LOCK_DIR/pid"
hash="$(printf '%s\0' "${cmd[@]}" | sha256sum | awk '{print $1}')"; printf -v quoted ' %q' "${cmd[@]}"; command_line="${quoted# }"
tmux new-session -d -s "$SESSION" "$command_line >>$(printf %q "$LOG_DIR/server.log") 2>&1"; sleep 1
pid="$(docker inspect --format '{{.State.Pid}}' "$SESSION" 2>/dev/null || true)"; [[ "$pid" =~ ^[0-9]+$ && "$pid" != 0 ]] || { echo 'could not determine vLLM container PID' >&2; exit 1; }; printf '%s\n' "$pid" > "$LOCK_DIR/pid"
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 - "$SERVER_MANIFEST" "$pid" "$MODEL" "$REVISION" "$VLLM_IMAGE" "$PORT" "$SESSION" "$LOG_DIR/server.log" "$LOCK_DIR" "$TASK_ID" "$EXPERIMENT_ID" "$hash" "$MAX_MODEL_LEN" "$HEALTH_CONTEXT" "$GPU_MEM_UTIL" "$TENSOR_PARALLEL_SIZE" "$PARSER" <<'PY'
import json,pathlib,sys,time
from agentic_sim.telemetry.clock import clock_metadata
out,pid,model,revision,image,port,session,log,lock,task,experiment,config_hash,max_model_len,health_context,gpu_memory_utilization,tensor_parallel_size,parser=sys.argv[1:]
clock = clock_metadata()
obj={"schema_version":"vllm-server.v3","pid":int(pid),"host":"127.0.0.1","port":int(port),"model":model,"model_revision":revision,"vllm_image":image,"dtype":"bfloat16","max_model_len":int(max_model_len),"health_context":int(health_context),"gpu_memory_utilization":float(gpu_memory_utilization),"tensor_parallel_size":int(tensor_parallel_size),"cuda_visible_devices":"0","tool_parser":parser,"metrics_endpoint":"/metrics","native_metrics_scope":"server_aggregate","native_request_correlation":False,"engine_selection":"auto_unresolved_until_runtime","otel_status":"deferred_not_enabled","tmux_session":session,"log_path":log,"gpu_lock":lock,"task_id":task,"experiment_id":experiment,"hostname":clock["hostname"],"clock":clock,"config_hash":config_hash,"started_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())}
p=pathlib.Path(out); tmp=p.with_name(p.name+'.tmp'); tmp.write_text(json.dumps(obj,indent=2)+"\n"); tmp.replace(p)
PY
python3 - "$LOCK_DIR/owner.json" "$pid" "$MODEL" "$REVISION" "$PORT" "$SESSION" "$TASK_ID" "$EXPERIMENT_ID" "$hash" "$MAX_MODEL_LEN" "$GPU_MEM_UTIL" "$PARSER" "$TENSOR_PARALLEL_SIZE" <<'PY'
import json,pathlib,sys,time,socket
out,pid,model,revision,port,session,task,experiment,h,max_model_len,gpu_memory_utilization,parser,tensor_parallel_size=sys.argv[1:]; pathlib.Path(out).write_text(json.dumps({"schema_version":"gpu-lease.v3","pid":int(pid),"gpu":"0","vllm_port":int(port),"model":model,"model_revision":revision,"max_model_len":int(max_model_len),"gpu_memory_utilization":float(gpu_memory_utilization),"tool_parser":parser,"tensor_parallel_size":int(tensor_parallel_size),"tmux_session":session,"task_id":task,"experiment_id":experiment,"hostname":socket.gethostname(),"config_hash":h,"acquired_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())},indent=2)+"\n")
PY
lease_live=0; echo "vLLM started: tmux=$SESSION manifest=$SERVER_MANIFEST gpu_lock=$LOCK_DIR"
