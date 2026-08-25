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

```bash
sudo apt-get update
sudo apt-get install -y git gh curl ca-certificates
gh auth login
gh repo clone jas0nriverah/agentic-workload-simulator
cd agentic-workload-simulator
./start.sh
```

For a private repository, complete `gh auth login` before cloning. On macOS,
install Git, GitHub CLI, curl, and Python with Homebrew, then clone the repo
and run `./start.sh`.

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

The default setup never starts Docker, a model server, a GPU workload, or an
experiment.

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

AMD/ROCm, Apple Metal, and other non-NVIDIA live backends are future
engineering work and future implementation. They would need their own runtime,
hardware checks, measurement tools, leakage tests, and calibration runs.

## Documentation

- [`H100_RESULTS.md`](H100_RESULTS.md) — frozen H100 result
- [`docs/CROSS_GPU_VALIDATION_PLAN.md`](docs/CROSS_GPU_VALIDATION_PLAN.md) —
  A100 validation plan
- [`docs/REPORT_TEMPLATE.md`](docs/REPORT_TEMPLATE.md) — report format
- [`cloud/gcp/RUNBOOK.md`](cloud/gcp/RUNBOOK.md) — GCP setup and runbook
- [`cloud/lambda/RUNBOOK.md`](cloud/lambda/RUNBOOK.md) — Lambda runbook
- [`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md) —
  independent parallel runs

The detailed implementation is in `src/agentic_sim/`, and cloud launchers are
under `scripts/cloud/`. Historical provider measurements remain separate and
are never presented as interchangeable results.
