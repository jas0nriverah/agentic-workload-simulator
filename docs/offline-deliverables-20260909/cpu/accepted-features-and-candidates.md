# Accepted feature and candidate brief

The train only common view is
`docs/offline-deliverables-20260909/training_view/tools.jsonl`, with integrity
metadata in `training_view/manifest.json`. Its manifest declares 23,245
retained CPU rows, 545 observed instances, 819 trajectories, and 546
train-calibration manifest clusters. Identity-only indices were decoded before
target/action values; no mixed-source file is used by this work. The
authoritative fixed rules are in
`docs/offline-deliverables-20260909/PROTOCOL.md`.

Prediction-time fields are derived from each already-authorized action before
execution: semantic class, operation, executable family, test runner, shell
execution mode, git pager susceptibility, `find -exec` mode, pipeline-stage
bucket, recursion flag, operand-count bucket, and declared command-byte bucket.
No target, current duration, output/return bytes, output tokens, failure/end
state, future action, measured residual, repository identity, case ID, exact
command, command hash, or high-cardinality path is used in a prediction key.
Rows with missing or invalid action features stay in the evaluation denominator
and use the declared original-class fallback. A prior-mechanism candidate is
excluded because the common view does not prove same-run event order; this
avoids using an ambiguous sequential join as a prospective feature.

The five fixed candidates are:

1. `coarse_class_median`: original-class median;
2. `mechanism_median`: mechanism → operation → semantic class → original class
   median, with 25 events and 3 instances required at every non-class route;
3. `log_geometric`: geometric mean centers under the same hierarchy;
4. `coverage_center`: finite-sample center maximizing the ≤25% gate, then
   minimum MAPE and lower log center as ties;
5. `tail_shrinkage`: a median center shrunk toward its parent by support and
   an explicit p90/median tail ratio; predictions are never clipped to a tail
   quantile.

All fixed candidates are scored in the five existing outer instance folds.
Within each outer training partition, the three-fold instance hash
`sha256('assignment.d9.cpu-inner-v1:' + instance_id)[:8] % 3` chooses among
the fixed candidates by maximum within-25% coverage subject to worst APE no
greater than the inner coarse-class baseline; ties use lower worst APE and then
simpler candidate complexity. If no alternative qualifies, the coarse
comparator remains selected. The selected candidate is then fit on all
outer-training rows and scored on the withheld outer fold. The final offline
candidate is fit on authorized training data only for packaging; it is not a
production artifact. No outer labels select the candidate.
