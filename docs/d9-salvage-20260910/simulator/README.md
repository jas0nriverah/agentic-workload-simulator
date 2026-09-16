# Runnable D9 salvage simulator

Current requirements and status: [PDF contract](../../current/PDF_CONTRACT.md).

This directory is an offline candidate package for Deliverable 9. It uses the
retained historical evidence and keeps every target boundary visible:

| Target | Supplied prediction inputs | Status |
| --- | --- | --- |
| `repaired_semantic_action` | supplied action, repository, recorded pre-action operation class, verified reference CPU domain | repaired gate-center semantic development candidate |
| `conditional_repaired_e2e` | supplied complete event/action counts and token totals | repaired direct conditional E2E; not an event sum |
| `conditional_native_phase` | phase, integer prompt/completion/cached counts, explicit cache-trace opt-in, verified native domain | queue/prefill/decode; prefill attention interaction selected |
| `historical_cpu_tool_wall` | current action-derived semantic buckets | retained CPU class hybrid |
| `historical_gpu_request_proxy_wall` | no request features | retained completed-request proxy |
| `conditional_gpu_request_proxy_wall` | supplied input/context/output/max token workload | conditional GPU request-proxy log model |
| `historical_start_known_e2e` | repository | retained direct E2E control |
| `conditional_trace_e2e` | declared tool/request counts and token sums | salvage trace-conditioned relative NNLS |
| `conditional_native_e2e` | prompt and completion tokens from a realized native request trace; optional cache trace | corrected scaled-linear native `native:e2e` candidate |
| `assignment_cpu_event`, `assignment_gpu_event` | explicit Step-3 descriptors plus hardware | optional v3 artifact path |
| legacy native phase target aliases and lifecycle targets | — | unsupported under those contracts; use the explicit conditional native-phase contract for phase inference |

September 14 iteration results and exact new input contracts are in
[ITERATION_RESULTS_20260914.md](../ITERATION_RESULTS_20260914.md). Changing the
hardware profile for these repaired candidates is rejected; no transfer law
is fabricated. Queue/prefill/decode predictions are separately scored and are
not added to an already inclusive native request E2E prediction.

The historical candidates were fit on one reference environment. Every result
contains the complete `assignment.hardware-profile.v1` profile, its SHA-256,
and a transfer flag. The default `h100-80gb-pace` profile is a legacy
simulator assumption copied from the historical D9 script; it is not a fresh
host measurement. The retained candidates never silently rescale for a new
profile. The optional shared v3 artifact path does use the profile in its
design row, but its cross-hardware accuracy remains unvalidated.

## Predict

From this directory:

```bash
python3 run.py predict --request example_request.json
```

The request body contains exactly `target` and `inputs`. Labels and measured
residuals are rejected. To bind prediction identity for a later score, add
`--event-id`, `--trajectory-id`, `--case-id`, and `--attempt-id`; those fields
are metadata, never model inputs.

The trace-conditioned E2E model is deliberately labeled conditional replay:

```json
{
  "target": "conditional_trace_e2e",
  "inputs": {
    "tool_count": 30,
    "request_count": 31,
    "input_tokens": 45000,
    "output_tokens": 3000
  }
}
```

Recorded output tokens are allowed here because they describe a supplied
workload. They are not a prospective prediction of the next response and do
not describe a native phase or an event sum.

The conditional GPU request-proxy model uses the repaired artifact in
`../conditional/fit_artifact.json` and has the same completed-trace boundary:

```json
{
  "target": "conditional_gpu_request_proxy_wall",
  "inputs": {
    "input_tokens": 1829,
    "context_tokens": 1829,
    "output_tokens": 75,
    "max_output_tokens": 2048
  }
}
```

Its selected candidate is `nonnegative_log_additive`, fit on the retained
request-proxy target. Hardware is recorded for provenance and remains
unvalidated for transfer; this target is not native GPU queue, prefill, decode,
or E2E composition.

The corrected native per-request model is available through the explicit
`conditional_native_e2e` target. The default request uses the token candidate:

```json
{
  "target": "conditional_native_e2e",
  "inputs": {
    "prompt_tokens": 23155,
    "completion_tokens": 55
  }
}
```

This returns the direct `native:e2e` request prediction from
`../native/fit_artifact.json`. The candidate is conditional replay: completion
tokens are supplied descriptors of a realized workload and are not a
prospective online feature. The fit is bound to the verified native hardware
domain recorded in the result; changing the simulator hardware profile does
not rescale it, and cross-hardware accuracy is unvalidated.

The cache candidate requires an explicit realized cache trace. Include both
`cache_trace: true` and `cached_tokens` to opt in:

```json
{
  "target": "conditional_native_e2e",
  "inputs": {
    "prompt_tokens": 23155,
    "completion_tokens": 55,
    "cache_trace": true,
    "cached_tokens": 23104
  }
}
```

Supplying `cached_tokens` without that opt-in is rejected. Queue, prefill, and decode are available under the separate
`conditional_native_phase` contract and are never added to this inclusive
direct E2E target.

## Strict scoring

Predictions and labels are separate JSONL files. Both use the target, event,
trajectory, case, attempt, and event identity fields. A missing prediction is
kept as `unsupported_missing_prediction`; it does not improve the denominator.
Zero-duration labels pass only with an exact zero prediction. For a literal
conjunction, declare the required target kinds:

```bash
python3 run.py score \
  --predictions frozen-predictions.jsonl \
  --labels labels.jsonl \
  --required-target cpu_tool \
  --required-target gpu_request \
  --required-target e2e \
  --output strict-metrics.json
```

The scorer joins on `(target, trajectory_id, case_id, attempt_id, event_id)`,
rejects duplicate scoped identities, reports missing target kinds, and flags
native E2E rows combined with queue/prefill/decode rows as an overlap that
cannot be summed into a full E2E claim.

## Current evidence boundary

The retained historical report gives the best CPU class hybrid at 68.51% of
tool events within 25% with a 481.08% worst error. The trace-conditioned E2E
salvage candidate reaches 78.9988% of 819 historical runs within 25% with a
94.9995% worst error, but uses the supplied realized workload list. Neither
result is a complete D9 acceptance result. The conditional GPU request-proxy
candidate reaches 96.69% of retained request events within 25% in its grouped
development comparison, but is still a historical conditional target. The
repaired evidence now includes an accepted bounded native calibration path.
Its corrected token candidate reaches 94.8558% of 2,080 requests within 25% in
grouped five-fold development OOF (7/49 cases have every request within 25%);
the explicit cache-trace candidate reaches 97.4519% (19/49). These are
conditional replay development numbers on 49 train-calibration cases, not a
sealed evaluator holdout or literal all-event D9 PASS. Hardware transfer
remains unvalidated.
