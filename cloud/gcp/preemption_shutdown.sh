#!/usr/bin/env bash
set -Eeuo pipefail

# Best-effort Spot shutdown hook. Persistent disks are authoritative; the
# Cloud Storage sync is an additional copy and must never block local cleanup.
WORK_ROOT="${EIC_WORK_ROOT:-/mnt/eic-work}"
GCS_URI="${EIC_GCS_URI:-}"
STATE_DIR="${EIC_STATE_DIR:-$WORK_ROOT/state/gcp}"
MARKER="$STATE_DIR/preemption_shutdown.json"
REPO_ROOT="${EIC_REPO_ROOT:-$WORK_ROOT/source/agentic-workload-simulator}"
SERVER_MANIFEST="${EIC_SERVER_MANIFEST:-$WORK_ROOT/artifacts/manifests/vllm_server.json}"

mkdir -p -- "$STATE_DIR"

metadata_value() {
  local path="$1"
  curl --silent --show-error --fail \
    --connect-timeout 1 --max-time 2 \
    -H 'Metadata-Flavor: Google' \
    "http://metadata.google.internal/computeMetadata/v1/$path" 2>/dev/null || true
}

INSTANCE_NAME="$(metadata_value instance/name)"
ZONE="$(metadata_value instance/zone)"
REASON="${EIC_SHUTDOWN_REASON:-spot_preemption_or_host_termination}"

python3 - "$MARKER" "$INSTANCE_NAME" "$ZONE" "$REASON" "$WORK_ROOT" <<'PY'
import json
import pathlib
import sys
import time

path, instance, zone, reason, work_root = sys.argv[1:]
payload = {
    "schema_version": "gcp-preemption-shutdown.v1",
    "status": "shutdown_started",
    "provenance": "measured",
    "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "instance_name": instance or None,
    "zone": zone or None,
    "reason": reason,
    "work_root": work_root,
}
temporary = pathlib.Path(path).with_name(pathlib.Path(path).name + ".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(path)
PY

stop_workloads() {
  if [[ -x "$REPO_ROOT/scripts/cloud/lambda_stop_workloads.sh" ]]; then
    "$REPO_ROOT/scripts/cloud/lambda_stop_workloads.sh" \
      --work-root "$WORK_ROOT" \
      --server-manifest "$SERVER_MANIFEST" || true
  fi
  if command -v docker >/dev/null 2>&1; then
    docker stop --time 30 vllm-agentic >/dev/null 2>&1 || true
  fi
}

sync_artifacts() {
  [[ -n "$GCS_URI" ]] || return 0
  command -v gcloud >/dev/null 2>&1 || return 0
  local destination="${GCS_URI%/}/${INSTANCE_NAME:-unknown-instance}"
  mkdir -p -- "$STATE_DIR/upload"
  gcloud storage rsync --recursive --checksums-only \
    "$WORK_ROOT/artifacts" "$destination/artifacts" \
    >"$STATE_DIR/upload/artifacts.log" 2>&1 || true
  gcloud storage rsync --recursive --checksums-only \
    "$WORK_ROOT/data/raw" "$destination/data-raw" \
    >"$STATE_DIR/upload/data-raw.log" 2>&1 || true
}

stop_workloads
sync
sync_artifacts
sync

python3 - "$MARKER" <<'PY'
import json
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["status"] = "shutdown_export_attempted"
payload["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
temporary = path.with_name(path.name + ".tmp")
temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
temporary.replace(path)
PY
