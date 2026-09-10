# D9 E2E composition development result — v4

This revision closes the offline E2E composition implementation gap using the
43 fully valid repaired executions / 22 instance groups. The valid conditional
composition predicts CPU interval union, joined native GPU time, and remaining
orchestration/client time separately, then adds each component once.

Grouped out-of-fold results:

| Candidate | Within 25% | Median error | P95 error | Worst error |
|---|---:|---:|---:|---:|
| Median baseline | 31/43 (72.09%) | 13.65% | 44.34% | 53.65% |
| Direct conditional | **42/43 (97.67%)** | 9.56% | 22.49% | **28.96%** |
| Calibrated composition | **42/43 (97.67%)** | **9.95%** | 23.53% | 31.17% |

The direct conditional model is selected because it matches composition
coverage with lower worst-case error. The composed model remains the valid
mechanism-accounting result. The only held-out miss is
`django__django-12113`; its CPU union is underpredicted. General action-class
features and training-only calibration did not close the miss.

The model is a conditional replay: completed action/request counts and terminal
token totals are supplied. No measured duration, residual, outcome, retry
result, case ID, or instance ID is a predictor. All executions of an instance
stay in one fold. Exact request identity remains 1,784/1,784.

Download `d9-offline-20260910-v4.tar.gz` for the complete source and model
layout. All 412 packaged file hashes were verified, 12 focused workspace tests
passed, and raw-free inference was exercised from an isolated extraction.

**Literal D9 is still not met:** one E2E development case exceeds 25%, CPU
individual-event coverage remains about 46% for the strongest tested broad
candidate, and cross-hardware validation is absent.
