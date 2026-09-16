"""Join retained grouped predictions; no target is used as a new model input."""
import collections
import csv
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent

def main():
    paths={'cpu':HERE.parent/'offline-deliverables-20260909/cpu/class_hybrid_predictions.csv',
           'gpu':HERE/'conditional/predictions.jsonl','e2e':HERE/'e2e/predictions.jsonl'}
    rows=[json.loads(s) for s in paths['e2e'].read_text().splitlines()]
    identities={r['run_id']:(r['instance_id'],r['outer_fold']) for r in rows}
    cpu,gpu=collections.defaultdict(list),collections.defaultdict(list)
    with paths['cpu'].open() as f:
        for r in csv.DictReader(f):
            assert identities[r['run_id']]==(r['instance_id'],int(r['outer_fold']))
            cpu[r['run_id']].append(abs(float(r['prediction'])-float(r['observed_ms']))<=.25*float(r['observed_ms']))
    for line in paths['gpu'].open():
        r=json.loads(line)
        if r['candidate']!='nonnegative_log_additive':continue
        assert identities[r['run_id']]==(r['instance_id'],int(r['outer_fold']))
        gpu[r['run_id']].append(abs(r['predicted_ms']-r['observed_ms'])<=.25*r['observed_ms'])
    assert set(cpu)==set(gpu)==set(identities)
    output=[]
    for r in rows:
        run=r['run_id'];epass=abs(r['conditional_relative_nnls']-r['observed_ms'])<=.25*r['observed_ms']
        output.append({'run_id':run,'instance_id':r['instance_id'],'fold':r['outer_fold'],
                       'cpu_events':len(cpu[run]),'gpu_requests':len(gpu[run]),
                       'all_cpu_pass':all(cpu[run]),'all_gpu_pass':all(gpu[run]),'e2e_pass':epass,
                       'all_retained_targets_pass':all(cpu[run]) and all(gpu[run]) and epass})
    report={'runs':len(output),'instances':len({r['instance_id'] for r in output}),
            'counts':{k:sum(r[k] for r in output) for k in ('all_cpu_pass','all_gpu_pass','e2e_pass','all_retained_targets_pass')},
            'source_hashes':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in paths.items()},
            'scope':'Historical tool-wall/request-proxy events and conditional direct E2E; not complete PDF atomic-operation population.',
            'selection':'Development comparisons; source models retain their own selection limitations.',
            'literal_d9_pass':False}
    (HERE/'historical_joint_report.json').write_text(json.dumps(report,indent=2)+'\n')
    (HERE/'historical_joint_predictions.jsonl').write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in output))
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
