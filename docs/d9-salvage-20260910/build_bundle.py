"""Package offline simulator code, compact evidence, models and results."""
import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent

def selected_files():
    files=set()
    for rel in ('src/agentic_sim','scripts','docs/offline-deliverables-20260909','docs/d9-salvage-20260910'):
        for p in (ROOT/rel).rglob('*'):
            if not p.is_file() or '__pycache__' in p.parts or p.suffix in ('.pyc','.pyo'):continue
            if rel=='scripts' and p.suffix not in ('.py','.sh'):continue
            rp=p.relative_to(ROOT).as_posix()
            if any(s in rp for s in ('/evidence/ledgers/','/cpu_lifecycle/inputs/')):continue
            if p.name in ('crontab-before.txt','bundle_receipt.json'):continue
            files.add(p)
    for p in (ROOT/'docs/offline-followup-20260909').rglob('*.py'):
        if not any(s.startswith('output-') for s in p.parts):files.add(p)
    for rel in ('pyproject.toml','uv.lock','README.md','tests/assignment/test_d9_salvage_simulator.py'):
        p=ROOT/rel
        if p.is_file():files.add(p)
    return sorted(files)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    files=selected_files()
    manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    readme='''# Offline D9 simulator bundle\n\nStart with docs/d9-salvage-20260910/REPORT.md and simulator/README.md in that directory.\n\nQuick smoke command from the extracted root:\n\n    python3 docs/d9-salvage-20260910/simulator/run.py predict --request docs/d9-salvage-20260910/simulator/example_request.json\n\nHistorical and native model comparisons, coefficients, predictions, compact native evidence, source hashes and focused tests are included. Prediction uses the Python standard library. Optional plotting/project workflows may require dependencies declared by the original project.\n\nThis is an offline candidate bundle, not a claim of literal D9 compliance. Hardware transfer remains unvalidated. Token/cache models are conditional on supplied workload descriptors.\n\nLarge CPU binaries and repeated per-case reconstruction ledgers are retained externally and are not included. Full raw-evidence reconstruction and CPU manifest regeneration require those original artifacts; their provenance paths and hashes remain recorded. Compact native and historical comparison inputs are included. PACKAGED_FILES.json binds the exact included source/results.\n'''
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with tarfile.open(args.output,'w:gz',compresslevel=6) as tar:
        for p in files:tar.add(p,arcname='d9-offline/'+str(p.relative_to(ROOT)),recursive=False)
        for name,payload in [('PACKAGED_FILES.json',json.dumps(manifest,indent=2).encode()),('D9_BUNDLE_README.md',readme.encode())]:
            item=tarfile.TarInfo('d9-offline/'+name);item.size=len(payload);item.mode=0o644;tar.addfile(item,io.BytesIO(payload))
    digest=hashlib.sha256(args.output.read_bytes()).hexdigest()
    receipt={'archive':str(args.output.resolve()),'sha256':digest,'bytes':args.output.stat().st_size,'source_files':len(files),
             'scope':'Runnable offline candidate bundle; external large raw CPU evidence is not packaged.'}
    (HERE/'bundle_receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))

if __name__=='__main__':main()
