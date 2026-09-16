"""Bounded lifecycle descriptor scan; does not promote or change acquisition."""
import json
import importlib.util
from pathlib import Path
from collections import defaultdict

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('scan_semantic', HERE/'semantic_repaired/compare.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def load_groups():
    partitions,_ = m.cal._load_pinned_partitions()
    cases = m.read(HERE/'cpu_lifecycle/manifest.json')['cases']
    groups = defaultdict(list)
    for case in cases:
        root = Path(case['case_root'])
        instance,cid,_ = m.cal._case_spec_identity(root)
        assert partitions[instance]=='train_calibration' and cid==case['case_id'] and instance==case['instance_id']
        assert m.sha(case['events_path'])==case['events_sha256']
        assert m.sha(case['validation_report_path'])==case['validation_report_sha256']
        report = m.read(case['validation_report_path'])
        assert report['validation']['status']=='valid'
        path = root/'runner_attempts/attempt-001/telemetry_v2/lifecycle_events.jsonl'
        expected = [s['sha256'] for s in report['source_hashes'] if s['path'].endswith('/lifecycle_events.jsonl')]
        assert expected==[m.sha(path)]
        raw = m.indexed(path)
        # Journal order is causal recording order; require the previous record's
        # timestamp to precede the current start in the same host/boot clock.
        before = {}
        before_two = {}
        previous = None
        penultimate = None
        for start in raw.values():
            if start.get('event_kind')=='client_processing_start':
                if previous and previous.get('clock')==start.get('clock'):
                    timestamp=previous.get('end_mono_ns') or previous.get('start_mono_ns')
                    if timestamp is not None and timestamp<=start['start_mono_ns']:
                        before[start['event_id']]=previous['event_kind']
                        if penultimate and penultimate.get('clock')==start.get('clock'):
                            time2=penultimate.get('end_mono_ns') or penultimate.get('start_mono_ns')
                            if time2 is not None and time2<=timestamp:
                                before_two[start['event_id']]=penultimate['event_kind']
            penultimate=previous
            previous=start
        admitted,_=m.cal._read_eligible_events(Path(case['events_path']),instance,cid)
        normalized={r['event_id']:r for r in admitted}
        for target in map(json.loads,Path(case['events_path']).open()):
            klass=target['event_class']
            if klass not in ('runtime_command','lifecycle:client_processing'):continue
            start=raw[target['pre_event_id']]
            assert all(start[k]==target[k] for k in ('case_id','attempt_id','start_mono_ns'))
            assert start['clock']['hostname']==target['host_id']
            assert start['clock']['clock_id']+'|boot='+start['clock']['boot_id']==target['clock_id']
            row=normalized[target['event_id']]
            if klass=='runtime_command':
                action=start.get('runtime_command')
                assert isinstance(action,str) and m.hashlib.sha256(action.encode()).hexdigest()==start['runtime_command_sha256']
                f=m.semantic_features(action,instance.split('__')[0])
                feature={'class':f['semantic_class'],'operation':f['operation'],'executable':f['executable']}
                parent=raw.get(start.get('parent_event_id'),{})
                if parent:
                    assert parent['case_id']==start['case_id'] and parent['attempt_id']==start['attempt_id']
                    assert parent['clock']==start['clock'] and parent['start_mono_ns']<=start['start_mono_ns']
                context={'parent_kind':parent.get('event_kind','unknown')}
            else:feature={'preceding_lifecycle_kind':before.get(start['event_id'],'unknown')}
            if klass!='runtime_command':context={'penultimate_kind':before_two.get(start['event_id'],'unknown')}
            groups[(klass,row['hardware_domain'],row['target_boundary'])].append({**row,'extra_features':feature,'context_features':context})
    return groups


def main():
    groups=load_groups()
    report={}
    for (klass,domain,boundary),rows in groups.items():
        out=[]
        for fold in range(5):
            train=[r for r in rows if m.cal._fold(r['instance_id'])!=fold]
            test=[r for r in rows if m.cal._fold(r['instance_id'])==fold]
            base,bt=m.cal._table(train)
            extra,et=m.cal._table([{**r,'features':r['extra_features']} for r in train])
            for r in test:
                out.append({**r,'predictions_ms':{'baseline':m.cal._prediction(r,base,bt),
                    'descriptor':m.cal._prediction({'features':r['extra_features']},extra,et)}})
        report[klass]={'domain':domain,'boundary':boundary,'features':dict(__import__('collections').Counter(json.dumps(r['extra_features'],sort_keys=True) for r in rows)),
                       'baseline':m.metrics(out,'baseline'),'descriptor':m.metrics(out,'descriptor')}
    result={'results':report,'code_sha256':m.sha(__file__),'manifest_sha256':m.sha(HERE/'cpu_lifecycle/manifest.json'),
            'scope':'fixed grouped training-only descriptor screening; not promoted; no acquisition edits'}
    (HERE/'remaining_scan_20260914.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:{x:y for x,y in v.items() if x!='features'} for k,v in report.items()},indent=2))


if __name__=='__main__':main()
