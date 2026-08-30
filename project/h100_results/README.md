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

The sealed feature-only validation result is recorded separately in
`h100_final_feature_validation.json`. It reports the 24-case calibration fit,
the frozen 12-case holdout prediction, real Nsight trace provenance, the
holdout metrics, and the SHA-256 inventory for the external raw-artifact root.
It does not copy raw traces or model weights into Git.

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

## Recovered live matrix checkpoint

The corrected PACE run `matrix-runs-h100-full-20260829-corrected-v4` is
preserved as a separate live checkpoint. The source snapshot reports the
matrix as still running, with one active case; these measurements are not
silently presented as a closed 1,088-case run.

- Scope: one NVIDIA H100 80 GB HBM3, concurrency 1, Lite suite only.
- Published measurements: 304 completed case-result records, case indices
  0--343 with gaps for failures; 132 officially resolved and 172 unresolved.
- Model traffic in completed records: 9,159 requests, all HTTP 200;
  122,847,583 prompt tokens, 1,866,381 completion tokens, and
  13,077,447.878 ms summed request duration.
- Published failures: 40 `invalid_result` records, each retaining its case
  identity and request summary; 948 requests total (919 HTTP 200 and 29 HTTP
  400), 13,387,348 prompt tokens, and 201,129 completion tokens.
- Case catalog: 345 case specifications, of which 304 have a result, 40 are
  failed published cases, and case 344 (`sympy__sympy-11870`) is the active
  case in the source snapshot.

The live files are:

| File | Contents |
| --- | --- |
| `live_matrix_case_specs.jsonl` | Identity and planned settings for all 345 case directories, including failed and active cases |
| `live_matrix_cases.jsonl` | Compact measurement records for the 304 valid results |
| `live_matrix_failures.jsonl` | Compact records for all 40 invalid results |
| `live_matrix_progress.json` | Full compact progress snapshot, source bindings, case records, and failures |
| `live_matrix_raw_inventory.json` | SHA-256 and byte-size entry for every regular file in the raw run |

The compact result projection predates the case catalog and leaves its
`suite`/`instance_id` fields null. Join on `case_index` and `case_sha256` with
`live_matrix_case_specs.jsonl`; the catalog is the identity source of truth,
not missing measurement data.

The complete raw artifact tree remains on durable PACE storage at:

```text
/storage/ice1/9/6/jriverah3/eic-work/full-assignment/assignment/matrix-runs-h100-full-20260829-corrected-v4
```

At checkpoint time it contained 17,021 regular files totaling 1,508,215,700
bytes. The inventory is the auditable pointer to every trajectory, proxy log,
evaluator artifact, prediction, result, state file, and checksum sidecar; raw
files are not copied into Git. The run is bound to plan SHA-256
`2bead159a24e244ecbf981c63f3d43d24f8bf1fe9a389c398f17325d941069fc`, runtime
manifest SHA-256
`7658f8f483d03734af2b97f1c0e69ee11cc3986a4c9ea6d87a3199d190368c44`, and
execution commit
`c8d03ba990cfe2263d5eb3e4b64124c969875301`.

The Git-side SHA-256 bindings for this checkpoint are:

```text
1cea4afd8f4b072848b964d207f57741058f558db8934956b547cf4ae4008903  live_matrix_case_specs.jsonl
afa68fc69ee099d841f4323eb162b212dff43f43d974e6144ce6dd5846db535a  live_matrix_raw_inventory.json
52ef4976bfc2df9498bd61fac70d443ff53f8d0dd8e40cc919074b89258709fe  live_matrix_cases.jsonl
9be2b40e08e65c53933e7e6c2b9e7a91585259b869d2a1dba02486ef2223fc45  live_matrix_failures.jsonl
3429bd66a66f10c4e5828867382e4dc4777cab6ad3998b371f03e36daccaa173  live_matrix_progress.json
```

To regenerate the two provenance files from the PACE mount:

```bash
python3 scripts/analysis/export_h100_case_specs.py \
  --raw-root /storage/ice1/9/6/jriverah3/eic-work/full-assignment/assignment/matrix-runs-h100-full-20260829-corrected-v4 \
  --output project/h100_results/live_matrix_case_specs.jsonl
python3 scripts/analysis/inventory_h100_run.py \
  --raw-root /storage/ice1/9/6/jriverah3/eic-work/full-assignment/assignment/matrix-runs-h100-full-20260829-corrected-v4 \
  --output project/h100_results/live_matrix_raw_inventory.json
```

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
