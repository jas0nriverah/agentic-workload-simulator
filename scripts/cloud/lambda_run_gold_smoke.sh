#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"; DRY=0; SUITE=both; WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"
prev=''; for a in "$@"; do case "$a" in --dry-run) DRY=1;; --suite) :;; --suite=*) SUITE="${a#*=}";; --work-root) :;; --work-root=*) WORK_ROOT="${a#*=}";; -h|--help) echo 'Usage: lambda_run_gold_smoke.sh [--suite lite|verified|both] [--work-root DIR] [--dry-run]'; exit 0;; *) if [[ "$prev" == --suite ]]; then SUITE="$a"; elif [[ "$prev" == --work-root ]]; then WORK_ROOT="$a"; else echo "unknown argument: $a" >&2; exit 2; fi;; esac; prev="$a"; done
run_suite(){ local name="$1" command_name="GOLD_${1^^}_COMMAND"; local command_value="${!command_name:-}"; [[ -n "$command_value" ]] || { echo "$command_name is required (resolved official evaluator command)" >&2; return 1; }; echo "running gold $name smoke"; bash -c "$command_value"; }
if ((DRY)); then echo "DRY-RUN: run configured official gold-patch evaluator smoke for suite=$SUITE; no command executes."; exit 0; fi
case "$SUITE" in lite) run_suite lite;; verified) run_suite verified;; both) run_suite lite; run_suite verified;; *) echo 'suite must be lite, verified, or both' >&2; exit 2;; esac
