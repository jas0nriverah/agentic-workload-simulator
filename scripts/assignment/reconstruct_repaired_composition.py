"""First retained-only integration of component predictions and serial accounting.

No acquisition edits, new inference, protected labels or measured-gap correction.
The replay hierarchy is supplied from recorded local interval containment; it is
not a prospective forecast of event ordering. Partial overlaps remain unsupported.
"""
import csv
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from agentic_sim.assignment.event_composition import compose_serial

BASE=ROOT/'docs/d9-salvage-20260910'
OUT=ROOT/'docs/current/composition'


def load_module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def read_rows(path):
    return [json.loads(line) for line in Path(path).open()]


def hierarchy(events):
    """Observed local containment supplies topology only, never predictions."""
    ordered=sorted(events,key=lambda r:(r['start_mono_ns'],-r['end_mono_ns'],r['event_id']))
    stack,nodes,overlaps=[],[],[]
    for r in ordered:
        while stack and (r['start_mono_ns']>=stack[-1]['end_mono_ns'] or r['end_mono_ns']>stack[-1]['end_mono_ns']):
            previous=stack.pop()
            if not stack and r['start_mono_ns']<previous['end_mono_ns']<r['end_mono_ns']:
                overlaps.append([previous['event_id'],r['event_id']])
        parent=stack[-1]['event_id'] if stack else None
        nodes.append({'event_id':r['event_id'],'parent_event_id':parent,'event_class':r['event_class']})
        stack.append(r)
    return nodes,overlaps


def startup_envelope(rows):
    """A separate accounting target; original overlapping events are retained."""
    selected=[r for r in rows if r['event_class'] in ('lifecycle:startup','lifecycle:setup')]
    if len(selected)!=2 or len({r['event_class'] for r in selected})!=2:
        raise ValueError('expected one startup and one setup')
    if max(r['start_mono_ns'] for r in selected)>min(r['end_mono_ns'] for r in selected):
        raise ValueError('disjoint startup/setup needs a different declared graph')
    start=min(r['start_mono_ns'] for r in selected);end=max(r['end_mono_ns'] for r in selected)
    return selected[0]|{'event_id':'composition:startup_setup:'+selected[0]['case_id'],
                        'event_class':'composition:startup_setup_envelope','target_boundary':'startup_setup_union',
                        'start_mono_ns':start,'end_mono_ns':end,'observed_ms':(end-start)/1e6,
                        'features':{'operation':'startup_setup_envelope'},'accounting_only':True}


def main():
    sem=load_module('composition_semantic',BASE/'semantic_repaired/compare.py')
    semantic,provenance=sem.load_rows()  # Pins training identities and verifies raw journals.
    native=load_module('composition_native',BASE/'native/run_native_comparison.py')
    native_rows,native_manifest,native_hashes=native.load_dataset(native.DEFAULT_DATASET,native.DEFAULT_MANIFEST)
    native_by_id={(r['case_id'],r['physical_request_id']):r for r in native_rows if r['native_component']=='e2e'}
    manifest=sem.read(BASE/'cpu_lifecycle/manifest.json')
    by_class=defaultdict(list)
    raw_by_case,outer_by_case={},{}
    sources=[]
    for case in manifest['cases']:
        cid=case['case_id']; raw=read_rows(case['events_path'])
        outer=[r for r in raw if r['event_class']=='lifecycle:outer_swe_agent']
        assert len(outer)==1
        outer_by_case[cid]=outer[0]
        admitted,_=sem.cal._read_eligible_events(Path(case['events_path']),case['instance_id'],cid)
        selected=[r for r in raw if r['event_class'] not in ('lifecycle:outer_swe_agent','lifecycle:runner_process_wrapper')]
        raw_by_case[cid]=selected
        for r in admitted:
            if r['event_class'] in ('lifecycle:outer_swe_agent','lifecycle:runner_process_wrapper'): continue
            by_class[(r['event_class'],r['target_boundary'],r['hardware_domain'])].append(r)
        envelope=startup_envelope(selected)
        reference=next(r for r in admitted if r['event_class']=='lifecycle:startup')
        envelope['hardware_domain']=reference['hardware_domain']
        selected.append(envelope)
        by_class[(envelope['event_class'],envelope['target_boundary'],envelope['hardware_domain'])].append(envelope)
        source=next(c for c in native_manifest['cases'] if c['case_id']==cid)['source']['model_events']
        assert sem.sha(source['path'])==source['sha256']; sources.append(source)
        model=read_rows(source['path'])
        clients={r['span_id']:r for r in model if r['event_kind']=='model_client_call'}
        starts={r['event_id']:r for r in model if r['event_kind']=='model_client_call_start'}
        seen=set()
        for proxy in model:
            if proxy['event_kind']!='model_request': continue
            client=clients[proxy['client_span_id']]; start=starts[proxy['parent_event_id']]
            assert client['span_id']==start['span_id'] and client['span_id'] not in seen
            seen.add(client['span_id'])
            assert proxy['logical_request_id']==client['logical_request_id']==start['logical_request_id']
            assert client['clock']==start['clock']==proxy['clock']
            assert client['case_id']==cid and client['attempt_id']==outer[0]['attempt_id']
            n=native_by_id[(cid,proxy['physical_request_id'])]
            row={'event_id':client['event_id'],'case_id':cid,'instance_id':case['instance_id'],
                 'event_class':'model_client_call','target_boundary':'client_call_inclusive',
                 'observed_ms':client['duration_ms'],
                 'prompt_tokens':n['prompt_tokens'],'completion_tokens':n['completion_tokens'],
                 'start_mono_ns':client['start_mono_ns'],'end_mono_ns':client['end_mono_ns'],
                 'host_id':client['clock']['hostname'],
                 'clock_id':client['clock']['clock_id']+'|boot='+client['clock']['boot_id']}
            by_class[('model_client_call','client_call_inclusive',n['hardware_domain'])].append(row)
            raw_by_case[cid].append(row)
        assert len(seen)==len(clients)
    # One common instance fold for every component; no cross-component stacking fit.
    predictions={}; fold_models=[]
    for group_key,rows in by_class.items():
        for fold in range(5):
            train=[r for r in rows if sem.cal._fold(r['instance_id'])!=fold]
            test=[r for r in rows if sem.cal._fold(r['instance_id'])==fold]
            if not test or not train: continue  # Unsupported stays in denominator.
            assert not ({r['instance_id'] for r in train}&{r['instance_id'] for r in test})
            if group_key[0]=='model_client_call':
                beta=native.COMPARE.fit([native.design(r,'relative_nnls_token') for r in train],
                                        [r['observed_ms'] for r in train],relative=True)
                fold_models.append({'group':group_key,'fold':fold,'kind':'relative_nnls_token','coefficients':list(beta)})
                for r in test:
                    inputs={k:r[k] for k in ('prompt_tokens','completion_tokens')}
                    predictions[(r['case_id'],r['event_id'])]=sum(b*x for b,x in zip(beta,native.design(inputs,'relative_nnls_token')))
            elif group_key[0]=='semantic_action':
                from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel
                fitted=SemanticCpuModel(center='gate').fit([r for r in semantic if r['fold']!=fold])
                fold_models.append({'group':group_key,'fold':fold,'kind':'semantic','model':fitted.to_mapping()})
                for r in semantic:
                    if r['fold']==fold:
                        predictions[(r['case_id'],r['event_id'])]=fitted.predict({k:r[k] for k in ('action','repository','operation_class')})
            else:
                fallback,table=sem.cal._table(train)
                fold_models.append({'group':group_key,'fold':fold,'kind':'feature_median','fallback':fallback,'table':table})
                for r in test: predictions[(r['case_id'],r['event_id'])]=sem.cal._prediction({'features':r['features']},fallback,table)
    # Restore selected descriptor models, now fitted on the same common folds.
    life=load_module('composition_lifecycle',BASE/'refine_lifecycle_20260914.py')
    for group_key,rows in life.scan.load_groups().items():
        for fold in range(5):
            fitted=life.fit([r for r in rows if sem.cal._fold(r['instance_id'])!=fold],'descriptor_median')
            fold_models.append({'group':group_key,'fold':fold,'kind':'descriptor_override','model':fitted})
            for r in rows:
                if sem.cal._fold(r['instance_id'])==fold:
                    predictions[(r['case_id'],r['event_id'])]=life.predict(fitted,{'extra_features':r['extra_features']})
    OUT.mkdir(parents=True,exist_ok=True)
    cases,events,graphs=[],[],[]
    for cid,rows in raw_by_case.items():
        outer=outer_by_case[cid]
        assert all((r['host_id'],r['clock_id'])==(outer['host_id'],outer['clock_id']) for r in rows)
        assert all(outer['start_mono_ns']<=r['start_mono_ns']<=r['end_mono_ns']<=outer['end_mono_ns'] for r in rows)
        nodes,overlaps=hierarchy(rows)
        values={r['event_id']:predictions[(cid,r['event_id'])] for r in rows if (cid,r['event_id']) in predictions}
        composition=compose_serial(nodes,values)
        graphs.append({'case_id':cid,'instance_id':outer['instance_id'],'nodes':nodes,'predictions_ms':values,
                       'partial_overlaps':overlaps,'topology':'supplied local containment accounting; causal transfer not established'})
        accounted=composition['predicted_accounted_ms'] if not overlaps else None
        roots=set(composition['root_event_ids'])
        measured_accounted=sum(r['observed_ms'] for r in rows if r['event_id'] in roots) if not overlaps else None
        predicted_error=abs(accounted-outer['observed_ms'])/outer['observed_ms'] if accounted is not None else None
        cases.append({'case_id':cid,'instance_id':outer['instance_id'],'outer_ms':outer['observed_ms'],
                      'predicted_accounted_ms':accounted,'observed_accounted_ms':measured_accounted,
                      'unaccounted_ms_diagnostic_only':outer['observed_ms']-measured_accounted if measured_accounted is not None else None,
                      'e2e_error_percent':predicted_error*100 if predicted_error is not None else None,
                      'within25':predicted_error is not None and predicted_error<=.25,
                      'partial_overlaps':overlaps, 'missing_predictions':composition['missing_event_ids']})
        for r in rows:
            p=values.get(r['event_id']); y=r['observed_ms']
            events.append({'case_id':cid,'instance_id':outer['instance_id'],'event_id':r['event_id'],
                           'event_class':r['event_class'],'observed_ms':y,'predicted_ms':p,'e2e_root':r['event_id'] in roots,
                           'within25':p is not None and abs(p-y)<=.25*y})
    summary={'cases':len(cases),'events':len(events),'e2e_within25_cases':sum(r['within25'] for r in cases),
             'partial_overlap_cases':sum(bool(r['partial_overlaps']) for r in cases),
             'all_event_and_e2e_cases':sum(c['within25'] and all(r['within25'] for r in events if r['case_id']==c['case_id']) for c in cases),
             'per_class':{k:{'events':len(v),'within25_percent':100*mean(r['within25'] for r in v)} for k in sorted({r['event_class'] for r in events}) if (v:=[r for r in events if r['event_class']==k])},
             'method':'Fixed instance-grouped component fits, inclusive serial roots; no residual correction. Model client-call wrapper is directly fitted using supplied tokens, not mislabeled native GPU service.',
             'hardware_transfer_validated':False,'d9_pass':False,'source_sha256':sem.sha(__file__),
             'composition_source_sha256':sem.sha(ROOT/'src/agentic_sim/assignment/event_composition.py'),
             'e2e_worst_error_percent':max(c['e2e_error_percent'] for c in cases if c['e2e_error_percent'] is not None),
             'missing_event_predictions':sum(len(c['missing_predictions']) for c in cases),
             'sources':sources,'native_hashes':native_hashes,'semantic_provenance':provenance}
    for name,data in [('summary',summary),('cases',cases)]: (OUT/(name+'.json')).write_text(json.dumps(data,indent=2)+'\n')
    (OUT/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in events))
    (OUT/'graphs.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in graphs))
    (OUT/'fold_models.json').write_text(json.dumps({'fold_prefix':sem.cal.FOLD_PREFIX,'models':fold_models},indent=2)+'\n')
    with (OUT/'e2e.csv').open('w') as f:
        columns=['case_id','instance_id','outer_ms','predicted_accounted_ms','observed_accounted_ms','unaccounted_ms_diagnostic_only','e2e_error_percent','within25']
        writer=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');writer.writeheader();writer.writerows(cases)
    print(json.dumps({k:v for k,v in summary.items() if k not in ('sources','native_hashes','semantic_provenance')},indent=2))


if __name__=='__main__': main()
