import importlib.util
import json
from pathlib import Path

PATH=Path(__file__).resolve().parents[2]/'docs/d9-salvage-20260910/refine_lifecycle_20260914.py'
spec=importlib.util.spec_from_file_location('tested_lifecycle_refinement',PATH)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def rows():
    return [dict(instance_id=str(i%3),observed_ms=10,extra_features={'operation':'read'},
                 context_features={'parent':'known'}) for i in range(30)]


def test_no_label_in_prediction_and_serialization():
    for candidate in ('descriptor_median','descriptor_gate','context_median','context_hierarchical'):
        model=m.fit(rows(),candidate)
        request={'extra_features':{'operation':'read'},'context_features':{'parent':'known'}}
        assert m.predict(model,request)==m.predict(model,{**request,'observed_ms':100000,'outcome':True})
        assert m.predict(model,request)==m.predict(json.loads(json.dumps(model)),request)


def test_sparse_context_uses_supported_descriptor():
    training=rows()+[dict(instance_id=str(i%3),observed_ms=1000,extra_features={'operation':'write'},
                         context_features={'parent':'known'}) for i in range(90)]
    request={'extra_features':{'operation':'read'},'context_features':{'parent':'unseen'}}
    assert m.predict(m.fit(training,'context_hierarchical'),request)==10
    assert m.predict(m.fit(training,'context_median'),request)==1000


def test_single_instance_cannot_support_stratum():
    training=[{**r,'instance_id':'single'} for r in rows()]
    assert m.fit(training,'descriptor_gate')['table']=={}
