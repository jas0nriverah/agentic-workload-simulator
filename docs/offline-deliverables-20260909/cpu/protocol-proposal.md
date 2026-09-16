# Prospective CPU event model protocol record

Status: proposal sent before fitting; the authoritative locked rules are in
`../PROTOCOL.md`. This bounded
offline development uses only the retained, eligible
`docs/retained-analysis-20260909/d9/train-calibration-descriptor-experiment.csv`
labels and a train-only action/feature cache supplied by the integration
agent. It does not acquire new data, open inference or GPU artifacts, or
reopen mixed heldout labels.

## Data boundary and identity gate

The production manifest is the authority for the 546 `train_calibration`
instance IDs. The retained event-label CSV has 23,245 CPU events from 545
observed instances; the 546th manifest instance is allowed to have zero rows.
Every incoming feature row must carry `event_id`, `instance_id`, and the
manifest partition. The loader will first validate the identity and partition
against the pinned manifest, reject missing/conflicting identities, and only
then retain the row. It will fail closed on any heldout/final/confirmation
partition row. A row is usable only if its `event_id` joins exactly once to the
retained CSV label and its instance is one of the five fixed folds.

The accepted feature contract is a pre-event action descriptor derived from
the action string without executing it or reading its output/filesystem state:
semantic/original class, executable, operation, runner/module, recursion,
`find -exec` mode, pipeline/redirect mode, pager susceptibility, declared
path-count and command-byte buckets, plus optional explicitly declared script
state/work buckets if the common dataset can prove they were available before
the event. Exact command text, command hash, case/repository memorization,
future state, measured duration, output/return bytes, current output tokens,
failure/end-state and residual/current wall time are excluded from features.
The original-class label remains an allowed target grouping/backoff key, not a
free-form identity key.

The prior D9 scripts/report bulk-read mixed historical action/prediction JSON
before filtering. That prior exposure is disclosed here and means this work
does not claim pristine blindness. This protocol itself reads only the
train-only eligible cache or a stream that discards non-train rows before
retained action/label use; it never uses the prior mixed cache as a new data
source.

## Fixed comparison and selection

Preserve the existing five folds exactly:
`sha256('assignment.d9.train-fold-v1:' + instance_id)` first eight bytes
modulo five. All events of an instance stay in one fold. Within each outer
fold, fit on the other four folds and score the withheld fold. Candidate
definitions, support thresholds, buckets and tie rules are frozen before
scoring. A three-fold instance-grouped inner CV on each outer-training
partition selects coverage subject to no worse worst APE than the coarse
comparator, then lower worst APE and simpler candidate. The event labels in a
withheld outer fold are not used to choose a center, support route or
candidate.

Compare five deterministic, interpretable candidates:

1. original-class coarse median baseline;
2. mechanism median with fixed sparse backoff;
3. log-geometric center with the same support-gated hierarchy;
4. coverage-optimal center (maximize the fixed 25% event gate on training
   labels, then minimum MAPE and lower log center as deterministic ties);
5. robust-tail-aware shrinkage: blend a supported group center toward its
   parent by event/instance support, with a prespecified upper-tail summary
   used only to choose the blend weight, never to clip predictions.

The default hierarchy is mechanism → operation → semantic/original class →
global. Group support is fixed before evaluation at at least 25 training
events and 3 training instances; unsupported groups immediately back off.
No candidate can memorize exact commands, case IDs or repository identities.

The prior-mechanism candidate is excluded because the common view does not
prove same-run event order; ambiguous sequential joins are not prospective
features.

No additional work-volume feature ablation is included: the common view does
not establish a separately validated declared-work field. Its missing-work
branch remains explicit in the feature contract.

## Required reporting

For every candidate and each original class, report event count, within-25%
rate, misses, mean/median APE, worst APE, p95 APE, absolute error and signed
bias. Report all-event CPU sum of predictions versus observed labels, the
per-instance event trajectory pass rate (all scored events within 25%), and the
strict conjunction with any supplied CPU-sum gate. Unsupported/absent rows
remain in the denominator and are counted explicitly.

The primary selection rule is the highest defensible all-event within-25%
coverage subject to no worse worst APE than the coarse comparator; ties favor
lower worst APE and simpler candidate. A candidate with a small coverage gain
but a sharply worse tail remains a diagnostic and is not selected. The final
report states the coverage-vs-tail tradeoff and why the selected center/backoff
is defensible.

All CPU sums are event sums over the retained 23,245-event target population.
No trajectory E2E pass is fabricated from this CPU-only data. Cross-hardware
transfer is a protocol only: retain CPU descriptors, fit no speed law from
one platform, and require a future paired measurement on the destination
hardware before claiming transfer.
