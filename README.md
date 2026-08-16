# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository starts with the cloud-ready
bootstrap and state machinery; empirical SWE-agent/SWE-bench results are added
only after the Lambda H100 gates pass.

## Current status

- CR13 pre-H100 hardening: local static PASS; H100 validation pending
- Shared clock, resolved vLLM configuration, artifact v2, and four-knob
  command contracts passed local validation and independent review
- Linux x86-64 rehearsal and first-trajectory inventory are free/local checks;
  unavailable host tools are reported explicitly and strict CI fails closed
- No paid compute launched
- No final empirical result exists
- Technical readiness does not authorize paid compute; the session gate remains
  fail-closed until the user supplies explicit authorization.

## First local checks

```bash
python3 scripts/doctor.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The Lambda runbook is at `cloud/lambda/RUNBOOK.md`. It is intentionally
idempotent and does not contain credentials. The first paid session runs the
gold smokes and one uninstrumented control trajectory only; thin telemetry and
the four sweeps are deferred until the raw trajectory contract is reviewed.

See `docs/assignment_traceability.md` for the frozen deliverable-to-evidence
map and `project/PUBLIC_REFERENCE_LOCK.json` for the comparison-only public
reference lock. No public result is claimed by either file.
