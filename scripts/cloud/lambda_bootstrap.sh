#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"; MANIFEST="${LAMBDA_MANIFEST:-$ROOT/cloud/lambda/instance_manifest.env}"; WORK_ROOT="${WORK_ROOT:-$ROOT/../agentic-work}"; DRY=0; RESUME=0; SKIP=0; LOG_DIR="$WORK_ROOT/logs/bootstrap"
usage(){ echo 'Usage: lambda_bootstrap.sh [--manifest FILE] [--dry-run] [--resume] [--skip-model-download] [--log-dir DIR]'; }
prev=''; for a in "$@"; do case "$a" in --dry-run) DRY=1;; --resume) RESUME=1;; --skip-model-download) SKIP=1;; --manifest) :;; --manifest=*) MANIFEST="${a#*=}";; --log-dir) :;; --log-dir=*) LOG_DIR="${a#*=}";; -h|--help) usage; exit 0;; *) if [[ "$prev" == --manifest ]]; then MANIFEST="$a"; elif [[ "$prev" == --log-dir ]]; then LOG_DIR="$a"; else echo "unknown argument: $a" >&2; exit 2; fi;; esac; prev="$a"; done
if [[ -f "$MANIFEST" ]]; then while IFS= read -r line || [[ -n "$line" ]]; do line="${line%%#*}"; key="${line%%=*}"; val="${line#*=}"; case "$key" in WORK_ROOT) WORK_ROOT="$val";; esac; done <"$MANIFEST"; fi
if ((DRY)); then echo 'DRY-RUN: preflight, utilities, venv, pinned repos, Docker, caches, model, and tests would run; no mutations occur.'; exit 0; fi
mkdir -p -- "$LOG_DIR" "$WORK_ROOT/state" "$WORK_ROOT/artifacts" "$WORK_ROOT/cache"; exec > >(tee -a "$LOG_DIR/bootstrap.log") 2>&1
stage(){ local n="$1"; shift; local m="$WORK_ROOT/state/bootstrap.$n.ok"; [[ "$RESUME" == 1 && -f "$m" ]] && { echo "stage $n already validated"; return; }; echo "stage $n start $(date -u +%FT%TZ)"; "$@"; touch "$m"; echo "stage $n complete $(date -u +%FT%TZ)"; }
stage preflight "$ROOT/scripts/cloud/lambda_preflight.sh" --manifest "$MANIFEST" --output "$WORK_ROOT/artifacts/lambda_preflight.json"
install_utils(){ local miss=() t; for t in git curl jq rsync tmux tar zstd python3 python3-venv; do command -v "$t" >/dev/null 2>&1 || miss+=("$t"); done; ((${#miss[@]}==0)) && return; command -v sudo >/dev/null && command -v apt-get >/dev/null || { echo "missing utilities: ${miss[*]}" >&2; return 1; }; sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${miss[@]}"; }
stage utilities install_utils; stage directories mkdir -p -- "$WORK_ROOT/repos" "$WORK_ROOT/venv" "$WORK_ROOT/cache/huggingface" "$WORK_ROOT/cache/torch" "$WORK_ROOT/artifacts/manifests"
stage python_environment bash -c "[[ -x '$WORK_ROOT/venv/bin/python' ]] || python3 -m venv '$WORK_ROOT/venv'; '$WORK_ROOT/venv/bin/python' -m pip install --upgrade pip"
stage project_install env PYTHONPATH="$ROOT/src" "$WORK_ROOT/venv/bin/pip" install --no-deps -e "$ROOT"
clone_pinned(){ local url="$1" rev="$2" dst="$3"; [[ -n "$rev" ]] || { echo "pinned revision required for $dst" >&2; return 1; }; if [[ -d "$dst/.git" ]]; then git -C "$dst" fetch --depth 1 origin "$rev"; else git clone --filter=blob:none "$url" "$dst"; fi; git -C "$dst" checkout --detach "$rev"; }
clone_repos(){ clone_pinned "${SWE_AGENT_REPO_URL:-https://github.com/SWE-agent/SWE-agent.git}" "${SWE_AGENT_REVISION:-}" "$WORK_ROOT/repos/SWE-agent"; clone_pinned "${SWE_BENCH_REPO_URL:-https://github.com/SWE-bench/SWE-bench.git}" "${SWE_BENCH_REVISION:-}" "$WORK_ROOT/repos/SWE-bench"; }
stage pinned_repositories clone_repos; stage runtime bash -c 'command -v docker >/dev/null && docker info >/dev/null && command -v tmux >/dev/null'
if ((SKIP)); then echo 'model download skipped'; else stage model_download "$ROOT/scripts/cloud/lambda_download_assets.sh" --manifest "$MANIFEST" --work-root "$WORK_ROOT"; fi
stage tests env PYTHONPATH="$ROOT/src" "$WORK_ROOT/venv/bin/python" -m unittest discover -s "$ROOT/tests" -v
echo "Bootstrap complete. Next command: $ROOT/scripts/cloud/lambda_start_vllm.sh --manifest $MANIFEST"
