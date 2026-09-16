import importlib.util
import json
import sys
import pytest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
SPEC = importlib.util.spec_from_file_location("e2e_composition_predict", HERE / "predict.py")
PREDICT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PREDICT)


def test_saved_artifact_predicts_without_raw_evidence():
    request = json.loads((HERE / "example_request.json").read_text())
    artifact = json.loads((HERE / "fit_artifact.json").read_text())
    result = PREDICT.predict_request(request, artifact)
    assert result["selected_model"] == "direct"
    assert result["selected_direct_e2e_ms"] > 0
    assert result["composed_e2e_ms"] == (
        result["cpu_union_ms"] + result["native_ms"] + result["remainder_ms"]
    ) * artifact["training_only_coverage_scales"]["composed"]


@pytest.mark.parametrize('value', [True, '43', 43.5, -1, float('nan')])
def test_request_counts_reject_coercion(value):
    request = json.loads((HERE / 'example_request.json').read_text())
    request['request_count'] = value
    with pytest.raises(ValueError):
        PREDICT.design(request)


def test_unknown_labels_and_impossible_cache_rejected():
    request = json.loads((HERE / 'example_request.json').read_text())
    with pytest.raises(ValueError):
        PREDICT.design({**request, 'observed_ms': 123})
    with pytest.raises(ValueError):
        PREDICT.design({**request, 'cached_tokens': request['input_tokens'] + 1})
