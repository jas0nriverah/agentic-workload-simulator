# Lightning Studio rehearsal runbook

This is a provider adapter for the frozen Lambda-compatible runtime. It does
not change the assignment, model, datasets, evaluator, or experiment order.
The Lambda runbook remains the source of truth for the workload commands.

## Recorded checkpoint

One paid Lightning H100 session has already completed the pinned preflight,
managed-Python bootstrap, vLLM health checks, Lite and Verified gold smokes,
one uninstrumented Lite control, official evaluation, and export. The compact
record is `project/FIRST_CONTROL_MEASURED.json`. Its unresolved/empty-patch
outcome is preserved without a score claim. This provider evidence does not
claim Lambda-host equivalence; do not rerun the control or start a new paid
gate without a fresh explicit authorization.

## Before starting H100 billing

Use a non-interruptible 1× H100 80 GB Studio. Verify the host is `x86_64`, has
one H100 with at least 80 GB, Docker, and at least 120 GiB free space. Keep the
Studio stopped while preparing the reviewed branch.

## Render the provider-local manifest

From the checked-out repository, create the untracked manifest. The renderer
rewrites `/home/ubuntu` paths to the Studio workspace and points command
contracts at the Studio's already-managed Conda interpreter. This is required
because a Lightning Studio permits one managed environment and rejects
`python3 -m venv`; the canonical Lambda manifest still creates a fresh venv.

```bash
scripts/cloud/render_studio_manifest.sh \
  --output cloud/lambda/instance_manifest.env \
  --studio-root /teamspace/studios/this_studio \
  --force
```

The rendered manifest records `PYTHON_ENV_MODE=managed`, the active Conda
prefix, and the actual Python major/minor version. Re-run this command after
pulling a bootstrap/renderer update; do not reuse a manifest rendered by an
older commit.

If the Studio reports Python 3.12, the renderer now selects the checked-in
Python 3.12 Linux lock automatically:

```bash
head -n 3 cloud/lambda/requirements-linux-x86_64-py312.txt
sha256sum cloud/lambda/requirements-linux-x86_64-py312.txt
```

The checked-in lock identifies Python 3.12 and `x86_64-manylinux2014`; do not
edit its header or relabel the 3.11 lock. If the provider reports another
Python version, or the selected lock is missing or fails its hash check, stop
and report that blocker rather than installing an unpinned environment.

The generated manifest is local configuration. Never commit it. The local
vLLM server does not require an external provider credential; provide a
process-only value such as `VLLM_API_KEY=local-only-key` when running the
SWE-agent command, and never store it in the manifest or artifacts.

The dataset gate is byte- and row-level, not a label check: the pinned
`huggingface_hub+pyarrow.parquet` reader records the source Parquet path and
SHA-256, and the bootstrap/gold/first-control gates recompute each selected
canonical one-row JSON hash. The source Parquet SHA-256 values are Lite
`f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b` and
Verified `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21`.

## Authorize the billed session

Copy the untracked Lightning authorization template and fill the exact UTC
window and spending cap you intend to allow. The gate is local-only; it never
launches or terminates a Studio.

```bash
mkdir -p cloud/lightning
cp cloud/lightning/cloud_session.yaml.example cloud/lightning/cloud_session.yaml
$EDITOR cloud/lightning/cloud_session.yaml
scripts/cloud/lambda_session_gate.sh \
  --session cloud/lightning/cloud_session.yaml --gate G3A
```

## Bootstrap and first control

Run the read-only checks first:

```bash
scripts/cloud/lambda_preflight.sh \
  --manifest cloud/lambda/instance_manifest.env \
  --output /teamspace/studios/this_studio/agentic-work/artifacts/manifests/lightning_preflight.json

scripts/cloud/lambda_bootstrap.sh \
  --manifest cloud/lambda/instance_manifest.env \
  --resume
```

Docker Hub may return HTTP 401/403 for an unauthenticated `/v2/` probe. The
preflight records that as reachable-with-warning; the pinned image digest is
still verified by Docker before the runtime stage proceeds.

After bootstrap succeeds, use the rendered manifest for every command:

```bash
scripts/cloud/lambda_start_vllm.sh --manifest cloud/lambda/instance_manifest.env
scripts/cloud/lambda_healthcheck.sh --manifest cloud/lambda/instance_manifest.env \
  --work-root /teamspace/studios/this_studio/agentic-work
scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite lite
scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite verified
scripts/cloud/lambda_run_first_experiment.sh --manifest cloud/lambda/instance_manifest.env \
  --instance-id astropy__astropy-12907 \
  --experiment-id first-lite-astropy__astropy-12907 --mode uninstrumented
```

Then export/checksum artifacts, stop workloads, and stop the Studio. Do not
start the four sweeps or thin telemetry in the first session.

## Parallel throughput after the control gate

For independent uninstrumented trajectories, use the additive
[`cloud/lightning/PARALLEL_RUNBOOK.md`](../lightning/PARALLEL_RUNBOOK.md). It
plans deterministic disjoint shards and runs one `--num_workers 1` worker per
GPU-isolated Studio. It does not replace the single-control fixture, bypass
the session gate, or turn a multi-GPU batch into one tensor-parallel result.
