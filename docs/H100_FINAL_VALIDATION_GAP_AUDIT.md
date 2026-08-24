# H100 final-validation gap audit

**Audit date:** 2026-08-24 UTC
**Audit basis:** the canonical H100 result package, the current checkout, and
the sealed feature-only validation protocol. This document is an evidence
inventory, not a claim that the new protocol has been run.

## Current measured baseline

The immutable H100 package already contains:

- 32/32 completed Lite evaluations (8 resolved) and 29/32 completed Verified
  evaluations (10 resolved); 61 completed evaluations in total.
- The controlled Kineto trajectory with 31/31 request intervals joined to
  process samples, all 31 containing the vLLM worker, 3,456 process rows, 385
  worker samples, approximately 92% peak SM utilization, and approximately
  47% peak memory. The package also records approximately 11.4 million CUDA
  kernel events and 56.06 seconds of overlap-aware CUDA activity.
- The historical phase-reconstruction simulator result: 10.715632% MAPE and
  0.162826 seconds MAE over its two declared holdouts. This uses measured
  CPU/CUDA phase labels and remains separate from the new feature-only model.
- An NCU capability result showing `ERR_NVGPUCTRPERM`. NCU is supplementary,
  not a gate, and is not a reason to spend more H100 time.

These artifacts are preserved as historical measured evidence. The final
validation protocol must not rewrite them or silently change their semantics.

## Requirements already supported

| Requirement | Evidence | Status |
| --- | --- | --- |
| Frozen Qwen/vLLM/SWE-agent/SWE-bench H100 stack | `H100_RESULTS.md`, canonical manifests, `configs/h100_final_validation.json` | Supported for the declared H100 configuration |
| Lite and Verified completion/resolution counts | canonical evaluator CSV/JSON | Supported, with 3 incomplete Verified selections explicitly retained |
| Request/process/GPU attribution for the controlled case study | Kineto/process attribution manifests | Supported for the measured controlled case |
| H100 hardware and single-GPU focus | H100 preflight and canonical hardware manifests | Supported for the historical H100 runs |
| Historical phase-reconstruction simulator check | `project/h100_results/` and `H100_RESULTS.md` | Supported only for that measured-phase claim |
| NCU limitation documented | `project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json` | Supported as an unavailable supplementary measurement |

## Gaps requiring new GPU-dependent measurements

### P0 — feature-only simulator calibration and sealed holdout

The historical 10.715632% result is not blind prediction: it uses measured
CPU-exclusive and CUDA-union phases. No current artifact reports error for a
model that receives only pre-request features. The sealed protocol therefore
requires 24 calibration cases × 3 measured repetitions and 12 untouched
holdout cases × 3 repetitions, with prediction artifacts frozen before the
holdout labels are revealed. This is the primary new H100 run.

### P1 — representative request/process/model timing

The existing attribution evidence is concentrated on the controlled
`astropy__astropy-12907` case. A second Requests profile has strace and sampled
NVML but not the same overlap-aware device-time attribution. If the final report
requires a multi-repository/category CPU:GPU comparison, one additional exact
profile from a distinct repository/category is useful. It must be isolated and
must not be presented as population-average evidence.

### P2 — population E2E latency provenance (conditional)

The compact evaluator summaries do not contain per-instance trajectory
durations. This can be recovered offline if remote trajectory manifests/logs
still exist; otherwise a new GPU trajectory would be required only if the
assignment’s final report explicitly demands a measured population E2E latency
distribution. Do not launch one merely to repair a missing compact field.

## Gaps resolvable offline

- Baseline/category/E2E figures can be generated from the canonical package,
  with single-case and unavailable categories labeled honestly.
- Report prose, claim boundaries, evaluator provenance fields, source-inventory
  verification, and archive packaging do not require the H100.
- The old simulator can be re-evaluated and the new feature-only predictions
  can be scored after the H100 artifacts are downloaded.
- The missing local raw Kineto/strace replay is a provenance limitation unless
  the external paths are recovered; compact metrics must not be expanded into
  invented traces.
- The Modal dataset-revision mismatch is a reproducibility caveat, not a reason
  to rerun a generic sweep on this final H100 protocol.
- Cross-hardware generalization is explicitly outside this H100-only phase.

## Sealed final-validation queue

1. **Feature-only calibration:** 24 declared input/output target pairs,
   serialized, two warmups, three repeats, fixed tokenizer/prompt and one warm
   vLLM server. Required artifact: immutable row manifests with wall-clock,
   actual usage, CPU/CUDA diagnostics, clock/hardware identity, and hashes.
2. **Offline calibration fit and prediction freeze:** fit only calibration
   medians using `FeatureLatencySimulator`; write and hash predictions for all
   12 holdouts before reading holdout labels.
3. **Sealed holdout:** measure the 12 declared interpolation/extrapolation
   cases with three repeats, then write the reveal receipt and score coverage,
   MAPE, MAE, RMSE, p95 APE, interpolation and extrapolation denominators.
4. **Optional distinct-repository profile:** run only if the gap audit after
   steps 1–3 still shows a report-critical category comparison missing and the
   exact runner can preserve the same timing contract. Otherwise defer it.

## Explicit non-goals

Do not launch A100/H200/cross-GPU work, generic SWE-bench batches, new generic
hyperparameter sweeps, or repeated NCU attempts. Do not claim broad
population, energy, cost, cross-hardware, or individual-event simulator
accuracy from the existing controlled traces or from the sealed token matrix.

## Acceptance and stop rule

The final H100 phase is complete when the sealed calibration/holdout artifacts,
prediction-before-reveal receipt, scoring output, unavailable-row reasons,
hardware/clock manifests, and checksums are durable and reproducible. At that
point remaining work is offline. If calibration or holdout rows are blocked by
hardware, clock, server, or runner contamination, retain explicit unavailable
rows and stop that phase rather than silently changing the split or denominator.
