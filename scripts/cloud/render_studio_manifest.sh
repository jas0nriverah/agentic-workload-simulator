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
FORCE=0

usage() {
  echo 'Usage: render_studio_manifest.sh [--source FILE] [--output FILE] [--studio-root DIR] [--force]'
}

while (($#)); do
  case "$1" in
    --source) [[ $# -gt 1 ]] || { echo '--source requires a file' >&2; exit 2; }; SOURCE="$2"; shift 2;;
    --source=*) SOURCE="${1#*=}"; shift;;
    --output) [[ $# -gt 1 ]] || { echo '--output requires a file' >&2; exit 2; }; OUTPUT="$2"; shift 2;;
    --output=*) OUTPUT="${1#*=}"; shift;;
    --studio-root) [[ $# -gt 1 ]] || { echo '--studio-root requires a directory' >&2; exit 2; }; STUDIO_ROOT="$2"; shift 2;;
    --studio-root=*) STUDIO_ROOT="${1#*=}"; shift;;
    --force) FORCE=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2;;
  esac
done

[[ -f "$SOURCE" ]] || { echo "source manifest not found: $SOURCE" >&2; exit 1; }
[[ "$STUDIO_ROOT" = /* && "$STUDIO_ROOT" != */ ]] || {
  echo 'studio root must be an absolute path without a trailing slash' >&2
  exit 1
}
if [[ -e "$OUTPUT" && ! "$FORCE" -eq 1 ]]; then
  echo "refusing to overwrite existing manifest; pass --force: $OUTPUT" >&2
  exit 1
fi
mkdir -p -- "$(dirname -- "$OUTPUT")"

python3 - "$SOURCE" "$OUTPUT" "$STUDIO_ROOT" <<'PY'
from pathlib import Path
import sys

source, output, studio_root = map(Path, sys.argv[1:])
text = source.read_text(encoding="utf-8")
if "/home/ubuntu" not in text:
    raise SystemExit("source manifest has no reviewed /home/ubuntu paths to render")
rendered = text.replace("/home/ubuntu", str(studio_root))
if "VLLM_API_KEY=local-only-placeholder" not in rendered:
    raise SystemExit("rendered manifest lost the non-secret API-key placeholder")
Path(output).write_text(rendered, encoding="utf-8")
PY

chmod 600 "$OUTPUT"
echo "Rendered untracked Studio manifest: $OUTPUT"
echo "Studio root: $STUDIO_ROOT"
