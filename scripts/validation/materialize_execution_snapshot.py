#!/usr/bin/env python3
"""Materialize current tracked AND untracked bytes into a staging Git clone.

No source worktree edits, Git resets, inferred HEAD-only releases, or deletion.
Ignored caches/environments/data are enumerated separately; runtime dependencies
must be bound by the runtime manifest. Outputs never overwrite old receipts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def paths(root: Path, *args: str) -> list[str]:
    return sorted(set(git(root, "ls-files", "-z", *args).decode().strip("\0").split("\0")) - {""})


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def write_new(path: Path, value: object) -> str:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    with path.open("xb") as handle:
        handle.write(payload)
    sha = hashlib.sha256(payload).hexdigest()
    with Path(str(path) + ".sha256").open("x") as handle:
        handle.write(f"{sha}  {path.name}\n")
    return sha


def exclusion_reason(name: str) -> str:
    parts = Path(name).parts
    if ".venv" in parts or any(part.endswith(".egg-info") for part in parts):
        return "installed environment; interpreter/dependencies bound separately in runtime evidence"
    if any(part in {"__pycache__", ".pytest_cache", ".ruff_cache"} for part in parts) or name.endswith(".pyc"):
        return "generated cache; not execution source"
    return "Git-ignored local data/config; not selected for this source snapshot; required runtime inputs need separate binding"


def file_role(name: str) -> str:
    first = Path(name).parts[0]
    if first in {"src", "scripts"}:
        return "execution_or_analysis_source"
    if first in {"configs", "cloud"} or name in {"pyproject.toml", "uv.lock"}:
        return "configuration_runtime_or_dependency_contract"
    if first == "tests":
        return "verification_source"
    if first == "SNAP":
        return "historical_generated_evidence_preserved_not_current_execution_source"
    if first == "docs" or name.endswith(".md"):
        return "documentation_and_decisions"
    if first == "project":
        return "retained_project_source_and_historical_artifacts"
    return "other_retained_repository_file"


def inventory(source: Path, output: Path, context_files: list[Path]) -> dict:
    source = source.resolve()
    output = output.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError("inventory output must be outside the working implementation")
    output.mkdir(parents=True, exist_ok=False)
    tracked = set(paths(source, "--cached"))
    modified = set(git(source, "diff", "HEAD", "--name-only", "-z").decode().strip("\0").split("\0")) - {""}
    included = paths(source, "--cached", "--others", "--exclude-standard")
    records = []
    for name in included:
        path = source / name
        metadata = path.lstat()
        records.append({"path": name, "role": file_role(name),
                        "git_class": "modified_tracked" if name in modified else ("tracked" if name in tracked else "untracked"),
                        "mtime_ns": metadata.st_mtime_ns, "bytes": metadata.st_size,
                        "mode": metadata.st_mode & 0o777,
                        "sha256": digest(path) if path.is_file() and not path.is_symlink() else None,
                        "symlink": str(path.readlink()) if path.is_symlink() else None})
    ignored = [{"path": name, "reason": exclusion_reason(name)}
               for name in paths(source, "--others", "--ignored", "--exclude-standard")]
    report = {"schema_version": "assignment.working-implementation-inventory.v1",
              "captured_at": datetime.now(timezone.utc).isoformat(), "root": str(source),
              "ancestry_git_head": git(source, "rev-parse", "HEAD").decode().strip(),
              "modified_tracked": sorted(modified), "untracked": [row["path"] for row in records if row["git_class"] == "untracked"],
              "files": records, "ignored": ignored,
              "context": [{"path": str(path.resolve()), "sha256": digest(path), "mtime_ns": path.stat().st_mtime_ns}
                          for path in context_files]}
    write_new(output / "working_inventory.json", report)
    patch = git(source, "diff", "HEAD", "--binary")
    with (output / "tracked_changes.patch").open("xb") as handle:
        handle.write(patch)
    return {"inventory": str(output / "working_inventory.json"), "files": len(records),
            "modified_tracked": len(modified), "untracked": len(report["untracked"]),
            "ignored": len(ignored), "tracked_patch_sha256": hashlib.sha256(patch).hexdigest()}


def materialize(source: Path, destination: Path, receipt_dir: Path) -> dict:
    source, destination, receipt_dir = source.resolve(), destination.resolve(), receipt_dir.resolve()
    if source == destination or destination.is_relative_to(source) or receipt_dir.is_relative_to(source):
        raise ValueError("snapshot and receipts must be outside the working implementation")
    if destination == receipt_dir or destination.is_relative_to(receipt_dir) or receipt_dir.is_relative_to(destination):
        raise ValueError("snapshot and receipts must be separate paths")
    if not (destination / ".git").exists() or git(destination, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("destination must be a pristine staging Git clone")
    ignored_destination = paths(destination, "--others", "--ignored", "--exclude-standard")
    if ignored_destination:
        raise ValueError(f"destination must be a pristine staging Git clone; ignored files present: {ignored_destination}")
    source_head = git(source, "rev-parse", "HEAD").decode().strip()
    if git(destination, "rev-parse", "HEAD").decode().strip() != source_head:
        raise ValueError("staging clone ancestry differs from working implementation")
    receipt_dir.mkdir(parents=True, exist_ok=False)
    included = paths(source, "--cached", "--others", "--exclude-standard")
    tracked = set(paths(source, "--cached"))
    ignored = paths(source, "--others", "--ignored", "--exclude-standard")
    # A removed tracked file must not survive unnoticed from the staging HEAD.
    # This helper does not perform deletion; such a snapshot needs fresh staging.
    missing = [name for name in included if not (source / name).is_file()]
    if missing:
        raise ValueError(f"non-file or removed tracked inputs require explicit handling: {missing}")
    manifest = {
        "schema_version": "assignment.execution-source-snapshot.v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source), "execution_root": str(destination),
        "ancestry_git_head": source_head,
        "source_git_status": git(source, "status", "--porcelain=v1", "--untracked-files=all").decode(),
        "inventory_policy": "Every current Git-visible tracked/untracked file, including dirty changes and retained SNAP evidence; not git diff alone.",
        "files": [],
        "excluded": [{"path": ".git", "reason": "Git metadata; ancestry recorded and staging clone retains its own metadata"}]
                    + [{"path": name, "reason": exclusion_reason(name)} for name in ignored],
    }
    for name in included:
        path, target = source / name, destination / name
        if path.is_symlink() or target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != destination):
            raise ValueError(f"symlink source/destination requires explicit handling: {name}")
        before = digest(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        after, copied = digest(path), digest(target)
        if before != after or after != copied:
            raise ValueError(f"source changed or copy differs: {name}")
        manifest["files"].append({"path": name, "sha256": copied, "bytes": target.stat().st_size,
                                  "mode": target.stat().st_mode & 0o777,
                                  "role": file_role(name), "source_mtime_ns": path.stat().st_mtime_ns,
                                  "git_class": "tracked_current_bytes" if name in tracked else "untracked_current_bytes"})
    if paths(source, "--cached", "--others", "--exclude-standard") != included:
        raise ValueError("working implementation file inventory changed during copy")
    for row in manifest["files"]:
        if digest(source / row["path"]) != row["sha256"]:
            raise ValueError(f"working implementation changed during snapshot: {row['path']}")
    if paths(destination, "--cached", "--others", "--exclude-standard") != included:
        raise ValueError("execution snapshot has missing or extra Git-visible files")
    manifest["status"] = "exact_byte_match"
    sha = write_new(receipt_dir / "source_manifest.json", manifest)
    return {"status": manifest["status"], "source_manifest_sha256": sha,
            "files": len(manifest["files"]), "tracked": len(tracked),
            "untracked": len(included) - len(tracked), "excluded_files": len(manifest["excluded"]),
            "manifest": str(receipt_dir / "source_manifest.json")}


def verify(manifest_path: Path, *, compare_working: bool) -> dict:
    manifest = json.loads(manifest_path.read_bytes())
    expected = Path(str(manifest_path) + ".sha256").read_text().split()[0]
    if digest(manifest_path) != expected or manifest.get("status") != "exact_byte_match":
        raise ValueError("source manifest is unbound or incomplete")
    roots = [Path(manifest["execution_root"])]
    if compare_working:
        roots.append(Path(manifest["source_root"]))
    names = sorted(row["path"] for row in manifest["files"])
    for root in roots:
        if paths(root, "--cached", "--others", "--exclude-standard") != names:
            raise ValueError(f"source file inventory changed: {root}")
        for row in manifest["files"]:
            path = root / row["path"]
            if path.is_symlink() or digest(path) != row["sha256"] or (path.stat().st_mode & 0o777) != row["mode"]:
                raise ValueError(f"source bytes/mode changed: {path}")
    return {"status": "pass", "source_manifest_sha256": expected,
            "compared_working_implementation": compare_working, "files_checked_per_root": len(names)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("inventory")
    listing.add_argument("--source", type=Path, required=True)
    listing.add_argument("--output", type=Path, required=True)
    listing.add_argument("--context-file", type=Path, action="append", default=[])
    copy = sub.add_parser("materialize")
    copy.add_argument("--source", type=Path, required=True)
    copy.add_argument("--destination", type=Path, required=True)
    copy.add_argument("--receipt-dir", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--compare-working", action="store_true")
    args = parser.parse_args()
    if args.command == "inventory":
        print(json.dumps(inventory(args.source, args.output, args.context_file), sort_keys=True))
        return 0
    result = (materialize(args.source, args.destination, args.receipt_dir) if args.command == "materialize"
              else verify(args.manifest, compare_working=args.compare_working))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
