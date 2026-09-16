"""Bounded, instance-grouped repaired command comparison; acquisition is immutable."""
from __future__ import annotations

import ast
from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path
import statistics
import random
import sys

HERE = Path(__file__).resolve().parent
BASE = HERE.parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel, semantic_features

spec = importlib.util.spec_from_file_location(
    'repaired_calibration', ROOT / 'docs/offline-followup-20260909/calibration/calibrate_repaired_d9.py')
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def indexed(path, kind=None):
    result = {}
    for row in map(json.loads, Path(path).open()):
        if kind and row.get('event_kind') != kind:
            continue
        key = row['event_id']
        if key in result:
            raise ValueError('duplicate raw event identity')
        result[key] = row
    return result


def script_descriptor(start, lifecycle, root):
    """Only static syntax of a verified pre-action snapshot; never execute it."""
    state = (start.get('actual_features') or start.get('features') or {}).get('script_state') or {}
    if state.get('status') != 'known' or not state.get('paths'):
        return 'unknown'
    source = lifecycle.get(state.get('source_event_id'))
    timestamp = state.get('observed_at_mono_ns')
    if (not source or not isinstance(timestamp, int)
            or timestamp > start['start_mono_ns']
            or any(source.get(k) != start.get(k) for k in ('case_id', 'attempt_id'))
            or any(source.get('clock', {}).get(k) != start.get('clock', {}).get(k)
                   for k in ('hostname', 'boot_id', 'clock_id'))):
        raise ValueError('script snapshot provenance mismatch')
    nodes = []
    for item in state['paths']:
        artifact = item.get('content_artifact') or {}
        if not artifact.get('artifact_path') or artifact.get('truncated'):
            return 'unknown'
        path = (root / artifact['artifact_path']).resolve()
        if not path.is_relative_to(root.resolve()) or sha(path) != artifact.get('sha256'):
            raise ValueError('script content path/hash mismatch')
        try:
            nodes.extend(ast.walk(ast.parse(path.read_text(encoding='utf-8'))))
        except (SyntaxError, UnicodeError, ValueError):
            return 'unknown'
    # Coarse generic syntax only: no identifier, literal, path or hash memorization.
    def bucket(n):
        return 0 if not n else 1 if n == 1 else 2 if n <= 4 else 3
    return json.dumps([
        bucket(sum(isinstance(n, (ast.Import, ast.ImportFrom)) for n in nodes)),
        bucket(sum(isinstance(n, (ast.For, ast.While, ast.comprehension)) for n in nodes)),
        bucket(sum(isinstance(n, ast.Call) for n in nodes)),
    ])


def join_target(target, start):
    clock = start.get('clock') or {}
    if (target['pre_event_id'] != start['event_id']
            or any(target.get(k) != start.get(k) for k in ('case_id', 'attempt_id', 'start_mono_ns'))
            or target['host_id'] != clock.get('hostname')
            or target['clock_id'] != str(clock.get('clock_id')) + '|boot=' + str(clock.get('boot_id'))):
        raise ValueError('target/start identity or clock mismatch')
    action = (start.get('actual_features') or start.get('features') or {}).get('action')
    if not isinstance(action, str) or not action.strip():
        raise ValueError('missing raw action')
    return action


def load_rows():
    partitions, _ = cal._load_pinned_partitions()
    manifest_path = BASE / 'cpu_lifecycle/manifest.json'
    manifest = read(manifest_path)
    rows, sources, seen = [], [], set()
    for case in manifest['cases']:
        root = cal._safe_resolve(Path(case['case_root']), cal.CASE_ROOTS_ROOT)
        instance, case_id, _ = cal._case_spec_identity(root)
        if (instance != case['instance_id'] or case_id != case['case_id']
                or partitions.get(instance) != 'train_calibration' or case_id in seen):
            raise ValueError('training identity/partition mismatch')
        seen.add(case_id)
        for name in ('events', 'validation_report'):
            if sha(case[name + '_path']) != case[name + '_sha256']:
                raise ValueError('normalized evidence hash mismatch')
        report = read(case['validation_report_path'])
        if report['validation']['status'] != 'valid':
            raise ValueError('invalid ledger')
        telemetry = root / 'runner_attempts/attempt-001/telemetry_v2'
        for name in ('tool_events.jsonl', 'lifecycle_events.jsonl'):
            expected = [s['sha256'] for s in report['source_hashes'] if s['path'].endswith('/' + name)]
            if len(expected) != 1 or sha(telemetry / name) != expected[0]:
                raise ValueError('raw journal hash mismatch')
        starts = indexed(telemetry / 'tool_events.jsonl', 'tool_event_start')
        lifecycle = indexed(telemetry / 'lifecycle_events.jsonl')
        validated, _ = cal._read_eligible_events(Path(case['events_path']), instance, case_id)
        admitted = {r['event_id']: r for r in validated}
        for target in map(json.loads, Path(case['events_path']).open()):
            if target['event_class'] != 'semantic_action':
                continue
            row = dict(admitted[target['event_id']])
            start = starts[target['pre_event_id']]
            row['action'] = join_target(target, start)
            row['operation_class'] = row['features']['operation_class']
            row['repository'] = instance.split('__')[0]
            row['script_descriptor'] = script_descriptor(start, lifecycle, telemetry)
            row['fold'] = cal._fold(instance)
            row['attempt_id'] = target['attempt_id']
            row['pre_event_id'] = target['pre_event_id']
            rows.append(row)
        sources.append({**case, 'tool_sha256': sha(telemetry / 'tool_events.jsonl'),
                        'lifecycle_sha256': sha(telemetry / 'lifecycle_events.jsonl')})
    return rows, {'manifest_sha256': sha(manifest_path), 'sources': sources,
                  'split_sha256': cal.PINNED_SPLIT_SHA256, 'implementation_sha256': sha(__file__)}


def script_key(row):
    features = semantic_features(row['action'], row['repository'])
    return json.dumps([features.get('semantic_class'), features.get('operation'), row['script_descriptor']])


def predict_fold(train, test):
    if any(r['observed_ms'] <= 0 for r in train + test):
        raise ValueError('semantic model requires positive labels; do not silently exclude')
    if {r['instance_id'] for r in train} & {r['instance_id'] for r in test}:
        raise ValueError('instance leakage')
    coarse, table = cal._table(train)
    semantic = SemanticCpuModel().fit(train)
    groups = defaultdict(list)
    for row in train:
        if row['script_descriptor'] != 'unknown':
            groups[script_key(row)].append(row)
    script_table = {k: statistics.median(r['observed_ms'] for r in values)
                    for k, values in groups.items()
                    if len(values) >= 25 and len({r['instance_id'] for r in values}) >= 3}
    result = []
    for row in test:
        # Predictions are handed only declared features, never validation labels.
        inputs = {k: row[k] for k in ('action', 'repository', 'operation_class', 'features', 'script_descriptor')}
        sem = semantic.predict(inputs)
        estimates = {'coarse': cal._prediction(inputs, coarse, table), 'semantic': sem,
                     'script': script_table.get(script_key(inputs), sem)}
        result.append({k: row[k] for k in ('case_id', 'instance_id', 'attempt_id', 'event_id', 'pre_event_id',
                                           'fold', 'observed_ms', 'hardware_domain', 'target_boundary')} |
                      {'predictions_ms': estimates, 'script_used': script_key(inputs) in script_table})
    return result


def metrics(rows, candidate):
    errors = [abs(r['predictions_ms'][candidate] - r['observed_ms']) / r['observed_ms'] for r in rows]
    groups = defaultdict(list)
    for row, error in zip(rows, errors):
        groups[row['instance_id']].append(error <= .25)
    return {'events': len(rows), 'within25': sum(e <= .25 for e in errors) / len(rows),
            'worst_error_pct': max(errors) * 100,
            'equal_instance_within25': statistics.mean(statistics.mean(g) for g in groups.values()),
            'all_events_pass_instances': sum(all(g) for g in groups.values()), 'instances': len(groups)}


def paired_gain(rows):
    groups = defaultdict(list)
    for row in rows:
        observed = row['observed_ms']
        p = row['predictions_ms']
        groups[row['instance_id']].append(
            int(abs(p['semantic'] - observed) <= .25 * observed)
            - int(abs(p['coarse'] - observed) <= .25 * observed))
    values = [groups[k] for k in sorted(groups)]
    rng = random.Random(20260913)
    gains = []
    for _ in range(2000):
        sample = [rng.choice(values) for _ in values]
        gains.append(sum(map(sum, sample)) / sum(map(len, sample)))
    gains.sort()
    return {'lower_95': gains[49], 'upper_95': gains[1949], 'replicates': 2000,
            'scope': 'paired instance bootstrap of fixed development predictions; excludes selection uncertainty'}


def main():
    rows, provenance = load_rows()
    groups = defaultdict(list)
    for row in rows:
        groups[(row['event_class'], row['target_boundary'], row['hardware_domain'])].append(row)
    predictions = []
    for group in groups.values():
        for fold in sorted({r['fold'] for r in group}):
            predictions.extend(predict_fold([r for r in group if r['fold'] != fold],
                                            [r for r in group if r['fold'] == fold]))
    report = {'cases': len(provenance['sources']), 'events': len(rows),
              'known_script_events': sum(r['script_descriptor'] != 'unknown' for r in rows),
              'script_override_events': sum(r['script_used'] for r in predictions),
              'metrics': {c: metrics(predictions, c) for c in ('coarse', 'semantic', 'script')},
              'scope': 'semantic action wall only; grouped development; no atomic/E2E/transfer claim',
              'promotion': 'none; comparison only; preserved final holdouts', 'd9_pass': False}
    report['semantic_minus_coarse_bootstrap'] = paired_gain(predictions)
    sem, coarse = report['metrics']['semantic'], report['metrics']['coarse']
    if (report['semantic_minus_coarse_bootstrap']['lower_95'] > 0
            and sem['equal_instance_within25'] > coarse['equal_instance_within25']
            and sem['worst_error_pct'] <= coarse['worst_error_pct']):
        report['promotion'] = 'semantic selected as development candidate; not D9 acceptance'
        if len(groups) != 1:
            raise ValueError('raw-free artifact requires one explicit target/hardware domain')
        artifact = {'schema': 'repaired-semantic-candidate.v1', 'model': SemanticCpuModel().fit(rows).to_mapping(),
                    'hardware_domain': rows[0]['hardware_domain'], 'target_boundary': rows[0]['target_boundary'],
                    'event_class': 'semantic_action', 'hardware_transfer_validated': False,
                    'training_provenance': provenance, 'development_metrics': sem}
        (HERE / 'fit_artifact.json').write_text(json.dumps(artifact, indent=2) + '\n')
    HERE.mkdir(exist_ok=True)
    for name, value in [('report', report), ('provenance', provenance)]:
        (HERE / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n')
    (HERE / 'predictions.jsonl').write_text(''.join(json.dumps(r, sort_keys=True) + '\n' for r in predictions))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
