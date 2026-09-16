"""Regression tests for the dirty-worktree execution snapshot utility."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

# The repository's ``scripts`` tree is intentionally a source namespace, not
# an installed package; keep this test runnable through the project venv too.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation import materialize_execution_snapshot as snapshot


def run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def make_dirty_repo(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "working"
    source.mkdir()
    run_git(source, "init", "--quiet")
    run_git(source, "config", "user.email", "snapshot-tests@example.invalid")
    run_git(source, "config", "user.name", "snapshot-tests")
    (source / ".gitignore").write_text("ignored/\n*.cache\n", encoding="utf-8")
    (source / "tracked.txt").write_text("HEAD bytes\n", encoding="utf-8")
    (source / "unchanged.txt").write_text("stable\n", encoding="utf-8")
    run_git(source, "add", ".")
    run_git(source, "commit", "--quiet", "-m", "fixture")

    # Dirty tracked bytes and an untracked source are the bytes a HEAD-only
    # copy would lose.  The ignored file must appear only in the exclusion
    # inventory, never in the execution snapshot.
    (source / "tracked.txt").write_text("CURRENT dirty bytes\n", encoding="utf-8")
    (source / "untracked_source.py").write_text("print('current')\n", encoding="utf-8")
    (source / "ignored").mkdir()
    (source / "ignored" / "cache.bin").write_bytes(b"ignored cache")
    (source / "local.cache").write_text("ignored suffix\n", encoding="utf-8")
    context = tmp_path / "context.json"
    context.write_text('{"context": "bound"}\n', encoding="utf-8")
    return source, context


def clone_head(source: Path, destination: Path) -> None:
    subprocess.run(["git", "clone", "--quiet", str(source), str(destination)], check=True)


def test_inventory_records_dirty_tracked_untracked_and_ignored(tmp_path):
    source, context = make_dirty_repo(tmp_path)
    output = tmp_path / "inventory"

    result = snapshot.inventory(source, output, [context])
    report = json.loads((output / "working_inventory.json").read_text(encoding="utf-8"))

    assert result["modified_tracked"] == 1
    assert result["untracked"] == 1
    assert result["ignored"] >= 2
    assert "tracked.txt" in report["modified_tracked"]
    assert "untracked_source.py" in report["untracked"]
    ignored = {row["path"] for row in report["ignored"]}
    assert {"ignored/cache.bin", "local.cache"} <= ignored
    assert {row["path"] for row in report["files"]} >= {
        ".gitignore",
        "tracked.txt",
        "unchanged.txt",
        "untracked_source.py",
    }
    assert report["context"][0]["sha256"] == snapshot.digest(context)
    assert not (output / "ignored" / "cache.bin").exists()


def test_inventory_refuses_to_write_inside_working_implementation(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    with pytest.raises(ValueError, match="outside the working implementation"):
        snapshot.inventory(source, source / "inventory", [])


def test_materialize_copies_current_bytes_not_head_and_inventories_exclusions(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    destination = tmp_path / "staging"
    receipt = tmp_path / "receipts"
    clone_head(source, destination)

    result = snapshot.materialize(source, destination, receipt)
    manifest_path = receipt / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    names = {row["path"] for row in manifest["files"]}
    excluded = {row["path"] for row in manifest["excluded"]}

    assert result["status"] == "exact_byte_match"
    assert manifest["ancestry_git_head"] == run_git(source, "rev-parse", "HEAD").strip()
    assert manifest["inventory_policy"].startswith("Every current Git-visible")
    assert "tracked.txt" in names and "untracked_source.py" in names
    assert "ignored/cache.bin" not in names and "local.cache" not in names
    assert {"ignored/cache.bin", "local.cache"} <= excluded
    assert (destination / "tracked.txt").read_text(encoding="utf-8") == "CURRENT dirty bytes\n"
    assert (destination / "untracked_source.py").read_text(encoding="utf-8") == "print('current')\n"
    assert run_git(source, "show", "HEAD:tracked.txt") == "HEAD bytes\n"
    assert (destination / "tracked.txt").read_text(encoding="utf-8") != run_git(source, "show", "HEAD:tracked.txt")
    assert snapshot.verify(manifest_path, compare_working=True)["status"] == "pass"


def test_verify_rejects_source_drift_and_execution_tamper(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    destination = tmp_path / "staging"
    receipt = tmp_path / "receipts"
    clone_head(source, destination)
    snapshot.materialize(source, destination, receipt)
    manifest_path = receipt / "source_manifest.json"

    (source / "tracked.txt").write_text("source drift\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source bytes/mode changed"):
        snapshot.verify(manifest_path, compare_working=True)

    # The execution copy is independently checked when comparing only the
    # saved staging root; source drift must not mask a destination mutation.
    (destination / "untracked_source.py").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source bytes/mode changed"):
        snapshot.verify(manifest_path, compare_working=False)


def test_verify_rejects_tampered_receipt_hash(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    destination = tmp_path / "staging"
    receipt = tmp_path / "receipts"
    clone_head(source, destination)
    snapshot.materialize(source, destination, receipt)
    manifest_path = receipt / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unbound or incomplete"):
        snapshot.verify(manifest_path, compare_working=False)


def test_materialize_refuses_head_only_or_dirty_destination(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    destination = tmp_path / "staging"
    receipt = tmp_path / "receipts"
    clone_head(source, destination)
    (destination / "destination-change.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="pristine staging Git clone"):
        snapshot.materialize(source, destination, receipt)


def test_materialize_refuses_nested_receipts_and_ignored_destination_files(tmp_path):
    source, _ = make_dirty_repo(tmp_path)
    destination = tmp_path / "staging"
    clone_head(source, destination)

    with pytest.raises(ValueError, match="separate paths"):
        snapshot.materialize(source, destination, destination / "receipts")

    (destination / "ignored").mkdir()
    (destination / "ignored" / "cache.bin").write_bytes(b"stale cache")
    with pytest.raises(ValueError, match="ignored files present"):
        snapshot.materialize(source, destination, tmp_path / "receipts")
