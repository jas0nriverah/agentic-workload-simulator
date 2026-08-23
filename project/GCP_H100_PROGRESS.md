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
| `lite-diverse-batch04-6` | Lite | 6 | 0 | 1 | 5 | 0 | `artifacts/batches/lite-diverse-batch04-6/worker-00/attempt-001/evaluator.log` |
| `lite-diverse-batch05-6` | Lite | 6 | 0 | 1 | 5 | 0 | `artifacts/batches/lite-diverse-batch05-6/worker-00/attempt-001/evaluator.log` |
| `lite-diverse-next-6` | Lite | 6 | 0 | 0 | 6 | 0 | `artifacts/batches/lite-diverse-next-6/worker-00/attempt-001/evaluator.log` |
| `lite-production-2` | Lite | 1 | 0 | 1 | 0 | 0 | `artifacts/batches/lite-production-2/worker-00/attempt-001/evaluator.log` |
| `verified-diverse-batch03-6` | Verified | 5 | 1 | 1 | 4 | 0 | `artifacts/batches/verified-diverse-batch03-6/worker-00/attempt-001/evaluator.log` |
| `verified-diverse-batch04-6` | Verified | 5 | 0 | 3 | 2 | 1 | `artifacts/batches/verified-diverse-batch04-6/worker-00/attempt-001/evaluator.log` |
| `verified-diverse-batch05-6` | Verified | 6 | 0 | 1 | 5 | 0 | `artifacts/batches/verified-diverse-batch05-6/worker-00/attempt-001/evaluator.log` |
| `verified-extra-1` | Verified | 1 | 0 | 0 | 1 | 0 | `artifacts/batches/verified-extra-1/worker-00/attempt-001/evaluator.log` |

The first Verified batch has one reproducible incomplete Django environment build; the batch03 retry also has one incomplete environment outcome. Both are retained as incomplete outcomes, not silently removed. The separate `verified-django-retry` reproduced the same `edit_anthropic` environment-install failure and is not counted as a completed evaluation.

The listed GCP artifacts contain 32 completed Lite evaluations (including the additional
`astropy__astropy-14182` run) and 29 completed Verified evaluations (including the
additional `astropy__astropy-14365` run), with two incomplete Verified outcomes in the
listed batches. These totals are descriptive of the recorded batches only; they are not
an assignment-wide resolved-rate claim.

### Additional pinned production runs (2026-08-23)

- Lite `astropy__astropy-14182` (`gcp-h100-additional-lite-20260823`): one worker,
  agent return code 0, official evaluator return code 0, completed, non-empty patch,
  unresolved by the official evaluator. Source/evaluator dataset SHA-256:
  `2a81fb7ede9f2f824ad2a7c093edc1b4114cc7e935e04ab0915de3f60811fb66`.
  Runtime dataset SHA-256:
  `4b0d788ea0adc873297c2ef9aadd0ead1245301fb6b7757129d5b186c3aa3eb6`.
  The worker ran from `2026-08-23T03:58:42Z` to `2026-08-23T04:02:07Z`.
- Verified `astropy__astropy-14365` (`gcp-h100-additional-verified-20260823`):
  one worker, agent return code 0, official evaluator return code 0, completed,
  non-empty patch, unresolved by the official evaluator. Source/evaluator dataset
  SHA-256: `7e5484a2bf332963c2f20c538d1618813e33e2cc6108824869ef8b80f0ddf740`.
  Runtime dataset SHA-256:
  `898a1d687277b62100dcd1cabccf47340168e9f32ebac3daee76cc146cc04d03`.
  The worker ran from `2026-08-23T04:03:52Z` to `2026-08-23T04:08:28Z`.

The two additional runs are measured evidence, not resolved-rate claims. Their exact
worker/evaluator command hashes and raw paths are retained in the corresponding
tracked evidence manifests and on the VM.

## Active/queued work

- The earlier unattended chain `/home/jasonrivera691/eic-work/chain_batches.sh` has completed; no batch worker is currently active.
- The extra Verified run used unused instance `astropy__astropy-12907`; it completed with a non-empty unresolved patch and zero evaluator errors.

## Other measured observability

- Calibration: `artifacts/observability/calibration-20260822.log` (4 requests; mean TTFT 45.01 ms; P99 TTFT 53.69 ms; mean TPOT 6.09 ms; output throughput 156.20 tok/s).
- vLLM metrics scrape: `artifacts/observability/metrics-20260822/vllm.metrics.prom`.
- Passive GPU dmon: `artifacts/observability/gpu-dmon-20260822/gpu.dmon`.
- Request/syscall profile: `artifacts/observability/strace-calibration-20260822/eic-chat.strace`; this is a measured syscall profile, not a GPU-kernel profile.
- Nsight Systems probe: `artifacts/observability/nsys-probe-20260823.nsys-rep` with `nsys-probe-20260823.profile_manifest.json`; host tool path and metadata validated on H100.
- Container CUDA micro-probe: `artifacts/observability/nsys-cuda-micro-20260823.nsys-rep` and its profile manifest. The host Nsight wrapper around `docker exec` produced no CUDA trace rows because the vLLM container namespace is not traced by the host tool; this is a profiling limitation, not a kernel measurement.

## Request-boundary profile (2026-08-23)

`project/GCP_H100_REQUEST_PROFILE_20260823.json` records the additive proxy
profile. The proxy forwarded the same model/tool payloads to the pinned vLLM
server and stored hashes plus timing metadata, never prompts or responses.

- Synthetic matrix: 6 serial requests, all HTTP 200, input lengths 512/4096/16384
  and output lengths 64/512; measured durations 410.088--644.424 ms.
- Real SWE-agent trajectory: 31 request-boundary events, 86 profile events and
  86 telemetry scrapes; agent and official evaluator both returned zero; the
  generated patch was non-empty but unresolved.
- All events share `CLOCK_MONOTONIC_RAW`, hostname
  `instance-20260822-182111`, and boot ID
  `24714e4f-4b78-4c1a-9111-d4c2ca8b4cb0`.
- This closes the lossless request-boundary timing gap. It does **not** assign
  GPU time to requests: `/metrics` remains server-aggregate and the host
  Nsight wrapper still cannot observe CUDA kernels inside the vLLM container.

## H100 CUDA and concurrency measurements (2026-08-23)

- Standalone CUDA calibration is recorded in
  `project/GCP_H100_CUDA_CALIBRATION_20260823.json`. A pinned-container
  `torch 2.7.1+cu128` microbenchmark measured 100 warmed-up float16 matmuls at
  matrix sizes 512, 1024, 2048, and 4096. CUDA-event time ranged from
  0.01249--0.17814 ms per matmul. This is a real GPU measurement, but it is
  deliberately not treated as vLLM or SWE-agent request time and cannot by
  itself calibrate the simulator.
- Synthetic request concurrency is recorded in
  `project/GCP_H100_CONCURRENCY_20260823.json`: 15 HTTP-200 requests through
  the additive proxy at concurrency 1, 2, 4, and 8. Wall time for the groups
  was 34.916, 44.515, 47.395, and 52.974 ms respectively. This is a proxy
  responsiveness measurement, not a task-throughput or resolved-rate claim.
- These measurements extend the evidence base without changing the pinned
  model, vLLM, SWE-agent, evaluator, or experiment settings. The remaining
  simulator blocker is narrowed: a paired control/thin end-to-end record now
  exists, but request-level GPU attribution and a measured simulator holdout still
  do not.

## Paired thin/control record (2026-08-23)

`project/GCP_H100_THIN_20260823.json` records the paired thin-telemetry run for
the same `astropy__astropy-12907` control instance. Both agent and official
evaluator returned zero; the official evaluator marked the generated non-empty
patch unresolved. The run captured 77 telemetry/profile samples with all required
vLLM metric families present, plus a real `counters.unavailable.json` marker.
The host GPU samples and vLLM metrics are server/host aggregate observations; no
per-request GPU time is inferred.

## Interpretation

These are real generated-patch evaluations on the pinned GCP H100 runtime. Resolved-rate claims are limited to the listed batches and are not extrapolated to the assignment target. Profiling, simulator fitting, and report synthesis must use the raw artifacts and preserve incomplete/unresolved outcomes. Remaining high-value work is request-level GPU attribution or an explicit validated limitation, simulator calibration/holdout validation, assignment sweep/report aggregation, and the final requirement audit.
