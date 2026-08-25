#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [[ "$#" -gt 0 && "$1" == "--dry-run" ]]; then
  python3 "$ROOT/scripts/cloud/a100_setup_doctor.py" --offline
  echo 'DRY-RUN: 24 calibration, 12 sealed holdouts, 3 repeats, 2 warmups; no GPU inspection'
  exit 0
fi
if [[ "$#" -gt 0 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
  echo 'Usage: run_a100_final_validation.sh --manifest FILE --execute --allow-a100 [--phase all|calibration|holdout] [--resume]'
  exit 0
fi
EXECUTE=0
ALLOW=0
MANIFEST=""
PHASE=all
RESUME=0
while (($#)); do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2;;
    --phase) PHASE="$2"; shift 2;;
    --resume) RESUME=1; shift;;
    --execute) EXECUTE=1; shift;;
    --allow-a100) ALLOW=1; shift;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 1;;
  esac
done
(( EXECUTE && ALLOW )) || { echo 'ERROR: execution requires --execute and --allow-a100' >&2; exit 1; }
[[ -n "$MANIFEST" ]] || { echo 'ERROR: --manifest is required' >&2; exit 1; }
python3 "$ROOT/scripts/cloud/a100_setup_doctor.py" --manifest "$MANIFEST"
if (( RESUME )); then
  exec python3 "$ROOT/scripts/cloud/a100_execution.py" --manifest "$MANIFEST" --phase "$PHASE" --resume
else
  exec python3 "$ROOT/scripts/cloud/a100_execution.py" --manifest "$MANIFEST" --phase "$PHASE"
fi
