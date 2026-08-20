# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository starts with the cloud-ready
bootstrap and state machinery; empirical SWE-agent/SWE-bench results are added
only after the Lambda H100 gates pass.

## Current status

- G4 local trajectory-contract review: local static PASS; the first H100
  control and official evaluator have completed with an unresolved/empty-patch
  outcome
- Shared clock, resolved vLLM configuration, artifact v2, and four-knob
  command contracts passed local validation and independent review
- Linux x86-64 rehearsal and first-trajectory inventory are free/local checks;
  unavailable host tools are reported explicitly and strict CI fails closed
- One paid H100 control session has completed; no thin-telemetry or sweep run
  has been launched
- No resolved-rate claim or final assignment result exists
- Technical readiness does not authorize paid compute; the session gate remains
  fail-closed until the user supplies explicit authorization.

## First local checks

```bash
python3 scripts/doctor.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The Lambda runbook is at `cloud/lambda/RUNBOOK.md`. It is intentionally
idempotent and does not contain credentials. The first paid control fixture is
preserved and normalized locally by
  `scripts/validation/normalize_sweagent_trajectory.py` and checked with
  `scripts/validation/validate_normalized_trajectory.py`; thin telemetry and
  the four sweeps remain deferred until a fresh paid-session authorization.

See `docs/assignment_traceability.md` for the frozen deliverable-to-evidence
map and `project/PUBLIC_REFERENCE_LOCK.json` for the comparison-only public
reference lock. No public result is claimed by either file.
