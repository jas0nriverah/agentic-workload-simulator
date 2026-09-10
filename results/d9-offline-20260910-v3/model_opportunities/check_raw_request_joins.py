"""Independent exact client/proxy/native join check on the43eligibleCPU cases."""
import hashlib
import json
from collections import Counter
from pathlib import Path

HERE=Path(__file__).resolve().parent
BASE=HERE.parent


def main():
    eligible=json.loads((BASE/'cpu_lifecycle/manifest.json').read_text())['cases']
    ids={c['case_id'] for c in eligible};assert len(ids)==43
    manifest=json.loads((BASE/'evidence/calibration_input_manifest.json').read_text())
    native={}
    native_bytes=(BASE/'evidence/native_phase_dataset.jsonl').read_bytes()
    assert hashlib.sha256(native_bytes).hexdigest()==manifest['native_phase_dataset_sha256']
    for line in native_bytes.splitlines():
        r=json.loads(line)
        if r['case_id'] in ids and r['native_component']=='e2e':
            key=(r['case_id'],r['physical_request_id']);assert key not in native;native[key]=r
    counts=Counter();errors=[];sources=[];joins=[];non_nested=[]
    for case in manifest['cases']:
        if case['case_id'] not in ids:continue
        source=case['source']['model_events'];data=Path(source['path']).read_bytes()
        assert hashlib.sha256(data).hexdigest()==source['sha256']
        sources.append(source);rows=[json.loads(s) for s in data.splitlines()]
        starts={r['event_id']:r for r in rows if r['event_kind']=='model_client_call_start'}
        clients={r['span_id']:r for r in rows if r['event_kind']=='model_client_call'}
        for r in rows:
            if r['event_kind']!='model_request':continue
            counts['complete_proxy_requests']+=1
            parent=starts.get(r.get('parent_event_id'));client=clients.get(r.get('client_span_id'))
            key=(case['case_id'],r['physical_request_id'])
            checks={'parent_start':parent is not None,'client_end':client is not None,'native':key in native}
            if parent and client:
                checks.update(parent_span=parent['span_id']==client['span_id'],
                              logical_id=r['logical_request_id']==parent['logical_request_id']==client['logical_request_id'],
                              local_clock=all(r['clock'].get(k)==client['clock'].get(k) for k in ('hostname','boot_id','clock_id')),
                              containment=client['start_mono_ns']<=r['start_mono_ns']<=r['end_mono_ns']<=client['end_mono_ns'])
            for check,ok in checks.items():counts[check]+=bool(ok)
            if not all(v for k,v in checks.items() if k!='containment'):errors.append({'case_id':case['case_id'],'request_id':r['physical_request_id'],'checks':checks})
            else:
                if not checks['containment']:
                    non_nested.append({'case_id':case['case_id'],'physical_request_id':r['physical_request_id'],
                                       'proxy_end_after_client_ms':(r['end_mono_ns']-client['end_mono_ns'])/1e6,
                                       'proxy_start_after_client_ms':(r['start_mono_ns']-client['start_mono_ns'])/1e6})
                joins.append({'case_id':case['case_id'],'physical_request_id':r['physical_request_id'],
                              'client_span_id':client['span_id'],'logical_request_id':r['logical_request_id'],
                              'client_start_event_id':parent['event_id'],'proxy_event_id':r['event_id'],
                              'local_clock':r['clock'],
                              'client_start_ns':client['start_mono_ns'],'client_end_ns':client['end_mono_ns'],
                              'proxy_start_ns':r['start_mono_ns'],'proxy_end_ns':r['end_mono_ns']})
    report={'eligible_cases':len(ids),'native_requests':len(native),'counts':dict(counts),'errors':errors,
            'sources':sources,'joins':joins,'non_nested_local_boundaries':non_nested,
            'native_dataset_sha256':hashlib.sha256(native_bytes).hexdigest(),
            'cpu_manifest_sha256':hashlib.sha256((BASE/'cpu_lifecycle/manifest.json').read_bytes()).hexdigest(),
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'decision':'Raw parent/logical/client-span links support exact joins; no acquisition defect follows from their absence in normalized adapter rows.',
            'limits':'Observed nesting proves reconstruction only, not a fitted future E2E model. Remote native timestamps are not unioned with the local clock.'}
    (HERE/'raw_request_join_proof.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'counts':dict(counts),'identity_errors':len(errors),'non_nested_boundaries':len(non_nested),'native_requests':len(native),'cases':len(ids)}))


if __name__=='__main__':main()
