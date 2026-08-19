#!/usr/bin/env bash
# Immutable SWE-bench v4.1.0 gold-smoke runner; no free-form command strings.
set -Eeuo pipefail
IFS=$'\n\t'

env_or_empty() { printenv "$1" 2>/dev/null || true; }
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
WORK_ROOT="$(env_or_empty WORK_ROOT)"
[[ -n "$WORK_ROOT" ]] || WORK_ROOT="$ROOT/../agentic-work"
MANIFEST="$(env_or_empty LAMBDA_MANIFEST)"
[[ -n "$MANIFEST" ]] || MANIFEST="$ROOT/cloud/lambda/instance_manifest.env"
MANIFEST_EXPLICIT=0
DRY_RUN=0
SUITE=both
EXPERIMENT_TYPE=gold
PREDICTIONS_PATH=
GENERATED_OUTPUT_ROOT=

# CR4 interface handoff: these values cannot be overridden by the manifest.
EXPECTED_SWE_BENCH_REVISION=726c5461e2ef52d83cf1ea2107870a8bb3328d57
EXPECTED_LITE_DATASET_REPO=SWE-bench/SWE-bench_Lite
EXPECTED_LITE_DATASET_REVISION=69611d31007e1c6731db8bd5b5c3f2d33f5bab6e
EXPECTED_LITE_DATASET_SHA256=4c6a0f689c8b4ba32f4232d611b0c9a86d2fe379e4beb85c23d7c051f3652790
EXPECTED_VERIFIED_DATASET_REPO=SWE-bench/SWE-bench_Verified
EXPECTED_VERIFIED_DATASET_REVISION=91aa3ed51b709be6457e12d00300a6a596d4c6a3
EXPECTED_VERIFIED_DATASET_SHA256=889bccf7ada1a43d211050ac666f3b31032997209afb10dccdc6ea52128a8435
EXPECTED_LITE_INSTANCE_ID=astropy__astropy-14182
EXPECTED_VERIFIED_INSTANCE_ID=astropy__astropy-14365
EXPECTED_NAMESPACE=swebench
EXPECTED_PLATFORM=linux/amd64
EXPECTED_LITE_IMAGE=swebench/sweb.eval.x86_64.astropy_1776_astropy-14182:latest
EXPECTED_LITE_DIGEST=sha256:1caa6363958e49791e9dc4c838fbfd8e8e134b7992e20e90def10072cb920c25
EXPECTED_VERIFIED_IMAGE=swebench/sweb.eval.x86_64.astropy_1776_astropy-14365:latest
EXPECTED_VERIFIED_DIGEST=sha256:ac22529003ab4df5a84eb0e6be4b269b691c0f3b4aca582161bdfb581e1e9305

SWE_BENCH_REVISION="$(env_or_empty SWE_BENCH_REVISION)"
LITE_DATASET_REPO="$(env_or_empty LITE_DATASET_REPO)"
LITE_DATASET_REVISION="$(env_or_empty LITE_DATASET_REVISION)"
LITE_DATASET_SHA256="$(env_or_empty LITE_DATASET_SHA256)"
LITE_GOLD_DATASET_PATH="$(env_or_empty LITE_GOLD_DATASET_PATH)"
VERIFIED_DATASET_REPO="$(env_or_empty VERIFIED_DATASET_REPO)"
VERIFIED_DATASET_REVISION="$(env_or_empty VERIFIED_DATASET_REVISION)"
VERIFIED_DATASET_SHA256="$(env_or_empty VERIFIED_DATASET_SHA256)"
LITE_GOLD_DATASET_SHA256="$(env_or_empty LITE_GOLD_DATASET_SHA256)"
VERIFIED_GOLD_DATASET_SHA256="$(env_or_empty VERIFIED_GOLD_DATASET_SHA256)"
LITE_DATASET_PATH="$(env_or_empty LITE_DATASET_PATH)"
VERIFIED_DATASET_PATH="$(env_or_empty VERIFIED_DATASET_PATH)"
DATASET_MANIFEST_PATH="$(env_or_empty DATASET_MANIFEST_PATH)"
GOLD_LITE_INSTANCE_ID="$(env_or_empty GOLD_LITE_INSTANCE_ID)"
GOLD_VERIFIED_INSTANCE_ID="$(env_or_empty GOLD_VERIFIED_INSTANCE_ID)"
EVALUATOR_IMAGE_NAMESPACE="$(env_or_empty EVALUATOR_IMAGE_NAMESPACE)"
EVALUATOR_PLATFORM="$(env_or_empty EVALUATOR_PLATFORM)"
EVALUATOR_LITE_GOLD_IMAGE="$(env_or_empty EVALUATOR_LITE_GOLD_IMAGE)"
EVALUATOR_LITE_GOLD_DIGEST="$(env_or_empty EVALUATOR_LITE_GOLD_DIGEST)"
EVALUATOR_VERIFIED_GOLD_IMAGE="$(env_or_empty EVALUATOR_VERIFIED_GOLD_IMAGE)"
EVALUATOR_VERIFIED_GOLD_DIGEST="$(env_or_empty EVALUATOR_VERIFIED_GOLD_DIGEST)"
SWE_BENCH_EVALUATOR_ROOT="$(env_or_empty SWE_BENCH_EVALUATOR_ROOT)"
EVALUATOR_PYTHON="$(env_or_empty EVALUATOR_PYTHON)"
GOLD_OUTPUT_ROOT="$(env_or_empty GOLD_OUTPUT_ROOT)"
MAX_WORKERS="$(env_or_empty SWE_BENCH_MAX_WORKERS)"
TIMEOUT_SECONDS="$(env_or_empty SWE_BENCH_TIMEOUT_SECONDS)"
[[ -n "$MAX_WORKERS" ]] || MAX_WORKERS=1
[[ -n "$TIMEOUT_SECONDS" ]] || TIMEOUT_SECONDS=1800

usage() {
  cat <<'USAGE'
Usage: lambda_run_gold_smoke.sh [options]
  --manifest FILE
  --suite lite|verified|both
  --experiment-type gold|generated
  --predictions-path FILE
  --work-root DIR
  --dry-run
USAGE
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

load_manifest() {
  [[ -f "$MANIFEST" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(printf '%s' "$line" | sed 's/#.*$//')"
    [[ "$line" == *=* ]] || continue
    key="$(printf '%s' "$line" | cut -d= -f1 | tr -d '[:space:]')"
    value="$(printf '%s' "$line" | cut -d= -f2-)"
    case "$key" in
      WORK_ROOT) WORK_ROOT="$value" ;;
      SWE_BENCH_REVISION) SWE_BENCH_REVISION="$value" ;;
      LITE_DATASET_REPO) LITE_DATASET_REPO="$value" ;;
      LITE_DATASET_REVISION) LITE_DATASET_REVISION="$value" ;;
      LITE_DATASET_SHA256) LITE_DATASET_SHA256="$value" ;;
      LITE_GOLD_DATASET_PATH) LITE_GOLD_DATASET_PATH="$value" ;;
      VERIFIED_DATASET_REPO) VERIFIED_DATASET_REPO="$value" ;;
      VERIFIED_DATASET_REVISION) VERIFIED_DATASET_REVISION="$value" ;;
      VERIFIED_DATASET_SHA256) VERIFIED_DATASET_SHA256="$value" ;;
      LITE_GOLD_DATASET_SHA256) LITE_GOLD_DATASET_SHA256="$value" ;;
      VERIFIED_GOLD_DATASET_SHA256) VERIFIED_GOLD_DATASET_SHA256="$value" ;;
      LITE_DATASET_PATH) LITE_DATASET_PATH="$value" ;;
      VERIFIED_DATASET_PATH) VERIFIED_DATASET_PATH="$value" ;;
      DATASET_MANIFEST_PATH) DATASET_MANIFEST_PATH="$value" ;;
      GOLD_LITE_INSTANCE_ID) GOLD_LITE_INSTANCE_ID="$value" ;;
      GOLD_VERIFIED_INSTANCE_ID) GOLD_VERIFIED_INSTANCE_ID="$value" ;;
      EVALUATOR_IMAGE_NAMESPACE) EVALUATOR_IMAGE_NAMESPACE="$value" ;;
      EVALUATOR_PLATFORM) EVALUATOR_PLATFORM="$value" ;;
      EVALUATOR_LITE_GOLD_IMAGE) EVALUATOR_LITE_GOLD_IMAGE="$value" ;;
      EVALUATOR_LITE_GOLD_DIGEST) EVALUATOR_LITE_GOLD_DIGEST="$value" ;;
      EVALUATOR_VERIFIED_GOLD_IMAGE) EVALUATOR_VERIFIED_GOLD_IMAGE="$value" ;;
      EVALUATOR_VERIFIED_GOLD_DIGEST) EVALUATOR_VERIFIED_GOLD_DIGEST="$value" ;;
      SWE_BENCH_EVALUATOR_ROOT) SWE_BENCH_EVALUATOR_ROOT="$value" ;;
      EVALUATOR_PYTHON) EVALUATOR_PYTHON="$value" ;;
      GOLD_OUTPUT_ROOT) GOLD_OUTPUT_ROOT="$value" ;;
      GENERATED_OUTPUT_ROOT) GENERATED_OUTPUT_ROOT="$value" ;;
      SWE_BENCH_MAX_WORKERS) MAX_WORKERS="$value" ;;
      SWE_BENCH_TIMEOUT_SECONDS) TIMEOUT_SECONDS="$value" ;;
      *) ;;
    esac
  done < "$MANIFEST"
}

while (($#)); do
  case "$1" in
    --manifest)
      (($# >= 2)) || die '--manifest requires a file'
      MANIFEST="$2"; MANIFEST_EXPLICIT=1; shift 2 ;;
    --suite)
      (($# >= 2)) || die '--suite requires lite, verified, or both'
      SUITE="$2"; shift 2 ;;
    --experiment-type)
      (($# >= 2)) || die '--experiment-type requires gold or generated'
      EXPERIMENT_TYPE="$2"; shift 2 ;;
    --predictions-path)
      (($# >= 2)) || die '--predictions-path requires a path'
      PREDICTIONS_PATH="$2"; shift 2 ;;
    --work-root)
      (($# >= 2)) || die '--work-root requires a directory'
      WORK_ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

if [[ -f "$MANIFEST" ]]; then
  load_manifest
elif ((MANIFEST_EXPLICIT)); then
  die "reviewed evaluator manifest does not exist: $MANIFEST"
elif ((DRY_RUN)) && [[ -z "$SWE_BENCH_REVISION$LITE_DATASET_PATH$VERIFIED_DATASET_PATH" ]]; then
  printf 'DRY-RUN: BLOCKED; provide --manifest with pinned evaluator assets.\n'
  exit 0
fi

[[ -n "$LITE_DATASET_PATH" ]] || LITE_DATASET_PATH="$WORK_ROOT/datasets/lite_${EXPECTED_LITE_INSTANCE_ID}.json"
[[ -n "$LITE_GOLD_DATASET_PATH" ]] || LITE_GOLD_DATASET_PATH="$LITE_DATASET_PATH"
[[ -n "$VERIFIED_DATASET_PATH" ]] || VERIFIED_DATASET_PATH="$WORK_ROOT/datasets/verified_${EXPECTED_VERIFIED_INSTANCE_ID}.json"
[[ -n "$DATASET_MANIFEST_PATH" ]] || DATASET_MANIFEST_PATH="$WORK_ROOT/artifacts/manifests/datasets.json"
[[ -n "$SWE_BENCH_EVALUATOR_ROOT" ]] || SWE_BENCH_EVALUATOR_ROOT="$WORK_ROOT/repos/SWE-bench"
[[ -n "$EVALUATOR_PYTHON" ]] || EVALUATOR_PYTHON="$WORK_ROOT/venv/bin/python"
[[ -n "$GOLD_OUTPUT_ROOT" ]] || GOLD_OUTPUT_ROOT="$WORK_ROOT/artifacts/gold-smoke"
[[ -n "$GENERATED_OUTPUT_ROOT" ]] || GENERATED_OUTPUT_ROOT="$WORK_ROOT/artifacts/generated-evaluation"

[[ "$SUITE" == lite || "$SUITE" == verified || "$SUITE" == both ]] || die 'suite must be lite, verified, or both'
[[ "$EXPERIMENT_TYPE" == gold || "$EXPERIMENT_TYPE" == generated ]] || die 'experiment type must be gold or generated'
if [[ "$EXPERIMENT_TYPE" == generated ]]; then
  [[ -n "$PREDICTIONS_PATH" && -f "$PREDICTIONS_PATH" ]] || die 'generated evaluation requires an existing --predictions-path'
fi
[[ "$SWE_BENCH_REVISION" == "$EXPECTED_SWE_BENCH_REVISION" ]] || die "SWE_BENCH_REVISION must be $EXPECTED_SWE_BENCH_REVISION"
[[ "$EVALUATOR_IMAGE_NAMESPACE" == "$EXPECTED_NAMESPACE" ]] || die "EVALUATOR_IMAGE_NAMESPACE must be $EXPECTED_NAMESPACE"
[[ "$EVALUATOR_PLATFORM" == "$EXPECTED_PLATFORM" ]] || die "EVALUATOR_PLATFORM must be $EXPECTED_PLATFORM"
[[ "$LITE_DATASET_SHA256" == "$EXPECTED_LITE_DATASET_SHA256" ]] || die "LITE_DATASET_SHA256 must be the reviewed manifest hash $EXPECTED_LITE_DATASET_SHA256"
[[ "$VERIFIED_DATASET_SHA256" == "$EXPECTED_VERIFIED_DATASET_SHA256" ]] || die "VERIFIED_DATASET_SHA256 must be the reviewed manifest hash $EXPECTED_VERIFIED_DATASET_SHA256"
if (( ! DRY_RUN )); then
  [[ -d "$SWE_BENCH_EVALUATOR_ROOT" ]] || die "pinned SWE-bench checkout is missing: $SWE_BENCH_EVALUATOR_ROOT"
  command -v "$EVALUATOR_PYTHON" >/dev/null 2>&1 || die "evaluator Python is unavailable: $EVALUATOR_PYTHON"
fi
[[ "$MAX_WORKERS" =~ ^[1-9][0-9]*$ ]] || die 'SWE_BENCH_MAX_WORKERS must be positive'
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die 'SWE_BENCH_TIMEOUT_SECONDS must be positive'
if (( ! DRY_RUN )) && [[ -d "$SWE_BENCH_EVALUATOR_ROOT/.git" ]]; then
  evaluator_revision="$(git -C "$SWE_BENCH_EVALUATOR_ROOT" rev-parse HEAD 2>/dev/null || true)"
  [[ "$evaluator_revision" == "$EXPECTED_SWE_BENCH_REVISION" ]] || die 'evaluator checkout revision mismatch'
elif (( ! DRY_RUN )) && [[ ! -f "$SWE_BENCH_EVALUATOR_ROOT/swebench/harness/run_evaluation.py" ]]; then
  die 'evaluator checkout is neither a pinned Git checkout nor a complete source tree'
fi

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -- "$1" | awk '{print $1}'
  else die 'sha256sum or shasum is required for dataset asset verification'; fi
}

validate_dataset() {
  local dataset_path="$1" instance_id="$2" image_ref="$3" suite="$4" expected_hash="$5" expected_selected_hash="$6" expected_repo="$7" expected_revision="$8" expected_rows="$9"
  [[ -f "$dataset_path" ]] || die "$suite dataset asset does not exist: $dataset_path"
  case "$dataset_path" in *.json|*.jsonl) ;; *) die "$suite dataset must be local .json or .jsonl" ;; esac
  [[ -f "$DATASET_MANIFEST_PATH" ]] || die "$suite dataset manifest asset does not exist: $DATASET_MANIFEST_PATH"
  [[ "$expected_selected_hash" =~ ^[0-9a-fA-F]{64}$ ]] || die "$suite selected-row hash must be a 64-hex SHA-256"
  python3 - "$dataset_path" "$instance_id" "$image_ref" "$suite" "$DATASET_MANIFEST_PATH" "$expected_repo" "$expected_revision" "$expected_rows" "$expected_hash" "$expected_selected_hash" <<'PY'
import hashlib
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
instance_id, image_ref, suite, manifest_path, expected_repo, expected_revision, expected_rows, expected_hash, expected_selected_hash = sys.argv[2:]
if path.suffix == ".json":
    rows = json.loads(path.read_text(encoding="utf-8"))
else:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if not isinstance(rows, list) or not rows:
    raise SystemExit(f"{suite} dataset asset is empty or not a row list")
matches = [row for row in rows if isinstance(row, dict) and row.get("instance_id") == instance_id]
if len(matches) != 1:
    raise SystemExit(f"{suite} dataset must contain exactly one row for {instance_id}; found {len(matches)}")
row = matches[0]
required = {"instance_id", "patch", "repo", "version", "base_commit", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"}
missing = sorted(required.difference(row))
if missing:
    raise SystemExit(f"{suite} row {instance_id} is missing evaluator fields: {', '.join(missing)}")
if "image" in row and row["image"] != image_ref:
    raise SystemExit(f"{suite} row image does not match pinned evaluator image")
if not isinstance(row["patch"], str):
    raise SystemExit(f"{suite} row {instance_id} has a non-string gold patch")
manifest = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
section = manifest.get(suite)
if not isinstance(section, dict):
    raise SystemExit(f"dataset manifest is missing the {suite} section")
if section.get("repo") != expected_repo or section.get("revision") != expected_revision:
    raise SystemExit(f"{suite} dataset manifest revision/repository does not match the reviewed pin")
if section.get("split") != "test" or section.get("rows") != int(expected_rows):
    raise SystemExit(f"{suite} dataset manifest split or row count is not the reviewed value")
selected = [item for item in section.get("selected", []) if item.get("instance_id") == instance_id]
if len(selected) != 1:
    raise SystemExit(f"dataset manifest must select exactly one {suite} row for {instance_id}")
selected_path = pathlib.Path(selected[0].get("path", "")).resolve()
if selected_path != path.resolve():
    raise SystemExit(f"dataset manifest path does not match the {suite} evaluator asset")
canonical = json.dumps([row], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
if hashlib.sha256(canonical).hexdigest() != selected[0].get("sha256"):
    raise SystemExit(f"{suite} evaluator asset hash does not match datasets.json")
if selected[0].get("sha256") != expected_selected_hash:
    raise SystemExit(f"{suite} evaluator asset hash does not match the reviewed selected-row hash")
if expected_hash not in {
    "4c6a0f689c8b4ba32f4232d611b0c9a86d2fe379e4beb85c23d7c051f3652790",
    "889bccf7ada1a43d211050ac666f3b31032997209afb10dccdc6ea52128a8435",
}:
    raise SystemExit(f"{suite} dataset manifest hash is not reviewed")
print(f"validated {suite} dataset row {instance_id}")
PY
}

validate_image() {
  local suite="$1" image="$2" digest="$3" expected_image="$4" expected_digest="$5"
  [[ "$image" == "$expected_image" ]] || die "$suite image reference mismatch"
  [[ "$digest" == "$expected_digest" ]] || die "$suite image digest mismatch"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || die "$suite image digest must be lowercase sha256:<64 hex digits>"
}

[[ "$GOLD_LITE_INSTANCE_ID" == "$EXPECTED_LITE_INSTANCE_ID" ]] || die "GOLD_LITE_INSTANCE_ID must be $EXPECTED_LITE_INSTANCE_ID"
[[ "$GOLD_VERIFIED_INSTANCE_ID" == "$EXPECTED_VERIFIED_INSTANCE_ID" ]] || die "GOLD_VERIFIED_INSTANCE_ID must be $EXPECTED_VERIFIED_INSTANCE_ID"
[[ "$GOLD_LITE_INSTANCE_ID" != "$GOLD_VERIFIED_INSTANCE_ID" ]] || die 'Lite and Verified gold instance IDs must be distinct'

if [[ "$SUITE" == lite || "$SUITE" == both ]]; then
  [[ "$LITE_DATASET_REPO" == "$EXPECTED_LITE_DATASET_REPO" ]] || die 'Lite dataset repository mismatch'
  [[ "$LITE_DATASET_REVISION" == "$EXPECTED_LITE_DATASET_REVISION" ]] || die 'Lite dataset revision mismatch'
  validate_image lite "$EVALUATOR_LITE_GOLD_IMAGE" "$EVALUATOR_LITE_GOLD_DIGEST" "$EXPECTED_LITE_IMAGE" "$EXPECTED_LITE_DIGEST"
  validate_dataset "$LITE_GOLD_DATASET_PATH" "$GOLD_LITE_INSTANCE_ID" "$EVALUATOR_LITE_GOLD_IMAGE" lite "$LITE_DATASET_SHA256" "$LITE_GOLD_DATASET_SHA256" "$EXPECTED_LITE_DATASET_REPO" "$EXPECTED_LITE_DATASET_REVISION" 300
fi
if [[ "$SUITE" == verified || "$SUITE" == both ]]; then
  [[ "$VERIFIED_DATASET_REPO" == "$EXPECTED_VERIFIED_DATASET_REPO" ]] || die 'Verified dataset repository mismatch'
  [[ "$VERIFIED_DATASET_REVISION" == "$EXPECTED_VERIFIED_DATASET_REVISION" ]] || die 'Verified dataset revision mismatch'
  validate_image verified "$EVALUATOR_VERIFIED_GOLD_IMAGE" "$EVALUATOR_VERIFIED_GOLD_DIGEST" "$EXPECTED_VERIFIED_IMAGE" "$EXPECTED_VERIFIED_DIGEST"
  validate_dataset "$VERIFIED_DATASET_PATH" "$GOLD_VERIFIED_INSTANCE_ID" "$EVALUATOR_VERIFIED_GOLD_IMAGE" verified "$VERIFIED_DATASET_SHA256" "$VERIFIED_GOLD_DATASET_SHA256" "$EXPECTED_VERIFIED_DATASET_REPO" "$EXPECTED_VERIFIED_DATASET_REVISION" 500
fi

if [[ "$EXPERIMENT_TYPE" == generated ]]; then
  python3 - "$PREDICTIONS_PATH" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
values = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if isinstance(values, dict):
    values = list(values.values())
if not isinstance(values, list) or not values:
    raise SystemExit("predictions must be a non-empty list or mapping")
for value in values:
    if not isinstance(value, dict) or not value.get("instance_id") or not value.get("model_name_or_path") or "model_patch" not in value:
        raise SystemExit("each prediction must contain instance_id, model_name_or_path, and model_patch")
PY
fi

print_image_check() {
  local suite="$1" image="$2" digest="$3" image_repo="${2%%:*}"
  printf 'IMAGE_CHECK[%s]: docker image inspect --format %q %q; require platform %q\n' "$suite" '{{.Os}}/{{.Architecture}}' "$image" "$EXPECTED_PLATFORM"
  printf 'IMAGE_CHECK[%s]: docker image inspect --format %q %q; require RepoDigests contains %q\n' "$suite" '{{join .RepoDigests "\n"}}' "$image" "$image_repo@$digest"
}

run_suite() {
  local suite="$1" dataset_path="$2" instance_id="$3" image="$4" digest="$5"
  local run_prefix=gold output_root="$GOLD_OUTPUT_ROOT"
  if [[ "$EXPERIMENT_TYPE" == generated ]]; then
    run_prefix=generated
    output_root="$GENERATED_OUTPUT_ROOT"
  fi
  local run_id="$run_prefix-$suite-$instance_id"
  local suite_root="$output_root/$suite-$instance_id"
  local report_dir="$suite_root/evaluation"
  local log_path="$suite_root/evaluator.log"
  local status_path="$suite_root/status.json"
  local manifest_path="$suite_root/run_manifest.json"
  local predictions=gold
  [[ "$EXPERIMENT_TYPE" == generated ]] && predictions="$PREDICTIONS_PATH"
  print_image_check "$suite" "$image" "$digest"
  printf 'ARTIFACT_ROOT[%s]: %s\n' "$suite" "$suite_root"
  printf 'COMMAND[%s]: ' "$suite"
  printf '%q ' "$EVALUATOR_PYTHON" -m swebench.harness.run_evaluation --dataset_name "$dataset_path" --split test --predictions_path "$predictions" --instance_ids "$instance_id" --max_workers "$MAX_WORKERS" --timeout "$TIMEOUT_SECONDS" --cache_level instance --clean False --run_id "$run_id" --namespace "$EVALUATOR_IMAGE_NAMESPACE" --instance_image_tag latest --env_image_tag latest --report_dir "$report_dir"
  printf '\n'
  ((DRY_RUN)) && return 0
  command -v docker >/dev/null 2>&1 || die 'Docker is required for official SWE-bench evaluation'
  local platform repo_digests image_repo
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$image" 2>/dev/null || true)"
  [[ "$platform" == "$EXPECTED_PLATFORM" ]] || die "$suite image platform mismatch"
  repo_digests="$(docker image inspect --format '{{join .RepoDigests "\n"}}' "$image" 2>/dev/null || true)"
  image_repo="${image%%:*}"
  grep -Fqx "$image_repo@$digest" <<<"$repo_digests" || die "$suite image is not present at the pinned digest"
  [[ ! -e "$suite_root" ]] || die "gold output already exists: $suite_root"
  mkdir -p -- "$report_dir"
  python3 - "$manifest_path" "$suite" "$EXPERIMENT_TYPE" "$run_id" "$instance_id" "$SWE_BENCH_REVISION" "$dataset_path" "$image" "$digest" "$report_dir" "$log_path" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone
output, suite, experiment_type, run_id, instance_id, evaluator_revision, dataset_path, image, digest, report_dir, log_path = sys.argv[1:]
pathlib.Path(output).write_text(json.dumps({
    "schema_version": "gold-smoke-run.v1", "status": "started",
    "experiment_type": experiment_type, "suite": suite, "run_id": run_id,
    "instance_id": instance_id, "evaluator_revision": evaluator_revision,
    "dataset_path": dataset_path,
    "image": {"ref": image, "digest": digest, "platform": "linux/amd64"},
    "report_dir": report_dir, "log_path": log_path,
    "started_at_utc": datetime.now(timezone.utc).isoformat(),
}, indent=2) + "\n", encoding="utf-8")
PY
  local evaluator_rc=0
  set +e
  ( cd -- "$suite_root"; "$EVALUATOR_PYTHON" -m swebench.harness.run_evaluation --dataset_name "$dataset_path" --split test --predictions_path "$predictions" --instance_ids "$instance_id" --max_workers "$MAX_WORKERS" --timeout "$TIMEOUT_SECONDS" --cache_level instance --clean False --run_id "$run_id" --namespace "$EVALUATOR_IMAGE_NAMESPACE" --instance_image_tag latest --env_image_tag latest --report_dir "$report_dir" ) 2>&1 | tee -- "$log_path"
  evaluator_rc="$?"
  set -e
  # v4.1.0's pinned source writes `<model>.<run_id>.json` in the evaluator
  # working directory.  Keep that exact path first, but also accept the
  # report_dir/results.json layout used by compatible harness builds so the
  # wrapper cannot turn a completed evaluator into a false error solely due
  # to report placement.
  local report_model=gold
  if [[ "$EXPERIMENT_TYPE" == generated ]]; then
    report_model="$(python3 - "$predictions" <<'PY'
import json
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
values = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
if isinstance(values, dict):
    values = list(values.values())
if len(values) != 1 or not isinstance(values[0], dict) or not values[0].get("model_name_or_path"):
    raise SystemExit("generated predictions must contain exactly one model_name_or_path for report binding")
print(str(values[0]["model_name_or_path"]).replace("/", "__"))
PY
)"
  fi
  local report_path="$suite_root/$report_model.$run_id.json"
  if [[ ! -f "$report_path" && -f "$report_dir/results.json" ]]; then
    report_path="$report_dir/results.json"
  fi
  local classification=evaluator_error
  if ((evaluator_rc == 0)) && [[ -f "$report_path" ]]; then
    classification="$(python3 - "$report_path" <<'PY'
import json
import pathlib
import sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    if any(not isinstance(value.get(key), int) for key in ("total_instances", "submitted_instances", "completed_instances")):
        raise ValueError("missing integer summary fields")
    if value["total_instances"] != 1 or value["submitted_instances"] != 1 or value["completed_instances"] != 1:
        raise ValueError("summary does not describe one completed instance")
    if value.get("error_ids"):
        raise ValueError("official evaluator reported error_ids")
    if value.get("resolved_instances") == 1:
        print("resolved")
    elif value.get("unresolved_instances") == 1:
        print("unresolved")
    else:
        raise ValueError("summary has neither resolved nor unresolved result")
except Exception as exc:
    print(f"invalid:{exc}")
PY
)"
    [[ "$classification" == resolved || "$classification" == unresolved ]] || classification=evaluator_error
  fi
  python3 - "$status_path" "$classification" "$evaluator_rc" "$report_path" "$run_id" "$suite" "$instance_id" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone
output, status, rc, report, run_id, suite, instance_id = sys.argv[1:]
pathlib.Path(output).write_text(json.dumps({
    "schema_version": "gold-smoke-status.v1", "status": status,
    "evaluator_exit_code": int(rc), "report_path": report,
    "run_id": run_id, "suite": suite, "instance_id": instance_id,
    "finished_at_utc": datetime.now(timezone.utc).isoformat(),
}, indent=2) + "\n", encoding="utf-8")
PY
  printf 'RESULT[%s]: %s (status=%s)\n' "$suite" "$report_path" "$classification"
  case "$classification" in resolved) return 0 ;; unresolved) return 3 ;; *) return 1 ;; esac
}

if ((DRY_RUN)); then
  printf 'DRY-RUN: official SWE-bench v4.1.0 %s evaluator contract; no evaluator or Docker command executes.\n' "$EXPERIMENT_TYPE"
fi
case "$SUITE" in
  lite) run_suite lite "$LITE_GOLD_DATASET_PATH" "$GOLD_LITE_INSTANCE_ID" "$EVALUATOR_LITE_GOLD_IMAGE" "$EVALUATOR_LITE_GOLD_DIGEST" ;;
  verified) run_suite verified "$VERIFIED_DATASET_PATH" "$GOLD_VERIFIED_INSTANCE_ID" "$EVALUATOR_VERIFIED_GOLD_IMAGE" "$EVALUATOR_VERIFIED_GOLD_DIGEST" ;;
  both)
    run_suite lite "$LITE_GOLD_DATASET_PATH" "$GOLD_LITE_INSTANCE_ID" "$EVALUATOR_LITE_GOLD_IMAGE" "$EVALUATOR_LITE_GOLD_DIGEST"
    run_suite verified "$VERIFIED_DATASET_PATH" "$GOLD_VERIFIED_INSTANCE_ID" "$EVALUATOR_VERIFIED_GOLD_IMAGE" "$EVALUATOR_VERIFIED_GOLD_DIGEST"
    ;;
esac
