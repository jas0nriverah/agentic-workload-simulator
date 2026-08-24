# Canonical H100 results package

This directory is the derived, provenance-first package for the H100 data
acquisition phase. It is generated from the tracked evaluator summaries and
compact measurement manifests; it does not replace or delete any raw run,
trajectory, profiler export, or VM artifact.

## Scope and frozen runtime

The package was audited on 2026-08-24 for the pinned NVIDIA H100 80 GB HBM3
runtime:

- `Qwen/Qwen3-Coder-30B-A3B-Instruct` (revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`), BF16;
- SWE-agent revision `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`;
- vLLM `v0.10.0` image digest
  `sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`.

The canonical status is **H100 DATA ACQUISITION CLOSED**. The package records
what was measured, what was derived, and what remains an offline-analysis or
cross-GPU limitation. It does not imply that every assignment report section
is complete.

## Rebuild and verify

From the repository root, regenerate the package with:

```bash
python3 scripts/analysis/build_h100_results.py
```

The builder is deterministic for a fixed set of source files. The focused
contract test rebuilds into two temporary directories and compares every
generated file byte-for-byte:

```bash
python3 -m unittest tests.analysis.test_build_h100_results -v
```

The test also verifies the canonical cohort counts, simulator MAPE, and the
expected plot-ready CSV row counts. The generated `source_inventory.csv`
contains SHA-256 hashes and sizes for the source evidence included in the
package.

## Package schema

`canonical_results.json` is the machine-readable index. Its stable top-level
sections are:

- `runtime`, `hardware`, and `status` — frozen execution context;
- `primary_evaluation_cohorts` and
  `canonical_unique_completed_inventory` — Lite/Verified population counts;
- `simulator` — calibration/holdout records and independently recomputed
  predictions;
- `real_sweagent_kineto` — direct CUDA-activity evidence for one trajectory;
- `process_attribution` — sampled process/NVML overlap evidence;
- `sweeps` and `ncu` — sensitivity endpoints and the profiling-counter result;
- `files` — the CSV members of this package.

The CSV members are deliberately flat so they can be loaded directly by the
offline analysis/plotting code:

| File | Purpose | Current data rows |
| --- | --- | ---: |
| `population_runs.csv` | Primary Lite/Verified population rows | 64 |
| `evaluation_attempts.csv` | Primary plus auxiliary attempts | 72 |
| `repository_coverage.csv` | Completed coverage by suite/repository | 23 |
| `sweep_results.csv` | Four measured sweep axes, four endpoints each | 16 |
| `kineto_matrix.csv` | Four calibration and two holdout matrix rows | 6 |
| `service_calibration.csv` | Three vLLM service calibration conditions | 3 |
| `observability_summary.csv` | Kineto, process, strace, and NCU evidence | 6 |
| `source_inventory.csv` | Hashed source-file inventory | 504 |
| `claim_provenance.csv` | Claim-to-derived-evidence map | 10 |
| `exclusions.csv` | Explicit exclusions and their reasons | 10 |

The row counts above exclude CSV headers and describe the current generated
package. The contract test checks the count-bearing tables that define the
canonical outputs.

## Current canonical measurements

- Lite: 32 selected/completed, 8 resolved, 24 unresolved (25.0% resolved).
- Verified: 32 selected, 30 submitted, 29 officially completed, 10 resolved,
  19 unresolved, 1 empty patch, and 2 incomplete attempts. The completed-case
  rate is 34.4827586%; the selected-cohort rate is 31.25%. The 29-versus-30
  completed Verified distinction is retained explicitly; 30 is not an
  assignment threshold and is not silently treated as complete.
- Simulator matrix: 4 calibration cases and 2 predeclared holdout cases;
  independently recomputed E2E MAPE is 10.715632310651648% (MAE
  0.1628258885 s).
- Direct Kineto: one real SWE-agent Lite Astropy trajectory with 31 serialized
  requests and exact per-request device-activity interval union.
- Process attribution: 31/31 request windows overlap process samples and the
  vLLM worker; the aggregate evidence is sampled NVML/process attribution, not
  exact GPU occupancy or causal attribution.
- NCU: blocked by `ERR_NVGPUCTRPERM`; no hardware-counter values or NCU report
  are claimed.

## Lineage and interpretation

The builder reads the pinned evaluator summaries listed in its
`BASELINE_SOURCES`/`ADDITIONAL_SOURCES` tables, the auxiliary sweep/profile
summaries, and the compact manifests under `project/`. Each derived row keeps
its source path where applicable; source hashes are retained in the CSVs and
`source_inventory.csv`.

The direct evidence types must not be conflated:

- Kineto activity is direct traced CPU/CUDA activity for one trajectory;
- process/NVML rows are sampled temporal overlap and utilization evidence;
- NCU would provide hardware performance counters, but the capability probe is
  permission-blocked;
- the simulator result is a controlled same-H100 reconstruction with a sealed
  two-case holdout, not a claim of broad unseen-workload or event-level
  predictive accuracy;
- the four sweep axes are measured one-instance sensitivity endpoints, and the
  dataset-revision contract was not enforced for that Modal campaign.

Use `claim_provenance.csv` and `exclusions.csv` before making any report claim.
`source_inventory.csv` is an inventory, not a copy of the raw artifacts.

## Raw-artifact policy

Raw and compact source artifacts remain in their original repository/VM export
locations. This package only adds reproducible derived tables and hashes. Do
not commit model weights, credentials, virtual environments, caches, huge raw
logs, or profiler dumps; if an untracked raw export is needed later, preserve
it outside Git and add only its provenance/hash.
