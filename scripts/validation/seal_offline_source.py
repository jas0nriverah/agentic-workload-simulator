#!/usr/bin/env python3
"""Seal exact repair source bytes without changing historical git provenance."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def seal(destination: Path):
    files = []
    for directory in ("src", "scripts", "tests", "configs", "docs", "cloud"):
        for path in sorted((ROOT / directory).rglob("*")):
            generated = any(part == "__pycache__" or part.endswith(".egg-info") for part in path.parts)
            if path.is_file() and not path.is_symlink() and not generated and path.suffix in {".py", ".c", ".h", ".sh", ".json", ".yaml", ".yml", ".toml", ".md", ".txt"}:
                files.append(path)
    for filename in ("pyproject.toml", "README.md", "uv.lock", ".python-version", ".gitignore", ".gitattributes", "LICENSE", "LICENSE.md", "LICENSE.txt"):
        if (ROOT / filename).is_file():
            files.append(ROOT / filename)
    files = sorted(set(files))
    destination.mkdir(parents=True, exist_ok=True)
    manifest = []
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            payload = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            manifest.append({"path": relative, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            archive.addfile(info, io.BytesIO(payload))
    payload = gzip.compress(tar_buffer.getvalue(), mtime=0)
    bundle = destination / "repair-source.tar.gz"
    bundle.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (destination / "repair-source.tar.gz.sha256").write_text(f"{digest}  {bundle.name}\n")
    git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=ROOT, text=True)
    record = {"schema_version": "assignment.offline-source-bundle.v1", "repository": str(ROOT),
              "git_head": git_head, "worktree_dirty_at_sealing": bool(status),
              "interpretation": "Git HEAD is ancestry; file hashes and bundle hash identify the actual repaired source. This is not the historical experiment revision.",
              "bundle": bundle.name, "bundle_sha256": digest, "files": manifest}
    metadata = destination / "source_manifest.json"
    metadata.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    metadata.with_suffix(".json.sha256").write_text(f"{hashlib.sha256(metadata.read_bytes()).hexdigest()}  {metadata.name}\n")
    return {"files": len(files), "bundle_sha256": digest, "bundle_size_bytes": len(payload)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(seal(args.destination), sort_keys=True, indent=2))
