#!/usr/bin/env bash
# Safely validate and collect compact Lambda experiment artifacts.
# No cloud API calls, credentials, deletion, or mutation of the source tree.
set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
DEFAULT_PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
PROJECT_ROOT=${PROJECT_ROOT:-$DEFAULT_PROJECT_ROOT}
WORK_ROOT=${WORK_ROOT:-"$PROJECT_ROOT/work"}
SOURCE_ROOT=${SOURCE_ROOT:-"$WORK_ROOT/results"}
OUTPUT_DIR=${OUTPUT_DIR:-"$PROJECT_ROOT/artifacts/lambda-collections"}
RUN_ID=${RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)"}
LARGE_THRESHOLD_BYTES=${LARGE_THRESHOLD_BYTES:-268435456}
SNAPSHOT=0
DRY_RUN=0
FORCE=0
DESTINATION=
ARCHIVE_NAME=

usage() {
  cat <<'USAGE'
Usage: lambda_collect_results.sh [options]

  --source-root PATH       Result tree (default: $WORK_ROOT/results)
  --output-dir PATH        Collection output directory
  --run-id ID              Safe identifier for output names
  --archive-name NAME      Archive filename (default: lambda-results-ID.tar.gz)
  --destination PATH       Copy completed outputs there
  --large-threshold BYTES  Exclude files at/above this size (default: 268435456)
  --snapshot                Permit collection while a run is active
  --force                  Replace same-named outputs
  --dry-run                Validate arguments and print the plan
  -h, --help               Show help
USAGE
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
is_safe_identifier() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]; }
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -- "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 -- "$1" | awk '{print $1}'
  else die 'sha256sum or shasum is required'; fi
}

is_active() {
  local marker pid pidfile cmd
  for marker in .active ACTIVE run.active .run.lock .running; do
    [[ -e "$SOURCE_ROOT/$marker" ]] && return 0
  done
  while IFS= read -r -d '' pidfile; do
    pid=$(tr -d '[:space:]' < "$pidfile" 2>/dev/null || true)
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    kill -0 "$pid" 2>/dev/null || continue
    cmd=$(ps -p "$pid" -o command= 2>/dev/null || true)
    if [[ "$cmd" == *"$PROJECT_ROOT"* || "$cmd" == *"$WORK_ROOT"* ]]; then return 0; fi
  done < <(find "$SOURCE_ROOT" -type f \( -name '*.pid' -o -name '*.lock' \) -print0 2>/dev/null)
  return 1
}

validate_tree() {
  local report=$1
  python3 - "$SOURCE_ROOT" "$OUTPUT_DIR" "$report" <<'PY'
import json
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
report = pathlib.Path(sys.argv[3])
classes = {n: [] for n in ("manifest", "predictions", "trajectories", "events", "evaluator", "status")}

def classify(path):
    name = path.name.lower()
    text = str(path).lower()
    suffix = path.suffix.lower()
    if "manifest" in name and suffix in {".json", ".jsonl", ".yaml", ".yml"}: return "manifest"
    if ("pred" in name or "prediction" in name) and suffix in {".json", ".jsonl"}: return "predictions"
    if ("trajectory" in text or name.endswith(".traj")) and suffix in {".json", ".jsonl", ".traj"}: return "trajectories"
    if ("event" in name or "/events/" in text) and suffix in {".json", ".jsonl"}: return "events"
    if ("result" in name or "evaluat" in name or ".eval." in name) and suffix in {".json", ".jsonl"}: return "evaluator"
    if ("status" in name or "/status/" in text) and suffix in {"", ".json", ".jsonl", ".txt"}: return "status"
    return None

def validate_json(path):
    if path.suffix.lower() in {".jsonl", ".traj"}:
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip(): continue
                try: json.loads(line)
                except json.JSONDecodeError as exc: raise ValueError(f"invalid JSON line {line_number}: {exc}") from exc
                count += 1
        if count == 0: raise ValueError("empty JSONL artifact")
    else:
        with path.open("r", encoding="utf-8") as handle: value = json.load(handle)
        if value in (None, [], {}): raise ValueError("empty JSON artifact")

files = []
for path in source.rglob("*"):
    if not path.is_file() or path.is_symlink(): continue
    try: path.resolve().relative_to(output)
    except ValueError: files.append(path)

lines = []
for path in sorted(files):
    kind = classify(path)
    if kind is None: continue
    try:
        if path.suffix.lower() in {".json", ".jsonl", ".traj"}: validate_json(path)
        elif path.stat().st_size == 0: raise ValueError("empty artifact")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"Invalid {kind} artifact {path}: {exc}", file=sys.stderr)
        raise SystemExit(2)
    lines.append(f"OK\t{kind}\t{path.relative_to(source).as_posix()}\t{path.stat().st_size}")

missing = [kind for kind in classes if not any(line.split("\t", 2)[1] == kind for line in lines)]
if missing:
    print("Missing required artifact classes: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(3)
report.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

while (($#)); do
  case "$1" in
    --source-root) [[ $# -ge 2 ]] || die '--source-root requires a path'; SOURCE_ROOT=$2; shift 2 ;;
    --output-dir) [[ $# -ge 2 ]] || die '--output-dir requires a path'; OUTPUT_DIR=$2; shift 2 ;;
    --run-id) [[ $# -ge 2 ]] || die '--run-id requires an identifier'; RUN_ID=$2; shift 2 ;;
    --archive-name) [[ $# -ge 2 ]] || die '--archive-name requires a filename'; ARCHIVE_NAME=$2; shift 2 ;;
    --destination) [[ $# -ge 2 ]] || die '--destination requires a path'; DESTINATION=$2; shift 2 ;;
    --large-threshold) [[ $# -ge 2 ]] || die '--large-threshold requires bytes'; LARGE_THRESHOLD_BYTES=$2; shift 2 ;;
    --snapshot) SNAPSHOT=1; shift ;;
    --force) FORCE=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

is_safe_identifier "$RUN_ID" || die "run id is not safe: $RUN_ID"
[[ "$LARGE_THRESHOLD_BYTES" =~ ^[0-9]+$ ]] || die 'large threshold must be a non-negative integer'
[[ -d "$SOURCE_ROOT" ]] || die "source root does not exist: $SOURCE_ROOT"
SOURCE_ROOT=$(cd -- "$SOURCE_ROOT" && pwd -P)
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd -P)
[[ "$SOURCE_ROOT" != "$OUTPUT_DIR" ]] || die 'output directory must not be the source root'
if (( ! SNAPSHOT )) && is_active; then die "source appears active; stop workloads first or use --snapshot"; fi
[[ -n "$ARCHIVE_NAME" ]] || ARCHIVE_NAME="lambda-results-${RUN_ID}.tar.gz"
[[ "$ARCHIVE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*\.tar\.gz$ ]] || die 'archive name must end in .tar.gz and contain no path separators'

ARCHIVE_PATH="$OUTPUT_DIR/$ARCHIVE_NAME"
CHECKSUM_PATH="$OUTPUT_DIR/${ARCHIVE_NAME%.tar.gz}.sha256"
RECEIPT_PATH="$OUTPUT_DIR/${ARCHIVE_NAME%.tar.gz}.collection_receipt.json"
if (( ! FORCE )); then
  [[ ! -e "$ARCHIVE_PATH" ]] || die "archive exists: $ARCHIVE_PATH (use --force)"
  [[ ! -e "$CHECKSUM_PATH" ]] || die "checksum exists: $CHECKSUM_PATH (use --force)"
  [[ ! -e "$RECEIPT_PATH" ]] || die "receipt exists: $RECEIPT_PATH (use --force)"
fi

if (( DRY_RUN )); then
  printf 'DRY-RUN: source=%s\nDRY-RUN: output=%s\nDRY-RUN: archive=%s\n' "$SOURCE_ROOT" "$OUTPUT_DIR" "$ARCHIVE_PATH"
  printf 'DRY-RUN: snapshot=%s threshold_bytes=%s\n' "$SNAPSHOT" "$LARGE_THRESHOLD_BYTES"
  exit 0
fi

STAGING=$(mktemp -d "${TMPDIR:-/tmp}/lambda-collect.XXXXXX")
trap 'rm -rf -- "$STAGING"' EXIT
PAYLOAD="$STAGING/payload"
mkdir -p "$PAYLOAD/.collection"
VALIDATION_REPORT="$PAYLOAD/.collection/validation.tsv"
validate_tree "$VALIDATION_REPORT" || die 'result validation failed'

LARGE_LIST="$PAYLOAD/.collection/large-files.tsv"
python3 - "$SOURCE_ROOT" "$OUTPUT_DIR" "$PAYLOAD" "$LARGE_THRESHOLD_BYTES" "$LARGE_LIST" <<'PY'
import pathlib
import shutil
import sys

source = pathlib.Path(sys.argv[1]).resolve()
output = pathlib.Path(sys.argv[2]).resolve()
payload = pathlib.Path(sys.argv[3]).resolve()
threshold = int(sys.argv[4])
large_list = pathlib.Path(sys.argv[5])
large_parts = {"raw", "trace", "traces", "nsys", "ncu", "profiler", "profiles"}
large = []
copied = 0
for path in sorted(source.rglob("*")):
    if not path.is_file() or path.is_symlink(): continue
    try: path.resolve().relative_to(output)
    except ValueError: pass
    else: continue
    rel = path.relative_to(source)
    size = path.stat().st_size
    if size >= threshold or any(part.lower() in large_parts for part in rel.parts):
        large.append((rel.as_posix(), size))
        continue
    target = payload / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    copied += 1
large_list.write_text("".join(f"{rel}\t{size}\n" for rel, size in large), encoding="utf-8")
print(f"copied_files={copied}")
print(f"excluded_large_files={len(large)}")
PY

CHECKSUMS_IN_ARCHIVE="$PAYLOAD/.collection/SHA256SUMS"
python3 - "$PAYLOAD" "$CHECKSUMS_IN_ARCHIVE" <<'PY'
import hashlib
import pathlib
import sys
root = pathlib.Path(sys.argv[1]).resolve()
out = pathlib.Path(sys.argv[2])
lines = []
for path in sorted(root.rglob("*")):
    if not path.is_file() or path == out: continue
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""): digest.update(block)
    lines.append(f"{digest.hexdigest()}  {path.relative_to(root).as_posix()}")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

tar -C "$PAYLOAD" -czf "$ARCHIVE_PATH" .
ARCHIVE_SHA=$(sha256_file "$ARCHIVE_PATH")
printf '%s  %s\n' "$ARCHIVE_SHA" "$(basename -- "$ARCHIVE_PATH")" > "$CHECKSUM_PATH"
ARCHIVE_LABEL=$ARCHIVE_PATH CHECKSUM_LABEL=$CHECKSUM_PATH RECEIPT_LABEL=$RECEIPT_PATH SOURCE_LABEL=$SOURCE_ROOT SNAPSHOT_LABEL=$SNAPSHOT RUN_ID_LABEL=$RUN_ID LARGE_LABEL=$LARGE_LIST python3 - <<'PY'
import json
import os
import pathlib
import time
archive = pathlib.Path(os.environ["ARCHIVE_LABEL"])
checksums = pathlib.Path(os.environ["CHECKSUM_LABEL"])
large = pathlib.Path(os.environ["LARGE_LABEL"])
receipt = {
    "schema_version": "lambda-collection.v1",
    "run_id": os.environ["RUN_ID_LABEL"],
    "collected_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "source_root": os.environ["SOURCE_LABEL"],
    "snapshot": os.environ["SNAPSHOT_LABEL"] == "1",
    "archive": archive.name,
    "archive_bytes": archive.stat().st_size,
    "archive_sha256": checksums.read_text(encoding="utf-8").split()[0],
    "embedded_manifest": ".collection/SHA256SUMS",
    "large_files_manifest": ".collection/large-files.tsv",
    "large_files_count": sum(1 for line in large.read_text(encoding="utf-8").splitlines() if line.strip()),
}
pathlib.Path(os.environ["RECEIPT_LABEL"]).write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
PY

if [[ -n "$DESTINATION" ]]; then
  mkdir -p -- "$DESTINATION"
  cp -- "$ARCHIVE_PATH" "$CHECKSUM_PATH" "$RECEIPT_PATH" "$DESTINATION/"
  printf 'Copied collection to %s\n' "$(cd -- "$DESTINATION" && pwd -P)"
else
  printf 'Rsync command (run from the local machine):\n'
  printf 'rsync -av --progress %q %q %q <local-user>@<local-host>:<local-directory>/\n' "$ARCHIVE_PATH" "$CHECKSUM_PATH" "$RECEIPT_PATH"
fi
printf 'Collection complete.\nArchive: %s\nSHA-256: %s\nReceipt: %s\n' "$ARCHIVE_PATH" "$ARCHIVE_SHA" "$RECEIPT_PATH"
