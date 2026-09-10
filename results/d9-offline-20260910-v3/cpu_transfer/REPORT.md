# Bounded Astropy-to-Django CPU-operation transfer

This is an offline, trace-conditioned development transfer diagnostic. It applies the existing four fixed Astropy leave-one-instance-out median fits to a fixed ordinal Django sample; it does not fit on Django targets, change acquisition, or claim the D9 all-event gate.

## Fixed Django sample and byte bound

The selection policy was ascending queue ordinal after the evidence validity gate, distinct `train_calibration` Django instance IDs, stopping at the first valid case that would exceed the raw-byte bound. The selected sample is **18 (django__django-10914)**. It contains **777670 raw records** and **311068000 bytes**; the bound is **400000000 bytes**. The next distinct fully valid case is retained in the selection audit with its overflow reason, but its stream was not opened.

The raw stream was decoded through the existing `BpfWorkCollector._event_row` v3 decoder into the atomic helper's compact typed arrays. No normalized event export or prediction JSONL was created.

## Transfer contract

Each existing fit is trained on the other three fixed Astropy instances selected by the atomic CPU helper. The fit is then applied unchanged to the selected Django instance. The four candidate families are exactly the existing `global_median`, `operation_median`, `operation_requested_size_bucket_median`, and `operation_path_class_median` families; no new model family or Django refit is introduced.

The target is positive completed BPF kernel duration. Failure status and return values are not features. Known fork/clone/thread lineage zeros are outside the individual-operation duration denominator; other zero, censored, or negative targets remain explicit required records and do not pass by omission.

## Per-event transfer results

Each row below scores the same Django instance once under one pre-existing Astropy fold. `count` is the number of positive-duration events; coverage is the fraction with absolute percentage error at most 25 percent.

| Astropy fit held out | Candidate | Count | Within 25% | Worst APE |
|---|---|---:|---:|---:|
| `astropy__astropy-14182` | `global_median` | 774506 | 31.97% (247628) | 245.50% |
| `astropy__astropy-14182` | `operation_median` | 774506 | 39.70% (307478) | 2286.30% |
| `astropy__astropy-14182` | `operation_requested_size_bucket_median` | 774506 | 42.27% (327420) | 2286.30% |
| `astropy__astropy-14182` | `operation_path_class_median` | 774506 | 43.52% (337097) | 2325.35% |
| `astropy__astropy-14995` | `global_median` | 774506 | 32.42% (251106) | 256.40% |
| `astropy__astropy-14995` | `operation_median` | 774506 | 39.83% (308523) | 1736.39% |
| `astropy__astropy-14995` | `operation_requested_size_bucket_median` | 774506 | 42.28% (327440) | 1736.39% |
| `astropy__astropy-14995` | `operation_path_class_median` | 774506 | 44.13% (341780) | 1757.41% |
| `astropy__astropy-6938` | `global_median` | 774506 | 32.46% (251413) | 258.29% |
| `astropy__astropy-6938` | `operation_median` | 774506 | 39.81% (308300) | 1352.16% |
| `astropy__astropy-6938` | `operation_requested_size_bucket_median` | 774506 | 42.45% (328766) | 1205.19% |
| `astropy__astropy-6938` | `operation_path_class_median` | 774506 | 43.20% (334560) | 1224.54% |
| `astropy__astropy-7746` | `global_median` | 774506 | 32.28% (250010) | 251.18% |
| `astropy__astropy-7746` | `operation_median` | 774506 | 39.67% (307212) | 1686.11% |
| `astropy__astropy-7746` | `operation_requested_size_bucket_median` | 774506 | 42.23% (327100) | 1686.11% |
| `astropy__astropy-7746` | `operation_path_class_median` | 774506 | 43.56% (337402) | 1708.33% |

## Per-instance and cross-fold stability

The selected Django instance is the only test instance in this bounded transfer pass. The summary reports fold-to-fold variation without treating repeated scoring of one trace as independent instances.

| Candidate | Test instance | Fold count | Count/fold | Coverage min / mean / max | Worst APE max |
|---|---|---:|---|---:|---:|
| `global_median` | `django__django-10914` | 4 | [774506, 774506, 774506, 774506] | 31.97% / 32.28% / 32.46% | 258.29% |
| `operation_median` | `django__django-10914` | 4 | [774506, 774506, 774506, 774506] | 39.67% / 39.75% / 39.83% | 2286.30% |
| `operation_requested_size_bucket_median` | `django__django-10914` | 4 | [774506, 774506, 774506, 774506] | 42.23% / 42.31% / 42.45% | 2286.30% |
| `operation_path_class_median` | `django__django-10914` | 4 | [774506, 774506, 774506, 774506] | 43.20% / 43.60% / 44.13% | 2325.35% |

## Existing four-Astropy reference

These are the atomic helper's existing four-instance leave-one-instance-out development metrics, copied as a comparison reference. They are not recomputed from Django and are not a sealed holdout.

| Candidate | Astropy count | Within 25% | Mean APE | P95 APE | Worst APE |
|---|---:|---:|---:|---:|---:|
| `global_median` | 974169 | 29.11% (283534) | 52.26% | 117.24% | 418.62% |
| `operation_median` | 974169 | 39.70% (386757) | 41.40% | 104.42% | 4083.77% |
| `operation_requested_size_bucket_median` | 974169 | 43.02% (419087) | 41.02% | 110.74% | 4083.77% |
| `operation_path_class_median` | 974169 | 46.32% (451237) | 39.96% | 109.79% | 4152.24% |

## Integrity and population diagnostics

- Raw records decoded: **777670**; valid positive-duration targets scored: **774506**.
- Raw hash/token joins: **True** raw hash verified; **0** action-token count mismatches; range mismatch count **0**.
- Status counts: `{"failure": 100321, "success": 677349}`; success **677349**, failure **100321**.
- Zero targets: **3164**, of which known lineage zeros are **3164** and modeled-operation zeros are **0**; censored **0**; negative **0**.
- Aggregate loss counters: `{"censored_pending_count": 0, "event_callback_error_count": 0, "lost_event_records": 0, "lost_path_records": 0, "lost_pending_records": 0, "perf_lost_events": 0}`. Callback and loss counters remain visible in `model.json` per selected case.
- Protected case-result, model-event, evaluator-label, and outcome files were not opened by this transfer script; the only target values read were completed CPU durations from the selected raw BPF stream for scoring.

## Interpretation

The transfer rows answer whether the existing Astropy operation medians retain their accuracy on one ordinally selected Django trace. They do not establish repository-wide generalization: the byte cap leaves one Django instance, the four transfer fits each train on only three Astropy instances, and the same Django trace is scored repeatedly across those folds. Compare both coverage and worst error; a candidate that improves Astropy coverage but has materially lower Django coverage or a larger transfer tail has not demonstrated portable behavior.
On this bounded trace, path-class coverage averages **43.60%** versus **46.32%** in the four-Astropy reference; size-bucket averages **42.31%** versus **43.02%**; operation-only is **39.75%** versus **39.70%**; and global is **32.28%** versus **29.11%**. The path family remains the best transfer candidate by coverage, but its coverage is lower than the Astropy reference, so these results are evidence against claiming that the four-Astropy result generalizes across repositories.

The output is reproducible with `run_cpu_transfer.py`. The script intentionally does not write event-level prediction exports to keep the artifact bounded.
