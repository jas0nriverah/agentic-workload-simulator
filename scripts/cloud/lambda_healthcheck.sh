#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"; WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"; PORT="${VLLM_PORT:-8000}"; TIMEOUT=120; DRY=0; MANIFEST=""; prev=""
usage(){ echo 'Usage: lambda_healthcheck.sh [--manifest FILE] [--work-root DIR] [--port PORT] [--timeout SEC] [--dry-run]'; }
for arg in "$@"; do case "$arg" in --dry-run) DRY=1;; --manifest|--work-root|--port|--timeout) :;; --manifest=*) MANIFEST="${arg#*=}";; --work-root=*) WORK_ROOT="${arg#*=}";; --port=*) PORT="${arg#*=}";; --timeout=*) TIMEOUT="${arg#*=}";; -h|--help) usage; exit 0;; *) case "$prev" in --manifest) MANIFEST="$arg";; --work-root) WORK_ROOT="$arg";; --port) PORT="$arg";; --timeout) TIMEOUT="$arg";; *) echo "unknown argument: $arg" >&2; exit 2;; esac;; esac; prev="$arg"; done
if [[ -f "$MANIFEST" ]]; then while IFS= read -r line || [[ -n "$line" ]]; do line="${line%%#*}"; [[ "$line" == *=* ]] || continue; key="${line%%=*}"; value="${line#*=}"; case "$key" in WORK_ROOT) WORK_ROOT="$value";; VLLM_PORT) PORT="$value";; esac; done < "$MANIFEST"; fi
SERVER_MANIFEST="$WORK_ROOT/artifacts/manifests/vllm_server.json"; GPU_SAMPLE="$WORK_ROOT/artifacts/manifests/health_gpu_sample.csv"; HEALTH_OUT="$WORK_ROOT/artifacts/manifests/lambda_healthcheck.json"
if (( DRY )); then
  echo "DRY-RUN: require live vLLM process, GET http://127.0.0.1:$PORT/v1/models, normal completion, SWE-agent tool call parsed by parser=qwen3_coder, GET http://127.0.0.1:$PORT/metrics (native Prometheus counters/histograms), GPU sample, and clean server logs."
  echo 'DRY-RUN: classify failures independently as server, tool-parser, or telemetry-contract; chat response fields never substitute for /metrics.'; exit 0
fi
command -v curl >/dev/null 2>&1 || { echo 'server failure: curl is required' >&2; exit 1; }; command -v python3 >/dev/null 2>&1 || { echo 'server failure: python3 is required' >&2; exit 1; }; [[ -f "$SERVER_MANIFEST" ]] || { echo 'server failure: missing vLLM manifest' >&2; exit 1; }
pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$SERVER_MANIFEST")"; [[ -d "/proc/$pid" ]] || { echo "server failure: vLLM process $pid is not alive" >&2; exit 1; }
base="http://127.0.0.1:$PORT"; deadline=$((SECONDS+TIMEOUT)); models=""
until models="$(curl -fsS --max-time 5 "$base/v1/models")"; do (( SECONDS < deadline )) || { echo 'server failure: /v1/models timeout' >&2; exit 1; }; sleep 1; done
model="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' <<<"$models")" || { echo 'server failure: malformed /v1/models response' >&2; exit 1; }
body='{"model":"'"$model"'","messages":[{"role":"user","content":"Reply READY."}],"max_tokens":8,"temperature":0}'; response="$(curl -fsS --max-time 60 -H 'Content-Type: application/json' -d "$body" "$base/v1/chat/completions")" || { echo 'server failure: normal completion request failed' >&2; exit 1; }
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d.get("choices"), "no choices"' "$response" || { echo 'server failure: normal completion response malformed' >&2; exit 1; }
tool='{"model":"'"$model"'","messages":[{"role":"user","content":"Call ping."}],"tools":[{"type":"function","function":{"name":"ping","description":"Return pong","parameters":{"type":"object","properties":{}}}}],"tool_choice":{"type":"function","function":{"name":"ping"}},"max_tokens":32,"temperature":0}'; tool_response="$(curl -fsS --max-time 60 -H 'Content-Type: application/json' -d "$tool" "$base/v1/chat/completions")" || { echo 'tool-parser failure: tool-call request failed' >&2; exit 2; }
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); m=d.get("choices",[{}])[0].get("message",{}); assert m.get("tool_calls"), "no tool_calls"' "$tool_response" || { echo 'tool-parser failure: response contains no parsed tool_calls' >&2; exit 2; }
metrics="$(curl -fsS --max-time 10 "$base/metrics")" || { echo 'telemetry-contract failure: /metrics unavailable' >&2; exit 3; }
for name in 'vllm:request_success_total' 'vllm:prompt_tokens_total' 'vllm:generation_tokens_total'; do grep -Eq "^${name}(\{|[[:space:]])" <<<"$metrics" || { echo "telemetry-contract failure: missing $name" >&2; exit 3; }; done
# vLLM exposes e2e latency as a Prometheus histogram: the wire samples are
# _bucket/_count/_sum, not a bare base-name sample.
grep -Eq '^vllm:e2e_request_latency_seconds_(bucket|count|sum)(\{|[[:space:]])' <<<"$metrics" || { echo 'telemetry-contract failure: missing vllm:e2e_request_latency_seconds histogram family' >&2; exit 3; }
mkdir -p -- "$WORK_ROOT/artifacts/manifests"; printf '%s\n' "$metrics" > "$WORK_ROOT/artifacts/manifests/vllm_metrics.prom"
if [[ -f "$ROOT/scripts/observability/scrape_vllm.py" ]]; then
  python3 "$ROOT/scripts/observability/scrape_vllm.py" \
    --url "$base/metrics" --output "$WORK_ROOT/artifacts/manifests/vllm_metrics.json" \
    --raw-output "$WORK_ROOT/artifacts/manifests/vllm_metrics.prom" \
    --snapshot-kind health --scope healthcheck --validate-required --force || {
      echo 'telemetry-contract failure: lossless vLLM snapshot missing required families' >&2; exit 3;
    }
fi
if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=name,memory.used,utilization.gpu --format=csv,noheader > "$GPU_SAMPLE" || { echo 'telemetry-contract failure: GPU sample failed' >&2; exit 3; }; else echo 'telemetry-contract failure: nvidia-smi unavailable' >&2; exit 3; fi
log="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("log_path", ""))' "$SERVER_MANIFEST")"; if [[ -n "$log" && -f "$log" ]] && grep -Eiq 'out of memory|cuda error|fatal|traceback' "$log"; then echo 'server failure: fatal/OOM text in log' >&2; exit 1; fi
python3 - "$HEALTH_OUT" "$model" <<'PY'
import json,pathlib,sys,time
out,model=sys.argv[1:]; p=pathlib.Path(out); tmp=p.with_name(p.name+'.tmp'); tmp.write_text(json.dumps({"schema_version":"lambda-healthcheck.v2","status":"PASS","model":model,"metrics_endpoint":"/metrics","tool_parser":"qwen3_coder","checked_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())},indent=2)+"\n"); tmp.replace(p)
PY
echo "vLLM healthcheck passed: $model"
