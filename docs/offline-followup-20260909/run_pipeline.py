#!/usr/bin/env python3
"""Bounded offline regeneration. No acquisition, inference, or held-out scoring."""
import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / 'offline-deliverables-20260909'
PROOF = Path('/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + '\n')


def default_plan():
    roots = sorted({str(p.parent) for p in PROOF.rglob('case_spec.json')
                    if not any(x in {'history', 'source-v8', 'execution_inputs'} for x in p.relative_to(PROOF).parts)})
    return {'inventory_case_roots': roots,
            'cases': [{'case_root': str(PROOF / 'combined-case-v8'),
                       'purpose': 'descriptive_confirmation', 'attempt_id': 'attempt-001'}]}


def select_cases(plan, inventory):
    if inventory['errors']:
        raise ValueError('metadata inventory rejected: ' + repr(inventory['errors']))
    by_root = {r['case_root']: r for r in inventory['cases']}
    selected, seen = [], set()
    for entry in plan.get('cases', []):
        root = str(Path(entry['case_root']).resolve())
        if root in seen:
            raise ValueError('duplicate requested case_root')
        seen.add(root)
        row = by_root[root]
        partition = row['derived_partition']
        purpose = entry.get('purpose', 'calibration')
        if partition == 'train_calibration':
            pass
        elif partition == 'confirmation_development_excluded' and purpose == 'descriptive_confirmation':
            pass
        else:
            raise ValueError('partition/purpose prohibits journal access: ' + partition)
        selected.append(dict(row, purpose=purpose, attempt_id=entry.get('attempt_id')))
    # Inventory discovers arrivals; only explicitly listed cases open journals.
    # This avoids choosing among retries/configurations implicitly.
    identities = [(r['instance_id'], r['case_id'], r['attempt_id']) for r in selected]
    if len(set(identities)) != len(identities):
        raise ValueError('duplicate case/attempt identity')
    return selected


def run(plan, output_dir):
    out = Path(output_dir).resolve()
    if out.exists():
        raise ValueError('output directory must be new to prevent stale success artifacts')
    out.mkdir(parents=True)
    try:
        save(out / 'plan.json', plan)
        calibration = load('followup_calibration', HERE / 'calibration/calibrate_repaired_d9.py')
        ledger = load('followup_ledger', HERE / 'ledger/validate_ledger.py')
        roots = list(dict.fromkeys(plan.get('inventory_case_roots', []) + [e['case_root'] for e in plan.get('cases', [])]))
        inventory = calibration.inventory(roots)
        save(out / 'identity_inventory.json', inventory)
        selected = select_cases(plan, inventory)
        reports, normalized = [], []
        for i, row in enumerate(selected):
            report = ledger.validate_case(row['case_root'], out / 'ledgers' / f'{i:03d}', row['attempt_id'])
            if report['validation']['status'] != 'valid':
                raise ValueError('ledger validation failed; see diagnostic report: ' + row['case_id'])
            if report['identity'] != {k: row[k] for k in ('instance_id', 'case_id')}:
                raise ValueError('ledger identity differs from gated inventory')
            spec = Path(row['case_root']) / 'case_spec.json'
            if sha(spec) != row['case_spec_sha256']:
                raise ValueError('case_spec changed after partition gate')
            report['derived_partition'] = row['derived_partition']
            reports.append(report)
            events = Path(report['case_event_records']['path'])
            validation = Path(report['validation_report_path'])
            normalized.append({k: row[k] for k in ('case_root', 'instance_id', 'case_id')} |
                              {'events_path': str(events), 'events_sha256': sha(events),
                               'validation_report_path': str(validation), 'validation_report_sha256': sha(validation)})
        manifest = {'cases': normalized}
        save(out / 'normalized_manifest.json', manifest)
        result = calibration.calibrate(manifest, out / 'calibration')
        if result.get('input_errors'):
            raise ValueError('calibration input contract failed: ' + repr(result['input_errors']))
        stats = load('followup_statistics', HERE / 'statistics/bounded_uncertainty.py')
        stats.run(out / 'statistics')
        renderer = load('followup_renderer', HERE / 'render_outputs.py')
        tables = renderer.render(reports, out / 'validated_figures')
        historical = load('followup_historical_figures', PREVIOUS / 'figures/build_figures.py')
        historical.OUT = out / 'retained_figures'
        historical.OUT.mkdir(parents=True)
        historical.main()
        status = {'status': 'analysis_complete_calibration_pending' if result['disposition'] == 'pending_no_fit' else 'analysis_complete_supported_calibration',
                  'calibration_disposition': result['disposition'], 'validated_cases': len(reports),
                  'derived_outputs': tables, 'd9_compliance': 'not_established',
                  'acquisition_changed': False, 'gpu_inference_used': False,
                  'eligible_inventory_cases': sum(r['derived_partition'] == 'train_calibration' for r in inventory['cases'])}
        save(out / 'pipeline_report.json', status)
        files = sorted(p for p in out.rglob('*') if p.is_file())
        code = sorted(p for p in HERE.rglob('*.py') if not p.is_relative_to(out))
        save(out / 'artifact_manifest.json', {'outputs': {str(p.relative_to(out)): sha(p) for p in files},
                                             'analysis_code': {str(p.relative_to(HERE)): sha(p) for p in code},
                                             'historical_renderer_sha256': sha(PREVIOUS / 'figures/build_figures.py')})
        return status
    except Exception as exc:
        save(out / 'pipeline_failure.json', {'status': 'failed', 'error': str(exc), 'outputs_are_not_accepted': True})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.manifest.read_text()) if args.manifest else default_plan()
    print(json.dumps(run(plan, args.output_dir), sort_keys=True))


if __name__ == '__main__':
    main()
