"""Join nested held-out predictions, never observed components as features."""
from pathlib import Path
import argparse
import collections
import csv
import hashlib
import json
import statistics as st

BASE=Path(__file__).resolve().parent
INPUTS={
    'cpu':BASE/'cpu/nested_predictions.csv',
    'gpu':BASE/'gpu_lifecycle/gpu_proxy_oof_predictions.jsonl',
    'e2e':BASE/'e2e/predictions.csv',
    'trajectories':BASE/'training_view/trajectories.jsonl',
}

def metric(pairs):
    values=sorted(abs(p-y)/y*100 for p,y in pairs)
    return {'n':len(values),'within25':sum(v<=25 for v in values),
            'within25_rate':sum(v<=25 for v in values)/len(values),
            'mean_ape_pct':st.mean(values),'p95_ape_pct':values[round(.95*(len(values)-1))],
            'worst_ape_pct':values[-1]}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu-predictions',type=Path,default=INPUTS['cpu'])
    parser.add_argument('--prefix',default='joint')
    args=parser.parse_args()
    assert args.prefix.isidentifier()
    INPUTS['cpu']=args.cpu_predictions
    cpu=list(csv.DictReader(INPUTS['cpu'].open()))
    gpu=[json.loads(x) for x in INPUTS['gpu'].open()]
    e2e=[r for r in csv.DictReader(INPUTS['e2e'].open()) if r['candidate']=='nested_selected']
    targets={r['run_id']:r for x in INPUTS['trajectories'].open() if (r:=json.loads(x))}
    by_cpu=collections.defaultdict(list);by_gpu=collections.defaultdict(list)
    seen=set()
    for r in cpu:
        key=(r['run_id'],r['event_id']);assert key not in seen;seen.add(key)
        target=targets[r['run_id']]
        assert r['instance_id']==target['instance_id'] and int(r['fold'])==target['outer_fold']
        by_cpu[r['run_id']].append((float(r['prediction']),float(r['observed_ms'])))
    seen=set()
    for r in gpu:
        key=(r['run_id'],r['request_id']);assert key not in seen;seen.add(key)
        target=targets[r['run_id']]
        assert r['instance_id']==target['instance_id'] and r['outer_fold']==target['outer_fold']
        by_gpu[r['run_id']].append((r['predicted_ms'],r['observed_ms']))
    by_e2e={r['run_id']:r for r in e2e}
    assert len(by_e2e)==len(e2e)==819
    assert set(targets)==set(by_e2e)==set(by_cpu)==set(by_gpu)
    output=[]
    for run_id,t in targets.items():
        e=by_e2e[run_id]
        assert e['instance_id']==t['instance_id'] and int(e['outer_fold'])==t['outer_fold']
        assert float(e['observed_ms'])==t['observed_ms']
        cp=sum(p for p,y in by_cpu[run_id]);cy=sum(y for p,y in by_cpu[run_id])
        gp=sum(p for p,y in by_gpu[run_id]);gy=sum(y for p,y in by_gpu[run_id])
        ep=float(e['predicted_ms']);y=t['observed_ms']
        cpu_pass=all(abs(p-z)<=.25*z for p,z in by_cpu[run_id])
        gpu_pass=all(abs(p-z)<=.25*z for p,z in by_gpu[run_id])
        e2e_pass=abs(ep-y)<=.25*y
        output.append({'run_id':run_id,'instance_id':t['instance_id'],'outer_fold':t['outer_fold'],
            'cpu_event_count':len(by_cpu[run_id]),'gpu_request_count':len(by_gpu[run_id]),
            'predicted_cpu_sum_ms':cp,'observed_cpu_sum_ms':cy,
            'predicted_gpu_proxy_sum_ms':gp,'observed_gpu_proxy_sum_ms':gy,
            'predicted_partial_replay_sum_ms':cp+gp,'observed_e2e_ms':y,
            'predicted_start_known_e2e_ms':ep,
            'all_recorded_cpu_pass':cpu_pass,'all_recorded_gpu_pass':gpu_pass,
            'start_known_e2e_pass':e2e_pass,
            'available_recorded_target_conjunction':cpu_pass and gpu_pass and e2e_pass,
            'literal_required_event_acceptance':'unsupported: historical tool/proxy targets; repaired atomic/native/lifecycle targets not calibrated'})
    summary={
        'runs':len(output),'instances':len({r['instance_id'] for r in output}),
        'cpu_event_count':len(cpu),'gpu_request_count':len(gpu),
        'cpu_sum':metric([(r['predicted_cpu_sum_ms'],r['observed_cpu_sum_ms']) for r in output]),
        'gpu_proxy_sum':metric([(r['predicted_gpu_proxy_sum_ms'],r['observed_gpu_proxy_sum_ms']) for r in output]),
        'partial_event_sum_against_e2e':metric([(r['predicted_partial_replay_sum_ms'],r['observed_e2e_ms']) for r in output]),
        'start_known_e2e':metric([(r['predicted_start_known_e2e_ms'],r['observed_e2e_ms']) for r in output]),
        'all_recorded_cpu_pass_runs':sum(r['all_recorded_cpu_pass'] for r in output),
        'all_recorded_gpu_pass_runs':sum(r['all_recorded_gpu_pass'] for r in output),
        'available_recorded_target_conjunction_runs':sum(r['available_recorded_target_conjunction'] for r in output),
        'literal_d9_status':'UNPROVEN; no complete calibrated repaired-event/lifecycle/cross-hardware evaluation',
        'limits':['partial replay sum uses realized event inventory; not a prospective start forecast',
                  'no lifecycle or measured residual term fitted or inserted',
                  'historical retained completed-event population is not the PDF all-required-event population',
                  'native phase/atomic-operation/auxiliary-runtime models remain unsupported by independent repaired calibration instances'],
        'join_validation':'unique event/request IDs; matching run, instance, outer fold and E2E target; all819runsets equal',
        'source_hashes':{k:hashlib.sha256(p.read_bytes()).hexdigest() for k,p in INPUTS.items()},
    }
    with (BASE/(args.prefix+'_predictions.csv')).open('x',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(output[0]));w.writeheader();w.writerows(output)
    (BASE/(args.prefix+'_metrics.json')).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))

if __name__=='__main__':main()
