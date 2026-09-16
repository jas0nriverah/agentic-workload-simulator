"""Bind the finished offline candidate, analyses and figure artifacts."""
from pathlib import Path
import hashlib
import importlib.util
import json

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    spec=importlib.util.spec_from_file_location('offline_candidate_api',BASE/'predict_candidate.py')
    api=importlib.util.module_from_spec(spec);spec.loader.exec_module(api)
    valid=[
        {'target':'historical_cpu_tool_wall','inputs':{'action':'cat README.md'}},
        {'target':'historical_gpu_request_proxy_wall','inputs':{}},
        {'target':'historical_start_known_e2e','inputs':{'repository':'django/django'}},
    ]
    predictions=[api.predict(x) for x in valid]
    rejected=0
    forbidden=('observed_ms','output_tokens','measured_residual_ms','future_state')
    for request in valid:
        for key in forbidden:
            bad=dict(request,inputs=dict(request['inputs'],**{key:1e99}))
            try:api.predict(bad)
            except ValueError:rejected+=1
            else:raise AssertionError('forbidden field accepted: '+key)
    for target in ('native_decode','native_prefill','atomic_cpu_operation','lifecycle'):
        try:api.predict({'target':target,'inputs':{}})
        except ValueError:rejected+=1
        else:raise AssertionError('unsupported target accepted')
    validation=json.loads((BASE/'candidate_api_validation.json').read_text())
    assert validation['feature_parity']=={'rows':23245,'class_mismatches':0,'feature_mismatches':0}
    validation.update({'prediction_smoke':predictions,'forbidden_or_unsupported_requests_rejected':rejected,
                       'api_sha256':sha(BASE/'predict_candidate.py')})
    (BASE/'candidate_api_validation.json').write_text(json.dumps(validation,indent=2)+'\n')
    components={
        'cpu_tool_wall':BASE/'cpu/class_hybrid_model.json',
        'gpu_request_proxy_wall':BASE/'gpu_lifecycle/gpu_proxy_chosen_model.json',
        'start_known_e2e':BASE/'e2e/candidate.json',
    }
    bundle={
        'schema_version':'assignment.offline-candidate-bundle.v1',
        'status':'prepared_for_later_validation_only',
        'production_or_acquisition_changes':False,
        'training_partition':'train_calibration',
        'training_instances':545,'training_runs':819,
        'components':{name:{'path':str(path.relative_to(BASE)),'sha256':sha(path)} for name,path in components.items()},
        'api':{'path':'predict_candidate.py','sha256':sha(BASE/'predict_candidate.py')},
        'reproduction':['build_training_view.py','cpu/run_cpu_event_models.py','cpu/run_cpu_class_hybrid.py',
                        'gpu_lifecycle/run_gpu_proxy.py','compare_start_e2e.py','join_predictions.py','figures/build_figures.py'],
        'reproduction_note':'CPU/extraction/E2E outputs refuse overwrite; use fresh output directories as supported. Joined hybrid command is documented in REPORT support files. No step runs inference.',
        'target_limits':['historical CPU tool duration, historical request wall proxy and direct historical E2E only',
                         'native phases, individual CPU operations and lifecycle remain unsupported for fitted prediction',
                         'cross-hardware transfer unvalidated; unchanged coefficients are a control, not a scaling law'],
        'selection_disclosure':'nested instance-grouped selection; sixth CPU class-wise extension developed after first-five diagnostics; not blind holdout confirmation',
        'd9_acceptance':'NOT_MET_BY_RETAINED_DIAGNOSTICS; complete repaired-event and hardware evaluation unproven',
        'source_modules':{str(p.relative_to(ROOT)):sha(p) for p in sorted((ROOT/'src/agentic_sim/assignment').glob('*.py'))},
    }
    (BASE/'candidate_bundle.json').write_text(json.dumps(bundle,indent=2)+'\n')
    artifacts={str(p.relative_to(BASE)):{'sha256':sha(p),'bytes':p.stat().st_size}
               for p in sorted(BASE.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts and p.name!='artifact_manifest.json'}
    (BASE/'artifact_manifest.json').write_text(json.dumps({'schema_version':'assignment.offline-artifacts.v1','files':artifacts},indent=2)+'\n')
    print(json.dumps({'bound_artifacts':len(artifacts),'rejected_forbidden_or_unsupported_requests':rejected,'feature_parity_rows':23245}))

if __name__=='__main__':main()
