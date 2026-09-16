#!/usr/bin/env python3
"""Strict, case-scoped D9 acceptance; never fits or changes a model.

Expected event identities/taxonomy must be frozen independently of predictions.
Use one case at a time so full-matrix callers can stream case results. Historical
v3 reports and scoring implementations are intentionally untouched.
"""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

SCHEMA = 'assignment.d9-individual-acceptance.v2'
KINDS = {'cpu', 'model', 'e2e'}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def assess(expected, predicted):
    errors = []
    if not expected:
        raise ValueError('Expected event inventory is empty')
    cases = {r.get('case_id') for r in expected}
    if len(cases) != 1 or not isinstance(next(iter(cases)), str) or not next(iter(cases)).strip():
        raise ValueError('Validate one exact, nonempty case identity at a time')
    case_id = next(iter(cases))
    for name, rows in [('expected', expected), ('predicted', predicted)]:
        ids = [r.get('event_id') for r in rows]
        if any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
            raise ValueError(f'{name}: missing or duplicate event identity')
        if any(r.get('kind') not in KINDS or r.get('case_id') != case_id for r in rows):
            raise ValueError(f'{name}: unknown event kind or mismatched case')
    if sum(r['kind'] == 'e2e' for r in expected) != 1:
        raise ValueError('Expected inventory requires exactly one E2E target')
    expected_ids = {r['event_id'] for r in expected}
    predictions = {r['event_id']: r for r in predicted}
    unexpected = sorted(set(predictions) - expected_ids)
    if unexpected:
        errors.append({'reason': 'unexpected_prediction_ids', 'event_ids': unexpected})
    details = []
    for row in expected:
        event_id = row['event_id']
        prediction = predictions.get(event_id)
        observed = row.get('observed_ms')
        value = prediction.get('predicted_ms') if prediction is not None else None
        reason = None
        if prediction is None:
            reason = 'missing_prediction'
        elif prediction['kind'] != row['kind']:
            reason = 'event_kind_mismatch'
        elif row.get('availability') != 'measured' or not number(observed) or observed <= 0:
            reason = 'unscorable_observation_or_censored_event'
        elif not number(value) or value < 0:
            reason = 'unsupported_or_nonfinite_prediction'
        ape = None if reason else 100 * abs(value - observed) / observed
        within = reason is None and ape <= 25.0
        details.append({'event_id': event_id, 'kind': row['kind'], 'absolute_percentage_error': ape,
                        'within_25_percent': within, 'reason': reason})
    scored = [r['absolute_percentage_error'] for r in details if r['absolute_percentage_error'] is not None]
    failures = [r for r in details if not r['within_25_percent']]
    return {'schema_version': SCHEMA, 'case_id': case_id, 'threshold_percent': 25.0,
            'status': 'pass' if not errors and not failures else 'fail',
            'expected_event_count': len(expected), 'prediction_count': len(predicted),
            'expected_counts_by_kind': dict(Counter(r['kind'] for r in expected)),
            'failed_or_unscorable_count': len(failures), 'structural_errors': errors,
            'mean_ape_diagnostic_only': sum(scored) / len(scored) if scored else None,
            'max_ape_diagnostic_only': max(scored) if scored else None,
            'individual_results': details,
            'gate_policy': 'Every expected individual event AND E2E must be scorable and within25%; mean error never substitutes.'}


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-events', type=Path, required=True)
    parser.add_argument('--predictions', type=Path, required=True)
    parser.add_argument('--event-taxonomy-sha256', required=True)
    args = parser.parse_args()
    if len(args.event_taxonomy_sha256) != 64 or any(c not in '0123456789abcdef' for c in args.event_taxonomy_sha256):
        parser.error('event taxonomy must be bound to its predeclared SHA-256')
    result = assess(read_rows(args.expected_events), read_rows(args.predictions))
    result['input_bindings'] = {name: {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                               for name, path in [('expected_events', args.expected_events), ('predictions', args.predictions)]}
    result['event_taxonomy_sha256'] = args.event_taxonomy_sha256
    print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
    raise SystemExit(0 if result['status'] == 'pass' else 1)
