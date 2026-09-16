# Conditional E2E candidate

Run `python3 docs/d9-salvage-20260910/e2e/compare.py` from the repository root, then `python3 docs/d9-salvage-20260910/e2e/plot.py`. No GPU or third-party Python package is used. Source hashes must match the retained training-view manifest.

The selected **development candidate** is nonnegative linear regression with training loss weighted by inverse squared target duration (relative-error least squares). Label-derived training weights are not predictor inputs. Inputs are supplied workload tool count, request count, total input tokens and total output tokens, with a nonnegative intercept. The inference function receives only those descriptors.

The declared trace-conditioned contract is essential: future action and token counts are not known before an agent executes. These results are not an online forecast. The target is historical outer E2E directly, not a sum of independently validated lifecycle/event models.

Five existing instance-grouped outer folds preserve every repeated instance. No excluded confirmation labels were used. The fixed comparison is median baseline, ordinary nonnegative least squares, and relative-error nonnegative least squares. Selection uses these development predictions and must not be represented as pristine holdout validation.

| Model | Within 25% | Worst error |
|---|---:|---:|
| Median baseline | 538/819 (65.69%) | 168.55% |
| Ordinary NNLS | 417/819 (50.92%) | 223.90% |
| Relative-error NNLS | **647/819 (79.00%)** | **95.00%** |

The gain is positive in all five folds. A paired instance-cluster bootstrap of fixed out-of-fold predictions gives +13.31 percentage points, conditional 95% interval +10.52 to +16.15 points. This interval excludes model-selection uncertainty. The p95 statistic uses the nearest-rank definition.

`model.json` contains full-training coefficients for later prediction; `predictions.jsonl` contains held-out development predictions, never in-sample predictions substituted for validation. `report.json` retains fold metrics and limitations. `e2e_error_cdf.svg` is generated from those predictions.

**D9 is not passed:** 172/819 E2E predictions still miss 25%; individual CPU/GPU events and cross-hardware accuracy require their own validation. No hardware transfer law is inferred by this fit.

Repository diagnostics are saved in `repository_diagnostics.json`. The candidate covers only 48.5% of 66 matplotlib runs and 58.6% of 29 pydata runs within 25%, versus 84.9% of 352 Django runs. These are descriptive slices of the same development predictions, not a new validation or an instruction to tune individual cases. The new partial-production cohort contains only Astropy/Django, so it cannot independently close those repository gaps.
