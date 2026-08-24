# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository contains the cloud-ready
bootstrap and state machinery plus measured controls from Lightning/Modal and
a completed Google Cloud A3 H100 acquisition. Provider-specific measurements remain
explicitly separate and are never presented as interchangeable results.

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
- One paid Lightning H100 measurement window, subsequent Modal H100
  measurements, and a Google Cloud H100 measurement window are recorded
  separately. Raw model patches may contain
  scratch files; clean derived submissions are explicitly labeled as derived.
- Deep profiling includes syscall-level file events, process/NVML sampling, and
  one direct 31-request Astropy Kineto trajectory. A controlled four-row
  calibration/two-row holdout matrix produced 10.715632% limited-scope E2E
  phase-reconstruction MAPE. NCU counters remain permission-blocked, and the
  result is not an individual-event or unseen-workload prediction claim.
- The audited H100 result is frozen in `H100_RESULTS.md`; plot-ready tables,
  provenance, exclusions, and a deterministic inventory are under
  `project/h100_results/`.

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

The GCP H100 setup and bounded pilot are documented in
[`cloud/gcp/RUNBOOK.md`](cloud/gcp/RUNBOOK.md). Measured GCP evidence is
indexed in `project/GCP_H100_PROGRESS.md` and
`project/GCP_H100_REQUEST_PROFILE_20260823.json`. Request-aware profiled attempts use
`scripts/observability/request_proxy.py`, which records timing and hashes
without storing prompts or responses.

The simulator implementation is `src/agentic_sim/simulator.py` and the
offline driver is `scripts/analysis/evaluate_simulator.py`. It requires an
explicit measured/calibrated CPU/GPU phase decomposition and rejects aggregate
vLLM counters or GPU utilization as fake per-request GPU time. The measured
controlled-matrix holdout is documented with its narrow validity boundary in
`H100_RESULTS.md`.

See `docs/assignment_traceability.md` for the frozen deliverable-to-evidence
map and `project/PUBLIC_REFERENCE_LOCK.json` for the comparison-only public
reference lock. No public result is claimed by either file.

Use [`docs/REPORT_TEMPLATE.md`](docs/REPORT_TEMPLATE.md) for the final offline
write-up and source every empirical number from the canonical package.

For independent post-control throughput, see
[`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md).
It keeps one vLLM/SWE-agent worker per isolated GPU and writes deterministic,
resume-safe shards without changing the single-control contract.
