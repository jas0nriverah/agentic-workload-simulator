#!/usr/bin/env python3
"""Preserve verified small model inputs and read-only container mount evidence.

No model weights, dataset/gold files, container environment, inference, or
cleanup are collected. Each call creates a new evidence directory; a failed
capture retains its partial artifacts and a failure manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


SMALL_INPUTS = (
    "config.json", "generation_config.json", "tokenizer.json",
    "tokenizer_config.json", "model.safetensors.index.json", "chat_template.jinja",
)
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
REPORT_SCHEMA = "assignment.model-snapshot-verification.v1"
HEX64 = re.compile(r"[0-9a-f]{64}")


class CaptureError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) > limit:
        raise CaptureError(f"input exceeds byte bound: {path}")
    identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise CaptureError(f"input changed during capture: {path}")
    return data


def bounded_command(argv: Sequence[str], *, timeout: float = 15, limit: int = MAX_METADATA_BYTES) -> bytes:
    """Bound both subprocess time and bytes in memory; no shell or environment dump."""
    if not 0 < timeout <= 60 or not 0 < limit <= MAX_TOTAL_BYTES:
        raise CaptureError("invalid command capture bounds")
    process = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                raise CaptureError("read-only command exceeded time bound")
            for key, _ in selector.select(min(left, 0.2)):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                stream = buffers[key.data]
                if len(stream) + len(data) > (limit if key.data == "stdout" else 65536):
                    raise CaptureError("read-only command exceeded output bound")
                stream.extend(data)
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode:
            # Arbitrary stderr may contain application-owned values. Keep only
            # its digest/size in the error, never dump it into evidence or logs.
            raise CaptureError(f"read-only command exited {process.returncode}; stderr_bytes={len(buffers['stderr'])} stderr_sha256={sha256(buffers['stderr'])}")
        return bytes(buffers["stdout"])
    except (BaseException,):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise CaptureError(f"invalid {label} JSON") from exc
    if not isinstance(value, dict):
        raise CaptureError(f"{label} must be a JSON object")
    return value


class Bundle:
    def __init__(self, output: Path, schema: str):
        self.root = output.absolute()
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise CaptureError("output must not traverse a symlink")
        self.root.mkdir(parents=True, exist_ok=False)
        self.value: dict[str, Any] = {"schema_version": schema, "started_epoch_ns": time.time_ns(),
                                     "status": "incomplete", "errors": [], "artifacts": []}

    def put(self, name: str, data: bytes) -> dict[str, Any]:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise CaptureError("unsafe output name")
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        digest = sha256(data)
        sidecar = path.with_name(path.name + ".sha256")
        with sidecar.open("xb") as target:
            target.write(f"{digest}  {path.name}\n".encode("ascii"))
            target.flush()
            os.fsync(target.fileno())
        descriptor = {"path": name, "sha256": digest, "bytes": len(data)}
        self.value["artifacts"].append(descriptor)
        return descriptor

    def json(self, name: str, value: Any) -> dict[str, Any]:
        return self.put(name, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())

    def finish(self) -> dict[str, Any]:
        self.value["finished_epoch_ns"] = time.time_ns()
        self.value["status"] = "pass" if not self.value["errors"] else "fail"
        # The manifest's artifact list excludes itself and its sidecar.
        body = json.loads(json.dumps(self.value))
        self.json("capture.json", body)
        for directory, _, _ in os.walk(self.root, topdown=False):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return body


def _verification(report: bytes, expected_sha256: str) -> dict[str, Any]:
    if not HEX64.fullmatch(expected_sha256) or sha256(report) != expected_sha256:
        raise CaptureError("snapshot verification report hash mismatch")
    value = _json(report, "snapshot verification report")
    if value.get("schema_version") != REPORT_SCHEMA or value.get("status") != "pass" or value.get("failures") != []:
        raise CaptureError("snapshot report is not a passing retained-metadata verification")
    revision = value.get("expected_revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise CaptureError("snapshot report lacks an exact revision")
    rows = value.get("files")
    if not isinstance(rows, list) or not rows or len(rows) > 4096:
        raise CaptureError("snapshot report files are absent or unbounded")
    names = set()
    for row in rows:
        if not isinstance(row, dict):
            raise CaptureError("invalid snapshot report file")
        name = row.get("name")
        if not isinstance(name, str) or Path(name).name != name or name in names:
            raise CaptureError("unsafe or duplicate snapshot report file")
        names.add(name)
        if (row.get("verified") is not True or row.get("hub_revision") != revision
            or not isinstance(row.get("sha256"), str) or not HEX64.fullmatch(row["sha256"])
            or type(row.get("bytes")) is not int or row["bytes"] < 0):
            raise CaptureError(f"snapshot report has an unverified file: {name}")
    if not set(SMALL_INPUTS) <= names:
        raise CaptureError("snapshot report lacks required tokenizer/template/config files")
    weights = [row for row in rows if row["name"].endswith(".safetensors")]
    if (not weights or len(weights) != value.get("weight_shard_count")
        or sum(row["bytes"] for row in weights) != value.get("weight_file_bytes")):
        raise CaptureError("snapshot report weight inventory is inconsistent")
    return value


def local_snapshot_reader(root: Path) -> Callable[[str, int], bytes]:
    root = root.resolve(strict=True)

    def read(name: str, limit: int) -> bytes:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise CaptureError("snapshot file is missing or escapes its root")
        return read_bounded(path, limit)
    return read


def ssh_snapshot_reader(root: str, target: str, *, control_path: Path | None = None,
                        timeout: float = 20) -> Callable[[str, int], bytes]:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", target) or target.startswith("-"):
        raise CaptureError("invalid SSH target")
    # No remote writes, installs, scans, Hub fetches, or weight reads.
    script = """import os,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
p=(root/sys.argv[2]).resolve(strict=True)
assert p.is_relative_to(root) and p.is_file()
limit=int(sys.argv[3])
with p.open('rb') as f:
 a=os.fstat(f.fileno()); data=f.read(limit+1); b=os.fstat(f.fileno())
identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
assert len(data)<=limit and identity(a)==identity(b)==identity(p.stat())
sys.stdout.buffer.write(data)
"""
    def read(name: str, limit: int) -> bytes:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
        if control_path:
            argv += ["-S", str(control_path)]
        argv += [target, shlex.join(["python3", "-c", script, root, name, str(limit)])]
        return bounded_command(argv, timeout=timeout, limit=limit)
    return read


def archive_snapshot(report_path: Path, report_sha256: str, output: Path, *,
                     model_root: Path | None = None,
                     reader: Callable[[str, int], bytes] | None = None,
                     source_origin: dict[str, Any] | None = None) -> dict[str, Any]:
    """Archive only six cheap inputs; prior report binds the untouched weights."""
    bundle = Bundle(output, "assignment.acquisition-model-inputs.v1")
    try:
        raw = read_bounded(report_path, MAX_METADATA_BYTES)
        verification = _verification(raw, report_sha256)
        bundle.put("snapshot_verification.json", raw)
        root = model_root or Path(verification["model_root"])
        read = reader or local_snapshot_reader(root)
        rows = {row["name"]: row for row in verification["files"]}
        expected_total = sum(rows[name]["bytes"] for name in SMALL_INPUTS)
        if expected_total > MAX_TOTAL_BYTES or any(rows[name]["bytes"] > MAX_FILE_BYTES for name in SMALL_INPUTS):
            raise CaptureError("small-input archive exceeds bounded acquisition size")
        bundle.value.update({"verification_report": {"source_path": str(report_path.absolute()), "sha256": report_sha256},
                             "model_revision": verification["expected_revision"],
                             "tokenizer_revision": verification["expected_revision"],
                             "verified_snapshot_root": verification["model_root"],
                             "source": source_origin or {"transport": "local", "path": str(root.absolute())},
                             "weight_shard_count": verification["weight_shard_count"],
                             "weight_file_bytes": verification["weight_file_bytes"],
                             "weight_bytes_read_or_copied": 0, "files": [],
                             "limitation": "Small bytes reverified now; weight hashes/revisions are linked to the prior retained-Hub-metadata report, not freshly rehashed or remotely attested."})
        for name in SMALL_INPUTS:
            row = rows[name]
            data = read(name, min(MAX_FILE_BYTES, row["bytes"] + 1))
            if len(data) != row["bytes"] or sha256(data) != row["sha256"]:
                raise CaptureError(f"small snapshot file differs from verified report: {name}")
            descriptor = bundle.put(f"inputs/{name}", data)
            bundle.value["files"].append({**descriptor, "name": name, "hub_revision": row["hub_revision"],
                                          "hub_etag": row.get("hub_etag"), "metadata": row.get("metadata")})
        if sha256(read_bounded(report_path, MAX_METADATA_BYTES)) != report_sha256:
            raise CaptureError("snapshot report changed during acquisition")
    except (CaptureError, OSError, ValueError, KeyError, TypeError) as exc:
        bundle.value["errors"].append({"type": type(exc).__name__, "reason": str(exc)})
    return bundle.finish()


def capture_cpu_host(output: Path) -> dict[str, Any]:
    """Retain model-independent tool-host descriptors before acquisition."""
    from agentic_sim.telemetry.clock import clock_fields, monotonic_ns
    from agentic_sim.telemetry.hardware import local_cpu_profile

    bundle = Bundle(output, "assignment.acquisition-cpu-host.v1")
    bundle.value.update(hostname=socket.gethostname(), clock=clock_fields(),
                        started_monotonic_ns=monotonic_ns(),
                        interpretation="Host inventory, not calibrated operating frequency, memory bandwidth, or storage performance.")
    try:
        profile = local_cpu_profile()
        bundle.json("local_cpu_profile.json", profile)
        bundle.value["affinity_cpus"] = sorted(os.sched_getaffinity(0))
        bundle.value["raw_sources"] = []
        for source in ("/proc/meminfo", "/proc/self/mountinfo", "/proc/self/cgroup",
                       "/proc/sys/kernel/random/boot_id", "/proc/loadavg",
                       "/sys/fs/cgroup/cpu.max", "/sys/fs/cgroup/cpuset.cpus.effective"):
            try:
                raw = read_bounded(Path(source), MAX_METADATA_BYTES)
                artifact = bundle.put("raw/" + source.lstrip("/").replace("/", "__"), raw)
                bundle.value["raw_sources"].append({"source": source, "status": "captured", **artifact})
            except OSError as exc:
                bundle.value["raw_sources"].append({"source": source, "status": "unavailable", "reason": type(exc).__name__})
        bundle.put("hardware_source.py", read_bounded(ROOT / "src/agentic_sim/telemetry/hardware.py", MAX_METADATA_BYTES))
    except (CaptureError, OSError, ValueError) as exc:
        bundle.value["errors"].append({"type": type(exc).__name__, "reason": str(exc)})
    bundle.value["ended_monotonic_ns"] = monotonic_ns()
    return bundle.finish()


# Filter at Docker, before receiving output. Config.Env, arbitrary labels,
# command arguments, volume driver options and credentials are never requested.
INSPECT_FORMAT = '''{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"running":{{json .State.Running}},"status":{{json .State.Status}},"pid":{{json .State.Pid}},"started_at":{{json .State.StartedAt}},"restart_count":{{json .RestartCount}},"driver":{{json .GraphDriver.Name}},"upper_dir":{{json (index .GraphDriver.Data "UpperDir")}},"lower_dir":{{json (index .GraphDriver.Data "LowerDir")}},"merged_dir":{{json (index .GraphDriver.Data "MergedDir")}},"work_dir":{{json (index .GraphDriver.Data "WorkDir")}},"mounts":[{{range $i,$m := .Mounts}}{{if $i}},{{end}}{"type":{{json $m.Type}},"source":{{json $m.Source}},"destination":{{json $m.Destination}},"rw":{{json $m.RW}},"propagation":{{json $m.Propagation}}}{{end}}],"devices":[{{range $i,$d := .HostConfig.Devices}}{{if $i}},{{end}}{"host_path":{{json $d.PathOnHost}},"container_path":{{json $d.PathInContainer}},"permissions":{{json $d.CgroupPermissions}}}{{end}}]}'''


def parse_mountinfo(data: bytes) -> list[dict[str, Any]]:
    records = []
    unescape = lambda text: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text)
    for line in data.decode("utf-8", "strict").splitlines():
        fields = line.split()
        try:
            split = fields.index("-")
            if split < 6 or len(fields) < split + 4 or not re.fullmatch(r"\d+:\d+", fields[2]):
                raise ValueError("invalid fields")
            records.append({"mount_id": int(fields[0]), "parent_id": int(fields[1]),
                            "major_minor": fields[2], "root": unescape(fields[3]),
                            "mountpoint": unescape(fields[4]), "mount_options": fields[5],
                            "optional_fields": fields[6:split], "filesystem": fields[split + 1],
                            "source": unescape(fields[split + 2]), "super_options": fields[split + 3]})
        except (ValueError, IndexError) as exc:
            raise CaptureError("malformed mountinfo record") from exc
    if not records or len(records) > 16384:
        raise CaptureError("mountinfo is empty or exceeds record bound")
    return records


def device_identity(device: str, sys_root: Path) -> dict[str, Any]:
    if not re.fullmatch(r"\d+:\d+", device):
        raise CaptureError("unsafe major:minor device identity")
    path = sys_root / "dev/block" / device
    if not path.exists():
        return {"major_minor": device, "status": "not_block_device_in_host_sysfs"}
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(sys_root.resolve()):
        raise CaptureError("device sysfs link escapes sysfs")
    files = {}
    for name in ("dev", "uevent", "size", "ro", "partition", "wwid", "uuid", "device/model", "device/vendor", "device/serial", "device/wwid"):
        candidate = resolved / name
        try:
            raw = read_bounded(candidate, 65536)
            files[name] = {"status": "captured", "text": raw.decode("utf-8", "replace"), "sha256": sha256(raw)}
        except FileNotFoundError:
            files[name] = {"status": "not_present"}
        except (OSError, CaptureError) as exc:
            files[name] = {"status": "unavailable", "reason": str(exc)}
    return {"major_minor": device, "status": "captured", "sysfs_path": str(path),
            "resolved_path": str(resolved), "files": files}


def capture_mounts(container: str, output: Path, *, docker: str = "docker", timeout: float = 15,
                   run: Callable[..., bytes] = bounded_command,
                   proc_root: Path = Path("/proc"), sys_root: Path = Path("/sys")) -> dict[str, Any]:
    """Capture an existing container only; never start/stop/create it."""
    bundle = Bundle(output, "assignment.acquisition-container-mounts.v1")
    try:
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise CaptureError("supply an explicit container ID, not a name or inferred inventory")
        def command(args: list[str]) -> bytes:
            return run([docker, *args], timeout=timeout, limit=MAX_METADATA_BYTES)
        before_raw = command(["inspect", "--format", INSPECT_FORMAT, container])
        before = _json(before_raw, "filtered docker inspect")
        cid = before.get("id")
        if not isinstance(cid, str) or not HEX64.fullmatch(cid) or not cid.startswith(container):
            raise CaptureError("Docker returned a different container identity")
        bundle.put("docker_inspect.before.json", before_raw)
        bundle.value.update({"container_id": cid, "hostname": socket.gethostname(),
                             "read_only_commands": ["docker inspect (filtered)", "docker info (name only)", "docker exec CONTAINER cat /proc/self/mountinfo"],
                             "limitation": "Host-local Docker/PID namespace required; pseudo/network filesystem devices may have no block sysfs identity."})
        daemon_name = json.loads(command(["info", "--format", "{{json .Name}} "]))
        bundle.value["docker_daemon_name"] = daemon_name
        if daemon_name != socket.gethostname():
            raise CaptureError("Docker daemon is not proven to be on the captured host")
        boot = read_bounded(proc_root / "sys/kernel/random/boot_id", 128)
        bundle.put("host_boot_id.txt", boot)
        host_raw = read_bounded(proc_root / "self/mountinfo", MAX_METADATA_BYTES)
        bundle.put("host_mountinfo.txt", host_raw)
        host_mounts = parse_mountinfo(host_raw)
        bundle.json("host_mounts.json", host_mounts)
        devices = {row["major_minor"] for row in host_mounts}
        source_paths = [row.get("source") for row in before.get("mounts", [])]
        source_paths += [before.get(key) for key in ("upper_dir", "merged_dir", "work_dir")]
        source_paths += (before.get("lower_dir") or "").split(":")
        provenance = []
        for source in sorted({path for path in source_paths if path}):
            if len(provenance) >= 4096:
                raise CaptureError("Docker storage paths exceed bound")
            try:
                stat = Path(source).stat()
                device = f"{os.major(stat.st_dev)}:{os.minor(stat.st_dev)}"
                devices.add(device)
                provenance.append({"path": source, "status": "captured", "major_minor": device,
                                   "inode": stat.st_ino, "resolved_path": str(Path(source).resolve())})
            except OSError as exc:
                provenance.append({"path": source, "status": "unavailable", "reason": str(exc)})
        bundle.json("docker_storage_paths.json", provenance)
        if before.get("running") is not True or type(before.get("pid")) is not int or before["pid"] <= 0:
            bundle.value["errors"].append({"reason": "container is stopped; current container mountinfo/PID proof unavailable"})
        else:
            pid = before["pid"]
            stat_before = read_bounded(proc_root / str(pid) / "stat", 65536)
            start_ticks = stat_before.rsplit(b")", 1)[1].split()[19]
            bundle.put("container_host_pid.stat.before.txt", stat_before)
            container_raw = command(["exec", cid, "cat", "/proc/self/mountinfo"])
            bundle.put("container_mountinfo.txt", container_raw)
            container_mounts = parse_mountinfo(container_raw)
            bundle.json("container_mounts.json", container_mounts)
            devices.update(row["major_minor"] for row in container_mounts)
            stat_after = read_bounded(proc_root / str(pid) / "stat", 65536)
            bundle.put("container_host_pid.stat.after.txt", stat_after)
            if stat_after.rsplit(b")", 1)[1].split()[19] != start_ticks:
                raise CaptureError("container host PID was reused during capture")
            bundle.value["container_host_identity"] = {"pid": pid, "start_ticks": int(start_ticks), "boot_id": boot.decode().strip()}
        if len(devices) > 1024:
            raise CaptureError("device inventory exceeds bound")
        bundle.json("host_device_sysfs.json", [device_identity(device, sys_root) for device in sorted(devices)])
        after_raw = command(["inspect", "--format", INSPECT_FORMAT, cid])
        bundle.put("docker_inspect.after.json", after_raw)
        after = _json(after_raw, "filtered docker inspect")
        if after != before:
            raise CaptureError("container identity/state/mounts changed during capture")
        if read_bounded(proc_root / "sys/kernel/random/boot_id", 128) != boot:
            raise CaptureError("host boot identity changed during capture")
    except (CaptureError, OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        bundle.value["errors"].append({"type": type(exc).__name__, "reason": str(exc)})
    return bundle.finish()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    model = sub.add_parser("archive-snapshot")
    model.add_argument("--verification-report", type=Path, required=True)
    model.add_argument("--verification-report-sha256", required=True)
    model.add_argument("--model-root", type=Path)
    model.add_argument("--ssh-target")
    model.add_argument("--ssh-control-path", type=Path)
    model.add_argument("--output", type=Path, required=True)
    mounts = sub.add_parser("capture-mounts")
    mounts.add_argument("--container-id", required=True)
    mounts.add_argument("--output", type=Path, required=True)
    mounts.add_argument("--timeout-seconds", type=float, default=15)
    cpu = sub.add_parser("capture-cpu-host")
    cpu.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "archive-snapshot":
            reader = origin = None
            if args.ssh_target:
                report = _verification(read_bounded(args.verification_report, MAX_METADATA_BYTES), args.verification_report_sha256)
                root = str(args.model_root or report["model_root"])
                reader = ssh_snapshot_reader(root, args.ssh_target, control_path=args.ssh_control_path)
                origin = {"transport": "ssh", "target": args.ssh_target, "path": root}
            result = archive_snapshot(args.verification_report, args.verification_report_sha256,
                                      args.output, model_root=args.model_root, reader=reader, source_origin=origin)
        elif args.command == "capture-cpu-host":
            result = capture_cpu_host(args.output)
        else:
            result = capture_mounts(args.container_id, args.output, timeout=args.timeout_seconds)
        print(json.dumps({"status": result["status"], "output": str(args.output.absolute()), "errors": result["errors"]}))
        return 0 if result["status"] == "pass" else 2
    except (CaptureError, OSError, ValueError) as exc:
        print(json.dumps({"status": "fail", "reason": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
