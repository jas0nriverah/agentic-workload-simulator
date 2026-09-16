# Bounded D9 individual CPU-operation comparison

This is an offline, trace-conditioned diagnostic on four fixed fully valid `train_calibration` instances. It does not change the BPF collector, acquire new data, or claim a complete D9 pass.

## Fixed population and raw stream contract

Cases were selected by ascending queue ordinal after the evidence validity gate, taking the first four distinct instances: **1 (astropy__astropy-14182), 3 (astropy__astropy-14995), 4 (astropy__astropy-6938), 17 (astropy__astropy-7746)**. Selection did not inspect operation durations, evaluator outcomes, or model errors.
The selected v3 ABI streams contain **977433 raw records** across **4 instances** and **390973200 bytes**. The helper streamed each 400-byte packet through the existing `BpfWorkCollector._event_row` packet decoder (the same v3 layout used by `iter_bpf_events`); it did not create a full decoded JSON export. The compact typed-array index used at most **52781382 bytes** (50.34 MiB).

Each event retains an identity made from case ID, action token, sequence, and raw byte range. Action-token ranges, BPF/kernel clock descriptors, host monotonic clock identity, source manifests, and recorded raw-stream hashes are retained in `model.json`.

## Feature and target contract

The target is the completed BPF kernel operation duration (`kernel_end_ns - kernel_start_ns`) for an individual event. Syscall failures remain valid duration targets; failure status and return values are diagnostics only. Positive-duration medians are conditional on positive targets and exclude zero and censored targets rather than imputing them. Known fork/clone/thread lineage records with zero duration are reported separately from the individual-operation denominator; an unclassified zero-duration operation would remain in the required gate and pass only with an exact-zero prediction. Censored targets remain explicitly unsupported.

Features are restricted to syscall-entry-known data: syscall number/kind, decoder-proven requested-size words where available, and a lexical path class from bounded path bytes copied by the sys_enter handler. No return bytes, failure/result status, completed latency, or post-event field enters a model key.

## Leave-one-instance-out results

| Model | Scored events | Within 25% | Mean APE | P95 APE | Worst APE |
|---|---:|---:|---:|---:|---:|
| `global_median` | 974169 | 29.11% (283534) | 52.26% | 117.24% | 418.62% |
| `operation_median` | 974169 | 39.70% (386757) | 41.40% | 104.42% | 4083.77% |
| `operation_requested_size_bucket_median` | 974169 | 43.02% (419087) | 41.02% | 110.74% | 4083.77% |
| `operation_path_class_median` | 974169 | 46.32% (451237) | 39.96% | 109.79% | 4152.24% |

Per-instance metrics and the identity/range of each model's worst event are recorded in `model.json`; the four candidates are fixed descriptive comparisons, not an automatic production selection.

## Duration-population gate

The positive-duration table is conditional coverage. This gate covers records that require an individual duration decision: positive targets must be within 25 percent, individual-operation zero targets pass only with an exact-zero prediction, and censored/unsupported targets remain in the denominator and do not pass. Known zero-duration lineage/provenance records are counted separately and are outside this duration population.

| Model | Duration records | Passing records | Coverage over duration population | Modeled zero exact-zero | Lineage zero | Unsupported/censored |
|---|---:|---:|---:|---:|---:|---:|
| `global_median` | 974169 | 283534 | 29.11% | 0/0 | 3264 | 0 |
| `operation_median` | 974169 | 386757 | 39.70% | 0/0 | 3264 | 0 |
| `operation_requested_size_bucket_median` | 974169 | 419087 | 43.02% | 0/0 | 3264 | 0 |
| `operation_path_class_median` | 974169 | 451237 | 46.32% | 0/0 | 3264 | 0 |

## Coverage and integrity diagnostics

- Valid positive-duration targets scored: **974169**.
- Zero-duration records: **3264** total, with **3264** known lineage/provenance zeros and **0** individual-operation zeros; by kind: `{"fork": 2274, "thread": 990}`.
- Censored targets: **0**; negative targets: **0**. Unsupported/censored records stay visible in the duration-population denominator and never pass.
- Event statuses observed: success **895425**, failure **82008**. Status was not a feature.
- Perf-buffer lost events: **0**; lost event/path/pending map records: **0 / 0 / 0**; callback errors: **0**.
- Action-range/event-count mismatches: **0**; raw records decoded: **977433**.

The zero-target classification is explicit: known lineage/provenance records are outside individual-operation duration coverage, while any modeled operation zero would be required to predict exactly zero. No drop or range mismatch was observed in the selected cases.

## Scope limits

This four-instance sample is an ABI/target-method check. It does not establish all-production CPU accuracy, cross-hardware transfer, a prospective pre-execution predictor, or the assignment's complete CPU+GPU+E2E D9 acceptance gate. The existing collector source remains unchanged.

`run_atomic_cpu.py` is the reproducible helper. An optional `--predictions path.jsonl.gz` audit stream preserves each scored event identity and all candidate predictions; it is intentionally disabled by default to avoid a giant JSON export.
