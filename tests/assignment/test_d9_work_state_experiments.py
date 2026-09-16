import importlib.util
import json
from pathlib import Path
import pytest

PATH=Path(__file__).resolve().parents[2]/'docs/d9-salvage-20260910/work_state_experiments.py'
spec=importlib.util.spec_from_file_location('work_state_tests',PATH)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_prior_payload_rejects_future_foreign_attempt_and_clock():
    start=dict(case_id='c',attempt_id='a',clock={'boot':'one'},start_mono_ns=10)
    good=dict(case_id='c',attempt_id='a',clock={'boot':'one'},end_mono_ns=8)
    bad=[good|{'end_mono_ns':11},good|{'attempt_id':'other','end_mono_ns':9},
         good|{'clock':{'boot':'two'},'end_mono_ns':9},good|{'case_id':'foreign','end_mono_ns':9}]
    assert m.preceding_request(bad+[good],start)==good
    assert m.preceding_request(bad,start) is None


def test_support_requires_independent_instances():
    rows=[dict(base='x',observed_ms=1,instance_id='one') for _ in range(100)]
    assert m.fit_table(rows,'base')=={}


def test_predict_ignores_labels_and_backs_off_to_matching_estimator():
    models={'base':{'x':2},'base_gate':{'x':3},'history_size_gate':{},'fallback':10}
    row={'base':'x','base_gate':'x','history_size_gate':'missing'}
    assert m.table_predict(models,row,'history_size_gate')==3
    assert m.table_predict(models,row|{'observed_ms':999,'duration_ms':1},'history_size_gate')==3


def test_queue_advances_predicted_work_and_resets_after_idle_gap():
    assert m.simulated_queue([0.,1.,8.],[5.,2.,1.],[.1,.1,.1])==pytest.approx([.1,4.2,.1])
    assert m.simulated_queue([],[],[])==[]


@pytest.mark.parametrize('a,s,d',[([1,0],[1,1],[0,0]),([0],[-1],[0]),([0],[1],[]),([0],[float('nan')],[0])])
def test_queue_rejects_invalid_inputs(a,s,d):
    with pytest.raises(ValueError):m.simulated_queue(a,s,d)


def test_runtime_export_roundtrip_and_domain_boundary():
    spec=importlib.util.spec_from_file_location('runtime_candidate',PATH.parent/'work_state_experiments/runtime_candidate.py')
    runtime=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    artifact=json.loads(runtime.ARTIFACT.read_text())
    domain=artifact['hardware_domain']
    for candidate,model in artifact['models'].items():
        for key,value in model['table'].items():
            assert runtime.predict(json.loads(key),hardware_domain=domain,candidate=candidate,artifact=artifact)==value
    features={'class':'unknown','operation':'unknown','executable':'unknown'}
    with pytest.raises(ValueError):runtime.predict(features,hardware_domain='different')
    with pytest.raises(ValueError):runtime.predict(features|{'observed_ms':1},hardware_domain=domain)
