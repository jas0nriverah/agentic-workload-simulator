"""Reference-domain raw-free serving contract."""
import importlib.util
import sys
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('repaired_semantic_predict', HERE / 'predict.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def test_raw_free_parity_and_no_silent_transfer():
    import compare
    train = [dict(action='python x.py', repository='repo', operation_class='shell',
                  instance_id=str(i), observed_ms=10) for i in range(30)]
    model = compare.SemanticCpuModel().fit(train)
    artifact = dict(schema='repaired-semantic-candidate.v1', model=model.to_mapping(),
                    hardware_domain='reference-cpu', target_boundary='tool_execution')
    request = dict(action='python x.py', repository='repo', operation_class='shell', hardware_domain='reference-cpu')
    assert p.predict_request(request, artifact)['predicted_ms'] == model.predict(train[0])
    with pytest.raises(ValueError, match='domain'):
        p.predict_request({**request, 'hardware_domain': 'different'}, artifact)
    with pytest.raises(ValueError):
        p.predict_request({**request, 'observed_ms': 10}, artifact)
