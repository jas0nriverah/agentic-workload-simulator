# Agentic Workload Simulator

Implementation of the Agentic Workload Simulator coding test. The assignment
PDF remains the source of truth. This repository contains the cloud-ready
bootstrap and state machinery plus measured controls from Lightning/Modal and
a completed Google Cloud A3 H100 acquisition. Provider-specific measurements remain
explicitly separate and are never presented as interchangeable results.

## Current status

- Modal control and sweep evidence: the full-prompt Lite control resolved
  officially, and a source-only derived patch also resolved. The original
  Verified control was unresolved, but a final targeted LLM control produced a
  clean one-file patch that resolved officially; all outcomes are preserved in
  separate measured manifests. Four one-instance sweep endpoint sets are now
  measured.
- Shared clock, resolved vLLM configuration, artifact v2, and four-knob
  command contracts passed local validation and independent review
- Linux x86-64 rehearsal and first-trajectory inventory are free/local checks;
  unavailable host tools are reported explicitly and strict CI fails closed
- One paid Lightning H100 measurement window, subsequent Modal H100
  measurements, and a Google Cloud H100 measurement window are recorded
  separately. Raw model patches may contain
  scratch files; clean derived submissions are explicitly labeled as derived.
- Deep profiling includes syscall-level file events, process/NVML sampling, and
  one direct 31-request Astropy Kineto trajectory. A controlled four-row
  calibration/two-row holdout matrix produced 10.715632% limited-scope E2E
  phase-reconstruction MAPE. NCU counters remain permission-blocked, and the
  result is not an individual-event or unseen-workload prediction claim.
- The audited H100 result is frozen in `H100_RESULTS.md`; plot-ready tables,
  provenance, exclusions, and a deterministic inventory are under
  `project/h100_results/`.

## First local checks

```bash
python3 scripts/doctor.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Runtime backends

The simulator has one pinned vLLM protocol and two launch backends. The root
launcher accepts `BACKEND=auto|docker|direct` (or `--backend`); the default is
`auto`.

In `auto`, Docker is selected only when the Docker daemon, NVIDIA Container
Toolkit runtime, pinned image, and a one-GPU H100 access probe all succeed.
Otherwise the launcher selects `direct`. An explicit `--backend docker`
failure is terminal and never falls back to direct. Direct mode never probes
or installs Docker and must run in an already-prepared VM or GPU container; it
does not provide Docker support inside an unprivileged container.

Both backends use the same model revision, tokenizer snapshot, vLLM version,
parser, server arguments, serialized request protocol, feature schema,
calibration/holdout split, prediction freeze, leakage guards, metrics, timeout,
and cleanup rules. Runtime artifacts are separated as
`<artifact-root>/docker/` and `<artifact-root>/direct/`. Do not point either
root at `artifacts/h100_final_validation/`, `project/`, or any canonical H100
artifact directory.

Docker setup requires Linux x86-64, an NVIDIA driver, one isolated compatible
GPU, Docker with the NVIDIA Container Toolkit, the locally available pinned
image, the pinned model cache, the pinned host Nsight Systems binary, Python
developer tools, and `curl`/`git`.
The Docker daemon and GPU capability probe are performed only for a selected
Docker/auto runtime; `--dry-run` never performs them.

Example Docker commands (use a manifest and cache outside the checkout):

```bash
./start.sh --backend docker \
  --manifest /mnt/agentic-work/runtime.env \
  --model-cache /mnt/agentic-work/cache/huggingface \
  --artifact-root /mnt/agentic-work/runtime-artifacts
./start.sh --backend docker \
  --manifest /mnt/agentic-work/runtime.env \
  --artifact-root /mnt/agentic-work/runtime-artifacts \
  --stop
```

Direct setup requires an already-prepared Linux x86-64 VM or GPU container
with one isolated compatible GPU, the pinned driver/CUDA and Nsight tracing
tools, Python 3.11, vLLM `0.10.0`, the tokenizer/model snapshot at revision
`b2cff646eb4bb1d68355c01b18ae02e7cf42d120`, and the common developer tools.
Docker, Docker-in-Docker, and runtime installation are not prerequisites.
The launcher starts vLLM under the host Nsight Systems session in direct mode;
an unprivileged container is never treated as a Docker host.
The direct manifest must identify the same `VLLM_*` pins as the sealed
protocol, and its Python executable must already contain the pinned vLLM
package.

Example direct commands:

```bash
./start.sh --backend direct \
  --manifest /mnt/agentic-work/direct-runtime.env \
  --python /mnt/agentic-work/venv/bin/python \
  --model-cache /mnt/agentic-work/cache/huggingface \
  --artifact-root /mnt/agentic-work/runtime-artifacts
./start.sh --backend direct \
  --manifest /mnt/agentic-work/direct-runtime.env \
  --python /mnt/agentic-work/venv/bin/python \
  --artifact-root /mnt/agentic-work/runtime-artifacts \
  --stop
```

The canonical H100 startup wrapper supports the same selection without
changing the sealed experiment protocol:

```bash
bash scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env \
  --backend auto --dry-run
bash scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env \
  --backend auto
bash scripts/cloud/start_h100.sh --manifest /mnt/eic-work/h100-startup.env \
  --backend direct
```

For an authorized validation run, request artifacts live below the external
trace mount as `<TRACE_ROOT>/<selected-backend>/validation`; lifecycle state
and provenance live below `<WORK_ROOT>/runtime/<selected-backend>`.
The repository's `artifacts/h100_final_validation/` and `project/` trees are
protected and are never used as runtime output roots.

`start.sh --dry-run` checks common developer dependencies and prints the
resolved command without starting vLLM, contacting Docker, touching a GPU, or
writing artifacts. Missing `pytest`/`ruff` are installed only by a non-dry
run; Docker remains optional for direct mode. The common offline checks are:

```bash
bash ./start.sh --dry-run
python3 -m compileall -q src scripts tests
find scripts cloud -type f -name '*.sh' -exec bash -n {} +
find scripts cloud -type f -name '*.sh' -exec shellcheck {} +
git diff --check
```

Environment limitations are part of the protocol: the canonical validation
is H100-only (80 GB class, compute capability 9.0), BF16, tensor parallelism
1, context length 32,768, and the pinned image/model/parser. Direct mode is
not a way to relax those limits. It must also be able to expose the same
request-scoped tracing provider and record host, kernel, GPU, driver/CUDA,
Python, vLLM, model/tokenizer, backend, command, and artifact-root
provenance. Holdout launches require an immutable prediction manifest before
any target label is readable.

Docker and direct results are not interchangeable without validation. They
must be compared only after backend-specific validation of environment,
provenance, request behavior, tracing, and metrics. Canonical H100 artifacts,
hashes, predictions, labels, and reports remain read-only.

The Lambda runbook is at `cloud/lambda/RUNBOOK.md`. It is intentionally
idempotent and does not contain credentials. The first paid control fixture is
preserved and normalized locally by
`scripts/validation/normalize_sweagent_trajectory.py` and checked with
`scripts/validation/validate_normalized_trajectory.py`. Reset-safe interval
accounting and a payload-free first-control summary are implemented locally;
empirical thin telemetry and the four sweeps remain deferred until a fresh
paid-session authorization.

The GCP H100 setup and bounded pilot are documented in
[`cloud/gcp/RUNBOOK.md`](cloud/gcp/RUNBOOK.md). Measured GCP evidence is
indexed in `project/GCP_H100_PROGRESS.md` and
`project/GCP_H100_REQUEST_PROFILE_20260823.json`. Request-aware profiled attempts use
`scripts/observability/request_proxy.py`, which records timing and hashes
without storing prompts or responses.

The simulator implementation is `src/agentic_sim/simulator.py` and the
offline driver is `scripts/analysis/evaluate_simulator.py`. It requires an
explicit measured/calibrated CPU/GPU phase decomposition and rejects aggregate
vLLM counters or GPU utilization as fake per-request GPU time. The measured
controlled-matrix holdout is documented with its narrow validity boundary in
`H100_RESULTS.md`.

See `docs/assignment_traceability.md` for the frozen deliverable-to-evidence
map and `project/PUBLIC_REFERENCE_LOCK.json` for the comparison-only public
reference lock. No public result is claimed by either file.

Use [`docs/REPORT_TEMPLATE.md`](docs/REPORT_TEMPLATE.md) for the final offline
write-up and source every empirical number from the canonical package.

For independent post-control throughput, see
[`cloud/lightning/PARALLEL_RUNBOOK.md`](cloud/lightning/PARALLEL_RUNBOOK.md).
It keeps one vLLM/SWE-agent worker per isolated GPU and writes deterministic,
resume-safe shards without changing the single-control contract.
