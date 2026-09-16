"""Bounded grouped conditional E2E experiment; stdlib only, no inference."""
import collections
import hashlib
import json
import math
import random
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / 'offline-deliverables-20260909/training_view'
FEATURES = ['intercept', 'tool_count/100', 'request_count/100',
            'declared_input_tokens/1000000', 'declared_output_tokens/10000']


def design(tools, models):
    # Explicit allowlist: no event timing, measured residual or outcome.
    return [1., len(tools)/100., len(models)/100.,
            sum(r['input_tokens'] for r in models)/1e6,
            sum(r['output_tokens'] for r in models)/1e4]


def fit(x, y, relative=False):
    """Nonnegative least squares by cyclic coordinate descent on Gram matrix."""
    n = len(x[0])
    weights = [1/v**2 if relative else 1. for v in y]
    gram = [[sum(w*r[j]*r[k] for w,r in zip(weights,x))
             for k in range(n)] for j in range(n)]
    rhs = [sum(w*r[j]*v for w,r,v in zip(weights,x,y)) for j in range(n)]
    b = [0.]*n
    for iteration in range(20000):
        old = b[:]
        for j in range(n):
            if gram[j][j]:
                b[j] = max(0., (rhs[j]-sum(gram[j][k]*b[k]
                                          for k in range(n) if k != j))/gram[j][j])
        if max(abs(a-c) for a,c in zip(old,b)) < 1e-9 * max(1.,max(b)):
            break
    return b


def predict(b, x):
    return sum(a*c for a,c in zip(b,x))


def metrics(rows, key):
    errors = sorted(abs(r[key]-r['observed_ms'])/r['observed_ms']*100 for r in rows)
    return {'runs':len(rows), 'instances':len({r['instance_id'] for r in rows}),
            'within25_count':sum(e<=25 for e in errors),
            'within25_fraction':sum(e<=25 for e in errors)/len(errors),
            'p95_error_pct':errors[math.ceil(.95*len(errors))-1],
            'worst_error_pct':errors[-1], 'median_error_pct':median(errors)}


def paired_uncertainty(rows):
    clusters = collections.defaultdict(list)
    for r in rows:
        y = r['observed_ms']
        clusters[r['instance_id']].append(
            int(abs(r['conditional_relative_nnls']-y)<=.25*y)
            - int(abs(r['global_median']-y)<=.25*y))
    values = [(sum(v),len(v)) for v in clusters.values()]
    rng = random.Random(20260910)
    gains = []
    for _ in range(2000):
        sampled = rng.choices(values,k=len(values))
        gains.append(sum(v for v,n in sampled)/sum(n for v,n in sampled))
    gains.sort()
    return {'coverage_gain_fraction':sum(v for v,n in values)/sum(n for v,n in values),
            'conditional_95pct_interval':[gains[49],gains[1949]],
            'replicates':2000,'unit':'instance cluster',
            'caveat':'Fixed development OOF predictions; does not account for model selection or establish blind-test performance.'}


def main():
    manifest = json.loads((SOURCE/'manifest.json').read_text())
    data = {}
    hashes = {}
    for name in ('tools','models','trajectories'):
        p = SOURCE/(name+'.jsonl')
        hashes[name] = hashlib.sha256(p.read_bytes()).hexdigest()
        assert hashes[name] == manifest['output_hashes'][p.name]
        data[name] = [json.loads(s) for s in p.read_text().splitlines()]
    byrun = {kind:collections.defaultdict(list) for kind in ('tools','models')}
    identities = {r['run_id']:(r['instance_id'],r['outer_fold']) for r in data['trajectories']}
    instance_folds = collections.defaultdict(set)
    for inst,fold in identities.values(): instance_folds[inst].add(fold)
    assert all(len(fs)==1 for fs in instance_folds.values())
    for kind in byrun:
        for r in data[kind]:
            assert identities[r['run_id']] == (r['instance_id'],r['outer_fold'])
            byrun[kind][r['run_id']].append(r)
    rows = []
    for r in data['trajectories']:
        assert r['observed_ms'] > 0
        rows.append({k:r[k] for k in ('run_id','instance_id','outer_fold','observed_ms')} |
                    {'design':design(byrun['tools'][r['run_id']],byrun['models'][r['run_id']])})
    for fold in range(5):
        train = [r for r in rows if r['outer_fold']!=fold]
        test = [r for r in rows if r['outer_fold']==fold]
        assert not {r['instance_id'] for r in train} & {r['instance_id'] for r in test}
        x,y = [r['design'] for r in train],[r['observed_ms'] for r in train]
        models = {'conditional_nnls':fit(x,y), 'conditional_relative_nnls':fit(x,y,True)}
        for r in test:
            r['global_median'] = median(y)
            for name,b in models.items(): r[name] = predict(b,r['design'])
    names = ('global_median','conditional_nnls','conditional_relative_nnls')
    report = {'contract':'trace-conditioned full workload; direct E2E, NOT event composition or prospective forecast',
              'features':FEATURES, 'source_hashes':hashes,
              'split':'existing five instance-grouped outer folds; development comparison, not untouched holdout',
              'metrics':{n:metrics(rows,n) for n in names},
              'paired_uncertainty':paired_uncertainty(rows),
              'literal_d9_pass':False, 'hardware_transfer_validated':False,
              'limitations':['Historical request/tool proxy boundaries; repaired native phases are separate.',
                             'Full event counts and realized token counts are supplied workload inputs.',
                             'Direct E2E accuracy cannot establish individual-event accuracy.']}
    for n in names:
        report['metrics'][n]['folds'] = {str(f):metrics([r for r in rows if r['outer_fold']==f],n)
                                        for f in range(5)}
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    repositories = collections.defaultdict(list)
    for row in rows:
        repositories[row['instance_id'].split('__')[0]].append(row)
    (HERE/'repository_diagnostics.json').write_text(json.dumps(
        {k:metrics(v,'conditional_relative_nnls') for k,v in sorted(repositories.items())},indent=2)+'\n')
    (HERE/'predictions.jsonl').write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in rows))
    x,y = [r['design'] for r in rows],[r['observed_ms'] for r in rows]
    artifact = {'contract':report['contract'],'features':FEATURES,'source_hashes':hashes,
                'selected_model':'conditional_relative_nnls',
                'selection_scope':'development OOF comparison; no untouched holdout claim',
                'coefficients':{n:fit(x,y,n.endswith('relative_nnls')) for n in names[1:]},
                'global_median':median(y),'hardware_transfer_validated':False}
    (HERE/'model.json').write_text(json.dumps(artifact,indent=2)+'\n')
    print(json.dumps(report['metrics'],indent=2))


if __name__ == '__main__': main()
