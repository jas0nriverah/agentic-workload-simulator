#!/usr/bin/env bash
set -Eeuo pipefail

# Render the reviewed Lambda manifest for a persistent Studio workspace.
# This only rewrites provider-local /home/ubuntu paths; pins and command
# contracts remain byte-for-byte unchanged. The output is intentionally
# untracked and must never contain a real API key.

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SOURCE="$ROOT/cloud/lambda/instance_manifest.env.example"
OUTPUT="$ROOT/cloud/lambda/instance_manifest.env"
STUDIO_ROOT="${LIGHTNING_STUDIO_ROOT:-/teamspace/studios/this_studio}"
PYTHON_ENV_MODE="${LIGHTNING_PYTHON_ENV_MODE:-managed}"
PYTHON_ENV_ROOT="${LIGHTNING_PYTHON_ENV_ROOT:-}"
FORCE=0
DRY=0

usage() {
  echo 'Usage: render_studio_manifest.sh [--source FILE] [--output FILE] [--studio-root DIR] [--python-env-mode managed|venv] [--python-env-root DIR] [--force] [--dry-run]'
}

while (($#)); do
  case "$1" in
    --source) [[ $# -gt 1 ]] || { echo '--source requires a file' >&2; exit 2; }; SOURCE="$2"; shift 2;;
    --source=*) SOURCE="${1#*=}"; shift;;
    --output) [[ $# -gt 1 ]] || { echo '--output requires a file' >&2; exit 2; }; OUTPUT="$2"; shift 2;;
    --output=*) OUTPUT="${1#*=}"; shift;;
    --studio-root) [[ $# -gt 1 ]] || { echo '--studio-root requires a directory' >&2; exit 2; }; STUDIO_ROOT="$2"; shift 2;;
    --studio-root=*) STUDIO_ROOT="${1#*=}"; shift;;
    --python-env-mode) [[ $# -gt 1 ]] || { echo '--python-env-mode requires managed or venv' >&2; exit 2; }; PYTHON_ENV_MODE="$2"; shift 2;;
    --python-env-mode=*) PYTHON_ENV_MODE="${1#*=}"; shift;;
    --python-env-root) [[ $# -gt 1 ]] || { echo '--python-env-root requires a directory' >&2; exit 2; }; PYTHON_ENV_ROOT="$2"; shift 2;;
    --python-env-root=*) PYTHON_ENV_ROOT="${1#*=}"; shift;;
    --force) FORCE=1; shift;;
    --dry-run) DRY=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

case "$PYTHON_ENV_MODE" in
  managed|venv) ;;
  *) echo "python environment mode must be managed or venv: $PYTHON_ENV_MODE" >&2; exit 1;;
esac

[[ -f "$SOURCE" ]] || { echo "source manifest not found: $SOURCE" >&2; exit 1; }
[[ "$STUDIO_ROOT" = /* && "$STUDIO_ROOT" != */ ]] || {
  echo 'studio root must be an absolute path without a trailing slash' >&2
  exit 1
}
if (( ! DRY )) && [[ -e "$OUTPUT" && ! "$FORCE" -eq 1 ]]; then
  echo "refusing to overwrite existing manifest; pass --force: $OUTPUT" >&2
  exit 1
fi
(( DRY )) || mkdir -p -- "$(dirname -- "$OUTPUT")"

python3 - "$SOURCE" "$OUTPUT" "$STUDIO_ROOT" "$PYTHON_ENV_MODE" "$PYTHON_ENV_ROOT" "$DRY" "$ROOT" <<'PY'
from pathlib import Path
import hashlib
import os
import subprocess
import sys

source = Path(sys.argv[1])
output = Path(sys.argv[2])
studio_root = Path(sys.argv[3])
python_env_mode = sys.argv[4]
provided_env_root = bool(sys.argv[5])
python_env_root = Path(sys.argv[5]) if provided_env_root else None
dry = sys.argv[6] == "1"
repo_root = Path(sys.argv[7]).resolve()
text = source.read_text(encoding="utf-8")
if "/home/ubuntu" not in text:
    raise SystemExit("source manifest has no reviewed /home/ubuntu paths to render")
rendered = text.replace("/home/ubuntu", str(studio_root))
# The Lambda example keeps the source checkout under agentic-work/source for
# archive extraction. A Studio checks out this repository directly, so the
# lock path must point at the checked-out project instead of a nested source
# directory that does not exist in the Studio.
rendered = rendered.replace(
    f"{studio_root}/agentic-work/source/agentic-workload-simulator",
    f"{studio_root}/agentic-workload-simulator",
)
if python_env_mode == "managed":
    if python_env_root is None:
        python_env_root = Path(sys.prefix)
    if not python_env_root.is_absolute() or str(python_env_root) == "/":
        raise SystemExit(f"managed Python environment root must be an absolute non-root path: {python_env_root}")
    target_python = python_env_root / "bin" / "python"
    if not target_python.is_file() or not os.access(target_python, os.X_OK):
        if provided_env_root:
            raise SystemExit(f"managed Python interpreter is unavailable: {target_python}")
        # macOS developer Python installations may not provide the conventional
        # $prefix/bin/python symlink. The Studio path does; this fallback only
        # keeps local dry-runs honest about the interpreter actually running
        # the renderer and never changes a provider-supplied prefix.
        target_python = Path(sys.executable)
    try:
        target_version_exact = subprocess.check_output(
            [str(target_python), "-c", "import platform; print(platform.python_version())"],
            text=True,
        ).strip()
        target_prefix = subprocess.check_output(
            [str(target_python), "-c", "import sys; print(sys.prefix)"],
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"could not inspect managed Python interpreter: {target_python}: {exc}") from exc
    if Path(target_prefix).resolve() != python_env_root.resolve():
        raise SystemExit(
            f"managed Python prefix mismatch: interpreter={target_prefix} configured={python_env_root}"
        )
    target_parts = target_version_exact.split(".")
    python_version = ".".join(target_parts[:2])
else:
    python_env_root = python_env_root or (studio_root / "agentic-work" / "venv")
    if not python_env_root.is_absolute() or str(python_env_root) == "/":
        raise SystemExit(f"venv Python environment root must be an absolute non-root path: {python_env_root}")
    target_version_exact = None
    python_version = None
# Every command contract that used the Lambda venv must use the already
# managed Studio interpreter. This is deliberately a provider adapter; the
# canonical Lambda manifest still creates a fresh venv.
rendered = rendered.replace(f"{studio_root}/agentic-work/venv", str(python_env_root))

def upsert(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    replacement = f"{key}={value}"
    found = False
    out = []
    for line in lines:
        if line.startswith(f"{key}="):
            if not found:
                out.append(replacement)
                found = True
        else:
            out.append(line)
    if not found:
        out.append(replacement)
    return "\n".join(out) + "\n"

rendered = upsert(rendered, "PYTHON_ENV_MODE", python_env_mode)
rendered = upsert(rendered, "PYTHON_ENV_ROOT", str(python_env_root))
if python_version is not None:
    rendered = upsert(rendered, "PYTHON_VERSION", python_version)
    rendered = upsert(rendered, "PYTHON_VERSION_EXACT", target_version_exact)

# If a provider-specific lock has been generated in the checkout, select it
# automatically for the managed interpreter. If it is absent, retain the
# reviewed 3.11 lock and let bootstrap fail closed with an explicit
# resolution-mismatch message; never relabel a 3.11 lock as 3.12.
if python_env_mode == "managed":
    lock_root = repo_root / "cloud" / "lambda"
    base_lock = lock_root / "requirements-linux-x86_64.txt"
    candidate = lock_root / f"requirements-linux-x86_64-py{python_version.replace('.', '')}.txt"
    selected_lock = candidate if candidate.is_file() else base_lock
    if not selected_lock.is_file():
        raise SystemExit(f"managed Python lock is unavailable: {selected_lock}")
    rendered = upsert(rendered, "PYTHON_LOCK_PATH", str(selected_lock))
    rendered = upsert(
        rendered,
        "PYTHON_LOCK_SHA256",
        hashlib.sha256(selected_lock.read_bytes()).hexdigest(),
    )
if "VLLM_API_KEY=local-only-placeholder" not in rendered:
    raise SystemExit("rendered manifest lost the non-secret API-key placeholder")
if not dry:
    output.write_text(rendered, encoding="utf-8")
PY

if (( DRY )); then
  echo "DRY-RUN: would render untracked Studio manifest: $OUTPUT"
else
  chmod 600 "$OUTPUT"
  echo "Rendered untracked Studio manifest: $OUTPUT"
fi
echo "Studio root: $STUDIO_ROOT"
echo "Python environment: $PYTHON_ENV_MODE${PYTHON_ENV_ROOT:+ ($PYTHON_ENV_ROOT)}"
