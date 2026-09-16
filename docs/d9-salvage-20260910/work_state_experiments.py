"""Bounded retained-only experiments. No acquisition or default-model mutation."""
import argparse
import hashlib
import importlib.util
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

HERE = Path(__file__).resolve().parent
OUT = HERE / 'work_state_experiments'


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''): h.update(block)
    return h.hexdigest()


def save(name, data):
    OUT.mkdir(exist_ok=True)
    (OUT / (name + '.json')).write_text(json.dumps(data, indent=2) + '\n')


def bucket(value):
    return 'unknown' if value is None else str(int(math.log2(max(1, value))))


def fit_table(rows, field, minimum=25, instances=3, center=median):
    groups = defaultdict(list)
    for r in rows: groups[r[field]].append(r)
    return {k: float(center([r['observed_ms'] for r in v])) for k, v in groups.items()
            if len(v) >= minimum and len({r['instance_id'] for r in v}) >= instances}


def table_predict(models, row, candidate):
    """Only explicit input keys participate; targets are never consulted."""
    parent='base_gate' if candidate.endswith('_gate') else 'base'
    for key in ([candidate, parent] if candidate != parent else [parent]):
        if row[key] in models[key]: return models[key][row[key]]
    return models['fallback']


def preceding_request(proxies, start):
    available = [r for r in proxies if r['case_id']==start['case_id'] and r['attempt_id']==start['attempt_id']
                 and r['clock']==start['clock'] and r['end_mono_ns']<=start['start_mono_ns']]
    return max(available,key=lambda r:r['end_mono_ns'],default=None)


def metrics(rows, candidate):
    errors = [abs(r['predictions_ms'][candidate] - r['observed_ms']) / r['observed_ms'] for r in rows]
    grouped = defaultdict(list)
    for r, e in zip(rows, errors): grouped[r['instance_id']].append(e <= .25)
    return dict(events=len(rows), within25_percent=100*mean(e <= .25 for e in errors),
                worst_error_percent=100*max(errors), equal_instance_percent=100*mean(mean(v) for v in grouped.values()),
                all_event_instances=sum(all(v) for v in grouped.values()))


def client():
    scan = module('work_scan', HERE/'scan_remaining_20260914.py')
    groups = scan.load_groups()  # Existing partition/hash/clock/identity gate.
    rows = next(v for k, v in groups.items() if k[0] == 'lifecycle:client_processing')
    sources, inputs, starts, counts = [], {}, {}, Counter()
    for case in json.loads((HERE/'cpu_lifecycle/manifest.json').read_text())['cases']:
        directory = Path(case['case_root'])/'runner_attempts/attempt-001/telemetry_v2'
        for target in map(json.loads,Path(case['events_path']).open()):
            if target['event_class']=='lifecycle:client_processing':
                starts[(case['case_id'],target['event_id'])]=target['pre_event_id']
        path = directory/'model_events.jsonl'
        validation = json.loads(Path(case['validation_report_path']).read_text())
        expected = [s['sha256'] for s in validation['source_hashes'] if s['path'].endswith('/model_events.jsonl')]
        assert expected == [sha(path)]
        sources.append({'path':str(path), 'sha256':sha(path)})
        requests = [json.loads(x) for x in path.open()]
        proxies = [r for r in requests if r['event_kind'] == 'model_request' and r.get('end_mono_ns')]
        lifecycle = [json.loads(x) for x in (directory/'lifecycle_events.jsonl').open()]
        for start in lifecycle:
            if start['event_kind'] != 'client_processing_start': continue
            latest = preceding_request(proxies,start)
            sizes = {'request':None, 'response':None}
            if latest:
                for kind in sizes:
                    artifact = latest.get('request_payload_artifact', {}).get(kind, {})
                    if artifact.get('complete'):
                        payload = directory/artifact['artifact_path']
                        assert payload.stat().st_size == artifact['bytes'] and sha(payload) == artifact['sha256']
                        sizes[kind] = artifact['bytes']
                counts['joined_prior_request'] += 1
            else: counts['no_prior_completed_request'] += 1
            inputs[(case['case_id'], start['event_id'])] = sizes
    for row in rows:
        sizes = inputs[(row['case_id'], starts[(row['case_id'],row['event_id'])])]
        row['base'] = json.dumps(row['extra_features'], sort_keys=True)
        row['response_size'] = row['base'] + '|' + bucket(sizes['response'])
        row['history_size'] = row['base'] + '|' + bucket(sizes['request'])
        for field in ('base','response_size','history_size'): row[field+'_gate']=row[field]
    predictions = []
    from agentic_sim.assignment.semantic_cpu_model import _gate_center
    candidates = ('base', 'response_size', 'history_size','base_gate','response_size_gate','history_size_gate')
    for fold in range(5):
        train = [r for r in rows if scan.m.cal._fold(r['instance_id']) != fold]
        models = {k:fit_table(train, k,center=_gate_center if k.endswith('_gate') else median) for k in candidates}
        models['fallback'] = median(r['observed_ms'] for r in train)
        for r in rows:
            if scan.m.cal._fold(r['instance_id']) != fold: continue
            predictions.append({k:r[k] for k in ('case_id','instance_id','event_id','observed_ms')} |
                               {'fold':fold, 'predictions_ms':{c:table_predict(models,r,c) for c in candidates}})
    report = {'metrics':{c:metrics(predictions,c) for c in candidates}, 'joins':dict(counts), 'sources':sources,
              'source_sha256':sha(__file__), 'manifest_sha256':sha(HERE/'cpu_lifecycle/manifest.json'),
              'contract':'Latest fully recorded preceding response/request bytes; same case/attempt/clock, end before client start. Prior request size is a history-size proxy, not current history size. Five original instance folds. No elapsed-time features.',
              'promoted':False}
    report['paired_gain'] = {c:scan.m.paired_gain([{**r,'predictions_ms':{'coarse':r['predictions_ms']['base'],'semantic':r['predictions_ms'][c]}} for r in predictions]) for c in candidates[1:]}
    report['paired_gain_vs_gate'] = {c:scan.m.paired_gain([{**r,'predictions_ms':{'coarse':r['predictions_ms']['base_gate'],'semantic':r['predictions_ms'][c]}} for r in predictions]) for c in ('response_size_gate','history_size_gate')}
    save('client',report)
    save('client_fits', {c:fit_table(rows,c,center=_gate_center if c.endswith('_gate') else median) for c in candidates} | {'fallback':median(r['observed_ms'] for r in rows)})
    OUT.joinpath('client_predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in predictions))
    # Explicitly preserve the already evaluated runtime tradeoff, no default change.
    life = module('work_lifecycle',HERE/'refine_lifecycle_20260914.py')
    runtime = next(v for k,v in groups.items() if k[0]=='runtime_command')
    save('runtime_alternatives', {'models':{c:life.fit(runtime,c) for c in ('descriptor_median','descriptor_gate')},
         'evaluation_report_sha256':sha(HERE/'lifecycle_refinement/report.json'), 'default':'descriptor_median',
         'contract':'Explicit opt-in gate-center tradeoff; same hardware/start-known descriptor contract; not a D9 pass.'})
    print(json.dumps(report['metrics']), flush=True)


def cpu():
    atomic = module('work_atomic', HERE/'atomic_cpu/run_atomic_cpu.py')
    flags = module('work_flags', HERE/'cpu_entry_flags/compare.py')
    from agentic_sim.telemetry.bpf_work import _CWorkEvent, BpfWorkCollector, BPF_EVENT_SCHEMA
    manifest = json.loads(atomic.CPU_EVIDENCE_MANIFEST.read_text())
    cases = [atomic.prepare_case(c) for c in atomic.select_cases(manifest,8)]
    assert sum(c['raw_bytes'] for c in cases) < 2_000_000_000
    rows, sources = [], []
    censored = Counter()
    for case in cases:
        assert not any(case['aggregate_drops'].values())
        assert case['range_gap_bytes'] == case['range_overlap_bytes'] == 0
        assert all(r['required_event_count']==r['record_count']==r['event_count']==r['event_count_at_boundary']
                   and r['post_boundary_event_count']==0 and r['event_records_complete'] for r in case['action_ranges'])
        digest, offset, cursor, tokens = hashlib.sha256(), 0, 0, Counter()
        with Path(case['raw_path']).open('rb') as stream:
            for chunk in iter(lambda:stream.read(400*4096), b''):
                digest.update(chunk)
                assert len(chunk)%400 == 0
                for i in range(0,len(chunk),400):
                    packet = chunk[i:i+400]
                    event = _CWorkEvent.from_buffer_copy(packet)
                    while cursor<len(case['action_ranges']) and offset+i>=case['action_ranges'][cursor]['offset_end']: cursor+=1
                    atomic._validate_event_membership(case['action_ranges'][cursor],raw_offset=offset+i,action_token=int(event.token))
                    tokens[int(event.token)] += 1
                    if event.syscall_nr not in (9,17,18,257): continue
                    decoded = BpfWorkCollector._event_row(packet,schema_version=BPF_EVENT_SCHEMA)
                    y = decoded.get('duration_ns')
                    if y is None or y<=0:
                        censored[str(event.syscall_nr)] += 1
                        continue
                    args = decoded['scalar_args']
                    op = str(event.syscall_nr)
                    if op=='257':
                        baseline = op+'|'+atomic.path_class(decoded)
                        extra = (args['open_flags'],flags.path_group(decoded))
                    elif op=='9':
                        baseline = op+'|'+bucket(args['length'])
                        extra = (args['prot'],args['flags'],bucket(args['offset']))
                    else:
                        baseline = op+'|'+bucket(args['requested_size'])
                        extra = (bucket(args['offset']), args['offset']%4096==0)
                    rows.append(dict(instance_id=case['instance_id'],case_id=case['case_id'],op=op,base=baseline,
                                     entry=baseline+'|'+json.dumps(extra),observed_ms=y/1e6,raw_offset=offset+i,
                                     token=int(event.token),sequence=int(event.sequence)))
                offset += len(chunk)
        assert digest.hexdigest()==case['raw_hash_recorded'] and offset==case['raw_bytes']
        assert dict(tokens)=={int(k):v for k,v in case['token_expected_records'].items()}
        sources.append({'case_id':case['case_id'],'instance_id':case['instance_id'],'path':case['raw_path'],'sha256':digest.hexdigest(),'bytes':offset})
        print('decoded',case['instance_id'], 'selected events',len(rows),flush=True)
    # Censored targets are reported, never silently removed from a compliance claim.
    results, fits = {}, {}
    for op in sorted({r['op'] for r in rows}):
        group = [r for r in rows if r['op']==op]
        predictions = []
        for held in sorted({r['instance_id'] for r in group}):
            train = [r for r in group if r['instance_id']!=held]
            models = {c:fit_table(train,c,20,2) for c in ('base','entry')}
            models['fallback'] = median(r['observed_ms'] for r in train)
            for r in group:
                if r['instance_id']==held:
                    predictions.append(r | {'predictions_ms':{c:table_predict(models,r,c) for c in ('base','entry')}})
        results[op] = {c:metrics(predictions,c) for c in ('base','entry')}
        results[op]['worst_examples']={c:max(predictions,key=lambda r:abs(r['predictions_ms'][c]-r['observed_ms'])/r['observed_ms']) for c in ('base','entry')}
        results[op]['per_instance'] = {i:{c:metrics([r for r in predictions if r['instance_id']==i],c) for c in ('base','entry')} for i in sorted({r['instance_id'] for r in group})}
        fits[op] = {c:fit_table(group,c,20,2) for c in ('base','entry')} | {'fallback':median(r['observed_ms'] for r in group)}
    save('cpu',dict(results=results,sources=sources,censored_or_nonpositive=dict(censored),source_sha256=sha(__file__),
                   manifest_sha256=sha(atomic.CPU_EVIDENCE_MANIFEST),promoted=False,
                   contract='First eight distinct valid ordinal training instances, full raw hash/range/token checks; leave-one-instance-out. Only openat,mmap,pread64,pwrite64 positive durations, not all CPU operations. Start-entry arguments only.'))
    save('cpu_fits',fits)
    print(json.dumps({k:{c:v for c,v in x.items() if c!='per_instance'} for k,x in results.items()}),flush=True)


def simulated_queue(arrivals, service_ms, dispatch_ms):
    """Single-server hypothesis: advance only with predicted service, never labels."""
    if not (len(arrivals)==len(service_ms)==len(dispatch_ms)):
        raise ValueError('length mismatch')
    if any(not math.isfinite(x) or x<0 for x in arrivals+service_ms+dispatch_ms):
        raise ValueError('invalid queue input')
    if arrivals!=sorted(arrivals): raise ValueError('arrivals must be ordered')
    end=arrivals[0] if arrivals else 0.
    result=[]
    for start,service,dispatch in zip(arrivals,service_ms,dispatch_ms):
        wait=max(0.,end-start)+dispatch
        result.append(wait)
        end=start+wait+service
    return result


def queue():
    refinement=module('work_native',HERE/'native_refinement/compare_phases.py')
    native=refinement.native
    rows,manifest,hashes=native.load_dataset(native.DEFAULT_DATASET,native.DEFAULT_MANIFEST)
    targets={(r['case_id'],r['physical_request_id']):r for r in rows if r['native_component']=='queue'}
    joined,checks,sources=[],[],[]
    for case in manifest['cases']:
        source=case['source']['native_attribution']
        assert sha(source['path'])==source['sha256']
        attrs=[json.loads(l) for l in open(source['path'])]
        attrs=[a for a in attrs if (case['case_id'],a.get('physical_request_id')) in targets]
        if not attrs: continue
        assert len({a['physical_request_id'] for a in attrs})==len(attrs)
        domains={json.dumps([a['server_identity'],a['counter_epoch'],a['clock']],sort_keys=True) for a in attrs}
        assert len(domains)==1
        peers={}
        journals={a['native_journal']['path']:a['native_journal']['sha256'] for a in attrs}
        for path,expected in journals.items():
            assert sha(path)==expected
            sources.append({'path':path,'sha256':expected})
            for l in open(path):
                record=json.loads(l)
                if record.get('record_type')!='native_finished': continue
                raw=record['raw']
                state=raw['request_state']
                if state.get('queued_ts') is None or state.get('last_token_ts') is None: continue
                peer=(state['queued_ts'],state['last_token_ts'])
                identity=raw['engine_request_id']
                if identity in peers: assert peers[identity]==peer
                peers[identity]=peer
        ids={a['native_measurement']['engine_request_id'] for a in attrs}
        first=min(a['native_measurement']['request_state']['queued_ts'] for a in attrs)
        last=max(a['native_measurement']['request_state']['last_token_ts'] for a in attrs)
        # Measured end times are used ONLY to audit the traffic boundary, never
        # in the simulated queue or in any fitted predictor.
        outside=[i for i,(s,e) in peers.items() if i not in ids and s<=last and e>=first]
        overlap=0
        for a in attrs:
            state=a['native_measurement']['request_state']
            start=state['queued_ts']; identity=a['native_measurement']['engine_request_id']
            assert peers[identity]==(start,state['last_token_ts'])
            overlap+=any(i!=identity and s<=start<e for i,(s,e) in peers.items())
            r=targets[(case['case_id'],a['physical_request_id'])]
            assert r['host_id']==a['clock']['hostname']
            assert r['clock_id']=='CLOCK_MONOTONIC_RAW|boot='+a['clock']['boot_id']
            joined.append(r|{'arrival_ms':start*1000,'external_traffic':bool(outside)})
        checks.append({'case_id':case['case_id'],'requests':len(attrs),'arrivals_with_observed_peer_active':overlap,
                       'external_overlapping_finished_requests':len(outside)})
    assert len(joined)==len(targets)
    predictions=[]
    for fold in range(5):
        train=[r for r in rows if native.fold_for_instance(r['instance_id'])!=fold]
        models={phase:refinement.fit([r for r in train if r['native_component']==phase],phase,
                                    'prefill_attention' if phase=='prefill' else 'pooled') for phase in ('queue','prefill','decode')}
        test=[r for r in joined if native.fold_for_instance(r['instance_id'])==fold]
        for case_id in sorted({r['case_id'] for r in test}):
            group=sorted([r for r in test if r['case_id']==case_id],key=lambda r:r['arrival_ms'])
            if group[0]['external_traffic']: continue
            inputs=[{k:r[k] for k in ('prompt_tokens','completion_tokens','cached_tokens')} for r in group]
            baseline=[refinement.predict(models['queue'],r) for r in inputs]
            service=[refinement.predict(models['prefill'],r)+refinement.predict(models['decode'],r) for r in inputs]
            modeled=simulated_queue([r['arrival_ms'] for r in group],service,baseline)
            for r,p,b in zip(group,modeled,baseline):
                predictions.append({k:r[k] for k in ('case_id','instance_id','event_id','observed_ms')}|
                                   {'fold':fold,'predictions_ms':{'base':b,'simulated_backlog':p}})
    report={'checks':checks,'total_requests':len(targets),'evaluated_requests':len(predictions),
            'metrics':{c:metrics(predictions,c) for c in ('base','simulated_backlog')} if predictions else {},
            'source_hashes':hashes,'native_journals':sources,'source_sha256':sha(__file__),'promoted':False,
            'contract':'Conditional recorded engine arrival schedule and supplied token/cache work, fold-trained predicted service only. Single-server backlog hypothesis, not asserted vLLM continuous-batching architecture. Measured peer completions only audit traffic boundary. Native finished journals do not by themselves prove absence of unfinished external traffic; diagnostic, not production acceptance.'}
    save('queue',report)
    OUT.joinpath('queue_predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in predictions))
    print(json.dumps({k:v for k,v in report.items() if k not in ('checks','native_journals','source_hashes')}),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('experiment',choices=['client','cpu','queue'])
    args=parser.parse_args()
    globals()[args.experiment]()
