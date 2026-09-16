import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validation import capture_acquisition_inputs as acquisition


def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    revision = "a" * 40
    rows = []
    for index, name in enumerate(acquisition.SMALL_INPUTS):
        data = (f"fixture-{index}\r\n" + "{%- exact bytes -%}\n").encode()
        (root / name).write_bytes(data)
        rows.append({"name": name, "bytes": len(data), "sha256": acquisition.sha256(data),
                     "hub_revision": revision, "hub_etag": "b" * 40,
                     "metadata": revision + "\n" + "b" * 40 + "\n", "verified": True})
    rows.append({"name": "model-00001-of-00001.safetensors", "bytes": 60_000_000_000,
                 "sha256": "c" * 64, "hub_revision": revision, "verified": True})
    report = tmp_path / "verification.json"
    value = {"schema_version": acquisition.REPORT_SCHEMA, "model_root": str(root),
             "expected_revision": revision, "status": "pass", "failures": [],
             "files": rows, "weight_shard_count": 1, "weight_file_bytes": 60_000_000_000}
    report.write_text(json.dumps(value, indent=3) + "\n")
    return root, report, acquisition.sha256(report.read_bytes())


def assert_bundle(output):
    value = json.loads((output / "capture.json").read_bytes())
    for row in value["artifacts"]:
        path = output / row["path"]
        assert len(path.read_bytes()) == row["bytes"]
        assert acquisition.sha256(path.read_bytes()) == row["sha256"]
        assert path.with_name(path.name + ".sha256").read_text() == f"{row['sha256']}  {path.name}\n"
    manifest = output / "capture.json"
    assert (output / "capture.json.sha256").read_text() == f"{acquisition.sha256(manifest.read_bytes())}  capture.json\n"
    return value


def test_snapshot_original_bytes_and_weight_report_only(tmp_path):
    root, report, digest = snapshot(tmp_path)
    opened = []

    def reader(name, limit):
        opened.append(name)
        assert name in acquisition.SMALL_INPUTS
        return acquisition.read_bounded(root / name, limit)

    output = tmp_path / "archive"
    result = acquisition.archive_snapshot(report, digest, output, reader=reader)
    assert result["status"] == "pass"
    assert opened == list(acquisition.SMALL_INPUTS)
    assert result["weight_bytes_read_or_copied"] == 0
    assert result["weight_file_bytes"] == 60_000_000_000
    assert result["model_revision"] == result["tokenizer_revision"] == "a" * 40
    assert (output / "snapshot_verification.json").read_bytes() == report.read_bytes()
    for name in acquisition.SMALL_INPUTS:
        assert (output / "inputs" / name).read_bytes() == (root / name).read_bytes()
    assert_bundle(output)
    with pytest.raises(FileExistsError):
        acquisition.archive_snapshot(report, digest, output)


@pytest.mark.parametrize("fault", ["report_hash", "failed_report", "file_bytes", "revision", "size", "duplicate", "escape"])
def test_snapshot_failure_retains_fail_manifest(tmp_path, fault):
    root, report, digest = snapshot(tmp_path)
    value = json.loads(report.read_bytes())
    if fault == "report_hash":
        digest = "0" * 64
    elif fault == "file_bytes":
        (root / "tokenizer.json").write_bytes(b"changed")
    elif fault == "escape":
        target = tmp_path / "outside"
        target.write_bytes((root / "tokenizer.json").read_bytes())
        (root / "tokenizer.json").unlink()
        (root / "tokenizer.json").symlink_to(target)
    else:
        if fault == "failed_report":
            value["status"] = "fail"
        elif fault == "revision":
            value["files"][0]["hub_revision"] = "d" * 40
        elif fault == "size":
            value["files"][0]["bytes"] = acquisition.MAX_FILE_BYTES + 1
        elif fault == "duplicate":
            value["files"].append(value["files"][0])
        report.write_text(json.dumps(value))
        digest = acquisition.sha256(report.read_bytes())
    output = tmp_path / "archive"
    result = acquisition.archive_snapshot(report, digest, output)
    assert result["status"] == "fail"
    assert result["errors"]
    assert_bundle(output)


def test_source_changed_while_reading_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "file"
    path.write_bytes(b"data")
    original = os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"mutated")
        return original(fd)

    monkeypatch.setattr(os, "fstat", changed)
    with pytest.raises(acquisition.CaptureError, match="changed"):
        acquisition.read_bounded(path, 100)


def fixture_docker(tmp_path, *, running=True, mutate=False):
    proc = tmp_path / "proc"
    sysfs = tmp_path / "sys"
    (proc / "self").mkdir(parents=True)
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("fixture-boot\n")
    host = b"21 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    (proc / "self/mountinfo").write_bytes(host)
    (proc / "42").mkdir()
    (proc / "42/stat").write_text("42 (fixture with spaces) " + " ".join(["S"] + ["0"] * 18 + ["12345"] + ["0"] * 10))
    block = sysfs / "devices/block/sda/sda1"
    block.mkdir(parents=True)
    (block / "dev").write_text("8:1\n")
    (block / "uevent").write_text("MAJOR=8\nMINOR=1\nDEVNAME=sda1\n")
    (sysfs / "dev/block").mkdir(parents=True)
    (sysfs / "dev/block/8:1").symlink_to(block)
    value = {"id": "f" * 64, "name": "/explicit-fixture", "image": "sha256:" + "1" * 64,
             "running": running, "status": "running" if running else "exited", "pid": 42 if running else 0,
             "started_at": "fixture-start", "restart_count": 0,
             "driver": "overlay2", "upper_dir": None, "lower_dir": None, "merged_dir": None,
             "work_dir": None, "mounts": [], "devices": []}
    calls = []

    def run(argv, **bounds):
        assert 0 < bounds["timeout"] <= 60
        assert bounds["limit"] <= acquisition.MAX_METADATA_BYTES
        calls.append(argv)
        if argv[1] == "inspect":
            assert argv[2] == "--format" and "Config.Env" not in argv[3]
            assert ".Config.Cmd" not in argv[3] and ".Config.Labels" not in argv[3]
            if mutate and len([c for c in calls if c[1] == "inspect"]) > 1:
                value["restart_count"] += 1
            return json.dumps(value).encode()
        if argv[1] == "info":
            return json.dumps(socket.gethostname()).encode()
        assert argv == ["docker", "exec", "f" * 64, "cat", "/proc/self/mountinfo"]
        return b"40 1 0:88 / / rw - overlay overlay rw,lowerdir=/lower\n41 40 8:1 /bind /space\\040name ro - ext4 /dev/sda1 ro\n"
    return proc, sysfs, run, calls


def test_live_mount_capture_binding_and_no_env_or_mutation(tmp_path):
    proc, sysfs, run, calls = fixture_docker(tmp_path)
    output = tmp_path / "mounts"
    result = acquisition.capture_mounts("f" * 12, output, run=run, proc_root=proc, sys_root=sysfs)
    assert result["status"] == "pass", result["errors"]
    assert result["container_host_identity"]["start_ticks"] == 12345
    assert [c[1] for c in calls] == ["inspect", "info", "exec", "inspect"]
    assert (output / "host_mountinfo.txt").read_bytes() == (proc / "self/mountinfo").read_bytes()
    parsed = json.loads((output / "container_mounts.json").read_bytes())
    assert parsed[1]["mountpoint"] == "/space name"
    devices = json.loads((output / "host_device_sysfs.json").read_bytes())
    physical = next(row for row in devices if row["major_minor"] == "8:1")
    assert physical["files"]["dev"]["text"] == "8:1\n"
    assert_bundle(output)


def test_stopped_fixture_preserves_metadata_but_cannot_claim_live_proof(tmp_path):
    proc, sysfs, run, calls = fixture_docker(tmp_path, running=False)
    output = tmp_path / "mounts"
    result = acquisition.capture_mounts("f" * 12, output, run=run, proc_root=proc, sys_root=sysfs)
    assert result["status"] == "fail"
    assert any("stopped" in row["reason"] for row in result["errors"])
    assert [c[1] for c in calls] == ["inspect", "info", "inspect"]
    assert not (output / "container_mountinfo.txt").exists()
    assert_bundle(output)


def test_container_restart_during_capture_is_not_accepted(tmp_path):
    proc, sysfs, run, _ = fixture_docker(tmp_path, mutate=True)
    result = acquisition.capture_mounts("f" * 12, tmp_path / "mounts", run=run, proc_root=proc, sys_root=sysfs)
    assert result["status"] == "fail"
    assert any("changed" in row["reason"] for row in result["errors"])


@pytest.mark.parametrize("data", [b"", b"bad mount line\n", b"1 2 not:device / / rw - ext4 /dev/sda1 rw\n"])
def test_mountinfo_malformed_fails_closed(data):
    with pytest.raises(acquisition.CaptureError):
        acquisition.parse_mountinfo(data)


def test_real_command_limits_and_stderr_not_disclosed():
    with pytest.raises(acquisition.CaptureError, match="output bound"):
        acquisition.bounded_command([sys.executable, "-c", "import os; os.write(1,b'x'*8192)"], limit=100)
    start = time.monotonic()
    with pytest.raises(acquisition.CaptureError, match="time bound"):
        acquisition.bounded_command([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1)
    assert time.monotonic() - start < 3
    with pytest.raises(acquisition.CaptureError) as error:
        acquisition.bounded_command([sys.executable, "-c", "import sys;sys.stderr.write('SECRET_SENTINEL');sys.exit(1)"])
    assert "SECRET_SENTINEL" not in str(error.value)


def test_ssh_reader_uses_only_six_explicit_paths_and_safe_quoting(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["ssh", "-o", "BatchMode=yes"]
        assert "python3 -c" in argv[-1]
        return b"fixture"

    monkeypatch.setattr(acquisition, "bounded_command", run)
    read = acquisition.ssh_snapshot_reader("/snapshot with spaces", "user@host", control_path=Path("/tmp/control"))
    assert read("tokenizer.json", 10) == b"fixture"
    assert "'/snapshot with spaces'" in calls[0][-1]
    with pytest.raises(acquisition.CaptureError):
        acquisition.ssh_snapshot_reader("/snapshot", "--malicious-option")
