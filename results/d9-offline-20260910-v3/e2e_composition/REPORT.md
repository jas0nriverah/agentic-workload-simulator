# E2E composition audit

This audit checks whether the retained repaired records support a leakage-safe
composition on the 43 fully valid CPU cases and 22 instance groups. It reads
the CPU inputs, identity-bound normalized ledgers, and the already retained
raw model and native journals. It opens no protected labels, retry-invalid CPU
cases, or GPU runs, and it does not acquire new traces.

Run it from the repository root with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/d9-salvage-20260910/e2e_composition/audit.py
```

The generated [audit artifact](audit.json) contains source hashes, per-case
interval accounting, overlap counts, raw identity joins, and the representative
reconstruction. The result is `unsupported_sequential_composition` for a full
prospective model, with a bounded trace-conditioned request envelope supported
offline.

## Lifecycle intervals and exact non-overlap

The CPU outer target is `lifecycle:outer_swe_agent`. In every case,
`lifecycle:runner_process_wrapper` has the identical start and end interval;
only one of those duplicate boundaries can enter an additive definition.

After removing the duplicate outer boundary and native rows, the eligible CPU
targets have this aggregate accounting:

| quantity | retained duration |
|---|---:|
| outer E2E target | 7,926,340.940 ms |
| sum of eligible CPU target intervals | 5,201,333.279 ms |
| union of eligible CPU target intervals | 3,930,315.259 ms |
| observation-only complement union | 3,996,567.989 ms |
| union of eligible targets plus observations | 7,926,340.940 ms |
| remaining union gap | 0.000 ms |

The target sum exceeds its union because lifecycle events are nested. The
eligible CPU union is 49.59% of outer wall. The zero-gap result reconstructs
already observed intervals; it does not define future outputs from saved
pre-event features.

For `00001-astropy__astropy-14182`:

| quantity | duration |
|---|---:|
| outer E2E | 149,770.617 ms |
| eligible CPU target union | 82,443.442 ms |
| eligible CPU target sum | 105,357.605 ms |
| observation-only complement union | 67,327.175 ms |
| combined union | 149,770.617 ms |

The representative has 43 complete `model_client_call` spans totaling
53,792.439 ms, 43 complete `model_request` spans totaling 52,240.468 ms, and
305 `unknown_residual` spans totaling 13,496.453 ms. `model_request` is nested
inside `model_client_call` in the interval reconstruction. Adding every target
and observation duration gives 224,886.966 ms, or 1.502 times outer wall, so
the union is required.

The named lifecycle boundaries cannot be treated as additive leaves. The
aggregate overlap evidence is:

| boundaries | overlapping event pairs | overlap |
|---|---:|---:|
| deployment_start / runtime_command | 172 | 412,938.273 ms |
| startup / runtime_command | 473 | 401,670.115 ms |
| setup / startup | 43 | 19,789.741 ms |
| get_state / runtime_command | 1,823 | 333,715.410 ms |
| client_processing / script_read | 305 | 49,045.365 ms |
| client_processing / runtime_command | 305 | 43,003.948 ms |
| script_read / runtime_command | 305 | 43,003.948 ms |
| bash_interrupt_control / semantic_action | 1 | 1,069.615 ms |

Without a supplied structural topology that assigns nested events to disjoint
leaves, adding per-event model outputs is not a valid sequential prediction.

## Raw request identity and the bounded envelope

The retained raw journals contain 1,784 model requests across the 43 cases.
Every request passes the exact identity chain:

* `model_request_start.parent_event_id` equals the matching
  `model_client_call_start.event_id`;
* the client start, client terminal, and proxy request share
  `logical_request_id`;
* the proxy terminal `client_span_id` equals the client terminal `span_id`;
* the proxy `physical_request_id` joins one native attribution row.

The aggregate counts are therefore 1,784/1,784 for both the raw
parent/logical/client-span chain and the native physical-ID join. The compact
normalized adapter does not preserve all of those wrapper fields and marks
the wrapper rows `model_eligible=false`; that is an adapter policy, not proof
that the raw identity is absent. The join can be derived offline from the
retained journals, so no acquisition correction or new run is required for
this part.

The raw `model_request` interval is a local CPU interval: all 1,784 rows lie
inside the CPU outer interval and use its CPU clock. The direct native `e2e`
metric joins each of those requests and is no larger than its local
`model_request` duration for all 1,784 rows. This supports one bounded
trace-conditioned composition: use the directly joined native duration as a
duration inside the local `model_request` envelope. It does not fit a future
request-time predictor, and its measured envelope overhead is diagnostic only:

| quantity | retained duration/count |
|---|---:|
| local `model_request` duration sum | 3,444,058.465 ms |
| native direct `e2e` duration sum | 3,168,984.163 ms |
| local envelope minus native `e2e` | 275,074.302 ms |
| native requests fitting the local envelope | 1,784 / 1,784 |

Strict nesting inside the client-call interval is not a safe boundary. 1,709
requests are inside their local client interval and 75 extend past its terminal
(the proxy can finish response finalization after client completion). The
75-row tail is an asynchronous boundary detail, not a missing identity or
clock join. Use `model_request` as the local envelope and do not append that
tail to the synchronous client critical path.

The native queue, prefill, decode, and direct `native:e2e` rows carry identical
request interval fields for all 1,784 requests. Their separate native clock
domain therefore cannot be unioned with CPU intervals, and the phase rows
cannot also be added to direct `native:e2e`:

| quantity | retained duration/count |
|---|---:|
| native phase sum | 3,160,006.946 ms |
| native direct `e2e` sum | 3,168,984.163 ms |
| requests with phase sum below `e2e` | 1,784 / 1,784 |
| requests with duplicate phase/e2e interval fields | 1,784 / 1,784 |

Unknown residual duration remains a target diagnostic. It is not a prospective
feature or predictor.

## Decision and sample need

No full prospective sequential model was fit or scored. The valid offline
result is the exact raw request join plus the directly joined native duration
bounded by its local `model_request` envelope. A full composition still lacks a
supplied action/request topology for resolving nested CPU leaves and a
prospective feature contract for unknown residual time. The measured residual
and envelope overhead remain diagnostics only.

The historical direct-E2E result of 79.00% is from 819 runs and 545 instances,
not this 43-case/22-instance cohort, so it is omitted from comparison.

Additional samples are not needed to establish the raw join or local envelope;
the retained records already establish both across all 43 cases. Runs would be
useful only after an offline topology/feature contract exists, for numerical
transfer or a protected evaluation. That is model-contract uncertainty rather
than evidence of an acquisition defect.
