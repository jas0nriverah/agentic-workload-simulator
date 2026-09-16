"""Bounded openat entry-feature check on fixed Astropy and Django traces."""
import hashlib
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median, mean

HERE=Path(__file__).resolve().parent
BASE=HERE.parent
sys.path.insert(0,str(BASE/'atomic_cpu'))
import run_atomic_cpu as atomic
from agentic_sim.telemetry.bpf_work import _CWorkEvent, BpfWorkCollector, BPF_EVENT_SCHEMA


def path_group(row):
    coarse=atomic.path_class(row)
    if coarse in ('entry_path_unknown','entry_path_truncated'):
        return coarse
    path=row.get('path') or ''
    for root in ('/proc/','/sys/','/dev/'):
        if path.startswith(root):return root
    if '/site-packages/' in path:return 'site_packages'
    suffix=Path(path).suffix
    return suffix if suffix in ('.py','.pyc','.so','.json') else 'other'


def features(row):
    # openat flags are scalar syscall-entry arg2, never ret/status.
    return (atomic.path_class(row),int(row['scalar_args']['open_flags']),path_group(row))


def load_cases():
    reference=json.loads((BASE/'atomic_cpu/model.json').read_text())
    assert reference['diagnostics']['range_mismatch_count']==0
    assert all(reference['diagnostics'][k]==0 for k in ('perf_lost_events','event_callback_error_count','lost_event_records','lost_path_records','lost_pending_records'))
    cases=reference['selection']['selected_cases']
    assert all(c['raw_hash_verified'] for c in cases)
    manifest=json.loads(atomic.CPU_EVIDENCE_MANIFEST.read_text())
    # Match the transfer experiment's fixed first ordinal Django case.
    candidate=next(c for c in manifest['cases'] if c['queue_ordinal']==18)
    assert candidate['instance_id']=='django__django-10914'
    assert atomic._fully_valid_case(candidate)[0]
    cases=cases+[atomic.prepare_case(dict(candidate))]
    transfer=json.loads((BASE/'cpu_transfer/model.json').read_text())
    assert transfer['population']['raw_hash_all_verified']
    assert transfer['population']['range_mismatch_count']==0
    assert transfer['population']['token_join_mismatch_count']==0
    assert all(v==0 for v in transfer['population']['aggregate_drops'].values())
    previous=transfer['selection']['selected_cases'][0]
    assert (previous['case_id'],previous['raw_hash_recorded'])==(cases[-1]['case_id'],cases[-1]['raw_hash_recorded'])
    assert sum(c['raw_bytes'] for c in cases)<750_000_000
    return cases


def acquire(cases):
    data=[];sources=[];unsupported=defaultdict(int)
    for index,case in enumerate(cases):
        assert case['record_size_bytes']==400
        digest=hashlib.sha256();offset=0;count=0
        with Path(case['raw_path']).open('rb') as stream:
            for chunk in iter(lambda:stream.read(400*4096),b''):
                digest.update(chunk);assert len(chunk)%400==0
                for start in range(0,len(chunk),400):
                    packet=chunk[start:start+400]
                    event=_CWorkEvent.from_buffer_copy(packet)
                    if event.syscall_nr==257:
                        row=BpfWorkCollector._event_row(packet,schema_version=BPF_EVENT_SCHEMA)
                        assert row['kind_name']=='open'
                        count+=1
                        duration=row.get('duration_ns')
                        if duration is None or duration<=0:
                            unsupported[case['instance_id']]+=1
                        else:
                            data.append((index,features(row),duration,offset+start,int(event.token),int(event.sequence)))
                offset+=len(chunk)
        assert offset==case['raw_bytes']
        assert digest.hexdigest()==case['raw_hash_recorded']
        sources.append({'instance_id':case['instance_id'],'case_id':case['case_id'],'raw_path':case['raw_path'],'raw_bytes':offset,'sha256':digest.hexdigest(),'openat_records':count})
    return data,sources,dict(unsupported)


def key(feat,candidate):
    coarse,flags,path=feat
    return {'path':(coarse,), 'flags':(flags,), 'flags_path':(flags,path)}[candidate]


def fit(rows,candidate):
    groups=defaultdict(list);instances=defaultdict(set)
    for case,feat,y,*_ in rows:
        groups[key(feat,candidate)].append(y);instances[key(feat,candidate)].add(case)
    # The same fixed support rule applies to every candidate.
    return median(r[2] for r in rows),{k:median(v) for k,v in groups.items() if len(v)>=20 and len(instances[k])>=2}


def score(rows,model,candidate,cases):
    fallback,groups=model;passed=0;worst=-1;worst_event=None;backoffs=0
    for index,feat,y,offset,token,sequence in rows:
        k=key(feat,candidate);prediction=groups.get(k,fallback);backoffs+=k not in groups
        error=abs(prediction-y)/y*100;passed+=error<=25
        if error>worst:
            worst=error;worst_event={'instance_id':cases[index]['instance_id'],'offset':offset,'token':token,'sequence':sequence,'observed_ns':y,'predicted_ns':prediction,'feature':k}
    return {'events':len(rows),'within25':passed,'coverage_percent':100*passed/len(rows),'worst_error_percent':worst,'worst_event':worst_event,'backoff_events':backoffs}


def serialize(model):
    fallback,groups=model
    return {'fallback_ns':fallback,'groups':[{'key':list(k),'prediction_ns':v} for k,v in sorted(groups.items(),key=lambda kv:str(kv[0]))]}


def main():
    cases=load_cases();data,sources,unsupported=acquire(cases)
    assert not unsupported, 'Nonpositive/censored openat targets require explicit full-population scoring; refusing partial coverage'
    assert len({c['instance_id'] for c in cases})==5
    results={}
    for candidate in ('path','flags','flags_path'):
        folds=[]
        for held in range(4):
            train=[r for r in data if r[0]<4 and r[0]!=held]
            test=[r for r in data if r[0]==held]
            model=fit(train,candidate)
            folds.append({'held_instance':cases[held]['instance_id'],'model':serialize(model),
                          'astropy':score(test,model,candidate,cases),
                          'django':score([r for r in data if r[0]==4],model,candidate,cases)})
        results[candidate]={'folds':folds,'full_astropy_fit':serialize(fit([r for r in data if r[0]<4],candidate)),
             'astropy_coverage_percent':100*sum(f['astropy']['within25'] for f in folds)/sum(f['astropy']['events'] for f in folds),
             'astropy_worst_error_percent':max(f['astropy']['worst_error_percent'] for f in folds),
             'django_mean_coverage_percent':mean(f['django']['coverage_percent'] for f in folds),
             'django_worst_error_percent':max(f['django']['worst_error_percent'] for f in folds)}
    report={'contract':'openat only; same fixed4Astropy instances leave-one-instance-out, unchanged fits evaluated on one fixed Django case; development comparison, not untouched validation',
            'features':'entry open_flags and fixed generic lexical path groups; result/status/latency excluded',
            'support_rule':'20 training events and2training instances per group; otherwise operation median',
            'sources':sources,'unsupported_openat_targets':unsupported,'results':results,
            'atomic_reference_sha256':atomic.sha256_file(BASE/'atomic_cpu/model.json'),
            'transfer_integrity_reference_sha256':atomic.sha256_file(BASE/'cpu_transfer/model.json'),
            'calibration_manifest_sha256':atomic.sha256_file(atomic.CPU_EVIDENCE_MANIFEST),
            'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'new_inference':False}
    (HERE/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:{f:v for f,v in r.items() if f not in ('folds','full_astropy_fit')} for k,r in results.items()},indent=2))


if __name__=='__main__':main()
