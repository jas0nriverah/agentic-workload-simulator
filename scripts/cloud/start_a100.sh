#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MANIFEST="${A100_STARTUP_MANIFEST:-}"
DRY_RUN=0
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --manifest) (($# >= 2)) || die '--manifest requires a path'; MANIFEST="$2"; shift 2;;
    --manifest=*) MANIFEST="${1#*=}"; shift;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) echo 'Usage: scripts/cloud/start_a100.sh --manifest FILE [--dry-run]'; exit 0;;
    *) die "unknown argument: $1";;
  esac
done
if (( DRY_RUN )); then
  python3 "$ROOT/scripts/cloud/a100_setup_doctor.py" --config "$ROOT/configs/a100_final_validation.json" --offline
  printf 'DRY-RUN: no GPU, Docker, Nsight, vLLM, server, or artifact access\n'
  exit 0
fi
[[ -n "$MANIFEST" && -f "$MANIFEST" ]] || die 'an external A100 startup manifest is required'
case "$(cd -- "$(dirname -- "$MANIFEST")" && pwd -P)" in
  "$ROOT"|"$ROOT"/*) die 'startup manifest must be outside the checkout';;
esac
python3 "$ROOT/scripts/cloud/a100_setup_doctor.py" --config "$ROOT/configs/a100_final_validation.json" --manifest "$MANIFEST"
manifest_value() { awk -F= -v key="$1" '$1 == key {print substr($0,index($0,"=")+1); exit}' "$MANIFEST"; }
MODEL="$(manifest_value VLLM_MODEL)"; REVISION="$(manifest_value VLLM_MODEL_REVISION)"; IMAGE="$(manifest_value VLLM_IMAGE)"
MODEL_CACHE="$(manifest_value MODEL_CACHE)"; MODEL_SNAPSHOT="$(manifest_value MODEL_SNAPSHOT)"; TRACE_ROOT="$(manifest_value TRACE_ROOT)"
CONTAINER="$(manifest_value A100_CONTAINER)"; SESSION="$(manifest_value A100_NSYS_SESSION)"; NSYS_BIN="$(manifest_value A100_NSYS_BIN)"
PORT="$(manifest_value VLLM_PORT)"; MAX_LEN="$(manifest_value VLLM_MAX_MODEL_LEN)"; GPU_UTIL="$(manifest_value VLLM_GPU_MEMORY_UTILIZATION)"
mkdir -p -- "$TRACE_ROOT"
command -v docker >/dev/null 2>&1 || die 'docker is required'; command -v curl >/dev/null 2>&1 || die 'curl is required'
docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'; docker image inspect "$IMAGE" >/dev/null 2>&1 || die "pinned image is unavailable: $IMAGE"
[[ -d "$MODEL_CACHE" && -d "$MODEL_SNAPSHOT" ]] || die 'A100 model cache/snapshot is missing'
[[ "$(basename -- "$MODEL_SNAPSHOT")" == "$REVISION" ]] || die 'model snapshot revision mismatch'
case "$MODEL_SNAPSHOT/" in "$MODEL_CACHE"/*) ;; *) die 'model snapshot must be inside model cache';; esac
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then die "container already exists; inspect it before reuse: $CONTAINER"; fi
if ss -H -ltn "sport = :$PORT" 2>/dev/null | grep -q .; then die "port $PORT is occupied"; fi
MODEL_CONTAINER="/root/.cache/huggingface/${MODEL_SNAPSHOT#"$MODEL_CACHE"/}"
docker run -d --name "$CONTAINER" --gpus device=0 --network host --ipc=host --shm-size=16g --pull=never \
  -e HF_HOME=/root/.cache/huggingface -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 \
  -v "$MODEL_CACHE:/root/.cache/huggingface" -v /usr/local/cuda:/host-cuda:ro -v /opt/nvidia:/opt/nvidia:ro -v "$TRACE_ROOT:/trace" \
  --entrypoint "$NSYS_BIN" "$IMAGE" launch --session-new="$SESSION" --trace=cuda,osrt --cuda-event-trace=false -- \
  python3 -m vllm.entrypoints.openai.api_server --model "$MODEL_CONTAINER" --revision "$REVISION" --served-model-name "$MODEL" \
  --host 127.0.0.1 --port "$PORT" --dtype bfloat16 --max-model-len "$MAX_LEN" --gpu-memory-utilization "$GPU_UTIL" \
  --tensor-parallel-size 1 --enable-auto-tool-choice --tool-call-parser qwen3_coder >/dev/null
startup_failed=1
cleanup() { if (( startup_failed )); then docker stop "$CONTAINER" >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM
for _ in $(seq 1 180); do
  docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true || { docker logs --tail 160 "$CONTAINER" >&2 || true; die 'A100 vLLM container exited'; }
  if curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/v1/models" | python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if any(x.get("id")==sys.argv[1] for x in d.get("data",[])) else 1)' "$MODEL" && curl -fsS --connect-timeout 2 --max-time 5 "http://127.0.0.1:$PORT/metrics" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS --connect-timeout 5 --max-time 10 "http://127.0.0.1:$PORT/health" >/dev/null || die 'A100 vLLM /health failed'
docker exec "$CONTAINER" "$NSYS_BIN" sessions list | grep -F "$SESSION" >/dev/null || die 'A100 Nsight session is not registered'
startup_failed=0
printf 'A100 profiled vLLM ready: container=%s session=%s model=%s revision=%s\n' "$CONTAINER" "$SESSION" "$MODEL" "$REVISION"
