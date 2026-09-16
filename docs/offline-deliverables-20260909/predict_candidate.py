"""Offline candidate API with explicit historical target boundaries."""
from pathlib import Path
from dataclasses import asdict
import json
import sys

BASE=Path(__file__).resolve().parent
ROOT=BASE.parents[1]
sys.path[:0]=[str(ROOT/'src'),str(BASE/'cpu'),str(BASE/'gpu_lifecycle')]
from agentic_sim.assignment.tool_features import extract_tool_features
from agentic_sim.assignment.cpu_event_model import enrich_row
from agentic_sim.assignment.semantic_cpu_model import semantic_features
from cpu_event_predictor import CpuEventPredictor,canonical_features
from run_gpu_proxy import predict_refitted_model

def action_input(action):
    if not isinstance(action,str) or not action.strip():
        raise ValueError('a nonempty current action is required')
    raw=enrich_row(dict(asdict(extract_tool_features(action)),action=action))
    return {'original_class':raw['operation_class'],
            'features':canonical_features(raw,semantic_features(action,'')),
            'feature_status':'ok'}

def predict(request):
    if not isinstance(request,dict) or set(request)!={'target','inputs'}:
        raise ValueError('request must contain exactly target and inputs')
    target=request['target'];inputs=request['inputs']
    if not isinstance(inputs,dict):raise ValueError('inputs must be an object')
    if target=='historical_cpu_tool_wall':
        if set(inputs)!={'action'}:raise ValueError('CPU accepts only the current action')
        row=action_input(inputs['action'])
        payload=json.loads((BASE/'cpu/class_hybrid_model.json').read_text())
        candidate=payload['class_candidate_map'].get(row['original_class'],'coarse_class_median')
        predicted=CpuEventPredictor.from_mapping(payload['models'][candidate]).predict(row)
    elif target=='historical_gpu_request_proxy_wall':
        if inputs:raise ValueError('selected GPU proxy baseline is feature-free')
        payload=json.loads((BASE/'gpu_lifecycle/gpu_proxy_chosen_model.json').read_text())
        predicted=predict_refitted_model(payload,{})
    elif target=='historical_start_known_e2e':
        if set(inputs)-{'repository'}:raise ValueError('E2E accepts only start-known repository')
        payload=json.loads((BASE/'e2e/candidate.json').read_text())
        predicted=payload['repository_ms'].get(inputs.get('repository',''),payload['global_ms'])
    else:
        raise ValueError('unsupported target: native phases, atomic CPU operations and lifecycle have no independently calibrated model here')
    return {'target':target,'predicted_ms':predicted,'status':'offline_candidate_only',
            'hardware_transfer':'unvalidated; unchanged-coefficient control only'}

if __name__=='__main__':
    print(json.dumps(predict(json.load(sys.stdin)),sort_keys=True))
