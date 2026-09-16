"""One fixed estimator comparison on the repaired command cohort."""
import json
from collections import defaultdict
from pathlib import Path
from compare import HERE, SemanticCpuModel, load_rows, metrics, sha, paired_gain, read


def main():
    rows, provenance = load_rows()
    domains = defaultdict(list)
    for row in rows:
        domains[(row['target_boundary'], row['hardware_domain'])].append(row)
    predictions = []
    for group in domains.values():
        for fold in sorted({r['fold'] for r in group}):
            train = [r for r in group if r['fold'] != fold]
            models = {c:SemanticCpuModel(center=c).fit(train) for c in ('median','gate')}
            for row in group:
                if row['fold'] != fold:
                    continue
                inputs = {k:row[k] for k in ('action','repository','operation_class')}
                predictions.append({k:row[k] for k in ('instance_id','case_id','event_id','fold','observed_ms','operation_class')} |
                                   {'predictions_ms':{c:m.predict(inputs) for c,m in models.items()}})
    results = {c:metrics(predictions,c) for c in ('median','gate')}
    baseline, candidate = results['median'], results['gate']
    winner = 'gate' if (candidate['within25'] > baseline['within25']
                        and candidate['equal_instance_within25'] >= baseline['equal_instance_within25']
                        and candidate['worst_error_pct'] <= baseline['worst_error_pct']) else 'median'
    report = {'metrics':results,'selected':winner,'code_sha256':sha(Path(__file__)),
              'scope':'fixed grouped development; semantic action only; final evaluation not accessed',
              'by_operation':{op:{c:metrics([r for r in predictions if r['operation_class']==op],c)
                                     for c in ('median','gate')} for op in sorted({r['operation_class'] for r in predictions})}}
    report['gate_minus_median_bootstrap'] = paired_gain([
        {**r, 'predictions_ms':{'semantic':r['predictions_ms']['gate'], 'coarse':r['predictions_ms']['median']}}
        for r in predictions])
    artifact = read(HERE/'fit_artifact.json')
    artifact.update(model=SemanticCpuModel(center=winner).fit(rows).to_mapping(),
                    development_metrics=results[winner], refinement=report)
    (HERE/'refined_fit_artifact.json').write_text(json.dumps(artifact,indent=2)+'\n')
    (HERE/'center_report.json').write_text(json.dumps(report,indent=2)+'\n')
    (HERE/'center_predictions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in predictions))
    print(json.dumps({k:v for k,v in report.items() if k!='by_operation'},indent=2))


if __name__ == '__main__': main()
