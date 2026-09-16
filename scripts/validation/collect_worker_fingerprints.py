#!/usr/bin/env python3
"""Read-only Slurm/vLLM process and GPU inventory; never dispatch model work.

Only allowlisted command arguments and CUDA_VISIBLE_DEVICES are exported.
Do not dump command lines, environments, logs, credentials or trajectories.
This captures candidate bindings; it does not itself approve an endpoint.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

from scripts.validation.serving_fingerprint import build_receipt


NODE_PROBE = r'''
import os, json, pathlib, subprocess, socket
allowed = {'--port','--host','--model','--served-model-name','--revision',
 '--tokenizer','--tokenizer-revision','--max-model-len','--dtype','--quantization',
 '--tensor-parallel-size','--pipeline-parallel-size','--gpu-memory-utilization',
 '--kv-cache-dtype','--seed','--swap-space','--max-num-seqs','--max-num-batched-tokens',
 '--enable-prefix-caching','--disable-prefix-caching','--enable-request-id-headers',
 '--middleware','--disable-log-requests','--enforce-eager'}
processes=[]
for p in pathlib.Path('/proc').iterdir():
 if not p.name.isdecimal(): continue
 try:
  if p.stat().st_uid != os.getuid(): continue
  argv=[b.decode('utf-8','replace') for b in (p/'cmdline').read_bytes().split(b'\0') if b]
  if not any('vllm' in a.lower() for a in argv[:4]): continue
  # Retain only the reviewed serving flags. Unknown flags and their values
  # never enter the fingerprint, so credentials/ordinary command arguments
  # remain excluded while the partial-capture status stays explicit.
  allowlisted_argv=[]
  for i,a in enumerate(argv):
   key,sep,value=a.partition('=')
   if key in allowed:
    allowlisted_argv.append(a)
    if not sep and i+1<len(argv) and not argv[i+1].startswith('--'):
     allowlisted_argv.append(argv[i+1])
  options={}
  for i,a in enumerate(argv):
   key,sep,value=a.partition('=')
   if key not in allowed: continue
   if not sep: value=argv[i+1] if i+1<len(argv) and not argv[i+1].startswith('--') else True
   options[key]=value
  # Only the serving process has endpoint arguments; omit inference children.
  if not any(k in options for k in ('--port','--model','--served-model-name')): continue
  env={x.split(b'=',1)[0]:x.split(b'=',1)[1] for x in (p/'environ').read_bytes().split(b'\0') if b'=' in x}
  stat=(p/'stat').read_text(); tail=stat[stat.rfind(')')+2:].split()
  processes.append({'pid':int(p.name),'start_ticks':int(tail[19]),'options':options,
   'allowlisted_argv':allowlisted_argv,
   'cuda_visible_devices':env.get(b'CUDA_VISIBLE_DEVICES',b'').decode('ascii','replace'),
   'slurm_job_id':env.get(b'SLURM_JOB_ID',b'').decode('ascii','replace'),
   'cgroup':(p/'cgroup').read_text()})
 except (OSError,ValueError,IndexError): continue
def command(argv):
 try:
  p=subprocess.run(argv,capture_output=True,text=True,timeout=15)
  return {'argv':argv,'returncode':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
 except (OSError,subprocess.TimeoutExpired) as e:return {'argv':argv,'error':type(e).__name__}
print(json.dumps({'hostname':socket.gethostname(),
 'boot_id':pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
 'serving_processes':processes,
 'gpus':command(['nvidia-smi','--query-gpu=index,uuid,name,memory.total,driver_version,pci.bus_id,compute_cap,clocks.current.sm,clocks.current.memory,power.limit,temperature.gpu','--format=csv,noheader,nounits']),
 'compute_processes':command(['nvidia-smi','--query-compute-apps=pid,gpu_uuid,used_memory','--format=csv,noheader,nounits'])}))
'''


def build_serving_fingerprint(process, *, inventory, source_sha256):
    """Build a descriptive receipt from one retained worker probe row.

    The remote probe deliberately has no startup log or counter epoch, so the
    resulting receipt records an unbound/partial configuration rather than
    upgrading allowlisted argv into an effective runtime setting.
    """

    identity = {
        "hostname": inventory.get("hostname"),
        "boot_id": inventory.get("boot_id"),
        "server_pid": process.get("pid"),
        "server_process_start_ticks": process.get("start_ticks"),
    }
    receipt = build_receipt(
        identity=identity,
        argv=process.get("allowlisted_argv", []),
        argv_complete=False,
        processes={"api": process},
    )
    receipt["discovery_probe_source_sha256"] = source_sha256
    return receipt


def parse_inventory(raw):
    # Slurm's site prolog precedes task stdout. Keep it in the raw artifact;
    # require exactly one complete JSON inventory from our probe.
    rows = [json.loads(line) for line in raw.splitlines() if line.startswith('{')]
    if len(rows) != 1 or not {'hostname', 'boot_id', 'gpus', 'serving_processes'} <= rows[0].keys():
        raise ValueError('expected exactly one complete node inventory')
    return rows[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--login', required=True)
    parser.add_argument('--ssh-control', required=True)
    parser.add_argument('--user', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--per-allocation', action='store_true',
                        help='probe via an overlapping read-only step inside each existing GPU allocation')
    parser.add_argument('--job-id', action='append', help='limit allocation probe to these existing Slurm job IDs')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    ssh = ['ssh', '-S', args.ssh_control, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.login]
    queue_script = "import subprocess; print(subprocess.check_output(['squeue','-h','-u'," + repr(args.user) + ",'--states=RUNNING','-o','%i|%N|%b|%C|%m|%L'],text=True),end='')"
    queue = subprocess.run(ssh + ['python3', '-'], input=queue_script, text=True, capture_output=True, timeout=30, check=True)
    (args.output / 'slurm-allocations.txt').write_text(queue.stdout)
    nodes = sorted({line.split('|')[1] for line in queue.stdout.splitlines() if '|gres/gpu:h100:' in line})
    allocations = [line.split('|') for line in queue.stdout.splitlines() if '|gres/gpu:h100:' in line]
    if args.job_id:
        allocations = [row for row in allocations if row[0] in args.job_id]
        if {row[0] for row in allocations} != set(args.job_id):
            raise ValueError('requested job is not an existing running H100 allocation')
    def collect(node):
        # Node names come from Slurm but are still treated as data, not shell code.
        nested = "import subprocess,sys; p=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8'," + repr(node) + ",'python3','-'],input=" + repr(NODE_PROBE) + ",text=True,capture_output=True,timeout=35);sys.stdout.write(p.stdout);sys.stderr.write(p.stderr);sys.exit(p.returncode)"
        result = subprocess.run(ssh + ['python3', '-'], input=nested, text=True, capture_output=True, timeout=50)
        item = {'node': node, 'returncode': result.returncode}
        if result.returncode:
            item['error'] = result.stderr[:1000]
        else:
            item['inventory'] = parse_inventory(result.stdout)
        return item
    def collect_allocation(row):
        job_id, node = row[:2]
        nested = "import subprocess,sys; p=subprocess.run(['srun','--jobid'," + repr(job_id) + ", '--overlap','--nodes=1','--ntasks=1','--cpus-per-task=1','--time=00:01:00','python3','-c'," + repr(NODE_PROBE) + "],text=True,capture_output=True,timeout=45);sys.stdout.write(p.stdout);sys.stderr.write(p.stderr);sys.exit(p.returncode)"
        result = subprocess.run(ssh + ['python3', '-'], input=nested, text=True, capture_output=True, timeout=55)
        item = {'node': node, 'job_id': job_id, 'returncode': result.returncode}
        (args.output / (job_id + '.stdout')).write_text(result.stdout)
        (args.output / (job_id + '.stderr')).write_text(result.stderr)
        if result.returncode:
            item['error'] = result.stderr[:1000]
        else:
            try:
                item['inventory'] = parse_inventory(result.stdout)
                item['inventory']['serving_processes'] = [p for p in item['inventory']['serving_processes']
                                                          if p['slurm_job_id'] == job_id]
            except (ValueError, KeyError, TypeError):
                item.update(returncode=1, error='invalid inventory JSON; raw output retained')
        return item
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(collect_allocation, allocations) if args.per_allocation else pool.map(collect, nodes))
    probe_source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for item in records:
        inventory = item.get('inventory')
        if not isinstance(inventory, dict):
            continue
        for process in inventory.get('serving_processes', []):
            if isinstance(process, dict):
                process['serving_fingerprint_receipt'] = build_serving_fingerprint(
                    process, inventory=inventory, source_sha256=probe_source_sha256)
    report = {'schema_version': 'assignment.worker-fingerprint-discovery.v1',
              'captured_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'read-only discovery; endpoint mapping, source hashes and final acceptance pending',
              'probe_scope': 'existing_allocation_overlap_step' if args.per_allocation else 'node_ssh_adopted_job_device_scope',
              'probe_source_sha256': probe_source_sha256,
              'nodes': records}
    (args.output / 'fingerprints.json').write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps({'nodes': len(records), 'successful_nodes': sum(r['returncode'] == 0 for r in records),
                      'output': str(args.output)}))


if __name__ == '__main__':
    main()
