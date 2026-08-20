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
# The archive is commit-based. Refuse to archive a stale commit when local
# hardening edits are staged, unstaged, or untracked.
if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git status --porcelain=v1 --untracked-files=all)" ]]; then
  echo 'refusing bundle creation: working tree contains staged, unstaged, or untracked files' >&2
  exit 1
fi
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

Run each gold smoke independently, then the first Lite task exactly once in
uninstrumented control mode. The first paid session intentionally stops after
the official generated-patch evaluation and export; do not spend a second
trajectory on thin telemetry before the raw `.traj` fixture has been reviewed
and a lossless normalizer has been implemented locally.

```bash
./scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite lite
./scripts/cloud/lambda_run_gold_smoke.sh --manifest cloud/lambda/instance_manifest.env --suite verified

./scripts/cloud/lambda_run_first_experiment.sh --manifest cloud/lambda/instance_manifest.env --instance-id astropy__astropy-12907 --experiment-id first-lite-astropy__astropy-12907 --mode uninstrumented
./scripts/cloud/lambda_collect_results.sh --source-root /home/ubuntu/agentic-work/data/raw/first-lite-astropy__astropy-12907 --output-dir /home/ubuntu/agentic-work/export --run-id first-lite-astropy__astropy-12907-control
```

The control command is the frozen direct SWE-agent command. The official
generated-prediction evaluator is a separate runtime and is excluded from
trajectory E2E timing. The resolved command contract records the four
assignment knobs and request-level `max_tokens`/`seed` fields in the attempt
manifest.

## Optional observability after the first result

The first session stops after collecting the control attempt. Only after the
raw trajectory and evaluator artifacts are reviewed may an explicitly
authorized later gate use
the additive helpers below; none changes the frozen model, SWE-agent command,
or baseline path:

```bash
python3 scripts/observability/probe_runtime.py \
  --output /home/ubuntu/agentic-work/artifacts/manifests/observability_capabilities.json
python3 scripts/observability/estimate_memory.py \
  --output /home/ubuntu/agentic-work/artifacts/manifests/memory_estimate.json \
  --model-revision b2cff646eb4bb1d68355c01b18ae02e7cf42d120 --precision bf16 \
  --parameter-count 30000000000 --context-length 32768 \
  --num-layers 48 --num-kv-heads 8 --head-dim 128
```

The memory report is `estimated`; actual vLLM/H100 fit is authoritative. The
optional `calibrate_vllm.sh` wrapper is gated by a fresh first-result marker,
the paid-session gate, and `--allow-calibration`; it uses the exact pinned
vLLM 0.10.0 `bench serve` flags and is not first-session traffic.

For one selected Step-3 case study only, prepare (or run after the separate
authorization and capability checks) a deep profile with isolated output:

```bash
SWE_AGENT_COMMAND="$(sed -n 's/^SWE_AGENT_COMMAND=//p' cloud/lambda/instance_manifest.env)"
bash scripts/observability/deep_profile.sh --mode strace \
  --command "$SWE_AGENT_COMMAND" \
  --output /home/ubuntu/agentic-work/artifacts/profiles/selected.strace \
  --run-id selected --attempt-id syscall-001 --dry-run
bash scripts/observability/deep_profile.sh --mode nsys \
  --command "$SWE_AGENT_COMMAND" \
  --output /home/ubuntu/agentic-work/artifacts/profiles/selected.nsys-rep \
  --run-id selected --attempt-id nsys-001 --dry-run
```

These Level-2 attempts are intrusive and are never mixed into baseline
latency. Native vLLM metrics remain aggregate server observations; only a
direct profiler can support a measured GPU-time claim. Perfetto export is a
deterministic visualization derivative of immutable JSONL and is optional.

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

## Lambda-target validations still required

The analogous checklist passed on the recorded Lightning H100 session. The
following target-specific checks are still required before making a Lambda
claim:

- Lambda Ubuntu x86-64/H100 preflight and model fit;
- vLLM normal completion, parsed tool call, and native `/metrics` counters on
  the Lambda host;
- exact amd64 evaluator image availability at all three digests;
- Lite and Verified gold smoke reports;
- first generated SWE-agent prediction and official evaluation;
- measured artifact export and checksum verification from the Lambda host.

Optional observability validations remain H100-only: actual DCGM field
discovery, nvidia-smi field support, Nsight/strace permissions, measured
profiling overhead, vLLM calibration behavior, and real Perfetto traces. Their
absence does not block the first control trajectory. Thin telemetry is also
deferred until the exported trajectory has a reviewed, lossless normalizer in a
new local bundle.
