#!/usr/bin/env bash
set -Eeuo pipefail

MODE=""; COMMAND=""; OUTPUT=""; RUN_ID=""; ATTEMPT_ID=""; LEVEL=""; CAPABILITY_MANIFEST=""; FIRST_RESULT_MARKER=""; SESSION=""; GATE="G6"; DRY=0; ALLOW=0
usage() {
  echo 'Usage: deep_profile.sh --mode strace|nsys --command CMD --output PATH --run-id ID --attempt-id ID [--capability-manifest FILE --first-result-marker FILE --session FILE --gate G6 --allow-profile] [--dry-run]'
}
while (($#)); do
  case "$1" in
    --mode) MODE="$2"; shift 2;; --mode=*) MODE="${1#*=}"; shift;;
    --command) COMMAND="$2"; shift 2;; --command=*) COMMAND="${1#*=}"; shift;;
    --output) OUTPUT="$2"; shift 2;; --output=*) OUTPUT="${1#*=}"; shift;;
    --run-id) RUN_ID="$2"; shift 2;; --run-id=*) RUN_ID="${1#*=}"; shift;;
    --attempt-id) ATTEMPT_ID="$2"; shift 2;; --attempt-id=*) ATTEMPT_ID="${1#*=}"; shift;;
    --observability-level) LEVEL="$2"; shift 2;; --observability-level=*) LEVEL="${1#*=}"; shift;;
    --capability-manifest) CAPABILITY_MANIFEST="$2"; shift 2;; --capability-manifest=*) CAPABILITY_MANIFEST="${1#*=}"; shift;;
    --first-result-marker) FIRST_RESULT_MARKER="$2"; shift 2;; --first-result-marker=*) FIRST_RESULT_MARKER="${1#*=}"; shift;;
    --session) SESSION="$2"; shift 2;; --session=*) SESSION="${1#*=}"; shift;;
    --gate) GATE="$2"; shift 2;; --gate=*) GATE="${1#*=}"; shift;;
    --allow-profile) ALLOW=1; shift;; --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;; *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done
[[ "$MODE" == strace || "$MODE" == nsys ]] || { echo '--mode must be strace or nsys' >&2; exit 2; }
[[ -n "$COMMAND" && -n "$OUTPUT" && -n "$RUN_ID" && -n "$ATTEMPT_ID" ]] || { echo 'command, output, run-id, and attempt-id are required' >&2; exit 2; }
if [[ -z "$LEVEL" ]]; then
  LEVEL="syscall"; [[ "$MODE" == nsys ]] && LEVEL="nsys"
fi
case "$LEVEL" in
  control|thin|thin-telemetry|uninstrumented) echo 'profilers are forbidden for control/thin-telemetry attempts' >&2; exit 2;;
  syscall|nsys|otel|deep-profile) ;;
  *) echo '--observability-level must be syscall, nsys, otel, or deep-profile' >&2; exit 2;;
esac
if [[ "$MODE" == strace && "$LEVEL" != syscall && "$LEVEL" != deep-profile ]]; then
  echo 'strace profiles must be labeled syscall or deep-profile' >&2; exit 2
fi
if [[ "$MODE" == nsys && "$LEVEL" != nsys && "$LEVEL" != deep-profile ]]; then
  echo 'Nsight profiles must be labeled nsys or deep-profile' >&2; exit 2
fi

if (( DRY )); then
  if [[ "$MODE" == strace ]]; then
    echo "DRY-RUN: strace -f -T -ttt -e trace=%file,%desc,%process -o $OUTPUT -- bash -lc '<command>'"
  else
    echo "DRY-RUN: nsys profile --trace=cuda,nvtx,osrt --sample=none --force-overwrite=true --output $OUTPUT -- bash -lc '<command>'"
  fi
  echo 'DRY-RUN: no profiler is required, no command executes, and no baseline attempt is modified.'
  exit 0
fi
(( ALLOW )) || { echo 'refusing to execute a deep profile without --allow-profile' >&2; exit 1; }
[[ -n "$CAPABILITY_MANIFEST" && -f "$CAPABILITY_MANIFEST" ]] || { echo '--capability-manifest is required and must be a recorded probe' >&2; exit 1; }
[[ -n "$FIRST_RESULT_MARKER" && -f "$FIRST_RESULT_MARKER" ]] || { echo '--first-result-marker must identify a reviewed first result' >&2; exit 1; }
[[ -n "$SESSION" ]] || SESSION="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)/cloud/lambda/cloud_session.yaml"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
"$ROOT/scripts/cloud/lambda_session_gate.sh" --session "$SESSION" --gate "$GATE"
[[ ! -e "$OUTPUT" ]] || { echo "refusing to overwrite profile output: $OUTPUT" >&2; exit 1; }
[[ ! -e "$OUTPUT.profile_manifest.json" ]] || { echo "refusing to overwrite profile manifest: $OUTPUT.profile_manifest.json" >&2; exit 1; }
mkdir -p -- "$(dirname -- "$OUTPUT")"
python3 "$ROOT/scripts/observability/run_deep_profile.py" \
  --mode "$MODE" --command "$COMMAND" --output "$OUTPUT" \
  --run-id "$RUN_ID" --attempt-id "$ATTEMPT_ID" \
  --observability-level "$LEVEL" --capability-manifest "$CAPABILITY_MANIFEST"
