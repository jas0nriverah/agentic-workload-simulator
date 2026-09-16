# Checked model-improvement opportunities

This bounded offline pass tests the proposed opportunities rather than assuming more runs will improve the simulator. No GPU inference, protected evaluation fitting, or frozen acquisition changes were used. All comparisons remain development analyses, not independent final D9 evaluation.

## CPU mechanisms and transfer

The [operation analysis](../cpu_mechanisms/REPORT.md) scores 974,169 positive-duration operations across the four fixed Astropy instances, grouped by instance. Openat, read, stat and close account for **75.78% of misses** under the operation/path model; [exact counts](cpu_miss_priorities.json) retain the source hash.

| Candidate | Within 25% | Worst error | Decision |
|---|---:|---:|---|
| Existing operation/path median | 46.32% | 4,152.24% | Retain general reference |
| Operation + size + path median | 46.77% | 4,152.24% | Small coverage gain, worse mean/p95 error; do not promote |
| Training-only maximum-coverage representative | 45.43% | 1,490.50% | Useful tail-error alternative; new repository transfer unproven |

The maximum-coverage estimator uses training-only overlapping 25% acceptance intervals; review corrected an initial restriction to observed target values. No held-out target selects a fitted representative.

The [Django transfer check](../cpu_transfer/REPORT.md) applies unchanged Astropy fits to one fixed ordinal Django instance with 774,506 positive-duration operations. The existing path model covers 43.20–44.13% across the four fits, averaging 43.60%. This extends the diagnostic beyond Astropy, but repeated scoring of one trace is still one test instance.

The [entry-flag experiment](../cpu_entry_flags/README.md) found a more useful mechanism-specific candidate using fields already retained. For openat alone, flags plus generic lexical path categories improve Astropy grouped coverage from **30.33% to 46.27%**, and Django mean coverage from **33.97% to 39.14%**. Worst Astropy openat error rises from 2,295.69% to 3,107.36%; Django worst error falls from 706.65% to 339.36%. Preserve this as a development candidate with serialized fits, not a validated replacement for all CPU events.

The flags model requires 20 training events from at least two instances per group. It uses no return values, outcomes, completed durations or individual-case rules as predictors. Other retained mmap/offset descriptors were identified but not exhaustively fitted in this bounded pass. Unknown/opaque descriptors and unmeasured cache/queue state are not invented.

## GPU sequence hypothesis

The [first-request comparison](../native_sequence/README.md) tests a request-start-known indicator using the same 2,080 native requests and five grouped folds. Token coverage falls from 94.8558% to 94.6635%. The conditional cache variant gains one passing request (97.4519% to 97.5000%) but worsens equal-instance coverage and worst error. Do not replace the existing GPU candidates or launch extra repetitions solely for this hypothesis.

## E2E reconstruction and composition

The initial normalized-table review incorrectly inferred a missing acquisition join. Root inspected the raw journals and independently proved **1,784/1,784 exact client/proxy/native joins across the 43 eligible cases**, using parent event IDs, logical request IDs and client span IDs. [The proof](raw_request_join_proof.json) records hashes and identities. The normalized adapter's missing columns and `model_eligible=false` classification do not establish absent raw evidence.

There is a real boundary nuance: 75 proxy spans end after their linked client call, by 0.051–145.609 ms. In the frozen proxy source, the response is written and flushed before serving-metrics finalization and the proxy end timestamp. Consequently, proxy finalization need not lie on the caller's synchronous critical path. The links are complete; treating the spans as strictly nested would be wrong. Remote native timestamps must not be unioned directly with local CPU timestamps.

Naively adding nested lifecycle/command spans, proxy spans and native request durations is invalid. Exact observed interval accounting is supported, but it is not itself a predictive E2E model. A conditional dependency graph and correctly scoped component targets still need to be implemented and evaluated before claiming a composition improvement. This pass does not manufacture a composed E2E score or put measured residual into a predictor, and it does not establish an acquisition defect requiring reruns.

## Additional-sample decision

For one fixed feature group, a point prediction can cover two positive observed durations within 25% only if their ratio is at most 5/3. The retained feature groups show much larger timing spreads. More observations cannot make a single group median satisfy every such event; better justified mechanism/state inputs or an explicitly limited accuracy claim are needed. This statement concerns the tested feature groups, not an impossibility result for every future model.

Continue CPU candidate checks using retained instances before buying more H100 time. The earlier optional 8–12-run same-H100 proposal is not automatically justified by these findings. Distinct-hardware validation remains a separate genuine data requirement: another H100 boot does not establish GPU hardware scaling, and changing a GPU does not validate workload-CPU transfer. Untouched validation stays sealed until the candidate and evaluation contract are frozen.
