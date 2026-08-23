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

The listed GCP artifacts contain 34 completed Lite evaluations (including the additional
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

### Controlled temperature condition (2026-08-23)

- `gcp-h100-temp02-lite-20260823` reused the pinned two-row Lite production set
  with only `agent.model.temperature` changed from the frozen baseline `0.0` to
  `0.2`; model, vLLM, dataset, evaluator, call-limit, output-limit, and
  observation-limit settings were unchanged.
- Both instances completed agent and official evaluation with return code 0;
  both generated non-empty patches were unresolved, with zero incomplete and zero
  evaluator-error outcomes. Exact hashes are in
  `project/GCP_H100_TEMP02_LITE_20260823.json`.
- This is one measured point in the assignment temperature sweep, not the full
  four-point sweep and not a resolved-rate claim.

### Call-limit condition (2026-08-23; sweep expansion stopped)

- `gcp-h100-calls20-lite-20260823` reused the same two pinned Lite rows while
  changing only `agent.model.per_instance_call_limit` from the frozen baseline
  `30` to `20`. The worker and evaluator commands both returned zero.
- The worker status is `completed`, but the evaluator report records **2
  submitted, 1 completed, 0 incomplete, 0 resolved, 1 unresolved, 1 empty
  patch, and 0 errors**. The empty patch is `astropy__astropy-12907`; the
  other submitted prediction is non-empty and unresolved. This discrepancy is
  preserved exactly in `project/GCP_H100_CALLS20_LITE_20260823.json` and is
  not presented as a two-instance resolved-rate result.
- This is the final additional sweep condition for the current execution. No
  further temperature, call-limit, token-limit, or observation-limit runs are
  queued unless a later gap audit demonstrates that an assignment deliverable
  is otherwise unsupported.

## Active/queued work

- The earlier unattended chain `/home/jasonrivera691/eic-work/chain_batches.sh` has completed; no batch worker is currently active.
- The call-limit worker completed at `2026-08-23T04:36:25Z`; no additional
  hyperparameter sweep condition is queued. Remaining work is limited to
  named timing/profiling/calibration/simulator/diversity gaps below.

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

## CPU/tool syscall profile (2026-08-23)

`project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json` records one real
115-second SWE-agent trajectory for `astropy__astropy-12907` under `strace`
(`file,process,network`) with a concurrent 100-ms H100 sampler. The run
produced 31 successful model-request boundaries, 561,374 prompt tokens,
7,535 completion tokens, 27,843 syscall lines (25,423 file-operation lines,
46 process-operation lines, 561 network-operation lines), and 835 H100
samples (414 active; mean utilization 40.23%, maximum 95%). Raw files and
hashes remain on the VM; raw logs are not copied into Git. The prediction was
empty and exited `exit_cost`, so this is timing/profiling evidence only, not an
official resolved result. The proxy and sampler share `CLOCK_MONOTONIC_RAW`;
strace `-ttt` timestamps are realtime and require an explicit offset before
any event-level merge.

## H100 CUDA and concurrency measurements (2026-08-23)

## Direct CPU/GPU timing case study (2026-08-23)

`project/GCP_H100_CPU_GPU_CASE_STUDY_20260823.json` records four serial HTTP-200
requests sent directly to the pinned vLLM server while `nvidia-smi dmon` sampled
the H100 at one-second intervals. Request wall durations were 404.588, 397.925,
398.469, and 399.174 ms (mean 400.039 ms; p50 398.822 ms). The sampler observed
an active aggregate row at 85% SM, 39% memory, and 268 W, with idle rows at 0%
SM and 0% memory outside the request window.

This closes the coarse request-level CPU/wall-versus-aggregate-GPU case-study
gap. It does not provide per-request GPU time: the probe's `CLOCK_MONOTONIC`
timestamps are not directly mergeable with the trajectory profile's
`CLOCK_MONOTONIC_RAW` timestamps, and `nvidia-smi dmon` is aggregate. The raw
files remain on the VM with SHA-256 values in the tracked manifest. The probe is
also deliberately not an SWE-agent trajectory or simulator-calibration record.

### Higher-frequency follow-up probe

`project/GCP_H100_CPU_GPU_CASE_STUDY_HIRES_20260823.json` adds six serial
direct-vLLM requests with 100-ms `nvidia-smi` sampling. All six returned HTTP
200; wall durations were 399.788--402.308 ms (mean 401.307 ms). Across 47 GPU
samples, 35 had nonzero SM utilization (maximum 86%; mean 41.809%) and mean
power was 187.825 W. This strengthens the temporal case-study evidence but
remains aggregate GPU sampling, not per-request GPU attribution, and is not a
simulator-calibration record.

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

## Aligned request/GPU overlap probe (2026-08-23)

`project/GCP_H100_ALIGNED_REQUEST_GPU_20260823.json` records six serial direct
vLLM requests (four predeclared calibration rows and two predeclared holdout
rows) with request start/end and 50-ms H100 samples on the same
`CLOCK_MONOTONIC_RAW` clock. All six returned HTTP 200. Aggregate GPU
utilization overlapped each request, with 51 total samples, 35 active samples,
35.196% mean utilization, and 85% maximum utilization. The raw request,
sample, and metrics files remain on the VM with recorded SHA-256 values. This
provides aligned aggregate overlap only: it does not assign device seconds per
request, and this probe did not produce validated vLLM counter deltas.

## Paired thin/control record (2026-08-23)

`project/GCP_H100_THIN_20260823.json` records the paired thin-telemetry run for
the same `astropy__astropy-12907` control instance. Both agent and official
evaluator returned zero; the official evaluator marked the generated non-empty
patch unresolved. The run captured 77 telemetry/profile samples with all required
vLLM metric families present, plus a real `counters.unavailable.json` marker.
The host GPU samples and vLLM metrics are server/host aggregate observations; no
per-request GPU time is inferred.

## Interpretation

## H100 CUDA-event/utilization calibration (2026-08-23)

An isolated in-container Torch matmul probe recorded CUDA-event execution time while a host sampler captured 50 ms `nvidia-smi` utilization samples on the same `CLOCK_MONOTONIC_RAW` clock. Three sizes completed successfully (1024×20, 2048×10, 4096×5; CUDA-event durations 1.009 ms, 0.474 ms, and 0.916 ms). The aggregate sampler observed zero or near-zero overlap because each kernel completed between samples. This is boundary evidence about sampler resolution, not a valid per-request vLLM GPU-seconds calibration; raw provenance is preserved in `GCP_H100_GPU_UTIL_CUDA_EVENT_CALIBRATION_20260823.json`.

These are real generated-patch evaluations on the pinned GCP H100 runtime. Resolved-rate claims are limited to the listed batches and are not extrapolated to the assignment target. Profiling, simulator fitting, and report synthesis must use the raw artifacts and preserve incomplete/unresolved outcomes. The remaining high-value work is, in order: request-level CPU/model/GPU timing evidence, profiling for the CPU/GPU case study, vLLM calibration for the simulator, held-out simulator validation/error, repository/category diversity for final plots, and only then any sweep condition shown to be required by the assignment.

## 2026-08-23 C — aligned request/NVML profile

- Completed one pinned Lite trajectory: astropy__astropy-12907 (gcp-request-profile3, nvml-01); agent and official evaluator exited 0, with the generated patch officially unresolved.
- Captured 16,819 direct NVML samples at 20 ms cadence using CLOCK_MONOTONIC_RAW on the same host/boot as the proxy. The proxy file contained 67 boundaries; 31 fell inside this sampler window and were retained as the aligned subset.
- Compact evidence: project/GCP_H100_REQUEST_PROFILE_20260823C.json and .md. The aligned subset covers 31 requests, 46,025.916 ms request duration, 427,737 tokens, and an aggregate 33.051809 GPU-active-second estimate from utilization integration.
- This is aggregate NVML utilization overlap, not exact per-request kernel/device time. It improves timing evidence but does not close the profiler-derived gpu_seconds_at_reference requirement or justify simulator fitting.

## 2026-08-23 D — 5 ms aligned request/NVML profile

- Completed a second pinned Lite trajectory with the same Astropy instance and unchanged model/agent settings; agent_rc=0 and evaluator_rc=0, official generated patch unresolved.
- Captured 49,299 direct NVML samples at approximately 5 ms target cadence. The proxy file contained 99 boundaries across prior and current runs; 31 fell inside this sampler window.
- Compact evidence: project/GCP_H100_REQUEST_PROFILE_20260823D.json and .md. The aligned subset covers 31 requests, 39,933.499 ms request duration, 425,826 tokens, and an aggregate 27.545960 GPU-active-second estimate from utilization integration.
- This improves temporal resolution for the aggregate CPU/model/GPU case study but remains an NVML utilization estimate, not exact per-request kernel/device time; simulator fit/holdout remains blocked by the existing contract.

## Request profile E (2026-08-23)

- Completed pinned Astropy Lite run gcp-request-profile5 / nvml-10 with unchanged Qwen/vLLM/SWE-agent settings; agent_rc=0 and evaluator_rc=0.
- Official evaluator completed with resolved=0 (unresolved outcome retained).
- Captured 81,113 direct NVML samples at approximately 5 ms target cadence and 3,940 vLLM metric snapshots at 100 ms cadence.
- Aligned 31 proxy request boundaries on the same host, boot identity, and CLOCK_MONOTONIC_RAW. Aggregate NVML utilization-overlap estimate: 25.060305 GPU-active seconds across 423,806 proxy tokens; this is an aggregate utilization estimate only.
- Aggregate vLLM deltas: 418,748 prompt tokens, 5,058 generation tokens, 31 successful requests, 34.471246 s E2E histogram sum, 34.406256 s inference sum, 0.858443 s TTFT sum.
- Compact artifacts: project/GCP_H100_REQUEST_PROFILE_20260823E.json and .md.
- Limitation remains: no exact per-request GPU/kernel attribution; do not use this aggregate estimate as simulator gpu_seconds_at_reference.

## 2026-08-23E calibration checkpoint

- Completed pinned vLLM serving calibration on the live H100 at input lengths 128, 512, and 2048 tokens; 16 prompts per condition, output length 64, concurrency 1.
- Compact evidence: project/GCP_H100_VLLM_CALIBRATION_20260823E.json and .md.
- Measured median TTFT: 24.84 / 33.80 / 62.84 ms; median TPOT: 6.33 / 5.94 / 6.03 ms; output throughput: 151.68 / 157.84 / 145.19 tok/s (128 / 512 / 2048 input).
- This is service calibration only. No GPU-time claim, fit, or holdout error was fabricated; aggregate NVML remains insufficient for gpu_seconds_at_reference.

## 2026-08-23 G — request-level profile with 5 ms NVML + vLLM metrics
- Completed one pinned uninstrumented Lite SWE-agent trajectory for `astropy__astropy-12907` through the local request proxy; agent and official evaluator both exited 0. The generated patch was officially unresolved and is retained as an outcome.
- Captured 31 serialized request boundaries, 2,186 vLLM metric snapshots, and 45,108 direct NVML samples. All aligned records share the same host, boot identity, and `CLOCK_MONOTONIC_RAW` clock.
- Compact evidence: `project/GCP_H100_REQUEST_PROFILE_20260823G.json` and `.md`. Aggregate proxy duration was 58.788029 s over 432,289 prompt and 8,388 completion tokens. vLLM cumulative deltas were 57.900302 s E2E, 57.835214 s inference, 0.002357 s queue, 0.903220 s TTFT, and 57.004277 s TPOT. NVML utilization-overlap estimate was 44.192767 active seconds.
- The NVML value is explicitly a utilization-integral proxy, not exact per-request device time or kernel attribution. It must not be used as `gpu_seconds_at_reference` for simulator fitting.
- This closes the serialized request-boundary/timing evidence gap and strengthens the CPU/model/GPU case study. Exact per-request GPU attribution and NCU hardware-counter profiling remain blocked by the container/permission boundary.
