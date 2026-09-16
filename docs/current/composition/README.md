# Repaired component composition: first implementation

This is an offline development integration, not D9 acceptance or a replacement
for the earlier direct E2E regression. Run from the repository root:

```sh
.venv/bin/python scripts/assignment/reconstruct_repaired_composition.py
```

## Checked result

- 43 retained training cases; 15,766 original CPU/lifecycle/client targets,
  plus 43 explicit startup/setup accounting envelopes.
- Common instance-grouped folds for every fitted component. All final holdouts
  remain excluded. No data collection or GPU inference.
- Inclusive component sum within25 on **31/43** outer E2E targets; worst
  error **41.93%**. Zero cases pass all original events plus E2E.
- One unsupported interrupt event remains a missing prediction. It is not
  removed from the event denominator.
- No unresolved root overlaps after explicitly representing startup/setup.
- Median measured unaccounted fraction **6.08%**. Unaccounted time is reported
  only as a diagnostic; no measured gap is used to improve a prediction.

## Why startup/setup needed explicit treatment

Startup and setup overlap partially in all 43 cases. They are neither disjoint
nor strictly nested. Add a joint accounting envelope with its own training-only
median predictor; keep both original events as individual prediction/scoring
targets. Their predictions are not added to the inclusive envelope again.
This fixes accounting without relabeling difficult original events or changing
their accuracy denominator. Other contained events likewise remain individual
targets but do not contribute twice to the outer sum.

The hierarchy is reconstructed from recorded local interval containment and
supplied to the composition engine. This is conditional accounting topology,
not proof that these are causal dependencies invariant across hardware. The
engine itself accepts event identities/parents/classes and predicted durations;
it cannot read measured durations, timestamps or residuals.

## Prediction boundaries

CPU semantic actions use the repaired gate-center model. Runtime/client-processing
spans use selected descriptor medians. Other lifecycle boundaries use their
own reference-domain median models. A directly fitted token model predicts
the inclusive local model-client-call wrapper: it covers 94.17% of 1,784 calls
within25. It is **not** a native GPU service prediction and is not a newly
validated hardware transfer law. Native timing is not summed into that wrapper.

These component models are jointly evaluated on one common instance fold
assignment. The earlier 42/43 result used a separate direct aggregate E2E
regression. Different models and targets explain the difference; the new
31/43 component result is not a claimed accuracy improvement.

## Saved outputs and verification

- `summary.json`: metrics, source identities/hashes and limitations.
- `cases.json`, `e2e.csv`: each case's accounted prediction, outer target,
  missing predictions and diagnostic gap.
- `events.jsonl`: every component target and prediction, with root membership.
- `graphs.jsonl`: supplied accounting forests and component predictions,
  sufficient to replay the composition engine without raw journals.
- `fold_models.json`: the fitted development models for reproduction; no
  assertion that these are a frozen final model.

29 focused tests passed across composition, overlap handling, repaired simulator
integration and earlier work/state boundaries. Tests preserve missing-child
failures, reject duplicate/cyclic/foreign identities and label-contaminated
graphs, and verify that overlapping startup/setup contributes only once.

Next work is concrete: model supported execution overhead, connect justified
hardware sensitivities, and complete required figure outputs. Do not replace
these gaps with an arithmetic residual or assert the assignment is impossible.
