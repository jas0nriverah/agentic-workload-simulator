# Assignment traceability and measured-evidence map

The attached coding-test PDF is authoritative. This table maps each required
deliverable to the evidence actually present as of the H100 acquisition freeze.
`partial` means the report must preserve the stated limitation; it does not mean
that a missing value may be inferred.

| Assignment requirement | Status | Canonical evidence | Report boundary |
| --- | --- | --- | --- |
| Step 1: Lite resolved rate | measured | `project/h100_results/population_runs.csv`: 32 selected/completed, 8 resolved, 25.0%, 11 repositories | Official evaluator is the resolution source |
| Step 1: Verified resolved rate | measured | Same table: 32 selected, 30 submitted, 29 completed, 10 resolved, 12 repositories | Report 34.4828% completed-case and 31.25% selected-cohort rates |
| Step 1: average population E2E latency | missing_from_compact_exports | `project/h100_results/exclusions.csv` | Compact evaluator summaries lack per-instance E2E durations; do not invent an average |
| Step 1: repository categories vs CPU:GPU ratio | partial | `project/GCP_H100_KINETO_TRAJECTORY_20260824.json` plus `project/h100_results/repository_coverage.csv` | Direct decomposition covers one Astropy trajectory; no population category ratio |
| Step 1: three categorized figures and observations | offline_partial | Canonical population and observability CSVs | Accuracy/category plots are possible; direct CPU:GPU category claims remain limited |
| Step 2: maximum-call sweep | measured_single_instance | `project/h100_results/sweep_results.csv` | Four endpoints, shared baseline, one Lite Astropy instance |
| Step 2: maximum-output sweep | measured_single_instance | Same | Four endpoints, same scope |
| Step 2: observation-budget sweep | measured_single_instance | Same | Four endpoints, same scope |
| Step 2: temperature sweep | measured_single_instance | Same | Four endpoints, same scope; two initial failed attempts retained in exclusions |
| Step 2: trade-off figures and observations | offline_ready_limited | `sweep_results.csv` and `project/MODAL_LITE_SWEEP_MEASURED.json` | Plot wall time and official outcome; no absent energy/GPU-time axes |
| Step 3: event-level CPU/GPU case study | measured_case_study | `project/GCP_H100_KINETO_TRAJECTORY_20260824.json`, `project/GCP_H100_CPU_TOOL_STRACE_PROFILE_20260823.json`, `project/h100_results/observability_summary.csv` | 31-request Astropy case; distinguish Kineto, strace, and sampled NVML clocks/semantics |
| Step 3: single-instance latency explanation | offline_ready_limited | Same | One unresolved Astropy instance; hardware counters unavailable |
| Step 4: hardware-parameterized simulator | implemented_limited_validation | `src/agentic_sim/simulator.py`, `project/GCP_H100_KINETO_SIMULATOR_20260824.json` | Controlled same-H100 phase reconstruction, not unseen-work prediction |
| Step 4: E2E error below 25% | measured_limited_scope | Four calibration rows, two predeclared holdouts, 10.715632% MAPE | Holdouts supply measured CPU/CUDA components; claim residual stability only |
| Step 4: individual-event error below 25% | not_measured | `project/BLOCKERS.md` | No individual-event predictor/error result |
| Official evaluation isolation | measured | `project/h100_results/evaluation_attempts.csv` and source hashes | Preserve empty and incomplete outcomes rather than silently dropping them |
| Reproducible result package | ready | `scripts/analysis/build_h100_results.py`, `project/h100_results/README.md`, source inventory and provenance CSVs | Raw multi-GB Kineto traces remain external by path/hash |

## Frozen runtime

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`, revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`, BF16.
- vLLM: 0.10.0 with `qwen3_coder`.
- SWE-agent: pinned revision `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`.
- SWE-bench: pinned revision `726c5461e2ef52d83cf1ea2107870a8bb3328d57`.
- H100 evidence: one GCP A3 H100 80 GB environment; provider-specific Modal
  sweep evidence remains labeled separately.

## Acquisition decision

**H100 DATA ACQUISITION CLOSED.** A 30th Verified completion is not an
assignment requirement. Cross-GPU validation, if later authorized, follows the
frozen A100 80 GB then H200 plan and is reported separately.
