"""Raw-free selected semantic CPU inference with an explicit reference domain."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))
from agentic_sim.assignment.semantic_cpu_model import SemanticCpuModel


def predict_request(request, artifact):
    if not isinstance(request, dict) or set(request) != {'action', 'repository', 'hardware_domain', 'operation_class'}:
        raise ValueError('requires action, repository, hardware_domain and recorded pre-action operation_class; labels are not inputs')
    if any(not isinstance(v, str) or not v.strip() for v in request.values()):
        raise ValueError('request values must be nonempty strings')
    if artifact.get('schema') != 'repaired-semantic-candidate.v1':
        raise ValueError('unsupported artifact schema')
    if request['hardware_domain'] != artifact['hardware_domain']:
        raise ValueError('different CPU hardware domain is unsupported; no transfer law was fitted')
    # Keep the recorded pre-action class. Re-extracting with a different parser
    # changes historical classifications and breaks fit/serve parity.
    if request['operation_class'] not in {'shell', 'read', 'write', 'patch', 'search', 'test', 'traversal', 'other'}:
        raise ValueError('unsupported operation class')
    inputs = {'action': request['action'], 'repository': request['repository'],
              'operation_class': request['operation_class']}
    return {'predicted_ms': SemanticCpuModel.from_mapping(artifact['model']).predict(inputs),
            'event_class': 'semantic_action', 'target_boundary': artifact['target_boundary'],
            'hardware_domain': artifact['hardware_domain'], 'hardware_transfer_validated': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('request', type=Path)
    parser.add_argument('--model', type=Path, default=Path(__file__).with_name('fit_artifact.json'))
    args = parser.parse_args()
    print(json.dumps(predict_request(json.loads(args.request.read_text()), json.loads(args.model.read_text())), indent=2))
