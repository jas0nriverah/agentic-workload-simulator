"""Offline local-CPU domain binding; preserve raw provenance and grouped fits."""
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bind(row, inventory, source_sha):
    clock = inventory['clock']
    expected_clock = clock['clock_id']+'|boot='+clock['boot_id']
    if row.get('host_id') != clock['hostname'] or row.get('clock_id') != expected_clock:
        raise ValueError('local CPU inventory host/boot/clock does not match target')
    profile = inventory['local_cpu_profile']
    if profile.get('source') != 'local_proc_sysfs_inventory':
        raise ValueError('not a local CPU inventory')
    names = ('architecture','logical_cpu_count','model_name','kernel_release','system')
    static = {k:profile[k] for k in names}
    domain = 'static_local_cpu_inventory_v1:'+hashlib.sha256(
        json.dumps(static,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    output = dict(row)
    provenance = dict(row['feature_provenance'])
    provenance.update(original_profile_fingerprint=provenance.get('hardware_fingerprint'),
                      hardware_fingerprint=domain,
                      hardware_binding='same_host_boot_clock_local_static_cpu_inventory',
                      local_inventory_source_sha256=source_sha,
                      local_inventory_static_fields=static,
                      hardware_transfer_validated=False)
    output['feature_provenance'] = provenance
    return output


def main():
    source = BASE/'evidence/calibration_input_manifest.json'
    manifest = json.loads(source.read_text())
    cases, excluded, bindings = [], [], []
    for case in manifest['cases']:
        original_report = Path(case['events_path']).parent/'validation_report.json'
        report = json.loads(original_report.read_text())
        if report['validation']['status'] != 'valid':
            excluded.append({'case_id':case['case_id'], 'reason':'original ledger not fully valid',
                             'codes':[r['code'] for r in report['validation']['errors']]})
            continue
        events = Path(case['events_path'])
        if sha(events) != case['events_sha256']:
            raise ValueError('normalized input hash mismatch')
        root = Path(case['case_root'])
        specpath = root/'normalization_spec.json'
        spec = json.loads(specpath.read_text())
        if spec['instance_id'] != case['instance_id']:
            raise ValueError('normalization inventory instance mismatch')
        inventory = spec['hardware_identity_components']
        out = []
        for line in events.read_text().splitlines():
            row = json.loads(line)
            if row.get('record_role') != 'TARGET' or row['event_class'].startswith('native:'):
                continue
            if row['instance_id'] != case['instance_id'] or row['case_id'] != case['case_id']:
                raise ValueError('normalized event identity mismatch')
            out.append(bind(row,inventory,sha(specpath)))
        destination = HERE/'inputs'/f"{case['queue_ordinal']:05d}.jsonl"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in out))
        cases.append({'case_root':case['case_root'], 'instance_id':case['instance_id'],
                      'case_id':case['case_id'],'events_path':str(destination),
                      'events_sha256':sha(destination),'validation_report_path':str(original_report),
                      'validation_report_sha256':sha(original_report)})
        bindings.append({'case_id':case['case_id'],'normalization_spec_path':str(specpath),
                         'normalization_spec_sha256':sha(specpath),'event_count':len(out),
                         'domains':sorted({r['feature_provenance']['hardware_fingerprint'] for r in out})})
    plan = {'cases':cases}
    (HERE/'manifest.json').write_text(json.dumps(plan,indent=2)+'\n')
    adapter_path = HERE.parents[1]/'offline-followup-20260909/calibration/calibrate_repaired_d9.py'
    spec = importlib.util.spec_from_file_location('cpu_lifecycle_adapter',adapter_path)
    adapter = importlib.util.module_from_spec(spec); spec.loader.exec_module(adapter)
    result = adapter.calibrate(plan,HERE/'calibration')
    summary = {'source_manifest_sha256':sha(source),'cases':len(cases),
               'instances':len({r['instance_id'] for r in cases}), 'excluded_cases':excluded,
               'bindings':bindings,'calibration_disposition':result['disposition'],
               'input_errors':result.get('input_errors'),
               'scope':'CPU command/lifecycle wall, not individual atomic operations; no cross-hardware law',
               'hardware_inventory_contract':'Static local inventory bound by host/boot/clock; not a measured operating-frequency predictor',
               'raw_verification_scope':'existing metadata/offset/loss proofs plus one full raw reconstruction; no full binary rehash here'}
    (HERE/'binding_report.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:v for k,v in summary.items() if k not in ('bindings','excluded_cases')},indent=2))


if __name__ == '__main__': main()
