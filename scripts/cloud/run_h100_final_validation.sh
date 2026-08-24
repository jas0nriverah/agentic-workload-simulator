#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
# Fail-closed/resumable H100 driver. The reviewed runner owns profiling.
ROOT="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
CONFIG="$ROOT/configs/h100_final_validation.json"
OUTPUT_DIR="$ROOT/artifacts/h100_final_validation"
PHASE=calibration
RUNNER=
PREDICTIONS_MANIFEST=
DRY_RUN=0
EXECUTE=0
ALLOW_H100=0
RESUME=0
EXPECTED_SERVER_CONTAINER="${H100_EXPECTED_SERVER_CONTAINER:-${H100_NSYS_CONTAINER:-h100-final-vllm}}"
EXPECTED_SERVER_SESSION="${H100_NSYS_SESSION:-h100-final-validation}"
PINNED_VLLM_IMAGE='vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271'
usage() {
  cat <<'USAGE'
Usage: run_h100_final_validation.sh [options]
  --config FILE --output-dir DIR --phase calibration|holdout
  --runner FILE --predictions-manifest FILE --dry-run --execute
  --allow-h100 --resume
USAGE
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -- "$1" | awk '{print $1}'
  else die 'sha256sum or shasum is required'; fi
}
while (($#)); do
  case "$1" in
    --config) (($# >= 2)) || die '--config requires a file'; CONFIG="$2"; shift 2;;
    --output-dir) (($# >= 2)) || die '--output-dir requires a directory'; OUTPUT_DIR="$2"; shift 2;;
    --phase) (($# >= 2)) || die '--phase requires calibration or holdout'; PHASE="$2"; shift 2;;
    --runner) (($# >= 2)) || die '--runner requires an executable'; RUNNER="$2"; shift 2;;
    --predictions-manifest) (($# >= 2)) || die '--predictions-manifest requires a file'; PREDICTIONS_MANIFEST="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    --execute) EXECUTE=1; shift;;
    --allow-h100) ALLOW_H100=1; shift;;
    --resume) RESUME=1; shift;;
    -h|--help) usage; exit 0;;
    *) die "unknown argument: $1";;
  esac
done
[[ "$PHASE" == calibration || "$PHASE" == holdout ]] || die '--phase must be calibration or holdout'
[[ -f "$CONFIG" ]] || die "protocol config is missing: $CONFIG"
command -v python3 >/dev/null 2>&1 || die 'python3 is required'
CONFIG_SUM="$(sha256_file "$CONFIG")"

CASE_LIST="$(python3 - "$CONFIG" "$PHASE" <<'PY'
import json, pathlib, sys
d = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")); phase = sys.argv[2]
if d.get("schema_version") != "h100-final-validation.v1" or d.get("launch_authorized") is not False: raise SystemExit("protocol schema/launch guard mismatch")
h = d.get("hardware", {}); names = " ".join(map(str, h.get("gpu_name_allowlist", [])))
if h.get("gpu_family") != "H100" or "H100" not in names or any(x in names for x in ("A100", "H200")): raise SystemExit("protocol is not H100-only")
cal, hold = d.get("calibration_configs"), d.get("sealed_holdouts")
if not isinstance(cal, list) or not 20 <= len(cal) <= 30: raise SystemExit("calibration count must be 20..30")
if not isinstance(hold, list) or not 10 <= len(hold) <= 12: raise SystemExit("holdout count must be 10..12")
rows = cal + hold; ids = [x.get("case_id") for x in rows]
if any(not isinstance(x, str) or not x for x in ids) or len(ids) != len(set(ids)): raise SystemExit("case IDs must be unique")
if any(x.get("split") != "calibration" for x in cal) or any(x.get("split") != "sealed_holdout" for x in hold): raise SystemExit("invalid split label")
if {x.get("holdout_kind") for x in hold} != {"interpolation", "extrapolation"}: raise SystemExit("both holdout kinds are required")
r = d.get("request_protocol", {})
if (r.get("concurrency"), r.get("warmup_requests"), r.get("measured_repetitions_per_case"), r.get("repetition_ids")) != (1, 2, 3, ["r01", "r02", "r03"]): raise SystemExit("request protocol must be serialized, 2 warmups, and 3 repeats")
required = {"name", "type", "unit", "formula", "source", "available_before_request", "allowed_for_fit"}
if not d.get("features") or any(not required.issubset(x) for x in d["features"]): raise SystemExit("feature definitions are incomplete")
feature_names = {x.get("name") for x in d["features"]}
required_features = {"prompt_tokens", "max_output_tokens", "context_tokens", "tool_calls", "hardware_score", "prompt_output_interaction", "concurrency", "warm_state"}
if not required_features.issubset(feature_names): raise SystemExit("feature definitions do not match the feature-only implementation contract")
for forbidden in {"wall_ms", "observed_seconds", "actual_prompt_tokens", "actual_completion_tokens", "generated_tokens", "completion_tokens"}:
    for feature in d["features"]:
        if feature.get("name") == forbidden and feature.get("allowed_for_fit"):
            raise SystemExit(f"measured/target-derived feature is allowed for fit: {forbidden}")
if not d.get("leakage_boundaries", {}).get("seal_before_run"): raise SystemExit("split is not sealed before run")
for x in (cal if phase == "calibration" else hold):
    print("\t".join((x["case_id"], x["split"], str(x["input_tokens"]), str(x["output_tokens"]))))
PY
)"
CAL_COUNT="$(python3 - "$CONFIG" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["calibration_configs"]))
PY
)"
HOLD_COUNT="$(python3 - "$CONFIG" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1], encoding="utf-8"))["sealed_holdouts"]))
PY
)"
printf 'Protocol SHA-256: %s\nPhase: %s (%s calibration, %s sealed holdouts; 3 repeats + 2 warmups)\n' "$CONFIG_SUM" "$PHASE" "$CAL_COUNT" "$HOLD_COUNT"

if (( DRY_RUN )); then
  printf 'DRY-RUN: no GPU inspection, server start, runner invocation, or artifact mutation\n'
  while IFS=$'\t' read -r case_id split input_tokens output_tokens; do
    [[ -n "$case_id" ]] || continue
    for repeat_id in r01 r02 r03; do printf 'DRY-RUN: %s %s %s/%s -> %s\n' "$split" "$case_id" "$input_tokens" "$output_tokens" "$repeat_id"; done
  done <<< "$CASE_LIST"
  [[ "$PHASE" != holdout || -n "$PREDICTIONS_MANIFEST" ]] || printf 'DRY-RUN: BLOCKED until --predictions-manifest is supplied\n'
  exit 0
fi
(( EXECUTE )) || die 'refusing execution without --execute (use --dry-run to inspect)'
(( ALLOW_H100 )) || die 'refusing execution without --allow-h100'
[[ -n "$RUNNER" && -x "$RUNNER" ]] || die "reviewed executable runner is required: $RUNNER"
GIT_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
[[ "$GIT_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || die 'a committed pre-run Git revision is required'
if [[ "$PHASE" == holdout ]]; then [[ -n "$PREDICTIONS_MANIFEST" && -f "$PREDICTIONS_MANIFEST" ]] || die 'holdout requires --predictions-manifest'; fi

command -v nvidia-smi >/dev/null 2>&1 || die 'nvidia-smi is required'
if [[ -n "$EXPECTED_SERVER_CONTAINER" ]]; then
  command -v docker >/dev/null 2>&1 || die 'docker is required when an existing profiled server is supplied'
fi
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
if [[ -n "$gpu_processes" ]]; then
  [[ -n "$EXPECTED_SERVER_CONTAINER" ]] || die "GPU already has compute processes: $gpu_processes"
  [[ "$(docker inspect --format '{{.State.Running}}' "$EXPECTED_SERVER_CONTAINER" 2>/dev/null || true)" == true ]] \
    || die "expected profiled server container is not running: $EXPECTED_SERVER_CONTAINER"
  server_image="$(docker inspect --format '{{.Config.Image}}' "$EXPECTED_SERVER_CONTAINER" 2>/dev/null || true)"
  [[ "$server_image" == "$PINNED_VLLM_IMAGE" ]] || die "profiled server image is not the pinned vLLM image: $server_image"
  server_command="$(docker inspect --format '{{json .Config.Cmd}}' "$EXPECTED_SERVER_CONTAINER" 2>/dev/null || true)"
  for required_arg in "--session-new=$EXPECTED_SERVER_SESSION" '--trace=cuda,osrt' '--cuda-event-trace=false' '--' 'vllm.entrypoints.openai.api_server' 'b2cff646eb4bb1d68355c01b18ae02e7cf42d120'; do
    grep -Fq -- "$required_arg" <<< "$server_command" || die "profiled server command is missing: $required_arg"
  done
  server_pids="$(docker top "$EXPECTED_SERVER_CONTAINER" -eo pid 2>/dev/null | awk 'NR > 1 {print $1}')"
  while IFS= read -r gpu_pid; do
    [[ -n "$gpu_pid" ]] || continue
    grep -Eq "(^|[[:space:]])${gpu_pid}([[:space:]]|$)" <<< "$server_pids" \
      || die "GPU process $gpu_pid is outside the expected profiled server container"
  done <<< "$gpu_processes"
  docker exec "$EXPECTED_SERVER_CONTAINER" /host-cuda/bin/nsys sessions list 2>/dev/null \
    | grep -Fq "$EXPECTED_SERVER_SESSION" \
    || die "expected Nsight session is not registered: $EXPECTED_SERVER_SESSION"
else
  [[ -z "$EXPECTED_SERVER_CONTAINER" ]] || die "expected profiled server has no visible GPU process: $EXPECTED_SERVER_CONTAINER"
fi

if [[ -e "$OUTPUT_DIR" ]]; then
  if (( RESUME )); then
    [[ -f "$OUTPUT_DIR/protocol.sha256" ]] || die "resume root has no protocol.sha256"
    [[ "$(awk 'NF {print $1; exit}' "$OUTPUT_DIR/protocol.sha256")" == "$CONFIG_SUM" ]] || die "protocol hash changed; refusing resume"
  elif [[ -d "$OUTPUT_DIR" && -z "$(find "$OUTPUT_DIR" -mindepth 1 -print -quit)" ]]; then
    : # A bind-mounted, empty trace root is safe to initialize.
  else
    die "output root exists; pass --resume after inspection: $OUTPUT_DIR"
  fi
else mkdir -p -- "$OUTPUT_DIR"; fi
if [[ "$PHASE" == holdout ]]; then
  [[ -f "$OUTPUT_DIR/run_state.json" ]] || die 'holdout requires a completed calibration run_state.json'
  python3 - "$OUTPUT_DIR/run_state.json" "$OUTPUT_DIR/calibration" "$CONFIG" <<'PY'
import json, pathlib, sys
state = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if state.get("status") != "completed" or state.get("completed_phase") != "calibration":
    raise SystemExit("calibration phase is not complete; holdout remains sealed")
root = pathlib.Path(sys.argv[2]); config = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
missing = []
for row in config["calibration_configs"]:
    for repeat in ("r01", "r02", "r03"):
        path = root / row["case_id"] / repeat / "row.json"
        if not path.is_file(): missing.append(str(path))
if missing: raise SystemExit("calibration artifacts are incomplete: " + ", ".join(missing[:3]))
PY
fi
mkdir -p -- "$OUTPUT_DIR/calibration" "$OUTPUT_DIR/holdout" "$OUTPUT_DIR/derived"
if [[ -e "$OUTPUT_DIR/protocol.config.json" ]]; then
  [[ "$(sha256_file "$OUTPUT_DIR/protocol.config.json")" == "$CONFIG_SUM" ]] || die "copied protocol differs"
else cp -- "$CONFIG" "$OUTPUT_DIR/protocol.config.json"; fi
if [[ -e "$OUTPUT_DIR/protocol.sha256" ]]; then
  [[ "$(awk 'NF {print $1; exit}' "$OUTPUT_DIR/protocol.sha256")" == "$CONFIG_SUM" ]] || die "protocol.sha256 mismatch"
else printf '%s  protocol.config.json\n' "$CONFIG_SUM" > "$OUTPUT_DIR/protocol.sha256"; fi

SPLIT_HASH="$(python3 - "$CONFIG" <<'PY'
import hashlib, json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
payload = json.dumps({"protocol_id": d["protocol_id"], "calibration_configs": d["calibration_configs"], "sealed_holdouts": d["sealed_holdouts"]}, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
PY
)"
if [[ -e "$OUTPUT_DIR/split_manifest.json" ]]; then
  old_split="$(python3 - "$OUTPUT_DIR/split_manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8")).get("split_sha256", ""))
PY
)"
  [[ "$old_split" == "$SPLIT_HASH" ]] || die "split manifest hash changed; refusing resume"
else
  python3 - "$OUTPUT_DIR/split_manifest.json" "$CONFIG_SUM" "$SPLIT_HASH" "$CONFIG" <<'PY'
import json, pathlib, sys, time
out, protocol_sha, split_sha, config = map(pathlib.Path, sys.argv[1:]); d = json.loads(config.read_text(encoding="utf-8"))
obj = {"schema_version": "h100-final-split.v1", "protocol_id": d["protocol_id"], "protocol_sha256": str(protocol_sha), "split_sha256": str(split_sha), "sealed": True, "sealed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "calibration_case_ids": [x["case_id"] for x in d["calibration_configs"]], "sealed_holdout_case_ids": [x["case_id"] for x in d["sealed_holdouts"]]}
tmp = out.with_name(out.name + ".tmp"); tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(out)
PY
fi

if [[ "$PHASE" == holdout ]]; then
  [[ "$PREDICTIONS_MANIFEST" == "$OUTPUT_DIR/derived/prediction_manifest.json" ]] || die 'holdout predictions must be the sealed manifest under this output root'
  [[ -f "$OUTPUT_DIR/derived/prediction_manifest.sha256" ]] || die 'prediction checksum sidecar is missing'
  PREDICTION_SHA256="$(sha256_file "$PREDICTIONS_MANIFEST")"
  [[ "$(awk 'NF {print $1; exit}' "$OUTPUT_DIR/derived/prediction_manifest.sha256")" == "$PREDICTION_SHA256" ]] || die 'prediction checksum sidecar mismatch'
  [[ -f "$OUTPUT_DIR/derived/feature_model.json" ]] || die 'feature model is missing; run calibration-only fit before holdout'
  python3 - "$PREDICTIONS_MANIFEST" "$OUTPUT_DIR/derived/feature_model.json" "$CONFIG" "$CONFIG_SUM" "$SPLIT_HASH" <<'PY'
import json, math, pathlib, sys
pred_path, model_path, config_path = map(pathlib.Path, sys.argv[1:4]); protocol_sha, split_sha = sys.argv[4:6]
d = json.loads(pred_path.read_text(encoding="utf-8")); model_obj = json.loads(model_path.read_text(encoding="utf-8")); config = json.loads(config_path.read_text(encoding="utf-8"))
if d.get("protocol_sha256") != protocol_sha or d.get("split_manifest_sha256") != split_sha: raise SystemExit("prediction manifest hash mismatch")
if model_obj.get("protocol_sha256") != protocol_sha or model_obj.get("split_manifest_sha256") != split_sha: raise SystemExit("feature model hash mismatch")
expected = [x["case_id"] for x in config["sealed_holdouts"]]; actual = [x.get("case_id") for x in d.get("predictions", [])]
if actual != expected or len(actual) != len(set(actual)): raise SystemExit("prediction manifest does not cover the exact ordered holdout matrix")
calibration = [x["case_id"] for x in config["calibration_configs"]]
if [x.get("case_id") for x in model_obj.get("calibration_case_medians", [])] != calibration: raise SystemExit("feature model was not fit on the exact calibration matrix")
if model_obj.get("fit_input_sha256") != d.get("fit_input_sha256"): raise SystemExit("prediction fit-input hash does not match feature model")
for row in d["predictions"]:
    value = row.get("predicted_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0: raise SystemExit("prediction contains an invalid predicted_seconds value")
def walk(x):
    if isinstance(x, dict):
        if any(k in x for k in ("wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms", "kineto_wall_ms", "kineto_cpu_ms", "kineto_cuda_ms", "measured_kineto_time_ms", "actual_prompt_tokens", "actual_completion_tokens", "observed_seconds")): raise SystemExit("prediction manifest contains a measured label")
        for v in x.values(): walk(v)
    elif isinstance(x, list):
        for v in x: walk(v)
walk(d)
PY
fi

STATE="$OUTPUT_DIR/run_state.json"
if [[ ! -e "$STATE" ]]; then
  python3 - "$STATE" "$CONFIG_SUM" "$SPLIT_HASH" "$PHASE" "$gpu_name" "$GIT_COMMIT" <<'PY'
import json, pathlib, sys, time
out, protocol_sha, split_sha, phase, gpu_name, git_commit = sys.argv[1:]
obj = {"schema_version": "h100-final-run-state.v1", "status": "running", "phase": phase, "protocol_sha256": protocol_sha, "split_manifest_sha256": split_sha, "pre_run_git_commit": git_commit, "gpu_name": gpu_name, "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "completed": [], "unavailable": []}
path = pathlib.Path(out); tmp = path.with_name(path.name + ".tmp"); tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(path)
PY
fi
update_state() {
  python3 - "$STATE" "$1" "$2" "$3" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); case_id, repeat_id, status = sys.argv[2:]
d = json.loads(p.read_text(encoding="utf-8")); bucket = "completed" if status == "completed" else "unavailable"; entry = {"case_id": case_id, "repeat_id": repeat_id}
if entry not in d.setdefault(bucket, []): d[bucket].append(entry)
tmp = p.with_name(p.name + ".tmp"); tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(p)
PY
}

while IFS=$'\t' read -r case_id split input_tokens output_tokens; do
  [[ -n "$case_id" ]] || continue
  for repeat_id in r01 r02 r03; do
    case_root="$OUTPUT_DIR/$([[ "$PHASE" == calibration ]] && echo calibration || echo holdout)/$case_id/$repeat_id"; row="$case_root/row.json"
    if [[ -e "$row" ]]; then
      (( RESUME )) || die "row exists without --resume: $row"
      status="$(python3 - "$row" "$case_id" "$split" "$repeat_id" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("case_id") != sys.argv[2] or d.get("split") != sys.argv[3] or d.get("repeat_id") != sys.argv[4]: raise SystemExit("existing row identity mismatch")
if d.get("status") not in {"completed", "unavailable"}: raise SystemExit("existing row is not terminal")
print(d["status"])
PY
)"
      update_state "$case_id" "$repeat_id" "$status"; [[ "$status" == completed ]] || die "existing unavailable row requires review: $row"; continue
    fi
    mkdir -p -- "$case_root"; printf 'RUN: %s %s %s/%s -> %s\n' "$split" "$case_id" "$input_tokens" "$output_tokens" "$repeat_id"
    set +e
    "$RUNNER" --config "$CONFIG" --case-id "$case_id" --split "$split" --input-tokens "$input_tokens" --output-tokens "$output_tokens" --repeat-id "$repeat_id" --output-dir "$case_root"
    runner_rc=$?; set -e
    [[ -f "$row" ]] || die "runner did not produce row.json: $row"
    status="$(python3 - "$row" "$case_id" "$split" "$repeat_id" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
if d.get("case_id") != sys.argv[2] or d.get("split") != sys.argv[3] or d.get("repeat_id") != sys.argv[4]: raise SystemExit("runner row identity/split mismatch")
if d.get("status") not in {"completed", "unavailable"}: raise SystemExit("runner row status must be completed or unavailable")
if d.get("status") == "completed" and not (d.get("artifact_sha256") or d.get("raw_artifact_sha256")): raise SystemExit("completed row lacks artifact SHA-256")
print(d["status"])
PY
)"
    update_state "$case_id" "$repeat_id" "$status"; (( runner_rc == 0 )) || die "runner failed; row preserved, no retry"; [[ "$status" == completed ]] || die "runner returned unavailable row"
  done
done <<< "$CASE_LIST"

if [[ "$PHASE" == holdout ]]; then
  [[ "$(sha256_file "$PREDICTIONS_MANIFEST")" == "$PREDICTION_SHA256" ]] || die 'prediction manifest changed during holdout collection'
  if [[ ! -e "$OUTPUT_DIR/holdout_reveal_receipt.json" ]]; then
    python3 - "$OUTPUT_DIR/holdout_reveal_receipt.json" "$PREDICTIONS_MANIFEST" "$CONFIG_SUM" "$SPLIT_HASH" <<'PY'
import hashlib, json, pathlib, sys, time
out, pred, protocol_sha, split_sha = map(pathlib.Path, sys.argv[1:])
obj = {"schema_version": "h100-holdout-reveal.v1", "protocol_sha256": str(protocol_sha), "split_manifest_sha256": str(split_sha), "prediction_manifest": str(pred), "prediction_manifest_sha256": hashlib.sha256(pred.read_bytes()).hexdigest(), "revealed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "labels_were_unavailable_to_fit": True}
tmp = out.with_name(out.name + ".tmp"); tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(out)
PY
  fi
fi

python3 - "$STATE" "$PHASE" <<'PY'
import json, pathlib, sys, time
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text(encoding="utf-8")); d["status"] = "completed"; d["completed_phase"] = sys.argv[2]; d["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
tmp = p.with_name(p.name + ".tmp"); tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n", encoding="utf-8"); tmp.replace(p)
PY
printf 'H100 final validation phase complete: %s (state %s)\n' "$PHASE" "$STATE"
