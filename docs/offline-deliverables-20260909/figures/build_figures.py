#!/usr/bin/env python3
"""Build bounded retained-evidence figure inputs; this never changes acquisition."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path('/home/riverahernandezjason/agentic-submission-repairs-20260908')
OUT = REPO / 'docs/offline-deliverables-20260909/figures'
RETAINED = REPO / 'docs/retained-analysis-20260909/results/refined'
HIST = Path('/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/figures-input')
TRAIN_VIEW = REPO / 'docs/offline-deliverables-20260909/training_view'
sys.path.insert(0, str(REPO))
from scripts.assignment.historical_analysis_scope import frozen_scope


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def csv_rows(path: Path):
    with path.open(newline='') as handle:
        return list(csv.DictReader(handle))


def write_csv(name: str, fields: list[str], rows: list[dict]):
    with (OUT / name).open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def number(value, places=3):
    return f'{value:.{places}f}'


def svg_begin(width, height, title, subtitle):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<title>{escape(title)}</title><desc>{escape(subtitle)}</desc>
<rect width="100%" height="100%" fill="#ffffff"/>
<style>text{{font-family:DejaVu Sans,Arial,sans-serif;fill:#172033}} .title{{font-size:20px;font-weight:bold}} .sub{{font-size:11px;fill:#4b5563}} .axis{{font-size:10px;fill:#374151}} .note{{font-size:9px;fill:#4b5563}} .value{{font-size:11px;font-weight:bold}}</style>
<text x="24" y="28" class="title">{escape(title)}</text><text x="24" y="45" class="sub">{escape(subtitle)}</text>'''


def svg_end(note):
    return f'<text x="24" y="{note[0]}" class="note">{escape(note[1])}</text></svg>\n'


def save_svg_png(stem: str, svg: str):
    svg_path = OUT / f'{stem}.svg'
    svg_path.write_text(svg)
    subprocess.run(['rsvg-convert', '-f', 'png', '-o', str(OUT / f'{stem}.png'), str(svg_path)], check=True)


def scale_y(value, max_value, top, height):
    return top + height * (1 - value / max_value)


def d1_figure(headlines):
    width, height, left, top, chart_h = 760, 440, 78, 84, 250
    values = [('Lite', float(headlines['lite']['resolved_percent']), 'resolved rate (%)'),
              ('Verified', float(headlines['verified']['resolved_percent']), 'resolved rate (%)')]
    svg = svg_begin(width, height, 'D1 historical headline aggregates', 'Published aggregate only; original accepted-case E2E mean is reported separately below.')
    svg += f'<line x1="{left}" y1="{top+chart_h}" x2="720" y2="{top+chart_h}" stroke="#64748b"/>'
    for tick in range(0, 51, 10):
        y = scale_y(tick, 50, top, chart_h)
        svg += f'<line x1="{left}" y1="{y:.1f}" x2="720" y2="{y:.1f}" stroke="#e5e7eb"/><text x="46" y="{y+4:.1f}" class="axis">{tick}</text>'
    for index, (suite, rate, _) in enumerate(values):
        x = 205 + index * 250
        y = scale_y(rate, 50, top, chart_h)
        svg += f'<rect x="{x}" y="{y:.1f}" width="112" height="{top+chart_h-y:.1f}" fill="{["#2f6f9f", "#5b9a8b"][index]}"/>'
        svg += f'<text x="{x+56}" y="{y-9:.1f}" text-anchor="middle" class="value">{rate:.2f}%</text><text x="{x+56}" y="{top+chart_h+20}" text-anchor="middle" class="axis">{suite}</text>'
        m = float(headlines[suite.lower()]['mean_original_accepted_e2e_s'])
        n = headlines[suite.lower()]['denominator']
        r = headlines[suite.lower()]['resolved']
        svg += f'<text x="{x+56}" y="{top+chart_h+45}" text-anchor="middle" class="note">{r}/{n}; mean E2E {m:.3f} s</text>'
    svg += svg_end((405, 'Rate denominator: all published suite cases. Mean: original accepted-case E2E, evaluator time excluded; not a CPU/GPU result.'))
    save_svg_png('d1_historical_headlines', svg)


def d4_figure(pair_rows, summaries):
    width, height = 1040, 650
    params = ['call_limit', 'max_output_tokens', 'observation_length', 'temperature']
    labels = {'call_limit': 'Call limit', 'max_output_tokens': 'Max output tokens', 'observation_length': 'Observation length', 'temperature': 'Temperature'}
    svg = svg_begin(width, height, 'D4 paired historical sweep: E2E wall time', 'Each faint line is one eligible matched pair (18 pairs across 17 independent instance clusters per parameter/setting); baseline copies stay within their parameter panel.')
    for i, param in enumerate(params):
        col, row = i % 2, i // 2
        x0, y0, w, h = 60 + col * 500, 80 + row * 250, 410, 155
        samples = [r for r in pair_rows if r['parameter'] == param]
        settings = sorted({r['setting'] for r in samples}, key=float)
        max_v = max(float(r['baseline_e2e_s']) for r in samples + [r for r in samples])
        max_v = max(max_v, max(float(r['treatment_e2e_s']) for r in samples)) * 1.08
        min_v = 0.0
        svg += f'<text x="{x0}" y="{y0-8}" class="value">{labels[param]}</text><line x1="{x0}" y1="{y0+h}" x2="{x0+w}" y2="{y0+h}" stroke="#94a3b8"/>'
        # Baseline point repeated only for within-panel comparison.
        by_pair = defaultdict(dict)
        for r in samples:
            by_pair[r['pair_id']][r['setting']] = r
        xs = [x0 + 46 + j * (w - 82) / (len(settings)) for j in range(len(settings) + 1)]
        for pair in by_pair.values():
            points = [(xs[0], scale_y(float(next(iter(pair.values()))['baseline_e2e_s']), max_v, y0, h))]
            points += [(xs[j+1], scale_y(float(pair[s]['treatment_e2e_s']), max_v, y0, h)) for j, s in enumerate(settings) if s in pair]
            svg += '<polyline points="' + ' '.join(f'{x:.1f},{y:.1f}' for x,y in points) + '" fill="none" stroke="#a8b6c8" stroke-opacity="0.52" stroke-width="1"/>'
        for j, setting in enumerate(['baseline'] + settings):
            x = xs[j]
            if setting == 'baseline':
                vals = [float(r['baseline_e2e_s']) for r in samples]
                label = 'base'
            else:
                vals = [float(r['treatment_e2e_s']) for r in samples if r['setting'] == setting]
                label = setting
            mean = statistics.mean(vals)
            y = scale_y(mean, max_v, y0, h)
            svg += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#d04f3c"/><text x="{x:.1f}" y="{y-7:.1f}" text-anchor="middle" class="note">{mean:.1f}s</text><text x="{x:.1f}" y="{y0+h+16}" text-anchor="middle" class="axis">{label}</text>'
        for fraction, label in ((0.0, '0'), (0.5, f'{max_v/2:.0f}'), (1.0, f'{max_v:.0f}')):
            y = y0 + h * (1-fraction)
            svg += f'<text x="{x0-6}" y="{y+3:.1f}" text-anchor="end" class="note">{label}s</text>'
    svg += svg_end((628, '72 baseline-coordinate copies represent 18 within-panel pairs over 17 dependent instance clusters, not 72 independent observations. Historical model/tool values are proxy walls.'))
    save_svg_png('d4_paired_sweep_e2e', svg)


def d8_boundary_figure(evidence):
    fixture = evidence['current_fixture']
    boundary = fixture['tool_boundary_disambiguation']
    native = fixture['native_phase_totals_ms']
    rows = [
        ('semantic agent actions', boundary['semantic_agent_tool_wall_ms'] / 1000, '#2f6f9f'),
        ('auxiliary runtime commands', boundary['auxiliary_runtime_command_wall_ms'] / 1000, '#6c8ebf'),
        ('tool-execution union', boundary['all_tool_execution_phase_union_ms'] / 1000, '#314a67'),
        ('native prefill service', native['prefill'] / 1000, '#d98e3f'),
        ('native decode service', native['decode'] / 1000, '#c15d3e'),
        ('native E2E service', native['e2e'] / 1000, '#7e5aa6'),
    ]
    width, height, left, top, chart_h = 900, 460, 260, 80, 270
    maximum = 85
    svg = svg_begin(width, height, 'D8 retained confirmation: command and native service walls', 'Retained confirmation fixture (descriptive only; excluded from fitting); 40 semantic actions, 60 runtime commands, and 40 physical native requests.')
    svg += f'<line x1="{left}" y1="350" x2="850" y2="350" stroke="#e5e7eb"/><text x="{left}" y="367" class="axis">0 s</text><text x="850" y="367" text-anchor="end" class="axis">85 s</text>'
    for i, (label, value, colour) in enumerate(rows):
        y = 92 + i * 40
        w = value / maximum * 570
        svg += f'<text x="{left-12}" y="{y+15}" text-anchor="end" class="axis">{escape(label)}</text><rect x="{left}" y="{y}" width="{w:.1f}" height="22" fill="{colour}"/><text x="{left+w+8:.1f}" y="{y+16}" class="value">{value:.3f}s</text>'
    semantic_ratio = boundary['semantic_tool_to_native_inference_ratio']
    all_ratio = boundary['all_tool_execution_to_native_inference_ratio']
    svg += svg_end((414, f'Ratios use native prefill+decode service ({(native["prefill"]+native["decode"])/1000:.3f} s): semantic/native={semantic_ratio:.6f}; union/native={all_ratio:.6f}. Bars are side-by-side, never stacked across overlapping lifecycle phases.'))
    save_svg_png('d8_current_command_boundary', svg)


def d8_cache_figure(requests):
    width, height = 920, 520
    svg = svg_begin(width, height, 'D8 retained confirmation: cache-aware prefill and decode', 'Retained confirmation fixture (descriptive only; excluded from fitting). Fresh tokens = max(prompt tokens − cached tokens, 0); service walls, not GPU kernel timing.')
    panels = [
        (60, 86, 370, 305, 'Prefill vs fresh tokens', 'fresh_tokens', 'prefill_ms', '#d98e3f'),
        (500, 86, 370, 305, 'Decode vs generated tokens', 'output_tokens', 'decode_ms', '#c15d3e'),
    ]
    for x0, y0, w, h, title, xkey, ykey, colour in panels:
        vals_x = [float(r[xkey]) for r in requests]
        vals_y = [float(r[ykey]) / 1000 for r in requests]
        max_x, max_y = max(vals_x) * 1.08, max(vals_y) * 1.08
        svg += f'<text x="{x0}" y="{y0-8}" class="value">{title}</text><line x1="{x0}" y1="{y0+h}" x2="{x0+w}" y2="{y0+h}" stroke="#64748b"/><line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0+h}" stroke="#64748b"/>'
        for r in requests:
            x = x0 + float(r[xkey]) / max_x * w
            y = scale_y(float(r[ykey])/1000, max_y, y0, h)
            svg += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{colour}" fill-opacity="0.72"/>'
        svg += f'<text x="{x0+w/2}" y="{y0+h+24}" text-anchor="middle" class="axis">{escape("fresh prompt tokens" if xkey=="fresh_tokens" else "generated tokens")}</text><text x="{x0+3}" y="{y0+12}" class="note">max {max_y/1.08:.2f}s</text>'
    svg += svg_end((490, 'Growing total context is not fresh prefill work: measured cached tokens range from 0 to 35,264. This descriptive fixture is not a new D8 example-selection result.'))
    save_svg_png('d8_current_cache_aware_phases', svg)


def main():
    evidence = json.loads((RETAINED / 'evidence.json').read_text())
    request_rows = csv_rows(RETAINED / 'current_native_requests.csv')
    for row in request_rows:
        row['fresh_tokens'] = max(int(row['prompt_tokens']) - int(row['cached_tokens']), 0)
    headlines = {r['suite']: r for r in csv_rows(RETAINED / 'historical_headlines.csv')}

    # Identity-only historical scope gate: retain no labels before this gate.
    scope = frozen_scope()
    eligible = {}
    metadata_path = HIST / 'sweep_metadata.jsonl'
    for line in metadata_path.read_text().splitlines():
        record = json.loads(line)
        if record.get('record_type') == 'metadata':
            continue
        identity = {'instance_id': record['instance_id'], 'run_id': record['run_id']}
        if scope.is_eligible(identity):
            eligible[record['run_id']] = {
                'pair_key': (record['instance_id'], record['suite'], record['repeat_id']),
                'parameter': record['sweep_parameter'],
                'setting': record['sweep_value'],
                'is_baseline': record['config_id'] == 'shared-baseline',
            }
    sweep_values = {}
    # Every CSV record is structurally parsed to locate its run ID.  Only rows
    # already admitted by the identity gate have outcome/wall cells converted
    # to numbers or included in any derived output.
    with (HIST / 'sweep_runs.csv').open(newline='') as handle:
        for row in csv.DictReader(handle):
            if row['run_id'] in eligible:
                sweep_values[row['run_id']] = row
    assert len(eligible) == len(sweep_values) == 288

    grouped = defaultdict(dict)
    for run_id, meta in eligible.items():
        grouped[(meta['parameter'], meta['pair_key'])][('base' if meta['is_baseline'] else meta['setting'])] = sweep_values[run_id]
    pair_rows = []
    for (parameter, pair_key), points in sorted(grouped.items()):
        assert 'base' in points
        pair_id = hashlib.sha256(repr(pair_key).encode()).hexdigest()[:12]
        for setting, treatment in points.items():
            if setting == 'base':
                continue
            base = points['base']
            pair_rows.append({
                'pair_id': pair_id, 'parameter': parameter, 'setting': setting,
                'baseline_e2e_s': number(float(base['e2e_wall_ms']) / 1000, 6),
                'treatment_e2e_s': number(float(treatment['e2e_wall_ms']) / 1000, 6),
                'delta_treatment_minus_baseline_s': number((float(treatment['e2e_wall_ms']) - float(base['e2e_wall_ms'])) / 1000, 6),
                'baseline_resolved': base['official_resolved'], 'treatment_resolved': treatment['official_resolved'],
            })
    write_csv('d4_eligible_paired_clusters.csv', list(pair_rows[0]), pair_rows)
    d4_summary = []
    for parameter, setting in sorted({(r['parameter'], r['setting']) for r in pair_rows}):
        rows = [r for r in pair_rows if r['parameter'] == parameter and r['setting'] == setting]
        deltas = [float(r['delta_treatment_minus_baseline_s']) for r in rows]
        n_instance_clusters = len({eligible_run['pair_key'][0] for eligible_run in eligible.values()
                                   if eligible_run['parameter'] == parameter})
        assert len(rows) == 18 and n_instance_clusters == 17
        d4_summary.append({
            'parameter': parameter, 'setting': setting, 'n_pairs': len(rows), 'n_instance_clusters': n_instance_clusters,
            'baseline_mean_e2e_s': number(statistics.mean(float(r['baseline_e2e_s']) for r in rows), 3),
            'treatment_mean_e2e_s': number(statistics.mean(float(r['treatment_e2e_s']) for r in rows), 3),
            'mean_paired_delta_s': number(statistics.mean(deltas), 3),
            'median_paired_delta_s': number(statistics.median(deltas), 3),
            'treatment_faster': sum(d < 0 for d in deltas), 'treatment_slower': sum(d > 0 for d in deltas), 'ties': sum(d == 0 for d in deltas),
            'baseline_resolved_n': sum(r['baseline_resolved'] == 'true' for r in rows),
            'treatment_resolved_n': sum(r['treatment_resolved'] == 'true' for r in rows),
            'interpretation': 'descriptive eligible matched pairs; no pooled baseline duplication or causal claim',
        })
    write_csv('d4_paired_sweep_summary.csv', list(d4_summary[0]), d4_summary)

    # A pre-built train_calibration view is the only source for these D2-D7
    # descriptive summaries.  It contains no native-device phases and is not
    # the historical full baseline or a production population.
    train_rows = [json.loads(line) for line in (TRAIN_VIEW / 'trajectories.jsonl').read_text().splitlines() if line]
    train_manifest = json.loads((TRAIN_VIEW / 'manifest.json').read_text())
    assert len(train_rows) == train_manifest['counts']['trajectories']['retained'] == 819
    def summarize_train(rows, group_name, group_value, category_definition):
        tool_sum = sum(r['tool_wall_ms'] for r in rows)
        model_sum = sum(r['model_wall_ms'] for r in rows)
        return {
            'group_name': group_name, 'group_value': group_value,
            'n_runs': len(rows), 'n_instance_clusters': len({r['instance_id'] for r in rows}),
            'mean_e2e_s': number(statistics.mean(r['observed_ms'] for r in rows) / 1000, 3),
            'mean_tool_wall_s': number(statistics.mean(r['tool_wall_ms'] for r in rows) / 1000, 3),
            'mean_model_proxy_wall_s': number(statistics.mean(r['model_wall_ms'] for r in rows) / 1000, 3),
            'summed_tool_to_model_proxy_ratio': number(tool_sum / model_sum, 6),
            'mean_run_tool_to_model_proxy_ratio': number(statistics.mean(r['tool_wall_ms'] / r['model_wall_ms'] for r in rows), 6),
            'category_definition': category_definition,
            'scope': 'train_calibration only; incomplete historical proxy, not production or whole historical baseline',
            'cpu_gpu_interpretation': 'tool/model proxy walls only; not CPU/GPU hardware or native GPU phase timing',
        }
    by_repo = defaultdict(list)
    for row in train_rows:
        by_repo[row['repository']].append(row)
    repository_summary = [summarize_train(rows, 'repository', repository, 'repository retained by the train-only view')
                          for repository, rows in sorted(by_repo.items())]
    # The train view has no independent category field; repository is its only
    # retained category-like grouping and is labeled as such rather than guessed.
    category_summary = [summarize_train(rows, 'category', repository, 'category unavailable in train view; repository used as the retained category-like grouping')
                        for repository, rows in sorted(by_repo.items())]
    write_csv('d2_d7_train_repository_summary.csv', list(repository_summary[0]), repository_summary)
    write_csv('d2_d7_train_category_summary.csv', list(category_summary[0]), category_summary)

    d1_rows = []
    for suite in ('lite', 'verified'):
        row = headlines[suite]
        d1_rows.append({'suite': suite, 'resolved_numerator': row['resolved'], 'suite_denominator': row['denominator'], 'resolved_percent': number(float(row['resolved_percent']), 6), 'mean_original_accepted_e2e_s': number(float(row['mean_original_accepted_e2e_s']), 6), 'population': 'published historical full-suite aggregate; all completed cases for latency; evaluator excluded'})
    write_csv('d1_historical_headline_table.csv', list(d1_rows[0]), d1_rows)

    native = evidence['current_fixture']['native_phase_totals_ms']
    boundary = evidence['current_fixture']['tool_boundary_disambiguation']
    ratio_rows = [
        {'metric': 'historical tool/model proxy ratio', 'numerator': 'sum historical tool-event wall', 'denominator': 'sum historical model-event wall', 'value': 'not recomputed here', 'population': 'legacy historical figure inputs; source-defined eligible/proxy population', 'cpu_gpu_interpretation': 'not a CPU/GPU ratio; no native phase or device-kernel attribution'},
        {'metric': 'current semantic/native inference proxy', 'numerator': '18.491612 s semantic agent tool wall', 'denominator': f'{(native["prefill"]+native["decode"])/1000:.6f} s native prefill+decode service', 'value': number(boundary['semantic_tool_to_native_inference_ratio'], 6), 'population': '1 retained confirmation fixture (descriptive only; excluded from fitting); 40 semantic actions / 40 physical requests', 'cpu_gpu_interpretation': 'not a CPU/GPU ratio; numerator is host command wall and denominator native service wall'},
        {'metric': 'current full tool-execution/native inference proxy', 'numerator': '32.634391 s tool-execution union (semantic + auxiliary runtime)', 'denominator': f'{(native["prefill"]+native["decode"])/1000:.6f} s native prefill+decode service', 'value': number(boundary['all_tool_execution_to_native_inference_ratio'], 6), 'population': 'same one fixture; 40 semantic actions, 60 auxiliary runtime commands, 40 physical requests', 'cpu_gpu_interpretation': 'not a CPU/GPU ratio; the union is broader than semantic agent work'},
        {'metric': 'current native prefill/decode service ratio', 'numerator': f'{native["prefill"]/1000:.6f} s prefill service', 'denominator': f'{native["decode"]/1000:.6f} s decode service', 'value': number(native['prefill']/native['decode'], 6), 'population': '40 physical requests in one fixture', 'cpu_gpu_interpretation': 'native service phase ratio only; not GPU kernel timing'},
    ]
    write_csv('ratio_definitions_and_populations.csv', list(ratio_rows[0]), ratio_rows)

    current_request_export = [{key: row[key] for key in ('request_ordinal', 'prompt_tokens', 'cached_tokens', 'fresh_tokens', 'output_tokens', 'prefill_ms', 'decode_ms', 'queue_ms', 'e2e_ms')} | {'population': '40 physical native requests, one retained confirmation fixture (descriptive only; excluded from fitting)', 'unit': 'tokens or milliseconds as named; native engine service wall'} for row in request_rows]
    write_csv('d8_current_native_requests_cache_aware.csv', list(current_request_export[0]), current_request_export)
    d8_boundary_rows = [
        {'component': 'semantic_agent_actions', 'count': 40, 'wall_s': number(boundary['semantic_agent_tool_wall_ms']/1000, 6), 'definition': 'terminal semantic agent tool events only'},
        {'component': 'auxiliary_runtime_commands', 'count': 60, 'wall_s': number(boundary['auxiliary_runtime_command_wall_ms']/1000, 6), 'definition': 'terminal runtime commands only'},
        {'component': 'tool_execution_union', 'count': '40 semantic + 60 auxiliary', 'wall_s': number(boundary['all_tool_execution_phase_union_ms']/1000, 6), 'definition': 'merged-clock phase union; do not add to overlapping lifecycle phase unions'},
        {'component': 'native_prefill_service', 'count': 40, 'wall_s': number(native['prefill']/1000, 6), 'definition': 'sum per-request native service phase'},
        {'component': 'native_decode_service', 'count': 40, 'wall_s': number(native['decode']/1000, 6), 'definition': 'sum per-request native service phase'},
        {'component': 'native_e2e_service', 'count': 40, 'wall_s': number(native['e2e']/1000, 6), 'definition': 'sum per-request native service E2E; no GPU kernel attribution'},
    ]
    write_csv('d8_current_command_and_service_boundaries.csv', list(d8_boundary_rows[0]), d8_boundary_rows)

    index = [
        {'deliverable': 'D1', 'status': 'derived current table/figure', 'data_domain': 'published historical aggregate', 'population': 'Lite 300; Verified 500', 'unit': 'percent; seconds', 'use': 'headline arithmetic only', 'constraint': 'no case-level recomputation'},
        {'deliverable': 'D2', 'status': 'derived train-only proxy table', 'data_domain': 'pre-built train_calibration trajectory view', 'population': '819 runs; 545 train instances; grouped rows vary by repository', 'unit': 'seconds; proxy ratio', 'use': 'repository/category descriptive summary', 'constraint': 'incomplete historical proxy; not CPU/GPU or production'},
        {'deliverable': 'D3', 'status': 'derived train-only proxy table', 'data_domain': 'pre-built train_calibration trajectory view', 'population': '819 runs; 545 train instances', 'unit': 'seconds; proxy ratio', 'use': 'repository/category descriptive summary', 'constraint': 'no native cache or device phase attribution'},
        {'deliverable': 'D4', 'status': 'derived current table/figure', 'data_domain': 'historical eligible sweep', 'population': '18 matched pairs across 17 independent instance clusters per parameter/setting after frozen-scope gate', 'unit': 'seconds', 'use': 'paired descriptive sweep', 'constraint': '72 baseline coordinates are dependent parameter-panel copies, not pooled independent observations'},
        {'deliverable': 'D5', 'status': 'derived train-only proxy table', 'data_domain': 'pre-built train_calibration trajectory view', 'population': '819 runs; 545 train instances', 'unit': 'seconds; proxy ratio', 'use': 'repository/category descriptive summary', 'constraint': 'incomplete historical proxy only'},
        {'deliverable': 'D6', 'status': 'derived train-only proxy table', 'data_domain': 'pre-built train_calibration trajectory view', 'population': '819 runs; 545 train instances', 'unit': 'seconds; proxy ratio', 'use': 'repository/category descriptive summary', 'constraint': 'not native-serving measurement'},
        {'deliverable': 'D7', 'status': 'derived train-only proxy table', 'data_domain': 'pre-built train_calibration trajectory view', 'population': '819 runs; 545 train instances', 'unit': 'seconds; proxy ratio', 'use': 'repository/category descriptive summary', 'constraint': 'not CPU/GPU hardware comparison'},
        {'deliverable': 'D8', 'status': 'derived current explanatory figure', 'data_domain': 'retained confirmation fixture (descriptive only; excluded from fitting)', 'population': '1 fixture; 40 semantic actions; 60 runtime commands; 40 physical requests', 'unit': 'seconds; milliseconds; tokens', 'use': 'explanation of retained confirmation fixture', 'constraint': 'not a new D8 selection, fitting input, or full production result'},
    ]
    write_csv('d1_d8_evidence_index.csv', list(index[0]), index)

    sources = {
        'scope_gate': {'module': str(REPO / 'scripts/assignment/historical_analysis_scope.py'), 'frozen_manifest_list_sha256': frozen_scope().artifact()['manifest_list_sha256'], 'rule': 'sweep metadata identity is gated before eligible rows are used; mixed sweep CSV records are structurally parsed to locate run IDs, but only eligible rows have outcome/wall cells decoded numerically or used'},
        'sources': {str(path): digest(path) for path in [RETAINED/'evidence.json', RETAINED/'current_native_requests.csv', RETAINED/'historical_headlines.csv', HIST/'d1_headline_metrics.json', HIST/'sweep_metadata.jsonl', HIST/'sweep_runs.csv', TRAIN_VIEW/'manifest.json', TRAIN_VIEW/'trajectories.jsonl']},
        'units': {'historical_and_current_wall': 'seconds in derived tables; source values are milliseconds where named', 'native': 'native engine service wall, not GPU kernel time', 'tokens': 'token counts as recorded'},
        'baseline_handling': 'Each of four parameter panels has 18 matched baseline-pair coordinates across 17 independent instance clusters. The 72 coordinate copies support within-panel contrasts and are not 72 independent observations.',
        'overlap_rule': 'Lifecycle phase unions may overlap and are never stacked. Semantic actions and auxiliary runtime commands are disjoint in this retained serial fixture and are shown separately from their union.',
        'unknowns': 'Unknown lifecycle gaps remain unknown; no cause or CPU attribution is fabricated.',
        'not_claimed': ['new experiment', 'frozen implementation change', 'new D8 example selection', 'CPU/GPU hardware ratio', 'causal sweep result', 'held-out case-level result'],
    }
    (OUT / 'provenance.json').write_text(json.dumps(sources, indent=2) + '\n')
    d1_figure(headlines)
    d4_figure(pair_rows, d4_summary)
    d8_boundary_figure(evidence)
    d8_cache_figure(request_rows)
    print(f'wrote retained evidence inputs and four SVG/PNG figures to {OUT}')


if __name__ == '__main__':
    main()
