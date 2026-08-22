# GCP H100 measured progress

This file records only measured Google Cloud H100 evidence from the live session. Modal evidence remains in its existing files and is not mixed into these counts.

## Runtime

- VM: `instance-20260822-182111` in `us-central1-a`
- GPU: 1x NVIDIA H100 80GB HBM3
- Model: Qwen/Qwen3-Coder-30B-A3B-Instruct, revision `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`
- vLLM: 0.10.0, image digest `sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`
- SWE-agent: pinned detached checkout used by the runner
- Raw artifacts: `/home/jasonrivera691/eic-work/artifacts` (not committed)

## Completed evaluated batches

| Batch | Dataset | Completed | Incomplete | Resolved | Unresolved | Empty patches | Evidence |
|---|---:|---:|---:|---:|---:|---:|---|
| `lite-diverse-6` | Lite | 6 | 0 | 2 | 4 | 0 | `artifacts/batches/lite-diverse-6/worker-00/attempt-001/evaluator.log` |
| `verified-diverse-6` | Verified | 5 | 1 | 3 | 2 | 0 | `artifacts/batches/verified-diverse-6/worker-00/attempt-001/evaluator.log` |
| `lite-diverse-next-6` | Lite | 6 | 0 | 0 | 6 | 0 | `artifacts/batches/lite-diverse-next-6/worker-00/attempt-001/evaluator.log` |
| `verified-diverse-next-6` | Verified | 6 | 0 | 2 | 4 | 0 | `artifacts/batches/verified-diverse-next-6/worker-00/attempt-001/evaluator.log` |
| `lite-diverse-batch03-6` | Lite | 6 | 0 | 3 | 3 | 0 | `artifacts/batches/lite-diverse-batch03-6/worker-00/attempt-001/evaluator.log` |

The first Verified batch has one reproducible incomplete Django environment build; it is retained as an incomplete outcome, not silently removed.

## Active/queued work

- `verified-diverse-batch03-6`: active during this update; its worker and Docker build are alive and producing trajectories.
- `lite-diverse-batch04-6`: queued to start automatically after Verified batch03 exits.
- `verified-diverse-batch04-6`, `lite-diverse-batch05-6`, and `verified-diverse-batch05-6`: planned and chained sequentially.
- The unattended chain is `/home/jasonrivera691/eic-work/chain_batches.sh`; it is outside Git and does not alter experiment settings.

## Other measured observability

- Calibration: `artifacts/observability/calibration-20260822.log` (4 requests; mean TTFT 45.01 ms; P99 TTFT 53.69 ms; mean TPOT 6.09 ms; output throughput 156.20 tok/s).
- vLLM metrics scrape: `artifacts/observability/metrics-20260822/vllm.metrics.prom`.
- Passive GPU dmon: `artifacts/observability/gpu-dmon-20260822/gpu.dmon`.
- Request/syscall profile: `artifacts/observability/strace-calibration-20260822/eic-chat.strace`; this is a measured syscall profile, not a GPU-kernel profile.

## Interpretation

These are real generated-patch evaluations on the pinned GCP H100 runtime. Resolved-rate claims are limited to the listed batches and are not extrapolated to the assignment target until the planned coverage is complete. Profiling, simulator fitting, and report synthesis must use the raw artifacts and preserve incomplete/unresolved outcomes.
