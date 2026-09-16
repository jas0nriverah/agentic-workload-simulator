# D9 retained modeling opportunities — 2026-09-09

This is a bounded analysis-only handoff. Measurement completeness remains **PASS CLOSED**. No evaluator, inference, GPU, network, runtime, telemetry, frozen source, or model artifact was changed. The current `combined-case-v8` evidence is descriptive and outside model selection.

## Boundary and provenance

The frozen scope was built from the five hash-bound manifests before this analysis opened the D9 aggregate artifacts. It excludes 137 final-evaluation/sealed instance clusters and 451 explicit run/case IDs. The scope helper records its purpose as `historical development mining only; not training authorization` and discloses that an earlier forensic pass accessed evaluation-cluster rows; filtering cannot restore blindness.

The two analysis scripts then bulk-read the complete saved prediction/action JSON objects and filtered rows by the frozen scope. This means the run has no pristine-blindness claim. The exposure was to aggregate historical prediction rows and cached action strings, including mixed historical duration labels for identities later excluded by scope. No separate sealed artifact or raw trajectory was opened, and no evaluator outcome or current production trace was used. The descriptor experiment fit only the manifest's `train_calibration` instance set in grouped folds and never used excluded rows in a fit or score.

The exact scope counts, source hashes, and skip counts are in `atomic-error-analysis.json` and `train-calibration-descriptor-experiment.json`. The JSON/CSV artifacts are compact and reproducible with the scripts beside this report.

## Highest-value ranking

The saved historical semantic median was compared with the saved original hierarchical baseline after filtering to 24,426 eligible CPU tool events across 860 trajectories. This reuses the previous D9 error analysis; it is a historical diagnostic, not independent confirmation.

| Priority | Opportunity | Evidence | Modeling action | Limit |
|---|---|---|---|---|
| 1 | Declared script and traversal work state | `python_script` actions contribute 1.818M ms, 33.14% of selected CPU absolute-error mass; `python_inline` contributes 0.498M ms, 9.08%. `find -exec` contributes 0.717M ms (13.06%) with p95 APE 993.6% and max 4,433%. | Model/adapt the already captured script prestate and work descriptors, with an explicit state invalidation and validation contract after arbitrary shell writes. Model traversal subprocess mode and a declared/observed work-volume input when the serving contract can provide it; this is a modeling change with no acquisition change. | Future script body, imported work, visited/matched file count, and child count are unavailable to the current stateless interface. Replayed realized work is conditional trace replay, not prospective authorization. |
| 2 | Test runner/module/scope semantics | `python`/`pytest` mechanisms contribute 1.285M ms, 23.41% of selected error mass; 54.02% within25, p95 APE 99.0%, max 1,282%. The historical classifier can confuse edits/searches with tests and loses the module in `python -m`. | Use executable, actual runner/module, selected scope, and environment declaration as prospective descriptors with support-aware backoff. | Local “pytest unavailable” observations and success/failure labels cannot select a prediction mode. Environment availability must be declared before execution. |
| 3 | Separate lifecycle and native phases | Historical direct CPU+GPU E2E within25 is 0.09%; overhead-aware E2E is 59.37%. The current fixture shows native decode dominates its native phase sum while host E2E contains lifecycle gaps. | Keep event models separate; compose predicted event sums with an explicitly trained nonnegative runner/lifecycle overhead. Split GPU request/queue/prefill/decode/transport classes where each phase is logged. | Current fixture is one descriptive case and cannot identify a causal lifecycle model or transfer its score. |
| 4 | Pager and pipeline modes | Piped/unpiped and pager-susceptible modes are supported syntax descriptors. They are lower mass than Python/find mechanisms; 89 eligible unpiped git events all exceed 20 s but pass the old timing gate. | Retain syntax-selected pager-aware fallback and coarse pipeline/redirect mode. | Pager susceptibility is an inference from syntax, not proof of a future timeout; do not use a cap to hide tails. |
| 5 | Hardware sensitivity only | One reference CPU profile and one H100 profile cannot separate serial CPU work, launch, storage, and waiting. `cpu_threads * cpu_base_ghz` is not identified as a speed law. | Keep frequency sensitivity as an explicit assumption and report hardware transfer as unvalidated. | Storage bandwidth, parallel speedup, serial fraction, and cross-GPU phase effects require interventions absent here. |

The broad original classes explain the ranking: shell has 3,133/5,520 misses and 49.17% of selected absolute-error mass; test has 549 misses and 24.73%; traversal has 686 misses and 19.43%. Together they account for 4,368/5,520 misses (79.13%) and 93.33% of absolute-error mass. Read, write, search, patch, and editor actions retain high event coverage and low error mass, so broad model expansion there has lower value.

## Atomic mechanism detail

The top saved-action groups are below. Percentages are within the 24,426-event eligible historical slice; `error share` is share of selected absolute-error mass and `miss share` is share of selected misses.

| Mechanism | Events | Within25 | Mean APE | p95 APE | Max APE | Error mass | Error share | Miss share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| shell / python script | 3,595 | 38.53% | 54.32% | 205.09% | 789.59% | 1.818M ms | 33.14% | 40.04% |
| test / python pytest | 896 | 54.02% | 43.90% | 99.04% | 1,281.73% | 1.285M ms | 23.41% | 7.46% |
| traversal / find `-exec`, unpiped | 467 | 49.46% | 176.10% | 993.62% | 4,432.96% | 0.717M ms | 13.06% | 4.28% |
| shell / inline Python | 937 | 38.85% | 55.66% | 240.52% | 550.70% | 0.498M ms | 9.08% | 10.38% |
| traversal / find `-exec`, two-stage pipe | 119 | 56.30% | 114.80% | 356.92% | 4,052.88% | 0.208M ms | 3.78% | 0.94% |

These are action-derived atomic mechanism classes. They do not mean that a syscall or returned byte is the target of the historical model. The historical rows target tool execution duration; the current reconstruction has a different target population of 846,533 atomic operations across 100 actions.

## Bounded train-calibration experiment

One small experiment was run with a fixed protocol on the manifest's 546 `train_calibration` clusters. One cluster has no observed row in the saved prediction cache, leaving 545 observed instance IDs and 23,245 CPU events. Five folds were assigned by `sha256('assignment.d9.train-fold-v1:' + instance_id)` modulo five. The plain candidate is an original-class median. The stratified candidate uses a fixed backoff sequence: action mechanism → operation → semantic class → original class, with at least 25 training events and three training instances for a group. No duration, CPU-time, end-state, failure, or evaluator field is a feature.

| Candidate | Within25 | Misses | Mean APE | Median APE | p95 APE | Max APE | Absolute error |
|---|---:|---:|---:|---:|---:|---:|---:|
| Plain original-class median | 67.74% | 7,498 | 37.35% | 14.97% | 153.07% | 481.08% | 9.779M ms |
| Fixed descriptor backoff | 72.39% | 6,418 | 38.20% | 13.38% | 119.66% | 4,181.50% | 8.205M ms |

The fixed descriptor backoff gains 4.65 percentage points and removes 1,080 event misses, while reducing absolute error by 1.574M ms and p95 APE by 33.41 points. Mean APE worsens by 0.85 points and the maximum tail worsens sharply, so this is evidence for supported semantic stratification with tail uncertainty, not a production recommendation. The gain is consistent in all five folds (+3.44 to +6.46 points), but no bootstrap interval was run for this small experiment. Class slices show the gain is concentrated in shell (+6.19 points), test (+33.26 points), and traversal (+7.81 points); compact editor/read/write/search classes change little.

This experiment demonstrates that syntax-level mechanism descriptors can move the fixed 25% gate within train-calibration data. It does not establish that realized workload volume or measured syscall return work will generalize, and it does not certify any holdout.

## Current reconstruction as conditional evidence

`docs/measurement-review-20260909/reconstruction/reconstruction-result-v2.json` is explicitly scoped to the closed `combined-case-v8` integration fixture and is outside selection. Its full BPF audit covers 846,533 raw operations across 100 actions with zero recorded loss. It joins 40 physical model requests exactly, with zero native hash mismatches and zero unmatched finished records. The realized work descriptors support conditional trace replay; only fields available before execution can be prospective. This does not authorize an acquisition change or create a new D9 score.

The host-clock reconstruction is 153,117.581093 ms outer E2E, comprising 145,814.380856 ms measured interval union and 7,303.200237 ms explicit unknown residual (4.77% of outer E2E). Boundary-position accounting places 4,260.54 ms before deployment start and 1,102.70 ms after teardown; their 73.44% share of the unknown residual is descriptive location evidence, not a causal label.

The 40 semantic tool rows sum to 18,491.611631 ms. The broader 32,634.391398 ms tool-execution union includes 60 auxiliary runtime commands totaling 14,142.779767 ms, so it must not be reported as semantic-tool CPU time. Native phase sums are prefill 3,407.418 ms, decode 74,575.329 ms, queue 1.922 ms, and native request E2E 78,120.326 ms. Cached request evidence spans input/context up to 35,264 tokens (prompt growth 1,410 → 35,298) and 10,803 output tokens in this fixture. These quantities motivate phase- and lifecycle-aware conditional replay; they do not resolve historical 55.98% unassigned E2E mass, which is a different cohort and timing contract.

## Identifiability and gate boundary

The GPU design has rank three in the cached calibration data because input and context token counts are identical. Separate input and context coefficients are therefore not identifiable. Output tokens are logged and accepted as an assignment-level conditional descriptor. The native phase evidence does not identify CUDA occupancy, GPU-count scaling, transfer costs, or prefill/decode hardware laws.

Supported interactions are syntax-derived combinations with enough distinct instance support: executable/runner/module, coarse operand bucket, recursion, traversal `-exec` mode, pipeline/redirect mode, inline imports, and pager exposure. Repository identity is a conditional descriptor within this logged environment, not a portable work law. Actual files visited, bytes transferred, subprocess counts, installed runner availability, mutable script contents, and hardware storage effects need an explicit train/serve contract before they can be fitted prospectively.

The ≤25% requirement remains unchanged. Historical event-level improvements do not repair the boundary mismatch: the old published full-cohort D9 report gives only 1/1,083 trajectories passing every recorded CPU event, GPU event, and overhead-aware E2E gate (0.092%), with 990 coverage-eligible trajectories and one pass. That 1,083-trajectory result is distinct from the new scope-filtered historical slice of 860 trajectories. Direct CPU+GPU plus E2E has zero passes. The experiment above reports CPU events only and must not be combined with the E2E or coverage gate.

The bounded conclusion is to prioritize a declared state/work contract and phase/lifecycle composition, then prespecify a grouped evaluation that keeps all events and the strict gate in the denominator. More center tuning, exact-command memorization, duration clipping, hardware scaling claims, confirmation mining, or sealed-label access has no support from this analysis.

## Reproduction files

- `analyze_atomic_errors.py` — saved historical atomic mechanism ranking.
- `atomic-error-analysis.json` / `atomic-error-analysis.csv` — scoped group metrics and paired baseline deltas.
- `experiment_train_calibration.py` — fixed five-fold train-calibration descriptor comparison.
- `train-calibration-descriptor-experiment.json` / `train-calibration-descriptor-experiment.csv` — fold-level and class-level metrics.
- `docs/measurement-review-20260909/reconstruction/reconstruct_bounded.py` and `reconstruction-result-v2.json` — referenced conditional fixture reconstruction helper and output; no new production reconstruction was run here.
