# D9 bounded offline iteration results — September 14

## What changed

Two model hypotheses and one integration pass were completed using retained
training evidence. No protected final evaluation, new inference, A100 work,
or acquisition changes. The original instance-grouped folds are preserved.
Selection uses development evidence and is not untouched validation.

| Target | Before this iteration: within 25% | Selected: within 25% | Worst error before → after |
|---|---:|---:|---:|
| Repaired semantic actions: 1,780 events / 43 cases / 22 instances | 57.75% | 58.09% | 2,779.45% → 2,309.37% |
| Native prefill: 2,080 requests / 49 cases / 25 instances | 83.70% | 90.58% | 99.76% → 99.73% |
| Native decode | 98.94% | unchanged | 68.42% |
| Native queue | 42.02% | unchanged | 98.73% |
| Native request E2E, supplied cache trace | 97.45% | unchanged | 96.77% |
| Outer agent E2E | 42/43 | unchanged | 28.96% |

CPU: the existing gate-center estimator was tested on the repaired command
cohort, after full commands were restored in the previous iteration. It adds
six passing events and lowers worst error by about 17%. Equal-instance
coverage rises 60.51% → 60.94%. The fixed-prediction bootstrap interval for its
coverage gain is -1.58 to +3.00 percentage points: the coverage gain is not
established statistically. Selection is a development tradeoff supported by
the observed tail reduction, not proof of a universal accuracy improvement.
Relative to the original coarse repaired adapter, coverage is 53.54% → 58.09%.

GPU: prefill previously used intercept + uncached input + total prompt length.
The additional nonnegative `uncached_input * total_prompt` term represents a
simple hypothesis about context-dependent attention work. It adds 143 passing
prefill events; equal-instance coverage rises 84.40% → 91.41%. The paired
instance-bootstrap interval for coverage gain is +4.61 to +9.98 percentage
points. This is conditional trace simulation, not a prospective cache forecast
or a hardware scaling law. Bootstrap intervals omit adaptive-selection uncertainty.

Splitting fits into cache-hit/cache-miss regimes did not produce a defensible
overall improvement and was rejected. Decode and native request E2E retain the
existing coefficients/feature formulation.

The prefill change is not uniformly better: all-prefill-events-passing cases
fall from 1 to 0. Across requests, simultaneous queue/prefill/decode/request-E2E
success improves **733/2,080 → 806/2,080**. Neither measure satisfies D9's
all-event requirement. CPU all-events-passing instances remain zero.

## Integrated prediction interfaces

The existing `simulator/run.py predict --request request.json` now supports:

- `repaired_semantic_action`: inputs are exactly `action`, `repository`,
  `operation_class`, `hardware_domain`. Uses `semantic_repaired/refined_fit_artifact.json`.
  Preserve the recorded pre-action class rather than reclassifying old actions.
- `conditional_repaired_e2e`: the unchanged request schema in
  `e2e_composition/example_request.json`, now served through the common API.
  The chosen direct model remains distinct from fitted component-accounting output.
- `conditional_native_phase`: inputs are exactly `phase` (queue/prefill/decode),
  integer `prompt_tokens`, `completion_tokens`, `cached_tokens`, `cache_trace:true`,
  and `hardware_domain`. Uses `native_refinement/fit_artifact.json`.

Hardware domains must match the saved artifacts; changing the common hardware
profile is rejected for these candidates. Generic historical profile metadata
remains explicitly an assumption, with the verified evidence domain separately
reported. Models do not silently rescale. Measured latency/outcome fields,
invalid token counts and missing cache opt-in are rejected. Native E2E and phase
overlap is also rejected by the strict scorer for the integrated target aliases.

## Reproduction and remaining limits

From the repository root:

```sh
.venv/bin/python docs/d9-salvage-20260910/semantic_repaired/refine_center.py
.venv/bin/python docs/d9-salvage-20260910/native_refinement/compare_phases.py
```

Reports, fitted artifacts and complete grouped predictions are retained in the
corresponding directories. Native source dataset/manifest hashes are verified
by the original loader; CPU source/split gates use the original repaired adapter.
The common API's numerical parity and input rejection are covered by
`tests/assignment/test_d9_repaired_integration.py`.

**D9 remains unproven.** Dominant remaining issues are individual CPU-operation
accuracy, queue variation, rare prefill tails, the outer-E2E miss, and absent
validated hardware sensitivities. Atomic CPU and lifecycle candidates are still
separate from the common API; this pass does not claim all integration is done.
Do not repeat generic center or cache-regime sweeps. More modeling work should
target a measured mechanism, not merely additional fitting flexibility.
