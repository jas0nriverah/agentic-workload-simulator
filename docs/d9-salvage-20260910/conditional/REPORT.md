# D9 conditional GPU request-duration comparison

This is a bounded historical, trace-conditioned comparison on the retained `train_calibration` view. It does not change acquisition or production and does not claim a pre-generation predictor.

## Population and contract

The run uses **545 instances**, **819 runs**, and **23868 completed request-proxy events**. The pre-existing `outer_fold` assignment is preserved; all rows of each instance and run remain in one outer fold. The view was validated against the manifest and no excluded instance or run was used.

`output_tokens` is a realized workload descriptor in the completed trace. The comparison therefore answers a conditional simulation question: given the declared/recorded token workload, does a simple duration model improve on the feature-free baseline? It is not a pre-generation forecast. `observed_ms` is the target only. Latency, residuals, phase timing, future actions, evaluator outcomes, and cache state are forbidden features. The target is the historical request-proxy wall duration, not native GPU prefill/decode/queue time.

Input and context counts are exactly equal in this view, so the model uses their sum and does not claim separately identifiable input/context coefficients. One prespecified prompt-output token interaction is included in the interaction candidates.

## Relation to the prior conditional simulator

The repository's prior assignment workload simulator (`src/agentic_sim/assignment/workload_simulator.py::gpu_design`) already uses input, output, and context token descriptors with hardware capacities. This bounded run did not refit or rescore that prior model. Its measured gains therefore establish improvement over the feature-free baseline only; they do not establish improvement over the prior conditional v3 model. The earlier stricter sealed prospective contract also forbade current `output_tokens`, so it answers a different question. Cross-hardware accuracy and native GPU phase accuracy remain unvalidated.

## Fixed outer-fold results

| Candidate | Event within 25% | All-events-per-run | Mean APE | P95 APE | Worst APE | Δ event coverage vs baseline | Δ run gate vs baseline |
|---|---:|---:|---:|---:|---:|---:|---:|
| `global_median_baseline` | 34.63% | 0.00% (0/819) | 46.70% | 93.52% | 234.83% | +0.00 pp | +0.00 pp |
| `nonnegative_log_additive` | 96.69% | 66.79% (547/819) | 8.71% | 21.11% | 97.30% | +62.06 pp | +66.79 pp |
| `nonnegative_log_token_interaction` | 96.69% | 66.79% (547/819) | 8.71% | 21.11% | 97.30% | +62.06 pp | +66.79 pp |
| `robust_nonnegative_log_token_interaction` | 96.69% | 66.79% (547/819) | 8.70% | 21.15% | 97.30% | +62.05 pp | +66.79 pp |

The highest event-level coverage among feature models is `nonnegative_log_additive`. Relative to the baseline it changes event coverage by **+62.06 percentage points**, the all-events-per-run gate by **+66.79 points**, p95 APE by **-72.41 points**, and worst APE by **-137.54 points**.

Using a predeclared review flag of at least 1 percentage point improvement on both event and all-events-per-run coverage with no tail worsening, meaningful conditional improvement is **supported** by this comparison. The raw metrics remain the evidence; this flag does not create a D9 acceptance criterion.

## Nested grouped selection

The inner procedure selected `nonnegative_log_additive` on the full retained partition. Nested outer-fold selected-procedure metrics are: `{"all_events_within_25_percent_runs": 66.7887667887668, "all_events_within_25_runs": 547, "max_ape_percent": 97.29803089853075, "mean_ape_percent": 8.706600825596675, "n_events": 23868, "n_runs": 819, "p95_ape_percent_nearest_rank": 21.112569335809358, "within_25_percent_events": 96.69012904307021}`. Outer-fold choices are recorded in `comparison.json`; no outer-test target was used to fit a fold model.

## All-events-per-run gate

A run passes this conditional request gate only when every retained request event in that run is within 25 percent. This is not the complete assignment D9 gate: it does not include CPU events, native GPU phases, lifecycle overhead, or a start-known E2E forecast. It must not be combined with those populations as if they were jointly measured.

## Reproduction artifacts

- `run_conditional_gpu_models.py` is the runnable bounded script; it reads only the named training view and manifest.
- `predictions.jsonl` contains fixed outer-OOF predictions for all four candidates (4 predictions per retained event); SHA-256: `c7444caaf662f6a76b8d8ebafea0e89a70fe1102cba3bda60d435770b5d3f53f`.
- `fit_artifact.json` contains full-train coefficients and the selected candidate for offline review.
- `comparison.json` contains metrics, fold choices, and model definitions.
- `provenance.json` binds source hashes, script hash, population, feature contract, and exclusions.

No acquisition, inference, existing module, hardware state, or production configuration was changed.
