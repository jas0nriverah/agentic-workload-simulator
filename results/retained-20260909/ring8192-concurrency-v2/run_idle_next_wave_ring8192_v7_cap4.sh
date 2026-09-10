#!/usr/bin/env bash
set -euo pipefail

# Staged unattended continuation.  Root review must install this entry; this
# file itself never installs a crontab or starts a run.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROD="$(cd -- "$HERE/../final-production-v1" && pwd -P)"
DRIVER="$HERE/rolling_idle_next_wave_ring8192_v6_cap4.py"
DRIVER_SHA256="cee95b88138c87920c8053afd4a4b74c60bfa7331255af238b1c920129cc4f4f"
AUTHORIZATION="$PROD/launch-authorization-v6-ring8192-cap4.json"
AUTHORIZATION_SHA256="b7cadc9ea84e3bff547c576a7169b07bdce55bf9b98b0d69286663bbb1f92890"
QUOTA_REFRESH="$PROD/refresh_quota_root_v1.py"
QUOTA_REFRESH_SHA256="36bb1aa20f54a03efc3a8754f9fbc7ce642d8880ce24a047071bdbcb2b47b4c8"
STATE_ROOT="$HERE/idle-next-wave-dispatch-state-ring8192-v7"
DRIVER_LOCK="$HERE/idle-next-wave-dispatch.lock"
PYTHON="/usr/bin/python3"

die() {
  printf 'idle_next_wave_dispatch: NOT_READY: %s\n' "$1" >&2
  exit 2
}

[[ -f "$AUTHORIZATION" ]] || die "v2 authorization missing"
mkdir -p -m 700 "$STATE_ROOT"
exec 9>"$DRIVER_LOCK"
flock -n 9 || exit 0

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_DIR="$STATE_ROOT/$RUN_ID"
mkdir -p -m 700 "$RUN_DIR/storage-reports" "$RUN_DIR/health" "$RUN_DIR/registry-resolutions"
INTENT="$STATE_ROOT/dispatcher-intent.json"
if [[ -f "$INTENT" ]]; then
  PRIOR_STATUS="$($PYTHON - "$INTENT" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    value = json.load(stream)
print(value.get("status", "UNKNOWN"))
PY
)"
  case "$PRIOR_STATUS" in
    UNKNOWN|RUNNING|LAUNCHING) die "prior dispatch intent is unresolved: $PRIOR_STATUS" ;;
    COMPLETED|DEFERRED) ;;
    *) die "prior dispatch intent has unknown status: $PRIOR_STATUS" ;;
  esac
fi

"$PYTHON" - "$INTENT" "$RUN_ID" <<'PY'
import json, os, pathlib, sys, time
path, run_id = pathlib.Path(sys.argv[1]), sys.argv[2]
tmp = path.with_name(path.name + ".tmp-" + str(os.getpid()))
with tmp.open("x") as stream:
    json.dump({
        "schema": "assignment.idle-next-wave-dispatch-intent.v1",
        "status": "RUNNING", "run_id": run_id, "started_epoch": time.time(),
    }, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(tmp, path)
PY

[[ "$(sha256sum "$AUTHORIZATION" | awk '{print $1}')" == "$AUTHORIZATION_SHA256" ]] || die "v2 authorization SHA changed"
[[ -f "$DRIVER" ]] || die "next-wave driver missing"
[[ "$(sha256sum "$DRIVER" | awk '{print $1}')" == "$DRIVER_SHA256" ]] || die "next-wave driver SHA changed"
[[ -f "$QUOTA_REFRESH" ]] || die "root quota refresher missing"
[[ "$(sha256sum "$QUOTA_REFRESH" | awk '{print $1}')" == "$QUOTA_REFRESH_SHA256" ]] || die "root quota refresher SHA changed"

RESOLUTION_ARGS=()
RESOLUTION_INDEX="$PROD/wave-0003-resolutions/index.json"
if [[ -f "$RESOLUTION_INDEX" ]]; then
  RESOLUTION_ARGS=(--resolution-index "$RESOLUTION_INDEX")
fi
SUPERVISOR_PYTHON="$("$PYTHON" - "$AUTHORIZATION" <<'PY'
import json, sys
with open(sys.argv[1]) as stream:
    print(json.load(stream)["supervisor"]["python"])
PY
)"

set +e
"$PYTHON" "$DRIVER" \
  --authorization "$AUTHORIZATION" \
  --authorization-sha256 "$AUTHORIZATION_SHA256" \
  --retention-ledger "$HERE/accepted-attempt-retention-ledger-v1.json" \
  --archive-root "$HERE/accepted-attempt-retention-receipts-v1" \
  --remote-root /storage/ice1/9/6/jriverah3/eic-work/runtime/astra-evidence-archive-20260909/squashfs-retained-20260909 \
  --queue-binding "$PROD/queue-binding-ring8192-v1.json" \
  --pace-receipt "$RUN_DIR/pace-quota-receipt-v1.json" \
  --report-dir "$RUN_DIR/storage-reports" \
  --health-output-dir "$RUN_DIR/health" \
  --launcher-state-dir "$RUN_DIR/launcher-state" \
  --resolution-dir "$RUN_DIR/registry-resolutions" \
  --pace-refresh-command "$SUPERVISOR_PYTHON" "$QUOTA_REFRESH" '{pace_receipt}' \
  "${RESOLUTION_ARGS[@]}" \
  --execute-next-wave \
  >"$RUN_DIR/result.json" 2>"$RUN_DIR/stderr.log"
STATUS=$?
set -e

"$PYTHON" - "$INTENT" "$RUN_DIR/result.json" "$RUN_DIR/stderr.log" "$STATUS" <<'PY'
import json, os, pathlib, re, sys, time
intent, result, stderr = map(pathlib.Path, sys.argv[1:4])
exit_code = int(sys.argv[4])
try:
    payload = json.loads(result.read_text().strip().splitlines()[-1])
except (FileNotFoundError, json.JSONDecodeError, IndexError):
    payload = {}
detail = stderr.read_text(errors="replace") if stderr.exists() else ""
if exit_code == 0 and payload.get("status") in {"LAUNCHED", "FINISHED"}:
    status = "COMPLETED"
elif re.search(r"queue has .* active/orphaned|queue has .* blocked|queue dispatch is halted|shared production rolling-batch lock is busy", detail):
    status = "DEFERRED"
else:
    status = "UNKNOWN"
value = {
    "schema": "assignment.idle-next-wave-dispatch-intent.v1",
    "status": status,
    "run_id": json.loads(intent.read_text()).get("run_id"),
    "finished_epoch": time.time(),
    "exit_code": exit_code,
    "result_path": str(result),
    "stderr_path": str(stderr),
}
tmp = intent.with_name(intent.name + ".tmp-" + str(os.getpid()))
with tmp.open("x") as stream:
    json.dump(value, stream, indent=2, sort_keys=True)
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
os.replace(tmp, intent)
PY

python3 - "$RUN_DIR/state.json" "$RUN_DIR/result.json" "$RUN_DIR/stderr.log" "$STATUS" <<'PY'
import json, pathlib, sys, time
state, result, stderr = map(pathlib.Path, sys.argv[1:4])
exit_code = int(sys.argv[4])
payload = {
    "schema": "assignment.idle-next-wave-dispatch-state.v2",
    "status": "PASS" if exit_code == 0 else "NOT_READY",
    "exit_code": exit_code,
    "captured_epoch": time.time(),
    "result_path": str(result),
    "stderr_path": str(stderr),
}
state.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY

exit "$STATUS"
