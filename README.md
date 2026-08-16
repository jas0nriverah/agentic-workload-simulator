# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository starts with the cloud-ready
bootstrap and state machinery; empirical SWE-agent/SWE-bench results are added
only after the Lambda H100 gates pass.

## Current status

- G0: passed locally
- LC0, LC1, LC3: locally implemented and validated
- LC2, LC4, LC5: pending root integration/review
- No paid compute launched
- No final empirical result exists

## First local checks

```bash
python3 scripts/doctor.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The Lambda runbook is at `cloud/lambda/RUNBOOK.md`. It is intentionally
idempotent and does not contain credentials. Do not publish or push this
repository until the owner explicitly approves it.
