#!/usr/bin/env bash
set -Eeuo pipefail

# Read-only four-minute production observation. The authorization path and
# SHA are arguments so a new root-reviewed v2 snapshot can be installed
# without silently falling back to the superseded v1 authorization.
if (( $# != 2 )); then
    echo "usage: $0 AUTHORIZATION AUTHORIZATION_SHA256" >&2
    exit 2
fi

PROD="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
N="$(dirname -- "$PROD")"
AUTHORIZATION="$1"
AUTHORIZATION_SHA256="$2"
MONITOR_SCRIPT="$PROD/production_monitor_v1.py"
HEALTH_SCRIPT="$PROD/refresh_health_v1.py"
CANDIDATE_MONITOR="$N/production-source-candidate-v9/scripts/assignment/acquisition_monitor.py"
WORKER_POOL="$PROD/ring8192-runtime-v1/worker-pool-manifest.json"
PLAN="$PROD/frozen-plan.jsonl"
HEALTH_DIR="$PROD/health-preflight-ring8192-v3"
HEALTH_RECEIPT="$HEALTH_DIR/refresh-health-v1.json"
MONITOR_STATE="$PROD/monitor-state-ring8192-v3"
LOG="$MONITOR_STATE/monitor-cron.log"
BATCH_LOCK="$N/storage-reclamation-v1/production-rolling-batch.lock"
WATCHER_LOCK="$N/cron-watch-v1/state/watcher.lock"
HANDLER_LOCK="$N/cron-watch-v1/state/handler.lock"
WATCHER_V2_LOCK="$N/cron-watch-v1/cron-watch-v2/state/watcher-v2.lock"
EXPECTED_MONITOR_SHA256="f67cec3a966787d3a1dcaee6a901ae0bdcfa5c10144cff001aba85d270392f5f"
EXPECTED_HEALTH_SHA256="46e9849006bf89a553a815deb6562fd5d2c688d13865794791fb87a9c7a393f5"
EXPECTED_CANDIDATE_MONITOR_SHA256="848398e4592b95c776847aaa5d1644c21e9cac61863e8583d71a7de088321c32"

mkdir -p -m 700 "$MONITOR_STATE" "$HEALTH_DIR"
exec >>"$LOG" 2>&1

die() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) production_monitor_cron: NOT_READY: $*" >&2
    exit 2
}

trim_log() {
    if [[ -f "$LOG" ]] && [[ "$(stat -c %s "$LOG")" -gt 5242880 ]]; then
        local temporary="$LOG.tmp.$$"
        tail -c 2097152 "$LOG" > "$temporary"
        chmod 600 "$temporary"
        mv -f -- "$temporary" "$LOG"
    fi
}

prune_health_raw() {
    local raw_dir="$HEALTH_DIR/raw"
    [[ -d "$raw_dir" ]] || return 0
    mapfile -t old_dirs < <(
        find "$raw_dir" -mindepth 1 -maxdepth 1 -type d -name 'refresh-*' -printf '%T@ %p\n' |
            sort -nr | tail -n +4 | cut -d' ' -f2-
    )
    local directory
    for directory in "${old_dirs[@]}"; do
        case "$directory" in
            "$raw_dir"/refresh-*) rm -rf -- "$directory" ;;
            *) die "refusing to prune unexpected health path: $directory" ;;
        esac
    done
}

exec {monitor_lock}>"$MONITOR_STATE/wrapper.lock"
flock -n "$monitor_lock" || exit 0

# Do not overlap the rolling launcher or the existing read-only collector.
declare -a lock_fds=()
for lock_path in "$BATCH_LOCK" "$WATCHER_LOCK" "$HANDLER_LOCK" "$WATCHER_V2_LOCK"; do
    mkdir -p -m 700 "$(dirname -- "$lock_path")"
    exec {lock_fd}>"$lock_path"
    flock -n "$lock_fd" || exit 0
    lock_fds+=("$lock_fd")
done

[[ "$(sha256sum "$MONITOR_SCRIPT" | awk '{print $1}')" == "$EXPECTED_MONITOR_SHA256" ]] || die "monitor SHA changed"
[[ "$(sha256sum "$HEALTH_SCRIPT" | awk '{print $1}')" == "$EXPECTED_HEALTH_SHA256" ]] || die "health refresh SHA changed"
[[ "$(sha256sum "$CANDIDATE_MONITOR" | awk '{print $1}')" == "$EXPECTED_CANDIDATE_MONITOR_SHA256" ]] || die "candidate monitor SHA changed"
[[ "$(sha256sum "$AUTHORIZATION" | awk '{print $1}')" == "$AUTHORIZATION_SHA256" ]] || die "authorization SHA changed"

# Validate the root snapshot and exact 1088-case queue binding before making
# the read-only health probes. This rejects a superseded v1 authorization.
/usr/bin/python3 - "$AUTHORIZATION" "$PLAN" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

auth_path, fallback_plan = map(Path, sys.argv[1:])
auth = json.loads(auth_path.read_text())
if not isinstance(auth, dict) or auth.get("status") != "PASS":
    raise SystemExit("authorization is not PASS")
plan = auth.get("plan")
queue = auth.get("queue")
if not isinstance(plan, dict) or not isinstance(queue, dict):
    raise SystemExit("authorization plan/queue binding missing")
plan_path = Path(plan.get("path", ""))
if plan_path != fallback_plan:
    raise SystemExit("authorization does not bind current final frozen plan")
digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
if digest != plan.get("sha256") or queue.get("plan_sha256") != digest:
    raise SystemExit("authorization/plan SHA binding mismatch")
case_ids = set()
for line in plan_path.read_text().splitlines():
    if not line.strip():
        continue
    row = json.loads(line)
    if row.get("record_type") == "case":
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or case_id in case_ids:
            raise SystemExit("frozen plan case identity malformed")
        case_ids.add(case_id)
if len(case_ids) != 1088:
    raise SystemExit("frozen plan is not exactly 1088 cases")
queue_path = Path(queue.get("path", str(Path(queue.get("dir", "")) / "queue.sqlite3")))
if not queue_path.is_file():
    raise SystemExit("authorized queue database missing")
db = sqlite3.connect("file:" + str(queue_path) + "?mode=ro", uri=True)
try:
    count = db.execute("select count(*) from cases").fetchone()[0]
    meta = db.execute("select value_json from meta where key='plan_sha256'").fetchone()
finally:
    db.close()
if count != 1088 or not meta:
    raise SystemExit("queue is not exactly 1088 cases or lacks plan metadata")
try:
    meta_value = json.loads(meta[0])
except (TypeError, json.JSONDecodeError):
    meta_value = meta[0]
if meta_value != digest:
    raise SystemExit("queue plan metadata mismatch")
PY

/usr/bin/python3 "$HEALTH_SCRIPT" \
    --worker-pool "$WORKER_POOL" \
    --plan "$PLAN" \
    --output-dir "$HEALTH_DIR" \
    --receipt-name "$(basename -- "$HEALTH_RECEIPT")"

HEALTH_SHA256="$(sha256sum "$HEALTH_RECEIPT" | awk '{print $1}')"
MONITOR_ARGS=(
    --authorization "$AUTHORIZATION"
    --monitor-sha256 "$EXPECTED_CANDIDATE_MONITOR_SHA256"
    --health-receipt "$HEALTH_RECEIPT"
    --health-receipt-sha256 "$HEALTH_SHA256"
    --state-dir "$MONITOR_STATE"
)
RETENTION_LEDGER="$N/storage-reclamation-v1/accepted-attempt-retention-ledger-v1.json"
if [[ -f "$RETENTION_LEDGER" ]]; then
    MONITOR_ARGS+=(--retention-ledger "$RETENTION_LEDGER" --retention-ledger-sha256 "$(sha256sum "$RETENTION_LEDGER" | awk '{print $1}')")
fi
/usr/bin/python3 "$MONITOR_SCRIPT" "${MONITOR_ARGS[@]}"

prune_health_raw
trim_log
