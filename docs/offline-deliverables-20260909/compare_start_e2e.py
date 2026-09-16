"""Bounded start-known E2E baselines; never reads measured component sums.

Repository is known at case dispatch. These are direct duration forecasts,
not an event simulator and not a calibrated cross-hardware model.
"""
from pathlib import Path
import collections
import csv
import hashlib
import json
import math
import statistics as st

BASE = Path(__file__).resolve().parent
SOURCE = BASE/'training_view/trajectories.jsonl'
OUT = BASE/'e2e'
NAMES = ('global_median','repository_median','repository_log_shrink')

def inner_fold(instance):
    return int.from_bytes(hashlib.sha256(('assignment.d9.inner-fold-v1:'+instance).encode()).digest()[:8],'big') % 3

def fit(rows, name):
    values = [r['observed_ms'] for r in rows]
    global_value = st.median(values)
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r['repository']].append(r)
    table = {}
    for key, rr in groups.items():
        n = len({r['instance_id'] for r in rr})
        if name == 'repository_median' and n >= 3:
            table[key] = st.median(r['observed_ms'] for r in rr)
        elif name == 'repository_log_shrink':
            # Ten equivalent instance clusters of a global geometric prior;
            # each observed cluster has equal influence on the log location.
            by_instance = collections.defaultdict(list)
            for r in rr:
                by_instance[r['instance_id']].append(math.log(r['observed_ms']))
            prior = st.mean(math.log(v) for v in values)
            table[key] = math.exp((sum(st.mean(v) for v in by_instance.values())+10*prior)/(n+10))
    return {'name':name,'global_ms':global_value,'repository_ms':table,
            'features':['repository'],'target':'outer historical accepted-attempt E2E ms',
            'hardware_scope':'historical reference environment only; transfer unvalidated'}

def predict(model, request):
    # Deliberate whitelist: labels, component times and realized event counts
    # in an evaluation row cannot enter inference.
    return model['repository_ms'].get(request.get('repository',''),model['global_ms'])

def metrics(rows):
    ape = sorted(abs(r['predicted_ms']-r['observed_ms'])/r['observed_ms']*100 for r in rows)
    return {'n':len(rows),'instances':len({r['instance_id'] for r in rows}),
            'within25':sum(v<=25 for v in ape),'within25_rate':sum(v<=25 for v in ape)/len(ape),
            'mean_ape_pct':st.mean(ape),'median_ape_pct':st.median(ape),
            'p95_ape_pct':ape[round(.95*(len(ape)-1))],'worst_ape_pct':ape[-1]}

def score(model, rows):
    return [dict(r,predicted_ms=predict(model,r)) for r in rows]

def choose(rows):
    evaluated = {}
    for name in NAMES:
        held = []
        for fold in range(3):
            train = [r for r in rows if inner_fold(r['instance_id'])!=fold]
            test = [r for r in rows if inner_fold(r['instance_id'])==fold]
            held.extend(score(fit(train,name),test))
        evaluated[name] = metrics(held)
    limit = evaluated['global_median']['worst_ape_pct']
    allowed = [name for name in NAMES if evaluated[name]['worst_ape_pct']<=limit+1e-12]
    chosen = min(allowed,key=lambda n:(-evaluated[n]['within25_rate'],evaluated[n]['worst_ape_pct'],NAMES.index(n)))
    return chosen,evaluated

def main():
    OUT.mkdir(exist_ok=False)
    # Select fields while reading the already scoped view. Component timing
    # columns are never passed to the experiment, even as training targets.
    fields = ('instance_id','run_id','repository','observed_ms','outer_fold')
    rows = [{k:d[k] for k in fields} for line in SOURCE.read_text().splitlines() if (d:=json.loads(line))]
    assert len(rows)==819 and len({r['instance_id'] for r in rows})==545
    assert all(math.isfinite(r['observed_ms']) and r['observed_ms']>0 for r in rows)
    outputs = {name:[] for name in NAMES+('nested_selected',)}
    choices=[]
    for fold in range(5):
        train=[r for r in rows if r['outer_fold']!=fold]
        test=[r for r in rows if r['outer_fold']==fold]
        assert not {r['instance_id'] for r in train}&{r['instance_id'] for r in test}
        name,diagnostic=choose(train)
        choices.append({'outer_fold':fold,'selected':name,'inner_metrics':diagnostic})
        for candidate in NAMES:
            outputs[candidate].extend(score(fit(train,candidate),test))
        outputs['nested_selected'].extend(score(fit(train,name),test))
    final_name,final_inner=choose(rows)
    model=fit(rows,final_name)
    base=predict(model,{'repository':rows[0]['repository']})
    assert base==predict(model,{'repository':rows[0]['repository'],'observed_ms':1e99,'model_wall_ms':1e99,'tool_wall_ms':1e99,'measured_residual_ms':1e99,'future_action_count':999999})
    (OUT/'candidate.json').write_text(json.dumps(model,indent=2)+'\n')
    summary={'protocol':'repository known at dispatch; direct E2E, not event composition',
             'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
             'metrics':{name:metrics(rr) for name,rr in outputs.items()},
             'outer_choices':choices,'full_training_selection':final_name,'full_training_inner_metrics':final_inner,
             'limitations':['historical completed-cache run intersection; not full required-event population',
                            'no lifecycle parameters fitted; no residual read or modeled',
                            'no cross-hardware scaling established; hardware transfer must be tested later',
                            'nested procedure score is separate from fixed-candidate selection diagnostics'],
             'validation':{'held_out_instances_disjoint':True,'poisoned_unavailable_fields_do_not_change_prediction':True}}
    (OUT/'comparison.json').write_text(json.dumps(summary,indent=2)+'\n')
    with (OUT/'predictions.csv').open('x',newline='') as f:
        names=list(rows[0])+['candidate','predicted_ms','within25']
        writer=csv.DictWriter(f,fieldnames=names);writer.writeheader()
        for name,rr in outputs.items():
            for r in rr:
                writer.writerow(dict(r,candidate=name,within25=abs(r['predicted_ms']-r['observed_ms'])<=.25*r['observed_ms']))
    print(json.dumps(summary['metrics']))
    print('packaged candidate: '+final_name)

if __name__=='__main__':
    main()
