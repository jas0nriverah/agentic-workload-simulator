"""Select declared training identities before decoding mixed-cache row values.

Encoded mixed JSON is structurally scanned; excluded row targets/actions are
never deserialized, logged, validated, fitted, or scored. This does not undo
the historical access disclosure. No source trajectory is opened.
"""
from pathlib import Path
import collections
import hashlib
import json
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.assignment.historical_analysis_scope import frozen_scope

A = Path('/home/riverahernandezjason/h100-assignment-work-20260905/assignment')
CACHE = A / 'submission/20260908T043000Z/d9-cpu/calibration_events.json'
ACTIONS = A / 'submission/20260908T060000Z/d9-cpu-review/calibration_actions.json'
MANIFEST = A / 'submission/20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json'
OUT = Path(__file__).resolve().parent / 'training_view'
STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]]')

def ws(s, i):
    while i < len(s) and s[i].isspace():
        i += 1
    return i

def end(s, i):
    i = ws(s, i)
    if s[i] == '"':
        return STRING.match(s, i).end()
    if s[i] in '{[':
        stack = []
        for match in TOKEN.finditer(s, i):
            token = match.group()
            if token in ('{', '['):
                stack.append(token)
            elif token in ('}', ']'):
                assert stack.pop() == ('{' if token == '}' else '[')
                if not stack:
                    return match.end()
        raise ValueError('unterminated JSON container')
    j = i
    while j < len(s) and s[j] not in ',]}':
        j += 1
    return j

def object_spans(s, i=0):
    i = ws(s, i)
    assert s[i] == '{'
    i = ws(s, i+1)
    while s[i] != '}':
        key_end = end(s, i)
        key = json.loads(s[i:key_end])
        i = ws(s, key_end)
        assert s[i] == ':'
        start = ws(s, i+1)
        stop = end(s, start)
        yield key, start, stop
        i = ws(s, stop)
        if s[i] == '}':
            return
        assert s[i] == ','
        i = ws(s, i+1)

def array_spans(s, i):
    assert s[i] == '['
    i = ws(s, i+1)
    while s[i] != ']':
        stop = end(s, i)
        yield i, stop
        i = ws(s, stop)
        if s[i] == ']':
            return
        assert s[i] == ','
        i = ws(s, i+1)

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    scope = frozen_scope()
    train = {r['instance_id'] for r in json.loads(MANIFEST.read_text())['clusters']
             if r['partition'] == 'train_calibration'}
    assert len(train) == 546 and not train & scope.excluded_instance_ids
    assert digest(CACHE) == 'caa764f424819d53ddca826c142f63699d24e262d7d9a4973653eb7b2427f6dc'
    assert digest(ACTIONS) == 'ab0bfc45e82e5e69745cb409f1a3791c58c4935f93bacfb498427b11e52614e3'
    OUT.mkdir(exist_ok=False)
    text = CACHE.read_text()
    tables = {}
    counts = {}
    for kind, start, stop in object_spans(text):
        if kind not in {'tools', 'models', 'trajectories'}:
            continue
        rows = []
        count = collections.Counter()
        for begin, finish in array_spans(text, start):
            identity = {key: json.loads(text[left:right])
                        for key, left, right in object_spans(text, begin)
                        if key in {'instance_id', 'run_id'}}
            instance = identity.get('instance_id')
            if not instance and identity.get('run_id'):
                instance = scope.identity_by_run_id.get(identity['run_id'])
                if instance:
                    identity['instance_id'] = instance
            if instance not in train:
                count['not_train'] += 1
                continue
            scope.assert_eligible(identity)
            row = json.loads(text[begin:finish])
            row['instance_id'] = instance
            row['outer_fold'] = int.from_bytes(hashlib.sha256(
                ('assignment.d9.train-fold-v1:' + instance).encode()).digest()[:8], 'big') % 5
            rows.append(row)
        tables[kind] = rows
        count['retained'] = len(rows)
        count['instances'] = len({r['instance_id'] for r in rows})
        counts[kind] = dict(count)
    del text
    allowed = {r['event_id']: r for r in tables['tools']}
    actions = {}
    text = ACTIONS.read_text()
    for key, start, stop in object_spans(text):
        if key not in allowed:
            continue
        action = json.loads(text[start:stop])
        assert hashlib.sha256(action.strip().encode()).hexdigest() == allowed[key]['command_sha256']
        actions[key] = action
    assert len(actions) == len(allowed)
    for row in tables['tools']:
        row['action'] = actions[row['event_id']]
    runs = {r['run_id'] for r in tables['trajectories']}
    assert all(r['run_id'] in runs for k in ('tools','models') for r in tables[k])
    for kind, rows in tables.items():
        with (OUT / (kind + '.jsonl')).open('x') as f:
            for row in rows:
                f.write(json.dumps(row, sort_keys=True) + '\n')
    metadata = {
        'partition': 'train_calibration', 'manifest_instances': len(train), 'counts': counts,
        'feature_policy': 'Rows retain targets separately for evaluation; models must explicitly whitelist prediction-time inputs. No output_tokens, timing, residual, outcome, or future-state feature is authorized.',
        'source_hashes': {str(p): digest(p) for p in (CACHE,ACTIONS,MANIFEST)},
        'output_hashes': {p.name: digest(p) for p in OUT.glob('*.jsonl')},
        'scope': scope.artifact(),
        'access': 'Mixed encoded JSON structurally scanned. Identity-only fields decoded first; complete row and action values decoded only for authorized train instances. No raw trajectory opened. Prior mixed-artifact exposure remains disclosed.',
    }
    (OUT/'manifest.json').write_text(json.dumps(metadata,indent=2)+'\n')
    print(json.dumps(counts))
    print(json.dumps({k: sorted(rows[0]) for k,rows in tables.items()}))

if __name__ == '__main__':
    main()
