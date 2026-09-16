# D9 improvement history recovered from Codex transcripts

Reviewed September 13, 2026. Searched 126 retained session JSONL files (about
1.4 GiB), extracted 388 readable assistant messages matching D9/modeling terms,
then read the relevant September 8–10 discussions and linked experiment reports.
Some old records omit channel metadata; the extracted messages include progress
updates as well as final answers. This is a retrospective review, not a rerun or
an independent reproduction of every historical score. No protected validation
labels, new inference, or acquisition changes were used.

## Completed work, with boundaries preserved

| Date / approach | What was actually done | Result / decision |
|---|---|---|
| Sept 8: semantic CPU model | Full-command shell parsing; runner/module, find-exec, pipeline, pager, import and operand descriptors; support-aware backoff; repository-aware and repository-free ablations | On 30,711 tool-duration events / 1,083 trajectories, instance-grouped coverage increased 72.77% to 77.21%. Traversal 63.03% to 79.14%; tests 46.27% to 66.44%. Accepted semantic median. These are command durations, not syscall durations. |
| Sept 8: estimator choice | Median versus interval-coverage-optimal center | Gate center reached 77.51%, but its +0.30 point advantage was uncertain and worsened mean/event-sum error. Median selected. |
| Sept 8: transfer and serving parity | Repository-held-out comparison, full model reload and calibration/serving comparison | Unseen-repository coverage 66.63% to 67.59%; zero prediction mismatches across 30,711 tool events. Transfer remained weak. Initial baseline feature-route discrepancy was numerically negligible, not the main error cause. |
| Sept 8: E2E boundary analysis | Compare measured event sums, predicted event sums, additive overhead fit and legacy direct regression | About 55% of outer agent wall was outside old event boundaries. Semantic-model overhead-aware E2E coverage 59.37%. Only 1/1,083 trajectories passed the coverage-aware event-plus-E2E conjunction. |
| Sept 8–9: acquisition repair | Lifecycle spans, semantic-action/runtime split, physical retry identities, native phases, raw CPU operations, script-state capture and offline reconstruction | Later fixture reconstructed 846,533 CPU operations and 40 exact native requests; explicit unknown wall 4.77% on that fixture. This did not retroactively repair missing historical events or validate a latency model. |
| Sept 9: mechanism backoff | Fixed mechanism-to-operation-to-class hierarchy on a filtered training cohort | 23,245 tool events / 545 instances: 67.74% to 72.39% coverage, but worst error 481% to 4,182%. Not promoted. |
| Sept 9: nested model selection | Inner/outer instance-grouped comparisons, class-specific estimators, error-tail constraint | Selected hybrid 68.51% coverage with worst error unchanged at 481.08%. Unconstrained candidate reached 74.19% but worst error 20,437%; rejected. Different cohort/baseline from Sept 8, not evidence that 77.21% regressed to 68.51%. |
| Sept 9: prospective GPU and E2E | Models limited to supported start-known descriptors | Prompt-length/cap GPU grouping 34.63% to 36.36%, worse tail, rejected. Repeated prompt/cap keys had incompatible target intervals. Start-known direct E2E baseline 65.69%. |
| Sept 9: uncertainty and adapters | Cluster bootstrap, equal-instance metrics, strict ledger and calibration pipeline | Hybrid CPU gain +0.76 points; conditional interval +0.45 to +1.13. At that checkpoint repaired samples were confirmation-only, so fitting was legitimately pending. That data-availability statement became stale when later training runs arrived. |
| Sept 10: conditional token models | Explicit completed-workload token inputs; relative-error NNLS | Historical proxy requests 96.69%; historical direct E2E 79.00% on 819 runs. These are conditional workload results, not prospective forecasts. |
| Sept 10: native GPU phases | Native request identities and hardware domain, scaled token and cache models, grouped folds | Native request E2E 94.86% token-only / 97.45% cache supplied, on 2,080 requests. Initial log-token design reached 32.50% and was rejected. Token-only queue/prefill/decode coverage: 39.66% / 35.58% / 98.89%; request E2E does not establish every phase's accuracy. |
| Sept 10: atomic CPU models | Decode four distinct Astropy training cases; compare operation, requested size, path, combinations and robust representative | 974,169 positive syscall targets: 29.11% pooled, 39.70% operation, 43.02% size, 46.32% path. Combined size/path 46.77% worsened other errors. Robust representative reduced worst error but coverage fell to 45.43%. |
| Sept 10: CPU transfer and openat flags | Unchanged Astropy fits scored on one Django instance; fixed entry flags and generic path groups | Path model averages 43.60% on Django. Openat-only flags/path improved Astropy 30.33% to 46.27% and Django 33.97% to 39.14%; not an all-CPU score. |
| Sept 10: first GPU request | Start-ordinal indicator added to existing token/cache models | Token model worsened; cache model gained one passing request but worsened equal-instance coverage and worst error. Rejected. |
| Latest v4: E2E fit | Exact raw client/proxy/native identity recovery; grouped aggregate CPU/native/remainder and direct fits | 1,784/1,784 joins available. Direct and calibrated composition each 42/43 within25; worst 28.96% / 31.17%. Action-class refinement and training-only scale did not remove last miss. Arithmetic remainder is not an identified transferable mechanism. |

## What “missing information” actually referred to

1. **Real historical omissions:** declared work volumes, mutable script state,
   lifecycle boundaries, and incomplete physical-request coverage. The Sept 8
   audit found 340 failed proxy requests across 80 calibration runs absent from
   the completed-event cache. Collecting these later does not fill old records.
2. **Raw evidence omitted by an adapter:** the later normalized tables dropped
   request parent/client IDs. Raw journals recovered all 1,784 joins. This was
   an offline extraction problem, not a reason for more GPU executions.
3. **Captured information not yet used by a tested model:** script snapshots,
   richer operation entry arguments, and joined state/work descriptors. Their
   presence does not prove useful predictive coverage, but earlier experiments
   do not establish that they have been exhausted.
4. **Information genuinely unavailable at prediction time:** future output
   tokens, completed syscall returns/work counts, and future runtime state.
   These may be explicit supplied-workload descriptors for a conditional replay,
   but cannot silently enter a prospective forecast.

## Highest-value unfinished continuation

The strongest old recommendation was **state/work-aware CPU modeling**, not
another median search. The Sept 8 systems review recovered apparent script
bodies for 4,513/4,789 Python script calls from earlier edits, but explicitly
warned that arbitrary shell writes could invalidate them. It did not fit this
stateful extension. Later script snapshots/invalidation machinery were added;
no completed evaluated model using those snapshots was found in the inspected
CPU model paths or experiment reports.

The Sept 9 ranking attributes 33.14% of historical selected-model absolute
error to Python scripts, 23.41% to pytest mechanisms, and 13.06% to unpiped
find-exec. These are historical command-target priorities, distinct from the
syscall miss-count ranking. The next bounded implementation should first prove
coverage and timing availability of script/action/work joins, then compare one
small state-aware model against the existing semantic baseline on identical
instance-grouped data. Do not key predictions by script hashes or case identity
as a substitute for generalizable work descriptors.

Other unexhausted paths are retained mmap protection/flags and positional I/O
offsets, plus lifecycle/native phase models with explicit dependency and clock
contracts. The small atomic experiments do not establish a ceiling across the
remaining retained training instances. Cross-hardware empirical validation is
still a separate limitation; one hardware domain cannot validate its own
transfer law.

Do not repeat completed parser fixes, command-lookup experiments, center-only
tuning, the rejected first-request GPU indicator, or the resolved raw request
identity investigation. No new full H100 matrix is justified by this history.

## Primary transcript and artifact anchors

- [Sept 8 root diagnosis and final model decision](/home/riverahernandezjason/.codex/sessions/2026/09/08/rollout-2026-09-08T03-58-42-01a07f2b-09dc-74b0-8bd9-69e9824cdfa1.jsonl:1433), earlier diagnoses at lines 85–313.
- [Sept 8 calibration completion](/home/riverahernandezjason/.codex/sessions/2026/09/08/rollout-2026-09-08T04-10-16-01a07f35-a275-72c1-a431-729d368eb697.jsonl:1006).
- [Sept 8 serving-parity completion](/home/riverahernandezjason/.codex/sessions/2026/09/08/rollout-2026-09-08T04-12-31-01a07f37-b165-7311-a23e-f58ca31c07a5.jsonl:1043).
- [Original systems analysis](D9_CPU_SYSTEMS_REVIEW.md) and [full numerical report](/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T060000Z/d9-cpu-review/D9_CPU_REVIEW_RESULTS.md).
- [Sept 9 opportunity experiment](/home/riverahernandezjason/.codex/sessions/2026/09/09/rollout-2026-09-09T14-55-24-01a086aa-a1b4-7b40-bbdc-a6ee6c1ba1bb.jsonl:380) and [saved report](retained-analysis-20260909/d9/report.md).
- [Sept 9 selected offline models](offline-deliverables-20260909/REPORT.md); [measurement closure](measurement-review-20260909/REPORT.md).
- [Sept 10 atomic/transfer and raw-join results](d9-salvage-20260910/model_opportunities/REPORT.md); [native phase comparison](d9-salvage-20260910/native/README.md); [latest E2E result](d9-salvage-20260910/e2e_composition/REPORT.md).

The 77%, 68%, 46%, and 97% figures above are different populations, target
boundaries, and input contracts. They must never be presented as a single model
accuracy progression or combined into a D9 compliance score.
