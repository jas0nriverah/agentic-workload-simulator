#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# Start the pinned vLLM server inside an interactive Nsight Systems session.
# h100_nsight_trace_provider.py starts/stops the per-request captures; this
# script deliberately does not launch a benchmark or collect a request.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MODEL="${VLLM_MODEL:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"
REVISION="${VLLM_MODEL_REVISION:-b2cff646eb4bb1d68355c01b18ae02e7cf42d120}"
VLLM_IMAGE="${VLLM_IMAGE:-vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271}"
MODEL_CACHE="${H100_MODEL_CACHE:-/home/jasonrivera691/eic-work/cache/huggingface}"
MODEL_SNAPSHOT="${H100_MODEL_SNAPSHOT:-$MODEL_CACHE/hub/models--Qwen--Qwen3-Coder-30B-A3B-Instruct/snapshots/$REVISION}"
TRACE_ROOT="${H100_TRACE_MOUNT_ROOT:-$ROOT/artifacts/h100_final_validation_retry}"
CONTAINER="${H100_NSYS_CONTAINER:-h100-final-vllm}"
SESSION="${H100_NSYS_SESSION:-h100-final-validation}"
PORT="${VLLM_PORT:-8000}"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
GPU_MEM_UTIL="${VLLM_GPU_MEMORY_UTILIZATION:-0.90}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
port_in_use() {
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}
container_logs() { docker logs "$CONTAINER" 2>&1 | tail -n 160 || true; }

command -v docker >/dev/null 2>&1 || die 'docker is required'
command -v nvidia-smi >/dev/null 2>&1 || die 'nvidia-smi is required'
command -v curl >/dev/null 2>&1 || die 'curl is required'
[[ -x /usr/local/cuda/bin/nsys ]] || die 'host Nsight Systems is missing: /usr/local/cuda/bin/nsys'
[[ -d "$MODEL_CACHE" ]] || die "H100 model cache is missing: $MODEL_CACHE"
[[ -d "$MODEL_SNAPSHOT" ]] || die "H100 model snapshot is missing: $MODEL_SNAPSHOT"
[[ "$(basename -- "$MODEL_SNAPSHOT")" == "$REVISION" ]] || die 'model snapshot final path is not the pinned revision'
MODEL_SNAPSHOT="$(cd -- "$MODEL_SNAPSHOT" && pwd -P)"
MODEL_CACHE="$(cd -- "$MODEL_CACHE" && pwd -P)"
case "$MODEL_SNAPSHOT/" in
  "$MODEL_CACHE"/*) MODEL_RELATIVE="${MODEL_SNAPSHOT#$MODEL_CACHE/}";;
  *) die 'model snapshot must be inside H100_MODEL_CACHE';;
esac

gpu_rows="$(nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits 2>/dev/null || true)"
[[ -n "$gpu_rows" ]] || die 'could not query GPU identity'
gpu_count="$(printf '%s\n' "$gpu_rows" | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')"
[[ "$gpu_count" == 1 ]] || die "expected exactly one visible GPU; found $gpu_count"
IFS=',' read -r gpu_name gpu_memory gpu_compute <<< "$gpu_rows"
gpu_name="$(printf '%s' "$gpu_name" | sed 's/^ *//;s/ *$//')"
gpu_memory="$(printf '%s' "$gpu_memory" | sed 's/^ *//;s/ *$//')"
gpu_compute="$(printf '%s' "$gpu_compute" | sed 's/^ *//;s/ *$//')"
[[ "$gpu_name" == *H100* && "$gpu_name" != *A100* && "$gpu_name" != *H200* ]] || die "visible GPU is not H100: $gpu_name"
[[ "$gpu_memory" =~ ^[0-9]+$ && "$gpu_memory" -ge 80000 ]] || die "H100 memory is below 80000 MiB: $gpu_memory"
[[ "$gpu_compute" == 9.0 || "$gpu_compute" == 9.0* ]] || die "compute capability must be 9.0: $gpu_compute"
gpu_processes="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | sed '/^[[:space:]]*$/d' || true)"
[[ -z "$gpu_processes" ]] || die "GPU already has compute processes: $gpu_processes"
port_in_use && die "port $PORT is occupied"

docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 || die "pinned vLLM image unavailable: $VLLM_IMAGE"
image_ref="${VLLM_IMAGE%@*}"
image_repo="${image_ref%%:*}"
image_digest="${VLLM_IMAGE##*@}"
repo_digests="$(docker image inspect "$VLLM_IMAGE" --format '{{join .RepoDigests "\n"}}' 2>/dev/null || true)"
grep -Fqx "$image_repo@$image_digest" <<< "$repo_digests" || die 'pinned vLLM image digest mismatch'
image_platform="$(docker image inspect "$VLLM_IMAGE" --format '{{.Os}}/{{.Architecture}}' 2>/dev/null || true)"
[[ "$image_platform" == "linux/amd64" ]] || die "pinned vLLM image platform mismatch: $image_platform"
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  die "container name already exists; inspect before reusing: $CONTAINER"
fi

mkdir -p -- "$TRACE_ROOT"
TRACE_ROOT="$(cd -- "$TRACE_ROOT" && pwd -P)"
MODEL_CONTAINER="/root/.cache/huggingface/$MODEL_RELATIVE"
started=0
cleanup_failed_launch() {
  if (( started )); then
    printf 'vLLM launch failed; container logs:\n' >&2
    container_logs
    docker stop "$CONTAINER" >/dev/null 2>&1 || true
  fi
}
trap cleanup_failed_launch ERR

docker run -d \
  --name "$CONTAINER" \
  --gpus device=0 \
  --network host \
  --ipc=host \
  --shm-size=16g \
  --pull=never \
  -e HF_HOME=/root/.cache/huggingface \
  -e HF_HUB_OFFLINE=1 \
  -e TRANSFORMERS_OFFLINE=1 \
  -v "$MODEL_CACHE:/root/.cache/huggingface" \
  -v /usr/local/cuda:/host-cuda:ro \
  -v "$TRACE_ROOT:/trace" \
  --entrypoint /host-cuda/bin/nsys \
  "$VLLM_IMAGE" \
  launch \
  --session-new="$SESSION" \
  --trace=cuda,osrt \
  --cuda-event-trace=false \
  -- \
  python3 -m vllm.entrypoints.openai.api_server \
  --model "$MODEL_CONTAINER" \
  --revision "$REVISION" \
  --served-model-name "$MODEL" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --dtype bfloat16 \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --tensor-parallel-size 1 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder >/dev/null
started=1

for _ in $(seq 1 180); do
  if ! docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
    container_logs
    die 'profiled vLLM container exited before health checks passed'
  fi
  if curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 \
    && curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/v1/models" \
      | python3 -c 'import json,sys; expected=sys.argv[1]; data=json.load(sys.stdin); raise SystemExit(0 if any(item.get("id") == expected for item in data.get("data", [])) else 1)' "$MODEL" \
    && curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/metrics" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS --connect-timeout 5 --max-time 10 "http://127.0.0.1:$PORT/health" >/dev/null || { container_logs; die 'vLLM /health did not become ready'; }
curl -fsS --connect-timeout 5 --max-time 10 "http://127.0.0.1:$PORT/v1/models" \
  | python3 -c 'import json,sys; expected=sys.argv[1]; data=json.load(sys.stdin); raise SystemExit(0 if any(item.get("id") == expected for item in data.get("data", [])) else "served model id mismatch")' "$MODEL" \
  || { container_logs; die 'vLLM served model id did not match the pinned model'; }
curl -fsS --connect-timeout 5 --max-time 10 "http://127.0.0.1:$PORT/metrics" >/dev/null || { container_logs; die 'vLLM /metrics did not become ready'; }
docker exec "$CONTAINER" /host-cuda/bin/nsys sessions list | grep -F "$SESSION" >/dev/null || { container_logs; die 'Nsight interactive session was not registered'; }
trap - ERR
printf 'Profiled vLLM ready: container=%s session=%s model=%s revision=%s trace_root=%s\n' \
  "$CONTAINER" "$SESSION" "$MODEL" "$REVISION" "$TRACE_ROOT"
