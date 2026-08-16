# Lambda H100 first-session runbook

This is the narrow first paid-session sequence for one Lambda Cloud 1× NVIDIA
H100 PCIe 80 GB Ubuntu host. It follows the frozen assignment methodology:
gold-patch evaluator smokes are separate from the generated-prediction
trajectory, and the uninstrumented control runs before thin telemetry. Do not
start a five-instance gate until the first generated prediction and evaluator
artifacts have been inspected.

The repository is not published by this runbook. Upload the locally reviewed
`lambda-ready-<commit>.tar.zst` bundle and its adjacent `.sha256` file to the
host; do not clone an unreviewed branch. The bundle must be verified locally
before termination.

Create and checksum the exact local bundle before any paid launch:

```bash
REVIEWED_COMMIT="$(git rev-parse HEAD)"
git diff --check
git archive --format=tar --prefix=agentic-workload-simulator/ "$REVIEWED_COMMIT" \
  | zstd -T0 -19 -o "lambda-ready-${REVIEWED_COMMIT}.tar.zst"
sha256sum "lambda-ready-${REVIEWED_COMMIT}.tar.zst" \
  > "lambda-ready-${REVIEWED_COMMIT}.tar.zst.sha256"
```

After transfer, set `REVIEWED_COMMIT` to that same recorded value on the host
and verify the checksum before extraction. For example, enter the recorded
40-hex value explicitly before the source-transfer block:

```bash
read -r REVIEWED_COMMIT  # paste the exact recorded 40-hex bundle commit
```

The host bootstrap installs `zstd` if it is missing.

## Paid-session gate

The copied `cloud/lambda/cloud_session.yaml` is the only authorization input.
It must contain the user's explicit cap, maximum gate, UTC deadlines, backup
destination, and confirmation that the user will be available to export and
terminate. The example is intentionally unauthorized. Run this first; a
nonzero result means no paid work is allowed:

```bash
cd /home/ubuntu/agentic-workload-simulator
./scripts/cloud/lambda_session_gate.sh --session cloud/lambda/cloud_session.yaml --gate G3A
```

This guard checks local state only. It never launches, bills, or terminates a
provider instance.

## Source and pinned bootstrap

The exact paths below are the reviewed Linux layout. The archive transfer is
performed by the user outside the repository; no credentials belong in the
manifest.

```bash
mkdir -p /home/ubuntu/agentic-work/source
sha256sum -c "/home/ubuntu/lambda-ready-${REVIEWED_COMMIT}.tar.zst.sha256"
tar --use-compress-program=zstd -xf "/home/ubuntu/lambda-ready-${REVIEWED_COMMIT}.tar.zst" -C /home/ubuntu/agentic-work/source
test -d /home/ubuntu/agentic-work/source/agentic-workload-simulator
cd /home/ubuntu/agentic-work/source/agentic-workload-simulator
cp cloud/lambda/instance_manifest.env.example cloud/lambda/instance_manifest.env
cp cloud/lambda/cloud_session.yaml.example cloud/lambda/cloud_session.yaml
$EDITOR cloud/lambda/instance_manifest.env
$EDITOR cloud/lambda/cloud_session.yaml
./scripts/cloud/lambda_session_gate.sh --session cloud/lambda/cloud_session.yaml --gate G3A
./scripts/cloud/lambda_preflight.sh --manifest cloud/lambda/instance_manifest.env --output /home/ubuntu/agentic-work/artifacts/manifests/lambda_preflight.json
./scripts/cloud/lambda_bootstrap.sh --manifest cloud/lambda/instance_manifest.env --dry-run
./scripts/cloud/lambda_bootstrap.sh --manifest cloud/lambda/instance_manifest.env --resume
```

`lambda_bootstrap.sh --resume` installs the pinned SWE-agent and SWE-bench
source revisions into `/home/ubuntu/agentic-work/venv`, downloads the pinned
model and selected dataset rows, and pulls only the three selected evaluator
images by digest. It must finish with all stage markers validated.

Before starting the server, provide `VLLM_API_KEY` through the process
environment or the host's approved secret mechanism. Do not put the real key
in `instance_manifest.env`, the session file, shell history, or collected
artifacts; the committed placeholder is intentionally not a credential.

## vLLM health gate

Run the server and health check in a persistent session. The launcher acquires
the recorded GPU-0 lease atomically and starts only the pinned amd64 image.

```bash
tmux new -s agentic
cd /home/ubuntu/agentic-work/source/agentic-workload-simulator
./scripts/cloud/lambda_start_vllm.sh --manifest cloud/lambda/instance_manifest.env
./scripts/cloud/lambda_healthcheck.sh --manifest cloud/lambda/instance_manifest.env --work-root /home/ubuntu/agentic-work
```

The health check requires a normal completion, a parsed `qwen3_coder` tool
call, the native vLLM Prometheus counters at `/metrics`, and an `nvidia-smi`
sample. A failure is classified as server, tool-parser, or telemetry-contract;
chat response fields never substitute for Prometheus metrics.

## Gold and first generated experiment

Run each gold smoke independently, then the first Lite task in control mode and
thin-telemetry mode. The commands use the exact selected IDs and local
one-row dataset files from the bootstrap manifest.

```bash
./scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite lite
./scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite verified

./scripts/cloud/lambda_run_first_experiment.sh --manifest cloud/lambda/instance_manifest.env --instance-id astropy__astropy-12907 --experiment-id first-lite-astropy__astropy-12907 --mode uninstrumented
./scripts/cloud/lambda_collect_results.sh --source-root /home/ubuntu/agentic-work/data/raw/first-lite-astropy__astropy-12907 --output-dir /home/ubuntu/agentic-work/export --run-id first-lite-astropy__astropy-12907-control

./scripts/cloud/lambda_run_first_experiment.sh --manifest cloud/lambda/instance_manifest.env --instance-id astropy__astropy-12907 --experiment-id first-lite-astropy__astropy-12907 --mode thin-telemetry --attempt-id attempt-002
./scripts/cloud/lambda_collect_results.sh --source-root /home/ubuntu/agentic-work/data/raw/first-lite-astropy__astropy-12907 --output-dir /home/ubuntu/agentic-work/export --run-id first-lite-astropy__astropy-12907-thin --snapshot
```

The two attempts share the same reviewed SWE-agent command; thin telemetry is
an output observer that records interval Prometheus/GPU samples with
`correlation_scope=run_interval` and never wraps or mutates requests. The
official generated-prediction evaluator is a separate runtime and is excluded
from trajectory E2E timing.

## Export, stop, and termination

Stop expansion at the configured deadline, export before the buffer expires,
verify the received archive locally, then stop project workloads. Stopping
vLLM or closing SSH does not stop Lambda billing.

```bash
./scripts/cloud/lambda_collect_results.sh --source-root /home/ubuntu/agentic-work/data/raw --output-dir /home/ubuntu/agentic-work/export --run-id first-session-final --snapshot
./scripts/cloud/lambda_stop_workloads.sh --work-root /home/ubuntu/agentic-work --server-manifest /home/ubuntu/agentic-work/artifacts/manifests/vllm_server.json
```

The user initiates the local `rsync` printed by collection, runs
`verify_lambda_archive_local.sh` against the received archive and checksum,
then terminates the VM in the Lambda console at
`hard_console_termination_utc`. Persistent filesystem retention or deletion is
a separate authorized decision and is never automatic.

On the local machine, verify the received result archive explicitly:

```bash
./scripts/cloud/verify_lambda_archive_local.sh \
  --archive "$HOME/Downloads/lambda-results-first-session-final.tar.gz" \
  --manifest "$HOME/Downloads/lambda-results-first-session-final.sha256" \
  --receipt "$HOME/Downloads/lambda-results-first-session-final.verification_receipt.json"
```

## H100-only validations still required

- Ubuntu x86-64/H100 preflight and model fit;
- vLLM normal completion, parsed tool call, and native `/metrics` counters;
- exact amd64 evaluator image availability at all three digests;
- Lite and Verified gold smoke reports;
- first generated SWE-agent prediction and official evaluation;
- measured artifact export and checksum verification.
