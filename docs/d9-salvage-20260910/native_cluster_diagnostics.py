"""Instance-weighted diagnostics of fixed native OOF predictions; no model search."""
import collections
import hashlib
import json
import random
from pathlib import Path

HERE=Path(__file__).resolve().parent

def main():
    source=HERE/'native/predictions.jsonl'
    rows=[json.loads(s) for s in source.read_text().splitlines()]
    folds=collections.defaultdict(set)
    cases=collections.defaultdict(set)
    for r in rows:
        folds[r['instance_id']].add(r['fold']);cases[r['instance_id']].add(r['case_id'])
    assert all(len(v)==1 for v in folds.values())
    metrics={}
    for candidate in ('relative_nnls_token','relative_nnls_token_cache'):
        groups=collections.defaultdict(list)
        for r in rows:
            y=r['observed_ms']['e2e'];p=r['predictions_ms'][candidate]['e2e']
            groups[r['instance_id']].append(p is not None and abs(p-y)<=.25*y)
        rates=[sum(v)/len(v) for v in groups.values()]
        rng=random.Random(20260910)
        draws=sorted(sum(rng.choices(rates,k=len(rates)))/len(rates) for _ in range(2000))
        metrics[candidate]={'equal_instance_within25_fraction':sum(rates)/len(rates),
                            'instances_all_requests_all_executions_pass':sum(all(v) for v in groups.values()),
                            'instances':len(rates),'conditional_95pct_interval':[draws[49],draws[1949]],
                            'per_instance':{k:{'requests':len(v),'within25':sum(v)} for k,v in groups.items()}}
    result={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'executions_per_instance':{k:len(v) for k,v in cases.items()},'metrics':metrics,
            'uncertainty_scope':'Bootstrap of fixed development OOF predictions; excludes model selection, fit uncertainty and hardware transfer.'}
    (HERE/'native_cluster_diagnostics.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:{a:b for a,b in v.items() if a!='per_instance'} for k,v in metrics.items()},indent=2))

if __name__=='__main__':main()
