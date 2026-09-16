# D9 GPU request and lifecycle handoff

This directory is offline development only. It does not change acquisition,
configuration, queues, network state, inference, GPU work, or any frozen artifact.

## Data boundary

The retained native evidence contains two repaired attempts for
`django__django-7530`: `combined-case-v8/runner_attempts/attempt-001` has 40
physical requests and the accepted configuration attempt has 35. They are
confirmation-excluded, share one cluster, and are descriptive only. They must
not enter a fit, cross-validation score, or final acceptance denominator.

The synthetic `SNAP/verification/v2-fixtures` journal is a contract smoke
fixture, not a measurement. It must be passed as `--source-kind synthetic_smoke`
and cannot establish timing accuracy.

The bounded historical proxy contains 23,868 completed model events from 545
eligible instances. It permits a grouped proxy comparison but cannot recover
the all-required-event denominator, so its rates cannot establish the D9
universal event/E2E gate. Its `input_tokens` value is usable only under the
historical contract that the token count was known before dispatch. The repaired
native confirmation fixture does not prove that condition: all 40 starts have
only `max_output_tokens=2048`, while prompt tokens appear only after completion.

Any historical training input must be constructed by the root-owned common
dataset builder: call `historical_analysis_scope.frozen_scope()` first, select
identity against the production split manifest, then open a raw case path. The
eligible partition is `train_calibration` only. The frozen denial boundary
excludes 137 clusters and 451 explicit run/case IDs. The scope denylist guards
access; it does not itself authorize training.

## Candidate model and targets

The candidate has three separately named nonnegative targets:

```
conditional E2E = predicted CPU events
                + predicted physical GPU-request E2E
                + predicted non-overlapping lifecycle intervals
```

GPU request E2E is a direct request wall target. Queue, prefill, and decode
may instead be separate targets when directly observed; they must not also be
summed with a request-E2E target. Lifecycle includes only intervals outside
the CPU/GPU targets (for example setup, startup, retry, and teardown), with
interval-union accounting to prevent overlap. UNKNOWN/unassigned complement
is reported as a coverage diagnostic and is neither a feature nor a fitted
residual target.

The adapter permits only `input_tokens`, directly available `context_tokens`,
and `max_output_tokens` for a prospective GPU request. It rejects realized
`output_tokens`, current request duration, queue/prefill/decode values,
current cache hits, measured residual, and future actions. A conditional
assignment-level replay may use realized output tokens only under a separately
labeled conditional-replay contract; this prospective candidate does not use
them.

`conditional_known_action_list` means the CPU actions, GPU requests, and
lifecycle spans are known before the composition is frozen. It can report a
sum for that declared list. `prospective_full_trajectory` cannot report E2E
until a pre-event policy supplies the future action/request list; otherwise a
replay sum would be mislabeled as a forecast.

## Validation protocol

Fit only after the common eligible dataset supplies independent
train-calibration identities with direct GPU targets. Use the same
instance-grouped folds as CPU, assigned once from the instance ID and shared
before fitting. Fit medians/regularized nonnegative models within each training
fold, with support-aware backoff. Never split requests from one instance across
train and validation.

For each validation fold, freeze features before reading its target. Report
every required CPU event, GPU event, lifecycle event, and E2E trajectory in the
denominator. An event passes only when APE is at most 25%. A trajectory passes
only when every required component event and its declared E2E are within 25%.
Report coverage and unsupported/censored records separately; do not replace
them with a residual or drop them. The historical-proxy result must be labeled
as such and cannot certify the native configuration.

## Adapter smoke

`gpu_lifecycle_adapter.py` has no estimator and never fits a residual. It
creates an availability artifact from a model journal and optionally composes
already-frozen, pre-event component predictions. For the synthetic smoke:

```text
python3 gpu_lifecycle_adapter.py \\
  --model-journal ../../../SNAP/verification/v2-fixtures/model_events.jsonl \\
  --source-kind synthetic_smoke \\
  --availability-output /tmp/gpu-availability.json
```

The expected smoke status is `unidentifiable_from_this_source`: one synthetic
identity and no native GPU phase labels. A future root-selected,
identity-filtered calibration journal may instead be passed with
`--source-kind eligible_train_calibration`.

The real confirmation smoke binds 40 `model_request_start` records one-to-one
with 40 native terminal records by physical request ID. It verifies the join
and preserves the fact that native terminal token/cache/phase values are
targets. It remains one excluded instance and therefore has no fitting or
validation role; see `real_native_smoke.json`.
