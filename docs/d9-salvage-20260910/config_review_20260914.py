"""Read-only training evidence review of sampling and context handling."""
import collections
import hashlib
import importlib.util
import json
from pathlib import Path
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('partition_gate', ROOT / 'docs/offline-followup-20260909/calibration/calibrate_repaired_d9.py')
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def main():
    manifest_path = HERE / 'evidence/calibration_input_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    partitions, _ = gate._load_pinned_partitions()
    output = []
    for case in manifest['cases']:
        root = Path(case['case_root'])
        instance, case_id, _ = gate._case_spec_identity(root)
        assert partitions[instance] == 'train_calibration'
        assert instance == case['instance_id'] and case_id == case['case_id']
        attempt = root / 'runner_attempts/attempt-001'
        config_path = attempt / 'run_batch.config.yaml'
        cfg = yaml.safe_load(config_path.read_text())
        cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
        model = cfg['agent']['model']
        native = json.loads((attempt / 'native_serving/native_evidence_manifest.json').read_text())
        epoch = native['counter_epoch']
        preparation = gate.PROOF_ROOT / 'comparison-resume-20260909-v1/runtime-preparation-v1/worker-inputs'
        fp_path = preparation / epoch / 'serving_fingerprint.json'
        fp = json.loads(fp_path.read_text()) if fp_path.is_file() else {}
        defaults = fp.get('server_sampling_defaults', {}).get('chat', {})
        observed = {}
        for key in ('temperature', 'top_p', 'top_k', 'repetition_penalty'):
            item = defaults.get(key, {})
            if item.get('status') != 'startup_observed':
                observed[key] = None
                continue
            source = item['source']
            assert sha(source['locator']) == source['sha256']
            observed[key] = item['value']
        effective = dict(observed)
        for key in effective:
            if model.get(key) is not None:
                effective[key] = model[key]
            if key in model.get('completion_kwargs', {}):
                effective[key] = model['completion_kwargs'][key]
        traj_path = attempt / instance / (instance + '.traj')
        traj = json.loads(traj_path.read_text())
        proxy_path = attempt / 'request_proxy.jsonl'
        requests = [json.loads(line) for line in proxy_path.open()]
        prompts = [r['prompt_tokens'] for r in requests if isinstance(r.get('prompt_tokens'), int)]
        output.append(dict(case_id=case_id, instance_id=instance,
            effective_sampling=effective, server_defaults=observed,
            history_processors=cfg['agent']['history_processors'],
            max_input_tokens=model['max_input_tokens'], calls_limit=model['per_instance_call_limit'],
            observation_limit=cfg['agent']['templates']['max_observation_length'],
            exit_status=traj['info'].get('exit_status'), api_calls=traj['info'].get('model_stats', {}).get('api_calls'),
            max_successful_prompt_tokens=max(prompts) if prompts else None,
            request_mutations=sum(bool(r.get('request_mutation')) for r in requests),
            sources={str(p): sha(p) for p in (config_path, fp_path, traj_path, proxy_path) if p.is_file()}))
    summary = dict(cases=len(output), instances=len({r['instance_id'] for r in output}),
        exit_counts=dict(collections.Counter(r['exit_status'] for r in output)),
        history_processor_counts=dict(collections.Counter(json.dumps(r['history_processors'], sort_keys=True) for r in output)),
        effective_sampling_counts=dict(collections.Counter(json.dumps(r['effective_sampling'], sort_keys=True) for r in output)),
        high_prompt_cases=sum((r['max_successful_prompt_tokens'] or 0) >= .9*r['max_input_tokens'] for r in output),
        request_mutations=sum(r['request_mutations'] for r in output),
        exact_recommended_combination_cases=sum(r['effective_sampling']==dict(temperature=.7, top_p=.8, top_k=20, repetition_penalty=1.05) for r in output))
    result = dict(summary=summary, cases=output, manifest_sha256=sha(manifest_path),
        script_sha256=sha(__file__), split_sha256=gate.PINNED_SPLIT_SHA256,
        scope='49 repaired training cases; startup defaults plus executed client configuration; no new inference or protected outcome access')
    (HERE/'config_review_20260914.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
