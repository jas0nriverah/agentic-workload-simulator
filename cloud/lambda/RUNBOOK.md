# Lambda H100 first-session runbook

This runbook is for the first authorized Lambda Cloud 1x NVIDIA H100 PCIe
80 GB Ubuntu session. It is deliberately narrow: reach one real SWE-agent /
SWE-bench experiment with validated artifacts. Do not launch a large batch,
change the research methodology, publish the repository, or store credentials.

## Before renting

1. Confirm the reviewed commit and create `lambda-ready-<commit>.tar.zst` plus
   its SHA-256 file as a local fallback.
2. Resolve the Linux x86-64 runtime path and evaluator image manifests before
   launch. The instance should not compile CUDA extensions from source.
3. Decide whether a Lambda filesystem is attached in the same region.
4. Complete `cloud/lambda/cloud_session.yaml` locally with the authorized price,
   maximum dollars/GPU-hours, maximum gate, export deadline, and termination
   deadline. This file is untracked and must not contain provider credentials.
5. Prepare `cloud/lambda/first_experiment.yaml` with the reviewed commit and
   selected instance IDs.

## After SSH login

Run inside a persistent `tmux` session. Replace the repository source with the
reviewed Git commit or archive; do not use an unreviewed working tree.

```bash
tmux new -s agentic
git clone <reviewed-repository-source> /home/ubuntu/agentic-workload-simulator
cd /home/ubuntu/agentic-workload-simulator
cp cloud/lambda/instance_manifest.env.example cloud/lambda/instance_manifest.env
$EDITOR cloud/lambda/instance_manifest.env
cp cloud/lambda/cloud_session.yaml.example cloud/lambda/cloud_session.yaml
$EDITOR cloud/lambda/cloud_session.yaml
./scripts/cloud/lambda_preflight.sh
./scripts/cloud/lambda_bootstrap.sh --dry-run
./scripts/cloud/lambda_bootstrap.sh --resume
./scripts/cloud/lambda_start_vllm.sh
./scripts/cloud/lambda_healthcheck.sh
```

The scripts must fail closed if billing authorization, GPU capability, disk,
ports, or required dependencies are not valid. Never paste secrets into shell
history or repository files.

## First experiment sequence

Gold-patch evaluator smoke is an independent prerequisite and may run beside
the model path. It does not replace the generated-prediction evaluation.

```bash
./scripts/cloud/lambda_run_gold_smoke.sh
./scripts/cloud/lambda_run_first_experiment.sh --mode uninstrumented
./scripts/cloud/lambda_collect_results.sh --experiment-id "$EXPERIMENT_ID"
./scripts/cloud/lambda_run_first_experiment.sh --mode thin-telemetry
./scripts/cloud/lambda_collect_results.sh --experiment-id "$EXPERIMENT_ID"
```

The first real trajectory is uninstrumented first, then thinly instrumented
only after the direct path produces a valid prediction. Do not start the
five-instance gate until the root verifies both runs and evaluator outputs.

## Disconnects, limits, and export

- Keep servers/runners inside `tmux`; preserve stdout/stderr paths.
- The host-side scripts stop launching new work at the configured UTC deadline.
- Warnings at 50/75/90% are written to console, log, and state; they are not
  guaranteed to be seen.
- Stopping vLLM, closing SSH, or `shutdown -h` does not terminate Lambda billing.
- At `begin_export_utc`, stop expansion and run collection.
- The user's local machine initiates `rsync` and runs
  `verify_lambda_archive_local.sh` against the SHA-256 manifest.
- At `hard_console_termination_utc`, the user terminates the VM in the Lambda
  console. Persistent filesystem retention/deletion is a separate decision.

## Official references

- https://lambda.ai/instances
- https://docs.lambda.ai/public-cloud/console/
- https://docs.lambda.ai/public-cloud/access-security/
