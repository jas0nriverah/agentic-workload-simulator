# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository contains the cloud-ready
bootstrap and state machinery plus a compact, measured first-control record
from a gated Lightning H100 session. The Lambda H100 remains the target
platform; Lightning measurements are kept explicitly separate and are never
presented as Lambda results.

## Current status

- Modal control and sweep evidence: the full-prompt Lite control resolved
  officially, and a source-only derived patch also resolved. The original
  Verified control was unresolved, but a final targeted LLM control produced a
  clean one-file patch that resolved officially; all outcomes are preserved in
  separate measured manifests. Four one-instance sweep endpoint sets are now
  measured.
- Shared clock, resolved vLLM configuration, artifact v2, and four-knob
  command contracts passed local validation and independent review
- Linux x86-64 rehearsal and first-trajectory inventory are free/local checks;
  unavailable host tools are reported explicitly and strict CI fails closed
- One paid Lightning H100 measurement window and subsequent authorized Modal
  H100 measurements are recorded separately. Raw model patches may contain
  scratch files; clean derived submissions are explicitly labeled as derived.
- Deep profiling now includes two additional H100 samples with syscall-level
  file events and aggregate vLLM token/request snapshots. The Verified sample
  resolved cleanly; the Lite sample remains unresolved/non-clean. Per-request
  CPU/GPU correlation and simulator holdout validation remain pending; sweep
  SVG figures are generated under `project/figures/`.

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

Use [`docs/REPORT_TEMPLATE.md`](docs/REPORT_TEMPLATE.md) as the evidence-gated
write-up structure once the pending H100 measurements exist; it is deliberately
not populated with speculative numbers.

For independent post-control throughput, see
[`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md).
It keeps one vLLM/SWE-agent worker per isolated GPU and writes deterministic,
resume-safe shards without changing the single-control contract.
