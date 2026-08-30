# Agentic Workload Simulator

A reproducible tool for estimating workload latency and checking those
estimates against measured GPU runs.

The project is designed to answer one focused question: **can a workload’s
latency be estimated from information known before it runs?** It keeps the
estimate separate from the measurement so the result can be checked honestly.

## What it does

- Builds estimates from declared workload and hardware information.
- Uses calibration runs to fit the estimator.
- Tests predictions on a sealed holdout set that the estimator cannot see.
- Records results, checksums, and run details so results can be reviewed.
- Detects the available operating system, GPU, Docker support, and required
  tools before starting work.
- Supports NVIDIA H100 and A100 validation, with Docker and supported direct
  host-runtime paths.

## Main features

- **Feature-only predictions:** measured timing is never used as an input to
  the predictor.
- **Sealed testing:** predictions are frozen before holdout measurements are
  revealed.
- **Leakage protection:** the workflow stops if target information enters the
  prediction path or if the experiment order is unsafe.
- **Docker support:** uses an NVIDIA-enabled Docker runtime when it is
  available and passes its checks.
- **Non-Docker support:** can use a supported direct host runtime when Docker
  is unavailable; it stops safely if that environment is not valid.
- **Automatic preflight:** checks the machine, GPU, model, runtime, and output
  locations before a live run.
- **Reusable setup:** `start.sh` installs common tools, creates the Python
  environment, runs local checks, and is safe to run again.

## Quick start

### From a new Ubuntu/Debian VM

The commands below prepare a fresh VM and check the checkout without starting
Docker, vLLM, Nsight, a GPU workload, or an experiment. If a hosted Studio is
still initializing, wait for its setup indicator to finish before retrying
`apt-get`.

```bash
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  git gh curl ca-certificates jq unzip build-essential \
  python3 python3-venv python3-pip shellcheck
gh auth login
# Clone the validated branch used by the current A100 workflow.
git clone --branch parallel-h100-shards \
  https://github.com/jas0nriverah/agentic-workload-simulator.git
cd agentic-workload-simulator
./start.sh --dry-run
./start.sh
```

Managed Studios that provide one pre-created Conda environment may point the
setup at it instead of creating another virtualenv:

```bash
AGENTIC_VENV="$CONDA_PREFIX" ./start.sh
```

For a public checkout, `gh auth login` is optional; for a private repository
or to push changes, complete it first. Use the explicit `git clone --branch`
command above rather than an unqualified `gh repo clone`, which defaults to
`main`. Replace the branch only when you intentionally want another branch.

Codex is optional. You may use any LLM-enabled CLI available in your
environment; Codex is not required for the simulator itself. On a headless
VM, first save the diagnostic output, then give it to your chosen assistant:

```bash
./start.sh --diagnose --json > setup-diagnostics.json
```

Use this prompt:

```text
Read setup-diagnostics.json and this repository's README. Resolve every
legitimate setup issue using documented, reversible fixes. You may install
missing packages and select the Docker or direct backend, but do not fabricate
telemetry, bypass safety checks, alter canonical experiment artifacts, or start
measurements until diagnostics pass. Rerun the diagnostics after each fix and
report any remaining provider or hardware requirement exactly.
```

Headless Codex install example:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex login --device-auth
```

On macOS, install GitHub CLI, Git, curl, Python, and Node.js with Homebrew,
then clone the repository and run `./start.sh`.

### From an existing checkout

```bash
./start.sh
source .venv/bin/activate
```

To inspect the setup plan without changing anything:

```bash
./start.sh --dry-run
```

To check a GPU VM before a live run:

```bash
./start.sh --check-only --require-docker --require-gpu
```

For a direct/non-Docker environment, omit `--require-docker`. The check still
fails closed if the selected runtime, model, telemetry, or artifact paths are
not valid for the requested experiment.

The default setup never starts Docker, a model server, a GPU workload, or an
experiment.

### A100 diagnostic preflight (no execution)

To collect every A100 safety check in one report without starting Docker,
vLLM, Nsight, calibration, holdout, scoring, or measurement work:

```bash
python3 scripts/cloud/a100_diagnostic.py \
  --manifest /mnt/eic-work/a100-startup.env \
  --json-out /tmp/a100-diagnostic.json
```

The command prints a human-readable pass/fail table and writes the same
results as machine-readable JSON. It runs hardware, Docker/NVIDIA runtime,
pinned image, model/tokenizer, Nsight path, external artifact-root, Git and
protocol, disk, and deadline checks independently, so one failure does not
hide the others. A nonzero exit status means at least one remediation is
required. The diagnostic never pulls an image, starts a container or server,
opens holdout data, writes canonical experiment artifacts, or fabricates
traces or timing data.

## Assignment Steps 1–3 and event-level validation

The submission target is the complete assignment: all 300 Lite tasks and all
500 Verified tasks for a public-scoreboard claim, complete same-trajectory
tool/model/E2E telemetry, four shared-cohort hyperparameter sweeps, a
post-Step-1 high-ratio case study, and Deliverable 9 event/E2E errors within
25%. The historical 32+32 H100 cohort is not a substitute for that target.

The feature-only H100 validation and the repository-level profiling needed by
the coding-test report are separate runs. The feature validation does not
produce one CPU/GPU timing pair for every SWE-bench repository. To finish that
data collection on a Google Cloud H100, use the dedicated runbook:

[`docs/H100_REPOSITORY_PROFILING_RUNBOOK.md`](docs/H100_REPOSITORY_PROFILING_RUNBOOK.md)

The profiling pass uses the existing pinned SWE-agent, Qwen/vLLM, and
SWE-bench revisions with serialized concurrency. Its assignment-facing ratio
is the wall-time phase ratio from the same complete trajectory:

```text
sum(tool-call wall_ms) / sum(model-request wall_ms)
```

CPU activity, CUDA activity, Kineto/Nsight traces, NVML samples, process
overlap, and kernel-duration sums remain useful diagnostics, but they do not
replace either side of that ratio. The assignment pipeline reuses exact
evidence where available and otherwise recollects the complete Lite/Verified
and sweep rows, normalizes tool/model events, regenerates Steps 1–3 figures,
and evaluates both per-event and end-to-end prediction error. The historical
GCP VM disk was wiped, so new output must use persistent storage. See the
completion and traceability documents before acquiring more GPU data.

## Runtime choices

The project can work in two supported ways:

1. **Docker path:** runs the pinned workload in an NVIDIA-enabled container.
2. **Direct path:** runs through the host environment when the required
   direct backend and telemetry are available.

The startup checks choose or validate the supported path from the environment.
They fail closed when Docker, the GPU, the model, or the required measurement
tools are missing. A restricted GPU Pod is not treated as a full VM.

## Current validation scope

The live validation work targets NVIDIA CUDA GPUs, specifically the H100 and
A100. The offline simulator can run without an NVIDIA GPU because it only uses
declared features and saved calibration data.

### Environments tested

- **[Google Cloud](https://cloud.google.com/) — NVIDIA H100 80GB HBM3:** the
  historical feature-only sealed validation completed on a Linux NVIDIA VM,
  using the pinned Qwen/vLLM stack and production GPU tracing. It does not
  complete the assignment's population Steps 1–3 or event-level evaluation.
- **[Lightning AI](https://lightning.ai/) — NVIDIA A100 80GB PCIe:** hardware,
  Docker/NVIDIA runtime, model, Nsight, storage, and artifact-isolation
  diagnostics passed on a Linux GPU Studio; the separate A100
  calibration/holdout run is the cross-GPU validation step.
- **Ubuntu 22.04 / Python 3.11 Linux rehearsal:** setup, dry-run, leakage,
  deterministic-manifest, and resume checks passed without GPU execution.

These results describe tested environments, not a guarantee for every cloud
provider or VM image. Restricted GPU containers without host Docker/NVIDIA
runtime access are unsupported for the Docker path; use a full GPU VM or the
validated direct-runtime path instead.

Provider note: managed Studios such as Lightning may add setup time because
they can provide a pre-created Conda environment, restricted filesystem paths,
containerized Docker access, or differently mounted Nsight tools. The first
setup/preflight on a new instance can therefore take longer while the runtime
and paths are verified. Later runs on the same prepared instance should be
substantially faster.

AMD/ROCm, Apple Metal, and other non-NVIDIA live backends are future
engineering work and future implementation. They would need their own runtime,
hardware checks, measurement tools, leakage tests, and calibration runs.

## Documentation

- [`H100_RESULTS.md`](H100_RESULTS.md) — frozen H100 result
- [`docs/CROSS_GPU_VALIDATION_PLAN.md`](docs/CROSS_GPU_VALIDATION_PLAN.md) —
  A100 validation plan
- [`docs/REPORT_TEMPLATE.md`](docs/REPORT_TEMPLATE.md) — report format
- [`cloud/gcp/RUNBOOK.md`](cloud/gcp/RUNBOOK.md) — GCP setup and runbook
- [`docs/H100_REPOSITORY_PROFILING_RUNBOOK.md`](docs/H100_REPOSITORY_PROFILING_RUNBOOK.md) —
  H100 repository-level CPU/GPU data collection
- [`docs/CPU_PACE_VLLM_TUNNEL_RUNBOOK.md`](docs/CPU_PACE_VLLM_TUNNEL_RUNBOOK.md) —
  reusable CPU-to-PACE VPN and SSH tunnel setup for H100 vLLM runs
- [`docs/ASSIGNMENT_COMPLETION_RUNBOOK.md`](docs/ASSIGNMENT_COMPLETION_RUNBOOK.md) —
  recovery-first Steps 1–3 and Deliverable 9 workflow
- [`docs/assignment_traceability.md`](docs/assignment_traceability.md) —
  measured evidence, claim boundaries, and exact remaining work
- [`cloud/lambda/RUNBOOK.md`](cloud/lambda/RUNBOOK.md) — Lambda runbook
- [`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md) —
  independent parallel runs

The detailed implementation is in `src/agentic_sim/`, and cloud launchers are
under `scripts/cloud/`. Historical provider measurements remain separate and
are never presented as interchangeable results.
