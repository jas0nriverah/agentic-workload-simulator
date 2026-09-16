"""Fixed grouped development check of a request-start-known indicator."""
import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
spec = importlib.util.spec_from_file_location('native_comparison', BASE/'native/run_native_comparison.py')
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def design(row, candidate):
    base = 'relative_nnls_token_cache' if candidate.startswith('cache') else 'relative_nnls_token'
    values = native.design(row, base)
    return values + ([float(row['start_ordinal'] == 0)] if candidate.endswith('_first') else [])


def metrics(rows):
    errors = [abs(r['prediction_ms']-r['observed_ms'])/r['observed_ms']*100 for r in rows]
    grouped = defaultdict(list)
    cases = defaultdict(list)
    for row, error in zip(rows, errors):
        grouped[row['instance_id']].append(error <= 25)
        cases[row['case_id']].append(error <= 25)
    return {'events':len(rows), 'within25':sum(e <= 25 for e in errors),
            'coverage_percent':100*mean(e <= 25 for e in errors),
            'equal_instance_coverage_percent':100*mean(mean(v) for v in grouped.values()),
            'worst_error_percent':max(errors),
            'all_request_cases_pass':sum(all(v) for v in cases.values())}


def main():
    rows, manifest, hashes = native.load_dataset(native.DEFAULT_DATASET, native.DEFAULT_MANIFEST)
    rows = [dict(r) for r in rows if r['native_component'] == 'e2e']
    start_map = {}
    sources = []
    for case in manifest['cases']:
        source = case['source']['model_events']
        path = Path(source['path'])
        assert native.sha256_file(path) == source['sha256']
        events = [json.loads(line) for line in path.read_text().splitlines()]
        starts = sorted((r for r in events if r.get('event_kind') == 'model_request_start'),
                        key=lambda r:r['start_mono_ns'])
        assert len({r['start_mono_ns'] for r in starts}) == len(starts)
        for ordinal, row in enumerate(starts):
            key = (case['case_id'],row['physical_request_id'])
            assert key not in start_map
            start_map[key] = ordinal
        sources.append(source)
    for row in rows:
        row['start_ordinal'] = start_map[(row['case_id'],row['physical_request_id'])]
        row['fold'] = native.fold_for_instance(row['instance_id'])
    predictions, fits = [], []
    names = ['token','token_first','cache','cache_first']
    for candidate in names:
        for fold in range(5):
            train = [r for r in rows if r['fold'] != fold]
            test = [r for r in rows if r['fold'] == fold]
            assert not {r['instance_id'] for r in train} & {r['instance_id'] for r in test}
            assert all(r['observed_ms'] > 0 for r in train+test)
            beta = native.COMPARE.fit([design(r,candidate) for r in train],
                                      [r['observed_ms'] for r in train],True)
            fits.append({'candidate':candidate,'fold':fold,'coefficients':beta})
            for row in test:
                predictions.append({k:row[k] for k in ('case_id','instance_id','physical_request_id','fold','start_ordinal','observed_ms')} |
                                   {'candidate':candidate,'prediction_ms':native.COMPARE.predict(beta,design(row,candidate))})
    result = {'contract':'Conditional supplied prompt/output/cache workload; first physical client request indicator is known at request start, not inferred from latency. Same five instance-grouped development folds; selection is adaptive development, not untouched evaluation.',
              'source_hashes':hashes,'start_sources':sources,
              'models':{n:metrics([r for r in predictions if r['candidate']==n]) for n in names},
              'first_request':{n:metrics([r for r in predictions if r['candidate']==n and r['start_ordinal']==0]) for n in names},
              'folds':{n:{str(f):metrics([r for r in predictions if r['candidate']==n and r['fold']==f]) for f in range(5)} for n in names},
              'fits':fits,'new_inference':False,'hardware_transfer_validated':False,
              'script_sha256':native.sha256_file(Path(__file__))}
    (HERE/'report.json').write_text(json.dumps(result,indent=2)+'\n')
    (HERE/'predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in predictions))
    print(json.dumps(result['models'],indent=2))


if __name__ == '__main__':
    main()
