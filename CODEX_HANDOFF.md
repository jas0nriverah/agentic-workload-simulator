# Codex handoff — current GCP H100 research state

Updated: 2026-08-23 (America/New_York)
Repository: `jas0nriverah/agentic-workload-simulator`  
Working branch: `parallel-h100-shards`
Handoff base commit: `141b879` (`Persist H100 process request overlap evidence`)
Original frozen experiment-harness commit: `3fcb5f28d3a363483b097a2787c051ca9c5b4a1b`

## Read this first

The EIC assignment PDF remains authoritative. Do not redesign the experiment,
change the pinned model/runtime, fabricate unavailable measurements, or repeat
work just because an older plan lists it. Use the repository, immutable raw VM
artifacts, official evaluator outputs, and compact measured manifests as the
source of truth.

Read these files before acting:

1. `project/ASSIGNMENT_LOCK.md`
2. `docs/assignment_traceability.md`
3. `project/PROJECT_STATE.yaml`
4. `project/GCP_H100_PROGRESS.md`
5. `project/GCP_H100_GAP_AUDIT_20260823.md`
6. `project/EXPERIMENT_LEDGER.jsonl`
7. `project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json`

Do not use the older `project/HANDOFF.md` or Lightning instructions as current
GCP execution state. They are historical context only.

## Git state

- The branch was synchronized and clean at `141b879` before this handoff edit.
- Later commits after `3fcb5f2` contain narrow runtime/collection fixes,
  instrumentation, and measured evidence. Do not reset back to the frozen
  harness commit.
- Raw trajectories, large logs, caches, model weights, credentials, and VM-local
  authorization files do not belong in Git.
- Before running or committing, use `git status --short --branch` and preserve
  unrelated/untracked work.

## Live GCP target

- Project: `project-3d59272d-3213-4e06-97b`
- Zone: `us-central1-a`
- VM: `instance-20260822-182111`
- Host user: `jasonrivera691`
- Repository: `/home/jasonrivera691/agentic-workload-simulator`
- Work/artifacts: `/home/jasonrivera691/eic-work`
- GPU: one NVIDIA H100 80 GB HBM3
- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`
- Model revision: `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`
- vLLM: 0.10.0, BF16, 32K context, `qwen3_coder`, TP=1
- vLLM image digest:
  `sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`

The VM has been stopped/restarted during the session. Never assume the model
server or a worker is alive from this document: verify live state read-only.
Use an already-open SSH terminal; do not open extra VM tabs or terminals.

## Measured evidence already complete

### Official SWE-bench evaluation runs

- The tracked GCP summaries describe 34 completed Lite evaluations and 29
  completed Verified evaluations. These are run counts, not yet proven unique
  instance counts; deduplicate the live manifests before making a 30/30 claim.
- Two Verified outcomes are explicitly incomplete due reproducible environment
  failures and remain excluded from completed-evaluation counts.
- Official evaluator results include resolved, unresolved, empty-patch, and
  incomplete outcomes. Preserve all outcomes; do not clean or relabel them.
- Additional non-Astropy Lite evidence exists for `pallets__flask-5063` and
  `psf__requests-2317`.
- The compact tracked summaries explicitly prove Astropy, Flask, and Requests.
  Audit the raw batch manifests before claiming six-repository coverage.

### Four assignment sweeps

`project/MODAL_LITE_SWEEP_MEASURED.json` already contains measured one-instance
endpoint evidence for all frozen grids:

- call limit: 10, 20, 30, 50
- maximum output: 512, 1024, 2048, 4096
- observation length: 10K, 25K, 50K, 100K characters
- temperature: 0.0, 0.2, 0.5, 0.8

Do not launch more generic sweep points unless a named assignment claim is
unsupported after the full audit.

### Request, process, CPU, and GPU timing

The repaired process-attribution run is the strongest current case-study
evidence:

- 31/31 serialized request intervals overlap process-NVML samples.
- All 31 contain the vLLM worker process.
- 3,456 valid process rows and 385 vLLM-worker samples were retained.
- Worker sampled SM utilization reached about 92% and sampled memory utilization
  about 47%; device utilization reached 100%.
- Request boundaries and process samples share hostname, boot ID, and
  `CLOCK_MONOTONIC_RAW`.
- The 31 intervals contain 59,739.423 ms of request duration and 525,380 total
  tokens.

Primary evidence:

- `project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json`
- `project/GCP_H100_REQUEST_PROFILE_20260823G.json`
- `project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json`

This supports request-level model-serving/process overlap and a defensible
CPU/tool-versus-model-serving case study. It is sampled utilization evidence,
not exact CUDA kernel time or exact GPU device seconds.

### vLLM calibration

`project/GCP_H100_VLLM_CALIBRATION_20260823E.json` contains warm-H100 serving
anchors at input lengths 128, 512, and 2048, 16 prompts per point, output 64,
concurrency 1. It records TTFT/TPOT/ITL and serving latency. It does not provide
exact GPU seconds.

### Profiling capability

The bounded Nsight Compute test is complete:

- `ncu` 2025.1.1.0 is present in the container.
- Profiling fails with `ERR_NVGPUCTRPERM`.
- Host Nsight cannot cross the vLLM container namespace for CUDA tracing.
- The exact kernel-counter result is environment/permission blocked.

Do not repeat NCU attempts, reboot, reload the NVIDIA driver, change modprobe
configuration, or restart a working vLLM merely to obtain counters. The PDF
does not require NCU specifically. Report this limitation honestly.

## Truthful remaining gaps

1. **Verified count:** tracked evidence reaches 29 completed Verified runs, so
   one new unique Verified control is likely required for the frozen 30-run
   target. Confirm by deduplicating live manifests first.
2. **Repository/category diversity:** compact Git evidence explicitly proves
   only Astropy, Flask, and Requests. Audit the raw completed batch manifests;
   if fewer than six repositories are present, select the next Verified task
   from an unrepresented repository/category.
3. **Simulator target:** a defensible measured `gpu_seconds_at_reference` and a
   sealed real holdout error are not available. NVML utilization overlap must
   not be relabeled as exact GPU seconds. The existing simulator must not be
   fitted to a fabricated target.
4. **Exact kernel/device attribution:** blocked by the environment. This is a
   reporting limitation, not a reason to repeat failed profiling or change the
   driver during the experiment.
5. **Offline-only work:** aggregation, plots, report prose, simulator coding,
   visual QA, and packaging do not justify H100 runtime.

## Highest-value next H100 action

After confirming no experiment is active and deduplicating completed instance
IDs, run exactly one new, unique SWE-bench Verified control. Prefer an
unrepresented non-Astropy repository/category so the same run improves both
the Verified count and diversity evidence.

For this expensive run, establish and record:

- requirement: close the frozen Verified count and a named diversity gap;
- insufficiency: only 29 completed Verified runs are currently tracked;
- exact instance: chosen only after live-manifest deduplication;
- expected warm runtime: approximately 3–10 minutes;
- isolation: serialized one-worker control, no simultaneous trajectory;
- reuse: keep the warm pinned vLLM server;
- artifacts: batch/instance manifest, resolved configuration and hashes,
  trajectory, patch/prediction, agent log, official evaluator report/log,
  request-proxy events, vLLM metric samples, process-NVML samples, clock/host/
  boot identity, and SHA-256 inventory;
- success: new instance ID, agent return code 0, evaluator return code 0,
  official result retained, one-to-one serialized request boundaries, and all
  request intervals overlapping vLLM-worker process samples.

Use the existing batch planner and
`scripts/cloud/lambda_run_parallel_shard.py`; do not hand-edit evaluator input
or reuse an old successful attempt as a new sample. Run a dry-run first and
verify the exact instance, dataset SHA, command hashes, output root, and that
the task has not already completed.

## After that run

Immediately reassess rather than launching another generic trajectory:

1. Recompute unique Lite/Verified completed counts from immutable manifests.
2. Recompute repository/category coverage from the selected rows.
3. If 30 unique completed evaluations per split and required diversity are
   satisfied, stop launching population runs.
4. Only if the simulator requirement still needs new H100 evidence, collect a
   small predeclared warm-vLLM calibration/holdout matrix with exact token
   counts and same-clock timing. Seal the holdout rows before fitting. Never
   call utilization-integrated wall time exact device seconds.
5. Preserve raw artifacts and commit only compact manifests/summaries/hashes.

## Safe first commands in the existing SSH terminal

These checks are read-only and do not restart anything:

```bash
cd /home/jasonrivera691/agentic-workload-simulator
git status --short --branch
git rev-parse HEAD
date -u
hostname
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader
tmux ls 2>/dev/null || true
pgrep -af 'vllm|sweagent|lambda_run_parallel_shard|run-batch' || true
```

Then inventory and deduplicate completed rows from
`/home/jasonrivera691/eic-work/batches`. Do not select a new instance from the
compact count alone.

## Non-negotiable continuation rules

- Do not shut down the VM, stop vLLM, terminate an active experiment, or close
  the existing SSH/browser session merely because one batch finished.
- Do not open more VM tabs/terminals when a working SSH session already exists.
- Do not ask for routine decisions; diagnose ordinary failures and continue.
- Do not spend H100 time on prose, figures, README changes, broad cleanup, or
  packaging.
- Do not launch more temperature/call/token/observation sweeps without a named
  unsupported assignment requirement.
- Do not repeat conclusively blocked NCU/Nsight approaches.
- Before any intentional stop, perform a complete gap audit covering counts,
  diversity, request/process timing, profiling, calibration, holdout evidence,
  data quality, and raw-artifact persistence.
- Every empirical claim must point to an immutable artifact or compact manifest.
  Use `PENDING`, `NOT MEASURED`, or `BLOCKED` when that is the truth.

## Lightweight repository validation

Run before committing handoff or compact evidence changes:

```bash
git diff --check
PYTHONPATH=src:. python3 -m unittest discover -s tests
```

Do not delay a running GPU experiment to run broad local tests; checkpoint its
immutable evidence first.
