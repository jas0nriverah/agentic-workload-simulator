import base64
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agentic_sim.telemetry.container_resources import capture_container_mounts, capture_container_resources


def fixture(tmp_path):
    proc, cg = tmp_path / 'proc', tmp_path / 'cgroup'
    process = proc / '123'
    process.mkdir(parents=True)
    (proc / 'sys/kernel/random').mkdir(parents=True)
    (proc / 'sys/kernel/random/boot_id').write_text('boot\n')
    (process / 'stat').write_text('123 (bash) ' + ' '.join(['S'] + ['0'] * 18 + ['456'] + ['0'] * 20))
    (process / 'cgroup').write_text('0::/docker/owned\n')
    container = cg / 'docker/owned'
    container.mkdir(parents=True)
    (container / 'cpu.stat').write_text('usage_usec 200\nuser_usec 150\nsystem_usec 50\nnr_throttled 2\n')
    (container / 'io.stat').write_text('8:0 rbytes=4096 wbytes=0 rios=1 wios=0\n')
    identity = SimpleNamespace(pid=123, start_ticks=456, boot_id='boot', container_pid=8)
    return proc, cg, identity


def test_actual_cgroup_text_retained_with_scope_and_clock(tmp_path):
    proc, cg, identity = fixture(tmp_path)
    with patch('os.sched_getaffinity', return_value={1, 2}):
        result = capture_container_resources(identity, proc_root=proc, cgroup_root=cg)
    assert result['status'] == 'measured'
    assert result['files']['cpu.stat']['raw'].startswith('usage_usec 200')
    assert 'rbytes=4096' in result['files']['io.stat']['raw']
    assert result['files']['memory.max']['unavailable'] == 'FileNotFoundError'
    assert result['target_affinity_cpus'] == [1, 2]
    assert result['started_monotonic_ns'] <= result['ended_monotonic_ns']


def test_reused_pid_and_root_cgroup_rejected(tmp_path):
    proc, cg, identity = fixture(tmp_path)
    identity.start_ticks += 1
    assert capture_container_resources(identity, proc_root=proc, cgroup_root=cg)['status'] == 'unavailable'
    identity.start_ticks -= 1
    (proc / '123/cgroup').write_text('0::/\n')
    result = capture_container_resources(identity, proc_root=proc, cgroup_root=cg)
    assert result['status'] == 'unavailable'
    assert 'container-specific' in result['unavailable_reason']


def test_membership_change_during_read_rejected(tmp_path):
    proc, cg, identity = fixture(tmp_path)
    def mutate(pid):
        (proc / '123/cgroup').write_text('0::/docker/other\n')
        return {0}
    with patch('os.sched_getaffinity', side_effect=mutate):
        result = capture_container_resources(identity, proc_root=proc, cgroup_root=cg)
    assert result['status'] == 'unavailable'


def test_mount_source_bytes_reconstruct_and_wrong_pid_is_rejected(tmp_path):
    proc, _, identity = fixture(tmp_path)
    (proc / '123/ns').mkdir()
    (proc / '123/ns/mnt').symlink_to('mnt:[42]')
    (proc / 'self').mkdir()
    raw = b'1 0 8:0 / / rw - ext4 /dev/root rw\n'
    (proc / '123/mountinfo').write_bytes(raw)
    (proc / 'self/mountinfo').write_bytes(raw)
    original_open = Path.open

    class BoundedKernelRead(io.BytesIO):
        def read(self, size=-1):
            if size < 0 or size > 65536:
                raise OSError(12, 'Cannot allocate memory')
            return super().read(size)

    def proc_open(path, mode='r', *args, **kwargs):
        if mode == 'rb':
            with original_open(path, 'rb') as source:
                return BoundedKernelRead(source.read())
        return original_open(path, mode, *args, **kwargs)

    with patch.object(Path, 'open', proc_open):
        result = capture_container_mounts(identity, proc_root=proc)
    assert result['status'] == 'measured'
    assert result['mount_namespace'] == 'mnt:[42]'
    for row in result['files'].values():
        reconstructed = base64.b64decode(row['raw_base64'])
        assert reconstructed == raw
        assert hashlib.sha256(reconstructed).hexdigest() == row['sha256']
    identity.start_ticks += 1
    result = capture_container_mounts(identity, proc_root=proc)
    assert result['status'] == 'unavailable'
    assert 'identity changed' in result['unavailable_reason']
