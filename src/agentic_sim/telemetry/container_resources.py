"""Bounded cgroup-v2 context for the actual container, never recorder rusage.

Counters describe a whole container interval, not an individual syscall or
prospective work feature. Raw kernel text preserves units and unavailable
fields. Identity and clock brackets prevent reuse of an unrelated cgroup.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any

from .clock import clock_fields, monotonic_ns

CGROUP_FILES = ("cpu.stat", "cpu.max", "cpu.weight", "cpuset.cpus.effective",
                "cpuset.mems.effective", "io.stat", "memory.stat", "memory.current",
                "memory.max", "cpu.pressure", "io.pressure", "memory.pressure")


def capture_container_mounts(identity: Any, *, proc_root: Path = Path('/proc')) -> dict[str, Any]:
    """Read once per target lifetime; no extra action inside the container.

    Mount options and filesystem/backing identities constrain later storage
    explanations. They do not measure cache hits or physical bandwidth.
    """
    record: dict[str, Any] = {
        "schema_version": "assignment.container-mounts.v1",
        "clock": dict(clock_fields()), "started_monotonic_ns": monotonic_ns(),
        "status": "unavailable", "files": {},
        "target_pid": identity.pid, "target_start_ticks": identity.start_ticks,
        "boot_id": identity.boot_id,
        "interpretation": "Filesystem/mount provenance only; logical syscall bytes are not physical device traffic.",
    }

    def read(path: Path) -> bytes:
        # Some proc-sysctl handlers allocate the requested read size in the
        # kernel. A single 8 MiB boot_id read can fail with ENOMEM even though
        # its value is only 37 bytes. Bound the total and each read separately.
        limit = 8 * 1024 * 1024
        chunks = []
        size = 0
        with path.open('rb') as stream:
            while True:
                chunk = stream.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise ValueError('mount snapshot exceeds byte bound')
        return b''.join(chunks)

    def binding():
        process = proc_root / str(identity.pid)
        stat = read(process / 'stat')
        start = int(stat[stat.rfind(b')') + 2:].split()[19])
        boot = read(proc_root / 'sys/kernel/random/boot_id').decode('ascii').strip()
        if start != identity.start_ticks or boot != identity.boot_id:
            raise ValueError('target process or boot identity changed')
        return read(process / 'cgroup'), os.readlink(process / 'ns/mnt')

    try:
        if identity.container_pid is None:
            raise ValueError('target is not bound to a container PID')
        before = binding()
        record['mount_namespace'] = before[1]
        for name, path in (("target_mountinfo", proc_root / str(identity.pid) / 'mountinfo'),
                           ("host_mountinfo", proc_root / 'self/mountinfo')):
            raw = read(path)
            record['files'][name] = {"source": str(path), "bytes": len(raw),
                                     "raw_base64": base64.b64encode(raw).decode('ascii'),
                                     "sha256": hashlib.sha256(raw).hexdigest()}
        if binding() != before:
            raise ValueError('target cgroup or mount namespace changed during snapshot')
        record['status'] = 'measured'
    except (OSError, ValueError, UnicodeError, IndexError) as exc:
        record['unavailable_reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        record['ended_monotonic_ns'] = monotonic_ns()
    return record


def capture_container_resources(identity: Any, *, proc_root: Path = Path('/proc'),
                                cgroup_root: Path = Path('/sys/fs/cgroup')) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": "assignment.container-resources.v1",
        "scope": "whole_target_container_interval_context_not_per_syscall_or_predictive_feature",
        "clock": dict(clock_fields()), "started_monotonic_ns": monotonic_ns(),
        "status": "unavailable", "files": {}, "host_context": {},
        "target_pid": identity.pid, "target_start_ticks": identity.start_ticks,
        "boot_id": identity.boot_id,
    }
    def bounded(path: Path) -> str:
        with path.open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError(f"resource snapshot exceeds bound: {path.name}")
        return data.decode('ascii', 'strict')

    def binding():
        process = proc_root / str(identity.pid)
        stat = bounded(process / 'stat')
        start = int(stat[stat.rfind(')') + 2:].split()[19])
        membership = bounded(process / 'cgroup')
        boot = bounded(proc_root / 'sys/kernel/random/boot_id').strip()
        if start != identity.start_ticks or boot != identity.boot_id:
            raise ValueError('target process or boot identity changed')
        unified = [row[3:] for row in membership.splitlines() if row.startswith('0::')]
        if len(unified) != 1 or not unified[0].startswith('/'):
            raise ValueError('unified cgroup membership unavailable')
        relative = Path(unified[0].lstrip('/'))
        if not relative.parts or '..' in relative.parts:
            raise ValueError('root or invalid cgroup is not container-specific')
        path = cgroup_root / relative
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(cgroup_root.resolve(strict=True)) or path.is_symlink():
            raise ValueError('cgroup escaped its mounted hierarchy')
        metadata = path.stat()
        return membership, resolved, metadata.st_dev, metadata.st_ino

    try:
        if identity.container_pid is None:
            raise ValueError('target is not bound to a container PID')
        before = binding()
        membership, directory, device, inode = before
        record.update(cgroup_membership=membership, cgroup_path=str(directory),
                      cgroup_device=device, cgroup_inode=inode)
        for name in CGROUP_FILES:
            try:
                raw = bounded(directory / name)
                record['files'][name] = {'raw': raw, 'sha256': hashlib.sha256(raw.encode('ascii')).hexdigest()}
            except OSError as exc:
                record['files'][name] = {'unavailable': type(exc).__name__}
        for name in ('loadavg', 'pressure/cpu', 'pressure/io', 'pressure/memory'):
            try:
                record['host_context'][name] = {'raw': bounded(proc_root / name)}
            except OSError as exc:
                record['host_context'][name] = {'unavailable': type(exc).__name__}
        try:
            record['target_affinity_cpus'] = sorted(os.sched_getaffinity(identity.pid))
        except OSError as exc:
            record['target_affinity_unavailable'] = type(exc).__name__
        if binding() != before:
            raise ValueError('container membership or cgroup identity changed during snapshot')
        if 'raw' not in record['files']['cpu.stat']:
            raise ValueError('actual cgroup CPU counters unavailable')
        record['status'] = 'measured'
    except (OSError, ValueError, UnicodeError, IndexError) as exc:
        record['unavailable_reason'] = f'{type(exc).__name__}: {exc}'
    finally:
        record['ended_monotonic_ns'] = monotonic_ns()
    return record
