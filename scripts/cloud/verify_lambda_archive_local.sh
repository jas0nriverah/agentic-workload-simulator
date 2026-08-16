#!/usr/bin/env bash
# Verify a received Lambda archive and its embedded SHA-256 manifest.
# Never overwrites or deletes the received archive.
set -Eeuo pipefail
IFS=$'\n\t'
ARCHIVE=
MANIFEST=
RECEIPT=
MIN_FREE_BYTES=0
DRY_RUN=0
usage() {
  cat <<'USAGE'
Usage: verify_lambda_archive_local.sh --archive PATH [options]
  --archive PATH       Received .tar.gz archive (required)
  --manifest PATH      External SHA-256 file (default: archive basename + .sha256)
  --receipt PATH       Write verification receipt (default: beside archive)
  --min-free-bytes N   Required free space before temporary extraction
  --dry-run            Check arguments and print the plan without extracting
  -h, --help           Show help
USAGE
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --archive) [[ $# -ge 2 ]] || die '--archive requires a path'; ARCHIVE=$2; shift 2 ;;
    --manifest) [[ $# -ge 2 ]] || die '--manifest requires a path'; MANIFEST=$2; shift 2 ;;
    --receipt) [[ $# -ge 2 ]] || die '--receipt requires a path'; RECEIPT=$2; shift 2 ;;
    --min-free-bytes) [[ $# -ge 2 ]] || die '--min-free-bytes requires bytes'; MIN_FREE_BYTES=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$ARCHIVE" ]] || die '--archive is required'
[[ -f "$ARCHIVE" ]] || die "archive does not exist: $ARCHIVE"
ARCHIVE=$(cd -- "$(dirname -- "$ARCHIVE")" && pwd -P)/$(basename -- "$ARCHIVE")
[[ "$ARCHIVE" =~ \.tar\.gz$ ]] || die 'archive must end in .tar.gz'
if [[ -z "$MANIFEST" ]]; then MANIFEST="${ARCHIVE%.tar.gz}.sha256"; fi
[[ -f "$MANIFEST" ]] || die "external SHA-256 manifest does not exist: $MANIFEST"
[[ "$MIN_FREE_BYTES" =~ ^[0-9]+$ ]] || die 'min free bytes must be a non-negative integer'
if [[ -z "$RECEIPT" ]]; then RECEIPT="${ARCHIVE%.tar.gz}.verification_receipt.json"; fi
ARCHIVE_BYTES=$(stat -c '%s' "$ARCHIVE" 2>/dev/null || stat -f '%z' "$ARCHIVE")
if (( MIN_FREE_BYTES == 0 )); then MIN_FREE_BYTES=$((ARCHIVE_BYTES * 2 + 1048576)); fi
ARCHIVE_DIR=$(dirname -- "$ARCHIVE")
FREE_BYTES=$(df -Pk "$ARCHIVE_DIR" | awk 'NR==2 {print $4 * 1024}')
[[ "$FREE_BYTES" =~ ^[0-9]+$ ]] || die "could not determine free space for $ARCHIVE_DIR"
(( FREE_BYTES >= MIN_FREE_BYTES )) || die "insufficient free space: ${FREE_BYTES} < ${MIN_FREE_BYTES} bytes"
ARCHIVE_LABEL="$ARCHIVE" MANIFEST_LABEL="$MANIFEST" python3 - <<'PY'
import hashlib
import os
import pathlib
import re
import sys
archive = pathlib.Path(os.environ["ARCHIVE_LABEL"])
manifest = pathlib.Path(os.environ["MANIFEST_LABEL"])
lines = [line.strip() for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
if len(lines) != 1:
    print("external manifest must contain exactly one archive checksum line", file=sys.stderr); raise SystemExit(2)
parts = lines[0].split()
if len(parts) < 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
    print("external manifest has an invalid SHA-256 line", file=sys.stderr); raise SystemExit(3)
if pathlib.Path(parts[-1]).name != archive.name:
    print("external manifest filename does not match the archive", file=sys.stderr); raise SystemExit(4)
digest = hashlib.sha256()
with archive.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""): digest.update(block)
if digest.hexdigest().lower() != parts[0].lower():
    print("archive SHA-256 mismatch", file=sys.stderr); raise SystemExit(5)
print(digest.hexdigest())
PY
if (( DRY_RUN )); then
  printf 'DRY-RUN: archive=%s\nDRY-RUN: manifest=%s\nDRY-RUN: required_free_bytes=%s\n' "$ARCHIVE" "$MANIFEST" "$MIN_FREE_BYTES"
  exit 0
fi
TMP_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/lambda-verify.XXXXXX")
trap 'rm -rf -- "$TMP_ROOT"' EXIT
EXTRACT_ROOT="$TMP_ROOT/extracted"
mkdir -p "$EXTRACT_ROOT"
ARCHIVE_LABEL="$ARCHIVE" EXTRACT_LABEL="$EXTRACT_ROOT" python3 - <<'PY'
import os
import pathlib
import tarfile
import sys
archive = pathlib.Path(os.environ["ARCHIVE_LABEL"])
root = pathlib.Path(os.environ["EXTRACT_LABEL"]).resolve()
with tarfile.open(archive, "r:gz") as handle:
    members = handle.getmembers()
    for member in members:
        if member.issym() or member.islnk():
            print(f"archive contains a link, refusing extraction: {member.name}", file=sys.stderr); raise SystemExit(2)
        target = (root / member.name).resolve()
        try: target.relative_to(root)
        except ValueError:
            print(f"archive path escapes extraction root: {member.name}", file=sys.stderr); raise SystemExit(3)
    handle.extractall(root)
PY
CHECKSUMS="$EXTRACT_ROOT/.collection/SHA256SUMS"
[[ -f "$CHECKSUMS" ]] || die 'archive is missing .collection/SHA256SUMS'
CHECKSUMS_LABEL="$CHECKSUMS" ROOT_LABEL="$EXTRACT_ROOT" python3 - <<'PY'
import hashlib
import os
import pathlib
import re
import sys
root = pathlib.Path(os.environ["ROOT_LABEL"]).resolve()
checksums = pathlib.Path(os.environ["CHECKSUMS_LABEL"])
lines = [line.rstrip("\n") for line in checksums.read_text(encoding="utf-8").splitlines() if line.strip()]
if not lines: print("embedded SHA-256 manifest is empty", file=sys.stderr); raise SystemExit(2)
seen = set()
for line in lines:
    parts = line.split(maxsplit=1)
    if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        print(f"invalid embedded checksum line: {line}", file=sys.stderr); raise SystemExit(3)
    rel = pathlib.PurePosixPath(parts[1].strip())
    if rel.is_absolute() or ".." in rel.parts:
        print(f"unsafe embedded path: {rel}", file=sys.stderr); raise SystemExit(4)
    path = (root / pathlib.Path(*rel.parts)).resolve()
    try: path.relative_to(root)
    except ValueError: raise SystemExit(f"path escapes root: {rel}")
    if not path.is_file() or path.is_symlink():
        print(f"missing or non-regular artifact: {rel}", file=sys.stderr); raise SystemExit(5)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""): digest.update(block)
    if digest.hexdigest().lower() != parts[0].lower():
        print(f"embedded SHA-256 mismatch: {rel}", file=sys.stderr); raise SystemExit(6)
    seen.add(path)
print(f"verified_files={len(seen)}")
PY
ARCHIVE_LABEL="$ARCHIVE" MANIFEST_LABEL="$MANIFEST" RECEIPT_LABEL="$RECEIPT" MIN_FREE_LABEL="$MIN_FREE_BYTES" python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import time
archive = pathlib.Path(os.environ["ARCHIVE_LABEL"])
digest = hashlib.sha256()
with archive.open("rb") as handle:
    for block in iter(lambda: handle.read(1024 * 1024), b""): digest.update(block)
receipt = {
    "schema_version": "lambda-local-verification.v1",
    "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "archive": str(archive),
    "archive_bytes": archive.stat().st_size,
    "archive_sha256": digest.hexdigest(),
    "external_manifest": str(pathlib.Path(os.environ["MANIFEST_LABEL"])),
    "required_free_bytes": int(os.environ["MIN_FREE_LABEL"]),
    "status": "verified",
}
out = pathlib.Path(os.environ["RECEIPT_LABEL"])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
PY
printf 'Archive verified: %s\nReceipt: %s\n' "$ARCHIVE" "$RECEIPT"
