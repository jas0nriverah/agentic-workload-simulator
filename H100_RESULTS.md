# H100 Results

**Audit date:** 2026-08-24
**Status: H100 DATA ACQUISITION CLOSED**

This is the measured-results ledger for the EIC hardware assignment.  It is
deliberately narrower than a final paper: compact manifests and hashes are
tracked in Git, while very large traces, model weights, credentials, and
machine-local logs remain at the recorded remote paths. A claim below is made
only at the scope supported by its cited artifact. The original EIC assignment
PDF remains authoritative and is intentionally not duplicated in this
repository.

## Executive summary

The frozen SWE-agent/Qwen/vLLM workload was run on a Google Cloud NVIDIA H100
80 GB HBM3.  The canonical completed inventory contains 32 Lite instances and
29 Verified instances.  Lite has 8 resolved cases (25.0%); Verified has 10
resolved cases, which is 34.4828% using the 29 completed-case denominator and
31.25% using the 32-instance selected-cohort denominator.  The official
evaluator, rather than patch existence or an agent exit code, is the source of
resolved status (`project/h100_results/canonical_results.json`).

The acquisition produced the following required or partial evidence:

* all four requested Modal sensitivity axes have measured endpoint coverage;
* a real SWE-agent trajectory has direct Kineto CPU/CUDA activity timing;
* a separate process/NVML capture temporally overlaps all 31 model requests;
* real CPU/file/process/network activity is recorded alongside model requests;
* four predeclared Kineto calibration rows and two holdout rows produce 10.72%
  MAPE for the existing controlled simulator, below the 25% target.

It does not establish population-average E2E latency, multi-repository direct
CPU:GPU ratios, individual-event simulator error, or cross-hardware
generalization. Those gaps remain explicit below rather than being inferred
from sampled utilization or the one-repository Kineto case study.

The last two bullets are different measurement contracts.  Kineto gives exact
serialized request-window CUDA activity for the case study; NVML gives sampled
process overlap and utilization.  Neither is silently promoted to a broad
per-request causal GPU attribution claim.

## Sealed feature-only H100 validation (2026-08-24)

The separately sealed feature-validation protocol completed on the same pinned
H100 runtime using a fresh recovery artifact root. Calibration-only fitting
used 24 cases × 3 repeats (72 completed rows, two warmups per case), then froze
all 12 holdout predictions before any holdout label was opened. The holdout
completed with 12 cases × 3 repeats: 8 interpolation cases and 4 extrapolation
cases, 36/36 completed and 0 unavailable.

| Evidence | Value |
|---|---|
| Protocol SHA-256 | `3fb870a4a619afe9cdc697c6f8edcee51c4814ff01ebd552a904f0c9a9271eac` |
| Split manifest SHA-256 | `c7c6bae7f78d6c7406456176badb57ef935b9afe285f77306624a3d1759e1d26` |
| Prediction manifest SHA-256 | `6bd701fb7b10c5c1c204668e102ff7efc5b1c9c3de35f9bb8288a6760c7afb70` |
| Primary case-median wall MAPE | **1.2476338%** |
| Primary MAE / RMSE | **0.0207811 s / 0.0421246 s** |
| Primary p95 absolute percentage error | **3.2340234%** |
| Coverage | **100.0% (12/12 cases; 36/36 repeats)** |
| Final artifact inventory SHA-256 | `f9f68ba5ecf5eddbc461efbb7ca416914b65c8d9627d2a35340ac922810b612a` |

All 108 calibration/holdout rows use the real production Nsight Systems
provider and validate against their raw `.nsys-rep`, SQLite, request,
response, arm, collect, and `trace_summary.json` checksums. The compact,
tracked result manifest is
`project/h100_results/h100_final_feature_validation.json`; the full artifact
root is machine-local at `artifacts/h100_final_validation_retry3_recovery2`
and its external trace-backed location. The original failed-attempt evidence
in `artifacts/h100_final_validation_retry3` and
`artifacts/h100_final_validation_retry3_recovery1` was preserved unchanged and
is excluded from the completed holdout denominator.

**No additional H100 run is justified solely to obtain a thirtieth completed
Verified case.**  The assignment does not require a 30-case denominator, and
the current evidence is sufficient to close H100 acquisition.  Remaining work
is offline analysis/reporting and, if desired as a separately authorized
follow-up, cross-GPU or broader-population validation.

## Frozen runtime and provenance

The acquisition used one GCP VM (`instance-20260822-182111`,
`us-central1-a`) with one NVIDIA H100 80 GB HBM3.  The runtime pins were:

| Component | Frozen value |
|---|---|
| Model | `Qwen/Qwen3-Coder-30B-A3B-Instruct` |
| Model revision | `b2cff646eb4bb1d68355c01b18ae02e7cf42d120` |
| Precision | BF16 |
| vLLM | 0.10.0; `qwen3_coder` tool parser |
| vLLM image | `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271` |
| SWE-agent revision | `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9` |
| SWE-bench revision | `726c5461e2ef52d83cf1ea2107870a8bb3328d57` |
| Context / memory settings | 32K context; GPU memory utilization 0.90; one GPU / TP=1 |

The pins and host identity are repeated in the compact manifests, including
`project/GCP_H100_MEASUREMENTS.json`,
`project/GCP_H100_KINETO_SIMULATOR_20260824.json`, and
`project/GCP_H100_KINETO_TRAJECTORY_20260824.json`.

## Exact experiments performed

The following is the acquisition inventory, not a proposal for future runs.

| Experiment | Exact scope | Evidence / result |
|---|---|---|
| Primary Lite cohort | 32 selected, submitted, and completed Lite instances; 11 repositories | 8 resolved, 24 unresolved; `project/h100_results/canonical_results.json` and `project/h100_results/population_runs.csv` |
| Primary Verified cohort | 32 selected slots; 30 submitted; 29 completed; 1 empty patch and 2 incomplete | 10 resolved, 19 unresolved among completed; same canonical files |
| GCP Lite/Verified batches and gold-smoke runs | Frozen SWE-agent command, official SWE-bench evaluator, pinned model/runtime | Source manifests and progress ledger: `project/GCP_H100_PROGRESS.md`, `project/GCP_H100_PROGRESS.json`, `project/GCP_H100_GAP_AUDIT_20260823.md` |
| Modal four-axis sweep | One Lite `astropy__astropy-12907` instance; calls, output tokens, observation length, temperature | 16 design endpoints, 13 unique successful trajectories, 2 retained initial infrastructure failures; `project/MODAL_LITE_SWEEP_MEASURED.json` |
| vLLM serving calibration | Serialized direct serving, input 128/512/2048, 16 prompts, output 64, concurrency 1 | TTFT/TPOT and output-throughput anchors; `project/GCP_H100_VLLM_CALIBRATION_20260823E.json` |
| Direct CPU/GPU case study | Four serial direct requests plus a six-request higher-resolution run | HTTP 200 service timing and sampled H100 utilization; `project/GCP_H100_CPU_GPU_CASE_STUDY_20260823.json`, `project/GCP_H100_CPU_GPU_CASE_STUDY_HIRES_20260823.json` |
| Request/profile captures | 31 aligned request boundaries; separate Astropy and Requests CPU/tool profiles | Request metrics, NVML/dmon samples, strace operations; `project/GCP_H100_REQUEST_PROFILE_20260823E.json`, `project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json`, `project/GCP_H100_PROFILE_REQUESTS_2317_20260823.json` |
| Process attribution | 31 request windows against process/NVML samples from the same host and boot | 31/31 request and vLLM-worker overlaps; `project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json` |
| Controlled Kineto simulator matrix | Four calibration conditions and two predeclared holdouts, serialized vLLM HTTP requests | 10.7156% holdout MAPE for the existing simulator; `project/GCP_H100_KINETO_SIMULATOR_20260824.json` |
| Real SWE-agent Kineto trajectory | Frozen Lite `astropy__astropy-12907`, 31 serialized model requests | 56.0637 s exact activity union inside 235.8545 s request wall; `project/GCP_H100_KINETO_TRAJECTORY_20260824.json` |
| NCU capability probe | Bounded in-container Nsight Compute check, no experiment restart | `ERR_NVGPUCTRPERM`; no report or hardware-counter claim; `project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json` |

## Lite and Verified evaluation denominators

The canonical inventory is a set union of unique completed pinned instance
IDs.  This avoids inflating a cohort when an additional attempt repeats an ID;
the full IDs and per-attempt treatment are in
`project/h100_results/canonical_results.json`,
`project/h100_results/evaluation_attempts.csv`, and
`project/h100_results/population_runs.csv`.

| Suite | Selected | Submitted | Completed denominator | Empty patch | Incomplete | Resolved | Unresolved | Resolved rate on completed | Resolved rate on selected |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Lite | 32 | 32 | 32 | 0 | 0 | 8 | 24 | **8/32 = 25.0000%** | **8/32 = 25.0000%** |
| Verified | 32 | 30 | 29 | 1 | 2 | 10 | 19 | **10/29 = 34.4828%** | **10/32 = 31.2500%** |

The two Verified rates are intentionally both reported.  The completed-case
rate excludes the two environment-install failures and the empty generated
patch from its denominator; the selected-cohort rate retains all 32 selected
slots.  The 30 submitted slots and 29 completed instances are not a
contradiction: one selected slot produced an empty patch and two selected
attempts were incomplete.  There is no assignment requirement that forces a
30-completed-Verified threshold.

An older descriptive progress paragraph refers to 34 Lite runs.  That prose
counts overlapping/duplicate batch attempts; the unique-ID recount above is
the canonical result and supersedes the stale number
(`project/GCP_H100_PROGRESS.md`, `project/h100_results/evaluation_attempts.csv`).

## Repository and category coverage

The Lite inventory covers 11 repositories and Verified covers 12.  Counts below
are completed unique instances; resolved rates are descriptive cohort rates,
not claims about repository difficulty or population performance.

### Lite

| Repository | Completed | Resolved | Unresolved |
|---|---:|---:|---:|
| `astropy/astropy` | 2 | 1 | 1 |
| `django/django` | 10 | 2 | 8 |
| `matplotlib/matplotlib` | 4 | 0 | 4 |
| `mwaskom/seaborn` | 3 | 1 | 2 |
| `pallets/flask` | 3 | 0 | 3 |
| `psf/requests` | 3 | 2 | 1 |
| `pydata/xarray` | 3 | 1 | 2 |
| `pytest-dev/pytest` | 1 | 1 | 0 |
| `scikit-learn/scikit-learn` | 1 | 0 | 1 |
| `sphinx-doc/sphinx` | 1 | 0 | 1 |
| `sympy/sympy` | 1 | 0 | 1 |

### Verified

| Repository | Completed | Resolved | Unresolved |
|---|---:|---:|---:|
| `astropy/astropy` | 2 | 0 | 2 |
| `django/django` | 9 | 2 | 7 |
| `matplotlib/matplotlib` | 4 | 1 | 3 |
| `mwaskom/seaborn` | 2 | 0 | 2 |
| `pallets/flask` | 1 | 1 | 0 |
| `psf/requests` | 2 | 1 | 1 |
| `pydata/xarray` | 3 | 2 | 1 |
| `pylint-dev/pylint` | 1 | 0 | 1 |
| `pytest-dev/pytest` | 2 | 1 | 1 |
| `scikit-learn/scikit-learn` | 1 | 1 | 0 |
| `sphinx-doc/sphinx` | 1 | 0 | 1 |
| `sympy/sympy` | 1 | 1 | 0 |

Machine-readable source: `project/h100_results/repository_coverage.csv`.

## vLLM serving calibration

Three isolated serialized conditions used 16 prompts, output length 64, and
maximum concurrency 1.  All 48 requests completed with return code 0.  The
record intentionally sets `gpu_time_claim` false and reports hardware-time
provenance as unavailable; these are serving anchors rather than GPU-second
measurements.

| Input tokens | Benchmark seconds | Output tok/s | Median TTFT | P99 TTFT | Median TPOT | P99 TPOT |
|---:|---:|---:|---:|---:|---:|---:|
| 128 | 6.75 | 151.68 | 24.84 ms | 25.44 ms | 6.33 ms | 6.38 ms |
| 512 | 6.49 | 157.84 | 33.80 ms | 34.64 ms | 5.94 ms | 5.99 ms |
| 2048 | 7.05 | 145.19 | 62.84 ms | 65.22 ms | 6.03 ms | 6.09 ms |

Source: `project/GCP_H100_VLLM_CALIBRATION_20260823E.json` and its compact
derivation `project/h100_results/service_calibration.csv`.

## Controlled Kineto simulator calibration and holdout

The existing simulator (`src/agentic_sim/simulator.py`) was evaluated against a
predeclared serialized vLLM matrix.  The split is declared in
`scripts/observability/run_kineto_matrix.py`, before the measurements were
fit/evaluated:

| Split | Case | Prompt / completion tokens | Wall | CPU-exclusive interval | CUDA activity union |
|---|---|---:|---:|---:|---:|
| Calibration | `cal_128_32` | 372 / 32 | 778.618 ms | 83.799 ms | 179.733 ms |
| Calibration | `cal_512_32` | 1484 / 32 | 861.559 ms | 188.187 ms | 192.867 ms |
| Calibration | `cal_2048_64` | 5932 / 64 | 1724.495 ms | 223.730 ms | 453.098 ms |
| Calibration | `cal_4096_64` | 11866 / 64 | 1777.929 ms | 159.382 ms | 543.619 ms |
| **Holdout** | `hold_1024_48` | 2970 / 48 | 1198.232 ms | 125.999 ms | 253.679 ms |
| **Holdout** | `hold_3072_64` | 8904 / 64 | 1573.904 ms | 154.429 ms | 349.624 ms |

For the existing fixed-phase simulator, the two holdout predictions were
1.161055 s (observed 1.198232 s; 3.1027% absolute percentage error) and
1.285430 s (observed 1.573904 s; 18.3286% error).  Aggregate MAE is
0.162826 s and MAPE is **10.7156%**, below the assignment's 25% target for
this controlled matrix (`project/GCP_H100_KINETO_SIMULATOR_20260824.json`,
`project/h100_results/kineto_matrix.csv`).

### Leakage and claim boundary

The calibration/holdout IDs were predeclared and no holdout row was used as a
calibration row.  However, each holdout's own measured CPU-exclusive and
CUDA-union phase values are used in the reconstruction.  Therefore this result
is a measured-phase residual check for a narrow, serialized, same-H100 matrix;
it is **not** blind prediction from only prompt/completion features.  It does
not establish individual-event error, broad SWE-agent trajectory accuracy,
population CPU/GPU ratios, or cross-hardware generalization.  CUDA time is the
overlap-aware union of Kineto activity intervals, not a sum of NVML samples or
an NCU counter estimate.  The older blocked status in
`project/GCP_H100_SIMULATOR_HOLDOUT_STATUS_20260823.json` is superseded by this
completed controlled Kineto result.

## Real SWE-agent Kineto evidence

One real frozen Lite trajectory, `astropy__astropy-12907`, was captured with
vLLM's built-in PyTorch Kineto profiler.  It had 31 serialized HTTP 200 model
requests, 462,104 prompt tokens, 9,688 completion tokens, and 235,854.532 ms
of request-wall time.  The trace contains 76,371,138 events, 11,498,871 device
activities, and 11,401,802 kernels.  Sorting per-request activity intervals
before exact union repaired 129 out-of-order activities.  Kernel-duration sum
was 56,043.252 ms and the overlap-aware device union was **56,063.686 ms**,
or 23.7705% of summed request wall time.

The official evaluator returned normally but classified this particular
prediction as unresolved (`official_resolved: 0`); the timing case study is
still valid and is not a quality claim.  The UTC join uses Kineto
`baseTimeNanoseconds` plus activity timestamps joined to serialized proxy UTC
request windows.  It is not a direct claim that independent Kineto and host
`CLOCK_MONOTONIC_RAW` values are numerically interchangeable.

Source: `project/GCP_H100_KINETO_TRAJECTORY_20260824.json`; the raw 1.259 GB
trace remains on the VM at the path and SHA-256 recorded in that manifest and
is intentionally not committed to Git.

## Process/NVML attribution

The repaired process-attribution capture used `CLOCK_MONOTONIC_RAW` on the
same host and boot identity as the request windows.  It contains 3,456 valid
process rows and 385 samples for the vLLM worker PID.  All 31/31 request
windows overlap at least one process sample, and all 31/31 overlap the vLLM
worker.  Within request windows, 2,666 process rows and 298 worker samples
overlap.  Sampled peaks were 100% device utilization, 92% worker-SM
utilization, and 47% worker-memory utilization
(`project/GCP_H100_PROCESS_ATTRIBUTION_20260823F2.json`,
`project/h100_results/observability_summary.csv`).

This establishes temporal process/request overlap and sampled utilization.  It
does not establish exact GPU seconds, SM-seconds, occupancy, per-kernel
ownership, or causality.  The aggregate request-profile capture separately
reports 31 requests, 25.060305 s aggregate NVML GPU-active estimate, and
server metric deltas; that estimate is not substituted for Kineto device
activity (`project/GCP_H100_REQUEST_PROFILE_20260823E.json`).

## CPU/tool profiling and CPU-versus-GPU case evidence

The real Astropy profile ran for 114.9607 s with 31 proxy request events and
31 action events.  It recorded 561,374 prompt tokens, 7,535 completion
tokens, 835 H100 samples (414 active), 40.225% mean sampled utilization, and
95% peak sampled utilization.  `strace` recorded 27,843 lines: 25,423 file
operations, 46 process operations, and 561 network operations
(`project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json`).  The Python,
request, and GPU sampler used `CLOCK_MONOTONIC_RAW`; strace `-ttt` timestamps
are realtime epoch and are not merged without an explicit offset.  The
profile's official evaluator was intentionally not run; it is a profile-only
trajectory.

A second Requests profile provides a separate workload profile with 32
requests and 176,174 compact strace lines
(`project/GCP_H100_PROFILE_REQUESTS_2317_20260823.json`).  These are useful
CPU/tool case studies, not a population estimate.

The direct service case studies used `CLOCK_MONOTONIC`, not the raw clock used
by the later aligned capture, and therefore are not merged into exact
per-request GPU attribution.  The four-request run measured mean wall time
400.039 ms; the six-request high-resolution run measured mean 401.307 ms,
min 399.788 ms, max 402.308 ms, mean sampled SM 41.8085%, and peak SM 86%
(`project/GCP_H100_CPU_GPU_CASE_STUDY_20260823.json`,
`project/GCP_H100_CPU_GPU_CASE_STUDY_HIRES_20260823.json`).  These provide
isolated service timing context only.  No broad category-level CPU:GPU ratio
or causal CPU-versus-model attribution is claimed.

## All four Modal sensitivity sweeps

`project/MODAL_LITE_SWEEP_MEASURED.json` records the four assignment axes on
one Lite `astropy__astropy-12907` instance with the frozen model/runtime.  The
shared baseline is 30 calls, 2,048 maximum output tokens, 100,000 observation
characters, and temperature 0.0; it resolved in 973 s.  The endpoint outcomes
are:

| Axis | Four endpoint values and official outcome |
|---|---|
| Maximum model calls (`agent.model.per_instance_call_limit`) | 10: empty patch; 20: empty patch; 30: shared baseline resolved; 50: resolved |
| Maximum output (`completion_kwargs.max_tokens`) | 512: unresolved; 1024: unresolved; 2048: shared baseline resolved; 4096: resolved |
| Observation length (`agent.templates.max_observation_length`) | 10,000: unresolved; 25,000: resolved; 50,000: resolved; 100,000: shared baseline resolved |
| Temperature (`agent.model.temperature`) | 0.0: shared baseline resolved; 0.2: unresolved; 0.5: resolved on retry; 0.8: unresolved on retry |

The four axes yield 16 design endpoint rows (the baseline is represented on
each axis), 13 unique successful trajectories, and two retained initial
infrastructure failures.  The initial temperature-0.5 and temperature-0.8
attempts failed during vLLM readiness/reset or stalled before a prediction;
their successful retries are the attributable endpoints.  These are measured
one-instance sensitivity observations, not estimates of general accuracy.

The intended dataset revision was `69611d...`, but the evaluator observed
`b0dde109...` and the revision contract was not enforced.  The sweep is thus
valid as recorded one-instance measurement evidence, but it is not a strict
reproduction claim for the intended dataset revision.  The sweep did not
produce exact per-cell GPU time.

## NCU and profiling boundary

The bounded in-container NCU probe found NCU 2025.1.1.0 and could connect to
the process, but every attempted matrix was blocked by
`ERR_NVGPUCTRPERM`.  No report was created and no GPU performance-counter or
kernel-metric value is claimed.  Host `nsys` was unavailable and its wrapper
could not cross the vLLM container namespace.  The probe did not restart vLLM,
change CUDA/drivers, or change experiment settings
(`project/GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json`).

This is an environment permission boundary, not evidence that the H100 lacks
the counters.  Repeating the same probe would not add evidence; enabling
host-level counter access or using a separately authorized profiling VM would
be a different experiment.

## Exclusions and inconsistencies

* `psf__requests-1724` and `django__django-10097` are retained as incomplete
  Verified attempts because of reproducible environment-install failures.
  `pylint-dev__pylint-4604` is retained as a submitted empty patch and is not
  in the completed denominator (`project/h100_results/exclusions.csv`).
* `astropy__astropy-14182` appears as an additional Lite attempt but duplicates
  an existing completed ID; it does not increase the unique Lite count.
* Historical NVML captures are retained as sampled-utilization evidence but
  excluded from exact GPU seconds.  The earlier simulator holdout status is
  superseded by the controlled Kineto matrix.
* The Modal evaluator did not enforce the intended dataset revision; no exact
  pinned-dataset reproduction claim is made for those sweep cells.
* Compact manifests contain hashes, outcomes, and remote paths.  They do not
  include model weights, credentials, the 1.259 GB Kineto trace, full raw
  strace, or generated caches.  Those omissions are deliberate and do not
  turn an uncommitted raw artifact into a measured absence.
* Older progress prose and current canonical CSV/JSON counts differ where
  attempts overlap.  Unique IDs plus explicit incomplete/empty treatments are
  the controlling denominator.

The canonical exclusion and provenance tables are
`project/h100_results/exclusions.csv` and
`project/h100_results/claim_provenance.csv`.

## Safe claims for the final report

The following claims are supported at the stated scope:

1. The frozen Qwen/SWE-agent/vLLM stack ran on an H100 80 GB with the pins in
   this document.
2. The canonical cohorts contain 32 completed Lite cases and 29 completed
   Verified cases, with the rates and denominators in the table above.
3. Lite spans 11 repositories and Verified spans 12 repositories, with the
   descriptive counts in `repository_coverage.csv`.
4. Four assignment sweep axes have endpoint coverage on the measured Modal
   one-instance workload.
5. A real SWE-agent Astropy trajectory has direct Kineto CPU/CUDA activity
   timing, and its exact activity union is 56.0637 s within 235.8545 s of
   serialized request wall time.
6. Process/NVML samples overlap all 31 request windows and all 31 vLLM-worker
   windows; the reported peaks are sampled values.
7. The existing simulator meets the 25% error target on the predeclared,
   controlled, measured-phase Kineto matrix with 10.7156% MAPE.
8. NCU hardware-counter metrics were unavailable because of a measured
   `ERR_NVGPUCTRPERM` permission boundary.

## Claims that are not safe

Do not claim that these measurements establish a universal SWE-agent resolved
rate, an average category-level CPU:GPU ratio, exact GPU seconds from NVML,
per-request causal GPU ownership for the old captures, NCU kernel metrics,
blind simulator prediction on unseen agent trajectories, cross-hardware
generalization, GPU energy/dollar efficiency, or a requirement-satisfying
30-completed-Verified denominator.  Do not describe the controlled simulator
holdout as an independent feature-only generalization test; its measured phase
values participate in reconstruction as described above.

## H100 acquisition decision

**H100 DATA ACQUISITION CLOSED.**  The high-value H100-dependent evidence has
been collected: evaluation cohorts and repository coverage, all four sweep
axes, serving calibration, aligned process/request overlap, CPU/tool profiles,
the real SWE-agent Kineto case study, and a controlled simulator calibration /
holdout.  NCU is conclusively blocked by the environment permission boundary.

No extra H100 is justified merely to turn 29 completed Verified cases into 30;
that threshold is not in the assignment and would not close a distinct
scientific requirement.  Further H100 time would be justified only by a new
explicit question, such as a cross-GPU comparison or a deliberately expanded
multi-repository Kineto population.  Those are follow-up studies, not missing
requirements for the present acquisition.

The remaining work is offline: render the assignment figures, write the final
report with the safe claim boundaries above, optionally export large raw traces
if a reviewer requires them, and document any future cross-GPU plan.  None of
those tasks requires keeping this H100 running.

## Source map

The machine-readable consolidation is in `project/h100_results/`, especially:

* `canonical_results.json` — canonical denominators, runtime, simulator,
  process, Kineto, NCU, and sweep summaries;
* `population_runs.csv` and `evaluation_attempts.csv` — per-instance outcomes;
* `repository_coverage.csv` — repository/category counts;
* `kineto_matrix.csv` — calibration/holdout rows;
* `service_calibration.csv` — vLLM TTFT/TPOT anchors;
* `observability_summary.csv` — cross-experiment measurement boundaries;
* `sweep_results.csv` — four Modal axes;
* `claim_provenance.csv` and `exclusions.csv` — claim and exclusion audit;
* `source_inventory.csv` — source artifact paths and hashes.
