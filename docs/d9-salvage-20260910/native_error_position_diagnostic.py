"""Bounded diagnostic of native errors by request-start ordinal; no refitting by position."""
import hashlib
import importlib.util
import json
from collections import defaultdict
from pathlib import Path

BASE=Path(__file__).resolve().parent

def main():
    spec=importlib.util.spec_from_file_location('d9_e2e_helper',BASE/'e2e/compare.py')
    fit=importlib.util.module_from_spec(spec);spec.loader.exec_module(fit)
    manifest=json.loads((BASE/'evidence/calibration_input_manifest.json').read_text())
    source=Path(manifest['native_phase_dataset_path'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==manifest['native_phase_dataset_sha256']
    rows=[json.loads(s) for s in source.read_text().splitlines()]
    rows=[r for r in rows if r['native_component']=='e2e']
    starts={}
    for case in manifest['cases']:
        item=case['source']['model_events'];p=Path(item['path'])
        assert hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256']
        rr=[json.loads(s) for s in p.read_text().splitlines()]
        rr=sorted((r for r in rr if r.get('event_kind')=='model_request_start'),key=lambda r:r['start_mono_ns'])
        for index,r in enumerate(rr):starts[(case['case_id'],r['physical_request_id'])]=index
    for r in rows:
        r['fold']=int.from_bytes(hashlib.sha256(('assignment.d9.native-fold-v1:'+r['instance_id']).encode()).digest()[:8],'big')%5
    def design(r):return [1,r['prompt_tokens']/1000,r['completion_tokens']/1000]
    bins=defaultdict(list)
    for fold in range(5):
        train=[r for r in rows if r['fold']!=fold]
        b=fit.fit([design(r) for r in train],[r['observed_ms'] for r in train],True)
        for r in rows:
            if r['fold']!=fold:continue
            index=starts[(r['case_id'],r['physical_request_id'])]
            error=abs(fit.predict(b,design(r))-r['observed_ms'])/r['observed_ms']*100
            bins['first_request' if index==0 else 'later_request'].append(error)
    result={'scope':'Diagnostic of fixed conditional token model; request position was not a model input or selection key.',
            'groups':{k:{'n':len(v),'within25':sum(e<=25 for e in v),'misses':sum(e>25 for e in v),
                         'worst_pct':max(v)} for k,v in bins.items()},
            'interpretation':'Position association does not identify a causal cold-start mechanism.'}
    (BASE/'native_error_positions.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
