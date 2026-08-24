# Cross-GPU validation plan

Status: **planned, not launched**. This document freezes the next optional GPU
experiment; it does not start a VM, a vLLM server, a Kineto capture, or a
SWE-agent trajectory. The machine order is A100 80GB first, then H200. The
machine-readable source is
[`configs/cross_gpu_validation.json`](../configs/cross_gpu_validation.json).

## Purpose and claim boundary

The existing H100 matrix is useful evidence for a controlled, same-H100 phase
reconstruction. It uses four measured calibration rows and two predeclared
holdouts, with CPU and CUDA activity phases retained separately. Its measured
E2E holdout MAPE is 10.715632%; that number is not evidence of cross-hardware
prediction, broad SWE-agent generalization, or individual-event prediction.

This plan tests whether the existing simulator can transfer to two named GPU
targets under a frozen workload. It will report two distinct things:

1. **Same-H100 reconstruction (already measured):** a calibration-derived
   reconstruction of the H100 controlled serialized matrix. Holdout labels are
   not used during fitting.
2. **Cross-hardware prediction (future, if launched):** target-hardware
   predictions produced from calibration-only conversion parameters, before
   target holdout labels are revealed. Target holdout labels are used only for
   the final blind error calculation.

The single Astropy run is a real end-to-end anchor, not a simulator calibration
row. One Astropy trajectory per target is enough to compare this fixed case;
it is not enough to support population-wide repository/category ratios.

## Frozen order and software

### Target 1: A100 80GB

Use one dedicated host exposing exactly one **A100 80GB** GPU. The 40GB model,
MIG slices, and multi-GPU configurations are not substitutes. Record the GPU
UUID/SKU, PCI bus ID, memory, clocks, driver, CUDA version, host CPU/RAM,
power mode, and clock identity in the target manifest.

Run the six-row Kineto matrix first, seal its raw package and split manifest,
then run exactly one real `astropy__astropy-12907` SWE-agent Lite trajectory.
No H200 run begins until this package is immutable and its provenance checks
pass.

### Target 2: H200

Use one dedicated host exposing exactly one **H200 SXM, 141GB** GPU. Record the
same host and GPU metadata. Reuse the already sealed workload and split
manifest byte-for-byte. Run the same six-row matrix, then exactly one real
Astropy trajectory. Do not use H200 observations to revise the A100 fit.

### Frozen stack

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`, revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`, BF16.
- vLLM: `0.10.0`, pinned image
  `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`,
  parser `qwen3_coder`, context guard 32,768, TP=1.
- SWE-agent: v1.1.0 at
  `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`.
- SWE-bench: v4.1.0 at
  `726c5461e2ef52d83cf1ea2107870a8bb3328d57`.
- Agent defaults: 30 calls, 2,048 output tokens, 100,000 observation
  characters, temperature 0.0, request seed 0, one worker.

No model, precision, parser, context, tensor parallelism, agent default, or
dataset revision may change between H100, A100, and H200 comparisons.

## Frozen six-row Kineto matrix

All requests are serialized (`concurrency=1`) with one warmup request outside
the measured set. Each measured row requests 64 output tokens and targets the
listed input length. The four calibration rows are fit inputs; the two
holdouts are sealed before any fitting.

| Case | Input-token target | Output-token target | Split | Fit use |
| --- | ---: | ---: | --- | --- |
| `calibration_128` | 128 | 64 | calibration | yes |
| `calibration_512` | 512 | 64 | calibration | yes |
| `calibration_2048` | 2,048 | 64 | calibration | yes |
| `calibration_4096` | 4,096 | 64 | calibration | yes |
| `holdout_1024` | 1,024 | 64 | sealed holdout | no |
| `holdout_3072` | 3,072 | 64 | sealed holdout | no |

The split manifest is hashed before measurement. The fitting process must
assert that no holdout case ID or target-hardware holdout timing appears in its
input. Predictions are materialized and checksummed before holdout observations
are joined for scoring. The two target-hardware holdout labels therefore cannot
influence conversion parameters.

## Real SWE-agent case

Run one isolated `astropy__astropy-12907` SWE-bench Lite trajectory per target,
after that target’s six-row matrix is sealed. Keep the pinned SWE-agent command,
one worker, the same model payload, and the same tool/parser behavior. Preserve
the trajectory, request/event logs, process/NVML samples, vLLM observations,
official evaluator output, resolved configuration, and SHA-256 inventory.

The existing H100 anchor is
`project/GCP_H100_KINETO_TRAJECTORY_20260824.json`. The target trajectory is a
fixed case-study comparison only. It must not be added to the six-row simulator
fit, and a failed evaluator must remain an explicit failed/incomplete artifact.

## Isolation and controls

Each target requires a dedicated one-GPU host with no concurrent model traffic,
benchmark, profiler, or unrelated GPU process. Capture a preflight and a
post-run process list. Keep the vLLM server warm across the six serialized rows,
but run the Astropy trajectory only after the matrix and without concurrent
traffic. The warmup request is not part of any reported row.

For each request record the request ID, token counts, start/end on the shared
monotonic clock, CPU-operation union, CUDA-activity interval union, vLLM
observation reference, host ID, GPU UUID, clock ID, and raw-artifact link.
Record CPU-operation union as traced CPU operations—not all host/tool time.
Record CUDA activity as overlap-aware interval union—not NVML utilization,
process attribution, or a sum of overlapping kernel durations. Keep sampled
process/NVML overlap and kernel timing as separate evidence classes.

Nsight Compute/NCU is optional supplementary evidence. `ERR_NVGPUCTRPERM`,
missing counters, or unavailable NCU must not invalidate the Kineto matrix and
must not justify rerunning a completed workload. This plan has no NCU
dependency.

## Artifact package

Do not put raw weights, credentials, caches, virtual environments, or huge raw
traces in Git. Keep raw traces on durable external storage and commit only
small manifests, checksums, normalized summaries, and derived tables when the
repository policy permits.

For each target, preserve:

```text
artifacts/cross_gpu/<target>/manifest.json
artifacts/cross_gpu/<target>/kineto/calibration/<case_id>/
artifacts/cross_gpu/<target>/kineto/holdout/<case_id>/
artifacts/cross_gpu/<target>/sweagent/astropy__astropy-12907/
artifacts/cross_gpu/<target>/normalized/request_rows.jsonl
artifacts/cross_gpu/<target>/derived/calibration_conversion.json
artifacts/cross_gpu/<target>/derived/holdout_predictions.json
artifacts/cross_gpu/<target>/derived/holdout_metrics.json
artifacts/cross_gpu/<target>/inventory.json
artifacts/cross_gpu/<target>/provenance.json
```

Every normalized row must identify its target, case/instance, split, source
artifact, status, clock identity, GPU identity, and whether a value is
measured, reconstructed, predicted, or unavailable. Raw artifacts are
immutable and checksummed. Failed or empty runs receive an explicit status and
failure reason rather than disappearing from the denominator.

## Simulator evaluation

Use the existing simulator implementation and its additive CPU/GPU/residual
semantics. For each target, estimate the target conversion from the four
calibration rows only. Generate both holdout predictions before loading the
target holdout timings, then calculate E2E error after the sealed join. Report
the denominator and each holdout’s measured/predicted values.

The existing H100 result may be described as a same-H100 controlled phase
reconstruction. A target result may be described as cross-hardware validation
only if the target identity, calibration-only fit, sealed holdout protocol, and
complete provenance chain are present. Do not claim individual-event error
unless every predicted event has a matching measured label. Do not turn one
Astropy trajectory into a category-population or general SWE-agent claim.

## Success and stop criteria

Success requires all six rows per target to have valid raw/normalized artifacts
or explicit unavailable markers; a sealed split and calibration-only fit; two
blind holdout predictions and metrics; and one isolated Astropy trajectory with
its evaluator/provenance artifact. Any pin mismatch, concurrent traffic,
missing clock identity, contamination, or unverifiable raw artifact invalidates
that target package and stops progression to the next target.

This is a validation plan, not a launch instruction. Do not launch either GPU
from this document. If the optional targets are not needed for the final claim,
retain this plan as a reproducible future experiment and keep the current H100
acquisition closed.
