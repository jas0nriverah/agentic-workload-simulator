# GCP H100 Spot pilot runbook

This runbook uses one Google Cloud `a3-highgpu-1g` Spot VM in
`us-central1`. It is intentionally narrower than the completed Modal sweeps:
the purpose of this session is request-correlated timing and one calibration
plus one sealed holdout, not another success-rate sweep.

## 1. Before launch

The Google Cloud project must have:

- one regional preemptible H100 GPU quota in the selected zone/region;
- one global GPU quota;
- a linked billing account with an explicit budget alert;
- a persistent boot disk of at least 200 GB and a separate persistent data
  disk of at least 250 GB;
- a Cloud Storage bucket if off-VM export is desired.

The browser console currently reports `a3-highgpu-1g`, one H100 80 GB, 26
vCPUs, and 234 GB RAM in `us-central1`. Its displayed Spot estimate is about
$6.64/hour, but the billing account and quota are independent gates.

The creator is safe by default:

```bash
./cloud/gcp/create_h100_spot.sh \
  --project project-3d59272d-3213-4e06-97b \
  --zone us-central1-a \
  --image IMAGE \
  --image-project IMAGE_PROJECT
```

Review the printed command and add `--apply` only after the quota and image
have been verified. It never deletes a VM or disk.

## 2. VM bootstrap

After SSH access, mount the persistent data disk at `/mnt/eic-work`, extract
the reviewed repository bundle, and copy the untracked manifest:

```bash
cp cloud/gcp/instance_manifest.env.example cloud/gcp/instance_manifest.env
$EDITOR cloud/gcp/instance_manifest.env
./scripts/cloud/lambda_preflight.sh \
  --manifest cloud/gcp/instance_manifest.env \
  --output /mnt/eic-work/artifacts/manifests/gcp_preflight.json
./scripts/cloud/lambda_bootstrap.sh \
  --manifest cloud/gcp/instance_manifest.env \
  --resume
```

Use the GPU-optimized Deep Learning VM image with CUDA when possible. The
plain Debian image requires a manual CUDA installation. Keep the pinned vLLM
image, model revision, SWE-agent revision, SWE-bench revision, and evaluator
digests unchanged.

### Backend selection

Set `BACKEND=auto` in the external startup manifest for the normal path. Auto
selects Docker only after the daemon, NVIDIA Container Toolkit runtime, pinned
image, and one-GPU access probe succeed; otherwise it selects the prepared
direct runtime. Use `BACKEND=docker` to require Docker or `BACKEND=direct` to
require the already-installed host/GPU-container vLLM environment. An
explicit Docker failure never falls back. No path installs Docker-in-Docker
or treats an unprivileged container as a Docker host.

The exact offline startup check is:

```bash
bash scripts/cloud/start_h100.sh \
  --manifest /mnt/eic-work/h100-startup.env \
  --backend auto --dry-run
```

After the prepared environment has been independently checked, use the same
command without `--dry-run`. Direct mode requires the pinned vLLM 0.10.0
package, model/tokenizer snapshot, request limits, host tracing binary, and
the external backend-specific artifact root. Docker and direct roots,
manifests, and provenance must remain separate; their results are not
interchangeable without validation.

The local SWE-agent API key is a placeholder. Supply the real local-only key
through the process environment; never place it in the manifest or artifacts.

## 3. Minimal measured sequence

1. Run the vLLM health gate and record hardware/profiler capabilities.
2. Run one request-aware synthetic matrix through the proxy:
   input lengths 512/4096/16384 × output lengths 64/512, serially.
3. Run Lite `astropy__astropy-12907` once with the frozen 30-call/2048-token
   control contract.
4. Freeze simulator coefficients and select an unseen holdout before
   inspecting its timings.
5. Run the sealed holdout once with the same telemetry contract.
6. Export compact manifests and hashes, then stop the VM.

Stop immediately on failed vLLM health, missing selected-backend prerequisites
(Docker/NVIDIA GPU access for Docker, or the prepared direct environment),
missing request IDs, incomplete trace coverage, configuration drift, or
preemption.
Do not repeat a trajectory merely because its patch did not resolve.

## 4. Request-aware profiled attempt

Start the proxy only for the separate profiled attempt:

```bash
AGENTIC_ENABLE_NVTX=1 PYTHONPATH=src \
  python3 scripts/observability/request_proxy.py \
  --events /mnt/eic-work/artifacts/request-profile/request_events.jsonl \
  --listen-port 8001 --upstream-port 8000
```

Point only that attempt's `api_base` at `http://127.0.0.1:8001/v1` by
setting `EIC_MODEL_API_BASE` on the run wrapper:

```bash
EIC_MODEL_API_BASE=http://127.0.0.1:8001/v1 \
  ./scripts/cloud/lambda_run_first_experiment.sh \
  --manifest cloud/gcp/instance_manifest.env \
  --instance-id astropy__astropy-12907 \
  --experiment-id gcp-request-profile \
  --mode thin-telemetry
```

`request_proxy.py` records request IDs, monotonic boundaries, status,
payload hashes, byte counts, and returned token counts. It does not store
prompts or responses.

The proxy interval is request-level CPU/network boundary evidence. It is not
GPU device time by itself. GPU-time claims require a successful Nsight/host
trace with the same clock identity and explicit request-to-kernel matching.

## 5. Preemption, export, and shutdown

The VM metadata shutdown hook is
`cloud/gcp/preemption_shutdown.sh`. It writes a durable marker, stops vLLM,
flushes the persistent disk, and best-effort syncs artifacts to the configured
bucket. Local persistent-disk artifacts remain authoritative.

At the end of the session:

```bash
./scripts/cloud/lambda_collect_results.sh \
  --source-root /mnt/eic-work/data/raw \
  --output-dir /mnt/eic-work/export \
  --run-id gcp-h100-pilot
sync
```

Stop the VM promptly after export. Retain the data disk until the received
archive has passed local checksum verification. Delete retained resources only
as a separate, explicit decision.
