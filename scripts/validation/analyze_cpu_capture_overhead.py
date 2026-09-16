#!/usr/bin/env python3
"""Offline extraction of existing CPU replay journals; never executes workloads.

Only emitted spans are called measured timings. Counts inferred from the pinned
driver/native writer are named separately. No fsync duration is synthesized.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import statistics
import struct
from typing import Any

MS = 1_000_000
MAX_BYTES = 64 * 1024 * 1024


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Inputs:
    def __init__(self):
        self.hashes: dict[str, dict[str, Any]] = {}

    def read(self, path: Path, expected: str | None = None) -> bytes:
        with path.open('rb') as f:
            raw = f.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, f'input exceeds bound: {path}')
        digest = sha(raw)
        require(expected is None or digest == expected, f'input hash mismatch: {path}')
        prior = self.hashes.get(str(path))
        require(prior is None or prior['sha256'] == digest, f'input changed: {path}')
        self.hashes[str(path)] = {'sha256': digest, 'bytes': len(raw)}
        return raw

    def json(self, path: Path, expected: str | None = None):
        return json.loads(self.read(path, expected))

    def rows(self, path: Path):
        return [json.loads(line) for line in self.read(path).splitlines() if line.strip()]


def paired_spans(rows: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get('event_kind') != 'tool_intent':
            groups[row['span_id']].append(row)
    result = []
    for span_id, group in groups.items():
        starts = [r for r in group if r['terminal'] is False]
        ends = [r for r in group if r['terminal'] is True]
        require(len(starts) == len(ends) == 1, f'unpaired/duplicate span: {span_id}')
        start, end = starts[0], ends[0]
        require(start['clock'] == end['clock'], f'clock mismatch: {span_id}')
        require(start['start_mono_ns'] == end['start_mono_ns'], f'start mismatch: {span_id}')
        require(end['end_mono_ns'] >= end['start_mono_ns'], f'negative span: {span_id}')
        require(abs(end['duration_ms'] - (end['end_mono_ns'] - end['start_mono_ns']) / MS) < 1e-6,
                f'duration mismatch: {span_id}')
        result.append({**end, 'start_event_id': start['event_id']})
    return sorted(result, key=lambda r: r['start_mono_ns'])


def binary_counts(raw: bytes, record_size: int) -> dict[int, dict]:
    require(record_size == 352 and len(raw) % record_size == 0, 'unexpected binary ABI/length')
    result: dict[int, dict] = {}
    for offset in range(0, len(raw), record_size):
        token, sequence, start, end = struct.unpack_from('<4Q', raw, offset)
        require(end >= start, 'negative native event interval')
        row = result.setdefault(token, {'records': 0, 'kernel_start_ns': start,
                                        'kernel_end_ns': end, 'sequences': set()})
        require(sequence not in row['sequences'], 'duplicate native token/sequence')
        row['sequences'].add(sequence)
        row['records'] += 1
        row['kernel_start_ns'] = min(start, row['kernel_start_ns'])
        row['kernel_end_ns'] = max(end, row['kernel_end_ns'])
    return {k: {f: v for f, v in r.items() if f != 'sequences'} for k, r in result.items()}


def wall_iso_ns(value: str) -> int:
    # UTC strings have millisecond resolution; only used for labeled approximate splits.
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000) * MS


def analyze_condition(inputs: Inputs, condition: dict, timeline: list[dict]) -> dict:
    root = Path(condition['output_dir'])
    result = inputs.json(root / 'replay_result.json', condition['result_sha256'])
    require(result == condition['result'], 'embedded replay result differs')
    require(condition['valid'] and condition['returncode'] == 0, 'condition is not a completed valid run')
    actions = inputs.rows(root / 'runtime_actions.jsonl')
    for action in actions:
        require(sha(action['command'].encode()) == action['command_sha256'], 'command hash mismatch')
        require(action['callback_error'] is None and action['exception'] is None, 'failed action/callback')
        require(action['recorded_at_wall_ns'] >= action['started_at_wall_ns'], 'negative wall call')
    calls = [{'label': a['label'], 'command': a['command'], 'command_sha256': a['command_sha256'],
              'action_id': a['action_id'], 'pre_event_id': a['pre_event_id'],
              'driver_call_realtime_ms': (a['recorded_at_wall_ns'] - a['started_at_wall_ns']) / MS}
             for a in actions]
    for a in actions:
        timeline.append({'condition': str(root), 'clock_id': 'CLOCK_REALTIME',
                         'kind': 'driver_call_plus_terminal_callbacks', 'label': a['label'],
                         'start_ns': a['started_at_wall_ns'], 'end_ns': a['recorded_at_wall_ns'],
                         'duration_ms': (a['recorded_at_wall_ns']-a['started_at_wall_ns'])/MS,
                         'command': a['command'], 'start_event_id': a['pre_event_id'],
                         'source': 'runtime_actions.jsonl'})
    metrics = {'work_ms': result['work_wall_ms'], 'startup_ms': result['startup_wall_ms'],
               'adapter_total_ms': condition['adapter_total_wall_ms'],
               'driver_calls_realtime_ms': sum(c['driver_call_realtime_ms'] for c in calls)}
    output = {'root': str(root), 'mode': result['instrumentation_mode'], 'metrics': metrics,
              'calls': calls, 'action_count': len(actions), 'capture': result['capture']}
    if output['mode'] == 'instrument_off':
        output['limitations'] = ['No hook/teardown timers in control; work minus driver calls is a mixed residual.']
        return output

    lifecycle = inputs.rows(root / 'telemetry/lifecycle_events.jsonl')
    tools = inputs.rows(root / 'telemetry/tool_events.jsonl')
    spans = paired_spans(lifecycle + tools)
    clocks = {(s['clock']['clock_id'], s['clock']['boot_id']) for s in spans}
    require(len(clocks) == 1, 'multiple telemetry clock identities')
    clock_id, boot_id = next(iter(clocks))
    require(clock_id == 'CLOCK_MONOTONIC_RAW', 'this extraction expects recorded RAW spans')
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for s in spans:
        by_kind[s['event_kind']].append(s)
        timeline.append({'condition': str(root), 'clock_id': clock_id, 'boot_id': boot_id,
                         'kind': s['event_kind'], 'start_ns': s['start_mono_ns'],
                         'end_ns': s['end_mono_ns'], 'duration_ms': s['duration_ms'],
                         'start_event_id': s['start_event_id'], 'end_event_id': s['event_id'],
                         'parent_event_id': s['parent_event_id'], 'command': s.get('runtime_command'),
                         'source': 'telemetry/tool_events.jsonl' if s['event_kind'] == 'tool_event'
                                   else 'telemetry/lifecycle_events.jsonl'})
        if s.get('runtime_command_start_mono_ns') is not None:
            a, b = s['runtime_command_start_mono_ns'], s['runtime_command_end_mono_ns']
            timeline.append({'condition': str(root), 'clock_id': clock_id, 'boot_id': boot_id,
                             'kind': 'runtime_wrapper', 'start_ns': a, 'end_ns': b,
                             'duration_ms': (b-a)/MS, 'start_event_id': s['start_event_id'],
                             'end_event_id': s['event_id'], 'command': s.get('runtime_command'),
                             'source': 'telemetry runtime_command_* fields'})
    require(len(by_kind['tool_event']) == len(actions), 'tool action count mismatch')
    by_pre = {s['start_event_id']: s for s in spans}
    for call in calls:
        s = by_pre[call['pre_event_id']]
        require(s['event_kind'] == 'tool_event' and s['action_id'] == call['action_id'], 'action ID join mismatch')
        require(s['runtime_command'] == call['command'], 'runtime command join mismatch')
        a, b = s['runtime_command_start_mono_ns'], s['runtime_command_end_mono_ns']
        require(s['start_mono_ns'] <= a <= b <= s['end_mono_ns'], 'runtime escapes tool span')
        call.update({'tool_span_raw_ms': s['duration_ms'], 'runtime_wrapper_raw_ms': (b-a)/MS,
                     'before_runtime_raw_ms': (a-s['start_mono_ns'])/MS,
                     'after_runtime_raw_ms': (s['end_mono_ns']-b)/MS,
                     'terminal_event_id': s['event_id']})
        prestate = [q for q in by_kind['script_read'] if q['parent_event_id'] == s['parent_event_id']]
        require(len(prestate) <= 1, 'ambiguous action prestate query')
        call['script_query_before_ms'] = prestate[0]['duration_ms'] if prestate else 0.0
        call['script_query_pre_event_id'] = prestate[0]['start_event_id'] if prestate else None

    queries = by_kind['script_read']
    metrics.update({'script_queries_ms': sum(s['duration_ms'] for s in queries),
                    'pwd_runtime_wrapper_ms': sum((s['runtime_command_end_mono_ns']-s['runtime_command_start_mono_ns'])/MS for s in queries),
                    'tool_spans_ms': sum(c['tool_span_raw_ms'] for c in calls),
                    'tool_before_runtime_ms': sum(c['before_runtime_raw_ms'] for c in calls),
                    'tool_after_runtime_ms': sum(c['after_runtime_raw_ms'] for c in calls),
                    'client_processing_ms_including_queries': sum(s['duration_ms'] for s in by_kind['client_processing']),
                    'teardown_ms': by_kind['teardown'][0]['duration_ms'],
                    'pid_discovery_startup_ms': by_kind['persistent_shell_pid_discovery'][0]['duration_ms']})
    journal = inputs.rows(root / 'linux_work/action_boundaries.jsonl')
    aggregates = inputs.rows(root / 'linux_work/raw_aggregates.jsonl')
    summary = inputs.json(root / 'linux_work/work_summary.json')
    manifest = inputs.json(root / 'linux_work/bpf_collector_manifest.json')
    inputs.read(root / 'linux_work/native_bpf_sink.c', manifest['native_sink']['source_sha256'])
    service = inputs.json(root / 'linux_work/service_lifecycle.json')
    require(service['status'] == 'stopped' and service['service_returncode'] == 0, 'collector not stopped successfully')
    stream = summary['raw_event_stream']
    raw = inputs.read(root / 'linux_work/raw_events.bin', stream['sha256'])
    packets = binary_counts(raw, stream['record_size_bytes'])
    require(sum(p['records'] for p in packets.values()) == stream['records_written'], 'native count mismatch')
    require(stream['records_written'] == result['capture']['individual_cpu_operation_records'], 'capture count mismatch')
    require(len(aggregates) == len(actions)+len(queries), 'unexpected collector action count')
    require(len(journal) == 2*len(aggregates), 'unexpected boundary count')
    require(not summary['action_finalizations'], 'deferred finalizations need a different decomposition')
    output['bpf_actions'] = []
    resource_ms = 0.0
    for agg in aggregates:
        require(agg['event_records_complete'] and not agg['event_callback_errors'] and agg['perf_lost_events'] == 0,
                'incomplete native capture')
        require(not agg['censored_pending'] and not agg['deferred_quiescence'], 'pending native events')
        boundary = agg['boundary']
        s = by_pre[boundary['event_id']]
        require(boundary['command'] == s['runtime_command'], 'BPF to hook command mismatch')
        native = packets[agg['action_token']]
        require(native['records'] == agg['event_count'] == agg['required_event_count'], 'token record count mismatch')
        output['bpf_actions'].append({'pre_event_id': boundary['event_id'], 'command': boundary['command'],
                                     'action_token': agg['action_token'], **native})
        timeline.append({'condition': str(root), 'clock_id': 'CLOCK_MONOTONIC', 'boot_id': boot_id,
                         'kind': 'selected_native_events_envelope_not_cpu_time',
                         'start_ns': native['kernel_start_ns'], 'end_ns': native['kernel_end_ns'],
                         'duration_ms': (native['kernel_end_ns']-native['kernel_start_ns'])/MS,
                         'start_event_id': boundary['event_id'], 'action_token': agg['action_token'],
                         'command': boundary['command'], 'source': 'linux_work/raw_events.bin'})
        for which in ['start', 'end']:
            snapshot = boundary[which+'_snapshot']
            resource = snapshot['container_resources']
            require(resource['status'] == 'measured' and resource['boot_id'] == boot_id, 'resource identity mismatch')
            duration = (resource['ended_monotonic_ns']-resource['started_monotonic_ns'])/MS
            require(duration >= 0, 'negative resource bracket')
            resource_ms += duration
    freeze = summary['capture_stop']
    require(freeze['clock_id'] == 'CLOCK_MONOTONIC', 'unexpected detach clock')
    metrics['tracepoint_detach_ms'] = (freeze['detach_completed_ns']-freeze['detach_started_ns'])/MS
    metrics['container_resource_snapshots_ms'] = resource_ms
    metrics['bpf_startup_ms_outside_work'] = manifest['startup_wall_ms']
    teardown = by_kind['teardown'][0]
    metrics['teardown_to_closed_manifest_wall_ms_approx'] = (manifest['closed_wall_ns']-wall_iso_ns(teardown['started_at_utc']))/MS
    metrics['closed_manifest_to_teardown_end_wall_ms_approx'] = (wall_iso_ns(teardown['ended_at_utc'])-manifest['closed_wall_ns'])/MS
    timeline.append({'condition': str(root), 'clock_id': 'CLOCK_MONOTONIC', 'boot_id': boot_id,
                     'kind': 'tracepoint_detach', 'start_ns': freeze['detach_started_ns'],
                     'end_ns': freeze['detach_completed_ns'], 'duration_ms': metrics['tracepoint_detach_ms'],
                     'source': 'linux_work/work_summary.json:capture_stop'})
    counts = {'fixture_actions': len(actions), 'script_query_spans': len(queries),
              'observed_tool_intents': sum(r['event_kind']=='tool_intent' for r in tools),
              'paired_tool_spans': len(by_kind['tool_event']), 'paired_lifecycle_spans': len(spans)-len(actions),
              'paired_client_spans': len(by_kind['client_processing']),
              'v2_journal_records': len(lifecycle)+len(tools),
              'bpf_boundary_records': len(journal), 'bpf_aggregate_records': len(aggregates),
              'accepted_native_sample_callbacks_inferred_from_packets': stream['records_written'],
              'driver_hook_calls_source_inferred': 5*len(actions),
              'work_v2_journal_records': sum((r['end_mono_ns'] if r['terminal'] else r['start_mono_ns'])
                                            > by_kind['setup'][0]['end_mono_ns'] for r in lifecycle+tools),
              'deferred_action_finalizations': 0, 'drops': result['capture']['dropped_cpu_records']}
    require(counts['observed_tool_intents'] == len(actions) and counts['paired_client_spans'] == 2*len(actions),
            'callback journal cardinality mismatch')
    counts['v2_fsync_calls_source_inferred'] = counts['v2_journal_records']
    counts['work_v2_fsync_calls_source_inferred'] = counts['work_v2_journal_records']
    counts['native_action_durability_syncs_source_inferred'] = len(aggregates)
    output['counts'] = counts
    output['observed_span_counts'] = dict(Counter(s['event_kind'] for s in spans))
    output['clock'] = {'clock_id': clock_id, 'boot_id': boot_id}
    output['limitations'] = [
        'RAW, kernel MONOTONIC, driver REALTIME and perf_counter durations remain separately labeled; no cross-clock absolute subtraction.',
        'Teardown wall split uses millisecond UTC strings and is approximate; manifest timestamp precedes its write and service exit.',
        'Runtime wrapper includes collector work where the runtime hook starts collection, notably pwd; it is not pure shell execution.',
        'Per-fsync/native-flush/poll-wakeup/function-callback durations are absent; record counts are not duration measurements.',
        'Resource snapshots are nested inside existing boundaries and must not be added again to enclosing durations.',
    ]
    return output


def analyze(handoff_path: Path, source_snapshot: Path) -> tuple[dict, list[dict], dict[str, bytes]]:
    inputs = Inputs()
    handoff = inputs.json(handoff_path)
    evidence = inputs.json(Path(handoff['cpu_subset_evidence']), handoff['cpu_subset_evidence_sha256'])
    require(evidence['condition_count'] == 12 and evidence['valid_pair_count'] == 6, 'expected existing 12-condition CPU subset')
    manifest_path = Path(handoff['fixture_manifest'])
    inputs.json(manifest_path, handoff['fixture_manifest_sha256'])
    pins = inputs.json(manifest_path.parent/'source/runtime_source_hashes.json')
    sources = {}
    for original, digest in pins.items():
        parts = Path(original).parts
        index = parts.index('agentic-submission-repairs-20260908')
        relative = str(Path(*parts[index+1:]))
        sources[relative] = inputs.read(source_snapshot/relative, digest)
    timeline, pairs = [], []
    for pair in evidence['pairs']:
        require(pair['valid'], 'invalid pair')
        off = analyze_condition(inputs, pair['control'], timeline)
        on = analyze_condition(inputs, pair['treatment'], timeline)
        require([r['command_sha256'] for r in off['calls']] == [r['command_sha256'] for r in on['calls']], 'paired command order differs')
        delta = on['metrics']['work_ms']-off['metrics']['work_ms']
        driver_delta = on['metrics']['driver_calls_realtime_ms']-off['metrics']['driver_calls_realtime_ms']
        comparisons = [{'label': a['label'], 'command': a['command'], 'command_sha256': a['command_sha256'],
                        'off_driver_realtime_ms': a['driver_call_realtime_ms'],
                        'on_driver_realtime_ms': b['driver_call_realtime_ms'],
                        'driver_realtime_delta_ms': b['driver_call_realtime_ms']-a['driver_call_realtime_ms'],
                        'on_pre_event_id': b['pre_event_id'], 'on_terminal_event_id': b['terminal_event_id'],
                        'on_runtime_wrapper_raw_ms': b['runtime_wrapper_raw_ms'],
                        'on_before_runtime_raw_ms': b['before_runtime_raw_ms'],
                        'on_after_runtime_raw_ms': b['after_runtime_raw_ms'],
                        'on_script_query_before_ms': b['script_query_before_ms'],
                        'on_script_query_pre_event_id': b['script_query_pre_event_id']}
                       for a,b in zip(off['calls'], on['calls'])]
        pairs.append({'case_id': pair['case_id'], 'repeat': pair['repeat'], 'order': pair['order'],
                      'control': off, 'treatment': on, 'work_delta_ms': delta,
                      'relative_overhead_percent': delta/off['metrics']['work_ms']*100,
                      'driver_realtime_delta_ms': driver_delta,
                      'residual_delta_after_queries_detach_and_driver_ms_approx': delta-driver_delta-on['metrics']['script_queries_ms']-on['metrics']['tracepoint_detach_ms'],
                      'calls': comparisons})
    summaries = {}
    for case_id in sorted({p['case_id'] for p in pairs}):
        selected = [p for p in pairs if p['case_id'] == case_id]
        summaries[case_id] = {
            'pairs': len(selected), 'actions_per_condition': selected[0]['control']['action_count'],
            'control_medians_ms': {k: statistics.median(p['control']['metrics'][k] for p in selected) for k in selected[0]['control']['metrics']},
            'treatment_medians_ms': {k: statistics.median(p['treatment']['metrics'][k] for p in selected) for k in selected[0]['treatment']['metrics']},
            'paired_medians': {k: statistics.median(p[k] for p in selected) for k in ['work_delta_ms','relative_overhead_percent','driver_realtime_delta_ms','residual_delta_after_queries_detach_and_driver_ms_approx']},
            'counts_per_treatment': [p['treatment']['counts'] for p in selected],
        }
    # Refuse a result if any consumed evidence/source changed during extraction.
    for path, ref in list(inputs.hashes.items()):
        inputs.read(Path(path), ref['sha256'])
    return {'schema_version': 'assignment.cpu-capture-overhead-extraction.v1',
            'scope': 'offline extraction of existing six CPU pairs; no execution or methodology change',
            'source_handoff': str(handoff_path), 'input_hashes': inputs.hashes,
            'condition_count': 12, 'pair_count': 6, 'cases': summaries, 'pairs': pairs,
            'no_new_workloads_executed': True, 'source_hashes_verified': True}, timeline, sources


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--handoff', type=Path, required=True)
    parser.add_argument('--source-snapshot', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    require(not args.output_dir.exists(), 'refusing existing output directory')
    report, timeline, sources = analyze(args.handoff, args.source_snapshot)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    (args.output_dir/'analysis.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
    (args.output_dir/'timeline.jsonl').write_text(''.join(json.dumps(r,sort_keys=True)+'\n' for r in timeline))
    calls = [{**{'case_id': p['case_id'], 'repeat': p['repeat'], 'order': p['order']}, **c}
             for p in report['pairs'] for c in p['calls']]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(calls[0])); writer.writeheader(); writer.writerows(calls)
    (args.output_dir/'paired_calls.csv').write_text(stream.getvalue())
    for relative, raw in sources.items():
        path = args.output_dir/'captured_source'/relative
        path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(raw)
    print(json.dumps({'output_dir': str(args.output_dir), 'analysis_sha256': sha((args.output_dir/'analysis.json').read_bytes()),
                      'cases': report['cases']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
