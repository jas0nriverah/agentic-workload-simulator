#!/usr/bin/env python3
"""Recover the failed, pre-case worker22 startup from its private checkpoint.

This finite recovery path never sends a signal. It refuses an occupied GPU,
port, original PID or failed replacement PID. Other workers are not inspected
or mutated. The generic restart helper handles subsequent healthy workers.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from controlled_vllm_restart import digest, durable_json, replacement_binding, process

source = Path(__file__).absolute().parent
root = Path('/storage/ice1/9/6/jriverah3/eic-work/runtime/prefreeze-worker22-restart-20260909-v1')
assert len(sys.argv) == 2 and digest(source / 'source_manifest.json') == sys.argv[1]
manifest = json.loads((source / 'source_manifest.json').read_text())
for name, expected in manifest['files'].items():
    assert Path(name).name == name and digest(source / name) == expected
assert digest(root / 'plan.json') == 'fc40f0a6d3720ea73036b6de6b9f41cebf55c7d0a62166d470a3500a23b3e3e8'
plan = json.loads((root / 'plan.json').read_text())
assert os.environ.get('SLURM_JOB_ID') == plan['job_id'] == '5741123'
assert socket.gethostname() == plan['hostname'] == 'atl1-1-03-011-28-0.pace.gatech.edu'
assert Path('/proc/sys/kernel/random/boot_id').read_text().strip() == plan['boot_id']
assert digest(root / 'original.private.json') == plan['original_sha256']
original = json.loads((root / 'original.private.json').read_text())
previous = json.loads((root / 'replacement-exec.json').read_text())
assert not Path('/proc', str(plan['pid'])).exists(), 'original PID still exists'
assert not Path('/proc', str(previous['pid'])).exists(), 'failed replacement PID still exists'
selector, affinity = replacement_binding(original)
assert selector == '0' and affinity == list(range(24, 32))
gpu = subprocess.check_output(['nvidia-smi', '--query-gpu=uuid', '--format=csv,noheader'], text=True, timeout=15).splitlines()
assert gpu == [plan['gpu_uuid']]
apps = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader'], text=True, timeout=15)
assert not apps.strip(), 'GPU still has a compute process'
with socket.socket() as probe:
    probe.settimeout(2)
    assert probe.connect_ex(('127.0.0.1', plan['port'])) != 0, 'server port occupied'
env = {k: v for k, v in original['environment'].items() if not k.startswith('SLURM_')}
env.update({k: v for k, v in os.environ.items() if k.startswith('SLURM_')})
env.update(json.loads((source / 'observer_env.json').read_text()))
env['CUDA_VISIBLE_DEVICES'] = selector
env['PYTHONPATH'] = str(source) + (':' + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
argv = list(original['argv'])
argv[argv.index('--max-model-len') + 1] = '65536'
argv += ['--middleware', 'serving_observer.ServingObserver', '--enable-prompt-tokens-details']
durable_json(root / 'replacement-exec-v3.json', {
    'schema_version': 'assignment.worker22-preflight-recovery.v1',
    'pid': os.getpid(), 'start_ticks': process(os.getpid())['start_ticks'],
    'source_manifest_sha256': sys.argv[1], 'original_checkpoint_sha256': plan['original_sha256'],
    'cpu_affinity': affinity, 'cuda_visible_devices': selector, 'gpu_uuid': gpu[0],
    'prior_failure': 'vLLM 0.10.0 rejects GPU UUID CUDA selectors; restore numeric selector and original eight-CPU binding',
    'argv_sha256': hashlib.sha256(json.dumps(argv).encode()).hexdigest(), 'unix_seconds': time.time(),
})
os.chdir(original['cwd'])
os.execve(argv[0], argv, env)
