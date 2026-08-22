#!/usr/bin/env bash
set -Eeuo pipefail

# Optional, separate model-serving calibration. It is never part of the first
# paid session and cannot run without a fresh result directory, explicit
# authorization, and a marker that the first SWE-agent result was inspected.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MANIFEST="$ROOT/cloud/lambda/instance_manifest.env"
SESSION="$ROOT/cloud/lambda/cloud_session.yaml"
OUTPUT=""
MARKER=""
BASE_URL="http://127.0.0.1:8000/v1"
MODEL=""
MODEL_REVISION=""
VLLM_VERSION="0.10.0"
VLLM_IMAGE="vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
HF_CACHE=""
INPUT_LEN=512
OUTPUT_LEN=64
NUM_PROMPTS=1
MAX_CONCURRENCY=1
ALLOW=0
DRY=0

usage() { echo 'Usage: calibrate_vllm.sh --output DIR --first-result-marker FILE --allow-calibration [--manifest FILE] [--session FILE] [--dry-run]'; }
while (($#)); do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2;;
    --session) SESSION="$2"; shift 2;;
    --output) OUTPUT="$2"; shift 2;;
    --first-result-marker) MARKER="$2"; shift 2;;
    --base-url) BASE_URL="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --input-len) INPUT_LEN="$2"; shift 2;;
    --output-len) OUTPUT_LEN="$2"; shift 2;;
    --num-prompts) NUM_PROMPTS="$2"; shift 2;;
    --max-concurrency) MAX_CONCURRENCY="$2"; shift 2;;
    --allow-calibration) ALLOW=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

manifest_value() {
  local wanted="$1" line key value
  [[ -f "$MANIFEST" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"; [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; value="${line#*=}"
    [[ "$key" == "$wanted" ]] && { printf '%s' "$value"; return 0; }
  done < "$MANIFEST"
  return 0
}

[[ -n "$MODEL" ]] || MODEL="$(manifest_value VLLM_MODEL)"
[[ -n "$MODEL" ]] || MODEL='Qwen/Qwen3-Coder-30B-A3B-Instruct'
MODEL_REVISION="$(manifest_value VLLM_MODEL_REVISION)"
VLLM_VERSION="$(manifest_value VLLM_VERSION)"
[[ -n "$VLLM_VERSION" ]] || VLLM_VERSION="0.10.0"
manifest_image="$(manifest_value VLLM_IMAGE)"
[[ -n "$manifest_image" ]] && VLLM_IMAGE="$manifest_image"
HF_CACHE="$(manifest_value HF_HUB_CACHE)"
cache_root="$(manifest_value CACHE_ROOT)"
# HF_HOME is the directory mounted into the container.  It must contain the
# `hub/` child; mounting the hub directory itself one level too high makes
# Transformers unable to resolve the already-downloaded model offline.
if [[ -z "$HF_CACHE" && -n "$cache_root" ]]; then HF_CACHE="$cache_root/huggingface"; fi
[[ -n "$HF_CACHE" ]] || HF_CACHE="/home/ubuntu/agentic-work/cache/huggingface"
EXPECTED_VLLM_IMAGE='vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271'
[[ "$VLLM_IMAGE" == "$EXPECTED_VLLM_IMAGE" ]] || { (( DRY )) || { echo "refusing non-frozen vLLM image: $VLLM_IMAGE" >&2; exit 1; }; }

CMD=(docker run --rm --network host --ipc=host --gpus device=0
  --entrypoint vllm -e HF_HOME=/root/.cache/huggingface -e HF_HUB_OFFLINE=1
  -v "$HF_CACHE:/root/.cache/huggingface" "$VLLM_IMAGE"
  bench serve --backend vllm --base-url "$BASE_URL" --model "$MODEL" --revision "$MODEL_REVISION"
  --dataset-name random --random-input-len "$INPUT_LEN" --random-output-len "$OUTPUT_LEN"
  --num-prompts "$NUM_PROMPTS" --max-concurrency "$MAX_CONCURRENCY" --save-result --save-detailed
  --result-dir "$OUTPUT")

if (( DRY )); then
  echo "DRY-RUN: calibration is separate from SWE-bench and is not first-session traffic"
  echo "DRY-RUN: require lambda_session_gate.sh --session $SESSION --gate G5, marker $MARKER, explicit --allow-calibration, and the frozen vLLM image digest"
  printf 'DRY-RUN: '; printf '%q ' "${CMD[@]}"; echo
  exit 0
fi

(( ALLOW )) || { echo 'calibration requires explicit --allow-calibration' >&2; exit 1; }
[[ -n "$OUTPUT" && -n "$MARKER" ]] || { echo '--output and --first-result-marker are required' >&2; exit 1; }
[[ -f "$MARKER" ]] || { echo "first-result marker is missing: $MARKER" >&2; exit 1; }
[[ ! -e "$OUTPUT" ]] || { echo "refusing to reuse calibration output: $OUTPUT" >&2; exit 1; }
[[ "$VLLM_VERSION" == "0.10.0" ]] || { echo "refusing non-pinned vLLM version: $VLLM_VERSION" >&2; exit 1; }
[[ "$MODEL_REVISION" =~ ^[0-9a-fA-F]{40}$ ]] || { echo 'calibration requires the pinned 40-hex model revision' >&2; exit 1; }
[[ -x "$ROOT/scripts/cloud/lambda_session_gate.sh" ]] || { echo 'session gate script is missing' >&2; exit 1; }
"$ROOT/scripts/cloud/lambda_session_gate.sh" --session "$SESSION" --gate G5
command -v docker >/dev/null 2>&1 || { echo 'Docker is required for pinned-container calibration' >&2; exit 1; }
docker image inspect "$VLLM_IMAGE" >/dev/null 2>&1 || { echo "pinned vLLM image is unavailable: $VLLM_IMAGE" >&2; exit 1; }
mkdir -p "$OUTPUT"
python3 - "$OUTPUT/service_calibration.json" "$MODEL" "$MODEL_REVISION" "$VLLM_VERSION" "$BASE_URL" "$INPUT_LEN" "$OUTPUT_LEN" "$NUM_PROMPTS" "$MAX_CONCURRENCY" "${CMD[@]}" <<'PY'
import hashlib, json, pathlib, sys, time
out, model, revision, version, base_url, input_len, output_len, prompts, concurrency, *argv = sys.argv[1:]
path = pathlib.Path(out)
if path.exists():
    raise SystemExit(f"refusing to overwrite calibration manifest: {path}")
encoded = json.dumps(argv, sort_keys=True, separators=(",", ":"))
path.write_text(json.dumps({
    "schema_version": "observability.service-calibration.v1",
    "status": "started", "provenance": "calibrated", "model": model,
    "model_revision": revision or None, "vllm_version": version,
    "base_url": base_url, "precision": "bf16", "input_tokens": int(input_len),
    "output_tokens": int(output_len), "num_prompts": int(prompts),
    "max_concurrency": int(concurrency), "gpu_time_claim": False,
    "hardware": {"status": "unavailable", "provenance": "unavailable"},
    "command_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
    "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
set +e
"${CMD[@]}"
rc=$?
set -e
python3 - "$OUTPUT/service_calibration.json" "$rc" <<'PY'
import json, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["status"] = "completed" if int(sys.argv[2]) == 0 else "failed"
value["returncode"] = int(sys.argv[2])
value["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
exit "$rc"
