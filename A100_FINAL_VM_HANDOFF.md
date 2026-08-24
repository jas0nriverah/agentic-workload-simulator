# A100 final validation — fresh VM handoff

This document is self-contained for a fresh Codex session inside an NVIDIA
A100 VM. The frozen H100 experiment is read-only. Do not open, copy, modify,
refit, or regenerate any H100 canonical artifact.

## Release identity and sealed pins

- Required branch: `parallel-h100-shards`
- Desktop release SHA: insert the exact pushed `git rev-parse HEAD` into the
  external startup manifest before execution; never guess it.
- A100 protocol: `configs/a100_final_validation.json`
- A100 protocol SHA-256: `109c825bc799f7f25ffecb1136e1414d894f5a8f758fb242037f5250f4b50788`
- A100 artifacts: external `ARTIFACT_ROOT`, default
  `artifacts/a100_final_validation/` only when running offline
- Recovery root: external `RECOVERY_ROOT`, distinct from the artifact root

The A100 protocol preserves 24 calibration cases × 3 measured repeats with 2
warmups, then 12 sealed holdouts × 3 repeats (8 interpolation and 4
extrapolation). The fit is calibration-only. Predictions and their SHA-256
must exist before any holdout row is opened.

## Expected VM

Exactly one NVIDIA A100 80GB PCIe or SXM4 GPU; Ampere architecture; compute
capability 8.0; at least 80,000 MiB; Linux amd64; Docker with the NVIDIA
runtime; CUDA 13-compatible driver; Nsight Systems 2025.1.3-compatible host
tooling; and no other GPU process. Use the pinned vLLM 0.10.0 image digest,
Qwen3-Coder model revision and tokenizer revision from the protocol. The
startup doctor captures GPU UUID, PCI identity, driver/CUDA, Nsight, kernel,
clock, image, model/tokenizer, and trace provenance.

## One-time external manifest

From the repository root, copy the non-secret template outside Git and replace
only the commit placeholder with the final pushed SHA:

```bash
install -m 600 cloud/gcp/a100_startup_manifest.env.example /mnt/eic-work/a100-startup.env
$EDITOR /mnt/eic-work/a100-startup.env
test "$(git branch --show-current)" = parallel-h100-shards
test -z "$(git status --porcelain)"
git rev-parse HEAD
```

Do not source the manifest. It is parsed as an allowlisted `KEY=VALUE` file;
credentials, shell expressions, and arbitrary commands are not evaluated.

## Offline preflight (safe on any machine)

```bash
python3 scripts/cloud/a100_setup_doctor.py --offline
scripts/cloud/start_a100.sh --dry-run
scripts/cloud/run_a100_final_validation.sh --dry-run
python3 scripts/cloud/a100_case_runner.py --validate-only \
  --config configs/a100_final_validation.json \
  --case-id cal_i128_o32 --split calibration \
  --input-tokens 128 --output-tokens 32 --repeat-id r01 \
  --output-dir /tmp/a100-runner-contract
```

These commands must report the A100 protocol, split, leakage, pins,
artifact-root separation, and four-hour safety deadline. They do not start
Docker, vLLM, Nsight, a server, or a workload.

## Exact live execution

After confirming current provider authorization, billing window, termination
plan, dedicated A100 host, and free disk:

```bash
python3 scripts/cloud/a100_setup_doctor.py \
  --manifest /mnt/eic-work/a100-startup.env
scripts/cloud/start_a100.sh --manifest /mnt/eic-work/a100-startup.env
scripts/cloud/run_a100_final_validation.sh \
  --manifest /mnt/eic-work/a100-startup.env \
  --execute --allow-a100 --phase all
```

The driver seals the protocol, starts a deadline before the first calibration
request, collects calibration, audits all 72 rows, fits only calibration
labels, freezes and hashes predictions, writes a pre-reveal proof, collects
holdout, writes the reveal receipt, scores, performs the adversarial offline
audit, and writes the freeze decision. The hard wall-clock deadline is 14,400
seconds (four hours), with 600-second per-request protection. The driver stops
the named profiled server in its exit cleanup and preserves the recovery root.

## Monitoring

In a second terminal, use read-only checks:

```bash
watch -n 5 nvidia-smi
docker logs --tail 200 -f a100-final-vllm
cat /mnt/eic-work/a100/artifacts/a100_final_validation/deadline.json
cat /mnt/eic-work/a100/artifacts/a100_final_validation/run_state.json
```

GPU usage must belong only to `a100-final-vllm`; unexpected process, port,
image, model, trace, clock, or artifact-root changes are stop conditions.

## Resume and failure handling

Never delete or overwrite a row, recovery root, prediction manifest, or trace.
For a pre-holdout interruption, preserve the external A100 artifact root and
restart the same pinned server, then rerun the driver with `--resume`. It will
reuse only terminal immutable rows, require the existing prediction freeze, and
refuse a changed protocol/split/hash. An unavailable row is evidence, not a
retryable success; preserve it and stop for review. If recovery requires a new
root, use a new external `ARTIFACT_ROOT` and copy only byte-identical sealed
protocol/split/calibration evidence; never copy H100 labels or holdout labels
into it.

If the deadline, GPU isolation, Docker runtime, model/tokenizer pin, Nsight
session, raw checksum, clock, health check, or prediction-before-reveal proof
fails, stop the server, preserve the root, and report the failure. Do not
disable a guard or substitute a synthetic trace provider in production.

## Stop conditions

Stop immediately for any non-A100/40GB GPU, more than one visible GPU, compute
capability other than 8.0, non-Ampere metadata, concurrent GPU process, wrong
branch/SHA, dirty checkout, changed protocol hash, wrong image/model/tokenizer,
missing Docker NVIDIA runtime, missing production Nsight provider, missing
CPU/CUDA/kernel evidence, artifact collision, holdout access before prediction
freeze, checksum mismatch, or deadline expiry.

## Final report format

```text
status: completed | blocked | failed
branch: parallel-h100-shards
pre_run_git_commit: <SHA>
final_git_commit: <SHA>
a100_protocol_sha256: 109c825bc799f7f25ffecb1136e1414d894f5a8f758fb242037f5250f4b50788
split_manifest_sha256: <SHA>
prediction_manifest_sha256: <SHA>
prediction_before_reveal: true | false
hardware: <A100 name / 80GB / Ampere / UUID / PCI>
runtime: <driver / CUDA / Nsight / Docker image digest / model revision>
calibration: 24 cases x 3 repeats, 2 warmups, completed/unavailable
holdout: 12 cases x 3 repeats, 8 interpolation / 4 extrapolation, completed/unavailable
metrics: case-median MAPE, MAE, RMSE, p95 APE, interpolation/extrapolation MAPE, coverage
artifacts: root / inventory SHA / recovery root
leakage_audit: PASS | FAIL
freeze_decision: READY_TO_FREEZE | NOT_READY
```

Remaining blocker on this desktop: live A100 hardware and the authorized
billable execution window. No H100 work is required or authorized.
