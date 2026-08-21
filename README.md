# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository contains the cloud-ready
bootstrap and state machinery plus a compact, measured first-control record
from a gated Lightning H100 session. The Lambda H100 remains the target
platform; Lightning measurements are kept explicitly separate and are never
presented as Lambda results.

## Current status

- G4 local trajectory-contract review: local static PASS; the original
  Lightning control was invalidated as an empty-patch runtime defect, then the
  fixed Lite control resolved at both 30- and 50-call settings. The fixed Lite
  gold and Verified runs remained unresolved.
- Shared clock, resolved vLLM configuration, artifact v2, and four-knob
  command contracts passed local validation and independent review
- Linux x86-64 rehearsal and first-trajectory inventory are free/local checks;
  unavailable host tools are reported explicitly and strict CI fails closed
- One paid Lightning H100 measurement window has completed, including pinned
  gold smokes, paired thin runs, and fixed Lite/Verified official evaluations.
  The two resolved Lite patches contain scratch/debug files, so this is not a
  clean-submission or resolved-rate claim; no sweeps have been launched.
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
`scripts/validation/validate_normalized_trajectory.py`. Reset-safe interval
accounting and a payload-free first-control summary are implemented locally;
empirical thin telemetry and the four sweeps remain deferred until a fresh
paid-session authorization.

See `docs/assignment_traceability.md` for the frozen deliverable-to-evidence
map and `project/PUBLIC_REFERENCE_LOCK.json` for the comparison-only public
reference lock. No public result is claimed by either file.

For independent post-control throughput, see
[`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md).
It keeps one vLLM/SWE-agent worker per isolated GPU and writes deterministic,
resume-safe shards without changing the single-control contract.
