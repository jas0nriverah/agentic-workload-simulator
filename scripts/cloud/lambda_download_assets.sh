#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"
WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"
CACHE_ROOT="${CACHE_ROOT:-$WORK_ROOT/cache}"
PYTHON_ENV_MODE="${PYTHON_ENV_MODE:-venv}"
PYTHON_ENV_ROOT="${PYTHON_ENV_ROOT:-}"
MODEL="${VLLM_MODEL:-Qwen/Qwen3-Coder-30B-A3B-Instruct}"
REVISION="${VLLM_MODEL_REVISION:-b2cff646eb4bb1d68355c01b18ae02e7cf42d120}"
MIN_FREE_GIB="${MIN_FREE_GIB:-120}"
DRY=0
LITE_REPO="${LITE_DATASET_REPO:-SWE-bench/SWE-bench_Lite}"
LITE_REVISION="${LITE_DATASET_REVISION:-69611d31007e1c6731db8bd5b5c3f2d33f5bab6e}"
LITE_ROWS="${LITE_DATASET_ROWS:-300}"
LITE_FIRST_ID="${FIRST_LITE_INSTANCE_ID:-astropy__astropy-12907}"
LITE_GOLD_ID="${GOLD_LITE_INSTANCE_ID:-astropy__astropy-14182}"
VERIFIED_REPO="${VERIFIED_DATASET_REPO:-SWE-bench/SWE-bench_Verified}"
VERIFIED_REVISION="${VERIFIED_DATASET_REVISION:-91aa3ed51b709be6457e12d00300a6a596d4c6a3}"
VERIFIED_ROWS="${VERIFIED_DATASET_ROWS:-500}"
VERIFIED_GOLD_ID="${GOLD_VERIFIED_INSTANCE_ID:-astropy__astropy-14365}"

usage() { echo 'Usage: lambda_download_assets.sh [--manifest FILE] [--work-root DIR] [--dry-run]'; }
while (($#)); do
  case "$1" in
    --dry-run) DRY=1; shift;;
    --manifest) [[ $# -gt 1 ]] || { echo '--manifest requires a file' >&2; exit 2; }; MANIFEST="$2"; shift 2;;
    --manifest=*) MANIFEST="${1#*=}"; shift;;
    --work-root) [[ $# -gt 1 ]] || { echo '--work-root requires a directory' >&2; exit 2; }; WORK_ROOT="$2"; shift 2;;
    --work-root=*) WORK_ROOT="${1#*=}"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; exit 2;;
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
for key in CACHE_ROOT PYTHON_ENV_MODE PYTHON_ENV_ROOT VLLM_MODEL VLLM_MODEL_REVISION LITE_DATASET_REPO LITE_DATASET_REVISION LITE_DATASET_ROWS FIRST_LITE_INSTANCE_ID GOLD_LITE_INSTANCE_ID VERIFIED_DATASET_REPO VERIFIED_DATASET_REVISION VERIFIED_DATASET_ROWS GOLD_VERIFIED_INSTANCE_ID MIN_FREE_GIB; do
  value="$(manifest_value "$key")"; [[ -n "$value" ]] || continue
  case "$key" in
    CACHE_ROOT) CACHE_ROOT="$value";; PYTHON_ENV_MODE) PYTHON_ENV_MODE="$value";; PYTHON_ENV_ROOT) PYTHON_ENV_ROOT="$value";; VLLM_MODEL) MODEL="$value";; VLLM_MODEL_REVISION) REVISION="$value";;
    LITE_DATASET_REPO) LITE_REPO="$value";; LITE_DATASET_REVISION) LITE_REVISION="$value";; LITE_DATASET_ROWS) LITE_ROWS="$value";;
    FIRST_LITE_INSTANCE_ID) LITE_FIRST_ID="$value";; GOLD_LITE_INSTANCE_ID) LITE_GOLD_ID="$value";;
    VERIFIED_DATASET_REPO) VERIFIED_REPO="$value";; VERIFIED_DATASET_REVISION) VERIFIED_REVISION="$value";; VERIFIED_DATASET_ROWS) VERIFIED_ROWS="$value";;
    GOLD_VERIFIED_INSTANCE_ID) VERIFIED_GOLD_ID="$value";; MIN_FREE_GIB) MIN_FREE_GIB="$value";;
  esac
done
case "$PYTHON_ENV_MODE" in
  managed)
    [[ -n "$PYTHON_ENV_ROOT" ]] || { echo 'managed Python environment root is missing' >&2; exit 1; }
    ;;
  venv)
    PYTHON_ENV_ROOT="${PYTHON_ENV_ROOT:-$WORK_ROOT/venv}"
    ;;
  *) echo "PYTHON_ENV_MODE must be managed or venv: $PYTHON_ENV_MODE" >&2; exit 1;;
esac

[[ "$REVISION" =~ ^[0-9a-fA-F]{40}$ ]] || { echo 'immutable 40-hex VLLM_MODEL_REVISION is required' >&2; exit 1; }
[[ "$LITE_REVISION" =~ ^[0-9a-fA-F]{40}$ && "$VERIFIED_REVISION" =~ ^[0-9a-fA-F]{40}$ ]] || { echo 'immutable dataset revisions are required' >&2; exit 1; }
for id in "$LITE_FIRST_ID" "$LITE_GOLD_ID" "$VERIFIED_GOLD_ID"; do
  [[ "$id" =~ ^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+$ ]] || { echo "invalid instance id: $id" >&2; exit 1; }
done

HF_HOME="${HF_HOME:-$CACHE_ROOT/huggingface}"
HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
HF_DATASETS_CACHE="${HF_DATASETS_CACHE:-$CACHE_ROOT/datasets-cache}"
if [[ "$PYTHON_ENV_MODE" == managed ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$PYTHON_ENV_ROOT/bin/python3}"
  # Prefer the CLI installed alongside the resolved managed interpreter. The
  # mutating path falls back to the user's pinned installation location when
  # the managed environment exposes Python but not console scripts.
  HF_CLI="${HF_CLI:-$PYTHON_ENV_ROOT/bin/hf}"
else
  PYTHON_BIN="${PYTHON_BIN:-$PYTHON_ENV_ROOT/bin/python}"
  HF_CLI="${HF_CLI:-$PYTHON_ENV_ROOT/bin/hf}"
fi
MODEL_OUT="$WORK_ROOT/artifacts/manifests/model_download.json"
DATASET_OUT="$WORK_ROOT/artifacts/manifests/datasets.json"

if (( DRY )); then
  cat <<EOF
DRY-RUN: verify at least ${MIN_FREE_GIB} GiB free, then download $MODEL@$REVISION with $HF_CLI into $HF_HUB_CACHE (resumable, no floating revision).
DRY-RUN: load $LITE_REPO@$LITE_REVISION split=test; assert $LITE_ROWS rows and IDs $LITE_FIRST_ID/$LITE_GOLD_ID.
DRY-RUN: load $VERIFIED_REPO@$VERIFIED_REVISION split=test; assert $VERIFIED_ROWS rows and ID $VERIFIED_GOLD_ID.
DRY-RUN: write exact one-row task/evaluator JSON files and manifests under $WORK_ROOT/datasets and $WORK_ROOT/artifacts/manifests; no patch is used as a result.
EOF
  exit 0
fi

command -v "$PYTHON_BIN" >/dev/null 2>&1 || { echo "pinned Python is unavailable: $PYTHON_BIN" >&2; exit 1; }
if [[ "$PYTHON_ENV_MODE" == managed && ! -x "$HF_CLI" && -x "$HOME/.local/bin/hf" ]]; then
  HF_CLI="$HOME/.local/bin/hf"
fi
command -v "$HF_CLI" >/dev/null 2>&1 || { echo "huggingface_hub CLI is unavailable: $HF_CLI" >&2; exit 1; }
mkdir -p -- "$HF_HUB_CACHE" "$HF_DATASETS_CACHE" "$WORK_ROOT/datasets" "$WORK_ROOT/logs" "$WORK_ROOT/artifacts/manifests"
free_kib="$(df -Pk "$CACHE_ROOT" | awk 'NR==2 {print $4}')"; min_kib=$((MIN_FREE_GIB * 1024 * 1024)); [[ "$free_kib" =~ ^[0-9]+$ && "$free_kib" -ge "$min_kib" ]] || { echo "insufficient free space before asset download" >&2; exit 1; }

start="$(date +%s)"; export HF_HOME HF_HUB_CACHE HF_DATASETS_CACHE
"$HF_CLI" download "$MODEL" --repo-type model --revision "$REVISION" --cache-dir "$HF_HUB_CACHE" --exclude '*optimizer*' --exclude '*checkpoint*' 2>&1 | tee "$WORK_ROOT/logs/model_download.log"
end="$(date +%s)"
snapshot="$(find "$HF_HUB_CACHE" -type d -path "*/snapshots/$REVISION" -print -quit 2>/dev/null || true)"
[[ -n "$snapshot" && -d "$snapshot" ]] || { echo 'immutable model snapshot not found' >&2; exit 1; }
[[ -s "$snapshot/config.json" ]] || { echo 'model config.json missing' >&2; exit 1; }
# Hugging Face snapshots use relative symlinks into the content-addressed
# blobs directory. Follow those links when counting the measured weight files.
shard_count="$(find -L "$snapshot" -maxdepth 1 -type f -name '*.safetensors' | wc -l | tr -d ' ')"; [[ "$shard_count" =~ ^[0-9]+$ && "$shard_count" -gt 0 ]] || { echo 'model safetensors are missing' >&2; exit 1; }
"$PYTHON_BIN" - "$MODEL_OUT" "$MODEL" "$REVISION" "$snapshot" "$start" "$end" "$shard_count" <<'PY'
import json, pathlib, sys
out, model, revision, snapshot, start, end, shards = sys.argv[1:]
root = pathlib.Path(snapshot)
files = [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size} for p in sorted(root.rglob("*")) if p.is_file()]
obj = {"schema_version":"model-download.v3", "model":model, "revision":revision, "snapshot":str(root), "duration_seconds":int(end)-int(start), "safetensor_shards":int(shards), "total_bytes":sum(x["bytes"] for x in files), "files":files, "provenance":"measured"}
p = pathlib.Path(out); tmp = p.with_name(p.name + ".tmp"); tmp.write_text(json.dumps(obj, indent=2) + "\n"); tmp.replace(p)
PY

# Resolve exact dataset revisions once and retain only the selected rows needed
# for the first trajectory and the two independent gold smokes.
"$PYTHON_BIN" - "$DATASET_OUT" "$WORK_ROOT/datasets" "$HF_DATASETS_CACHE" "$LITE_REPO" "$LITE_REVISION" "$LITE_ROWS" "$LITE_FIRST_ID" "$LITE_GOLD_ID" "$VERIFIED_REPO" "$VERIFIED_REVISION" "$VERIFIED_ROWS" "$VERIFIED_GOLD_ID" <<'PY'
import hashlib, json, pathlib, sys

import pyarrow.parquet as parquet
from huggingface_hub import hf_hub_download

(out, root, cache_dir, lite_repo, lite_rev, lite_rows, lite_first, lite_gold,
 verified_repo, verified_rev, verified_rows, verified_gold) = sys.argv[1:]
root = pathlib.Path(root)
def get(repo, rev, expected, ids, stem):
    # The managed Studio base carries a SciPy binary built for NumPy 1.x,
    # while the frozen workload lock pins NumPy 2.x.  Importing datasets
    # triggers SciPy even for Parquet, so use the pinned HF client plus the
    # already-present Parquet reader and preserve the same revision/row checks.
    source = hf_hub_download(
        repo_id=repo,
        filename="data/test-00000-of-00001.parquet",
        revision=rev,
        repo_type="dataset",
        cache_dir=cache_dir,
    )
    table = parquet.read_table(source)
    rows = table.to_pylist()
    if len(rows) != int(expected):
        raise SystemExit(f"{repo}@{rev}: row count {len(rows)} != {expected}")
    by_id = {str(row["instance_id"]): row for row in rows}
    missing = [i for i in ids if i not in by_id]
    if missing: raise SystemExit(f"{repo}@{rev}: missing IDs {missing}")
    rows = []
    for iid in ids:
        row = by_id[iid]
        data = json.dumps([row], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        path = root / f"{stem}_{iid}.json"
        path.write_bytes(data + b"\n")
        rows.append({"instance_id": iid, "path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": path.stat().st_size})
    source_path = pathlib.Path(source)
    return {"repo": repo, "revision": rev, "split": "test", "rows": int(expected), "source_file": str(source_path), "source_file_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(), "selected": rows}
obj = {"schema_version": "datasets.v3", "lite": get(lite_repo, lite_rev, lite_rows, [lite_first, lite_gold], "lite"), "verified": get(verified_repo, verified_rev, verified_rows, [verified_gold], "verified"), "provenance": "measured", "reader": "huggingface_hub+pyarrow.parquet"}
p = pathlib.Path(out); tmp = p.with_name(p.name + ".tmp"); tmp.write_text(json.dumps(obj, indent=2) + "\n"); tmp.replace(p)
PY
echo "Pinned model and dataset assets verified: $MODEL@$REVISION; manifest=$MODEL_OUT datasets=$DATASET_OUT"
