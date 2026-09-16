"""Raw-free opt-in runtime alternatives; never changes the simulator default."""
import json
from pathlib import Path

ARTIFACT = Path(__file__).with_name('runtime_alternatives.json')


def predict(features, *, hardware_domain, candidate='descriptor_median', artifact=None):
    data = artifact if artifact is not None else json.loads(ARTIFACT.read_text())
    if hardware_domain != data['hardware_domain']:
        raise ValueError('runtime alternative is calibrated only for its recorded CPU domain')
    if candidate not in ('descriptor_median','descriptor_gate'):
        raise ValueError('unsupported candidate')
    if set(features) != {'class','operation','executable'} or not all(isinstance(v,str) for v in features.values()):
        raise ValueError('only start-known class, operation and executable strings are allowed')
    model=data['models'][candidate]
    return model['table'].get(json.dumps(features,sort_keys=True),model['fallback'])


def bind_artifact():
    # This packaging step reads fitted artifacts only, never execution labels.
    data=json.loads(ARTIFACT.read_text())
    source=ARTIFACT.parent.parent/'lifecycle_refinement/fit_artifact.json'
    reference=json.loads(source.read_text())['models']['runtime_command']
    data.update(schema='runtime-alternatives.v1',hardware_domain=reference['domain'],target_boundary=reference['boundary'])
    ARTIFACT.write_text(json.dumps(data,indent=2)+'\n')


if __name__=='__main__': bind_artifact()
