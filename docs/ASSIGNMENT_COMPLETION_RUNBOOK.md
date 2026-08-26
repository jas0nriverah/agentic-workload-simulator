# Assignment completion runbook

This runbook is the assignment-facing path for completing Steps 1-3 and
Deliverable 9. It does not replace, rename, or reinterpret the historical H100
evidence. Existing feature-only, Kineto, process-attribution, and recovery
artifacts remain useful, but they answer narrower questions than the assignment.

## Full-submission recommendation

The target is the complete assignment, not a reduced or illustrative cohort.
Step 1 must cover all 300 SWE-bench Lite tasks and all 500 SWE-bench Verified
tasks if the report claims public-scoreboard reproduction. Every accepted row
must include the official evaluator outcome, end-to-end wall time, complete
tool-event timing, complete model-request timing, the same-trajectory primary
ratio, and reproducibility metadata.

Run the work as one persistent remote H100 session with durable output storage.
The session may contain ordered phases—calibration, Step 1, Step 2, Step 3
selection/profile, Deliverable 9 holdout evaluation, and final audit—but it
must not silently downgrade to the historical 32+32 cohort. The checked-in
Step 2 design uses 24 shared tasks per suite, four parameters, four values per
parameter, and 288 non-baseline sweep trajectories after baseline reuse.

The previous GCP VM disk is confirmed wiped. Its raw trajectories and logs
cannot be recovered from this repository; only compact summaries and hashes
remain. A new run must write every artifact to persistent disk or export it
continuously to object storage before proceeding.

## Authoritative assignment contract

### Step 1: baseline SWE-bench profiling

Run SWE-agent with the pinned Qwen model through vLLM on both SWE-bench Lite
and SWE-bench Verified. The assignment requires:

1. resolved rate and average end-to-end wall latency for Lite and Verified;
2. deterministic repository categories;
3. a sample-level plot with category on the y-axis and the CPU-to-GPU latency
   ratio on the x-axis;
4. three category plots:
   - resolved rate versus average end-to-end latency;
   - resolved rate versus CPU-to-GPU latency ratio;
   - average end-to-end latency versus CPU-to-GPU latency ratio;
5. an explanation of why category ratios differ.

The public-scoreboard comparison must state the exact task cohort, denominator,
model, revisions, hardware, and execution policy. A partial cohort is not a
full-scoreboard reproduction.

### Step 2: four hyperparameter sweeps

Sweep four declared hyperparameters over multiple values. The checked-in plan
uses `call_limit`, `max_output_tokens`, `observation_length`, and
`temperature`. For each hyperparameter, generate the same three trade-off views
listed above, then produce one combined figure summarizing all four sweeps and
write the observations. The Step 1 baseline is reused rather than rerun.

### Step 3: high-ratio event analysis

After Step 1 is complete and audited, choose the eligible instance with the
largest primary ratio using the deterministic tie breakers in the plan. For
that single instance:

- plot the end-to-end latency breakdown;
- log each tool event, including file reads, writes, traversal, searches,
  patches, tests, and shell operations;
- log each model request, including input tokens, output tokens, context length,
  and request wall latency;
- explain how individual tool and model events produce the end-to-end latency;
- relate model latency to token counts, model size, and effective memory
  bandwidth where those inputs are actually available.

### Deliverable 9

The simulator must:

- expose the relevant CPU and GPU hardware parameters so another platform can
  be supplied without rewriting the model;
- regenerate every required Steps 1-3 figure from canonical tables;
- predict individual event latency with no individual event error above 25%;
- predict end-to-end latency with no trajectory error above 25%.

Mean error alone is insufficient because the assignment says each individual
event must be within 25%. The acceptance report must include every denominator,
unavailable row, absolute percentage error, hardware profile, model fit split,
and source hash.

## Primary metric and secondary diagnostics

The assignment-facing CPU-to-GPU latency ratio is the serialized phase wall
time ratio:

```text
sum(tool-call wall_ms) / sum(model-request wall_ms)
```

The numerator comes from measured SWE-agent tool execution boundaries. The
denominator comes from measured model-request boundaries. Both sets must belong
to the same trajectory and monotonic timing domain.

The following are secondary diagnostics only:

- `cpu_activity_union_ms`;
- `cuda_activity_union_ms` or device-activity union;
- `kernel_duration_sum_ms`;
- Kineto/Nsight traces;
- sampled GPU utilization, process overlap, and vLLM aggregate counters.

Do not substitute CPU activity divided by CUDA activity for the primary ratio.
Those diagnostics explain activity inside a phase; they do not define the
assignment's tool-versus-model phase latency ratio.

## Historical H100 disk status

The previously used GCP H100 VM disk is confirmed wiped. The repository retains
the source identity and compact hashes, but not the raw payloads. The recorded
source was:

```text
instance: instance-20260822-182111
source root: /home/jasonrivera691/eic-work
```

These are historical paths only:

```text
/home/jasonrivera691/eic-work/data/raw
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-batch03-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-batch03-6
```

Additional batch roots are enumerated in `project/GCP_H100_PROGRESS.json` and
standalone trajectory paths in `project/GCP_H100_MEASUREMENTS.json`. Do not
delay the new experiment looking for these paths. Only an independently
existing snapshot, object-storage export, or backup should be audited.

Run the bounded, read-only audit from the repository root:

```bash
python3 scripts/assignment/audit_h100_recovery.py \
  --root /home/jasonrivera691/eic-work \
  --json-out /tmp/h100-recovery-audit.json
```

The audit classifies whether an externally recovered record has end-to-end
timing, tool events, model-request events, token counts, and an official
outcome. It does not modify the source tree. Without an external backup, the
missing raw GCP telemetry must be recollected and written to durable storage.

## Local assignment pipeline

Use external writable roots for task manifests, measured runs, compiled tables,
and figures. The examples below are executable from the repository root after
replacing only the absolute source paths with the actual mounted locations.

### 1. Plan the deterministic matrix

Each task manifest is JSONL with one object per task and at least
`instance_id`; `suite` must be `lite` or `verified`, and `repo` or
`repository` should be present. Generate and hash the plan without executing a
workload:

```bash
python3 scripts/assignment/plan_matrix.py \
  --config configs/assignment_steps_1_3.json \
  --lite-tasks /absolute/path/manifests/lite.jsonl \
  --verified-tasks /absolute/path/manifests/verified.jsonl \
  --output /absolute/path/assignment/plan.jsonl \
  --sha256-sidecar /absolute/path/assignment/plan.jsonl.sha256
```

The baseline count equals the total rows in the two input manifests. The plan
adds exactly 288 non-baseline sweep rows: 24 deterministically selected tasks
(12 Lite and 12 Verified), four knobs, and three non-baseline values per knob.
Step 3 is a post-Step-1 selection policy, not a preselected execution row.

Reconcile already-normalized evidence against the sealed plan before running
anything. Only an exact, unique, completed measured match removes a plan row:

```bash
python3 scripts/assignment/reconcile_plan.py \
  --plan /absolute/path/assignment/plan.jsonl \
  --trajectories /absolute/path/compiled/trajectories.csv \
  --remaining-output /absolute/path/assignment/remaining-plan.jsonl \
  --report-output /absolute/path/assignment/reconciliation.json
```

The checked-in `run_matrix.py` executor consumes that hashed remaining plan
serially, enforces concurrency 1 plus per-case/global deadlines, and persists
resumable state. Its reviewed case runner owns the actual SWE-agent invocation.
Do not mistake successful planning, reconciliation, or dry-run validation for
live acquisition.

Bind a clean checkout and external work root into a hardware-specific runtime
manifest. This command is offline and writes no manifest in validation mode:

```bash
python3 scripts/assignment/render_runtime_manifest.py \
  --repo-root "$PWD" \
  --work-root /absolute/path/assignment-work \
  --hardware h100 \
  --expected-branch parallel-h100-shards \
  --output /absolute/path/assignment-work/runtime-manifest.json \
  --validation-only
```

After reviewing that output, omit `--validation-only` to write the mode-0600
manifest and exact SHA-256 sidecar. Select `--hardware a100` only on the A100
acquisition host. The renderer refuses a dirty checkout, wrong branch,
detached HEAD, unpinned dataset, overwrite, or non-absolute work root.

Validate the sealed remaining matrix without launching its case runner:

```bash
python3 scripts/assignment/run_matrix.py \
  --plan /absolute/path/assignment/remaining-plan.jsonl \
  --sha256-sidecar /absolute/path/assignment/remaining-plan.jsonl.sha256 \
  --runner scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --output-dir /absolute/path/assignment-work/matrix-runs
```

The runtime manifest is an explicit matrix input, not ambient shell state. The
executor verifies its adjacent `.sha256` sidecar, passes the exact path to each
case runner, and binds both path and digest into resumable state and completed
case results.

Live execution is intentionally a separate command and requires both explicit
flags. Run it only on the intended paid GPU host after every preflight passes:

```bash
python3 scripts/assignment/run_matrix.py \
  --plan /absolute/path/assignment/remaining-plan.jsonl \
  --sha256-sidecar /absolute/path/assignment/remaining-plan.jsonl.sha256 \
  --runner scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --output-dir /absolute/path/assignment-work/matrix-runs \
  --execute \
  --acknowledge-paid-gpu-work \
  --max-wall-seconds 14400
```

For each executed case, the runner starts the manifest-pinned
`scripts/observability/request_proxy.py` on a deterministic free loopback port
before starting SWE-agent. The manifest's `model.api_base` remains the
upstream vLLM endpoint; SWE-agent receives only the proxy endpoint. Request
events and proxy provenance are written below that case's artifact root. The
runner waits for the proxy readiness line without sending a model request,
reaps it on every exit path, and fails closed on an early exit, missing or
unsuccessful events, or any pin/hash mismatch. `--validate-only` performs none
of this runtime startup and is side-effect-free.

Use `--resume` with the same live command after a preemption. Never change the
plan, runtime manifest, or output root between attempts.

### Parallelize independent cases safely

The 300 Lite plus 500 Verified cases can run in parallel across homogeneous
H100 workers. Parallelism is across independent trajectories; each trajectory
and each worker still uses `concurrency=1`. Do not run multiple shards on one
GPU while measuring latency, because GPU contention changes E2E and
tool/model phase timing.

Create deterministic shards from the sealed plan:

```bash
python3 scripts/assignment/shard_plan.py \
  --plan /absolute/path/assignment/plan.jsonl \
  --plan-sha256-sidecar /absolute/path/assignment/plan.jsonl.sha256 \
  --shard-count 8 \
  --output-dir /mnt/eic-work/assignment/shards \
  --manifest /mnt/eic-work/assignment/shards.json
```

Launch one command per worker, assigning one shard and one durable output root
to each worker:

```bash
python3 scripts/assignment/run_matrix.py \
  --plan /mnt/eic-work/assignment/shards/shard-000-of-008.jsonl \
  --sha256-sidecar /mnt/eic-work/assignment/shards/shard-000-of-008.jsonl.sha256 \
  --shards-manifest /mnt/eic-work/assignment/shards.json \
  --runner scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /mnt/eic-work/runtime-manifest.json \
  --output-dir /mnt/eic-work/assignment/runs/worker-000 \
  --execute \
  --acknowledge-paid-gpu-work
```

Change only the shard index and worker output root for the other workers. The
shard manifest binds every shard to the parent plan and rejects overlap,
missing cases, altered case specifications, or altered shard files. After all
workers finish, compile all worker normalization roots together; the final
audit must still prove exact Lite/Verified coverage.

### 2. Ingest one completed measured trajectory

For every completed run, provide its plan-derived run specification, raw
SWE-agent trajectory, request-proxy JSONL, runner summary, and official
evaluator outcome:

```bash
python3 scripts/assignment/ingest_sweagent_run.py \
  --run-spec /absolute/path/raw/RUN_ID/run_spec.json \
  --trajectory /absolute/path/raw/RUN_ID/trajectory.json \
  --model-events /absolute/path/raw/RUN_ID/model_events.jsonl \
  --runner-summary /absolute/path/raw/RUN_ID/runner_summary.json \
  --evaluator-result /absolute/path/raw/RUN_ID/evaluator_result.json \
  --runtime-manifest /absolute/path/assignment-runtime.json \
  --case-result /absolute/path/raw/RUN_ID/case_result.json \
  --output-dir /absolute/path/normalized/RUN_ID
```

The official adapter result is the only accepted source for `submitted` and
`official_resolved`; manual outcome flags are rejected. The normalizer verifies
the evaluator result, official report, dataset, and predictions hashes and
fails closed when a completed run lacks tool events, model requests, positive
end-to-end time, revisions, hashes, or evaluator outcomes.
It also requires exact runtime-manifest and case-result SHA-256 sidecars and
rejects any raw source absent from the case result's immutable artifact
inventory.

### 3. Compile all normalized runs

```bash
python3 scripts/assignment/compile_dataset.py \
  --runs-root /absolute/path/normalized \
  --output-dir /absolute/path/compiled
```

This writes `trajectories.csv`, `tool_events.csv`, `model_events.csv`,
`sweep_runs.csv`, and a hashed inventory. Review the inventory counts before
plotting.

### 4. Generate Steps 1-3 figures

Seal the Step 3 choice from the canonical Step 1 baseline table before any
Step 3 rendering. The selector excludes sweep rows and incomplete trajectories,
uses the declared tool/model wall-time ratio, and writes an immutable sidecar:

```bash
python3 scripts/assignment/select_step3_case.py \
  --trajectories /absolute/path/compiled/trajectories.csv \
  --output /absolute/path/compiled/step3-selection.json
```

```bash
python3 scripts/assignment/generate_step_figures.py \
  --trajectories /absolute/path/compiled/trajectories.csv \
  --tool-events /absolute/path/compiled/tool_events.csv \
  --model-events /absolute/path/compiled/model_events.csv \
  --sweep-runs /absolute/path/compiled/sweep_runs.csv \
  --reconciliation-report /absolute/path/assignment/reconciliation.json \
  --step3-selection /absolute/path/compiled/step3-selection.json \
  --output-dir /absolute/path/figures
```

The generator validates all input tables before writing. It reports the
assignment matrix as complete only when the reconciliation report is bound to
the exact trajectories CSV, has zero remaining or ambiguous cases, and all
four declared sweep parameters are present. Step 3 selection is restricted to
completed measured `shared-baseline` rows; a higher-ratio sweep row cannot be
selected. Use `--force` only when intentionally replacing a previously
generated figure directory from the same reviewed source tables.

### 5. Fit, freeze, and evaluate event/E2E predictions

#### Required adaptive live workflow

For a real SWE-agent trajectory, execute the following order without
exception. This is the only order that supports an event-level or E2E
prediction claim for an adaptive run:

1. collect the declared **calibration** trajectories and their complete
   measured event/E2E labels;
2. perform the calibration integrity audit (hashes, runtime/hardware identity,
   official outcomes, event coverage, and unavailable rows);
3. fit/select the event and E2E models using calibration labels only, then
   freeze the calibration-model artifact and its bindings;
4. before a holdout tool or model request executes, derive only allowed
   pre-execution features and durably append its prediction;
5. execute that one event, then reveal and append its measured label only after
   the durable prediction exists;
6. before the trajectory ends, freeze the E2E prediction manifest; then reveal
   the measured E2E label, score the frozen predictions, and run the
   independent adversarial completion audit.

The holdout runner must fail closed if it cannot prove this order. In
particular, no prediction may use measured holdout wall time, CPU time, CUDA
time, Kineto/Nsight timing, actual output tokens, response content/size, or
any other target-derived field. The runner records these ordering and
provenance checks as artifacts; a dry run or a passing offline test is not a
live-GPU result.

The static protocol below is suitable only when every holdout event is already
predeclared. The adaptive protocol later in this section is the required path
for an ordinary live SWE-agent trajectory, whose next tool/model event is not
known in advance.

First seal a calibration/holdout split manifest and its exact `.sha256`
sidecar. Capture each holdout event's feature declaration before that event is
executed; later-observed timing, output-token, response-size, CPU, CUDA,
Kineto, and timestamp fields are forbidden. Seal that feature journal against
the reviewed runtime, split, hardware, implementation, monotonic clock, and
boot identity before building any fit input:

```bash
PYTHONPATH=src python3 scripts/assignment/build_event_protocol.py capture-features \
  --split-manifest /absolute/path/event-split-manifest.json \
  --hardware-profile /absolute/path/hardware-profile.json \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --feature-journal /absolute/path/holdout-feature-journal.json \
  --output-dir /absolute/path/event-capture
```

The capture command writes immutable `holdout_features.json` and
`capture_receipt.json` files with exact SHA-256 sidecars. It rejects measured
or target-derived fields and refuses overwrite. Build the calibration fit
input from calibration labels plus only that captured feature payload, without
opening any holdout label table:

```bash
PYTHONPATH=src python3 scripts/assignment/build_event_protocol.py prepare \
  --split-manifest /absolute/path/event-split-manifest.json \
  --hardware-profile /absolute/path/hardware-profile.json \
  --calibration-trajectories /absolute/path/calibration/trajectories.csv \
  --calibration-tool-events /absolute/path/calibration/tool_events.csv \
  --calibration-model-events /absolute/path/calibration/model_events.csv \
  --capture-receipt /absolute/path/event-capture/capture_receipt.json \
  --holdout-features /absolute/path/event-capture/holdout_features.json \
  --output-dir /absolute/path/event-protocol
```

The adapter verifies the hashed split, exact run/event coverage, canonical
event counts, and hardware identity. It writes hashed `calibration.json`,
`holdout_features.json`, and `prepare_receipt.json` files; the receipt records
that holdout labels were not accessed.

Fit only on those calibration event/E2E labels and freeze holdout predictions
from the feature-only journal:

```bash
PYTHONPATH=src python3 scripts/assignment/evaluate_predictions.py fit-freeze \
  --calibration /absolute/path/event-protocol/calibration.json \
  --holdout-features /absolute/path/event-protocol/holdout_features.json \
  --prepare-receipt /absolute/path/event-protocol/prepare_receipt.json \
  --prediction-manifest /absolute/path/prediction-manifest.json
```

Only after the prediction manifest and SHA-256 sidecar are frozen may the
canonical holdout tables be opened. The reveal adapter verifies the frozen
manifest before any holdout read, requires exact predicted/canonical event
coverage, and binds the labels to the prediction SHA-256:

```bash
PYTHONPATH=src python3 scripts/assignment/build_event_protocol.py reveal \
  --split-manifest /absolute/path/event-split-manifest.json \
  --prediction-manifest /absolute/path/prediction-manifest.json \
  --prepare-receipt /absolute/path/event-protocol/prepare_receipt.json \
  --holdout-trajectories /absolute/path/holdout/trajectories.csv \
  --holdout-tool-events /absolute/path/holdout/tool_events.csv \
  --holdout-model-events /absolute/path/holdout/model_events.csv \
  --output-labels /absolute/path/event-holdout-labels.json
```

Then score every event and trajectory:

```bash
PYTHONPATH=src python3 scripts/assignment/evaluate_predictions.py score \
  --prediction-manifest /absolute/path/prediction-manifest.json \
  --holdout-labels /absolute/path/event-holdout-labels.json \
  --prepare-receipt /absolute/path/event-protocol/prepare_receipt.json \
  --output /absolute/path/event-evaluation.json
```

The feature schemas reject measured wall/CPU/CUDA/Kineto timing, actual output
tokens, and other target-derived inputs. The scoring command verifies the
frozen SHA-256 binding and exits nonzero if any tool/model event is unavailable,
if any event exceeds 25% absolute percentage error, or if any complete
trajectory exceeds 25%. The evaluator is implemented; the assignment
acceptance claim remains pending until it is run on valid held-out complete
SWE-agent trajectories.

There are two fail-closed prediction modes:

- `build_event_protocol.py` is for `static_predeclared` controlled workloads,
  where every holdout event and feature row exists before execution.
- `adaptive_event_protocol.py` is for a real SWE-agent trajectory, where the
  next event is not known in advance. A reviewed runner must invoke
  `predict-event` immediately before each tool/model event and may invoke
  `reveal-event` only after that prediction has been durably appended.

The adaptive path implements the required live order: calibration collection
and integrity audit; calibration-only fit/selection; durable prediction before
each target event; label reveal after that event; frozen E2E manifest before
trajectory-end reveal; scoring; then an adversarial audit. It binds every
record to split/runtime/hardware/model hashes, maintains an append-only clock
and SHA-256 chain across restarts, and rejects wall, CPU, CUDA, Kineto,
actual-output-token, response, and other target-derived fields from prediction
inputs.

For an ordinary SWE-agent holdout case, use the reviewed integration rather
than manually wrapping individual events. The case specification must already
be inside its fresh case output directory. First freeze a
calibration-derived, hash-bound E2E forecast. Its feature-only event forecast
is checked against the frozen trajectory model; it is not a caller-entered
duration and it must not contain measured labels.

```bash
CASE_DIR=/absolute/path/assignment-work/holdout-case
RUN_ID=ASSIGNMENT_RUN_ID

PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py \
  freeze-e2e-prediction \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --hardware-profile /absolute/path/hardware-profile.json \
  --run-id "$RUN_ID" \
  --features-json /absolute/path/adaptive/pre-trajectory-features.json \
  --output "$CASE_DIR/e2e-prediction.json"

PYTHONPATH=src python3 scripts/assignment/render_adaptive_runtime_config.py \
  --case-spec "$CASE_DIR/case_spec.json" \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --split-manifest /absolute/path/event-split-manifest.json \
  --hardware-profile /absolute/path/hardware-profile.json \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --tokenizer-snapshot /absolute/path/pinned-tokenizer-snapshot \
  --protocol-root "$CASE_DIR/adaptive-protocol" \
  --e2e-prediction "$CASE_DIR/e2e-prediction.json" \
  --output "$CASE_DIR/adaptive-runtime.json"

# Read-only integration check: no runner, proxy, model request, or measurement.
PYTHONPATH=src python3 scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --case-spec "$CASE_DIR/case_spec.json" \
  --output-dir "$CASE_DIR" \
  --adaptive-runtime-config "$CASE_DIR/adaptive-runtime.json" \
  --validate-only

# Only after review, on the intended live GPU host:
PYTHONPATH=src python3 scripts/assignment/sweagent_case_runner.py \
  --runtime-manifest /absolute/path/assignment-work/runtime-manifest.json \
  --case-spec "$CASE_DIR/case_spec.json" \
  --output-dir "$CASE_DIR" \
  --adaptive-runtime-config "$CASE_DIR/adaptive-runtime.json" \
  --execute
```

The reviewed adaptive SWE-agent wrapper predicts each tool event immediately
before execution, while the reviewed request proxy predicts each model event
before forwarding the request. Both reveal labels only afterward. At trajectory
end the wrapper freezes the complete prediction manifest before revealing E2E
wall time, scores the run, and writes the adversarial audit artifacts under the
case root. A resume reuses the same hash-bound protocol root and append-only
chain.

The equivalent low-level protocol command sequence, useful only for ordering
audits and unit tests, is shown below. It deliberately does not carry the
calibration-derived E2E artifact and must not be submitted as final evidence;
the completion auditor rejects such a root:

```bash
PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py freeze-model \
  --model-json /absolute/path/calibration-model-input.json \
  --output /absolute/path/adaptive/calibration-model.json \
  --calibration-run-id CALIBRATION_RUN_ID \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256"

# Low-level ordering test only; final adaptive evidence must use the
# hash-bound freeze-e2e-prediction artifact shown above.
PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py arm \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256" \
  --run-id HOLDOUT_RUN_ID \
  --predicted-e2e-ms PREDICTED_E2E_MS

# Repeat these two commands in order for each newly known adaptive event.
PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py predict-event \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256" \
  --kind tool \
  --features-json /absolute/path/next-tool-pre-event-features.json

PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py reveal-event \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256" \
  --kind tool \
  --identifier EVENT_ID \
  --label-json /absolute/path/measured-event-label.json

PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py freeze-manifest \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256"

PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py reveal-trajectory \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256" \
  --observed-e2e-ms OBSERVED_E2E_MS

PYTHONPATH=src python3 scripts/assignment/adaptive_event_protocol.py score \
  --root /absolute/path/adaptive/holdout-run \
  --calibration-model /absolute/path/adaptive/calibration-model.json \
  --split-manifest-sha256 "$SPLIT_SHA256" \
  --runtime-manifest-sha256 "$RUNTIME_SHA256" \
  --hardware-profile-sha256 "$HARDWARE_SHA256" \
  --model-revision-sha256 "$MODEL_REVISION_SHA256" \
  --output /absolute/path/adaptive/score.json
```

Use `--kind model` with the matching model-request feature/label files for
model events. A resume must reuse the same root and bindings; duplicate,
reordered, pending, relabeled, or tampered records fail closed.

### 6. Audit final evidence independently

The completion auditor verifies every upstream hash and figure file, then
recomputes the event/E2E score from the sealed prediction manifest, revealed
labels, and prepare receipt instead of trusting a claimed score report:

```bash
PYTHONPATH=src python3 scripts/assignment/audit_completion.py \
  --plan /absolute/path/assignment/plan.jsonl \
  --plan-sha256 /absolute/path/assignment/plan.jsonl.sha256 \
  --reconciliation-report /absolute/path/assignment/reconciliation.json \
  --trajectories /absolute/path/compiled/trajectories.csv \
  --tool-events /absolute/path/compiled/tool_events.csv \
  --model-events /absolute/path/compiled/model_events.csv \
  --sweep-runs /absolute/path/compiled/sweep_runs.csv \
  --inventory /absolute/path/compiled/inventory.json \
  --inventory-sha256 /absolute/path/compiled/inventory.json.sha256 \
  --step3-selection /absolute/path/compiled/step3-selection.json \
  --figures-report /absolute/path/figures/figures.json \
  --prediction-manifest /absolute/path/prediction-manifest.json \
  --holdout-labels /absolute/path/event-holdout-labels.json \
  --prepare-receipt /absolute/path/event-protocol/prepare_receipt.json \
  --evaluation-report /absolute/path/event-evaluation.json \
  --output /absolute/path/completion-audit.json \
  --output-sha256-sidecar /absolute/path/completion-audit.json.sha256
```

Any missing row, stale sidecar, forged score, incomplete figure inventory,
ordering/provenance violation, or event/E2E error above 25% blocks completion.
For an adaptive run, the audit independently verifies that every measured
event label follows its frozen prediction and that the E2E observation follows
the frozen E2E manifest.

### 7. Verify the offline tooling

```bash
PYTHONPATH=src python3 -m unittest discover -s tests/assignment -v
python3 -m compileall -q src/agentic_sim/assignment scripts/assignment tests/assignment
```

These checks validate the offline contracts. They are not substitutes for live
SWE-bench measurements or Deliverable 9 prediction evaluation.

## Offline-complete work versus live-GPU evidence

The repository can complete offline: deterministic planning and reconciliation;
recovery inventory; runtime/feature/split/hash validation; canonical ingest and
compilation; figure generation from supplied canonical tables; calibration-only
fit/freeze logic; adaptive ordering, leakage, resume, and adversarial-audit
tests. These are implementation and reproducibility results, not measured
assignment outcomes.

The following always require valid live-GPU artifacts before they may be
claimed for either H100 or A100: completed SWE-agent trajectories; official
outcomes; measured per-tool and per-model event labels; measured E2E latency;
frozen prediction manifests; scoring reports; and a successful independent
completion audit. Neither an installed runtime, a dry run, a cached image, nor
a passing unit test establishes H100 or A100 completion.

## What existing evidence establishes

Existing H100 evidence establishes:

- 64 compact population outcome rows: 32 Lite and 32 selected Verified;
- Lite: 8 resolved of 32 selected/completed;
- Verified: 10 resolved, 29 officially completed, 30 submitted, and 32
  selected;
- 16 measured one-instance hyperparameter sweep cells;
- one detailed H100 Astropy trajectory with 31 serialized model requests,
  request timing, token counts, and direct Kineto device activity;
- additional strace/tool, process-attribution, service-calibration, controlled
  Kineto, and sealed feature-only evidence.

That evidence does not establish:

- full-suite public-scoreboard reproduction;
- population average end-to-end latency from the compact 64-row export;
- population repository/category tool-to-model ratios;
- event-level prediction error below 25%;
- end-to-end error below 25% for the assignment simulator;
- cross-platform assignment accuracy on A100;
- that feature-only request-latency validation satisfies Deliverable 9.

## Exact remaining live acquisition

### H100

1. Audit and normalize the old disk first.
2. Match each complete recovered row to a sealed plan `resume_key`.
3. Run only plan rows still missing a complete trajectory, official outcome,
   tool-event log, model-request log, end-to-end wall time, and provenance.
4. Step 1 needs one valid baseline row for every task in the declared Lite and
   Verified manifests. A sampled manifest must be reported as sampled and
   cannot be called full-scoreboard reproduction.
5. Step 2 needs the 24 shared baselines plus the plan's 288 non-baseline cells.
   Existing one-instance sweep rows may be retained as historical evidence but
   do not replace cells whose task/config identity does not match the plan.
6. Step 3 needs one highest-ratio eligible baseline trajectory. Reuse its
   existing complete event logs; run one targeted repeat only if its tool or
   model event evidence is incomplete.

The exact H100 live count is therefore the number of unmatched plan rows after
recovery, plus at most one targeted Step 3 repeat. It is not automatically the
entire matrix.

### A100

No assignment-normalized live A100 SWE-agent trajectory is currently claimed.
For cross-platform validation, acquire the same 24 deterministic Step 2
baseline task IDs (12 Lite and 12 Verified) on one A100 80 GB at concurrency 1,
with complete tool events, model-request events, end-to-end wall time, official
outcomes, hardware metadata, and hashes. Freeze predictions before reading the
A100 labels. Do not train on those A100 labels if the claim is zero-shot
hardware transfer.

Step 2 sweeps do not need to be repeated on A100 unless the report explicitly
claims A100-specific sweep figures. The previous A100 request-only calibration
protocol is separate and cannot substitute for the 24 complete SWE-agent
trajectories above.

The same restriction applies to H100: historical request-only and
feature-only artifacts remain useful evidence, but neither is a substitute for
complete assignment-normalized tool/model/E2E trajectories and the adaptive
prediction/reveal proof above.

## Completion gate

The assignment is complete only when:

- every claimed baseline and sweep denominator is explicit;
- required raw events normalize and compile without exclusions being hidden;
- all Steps 1-3 figures regenerate from hashed canonical tables;
- the hardware profile is explicit and portable;
- predictions are frozen before evaluation labels are joined;
- every valid individual event has absolute percentage error at or below 25%;
- every valid trajectory has end-to-end absolute percentage error at or below
  25%;
- missing or unavailable rows remain visible in the final report.
