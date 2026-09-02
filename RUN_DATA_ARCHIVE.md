# Agentic Workload Simulator — Run Data Archive

**Archive date:** 2026-09-02
**Repository:** `jas0nriverah/agentic-workload-simulator`
**Branch:** `parallel-h100-shards`

This file consolidates the relevant run evidence found in the repository and
the archived Cursor conversations on this VM. It records measured results,
infrastructure preparation, known failures, and claim boundaries. Cursor chat
transcripts are not copied into the repository because they contain session
metadata and may contain machine-local paths or sensitive operational details.

## Executive status

- The repository contains a substantial, compact H100 measurement archive.
- H100 acquisition for the earlier completed measurement set is recorded as
  closed in `H100_RESULTS.md`.
- The later six-worker collection under
  `/mnt/eic-work/assignment/cpu-docker-canary-20260830-v3/results-full` was not
  completed: it produced zero valid `case_result.json` files and no
  `final_status.json`.
- A 1,088-case authoritative plan was found on the mounted filesystem. A
  deterministic 16-way preparation was generated offline, with 68 cases per
  shard. This did not boot GPUs or modify the incomplete six-worker results.
- PACE/VPN control-plane access was unavailable during the latest continuation
  attempt, so no new H100 workers were safely provisioned.
- The current repository’s uncommitted work consists of an outcome-only
  baseline figure, its generator and test, and a project autonomy rule. These
  are included in the commit associated with this archive.

## Measured H100 archive

The primary source is `H100_RESULTS.md`; machine-readable evidence is under
`project/h100_results/`.

### Canonical evaluation cohorts

| Suite | Selected | Completed | Resolved | Unresolved | Resolved rate |
|---|---:|---:|---:|---:|---:|
| Lite | 32 | 32 | 8 | 24 | 25.0000% |
| Verified | 32 | 29 | 10 | 19 | 34.4828% of completed |

For Verified, the selected-cohort rate is 10/32 = 31.25%. The completed-case
rate excludes incomplete and empty-patch attempts. Canonical outcomes come
from the official evaluator and
`project/h100_results/canonical_results.json`.

The cohorts cover 11 Lite repositories and 12 Verified repositories. Detailed
counts are in `project/h100_results/repository_coverage.csv`.

### Frozen runtime

- Hardware: one Google Cloud NVIDIA H100 80 GB HBM3
- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`
- Model revision:
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`
- Precision: BF16
- vLLM: 0.10.0 with the Qwen3 Coder tool parser
- SWE-agent revision:
  `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`
- SWE-bench revision:
  `726c5461e2ef52d83cf1ea2107870a8bb3328d57`
- One GPU, tensor parallelism 1, concurrency 1

### Additional measured evidence

- All four Modal sensitivity axes have endpoint coverage:
  model call limit, maximum output tokens, observation length, and
  temperature. The detailed one-instance results are in
  `project/MODAL_LITE_SWEEP_MEASURED.json`.
- The controlled Kineto simulator matrix reports 10.7156% holdout MAPE,
  below the 25% target for that narrow measured-phase matrix.
- The sealed feature-validation artifact reports 1.2476338% primary case-median
  wall MAPE with 100% holdout case coverage.
- One real SWE-agent trajectory contains 31 serialized HTTP 200 requests.
  Its overlap-aware Kineto CUDA activity union is 56.0637 seconds within
  235.8545 seconds of request-wall time.
- Process/NVML samples overlap all 31 request windows and all 31 vLLM-worker
  windows. These are sampled overlap/utilization measurements, not exact GPU
  seconds.
- The NCU probe was blocked by `ERR_NVGPUCTRPERM`; no hardware-counter metrics
  are claimed.

## Later six-worker collection

The most recent Cursor work attempted a six-worker full collection before
planning a 16-H100 expansion.

### Intended setup

- Workers `00`–`05`, one trajectory at a time per GPU
- Unique worker shards, proxy ports, Podman sockets, caches, endpoints, and
  output roots
- Resume existing work rather than delete or overwrite artifacts
- Preserve raw logs, manifests, failed attempts, and hashes
- Continue until the final audit writes `final_status.json`

### What worked

- Six H100 allocations were launched during the prior session.
- Worker-specific isolation and resume keys were established.
- The continuation/runtime fixes were committed from an alternate checkout.
- The authoritative output tree and failed-attempt logs remained preserved.

### What failed or remained unproven

- An initial failure involved overwritten Docker image metadata.
- A subsequent failure involved a stale reviewed-runner digest.
- Those infrastructure defects were patched, but the corrected path was not
  proven end-to-end before the control plane became unavailable.
- The last preserved state showed workers running with many failures, zero
  completed cases, zero `case_result.json` artifacts, and no
  `final_status.json`.
- Worker/process startup and endpoint health were therefore not sufficient
  evidence of valid data collection.

The later six-worker attempt must not be merged into the completed H100
denominators above.

## Offline 16-way preparation

The mounted filesystem contained the authoritative 1,088-row plan and its
SHA-256 sidecar. Offline preparation generated:

- 16 deterministic, non-overlapping shards
- 68 cases per shard
- Coverage manifest: `/mnt/eic-work/assignment/shards-16.json`
- Shard directory:
  `/mnt/eic-work/assignment/shards-16/`
- Validated runtime manifest:
  `/mnt/eic-work/assignment/cpu-docker-canary-20260830-v3/runtime-manifest-16.json`

These mounted files are not part of this Git repository. They are listed here
as provenance and must be revalidated before any future paid run.

The required execution gates remain:

1. Restore approved VPN/SSH access to PACE.
2. Verify all 16 allocations, endpoint model IDs, health, metrics, and actual
   worker-to-GPU mappings.
3. Run a disjoint canary that produces request events, a trajectory,
   `case_result.json`, evaluator output, and matching hashes.
4. Only then launch the full 16-way collection.
5. Audit exact coverage and provenance before declaring analysis readiness.

## Archived Cursor chat findings

Relevant archived conversations were found under the VM’s Cursor chat and
agent-transcript directories. The useful history is:

- **H100 Canary Run:** initial single-case H100 validation.
- **CPU Controller Evaluator / CPU Control Plane:** CPU-side setup, evaluator,
  SSH control-plane, tunnel, and endpoint diagnostics.
- **Clone Workload Simulator:** repository setup and baseline-figure work.
- **Slurm Canary Dispatch:** six-H100 concurrent collection, worker isolation,
  runtime fixes, and continuation monitoring.
- **Scale H100 Collection / Boot 16 H100 Run:** the proposed 16-worker plan,
  offline shard generation, and the PACE connectivity blocker.

The chats contain both plans and status reports. The status reports are
treated as evidence only where they agree with repository artifacts or
preserved run files. In particular, “workers running” was not treated as
successful data collection.

## Current repository additions

The current uncommitted work found before this archive was:

- `scripts/assignment/generate_baseline_figure.py`
- `tests/assignment/test_generate_baseline_figure.py`
- `project/h100_results/figures/baseline_resolved_rate.svg`
- `project/h100_results/figures/baseline_resolved_rate.json`
- `.cursor/rules/project-autonomy.mdc`

The baseline figure is intentionally outcome-only: 23 repository rows,
11 Lite and 12 Verified. It contains no timing data and makes no full-suite
claim.

## Claim boundaries and exclusions

Do not claim that the available evidence establishes:

- a universal SWE-agent resolved rate or public-scoreboard reproduction;
- a population-average end-to-end latency;
- a broad CPU-to-GPU latency ratio or causal GPU ownership;
- exact GPU seconds from NVML samples;
- NCU kernel metrics;
- blind simulator prediction on unseen agent trajectories;
- cross-hardware generalization, energy, or dollar efficiency;
- completion of the later 1,088-case six-worker collection;
- a requirement that Verified must have 30 completed cases.

Credentials, model weights, raw multi-gigabyte traces, machine-local logs,
provider authorization files, caches, and mounted experiment trees are
deliberately not copied into Git. Their paths and compact hashes are retained
in the tracked manifests where available.

## Reproduction references

- `H100_RESULTS.md`
- `CURSOR_HANDOFF.md`
- `docs/ASSIGNMENT_COMPLETION_RUNBOOK.md`
- `project/h100_results/canonical_results.json`
- `project/h100_results/source_inventory.csv`
- `project/h100_results/claim_provenance.csv`
- `project/h100_results/exclusions.csv`
- `project/h100_results/kineto_matrix.csv`
- `project/h100_results/sweep_results.csv`
