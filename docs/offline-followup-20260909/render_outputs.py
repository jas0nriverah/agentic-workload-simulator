"""Descriptive tables from validated ledgers; durations are separate boundaries."""
import csv
import html
import json
import math
from collections import defaultdict
from pathlib import Path


def write_csv(path, rows, fields):
    with path.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def render(reports, output_dir):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases, components = [], []
    for report in reports:
        if report['validation']['status'] != 'valid':
            raise ValueError('invalid ledger cannot produce figure inputs')
        identity = report['identity']
        groups = defaultdict(list)
        for line in Path(report['case_event_records']['path']).read_text().splitlines():
            row = json.loads(line)
            value = row.get('observed_ms')
            if isinstance(value, (float, int)) and math.isfinite(value) and value >= 0:
                groups[(row['attempt_id'], row['event_class'], row['target_boundary'])].append(value)
        for (attempt, klass, boundary), values in sorted(groups.items()):
            if klass in {'evidence_observation', 'tool_intent'}:
                continue
            components.append(dict(identity, attempt_id=attempt, event_class=klass,
                                   target_boundary=boundary, count=len(values),
                                   sum_observed_ms=sum(values),
                                   interpretation='separate boundary; overlaps other rows; do not sum across classes'))
        for attempt in report['attempts']:
            aid = attempt['attempt_id']
            def metric(klass):
                values = [v for (a, k, b), vs in groups.items() if a == aid and k == klass for v in vs]
                return len(values), sum(values)
            row = dict(identity, attempt_id=aid, partition=report['derived_partition'],
                       outer_e2e_ms=attempt['outer_e2e_boundary']['observed_ms'],
                       evaluator_score_status='unproven_patch_evaluator_binding; no score emitted')
            for klass, label in [('semantic_action', 'semantic_action'), ('runtime_command', 'runtime_command'),
                                 ('native:queue', 'native_queue'), ('native:prefill', 'native_prefill'),
                                 ('native:decode', 'native_decode'), ('native:e2e', 'native_e2e')]:
                row[label + '_count'], row[label + '_ms'] = metric(klass)
            cases.append(row)
    write_csv(out / 'case_measurements.csv', cases, list(cases[0]) if cases else ['case_id'])
    write_csv(out / 'component_metrics.csv', components, list(components[0]) if components else ['case_id'])
    # Separate bars, never a stacked E2E decomposition or CPU/GPU device ratio.
    bars = [(r['case_id'], label, r[label + '_ms'] / 1000) for r in cases
            for label in ('semantic_action', 'runtime_command', 'native_prefill', 'native_decode')]
    height = 110 + 32 * len(bars)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="{height}" viewBox="0 0 1100 {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             '<g font-family="sans-serif" fill="#182c3a">',
             '<text x="20" y="28" font-size="19">Validated retained cases: separate command and native service boundaries</text>',
             '<text x="20" y="52" font-size="12">Seconds. Descriptive only; overlapping boundaries are not additive. Native service is not GPU kernel time.</text>']
    scale = 470 / max([value for _, _, value in bars] + [1])
    for i, (case, label, value) in enumerate(bars):
        y = 85 + i * 32
        parts += [f'<text x="20" y="{y + 15}" font-size="11">{html.escape(case[-42:])} / {label}</text>',
                  f'<rect x="535" y="{y}" width="{value * scale:.3f}" height="20" fill="#247b9e"/>',
                  f'<text x="{542 + value * scale:.3f}" y="{y + 15}" font-size="12">{value:.3f}</text>']
    parts += ['</g></svg>']
    (out / 'case_boundaries.svg').write_text('\n'.join(parts) + '\n')
    return {'case_attempt_count': len(cases), 'component_rows': len(components),
            'score_status': 'not_emitted_without_patch_evaluator_binding'}
