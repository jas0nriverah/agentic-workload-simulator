#!/usr/bin/env bash
set -Eeuo pipefail

# Execute one reviewed SWE-agent command. This wrapper owns isolation, logs,
# and evaluator handoff; it does not implement or replace SWE-agent.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export AGENTIC_SOURCE_ROOT="$ROOT"
MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"
WORK_ROOT_WAS_SET=0
[[ -n "${WORK_ROOT+x}" ]] && WORK_ROOT_WAS_SET=1
WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"
LOG_DIR=""
ID=""
MODE="uninstrumented"
EXPERIMENT_ID=""
DRY=0
RESUME=0

usage() {
  echo 'Usage: lambda_run_first_experiment.sh [--manifest FILE] [--instance-id ID] [--experiment-id ID] [--attempt-id ID] [--mode uninstrumented|thin-telemetry] [--work-root DIR] [--log-dir DIR] [--resume] [--dry-run]'
}

while (($#)); do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --resume) RESUME=1; shift;;
    --manifest) [[ $# -ge 2 ]] || { echo '--manifest requires a file' >&2; exit 2; }; MANIFEST="$2"; shift 2;;
    --manifest=*) MANIFEST="${1#*=}"; shift;;
    --instance-id) [[ $# -ge 2 ]] || { echo '--instance-id requires an ID' >&2; exit 2; }; ID="$2"; shift 2;;
    --instance-id=*) ID="${1#*=}"; shift;;
    --experiment-id) [[ $# -ge 2 ]] || { echo '--experiment-id requires an ID' >&2; exit 2; }; EXPERIMENT_ID="$2"; shift 2;;
    --experiment-id=*) EXPERIMENT_ID="${1#*=}"; shift;;
    --attempt-id) [[ $# -ge 2 ]] || { echo '--attempt-id requires an ID' >&2; exit 2; }; ATTEMPT_ID="$2"; shift 2;;
    --attempt-id=*) ATTEMPT_ID="${1#*=}"; shift;;
    --mode) [[ $# -ge 2 ]] || { echo '--mode requires a value' >&2; exit 2; }; MODE="$2"; shift 2;;
    --mode=*) MODE="${1#*=}"; shift;;
    --work-root) [[ $# -ge 2 ]] || { echo '--work-root requires a directory' >&2; exit 2; }; WORK_ROOT="$2"; WORK_ROOT_WAS_SET=1; shift 2;;
    --work-root=*) WORK_ROOT="${1#*=}"; WORK_ROOT_WAS_SET=1; shift;;
    --log-dir) [[ $# -ge 2 ]] || { echo '--log-dir requires a directory' >&2; exit 2; }; LOG_DIR="$2"; shift 2;;
    --log-dir=*) LOG_DIR="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

# Read only manifest keys; never source a cloud file that may contain secrets.
manifest_value() {
  local wanted="$1" line key value
  [[ -f "$MANIFEST" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; value="${line#*=}"
    [[ "$key" == "$wanted" ]] && { printf '%s' "$value"; return 0; }
  done <"$MANIFEST"
  return 0
}

if (( ! WORK_ROOT_WAS_SET )); then
  manifest_work_root="$(manifest_value WORK_ROOT)"
  [[ -n "$manifest_work_root" ]] && WORK_ROOT="$manifest_work_root"
fi

[[ -n "$ID" ]] || ID="$(manifest_value FIRST_LITE_INSTANCE_ID)"
[[ -n "$EXPERIMENT_ID" ]] || EXPERIMENT_ID="$(manifest_value EXPERIMENT_ID)"
[[ -n "$EXPERIMENT_ID" ]] || EXPERIMENT_ID="first-lite-${ID:-unconfigured}"
[[ -n "$LOG_DIR" ]] || LOG_DIR="$WORK_ROOT/logs/first-experiment/$EXPERIMENT_ID/$ID"

case "$MODE" in
  uninstrumented) COMMAND="${SWE_AGENT_COMMAND:-$(manifest_value SWE_AGENT_COMMAND)}";;
  thin-telemetry) COMMAND="${SWE_AGENT_TELEMETRY_COMMAND:-$(manifest_value SWE_AGENT_TELEMETRY_COMMAND)}";;
  *) echo 'mode must be uninstrumented or thin-telemetry' >&2; exit 2;;
esac
EVALUATOR="${EVALUATE_COMMAND:-$(manifest_value EVALUATE_COMMAND)}"
SWE_AGENT_REVISION="${SWE_AGENT_REVISION:-$(manifest_value SWE_AGENT_REVISION)}"
SWE_BENCH_REVISION="${SWE_BENCH_REVISION:-$(manifest_value SWE_BENCH_REVISION)}"
MODEL_REVISION="${VLLM_MODEL_REVISION:-$(manifest_value VLLM_MODEL_REVISION)}"
DATASET_MANIFEST_PATH="${DATASET_MANIFEST_PATH:-$(manifest_value DATASET_MANIFEST_PATH)}"
LITE_SOURCE_SHA256="${LITE_SOURCE_SHA256:-$(manifest_value LITE_DATASET_SHA256)}"
LITE_FIRST_ROW_SHA256="${LITE_FIRST_ROW_SHA256:-$(manifest_value LITE_FIRST_DATASET_SHA256)}"
ATTEMPT_ID="${ATTEMPT_ID:-$(manifest_value ATTEMPT_ID)}"
PREDICTION_PATH="${PREDICTION_PATH:-$(manifest_value PREDICTION_PATH)}"
TIMEOUT_SECONDS="${FIRST_EXPERIMENT_TIMEOUT_SECONDS:-$(manifest_value FIRST_EXPERIMENT_TIMEOUT_SECONDS)}"
[[ -n "$TIMEOUT_SECONDS" ]] || TIMEOUT_SECONDS=7200
[[ -n "$ATTEMPT_ID" ]] || ATTEMPT_ID="attempt-001"
METRICS_URL="${VLLM_METRICS_URL:-$(manifest_value VLLM_METRICS_URL)}"
[[ -n "$METRICS_URL" ]] || METRICS_URL="http://127.0.0.1:8000/metrics"
TELEMETRY_INTERVAL_SECONDS="${TELEMETRY_INTERVAL_SECONDS:-$(manifest_value TELEMETRY_INTERVAL_SECONDS)}"
[[ -n "$TELEMETRY_INTERVAL_SECONDS" ]] || TELEMETRY_INTERVAL_SECONDS=1
EVALUATOR_TIMEOUT_SECONDS="${SWE_BENCH_TIMEOUT_SECONDS:-$(manifest_value SWE_BENCH_TIMEOUT_SECONDS)}"
[[ -n "$EVALUATOR_TIMEOUT_SECONDS" ]] || EVALUATOR_TIMEOUT_SECONDS=1800
EXPERIMENT_MAX_CALLS="${EXPERIMENT_MAX_CALLS:-$(manifest_value EXPERIMENT_MAX_CALLS)}"; [[ -n "$EXPERIMENT_MAX_CALLS" ]] || EXPERIMENT_MAX_CALLS=30
EXPERIMENT_MAX_OUTPUT_TOKENS="${EXPERIMENT_MAX_OUTPUT_TOKENS:-$(manifest_value EXPERIMENT_MAX_OUTPUT_TOKENS)}"; [[ -n "$EXPERIMENT_MAX_OUTPUT_TOKENS" ]] || EXPERIMENT_MAX_OUTPUT_TOKENS=2048
EXPERIMENT_MAX_OBSERVATION_LENGTH="${EXPERIMENT_MAX_OBSERVATION_LENGTH:-$(manifest_value EXPERIMENT_MAX_OBSERVATION_LENGTH)}"; [[ -n "$EXPERIMENT_MAX_OBSERVATION_LENGTH" ]] || EXPERIMENT_MAX_OBSERVATION_LENGTH=100000
EXPERIMENT_TEMPERATURE="${EXPERIMENT_TEMPERATURE:-$(manifest_value EXPERIMENT_TEMPERATURE)}"; [[ -n "$EXPERIMENT_TEMPERATURE" ]] || EXPERIMENT_TEMPERATURE=0.0
EXPERIMENT_SEED="${EXPERIMENT_SEED:-$(manifest_value EXPERIMENT_SEED)}"; [[ -n "$EXPERIMENT_SEED" ]] || EXPERIMENT_SEED=0
BASE_EVALUATOR_REPORT_DIR="${EVALUATOR_REPORT_DIR:-$(manifest_value EVALUATOR_REPORT_DIR)}"
[[ -n "$BASE_EVALUATOR_REPORT_DIR" ]] || BASE_EVALUATOR_REPORT_DIR="$WORK_ROOT/artifacts/$EXPERIMENT_ID/evaluation"
EVALUATOR_REPORT_DIR="$BASE_EVALUATOR_REPORT_DIR/$ATTEMPT_ID"
BASE_AGENT_OUTPUT_DIR="${SWE_AGENT_OUTPUT_DIR:-$(manifest_value SWE_AGENT_OUTPUT_DIR)}"
[[ -n "$BASE_AGENT_OUTPUT_DIR" ]] || BASE_AGENT_OUTPUT_DIR="$WORK_ROOT/experiments/$EXPERIMENT_ID"
AGENT_OUTPUT_DIR="$BASE_AGENT_OUTPUT_DIR/$ATTEMPT_ID"
BASE_PREDICTION_PATH="$PREDICTION_PATH"
if [[ -n "$BASE_PREDICTION_PATH" && "$BASE_PREDICTION_PATH" == "$BASE_AGENT_OUTPUT_DIR"/* ]]; then
  PREDICTION_PATH="$AGENT_OUTPUT_DIR/${BASE_PREDICTION_PATH#"$BASE_AGENT_OUTPUT_DIR"/}"
else
  PREDICTION_PATH="$AGENT_OUTPUT_DIR/preds.json"
fi
RAW_DIR="$WORK_ROOT/data/raw/$EXPERIMENT_ID/lite/$ID/$ATTEMPT_ID"

# SWE-agent's run-batch refuses to redo an existing instance by default. Keep
# the control and thin attempts physically separate while preserving every
# model/sampling/request flag. The reviewed manifest uses the base output path;
# only that artifact-root prefix is rewritten for this named attempt.
if [[ -n "$BASE_AGENT_OUTPUT_DIR" ]]; then
  COMMAND="${COMMAND//$BASE_AGENT_OUTPUT_DIR/$AGENT_OUTPUT_DIR}"
  EVALUATOR="${EVALUATOR//$BASE_AGENT_OUTPUT_DIR/$AGENT_OUTPUT_DIR}"
fi
EVALUATOR="${EVALUATOR//$BASE_EVALUATOR_REPORT_DIR/$EVALUATOR_REPORT_DIR}"
EVALUATOR_RUN_ID="${EXPERIMENT_ID}-${ATTEMPT_ID}"
EVALUATOR="${EVALUATOR//--run_id $EXPERIMENT_ID/--run_id $EVALUATOR_RUN_ID}"

AGENT_LOG="$LOG_DIR/$ID.$MODE.agent.log"
EVAL_LOG="$LOG_DIR/$ID.$MODE.evaluation.log"

clock_now_ns() {
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'from agentic_sim.telemetry.clock import monotonic_ns; print(monotonic_ns())'
}

clock_now_utc() {
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 -c 'from agentic_sim.telemetry.clock import utc_now; print(utc_now())'
}

if ((DRY)); then
  echo "DRY-RUN: direct SWE-agent mode=$MODE instance=${ID:-<configured instance>} experiment=$EXPERIMENT_ID attempt=$ATTEMPT_ID"
  echo "DRY-RUN: output=$RAW_DIR stdout=$AGENT_LOG stderr=$EVAL_LOG"
  if [[ -n "$COMMAND" ]]; then echo "DRY-RUN: command=$COMMAND"; else echo 'DRY-RUN: command=<SWE_AGENT_COMMAND required; no command executes>'; fi
  if [[ -n "$EVALUATOR" ]]; then echo "DRY-RUN: evaluator=$EVALUATOR (runtime is separate from trajectory E2E)"; else echo 'DRY-RUN: evaluator=<EVALUATE_COMMAND required; no command executes>'; fi
  echo "DRY-RUN: pins swe_agent=${SWE_AGENT_REVISION:-<missing>} swe_bench=${SWE_BENCH_REVISION:-<missing>} model=${MODEL_REVISION:-<missing>} timeout_seconds=$TIMEOUT_SECONDS"
  echo "DRY-RUN: resolved knobs calls=$EXPERIMENT_MAX_CALLS output_tokens=$EXPERIMENT_MAX_OUTPUT_TOKENS observation_chars=$EXPERIMENT_MAX_OBSERVATION_LENGTH temperature=$EXPERIMENT_TEMPERATURE seed=$EXPERIMENT_SEED"
  exit 0
fi

[[ -f "$MANIFEST" ]] || { echo "instance manifest is required for a non-dry experiment run: $MANIFEST" >&2; exit 1; }
[[ -n "$ID" ]] || { echo 'FIRST_LITE_INSTANCE_ID/--instance-id is required' >&2; exit 1; }
[[ -n "$COMMAND" ]] || { echo "reviewed command for mode $MODE is required (SWE_AGENT_COMMAND or SWE_AGENT_TELEMETRY_COMMAND)" >&2; exit 1; }
[[ -n "$EVALUATOR" ]] || { echo 'EVALUATE_COMMAND must be the reviewed official generated-prediction evaluator command' >&2; exit 1; }
if [[ "$COMMAND" == *"VLLM_API_KEY"* && -z "${VLLM_API_KEY:-}" ]]; then
  echo 'VLLM_API_KEY must be provided through the process environment; it is never read from or sourced from the manifest' >&2
  exit 1
fi
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo 'FIRST_EXPERIMENT_TIMEOUT_SECONDS must be a positive integer' >&2; exit 1; }
[[ "$EVALUATOR_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo 'SWE_BENCH_TIMEOUT_SECONDS must be a positive integer' >&2; exit 1; }
[[ "$TELEMETRY_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]] || { echo 'TELEMETRY_INTERVAL_SECONDS must be a positive integer' >&2; exit 1; }
for pin in "$SWE_AGENT_REVISION" "$SWE_BENCH_REVISION" "$MODEL_REVISION"; do
  [[ "$pin" =~ ^[0-9a-fA-F]{12,64}$ ]] || { echo 'refusing unpinned SWE-agent/SWE-bench/model revision' >&2; exit 1; }
done
if [[ "$COMMAND" == *" main "* || "$COMMAND" == *" latest "* || "$COMMAND" == *" master "* ]]; then
  echo 'refusing floating revision in SWE-agent command' >&2; exit 1
fi
for required in 'run-batch' '--instances.type file' '--instances.path' '--agent.model.name' '--agent.model.api_base' '--agent.model.api_key' '--num_workers 1'; do
  [[ "$COMMAND" == *"$required"* ]] || { echo "reviewed SWE-agent command is missing required contract: $required" >&2; exit 1; }
done
[[ "$COMMAND" != *"--instances.split"* ]] || { echo 'file-backed SWE-agent command must not specify an unsupported split flag' >&2; exit 1; }
EXPECTED_MODEL="$(manifest_value VLLM_MODEL)"; [[ -n "$EXPECTED_MODEL" ]] || EXPECTED_MODEL='Qwen/Qwen3-Coder-30B-A3B-Instruct'
EXPECTED_DATASET_PATH="$(manifest_value LITE_DATASET_PATH)"
if [[ -n "$EXPECTED_DATASET_PATH" ]]; then
  [[ "$COMMAND" == *"--instances.path $EXPECTED_DATASET_PATH"* ]] || { echo 'reviewed SWE-agent command dataset path does not match the frozen Lite asset' >&2; exit 1; }
  [[ "$COMMAND" == *"--instances.filter '^$ID$'"* ]] || { echo 'reviewed SWE-agent command instance filter does not match the selected Lite ID' >&2; exit 1; }
  [[ "$COMMAND" == *"--agent.model.name openai/$EXPECTED_MODEL"* ]] || { echo 'reviewed SWE-agent command model does not match the frozen model' >&2; exit 1; }
  [[ "$COMMAND" == *"--agent.model.api_base http://127.0.0.1:8000/v1"* ]] || { echo 'reviewed SWE-agent command API base is not the frozen localhost vLLM endpoint' >&2; exit 1; }
fi
if [[ -n "$EXPECTED_DATASET_PATH" && -n "$DATASET_MANIFEST_PATH" ]]; then
  python3 - "$EXPECTED_DATASET_PATH" "$DATASET_MANIFEST_PATH" "$ID" "$LITE_SOURCE_SHA256" "$LITE_FIRST_ROW_SHA256" <<'PY'
import hashlib
import json
import pathlib
import sys

asset_path = pathlib.Path(sys.argv[1])
manifest_path = pathlib.Path(sys.argv[2])
instance_id = sys.argv[3]
expected_source = sys.argv[4]
expected_row = sys.argv[5]
if instance_id != "astropy__astropy-12907":
    raise SystemExit("first paid control must use the reviewed Lite instance astropy__astropy-12907")
if not asset_path.is_file() or asset_path.suffix != ".json":
    raise SystemExit("reviewed Lite first-instance JSON asset is missing")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
section = manifest.get("lite")
if not isinstance(section, dict) or section.get("repo") != "SWE-bench/SWE-bench_Lite" or section.get("revision") != "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e":
    raise SystemExit("Lite dataset manifest revision/repository is not the reviewed pin")
source = pathlib.Path(section.get("source_file", ""))
if source.suffix != ".parquet" or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected_source or section.get("source_file_sha256") != expected_source:
    raise SystemExit("Lite source Parquet hash does not match the reviewed measured pin")
values = json.loads(asset_path.read_text(encoding="utf-8"))
if not isinstance(values, list) or len(values) != 1 or values[0].get("instance_id") != instance_id:
    raise SystemExit("Lite first-instance asset is not the canonical one-row file")
canonical = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
if hashlib.sha256(canonical).hexdigest() != expected_row:
    raise SystemExit("Lite first-instance canonical row hash does not match the reviewed measured pin")
selected = [item for item in section.get("selected", []) if item.get("instance_id") == instance_id]
if len(selected) != 1 or selected[0].get("sha256") != expected_row:
    raise SystemExit("Lite first-instance selected-row hash is not the reviewed measured pin")
print("validated Lite source Parquet and first control row provenance")
PY
fi
if [[ -n "$(manifest_value SWE_AGENT_OUTPUT_DIR)" ]]; then
  [[ "$COMMAND" == *"--output_dir $AGENT_OUTPUT_DIR"* || "$COMMAND" == *"--output_dir \"$AGENT_OUTPUT_DIR\""* || "$COMMAND" == *"$AGENT_OUTPUT_DIR"* ]] || { echo 'reviewed SWE-agent command output directory is not isolated to this attempt' >&2; exit 1; }
fi
[[ "$EVALUATOR" == *"swebench.harness.run_evaluation"* && "$EVALUATOR" == *"--predictions_path"* && "$EVALUATOR" == *"--instance_ids"* ]] || { echo 'reviewed evaluator command is not the official generated-prediction contract' >&2; exit 1; }
mkdir -p -- "$RAW_DIR"

if (( RESUME )); then
  [[ -f "$RAW_DIR/config.json" ]] || { echo "cannot resume an attempt without its immutable config: $RAW_DIR" >&2; exit 1; }
fi
if [[ -e "$RAW_DIR/summary.json" ]] && grep -q '"status": "completed"' "$RAW_DIR/summary.json" 2>/dev/null; then
  echo "successful attempt exists; use a new ATTEMPT_ID (raw attempts are append-only): $RAW_DIR" >&2
  exit 1
fi

# SWE-agent v1.1.0 rejects nested completion_kwargs.* CLI flags. The reviewed
# manifest points at a tracked baseline fragment; each attempt receives an
# immutable copy with its resolved max_tokens/seed values, and the command is
# rewritten to that copy before validation and execution. Control and thin
# modes therefore share the same resolved model/request payload.
REQUEST_CONFIG_TEMPLATE="$(manifest_value SWE_AGENT_REQUEST_CONFIG)"
if [[ -n "$REQUEST_CONFIG_TEMPLATE" && "$COMMAND" == *"$REQUEST_CONFIG_TEMPLATE"* ]]; then
  REQUEST_CONFIG_PATH="$RAW_DIR/sweagent_request.yaml"
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/scripts/cloud/write_sweagent_request_config.py" \
    --output "$REQUEST_CONFIG_PATH" --max-tokens "$EXPERIMENT_MAX_OUTPUT_TOKENS" --seed "$EXPERIMENT_SEED"
  COMMAND="${COMMAND//$REQUEST_CONFIG_TEMPLATE/$REQUEST_CONFIG_PATH}"
fi
validator_args=(--command "$COMMAND" --expected-instance "$ID" --expected-calls "$EXPERIMENT_MAX_CALLS"
  --expected-output-tokens "$EXPERIMENT_MAX_OUTPUT_TOKENS"
  --expected-observation-length "$EXPERIMENT_MAX_OBSERVATION_LENGTH"
  --expected-temperature "$EXPERIMENT_TEMPERATURE" --expected-seed "$EXPERIMENT_SEED"
  --output "$RAW_DIR/resolved_command.json")
[[ -n "$(manifest_value VLLM_MODEL)" ]] && validator_args+=(--expected-model "openai/$EXPECTED_MODEL")
[[ -n "$EXPECTED_DATASET_PATH" ]] && validator_args+=(--expected-dataset-path "$EXPECTED_DATASET_PATH")
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/scripts/cloud/validate_sweagent_command.py" "${validator_args[@]}" >/dev/null
if [[ -n "$BASE_PREDICTION_PATH" ]]; then
  [[ "$EVALUATOR" == *"--predictions_path $PREDICTION_PATH"* || "$EVALUATOR" == *"$PREDICTION_PATH"* ]] || { echo 'reviewed evaluator prediction path is not isolated to this attempt' >&2; exit 1; }
fi
if [[ "$EVALUATOR" == *"--run_id"* ]]; then
  [[ "$EVALUATOR" == *"--run_id $EVALUATOR_RUN_ID"* ]] || { echo 'reviewed evaluator run ID is not isolated to this attempt' >&2; exit 1; }
fi

run_limited() {
  local limit="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30 "$limit" "$@"
    return $?
  fi
  # macOS/local development lacks GNU timeout. Keep dry-run and fixture
  # rehearsal safe without weakening the Linux path used for paid runs.
  "$@" &
  local child=$! deadline=$((SECONDS + limit))
  while kill -0 "$child" 2>/dev/null && ((SECONDS < deadline)); do sleep 1; done
  if kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    sleep 1
    kill -KILL "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
    return 124
  fi
  wait "$child"
}

run_thin_observer() {
  local agent_pid="$1" output="$RAW_DIR/events.jsonl" sample_count=0
  touch "$RAW_DIR/telemetry_scrapes.jsonl"
  sample_count="$(wc -l < "$output" 2>/dev/null | tr -d ' ')"
  [[ "$sample_count" =~ ^[0-9]+$ ]] || sample_count=0
  while kill -0 "$agent_pid" 2>/dev/null; do
    python3 "$ROOT/scripts/observability/collect_interval.py" \
      --metrics-url "$METRICS_URL" --events "$output" \
      --scrapes "$RAW_DIR/telemetry_scrapes.jsonl" --run-id "$EXPERIMENT_ID" \
      --instance-id "$ID" --attempt-id "$ATTEMPT_ID" --seq "$sample_count" || true
    sample_count=$((sample_count + 1))
    sleep "$TELEMETRY_INTERVAL_SECONDS"
  done
  # Capture one final full snapshot even if the agent completed before the
  # first loop tick; per-field failures remain explicitly unavailable.
  python3 "$ROOT/scripts/observability/collect_interval.py" \
    --metrics-url "$METRICS_URL" --events "$output" \
    --scrapes "$RAW_DIR/telemetry_scrapes.jsonl" --run-id "$EXPERIMENT_ID" \
    --instance-id "$ID" --attempt-id "$ATTEMPT_ID" --seq "$sample_count" \
    --snapshot-kind final || true
  printf '%s\n' "$((sample_count + 1))" > "$RAW_DIR/telemetry_sample_count"
}

mkdir -p -- "$LOG_DIR" "$RAW_DIR"

export FIRST_LITE_INSTANCE_ID="$ID" EXPERIMENT_ID ATTEMPT_ID AGENTIC_RUN_OUTPUT="$RAW_DIR" AGENTIC_SWE_OUTPUT_DIR="$AGENT_OUTPUT_DIR" AGENTIC_PREDICTION_PATH="$PREDICTION_PATH"
python3 - "$RAW_DIR" "$EXPERIMENT_ID" "$ID" "$ATTEMPT_ID" "$MODE" "$SWE_AGENT_REVISION" "$SWE_BENCH_REVISION" "$MODEL_REVISION" "$COMMAND" "$RAW_DIR/resolved_command.json" <<'PY'
import hashlib, json, os, pathlib, sys
source_root = os.environ.get("AGENTIC_SOURCE_ROOT")
if source_root:
    sys.path.insert(0, str(pathlib.Path(source_root) / "src"))
from agentic_sim.telemetry.clock import clock_metadata

root, run_id, instance_id, attempt_id, mode, swe, bench, model, command, resolved_path = sys.argv[1:]
root = pathlib.Path(root)
resolved_command = json.loads(pathlib.Path(resolved_path).read_text(encoding="utf-8"))
argv_hash = resolved_command["command_sha256"]
config = {
    "schema_version": "cr6.run-config.v2", "artifact_contract_version": 2, "run_id": run_id,
    "instance_id": instance_id, "attempt_id": attempt_id, "mode": mode,
    "swe_agent_revision": swe, "swe_bench_revision": bench,
    "model_revision": model, "command_hash": argv_hash,
    "provenance": "measured", "command": command,
    "resolved_experiment": resolved_command["resolved"],
    "request_contract": resolved_command["request_contract"],
    "control_thin_payload_equivalence": True,
    "clock": clock_metadata(),
    "observability_level": "control" if mode == "uninstrumented" else "thin",
    "profilers_enabled": [] if mode == "uninstrumented" else ["vllm_prometheus_interval", "nvidia_smi_interval"],
    "instrumentation_version": "obs-1",
    "vllm_metrics_available": [],
    "dcgm_metrics_available": [],
    "measurement_rules": {
        "native_vllm_scope": "server_aggregate",
        "request_id_from_native_metrics": False,
        "gpu_time_from_nvidia_smi": False,
    },
}
config_path = root / "config.json"
encoded = json.dumps(config, indent=2, sort_keys=True) + "\n"
if config_path.exists():
    if config_path.read_text() != encoded:
        raise SystemExit("attempt config already exists with different content; choose a new ATTEMPT_ID")
else:
    config_path.write_text(encoded)
for name in ("events.jsonl", "model_calls.jsonl", "tool_calls.jsonl"):
    (root / name).touch(exist_ok=True)
for name, reason in (("prediction.json", "SWE-agent output path is not configured in this wrapper"), ("eval.json", "official evaluator runs separately"), ("summary.json", "run has not completed")):
    path = root / name
    if not path.exists():
        path.write_text(json.dumps({"schema_version": "cr6.artifact.v2", "status": "unavailable", "provenance": "unavailable", "reason": reason}, sort_keys=True) + "\n")
counters = root / "counters.unavailable.json"
if not counters.exists():
    counters.write_text(json.dumps({"schema_version": "cr6.artifact.v2", "artifact": "counters", "status": "unavailable", "provenance": "unavailable", "reason": "counter export requires pinned telemetry interface"}, sort_keys=True) + "\n")
PY

if [[ "$MODE" == thin-telemetry ]]; then
  # Take the cumulative server snapshot immediately before the agent starts;
  # healthcheck traffic is thereby excluded from this attempt's delta.
  python3 "$ROOT/scripts/observability/scrape_vllm.py" \
    --url "$METRICS_URL" --output "$RAW_DIR/vllm_metrics_start.json" \
    --raw-output "$RAW_DIR/vllm_metrics_start.prom" --run-id "$EXPERIMENT_ID" \
    --attempt-id "$ATTEMPT_ID" --snapshot-kind start --scope run_interval || true
fi

echo "running direct SWE-agent mode=$MODE instance=$ID"
set +e
started_utc="$(clock_now_utc)"
started_ns="$(clock_now_ns)"
if [[ "$MODE" == thin-telemetry ]]; then
  run_limited "$TIMEOUT_SECONDS" bash -c "$COMMAND" > >(tee -a "$AGENT_LOG") 2>&1 &
  agent_pid=$!
  run_thin_observer "$agent_pid" &
  observer_pid=$!
  wait "$agent_pid"
  agent_rc=$?
  wait "$observer_pid" || true
else
  run_limited "$TIMEOUT_SECONDS" bash -c "$COMMAND" > >(tee -a "$AGENT_LOG") 2>&1
  agent_rc=$?
fi
if [[ "$MODE" == thin-telemetry ]]; then
  # End the aggregate window before the official evaluator starts.
  python3 "$ROOT/scripts/observability/scrape_vllm.py" \
    --url "$METRICS_URL" --output "$RAW_DIR/vllm_metrics_end.json" \
    --raw-output "$RAW_DIR/vllm_metrics_end.prom" --run-id "$EXPERIMENT_ID" \
    --attempt-id "$ATTEMPT_ID" --snapshot-kind end --scope run_interval || true
  if [[ -s "$RAW_DIR/vllm_metrics_start.json" && -s "$RAW_DIR/vllm_metrics_end.json" ]]; then
    python3 "$ROOT/scripts/observability/derive_vllm_delta.py" \
      --before "$RAW_DIR/vllm_metrics_start.json" --after "$RAW_DIR/vllm_metrics_end.json" \
      --output "$RAW_DIR/vllm_metrics_delta.json" || true
  fi
fi
# The trajectory timing boundary closes before the official evaluator starts.
# Evaluator wall time is recorded separately and is never included in E2E.
ended_utc="$(clock_now_utc)"
ended_ns="$(clock_now_ns)"
eval_rc="unavailable"
evaluator_started_ns=""
evaluator_ended_ns=""
evaluator_started_utc=""
evaluator_ended_utc=""
if ((agent_rc == 0)); then
  echo "running official generated-prediction evaluation (separate runtime)"
  # SWE-bench v4.1.0 writes its final report in the evaluator process's
  # working directory; --report_dir controls setup/report artifacts but does
  # not relocate that final JSON. Run in the isolated per-attempt directory
  # so the official report is captured with the raw trajectory.
  mkdir -p -- "$EVALUATOR_REPORT_DIR"
  evaluator_started_utc="$(clock_now_utc)"
  evaluator_started_ns="$(clock_now_ns)"
  run_limited "$EVALUATOR_TIMEOUT_SECONDS" bash -c "cd -- \"$EVALUATOR_REPORT_DIR\" && $EVALUATOR" > >(tee -a "$EVAL_LOG") 2>&1
  eval_rc=$?
  evaluator_ended_utc="$(clock_now_utc)"
  evaluator_ended_ns="$(clock_now_ns)"
fi
set -e
if [[ -n "$PREDICTION_PATH" && -f "$PREDICTION_PATH" ]] && grep -q '"status": "unavailable"' "$RAW_DIR/prediction.json" 2>/dev/null; then
  cp -- "$PREDICTION_PATH" "$RAW_DIR/prediction.json"
fi
if [[ -d "$AGENT_OUTPUT_DIR" ]]; then
  mkdir -p -- "$RAW_DIR/sweagent_output"
  cp -a -- "$AGENT_OUTPUT_DIR/." "$RAW_DIR/sweagent_output/"
fi
if [[ -d "$EVALUATOR_REPORT_DIR" ]]; then
  mkdir -p -- "$RAW_DIR/evaluator_report"
  cp -a -- "$EVALUATOR_REPORT_DIR/." "$RAW_DIR/evaluator_report/"
fi
python3 - "$RAW_DIR/eval.json" "$eval_rc" "$EVALUATOR" "$EVALUATOR_REPORT_DIR" "$RAW_DIR/evaluator_report" "$evaluator_started_ns" "$evaluator_ended_ns" "$evaluator_started_utc" "$evaluator_ended_utc" <<'PY'
import hashlib, json, os, pathlib, sys
source_root = os.environ.get("AGENTIC_SOURCE_ROOT")
if source_root:
    sys.path.insert(0, str(pathlib.Path(source_root) / "src"))
from agentic_sim.telemetry.clock import clock_metadata

path, result, command, report_dir, report_snapshot, evaluator_started_ns, evaluator_ended_ns, evaluator_started_utc, evaluator_ended_utc = sys.argv[1:]
status = "unavailable" if result == "unavailable" else ("completed" if result == "0" else ("timeout" if result in {"124", "137"} else "failed"))
report_files = sorted(str(item.relative_to(pathlib.Path(report_snapshot))) for item in pathlib.Path(report_snapshot).rglob("*") if item.is_file()) if pathlib.Path(report_snapshot).is_dir() else []
clock = clock_metadata()
pathlib.Path(path).write_text(json.dumps({
    "schema_version": "cr6.evaluation.v2", "status": status, "clock": clock,
    "provenance": "measured" if result != "unavailable" else "unavailable",
    "returncode": None if result == "unavailable" else int(result),
    "command_sha256": hashlib.sha256(command.encode()).hexdigest() if command else None,
    "report_dir": report_dir,
    "report_snapshot": "evaluator_report",
    "report_files": report_files,
    "evaluator_started_mono_ns": int(evaluator_started_ns) if evaluator_started_ns else None,
    "evaluator_ended_mono_ns": int(evaluator_ended_ns) if evaluator_ended_ns else None,
    "evaluator_started_at_utc": evaluator_started_utc or None,
    "evaluator_ended_at_utc": evaluator_ended_utc or None,
    "trajectory_timing_excluded": True,
    "runtime_excluded_from_trajectory": True,
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
python3 - "$RAW_DIR/telemetry_contract.json" "$RAW_DIR/events.jsonl" "$MODE" <<'PY'
import json, pathlib, sys
out, events_path, mode = sys.argv[1:]
rows = []
path = pathlib.Path(events_path)
if path.exists():
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
measured = sum(1 for row in rows if row.get("event_type") == "telemetry_sample" and row.get("provenance") == "measured")
samples = sum(1 for row in rows if row.get("event_type") == "telemetry_sample")
if mode == "thin-telemetry" and measured:
    obj = {"schema_version":"obs.telemetry-contract.v2","status":"measured","provenance":"measured","sample_count":samples,"measured_sample_count":measured,"correlation_scope":"run_interval","request_mutation":False,"native_vllm_scope":"server_aggregate","native_vllm_request_correlation":False,"snapshot_paths":["vllm_metrics_start.json","vllm_metrics_end.json","vllm_metrics_delta.json"]}
else:
    obj = {"schema_version":"obs.telemetry-contract.v2","status":"unavailable","provenance":"unavailable","sample_count":samples,"measured_sample_count":measured,"reason":"control run or no complete Prometheus/GPU interval sample","request_mutation":False,"native_vllm_scope":"server_aggregate","native_vllm_request_correlation":False}
pathlib.Path(out).write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
python3 - "$RAW_DIR/summary.json" "$EXPERIMENT_ID" "$ID" "$ATTEMPT_ID" "$MODE" "$agent_rc" "$eval_rc" "$started_utc" "$ended_utc" "$started_ns" "$ended_ns" <<'PY'
import json, os, pathlib, sys
source_root = os.environ.get("AGENTIC_SOURCE_ROOT")
if source_root:
    sys.path.insert(0, str(pathlib.Path(source_root) / "src"))
from agentic_sim.telemetry.clock import clock_metadata

path, run_id, instance_id, attempt_id, mode, agent_rc, eval_rc, started_utc, ended_utc, start, end = sys.argv[1:]
agent_rc = int(agent_rc); end = int(end); start = int(start)
status = "completed" if agent_rc == 0 and eval_rc == "0" else ("timeout" if agent_rc in (124, 137) or eval_rc in ("124", "137") else ("runner_failed" if agent_rc else "evaluation_failed"))
pathlib.Path(path).write_text(json.dumps({
    "schema_version": "cr6.summary.v2", "artifact_contract_version": 2, "run_id": run_id,
    "instance_id": instance_id, "attempt_id": attempt_id, "mode": mode,
    "status": status, "agent_returncode": agent_rc,
    "started_at_utc": started_utc, "ended_at_utc": ended_utc,
    "evaluator_returncode": None if eval_rc == "unavailable" else int(eval_rc),
    "start_mono_ns": start, "end_mono_ns": end,
    "duration_ms": (end - start) / 1_000_000,
    "evaluator_runtime_excluded_from_trajectory": True,
    "clock": clock_metadata(),
    "provenance": "measured",
}, indent=2, sort_keys=True) + "\n")
PY
python3 - "$RAW_DIR/summary.json" "$RAW_DIR/eval.json" "$RAW_DIR/evaluation.json" "$RAW_DIR/status.json" "$RAW_DIR/run_manifest.json" "$EVALUATOR" "$COMMAND" "$RAW_DIR" "$AGENT_OUTPUT_DIR" "$RAW_DIR/resolved_command.json" <<'PY'
import hashlib, json, os, pathlib, sys, time
source_root = os.environ.get("AGENTIC_SOURCE_ROOT")
if source_root:
    sys.path.insert(0, str(pathlib.Path(source_root) / "src"))
from agentic_sim.telemetry.clock import clock_metadata

summary_path, eval_path, evaluation_path, status_path, manifest_path, evaluator, command, raw_dir, output_dir, resolved_path = sys.argv[1:]
summary = json.loads(pathlib.Path(summary_path).read_text(encoding="utf-8"))
evaluation = json.loads(pathlib.Path(eval_path).read_text(encoding="utf-8"))
resolved = json.loads(pathlib.Path(resolved_path).read_text(encoding="utf-8"))
pathlib.Path(evaluation_path).write_text(json.dumps(evaluation, indent=2, sort_keys=True) + "\n", encoding="utf-8")
pathlib.Path(status_path).write_text(json.dumps({
    "schema_version":"cr6.status.v2", "status":summary["status"], "run_id":summary["run_id"],
    "instance_id":summary["instance_id"], "attempt_id":summary["attempt_id"],
    "agent_returncode":summary["agent_returncode"], "evaluator_returncode":summary["evaluator_returncode"],
    "updated_at_utc":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
pathlib.Path(manifest_path).write_text(json.dumps({
    "schema_version":"cr6.run-manifest.v2", "artifact_contract_version": 2, "run_id":summary["run_id"],
    "instance_id":summary["instance_id"], "attempt_id":summary["attempt_id"], "mode":summary["mode"],
    "raw_dir":raw_dir, "sweagent_output_dir":output_dir,
    "agent_command_sha256":hashlib.sha256(command.encode()).hexdigest(),
    "evaluator_command_sha256":hashlib.sha256(evaluator.encode()).hexdigest(),
    "summary_sha256":hashlib.sha256(pathlib.Path(summary_path).read_bytes()).hexdigest(),
    "resolved_experiment":resolved["resolved"],
    "request_contract":resolved["request_contract"],
    "clock":clock_metadata(),
    "provenance":"measured",
    "observability_level":"control" if summary["mode"] == "uninstrumented" else "thin",
    "profilers_enabled":[] if summary["mode"] == "uninstrumented" else ["vllm_prometheus_interval", "nvidia_smi_interval"],
    "instrumentation_version":"obs-1",
    "native_vllm_metrics_scope":"server_aggregate",
    "native_vllm_request_correlation":False,
    "nvidia_smi_is_gpu_time":False,
}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
echo "first experiment status: agent_rc=$agent_rc evaluator_rc=$eval_rc artifacts=$RAW_DIR logs=$LOG_DIR"
if ((agent_rc != 0)); then exit "$agent_rc"; fi
if [[ "$eval_rc" != 0 ]]; then exit "$eval_rc"; fi
