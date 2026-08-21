# Parallel Lightning batch runbook

This is an additive throughput path for the already-reviewed single-H100
control. It does not replace the assignment baseline, alter the model, or
claim that four GPUs make one trajectory faster. Each worker owns exactly one
GPU, one vLLM server, and one disjoint subset of the pinned dataset. Never use
tensor parallelism across these workers.

## When to use it

Use this only after the existing control/gold-smoke gates and the current
Lightning session gate have passed. The session gate remains mandatory; these
commands do not authorize paid work. Run the uninstrumented control shard
first. Thin telemetry is a separate paired condition and must not be mixed
into this batch.

Four workers are reasonable only when four GPU-isolated Studios are available
and the account's credits/quota cover their concurrent runtime. They provide
approximately four trajectories per wall-clock window at approximately four
GPU-hours per hour. One worker per H100 is the safe maximum; do not request
four GPUs inside one Studio unless the provider explicitly isolates them and a
new manifest has been reviewed.

## 1. Prepare the full pinned dataset

The first-control manifest intentionally points at one-row smoke fixtures. A
parallel result requires the full pinned Lite or Verified source asset produced
by the existing bootstrap/download stage. Do not use a floating Hugging Face
download. Pass the exact `.parquet`, `.json`, or `.jsonl` path and let the
planner record its SHA-256.

Example paths on a bootstrapped host (replace with the path recorded in the
validated `datasets.json`):

```bash
cd /home/ubuntu/agentic-workload-simulator
LITE_SOURCE=/home/ubuntu/agentic-work/datasets/raw/lite.parquet
```

The source file must be the pinned Lite/Verified revision. The planner rejects
duplicate or unsafe `instance_id` values and records every selected-row hash.

## 2. Create the deterministic shards (free/local operation)

Run this once for each dataset. A four-shard plan is safe because assignment
is round-robin, source order is preserved within each worker, and the manifest
contains the complete ID union and every row hash.

```bash
python3 scripts/cloud/plan_parallel_batch.py \
  --dataset "$LITE_SOURCE" \
  --output-root /home/ubuntu/agentic-work/parallel/lite-batch-001 \
  --batch-id lite-batch-001 \
  --experiment-id lite-parallel-control-001 \
  --dataset-name lite \
  --shards 4
```

The command writes `batch_manifest.json`, `worker-00/` through `worker-03/`,
and raw `instances.json` files. To resume only final successful attempts, use
the same experiment ID and add `--resume --work-root
/home/ubuntu/agentic-work`. Failed, timed-out, or evaluator-error attempts are
not silently skipped.

## 3. Verify every worker before billing work

On each GPU-isolated Studio, run the dry-run. It rewrites only the dataset,
output, evaluator, and run-ID fields; it never starts a process or writes
artifacts.

```bash
python3 scripts/cloud/lambda_run_parallel_shard.py \
  --manifest cloud/lambda/instance_manifest.env \
  --batch-manifest /home/ubuntu/agentic-work/parallel/lite-batch-001/batch_manifest.json \
  --worker-index 0 \
  --dry-run
```

Use `--worker-index 1`, `2`, or `3` in the other Studios. Confirm that the
printed IDs do not overlap, `--instances.filter` is absent, `--num_workers 1`
remains present, and the command hashes differ only where the worker paths and
IDs differ. Run `scripts/cloud/lambda_preflight.sh` and the provider session
gate on every Studio before starting.

## 4. Start one worker per Studio

After the gate passes, remove `--dry-run` and provide the vLLM API key through
the process environment. The key is never written to a manifest or artifact.

```bash
export VLLM_API_KEY='the-runtime-key'
python3 scripts/cloud/lambda_run_parallel_shard.py \
  --manifest cloud/lambda/instance_manifest.env \
  --batch-manifest /home/ubuntu/agentic-work/parallel/lite-batch-001/batch_manifest.json \
  --worker-index 0 \
  --resume
```

The worker runs SWE-agent first and the official evaluator second. Evaluator
runtime is recorded separately and excluded from trajectory E2E timing. Each
attempt is immutable under `worker-NN/attempt-NNN/`; a failed attempt requires
`--force-retry` and receives a new attempt directory. A completed attempt with
`--resume` is skipped without re-running the model.

Repeat the plan and worker commands for Verified after Lite, using a new
`batch-id` and `--dataset-name verified`. Keep Lite and Verified manifests,
predictions, evaluator reports, and costs separate.

## 5. Stop and export

Do not leave Studios running after the worker/evaluator logs and
`worker_status.json` are exported. Copy the batch directory to durable local
storage, verify the batch and worker hashes, stop workloads, and terminate the
provider sessions in the console. No parallel status is an accuracy claim
until the official evaluator report is present and independently inventoried.
