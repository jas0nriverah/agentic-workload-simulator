# H100 final validation — fresh VM handoff

This handoff is for a new Codex session on a dedicated H100 host. The protocol
definition is sealed; this startup flow does not authorize or repeat
calibration or holdout. Read this file, then read
`configs/h100_final_validation.json` and
`docs/H100_FINAL_VALIDATION_PROTOCOL.md`; the JSON is the source of truth. The
scope is H100 only. Do not broaden the workload, substitute hardware, start a
generic benchmark, or debug NCU as a prerequisite.

## Required checkout and release identity

The required branch is `parallel-h100-shards`. The desktop release commit is
not known until the final commit is created, so the VM must record the exact
checked-out SHA rather than guessing it. Before execution, verify the branch
and record the SHA used for the run:

```bash
test "$(git branch --show-current)" = parallel-h100-shards
git status --short
git rev-parse HEAD
```

Use that `git rev-parse HEAD` value as `pre_run_git_commit` in the run manifest
and final report. If the handoff was copied before the desktop commit existed,
fetch the branch and repeat the checks after checking out the final pushed
commit. Never run from a dirty checkout or a different branch.

## Canonical deterministic VM startup

Use `scripts/cloud/start_h100.sh` for every fresh or resumed VM. Do not call
the lower-level Nsight launcher directly. The startup entrypoint resolves the
repository root from its own path, verifies the branch/commit/worktree,
sealed protocol hash, external startup manifest, Python lock, and Ubuntu
system-package lock, then verifies Docker, the NVIDIA runtime, one allowlisted
H100, CUDA, Nsight Systems, the pinned image digest, the exact local model
snapshot, and the executable production trace provider. It installs only
missing or lock-mismatched packages, uses cached packages first, and uses
`--require-hashes` for Python installation. It never sources the manifest, so
credentials or arbitrary shell text are not evaluated or logged.

Create the non-secret manifest outside the checkout once per VM. Replace the
commit placeholder with the final pushed commit and preserve every other pin:

```bash
install -m 600 cloud/gcp/h100_startup_manifest.env.example /mnt/eic-work/h100-startup.env
```

Set `REQUIRED_COMMIT` in `/mnt/eic-work/h100-startup.env` to the checked-out
commit, and set the host-local cache, model snapshot, work, and trace paths.
The system lock hash in the template is:

```text
a2507fbf3cb360091c9657ff1a000a975521d8aa360cf6e3f18a3318fb15f1e5
```

The safe preflight is:

```bash
scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env --dry-run
```

After that passes, the one-command startup/reuse flow is:

```bash
scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env
```

This command performs `/health`, `/v1/models`, a normal completion, Qwen
`qwen3_coder` tool parsing, `/metrics`, GPU-process ownership, Nsight session,
and fatal-log checks. A correctly running matching `h100-final-vllm` server is
reused. A stopped, mismatched, duplicate, unhealthy, or otherwise stale
server fails closed. Startup does not run calibration, holdout, fitting,
scoring, or any cloud allocation; those remain explicit separate commands.

The startup trace mount must be outside the repository's validation artifact
roots. Never set it to `artifacts/h100_final_validation/` or an existing
calibration/holdout path.

### Stale-server recovery

The startup script never stops or removes a stale container. Inspect it first:

```bash
docker inspect h100-final-vllm
docker logs --tail 240 h100-final-vllm
nvidia-smi
ss -ltn '( sport = :8000 )'
```

If the container is confirmed stale and no authorized run is using it, clean
up only that container, without touching any repository or artifact root, then
rerun the dry-run and startup commands:

```bash
docker stop h100-final-vllm
docker rm h100-final-vllm
scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env --dry-run
scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env
```

For a duplicate server or occupied port, identify the owning process/container
and resolve it explicitly; do not bypass the startup checks or change the
sealed pins.

Record these placeholders before and after execution; do not guess them:

```text
pre_run_git_commit: <TO_BE_RECORDED_BEFORE_EXECUTION>
protocol_sha256: <COMPUTED_BY_ENTRYPOINT>
final_artifact_inventory_sha256: <TO_BE_RECORDED_AFTER_EXPORT>
final_git_commit: <TO_BE_RECORDED_AFTER_EXPORT>
```

## Safety gate

No paid action is authorized by this repository. Before any non-dry command,
the operator must provide current provider authorization, verify the billing
window and termination plan, and confirm a dedicated host. Do not put tokens,
credentials, model weights, caches, or raw multi-gigabyte traces in Git.

The calibration/holdout driver remains fail-closed: it needs both `--execute`
and `--allow-h100`, validates the protocol hash and host GPU, and refuses to
reuse or overwrite an output root. A dry run is always safe:

```bash
cd /path/to/agentic-workload-simulator
git status --short
python3 -m json.tool configs/h100_final_validation.json >/dev/null
scripts/cloud/run_h100_final_validation.sh --dry-run
```

The dry run must show 24 calibration cases, 12 sealed holdouts, three measured
repeats, two warmups, and no GPU launch. If it reports a mismatch, stop and
resolve it before proceeding.

## Host preflight

Record the output of `uname -a`, `/etc/os-release`, `nvidia-smi -L`,
`nvidia-smi -q`, `docker version`, `python3 --version`, the current Git commit,
and UTC time in the run manifest. Require exactly one allowlisted H100 80 GB
GPU, at least 80,000 MiB, compute capability 9.0, no compute processes, and no
other benchmark or profiler traffic. Check free disk before downloading or
starting the pinned image. Preserve GPU UUID, PCI bus, driver/CUDA, power and
clock state, host/boot/kernel identity, and monotonic clock metadata.

The frozen stack in the JSON must match byte-for-byte: Qwen Coder revision,
BF16, context 32,768, vLLM 0.10.0 image digest, parser `qwen3_coder`, TP=1,
SWE-agent and SWE-bench revisions, and agent defaults. Use an offline/local
model cache only after its revision and file inventory are verified. Start one
vLLM server on the reviewed port and pass health, model, and metrics checks.

## Optional read-only setup doctor

The repository doctor may be used as an additional read-only gate before
starting any service. It is read-only and
never allocates a VM, starts Docker, starts vLLM, invokes the trace provider, or
mutates the validation output root:

```bash
scripts/cloud/h100_setup_doctor.sh --offline
```

It checks the required branch and clean checkout, sealed protocol invariants,
manifest pins, exactly one isolated H100, Docker and the pinned image, the
revision-qualified model snapshot, the executable production trace provider,
and production mode. It must return `READY_FOR_PREFLIGHT`. On a desktop or
other non-GPU host, use `--offline`; that mode is only a repository/config
check and is never an execution authorization:

```bash
scripts/cloud/h100_setup_doctor.sh --offline
```

On a VM, the doctor can be pointed at the external startup manifest after the
startup environment variables are exported. It does not replace the
canonical startup command above:

```bash
scripts/cloud/h100_setup_doctor.sh \
  --manifest /mnt/eic-work/h100-startup.env
scripts/cloud/start_h100.sh \
  --manifest /mnt/eic-work/h100-startup.env
scripts/cloud/h100_setup_doctor.sh \
  --manifest /mnt/eic-work/h100-startup.env --check-server
```

The doctor does not start a service, while `start_h100.sh` owns the pinned
server and the reviewed lower-level Nsight launcher. The case runner owns one
serialized request and its measured trace. A successful doctor or startup does
not authorize paid execution; retain the explicit provider/billing/
termination confirmation and the `--execute --allow-h100` gate.

## Runner interface

The entrypoint does not invent a workload runner. Supply a reviewed executable
with `--runner`. For each case/repeat it invokes:

```text
RUNNER --config CONFIG --case-id ID --split calibration|sealed_holdout \
  --input-tokens N --output-tokens N --repeat-id r01|r02|r03 --output-dir DIR
```

The runner must use the frozen server and prompt/tokenizer, perform no
concurrency, and write an immutable row manifest plus raw-response/trace
references under `DIR`. The row must include request start/end on
`CLOCK_MONOTONIC_RAW`, actual usage counts, status, CPU-operation union,
overlap-aware CUDA activity union, kernel-duration sum as diagnostic only,
hardware/clock identity, source paths, provenance, and SHA-256 values. A
non-zero runner status creates an explicit unavailable row and stops the phase;
it must not be silently retried or counted as success.

The reviewed executable runner is committed at:

```text
scripts/cloud/h100_case_runner.py
```

Validate its CLI contract without contacting a server, starting a workload, or
writing artifacts:

```bash
scripts/cloud/h100_case_runner.py --validate-only \
  --config configs/h100_final_validation.json \
  --case-id cal_i128_o32 --split calibration \
  --input-tokens 128 --output-tokens 32 --repeat-id r01 \
  --output-dir /tmp/h100-runner-contract
```

For an authorized execution, set `H100_VLLM_BASE_URL` to the already-running
server, `H100_MODEL_SNAPSHOT` to the local snapshot whose final directory name
is the sealed model revision, and `H100_TRACE_PROVIDER` to the reviewed
executable that writes `trace_summary.json` under its `--output-dir`. The trace
summary must use schema `h100-trace-summary.v1`, provenance `measured`,
`CLOCK_MONOTONIC_RAW`, the `overlap_aware_request_window` CUDA union rule,
finite non-negative CPU/CUDA/kernel fields, and checksummed raw-artifact
references. The runner refuses missing or invalid telemetry and writes an
unavailable row with a non-zero exit instead of fabricating measurements.

## Execution order

Run calibration first. The command below is illustrative and remains blocked
unless authorization and the reviewed runner are present:

```bash
scripts/cloud/run_h100_final_validation.sh --phase calibration --execute \
  --allow-h100 --runner scripts/cloud/h100_case_runner.py
```

After all calibration artifacts are immutable, fit the predeclared
`h100_feature_latency_v1` model offline using calibration medians only. Persist
`artifacts/h100_final_validation/derived/prediction_manifest.json` with the
protocol/split hash, fit-input hash, model formula/regularization, predictions
for all 12 holdout cases, and prediction hash. Do this before reading holdout
raw labels. Inspect the prediction manifest; no target label may appear in it.

The exact offline transition is:

```bash
python3 scripts/analysis/feature_validation.py fit \
  --config configs/h100_final_validation.json \
  --artifact-root artifacts/h100_final_validation
test -s artifacts/h100_final_validation/derived/prediction_manifest.json
test -s artifacts/h100_final_validation/derived/prediction_manifest.sha256
```

The fit command reads only calibration row manifests, uses the fixed formula
and regularization in the sealed config, and freezes all 12 holdout predictions.
Do not open, copy, or summarize holdout `row.json` files until the prediction
manifest and checksum exist.

Then reveal and measure holdouts with an explicit resume:

```bash
scripts/cloud/run_h100_final_validation.sh --phase holdout --execute \
  --allow-h100 --resume \
  --predictions-manifest artifacts/h100_final_validation/derived/prediction_manifest.json \
  --runner scripts/cloud/h100_case_runner.py
```

The entrypoint must verify the prediction manifest exists, is immutable, and
references the exact split hash before invoking a holdout case. After the run,
write a reveal receipt, join labels, and calculate primary case-median wall MAPE
plus MAE, RMSE, repeat-level, interpolation, extrapolation, p95, coverage, and
all denominators. Keep calibration fit errors separate from sealed holdout
errors.

After holdout collection and receipt creation, score with the exact command:

```bash
python3 scripts/analysis/feature_validation.py score \
  --config configs/h100_final_validation.json \
  --artifact-root artifacts/h100_final_validation
```

After scoring, create the final inventory without adding raw traces to Git:

```bash
python3 - <<'PY'
import hashlib
from pathlib import Path
root = Path("artifacts/h100_final_validation")
lines = []
for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "inventory.sha256"):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    lines.append(f"{digest}  {path.relative_to(root)}")
(root / "inventory.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
sha256sum artifacts/h100_final_validation/inventory.sha256
```

The enforced order is therefore calibration collection → calibration-only fit
and model selection → blind holdout prediction freeze/hash → holdout measurement
→ reveal receipt → scoring. The runner refuses to enter holdout unless
calibration is complete and the prediction manifest is present and checksummed;
it writes the reveal receipt only after all holdout rows are terminal.

## Monitoring and preemption

In a second shell, monitor without changing the run:

```bash
tail -f artifacts/h100_final_validation/run_state.json
nvidia-smi --query-gpu=timestamp,name,uuid,utilization.gpu,memory.used,power.draw --format=csv -l 5
```

On preemption, interruption, or a runner failure, stop and preserve the output
root. Inspect `run_state.json` and the affected case directory, correct the
external cause, then resume with the same commit, config, output root, and
prediction manifest:

```bash
scripts/cloud/run_h100_final_validation.sh --phase calibration --execute \
  --allow-h100 --resume --runner scripts/cloud/h100_case_runner.py
# or, after calibration fit is already frozen:
scripts/cloud/run_h100_final_validation.sh --phase holdout --execute \
  --allow-h100 --resume \
  --predictions-manifest artifacts/h100_final_validation/derived/prediction_manifest.json \
  --runner scripts/cloud/h100_case_runner.py
```

Never delete a partial row, clear a lock, change the split, or rerun an
unavailable row without review. Stop immediately on any stop condition in the
protocol, including GPU/pin mismatch, concurrent traffic, clock-join failure,
output collision, checksum mismatch, changed protocol/split hash, or a missing
prediction receipt.

## Failure and resume rules

Stop on any GPU identity/pin mismatch, concurrent process, clock-join failure,
port collision, changed protocol or split hash, output collision, missing raw
checksum, missing prediction receipt, or contamination. Do not clear a lock or
delete a partial case to make a run resume. Inspect `run_state.json`, preserve
the failed artifact, and resume only with the same output root and hashes after
the cause is reviewed. NCU permission errors are supplementary unavailable
evidence; they do not invalidate complete Kineto rows and do not justify a
rerun.

Before handoff, verify:

```bash
bash -n scripts/cloud/run_h100_final_validation.sh
python3 -m json.tool configs/h100_final_validation.json >/dev/null
git diff --check
```

Return the final protocol hash, host/GPU manifest, run-state status, raw-artifact
inventory, prediction-before-reveal receipt, holdout metrics, unavailable-row
reasons, and the final Git commit/hash. If no authorized host or reviewed
runner is available, report `not launched` and leave this scaffold unchanged.

## Final report format

Return a compact report containing exactly these fields:

```text
status: completed | incomplete | not launched
required_branch: parallel-h100-shards
pre_run_git_commit: <40-hex SHA>
final_git_commit: <40-hex SHA or not exported>
protocol_sha256: <64-hex>
split_manifest_sha256: <64-hex>
prediction_manifest_sha256: <64-hex>
prediction_before_reveal: true | false
hardware_runtime: <GPU UUID/name, memory, driver/CUDA, host/kernel, image digest>
calibration: <24 cases x 3 repeats, warmups, completed/unavailable counts>
holdout: <12 cases x 3 repeats, interpolation/extrapolation counts>
metrics: <MAPE, MAE, RMSE, p95 APE, coverage, denominators>
unavailable_rows: <case/repeat and reason, or none>
artifact_inventory_sha256: <64-hex>
```
