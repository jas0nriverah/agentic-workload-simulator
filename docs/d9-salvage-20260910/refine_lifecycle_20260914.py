"""Fixed grouped lifecycle comparisons; predictors consume start-known categories."""
import importlib.util
import json
from pathlib import Path
from collections import defaultdict
from statistics import median

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('lifecycle_scan',HERE/'scan_remaining_20260914.py')
scan=importlib.util.module_from_spec(spec);spec.loader.exec_module(scan)
m=scan.m
from agentic_sim.assignment.semantic_cpu_model import _gate_center


def key(row, candidate):
    features=dict(row['extra_features'])
    if candidate.startswith('context_'):features.update(row['context_features'])
    return json.dumps(features,sort_keys=True)


def fit(rows,candidate):
    groups=defaultdict(list)
    for r in rows:groups[key(r,candidate)].append(r)
    center=_gate_center if candidate=='descriptor_gate' else median
    result = {'candidate':candidate,'fallback':median(r['observed_ms'] for r in rows),
            'table':{k:float(center([r['observed_ms'] for r in group])) for k,group in groups.items()
                     if len(group)>=25 and len({r['instance_id'] for r in group})>=3}}
    if candidate=='context_hierarchical':result['parent_model']=fit(rows,'descriptor_median')
    return result


def predict(model,row):
    k=key(row,model['candidate'])
    if k in model['table']:return model['table'][k]
    return predict(model['parent_model'],row) if 'parent_model' in model else model['fallback']


def main():
    groups=scan.load_groups()
    reports,models,all_predictions={},{},[]
    for (klass,domain,boundary),rows in groups.items():
        out=[]
        for fold in range(5):
            train=[r for r in rows if m.cal._fold(r['instance_id'])!=fold]
            test=[r for r in rows if m.cal._fold(r['instance_id'])==fold]
            fitted={c:fit(train,c) for c in ('descriptor_median','descriptor_gate','context_median','context_hierarchical')}
            for r in test:
                inputs={k:r[k] for k in ('extra_features','context_features')}
                out.append({k:r[k] for k in ('case_id','instance_id','event_id','observed_ms')} |
                           {'fold':fold,'event_class':klass,'predictions_ms':{c:predict(model,inputs) for c,model in fitted.items()}})
        metrics={c:m.metrics(out,c) for c in fitted}
        intervals={c:m.paired_gain([{**r,'predictions_ms':{'semantic':r['predictions_ms'][c],
                            'coarse':r['predictions_ms']['descriptor_median']}} for r in out]) for c in ('descriptor_gate','context_median','context_hierarchical')}
        base=metrics['descriptor_median']
        eligible=[c for c in intervals if intervals[c]['lower_95']>0 and
                  metrics[c]['equal_instance_within25']>=base['equal_instance_within25'] and
                  metrics[c]['worst_error_pct']<=base['worst_error_pct']]
        winner=max(eligible,key=lambda c:metrics[c]['within25']) if eligible else 'descriptor_median'
        reports[klass]={'metrics':metrics,'gain_intervals_vs_descriptor':intervals,'selected':winner}
        models[klass]={'domain':domain,'boundary':boundary,'model':fit(rows,winner)}
        all_predictions+=out
    output=HERE/'lifecycle_refinement';output.mkdir(exist_ok=True)
    report={'results':reports,'code_sha256':m.sha(__file__),'adapter_sha256':m.sha(HERE/'scan_remaining_20260914.py'),
            'manifest_sha256':m.sha(HERE/'cpu_lifecycle/manifest.json'),
            'scope':'fixed grouped development; no final labels, no inference; not literal D9 acceptance'}
    for name,value in [('report',report),('fit_artifact',{'schema':'lifecycle-refinement.v1','models':models,'provenance':report})]:
        (output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    (output/'predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in all_predictions))
    print(json.dumps(reports,indent=2))


if __name__=='__main__':main()
