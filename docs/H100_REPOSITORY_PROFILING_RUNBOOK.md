# H100 repository-profiling runbook

This runbook completes the full H100 evidence needed for assignment Steps 1-3.
It preserves the existing H100 result package. The previous GCP VM disk was
wiped, so a new persistent-storage-backed H100 run is required unless an
independent snapshot or export exists.

## Required assignment outputs

The H100 evidence must support:

- Lite and Verified resolved rate and average end-to-end wall latency;
- deterministic repository categories;
- a sample-level category versus CPU-to-GPU latency-ratio plot;
- category-level resolved-rate versus E2E, resolved-rate versus ratio, and E2E
  versus ratio plots;
- four hyperparameter sweeps, the same three views for each sweep, and one
  combined summary;
- one highest-ratio trajectory with a complete E2E breakdown, every tool event,
  every model request, and token/context information.

The primary ratio is:

```text
sum(tool-call wall_ms) / sum(model-request wall_ms)
```

Tool-call wall time and model-request wall time must be measured on the same
SWE-agent trajectory. This is not CPU activity divided by CUDA activity.

## Historical disk status

The historical source location was:

```text
instance: instance-20260822-182111
root: /home/jasonrivera691/eic-work
```

Known candidate locations include:

```text
/home/jasonrivera691/eic-work/data/raw
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-batch03-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-batch03-6
```

Use `project/GCP_H100_PROGRESS.json` for the historical batch IDs and
`project/GCP_H100_MEASUREMENTS.json` for the historical trajectory, evaluator,
and worker-state paths. These paths are not present after the disk wipe. Audit
them only if an independent snapshot or export is available.

From the repository root:

```bash
python3 scripts/assignment/audit_h100_recovery.py \
  --root /home/jasonrivera691/eic-work \
  --json-out /tmp/h100-recovery-audit.json
```

The scanner is bounded and read-only. It reports, per candidate record, whether
E2E timing, tool events, model requests, token counts, and official outcomes
appear recoverable. Without an external backup, proceed directly to the new
full acquisition.

Copy compact recoverable files into a new external root and preserve source
paths and SHA-256 hashes. Do not mutate raw batch directories. Do not commit
model caches, profiler reports, credentials, or large logs.

## What can be recovered and what cannot be assumed

Potentially recoverable:

- E2E latency from worker state, run boundaries, or timestamped agent logs;
- tool operation and duration records from SWE-agent trajectories;
- model request wall time and token counts from request-proxy or sufficiently
  detailed vLLM logs;
- official resolution outcomes already represented in compact exports;
- exact source hashes and run/configuration identity.

Not safe to assume:

- every compact population row has a matching raw trajectory;
- every trajectory has request-level model timing;
- vLLM aggregate latency equals the sum of model requests for one trajectory;
- strace CPU time equals tool-call wall time;
- Kineto CUDA activity equals model-request wall time;
- a batch start/end timestamp isolates one task when workers overlap.

If a recovered row lacks either phase sum, it cannot contribute to the primary
ratio. Preserve it as partial evidence rather than filling values from another
run.

## Existing evidence boundary

The repository already preserves:

- 64 compact population outcomes: 32 Lite and 32 selected Verified;
- 16 measured one-instance sweep cells;
- one detailed H100 Astropy trajectory with 31 model requests, token counts,
  request wall timing, and direct Kineto device activity;
- H100 strace/tool, process-attribution, service-calibration, controlled
  Kineto, and sealed feature-only evidence.

This establishes selected-cohort outcomes and several bounded case studies. It
does not establish population average E2E, population category ratios,
full-suite public-scoreboard reproduction, or Deliverable 9 event/E2E
prediction accuracy.

## Targeted live H100 acquisition

Launch H100 compute only after recovered rows have been normalized and matched
to the deterministic plan. Reuse the pinned assets:

- `Qwen/Qwen3-Coder-30B-A3B-Instruct` and revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`;
- SWE-agent revision `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`;
- SWE-bench revision `726c5461e2ef52d83cf1ea2107870a8bb3328d57`;
- vLLM 0.10.0 and the recorded pinned image/runtime;
- one H100 80 GB, concurrency 1, deterministic request construction, and
  explicit per-case/global deadlines.

Run non-measuring checks before starting a server:

```bash
./start.sh --dry-run
./start.sh --check-only --require-gpu

bash scripts/cloud/start_h100.sh \
  --manifest /mnt/eic-work/h100-startup.env \
  --backend auto \
  --dry-run
```

Plan the assignment matrix outside the repository:

```bash
python3 scripts/assignment/plan_matrix.py \
  --config configs/assignment_steps_1_3.json \
  --lite-tasks /absolute/path/manifests/lite.jsonl \
  --verified-tasks /absolute/path/manifests/verified.jsonl \
  --output /mnt/eic-work/assignment/plan.jsonl \
  --sha256-sidecar /mnt/eic-work/assignment/plan.jsonl.sha256
```

The checked-in planner does not execute workloads. Use a reviewed live runner
that consumes this plan serially and records resumable state. Do not launch from
an improvised shell loop that loses per-case identity or deadlines.

The live acquisition target is exact and resume-based:

1. Step 1: every unmatched baseline row from the declared Lite and Verified
   manifests.
2. Step 2: every unmatched plan sweep row. The current plan contains exactly
   288 non-baseline sweep rows in addition to reused baselines.
3. Step 3: the highest primary-ratio eligible baseline after Step 1 is audited;
   run one targeted repeat only if the selected row lacks complete event data.

## Required measured boundaries

For each trajectory, retain:

```text
run_id
suite
repository
category
instance_id
config_id and sweep value
repeat_id
official submitted/resolved outcome
trajectory start/end and e2e_wall_ms
every tool event and tool wall_ms
every model request and model-request wall_ms
input_tokens, output_tokens, context_tokens
hardware, model, tokenizer, SWE-agent, SWE-bench, vLLM metadata
command, source, plan, trace, and output hashes
failure or unavailable reason
```

The primary aggregates are:

```text
tool_wall_ms  = sum(completed tool-event wall_ms)
model_wall_ms = sum(completed model-request wall_ms)
tool_model_ratio = tool_wall_ms / model_wall_ms
```

E2E wall time is measured independently and must be at least the serialized
phase time within a documented tolerance. Any unaccounted remainder stays
visible as orchestration/other overhead.

## Secondary diagnostics

Nsight Systems, Kineto, and CUPTI-derived fields are valuable when available:

```text
cpu_activity_union_ms
cuda_activity_union_ms
kernel_duration_sum_ms
request_start_mono_ns
request_end_mono_ns
clock_id
raw trace path and SHA-256
```

They explain activity inside model-request windows and validate provenance.
They are not required to calculate the assignment's primary ratio and must not
be used as substitutes for tool-call or model-request wall time. Sampled NVML
utilization and process overlap remain attribution diagnostics, not elapsed GPU
latency.

## Normalize, compile, and plot

Normalize each completed raw run:

```bash
python3 scripts/assignment/ingest_sweagent_run.py \
  --run-spec /absolute/path/raw/RUN_ID/run_spec.json \
  --trajectory /absolute/path/raw/RUN_ID/trajectory.json \
  --model-events /absolute/path/raw/RUN_ID/model_events.jsonl \
  --runner-summary /absolute/path/raw/RUN_ID/runner_summary.json \
  --evaluator-result /absolute/path/raw/RUN_ID/evaluator_result.json \
  --runtime-manifest /absolute/path/assignment-runtime.json \
  --case-result /absolute/path/raw/RUN_ID/case_result.json \
  --output-dir /mnt/eic-work/assignment/normalized/RUN_ID
```

`evaluator_result.json` must be produced by the reviewed official SWE-bench
adapter. Manual submitted/resolved flags, agent exit status, and log text are
not accepted as outcome evidence. Every raw source must also match the completed
case result's hashed artifact inventory, and both the runtime manifest and case
result require exact SHA-256 sidecars.

Compile all normalized runs:

```bash
python3 scripts/assignment/compile_dataset.py \
  --runs-root /mnt/eic-work/assignment/normalized \
  --output-dir /mnt/eic-work/assignment/compiled
```

Generate assignment figures:

```bash
python3 scripts/assignment/generate_step_figures.py \
  --trajectories /mnt/eic-work/assignment/compiled/trajectories.csv \
  --tool-events /mnt/eic-work/assignment/compiled/tool_events.csv \
  --model-events /mnt/eic-work/assignment/compiled/model_events.csv \
  --sweep-runs /mnt/eic-work/assignment/compiled/sweep_runs.csv \
  --reconciliation-report /mnt/eic-work/assignment/reconciliation.json \
  --output-dir /mnt/eic-work/assignment/figures
```

The assignment-level simulator freezes predictions before label reveal:

```bash
PYTHONPATH=src python3 scripts/assignment/evaluate_predictions.py fit-freeze \
  --calibration /absolute/path/event-calibration.json \
  --holdout-features /absolute/path/event-holdout-features.json \
  --prediction-manifest /absolute/path/prediction-manifest.json

PYTHONPATH=src python3 scripts/assignment/evaluate_predictions.py score \
  --prediction-manifest /absolute/path/prediction-manifest.json \
  --holdout-labels /absolute/path/event-holdout-labels.json \
  --output /absolute/path/event-evaluation.json
```

The strict feature schemas reject measured wall/CPU/CUDA/Kineto timing and
actual output lengths. The scorer verifies the frozen SHA-256 and returns
nonzero if any available event or complete trajectory exceeds 25%. The tool is
present, but neither acceptance gate may be claimed until this protocol is run
on valid held-out complete assignment trajectories.

## Stop and preservation rules

Set the hard billing deadline before launch. Stop cleanly and preserve
resumable state for model/dataset revision drift, GPU contamination, output-root
collision, missing request boundaries, missing official evaluator output,
clock mismatch, disk exhaustion, or insufficient time to finish the current
case and flush its artifacts.

After each bounded acquisition segment:

1. stop the SWE-agent case and vLLM server cleanly;
2. flush request logs and optional profiler traces;
3. atomically update run state;
4. hash compact outputs and inventory raw external paths;
5. verify the GPU is idle;
6. stop the VM before the billing deadline.

The H100 assignment evidence is complete only when every claimed plan row is
accounted for as valid or explicitly unavailable, the Steps 1-3 figures rebuild
from hashed canonical tables, and no historical evidence has been overwritten.
