# Offline CPU event candidate comparison

This is a bounded train-calibration model selection artifact. It does not modify the frozen model or production snapshot and does not claim a holdout or cross-hardware result.

The retained target has 23245 CPU events across 545 observed instances and 819 run trajectories. All events of an instance remain in one of the five established outer folds. The common view was identity-filtered before target/action decoding; prior mixed-artifact exposure remains disclosed.

## Fixed outer diagnostics

| Candidate | Events within 25% | Worst APE | p95 APE | Mean APE | CPU observed ms | CPU predicted ms | Instance trajectory pass | Unsupported features |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `coarse_class_median` | 0.6774 | 481.08% | 153.07% | 37.35% | 14636931.413 | 6986459.961 | 0.0000 | 0 |
| `mechanism_median` | 0.7172 | 4152.02% | 128.68% | 38.49% | 14636931.413 | 9721194.836 | 0.0037 | 0 |
| `log_geometric` | 0.6949 | 3277.72% | 146.30% | 38.38% | 14636931.413 | 9199551.003 | 0.0000 | 0 |
| `coverage_center` | 0.7419 | 20437.25% | 97.79% | 57.92% | 14636931.413 | 11297842.174 | 0.0055 | 0 |
| `tail_shrinkage` | 0.7091 | 2025.76% | 133.08% | 37.86% | 14636931.413 | 8665931.863 | 0.0018 | 0 |

## Nested selected procedure

The final offline candidate selected by the three-fold inner rule is `coarse_class_median`. Inner selection maximizes within-25% coverage subject to worst APE no greater than the coarse class-median comparator, then lower worst APE and simpler candidate. The selected procedure's five outer-fold estimate is 0.6774 within 25%, worst APE 481.08%, p95 APE 153.07%, and instance trajectory pass rate 0.0000.

The candidate is packaged for later validation only. CPU sums are reported over this retained event population; no full E2E pass is fabricated from CPU-only labels. No CPU frequency/core or cross-hardware scaling law is inferred.

## Per-class metrics for nested selected procedure

| Original class | Events within 25% | Worst APE | p95 APE | Mean APE | Misses |
|---|---:|---:|---:|---:|---:|
| `patch` | 0.9059 | 75.66% | 28.32% | 13.13% | 142 |
| `read` | 0.9435 | 70.20% | 25.77% | 11.96% | 341 |
| `search` | 0.8964 | 93.61% | 50.33% | 8.43% | 328 |
| `shell` | 0.2963 | 481.08% | 370.21% | 88.70% | 3639 |
| `test` | 0.0942 | 321.45% | 237.58% | 98.53% | 1269 |
| `traversal` | 0.5369 | 98.10% | 96.54% | 35.74% | 1470 |
| `write` | 0.8891 | 94.94% | 33.21% | 14.11% | 309 |

Unsupported feature rows remain in every denominator and route to the original-class/global fallback. See `comparison.json` for fold audits and CPU sums by instance.

After these five fixed diagnostics, one bounded sixth class-specific hybrid was
evaluated separately; it improves coverage while retaining the global worst
APE and is documented in `class_hybrid_report.md`. It remains an offline
candidate only.
