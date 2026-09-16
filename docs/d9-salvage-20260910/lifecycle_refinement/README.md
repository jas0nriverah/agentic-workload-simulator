# Lifecycle refinement results — September 14

Three bounded follow-ups to the descriptor scan were evaluated on the same
43 training cases / 22 instances and fixed grouped folds. All 2,730 runtime
and 7,128 client-processing targets remain in their respective denominators.
Original acquisition and final-evaluation cases are untouched.

| Candidate | Runtime within25 | Runtime worst error | Client within25 | Client worst error |
|---|---:|---:|---:|---:|
| Previous descriptor median | 87.77% | 99.294% | 25.95% | 338.39% |
| Training-only 25%-coverage center | **91.03%** | 99.325% | **29.70%** | **245.93%** |
| Additional lifecycle context | 86.41% | 721.15% | 27.36% | 361.58% |
| Context with descriptor fallback | 86.41% | 721.15% | 27.36% | 361.58% |

The coverage-center runtime gain over descriptor median is +3.26 percentage
points; the paired instance-bootstrap interval is +1.81 to +4.75 points.
Worst error increases by 0.031 percentage points. The gain is credible on this
development cohort but not a uniform improvement, and it fails the preset
no-worst-error-regression promotion gate.

For client processing the center improves coverage and worst error, but the
coverage-gain interval is -1.20 to +7.86 points. More preceding-event context
improves coverage less and worsens the tail. Hierarchical fallback is numerically
identical on this cohort, disproving the tentative explanation that unsupported
context buckets caused the observed regression. Do not repeat that hypothesis.

Retain descriptor medians as the selected artifacts under the predeclared
promotion rule (positive bootstrap lower bound, nondecreasing equal-instance
coverage, no worse maximum error). Preserve the coverage-center alternatives
as explicit tradeoff candidates. Zero instances have all events within25.
These development bootstrap intervals omit model-selection uncertainty.

`report.json`, `fit_artifact.json` and `predictions.jsonl` retain the results,
serialized selected tables and every grouped prediction. Regenerate using:

```sh
.venv/bin/python docs/d9-salvage-20260910/refine_lifecycle_20260914.py
```

The source adapter verifies pinned training identities, raw hashes, target/start
joins, and same-clock parent/preceding-event ordering. Predictors use only
categorical start-time command/lifecycle information. No outcomes, latencies,
case IDs or future events form prediction keys. Tests cover label independence,
serialization parity, independent-instance support and sparse-context fallback.
Together with repaired-simulator integration tests, **11 tests passed**.

No new serving promotion, GPU inference or acquisition changes. A raw-free
serving interface for these lifecycle candidates remains separate work; this
experiment does not imply full D9 or hardware transfer acceptance.
