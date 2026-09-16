"""Raw-free integration and leakage checks for the repaired D9 candidates."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT/'docs/d9-salvage-20260910'
sys.path.insert(0,str(BASE/'simulator'))
from d9_simulator import D9Simulator, PredictionContractError


def load(path, name):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_semantic_roundtrip_and_reference_domain():
    artifact = json.loads((BASE/'semantic_repaired/refined_fit_artifact.json').read_text())
    predictor = load(BASE/'semantic_repaired/predict.py','integration_semantic')
    request = dict(action='python example.py', repository='astropy', operation_class='shell',
                   hardware_domain=artifact['hardware_domain'])
    result = D9Simulator().predict('repaired_semantic_action',request)
    assert result['predicted_ms'] == predictor.predict_request(request,artifact)['predicted_ms']
    with pytest.raises(PredictionContractError):
        D9Simulator().predict('repaired_semantic_action',{**request,'observed_ms':1})
    with pytest.raises(PredictionContractError):
        D9Simulator().predict('repaired_semantic_action',{**request,'hardware_domain':'different'})


def test_repaired_e2e_matches_existing_predictor():
    predictor = load(BASE/'e2e_composition/predict.py','integration_e2e')
    request = json.loads((BASE/'e2e_composition/example_request.json').read_text())
    artifact = json.loads((BASE/'e2e_composition/fit_artifact.json').read_text())
    result = D9Simulator().predict('conditional_repaired_e2e',request)
    assert result['predicted_ms'] == predictor.predict_request(request,artifact)['selected_direct_e2e_ms']
    assert 'not_event_sum' in result['contract']


@pytest.mark.parametrize('phase',['queue','prefill','decode'])
def test_native_phase_roundtrip_and_cache_contract(phase):
    predictor = load(BASE/'native_refinement/compare_phases.py','integration_native')
    artifact = json.loads((BASE/'native_refinement/fit_artifact.json').read_text())
    request = dict(phase=phase,prompt_tokens=2048,completion_tokens=128,cached_tokens=1024,
                   cache_trace=True,hardware_domain=artifact['hardware_domain'])
    actual = D9Simulator().predict('conditional_native_phase',request)
    assert actual['predicted_ms'] == predictor.predict(artifact['models'][phase],request)
    for patch in ({'cache_trace':False},{'prompt_tokens':2.5},{'observed_ms':1},{'hardware_domain':'different'}):
        with pytest.raises(PredictionContractError):
            D9Simulator().predict('conditional_native_phase',{**request,**patch})


def test_phase_design_cannot_use_outcomes_or_duration():
    predictor = load(BASE/'native_refinement/compare_phases.py','integration_native_design')
    row = dict(prompt_tokens=1000,completion_tokens=10,cached_tokens=500)
    for phase in ('queue','prefill','decode','e2e'):
        for candidate in predictor.CANDIDATES:
            assert predictor.design(row,phase,candidate) == predictor.design(
                {**row,'observed_ms':100000,'outcome':'resolved','instance_id':'secret'},phase,candidate)


def test_phase_fold_identity_and_baseline_reproduction():
    module = load(BASE/'native_refinement/compare_phases.py','integration_native_folds')
    report = json.loads((BASE/'native_refinement/report.json').read_text())
    assert report['phases']['prefill']['metrics']['pooled']['within25_percent'] == pytest.approx(83.7019230769)
    seen = {}
    for row in map(json.loads,(BASE/'native_refinement/predictions.jsonl').open()):
        seen.setdefault(row['instance_id'],set()).add(row['fold'])
        assert row['fold'] == module.native.fold_for_instance(row['instance_id'])
    assert len(seen) == 25 and all(len(folds)==1 for folds in seen.values())


def test_integrated_native_overlap_cannot_pass_literal_gate():
    from strict_metrics import score_bundle
    rows = [dict(target=t,trajectory_id='run',event_id=t,observed_ms=10)
            for t in ('conditional_native_e2e','conditional_native_phase')]
    report = score_bundle([{**r,'predicted_ms':10} for r in rows],rows,
                          required_target_kinds=('conditional_native_e2e','conditional_native_phase'))
    assert report['composition_status'] == 'overlap_rejected_native_e2e_and_phase_targets'
    assert report['literal_d9_status'] == 'UNPROVEN_OR_FAILED'
