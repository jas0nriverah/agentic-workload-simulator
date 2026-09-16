"""Two bounded native-phase hypotheses, using the original instance folds."""
import importlib.util
import json
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
spec = importlib.util.spec_from_file_location('refinement_native', BASE/'native/run_native_comparison.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
CANDIDATES = ('pooled', 'cache_regime', 'prefill_attention')


def regime(row):
    return 'cache_hit' if row['cached_tokens'] > 0 else 'cache_miss'


def design(row, phase, candidate):
    x = native.design(row, 'relative_nnls_token_cache', phase)
    if candidate == 'prefill_attention' and phase == 'prefill':
        fresh = row['prompt_tokens'] - row['cached_tokens']
        x += [fresh * row['prompt_tokens'] / 1e6]
    return x


def fit(rows, phase, candidate):
    def coefficients(group):
        positive = [r for r in group if r['observed_ms'] > 0]
        if not positive:
            raise ValueError('no positive training targets')
        return list(native.COMPARE.fit([design(r, phase, candidate) for r in positive],
                                      [r['observed_ms'] for r in positive], relative=True))
    models = {'pooled': coefficients(rows)}
    if candidate == 'cache_regime':
        for key in ('cache_hit', 'cache_miss'):
            group = [r for r in rows if regime(r) == key]
            if len(group) >= 25 and len({r['instance_id'] for r in group}) >= 3:
                models[key] = coefficients(group)
    return {'phase': phase, 'candidate': candidate, 'coefficients': models}


def predict(model, inputs):
    key = regime(inputs) if model['candidate'] == 'cache_regime' else 'pooled'
    coefficients = model['coefficients'].get(key, model['coefficients']['pooled'])
    return max(0., sum(a*b for a,b in zip(coefficients, design(inputs, model['phase'], model['candidate']))))


def metric(rows, name):
    errors = []
    instances, cases = defaultdict(list), defaultdict(list)
    for row in rows:
        y, p = row['observed_ms'], row['predictions'][name]
        error = abs(p-y)/y if y else (0. if p == 0 else float('inf'))
        errors.append(error)
        instances[row['instance_id']].append(error <= .25)
        cases[row['case_id']].append(error <= .25)
    return dict(events=len(rows), within25_percent=100*mean(e <= .25 for e in errors),
                equal_instance_percent=100*mean(mean(v) for v in instances.values()),
                worst_error_percent=100*max(errors), all_event_cases=sum(all(v) for v in cases.values()))


def joint_diagnostic(rows, reports):
    groups, joints = defaultdict(list), defaultdict(dict)
    for row in rows:
        y = row['observed_ms']
        passed = {k:abs(v-y) <= .25*y for k,v in row['predictions'].items()}
        if row['phase'] == 'prefill':
            groups[row['instance_id']].append(int(passed['prefill_attention'])-int(passed['pooled']))
        joints[(row['case_id'],row['physical_request_id'])][row['phase']] = passed
    if any(set(v) != {'queue','prefill','decode','e2e'} for v in joints.values()):
        raise ValueError('incomplete native target conjunction')
    rng = random.Random(20260914)
    values = list(groups.values())
    gains = []
    for _ in range(2000):
        sample = [rng.choice(values) for _ in values]
        gains.append(sum(map(sum,sample))/sum(map(len,sample)))
    gains.sort()
    return {'prefill_fixed_prediction_cluster_bootstrap_95':[gains[49],gains[1949]],
            'requests':len(joints),
            'all_four_native_targets_within25_before':sum(all(v['pooled'] for v in x.values()) for x in joints.values()),
            'all_four_native_targets_within25_selected':sum(all(v[reports[phase]['selected']] for phase,v in x.items()) for x in joints.values()),
            'scope':'grouped development; four targets=queue,prefill,decode,request E2E, not outer E2E; bootstrap excludes selection uncertainty'}


def main():
    rows, manifest, hashes = native.load_dataset(native.DEFAULT_DATASET, native.DEFAULT_MANIFEST)
    if len({r['hardware_domain'] for r in rows}) != 1:
        raise ValueError('experiment requires one verified native hardware domain')
    predictions, reports, selected = [], {}, {}
    for phase in ('queue', 'prefill', 'decode', 'e2e'):
        group = [r for r in rows if r['native_component'] == phase]
        out = []
        for fold in range(5):
            train = [r for r in group if native.fold_for_instance(r['instance_id']) != fold]
            test = [r for r in group if native.fold_for_instance(r['instance_id']) == fold]
            models = {c: fit(train, phase, c) for c in CANDIDATES}
            for row in test:
                inputs = {k:row[k] for k in ('prompt_tokens','completion_tokens','cached_tokens')}
                out.append({k:row[k] for k in ('event_id','case_id','instance_id','physical_request_id','observed_ms')} |
                           dict(phase=phase, fold=fold, predictions={c:predict(model,inputs) for c,model in models.items()}))
        metrics = {c:metric(out,c) for c in CANDIDATES}
        baseline = metrics['pooled']
        eligible = [c for c in CANDIDATES if metrics[c]['within25_percent'] > baseline['within25_percent']
                    and metrics[c]['equal_instance_percent'] >= baseline['equal_instance_percent']
                    and metrics[c]['worst_error_percent'] <= baseline['worst_error_percent']]
        winner = max(eligible, key=lambda c:metrics[c]['within25_percent']) if eligible else 'pooled'
        selected[phase] = fit(group,phase,winner)
        reports[phase] = {'metrics':metrics, 'selected':winner}
        predictions += out
    HERE.mkdir(exist_ok=True)
    report = dict(phases=reports, source_hashes=hashes, code_sha256=native.sha256_file(Path(__file__)),
                  scope='conditional supplied token/cache workload; fixed grouped development, not final validation',
                  hardware_transfer_validated=False, d9_pass=False)
    artifact = dict(schema='native-refinement.v1', hardware_domain=rows[0]['hardware_domain'], models=selected,
                    source_hashes=hashes, report=report)
    for name, value in [('report',report),('fit_artifact',artifact)]:
        (HERE/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    (HERE/'predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in predictions))
    (HERE/'joint_diagnostic.json').write_text(json.dumps(joint_diagnostic(predictions,reports),indent=2)+'\n')
    print(json.dumps(reports,indent=2))


if __name__ == '__main__': main()
