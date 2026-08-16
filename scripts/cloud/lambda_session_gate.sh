#!/usr/bin/env bash
set -Eeuo pipefail

# Validate the user's local paid-session authorization without contacting a
# provider API. This is a guardrail, not a launcher or termination service.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SESSION="${LAMBDA_SESSION_FILE:-$ROOT/cloud/lambda/cloud_session.yaml}"
GATE="G3A"
DRY=0

usage() { echo 'Usage: lambda_session_gate.sh [--session FILE] [--gate G3A|G5|G6] [--dry-run]'; }

while (($#)); do
  case "$1" in
    --session) [[ $# -ge 2 ]] || { echo '--session requires a file' >&2; exit 2; }; SESSION="$2"; shift 2;;
    --session=*) SESSION="${1#*=}"; shift;;
    --gate) [[ $# -ge 2 ]] || { echo '--gate requires G3A, G5, or G6' >&2; exit 2; }; GATE="$2"; shift 2;;
    --gate=*) GATE="${1#*=}"; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

case "$GATE" in G3A|G5|G6) ;; *) echo 'gate must be G3A, G5, or G6' >&2; exit 2;; esac

if (( DRY )); then
  echo "DRY-RUN: inspect $SESSION for authorized=true, positive dollar/GPU-hour caps, permitted gate <= $GATE, UTC window, export/termination availability, and backup destination."
  echo 'DRY-RUN: no provider API, VM launch, billing mutation, or termination is performed.'
  exit 0
fi

[[ -f "$SESSION" ]] || { echo "paid-session authorization file is missing: $SESSION" >&2; exit 1; }
python3 - "$SESSION" "$GATE" <<'PY'
from __future__ import annotations

import datetime as dt
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
requested = sys.argv[2]
values: dict[str, str] = {}
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.split("#", 1)[0].strip()
    if not line or ":" not in line:
        continue
    key, value = line.split(":", 1)
    values[key.strip()] = value.strip().strip('"\'')

def fail(message: str) -> None:
    raise SystemExit(f"paid-session gate blocked: {message}")

if values.get("authorized", "false").lower() != "true":
    fail("authorized must be true")
if values.get("provider") != "lambda":
    fail("provider must be lambda")
try:
    dollars = float(values.get("maximum_dollars", "0"))
    gpu_hours = float(values.get("maximum_gpu_hours", "0"))
except ValueError:
    fail("maximum_dollars and maximum_gpu_hours must be numeric")
if dollars <= 0 or gpu_hours <= 0:
    fail("maximum_dollars and maximum_gpu_hours must be positive")
rank = {"G3A": 1, "G5": 2, "G6": 3}
maximum_gate = values.get("maximum_gate", "")
if maximum_gate not in rank or rank[requested] > rank[maximum_gate]:
    fail(f"requested {requested} exceeds maximum_gate {maximum_gate or '<missing>'}")

def timestamp(name: str) -> dt.datetime:
    raw = values.get(name, "")
    if not raw:
        fail(f"{name} is required")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        fail(f"{name} must be ISO-8601")
    if parsed.tzinfo is None:
        fail(f"{name} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)

start = timestamp("authorized_start_utc")
stop = timestamp("stop_launching_utc")
export = timestamp("begin_export_utc")
termination = timestamp("hard_console_termination_utc")
now = dt.datetime.now(dt.timezone.utc)
if not (start <= now < stop):
    fail("current UTC time is outside the authorized launch window")
if not (stop <= export <= termination):
    fail("stop/export/termination deadlines are not ordered")
if values.get("user_available_to_export", "false").lower() != "true":
    fail("user_available_to_export must be true")
if values.get("user_available_to_terminate", "false").lower() != "true":
    fail("user_available_to_terminate must be true")
if not values.get("backup_destination"):
    fail("backup_destination is required")
print(f"paid-session gate passed: gate={requested} maximum_gate={maximum_gate} dollars={dollars:g} gpu_hours={gpu_hours:g}")
PY
