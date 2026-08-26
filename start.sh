#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# Safe repository bootstrap. It installs developer prerequisites, creates a
# project virtualenv, and runs offline checks. It never launches Docker, vLLM,
# Nsight, a GPU workload, or an experiment.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="${AGENTIC_VENV:-$ROOT/.venv}"
PYTHON_BIN="${AGENTIC_PYTHON_BIN:-}"
DRY_RUN=0
CHECK_ONLY=0
CLOUD_DEPS=0
REQUIRE_DOCKER=0
REQUIRE_GPU=0

# Keep the package contract in one place so the install path and --dry-run
# cannot drift apart. Docker is intentionally not in this list: a Docker
# daemon and NVIDIA Container Toolkit are host/provider capabilities, not
# safe packages to install inside an arbitrary GPU container.
UBUNTU_PACKAGES=(
  git gh curl ca-certificates jq unzip build-essential
  python3 python3-venv python3-pip shellcheck
)
MACOS_PACKAGES=(git gh curl jq unzip shellcheck python)

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: ./start.sh [OPTIONS]

Default: install common tools, create .venv, install the project/dev extras,
compile Python, and run the local test suite. This is safe to rerun and never
starts a model, Docker, GPU workload, or experiment.

Options:
  --dry-run             Print planned actions without changing the machine
  --check-only          Verify the current environment without installing
  --cloud-deps          Install the pinned Linux cloud requirements lock
  --require-docker      Require a usable Docker daemon and NVIDIA runtime
  --require-gpu         Require nvidia-smi to report a GPU
  --venv PATH           Use PATH instead of .venv
  -h, --help            Show this help
USAGE
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --check-only|--check) CHECK_ONLY=1; shift ;;
    --cloud-deps) CLOUD_DEPS=1; shift ;;
    --require-docker) REQUIRE_DOCKER=1; shift ;;
    --require-gpu) REQUIRE_GPU=1; shift ;;
    --venv)
      (($# >= 2)) || die '--venv requires a path'
      VENV="$2"
      shift 2
      ;;
    --venv=*) VENV="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

case "$VENV" in
  /*) ;;
  *) VENV="$ROOT/$VENV" ;;
esac

OS_ID=""
OS_VERSION=""
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  OS_ID="${ID:-}"
  OS_VERSION="${VERSION_ID:-}"
elif [[ "$(uname -s)" == Darwin ]]; then
  OS_ID=macos
  OS_VERSION="$(sw_vers -productVersion 2>/dev/null || true)"
fi

if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.11)"
  else
    PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
  fi
fi

IN_CONTAINER=0
[[ -f /.dockerenv || -n "${container:-}" ]] && IN_CONTAINER=1

run() {
  if (( DRY_RUN )); then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

as_root() {
  if [[ "${EUID:-$(id -u)}" == 0 ]]; then
    return 0
  fi
  command -v sudo >/dev/null 2>&1 || die 'system packages require root or sudo'
  printf 'sudo'
}

install_system_packages() {
  local sudo_cmd package
  case "$OS_ID" in
    ubuntu|debian)
      sudo_cmd="$(as_root)"
      run $sudo_cmd apt-get update
      run $sudo_cmd env DEBIAN_FRONTEND=noninteractive apt-get install -y "${UBUNTU_PACKAGES[@]}"
      ;;
    macos)
      command -v brew >/dev/null 2>&1 || die 'Homebrew is required on macOS: https://brew.sh'
      for package in "${MACOS_PACKAGES[@]}"; do
        if brew list --formula "$package" >/dev/null 2>&1; then
          printf 'Already installed: %s\n' "$package"
        else
          run brew install "$package"
        fi
      done
      ;;
    rhel|rocky|almalinux|fedora)
      # Managed HPC systems commonly provide modules and deny sudo. Reuse
      # those user-facing tools instead of treating the OS package manager as
      # part of the repository contract.
      if [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]]; then
        printf 'Using provider-managed %s runtime; no system package mutation\n' "$OS_ID"
        return 0
      fi
      if command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
        run sudo dnf install -y git curl ca-certificates jq unzip gcc gcc-c++ make
        return 0
      fi
      die "provider-managed $OS_ID runtime is missing Python; set AGENTIC_PYTHON_BIN or load a Python module"
      ;;
    *)
      die "unsupported operating system: ${OS_ID:-unknown}; use Ubuntu/Debian, RHEL-compatible Linux, or macOS"
      ;;
  esac
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

verify_base_tools() {
  local command_name
  for command_name in git curl; do
    require_command "$command_name"
  done
  [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || die 'Python 3 is required; load a provider module or set AGENTIC_PYTHON_BIN'
  if [[ -x "$VENV/bin/python" ]]; then
    "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.executable else 1)' \
      >/dev/null 2>&1 || die "the configured Python environment is unusable: $VENV"
  else
    "$PYTHON_BIN" -m venv --help >/dev/null 2>&1 || {
      command -v uv >/dev/null 2>&1 || die 'Python venv support is unavailable; load uv or install venv support'
    }
  fi
  for command_name in gh jq unzip shellcheck; do
    if command -v "$command_name" >/dev/null 2>&1; then
      printf '%s: available\n' "$command_name"
    else
      printf '%s: optional/unavailable\n' "$command_name"
    fi
  done
  printf 'Git: %s\n' "$(git --version)"
  printf 'Python: %s\n' "$("$PYTHON_BIN" --version 2>&1)"
}

verify_runtime() {
  if (( REQUIRE_DOCKER )); then
    (( IN_CONTAINER == 0 )) || die 'Docker VM validation is unavailable inside a container/Pod; use a full VM'
    require_command docker
    docker info >/dev/null 2>&1 || die 'Docker daemon is unavailable'
    docker info --format '{{json .Runtimes}}' | grep -q nvidia || die 'Docker NVIDIA runtime is unavailable'
    printf 'Docker/NVIDIA runtime: usable\n'
  elif command -v docker >/dev/null 2>&1; then
    printf 'Docker: present (not required for default bootstrap)\n'
  elif command -v podman >/dev/null 2>&1; then
    printf 'Podman: present (official evaluator compatibility is checked at execution)\n'
  else
    printf 'Container runtime: not present (acceptable for local/offline setup)\n'
  fi

  if (( REQUIRE_GPU )); then
    require_command nvidia-smi
    nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader
  elif command -v nvidia-smi >/dev/null 2>&1; then
    printf 'GPU: %s\n' "$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | sed -n '1p')"
  else
    printf 'GPU: not detected (acceptable for local/offline setup)\n'
  fi
}

prepare_python() {
  if [[ ! -x "$VENV/bin/python" ]]; then
    if command -v uv >/dev/null 2>&1; then
      if ! run uv venv --python "$PYTHON_BIN" "$VENV"; then
        die "could not create Python environment at $VENV with uv"
      fi
    elif ! run "$PYTHON_BIN" -m venv "$VENV"; then
      die "could not create Python venv at $VENV; load uv or install venv support"
    fi
  fi
  [[ -x "$VENV/bin/python" ]] || die "failed to create Python environment: $VENV"
  if command -v uv >/dev/null 2>&1; then
    run uv pip install --python "$VENV/bin/python" -e "${ROOT}[dev]"
  else
    run "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
    run "$VENV/bin/python" -m pip install -e "${ROOT}[dev]"
  fi
  if (( CLOUD_DEPS )); then
    [[ "$OS_ID" != macos ]] || die '--cloud-deps requires Linux'
    if command -v uv >/dev/null 2>&1; then
      run uv pip install --python "$VENV/bin/python" --require-hashes -r "$ROOT/cloud/lambda/requirements-linux-x86_64.txt"
    else
      run "$VENV/bin/python" -m pip install --require-hashes -r "$ROOT/cloud/lambda/requirements-linux-x86_64.txt"
    fi
  fi
}

run_local_checks() {
  [[ -x "$VENV/bin/python" ]] || die "Python environment is missing: $VENV"
  run "$VENV/bin/python" -m compileall -q "$ROOT/src" "$ROOT/scripts"
  run env PYTHONPATH="$ROOT/src" "$VENV/bin/python" -m unittest discover -s "$ROOT/tests" -q
  if command -v shellcheck >/dev/null 2>&1; then
    run shellcheck "$ROOT/start.sh"
  else
    printf 'ShellCheck: skipped (optional developer tool unavailable)\n'
  fi
}

PLATFORM_SUFFIX=""
if (( IN_CONTAINER )); then
  PLATFORM_SUFFIX=' (container)'
fi
printf 'Repository: %s\n' "$ROOT"
printf 'Platform: %s %s%s\n' "${OS_ID:-unknown}" "${OS_VERSION:-unknown}" "$PLATFORM_SUFFIX"

if (( DRY_RUN )); then
  printf 'DRY-RUN: no files, packages, virtualenv, or artifacts will be changed\n'
  printf 'Planned base setup:\n'
  if [[ "$OS_ID" == ubuntu || "$OS_ID" == debian ]]; then
    printf '  apt-get:'
    printf ' %s' "${UBUNTU_PACKAGES[@]}"
    printf '\n'
  elif [[ "$OS_ID" == macos ]]; then
    printf '  brew:'
    printf ' %s' "${MACOS_PACKAGES[@]}"
    printf '\n'
  elif [[ "$OS_ID" == rhel || "$OS_ID" == rocky || "$OS_ID" == almalinux || "$OS_ID" == fedora ]]; then
    printf '  provider-managed modules/packages (no system mutation)\n'
  else
    printf '  unsupported platform\n'
  fi
  printf '  venv: %s\n' "$VENV"
  (( CLOUD_DEPS )) && printf '  cloud lock: %s\n' "$ROOT/cloud/lambda/requirements-linux-x86_64.txt"
  (( REQUIRE_DOCKER )) && printf '  Docker/NVIDIA runtime: required and fail-closed in containers\n'
  exit 0
fi

if (( CHECK_ONLY )); then
  verify_base_tools
  verify_runtime
  if [[ -x "$VENV/bin/python" ]]; then
    printf 'Project Python environment: %s\n' "$VENV"
  else
    printf 'Project Python environment: not initialized (run setup to create %s)\n' "$VENV"
  fi
  printf 'CHECK: host prerequisites passed\n'
  exit 0
fi

install_system_packages
verify_base_tools
verify_runtime
prepare_python
run_local_checks

cat <<EOF
Setup complete.
Activate the environment with:
  source "$VENV/bin/activate"

No Docker daemon, model server, GPU workload, or experiment was started.
For exact A100/H100 Docker validation, use a full VM and run:
  ./start.sh --check-only --require-docker --require-gpu
EOF
