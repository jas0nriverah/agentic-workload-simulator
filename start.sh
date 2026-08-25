#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# Backend-neutral developer/runtime entry point.  Auto selection is explicit
# and fail-closed: Docker is used only after a capability probe succeeds.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BACKEND="${BACKEND:-auto}"
MANIFEST=""
ARTIFACT_ROOT="$ROOT/artifacts/runtime"
PHASE="calibration"
PYTHON_EXECUTABLE=""
MODEL_CACHE=""
CONTAINER_NAME="agentic-sim-vllm"
HEALTH_TIMEOUT="180"
PROTOCOL_SHA256=""
SPLIT_SHA256=""
PREDICTION_SHA256=""
DRY_RUN=0
RESUME=0
STOP=0
PREDICTION_FROZEN=0
INSTALL_DEV=1
TRACE_BINARY="${H100_NSYS_BIN:-}"
TRACE_SESSION="${H100_NSYS_SESSION:-h100-final-validation}"
TRACE_ROOT="${H100_TRACE_MOUNT_ROOT:-}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./start.sh [--backend auto|docker|direct] [options]

Backend selection:
  --backend NAME              Select auto, docker, or direct (default: auto).

Runtime options:
  --manifest FILE             Reviewed vLLM/runtime manifest.
  --artifact-root DIR         Common root; artifacts go in DIR/docker or DIR/direct.
  --phase calibration|holdout Runtime phase; holdout requires --prediction-frozen.
  --prediction-frozen         Assert the immutable prediction manifest is sealed.
  --protocol-sha256 HASH      Bind runtime state to the sealed protocol hash.
  --split-sha256 HASH         Bind runtime state to the sealed split hash.
  --prediction-sha256 HASH    Bind runtime state to the prediction manifest hash.
  --model-cache DIR           Prepared offline Hugging Face cache.
  --python FILE               Direct-runtime Python executable.
  --trace-binary FILE         Nsight Systems binary for request-scoped tracing.
  --trace-session NAME         Nsight interactive session name.
  --trace-root DIR             Host trace mount root.
  --container-name NAME       Docker container name.
  --health-timeout SECONDS    Readiness deadline (default: 180).
  --resume                    Resume only a matching failed/partial runtime state.
  --stop                      Stop the recorded runtime instead of starting one.
  --no-install-dev            Check developer modules but do not install missing ones.
  --dry-run                   Print the selected command; make no runtime/artifact changes.
USAGE
}

while (($#)); do
  case "$1" in
    --backend) (($# >= 2)) || die '--backend requires auto, docker, or direct'; BACKEND="$2"; shift 2 ;;
    --backend=*) BACKEND="${1#*=}"; shift ;;
    --manifest) (($# >= 2)) || die '--manifest requires a file'; MANIFEST="$2"; shift 2 ;;
    --artifact-root) (($# >= 2)) || die '--artifact-root requires a directory'; ARTIFACT_ROOT="$2"; shift 2 ;;
    --phase) (($# >= 2)) || die '--phase requires calibration or holdout'; PHASE="$2"; shift 2 ;;
    --prediction-frozen) PREDICTION_FROZEN=1; shift ;;
    --protocol-sha256) (($# >= 2)) || die '--protocol-sha256 requires a hash'; PROTOCOL_SHA256="$2"; shift 2 ;;
    --split-sha256) (($# >= 2)) || die '--split-sha256 requires a hash'; SPLIT_SHA256="$2"; shift 2 ;;
    --prediction-sha256) (($# >= 2)) || die '--prediction-sha256 requires a hash'; PREDICTION_SHA256="$2"; shift 2 ;;
    --model-cache) (($# >= 2)) || die '--model-cache requires a directory'; MODEL_CACHE="$2"; shift 2 ;;
    --python) (($# >= 2)) || die '--python requires an executable'; PYTHON_EXECUTABLE="$2"; shift 2 ;;
    --trace-binary) (($# >= 2)) || die '--trace-binary requires a file'; TRACE_BINARY="$2"; shift 2 ;;
    --trace-session) (($# >= 2)) || die '--trace-session requires a name'; TRACE_SESSION="$2"; shift 2 ;;
    --trace-root) (($# >= 2)) || die '--trace-root requires a directory'; TRACE_ROOT="$2"; shift 2 ;;
    --container-name) (($# >= 2)) || die '--container-name requires a name'; CONTAINER_NAME="$2"; shift 2 ;;
    --health-timeout) (($# >= 2)) || die '--health-timeout requires seconds'; HEALTH_TIMEOUT="$2"; shift 2 ;;
    --resume) RESUME=1; shift ;;
    --stop) STOP=1; shift ;;
    --no-install-dev) INSTALL_DEV=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ "$BACKEND" == auto || "$BACKEND" == docker || "$BACKEND" == direct ]] || die 'backend must be auto, docker, or direct'
[[ "$PHASE" == calibration || "$PHASE" == holdout || "$PHASE" == sealed_holdout ]] || die 'phase must be calibration or holdout'
if [[ "$PHASE" != calibration && "$PREDICTION_FROZEN" -ne 1 ]]; then
  die 'holdout runtime requires --prediction-frozen (auto may select direct)'
fi

command -v python3 >/dev/null 2>&1 || die 'python3 is required'

check_common_deps() {
  local missing_modules=() module
  for module in pytest ruff; do
    if ! python3 -c 'import importlib.util,sys; sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)' "$module" >/dev/null 2>&1; then
      missing_modules+=("$module")
    fi
  done
  if ((${#missing_modules[@]})); then
    if (( DRY_RUN || !INSTALL_DEV )); then
      printf 'DRY-RUN: common developer modules missing (would install): %s\n' "${missing_modules[*]}"
    else
      command -v python3 >/dev/null 2>&1 || die 'python3 is required to install developer dependencies'
      python3 -m pip install --user 'pytest>=7' 'ruff>=0.6' || die 'failed to install common developer dependencies'
    fi
  else
    printf 'Common developer dependencies: pytest and ruff available\n'
  fi
  for module in git curl; do
    command -v "$module" >/dev/null 2>&1 || printf 'NOTICE: optional developer command unavailable: %s\n' "$module" >&2
  done
  local in_container=0
  if [[ -n "${container:-}" || -n "${CONTAINER:-}" || -e /.dockerenv || -e /run/.containerenv ]]; then
    in_container=1
  fi
  if [[ "$BACKEND" == docker ]]; then
    if (( in_container )); then
      if (( DRY_RUN )); then
        printf 'DRY-RUN: explicit Docker backend is unsupported in an unprivileged container; Docker-in-Docker will not be attempted\n'
      else
        die 'Docker backend is unsupported in an unprivileged container; Docker-in-Docker is not attempted'
      fi
    elif command -v docker >/dev/null 2>&1; then
      printf 'Docker backend selected: docker CLI detected\n'
    elif (( DRY_RUN )); then
      printf 'DRY-RUN: Docker is optional globally; selected Docker backend would require docker and NVIDIA Container Toolkit\n'
    else
      die 'Docker backend selected but docker is unavailable; direct fallback is disabled'
    fi
  elif [[ "$BACKEND" == direct ]]; then
    printf 'Direct backend selected: Docker is optional and will not be probed\n'
  else
    if (( in_container )); then
      printf 'Auto backend selected: unprivileged container detected; Docker-in-Docker disabled, direct will be selected\n'
    else
      printf 'Auto backend selected: Docker/NVIDIA capability will be probed at runtime; direct is the only fallback\n'
    fi
  fi
}

check_common_deps

PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
backend_args=(
  --backend "$BACKEND"
  --artifact-root "$ARTIFACT_ROOT"
  --phase "$PHASE"
  --container-name "$CONTAINER_NAME"
  --health-timeout "$HEALTH_TIMEOUT"
  --trace-session "$TRACE_SESSION"
  --protected-root "$ROOT/artifacts/h100_final_validation"
  --protected-root "$ROOT/project"
)
[[ -n "$TRACE_BINARY" ]] && backend_args+=(--trace-binary "$TRACE_BINARY")
[[ -n "$TRACE_ROOT" ]] && backend_args+=(--trace-root "$TRACE_ROOT")
[[ -n "$MANIFEST" ]] && backend_args+=(--manifest "$MANIFEST")
[[ -n "$PYTHON_EXECUTABLE" ]] && backend_args+=(--python "$PYTHON_EXECUTABLE")
[[ -n "$MODEL_CACHE" ]] && backend_args+=(--model-cache "$MODEL_CACHE")
[[ -n "$PROTOCOL_SHA256" ]] && backend_args+=(--protocol-sha256 "$PROTOCOL_SHA256")
[[ -n "$SPLIT_SHA256" ]] && backend_args+=(--split-manifest-sha256 "$SPLIT_SHA256")
[[ -n "$PREDICTION_SHA256" ]] && backend_args+=(--prediction-manifest-sha256 "$PREDICTION_SHA256")
(( PREDICTION_FROZEN )) && backend_args+=(--prediction-frozen)
(( RESUME )) && backend_args+=(--resume)
(( STOP )) && backend_args+=(--stop)
(( DRY_RUN )) && backend_args+=(--dry-run)

exec python3 -m agentic_sim.runtime.backend "${backend_args[@]}"
