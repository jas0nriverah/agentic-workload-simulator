# D9 simulator: retained-evidence completion pass

The offline simulator now has stronger conditional models, repaired hardware-domain calibration inputs, and a runnable prediction/scoring interface. **The literal D9 requirement is still not met.** No acquisition implementation was changed and no new inference was used.

## Selected results and boundaries

| Target | Evidence | Within 25% | Worst error | Interpretation |
|---|---|---:|---:|---|
| Historical CPU tool wall | 23,245 events; 545 instances | 68.51% | 481.08% | Retained hybrid control; not atomic CPU operations |
| Historical request-proxy wall | 23,868 requests; 545 instances | 96.69% | 97.30% | Conditional on supplied token counts; proxy boundary |
| Historical outer E2E | 819 runs; 545 instances | 79.00% | 95.00% | Conditional direct E2E; not lifecycle composition |
| Repaired native GPU request E2E | 2,080 requests; 25 instances | 94.86% | 96.68% | Supplied prompt/output token workload |
| Repaired native GPU request E2E, cache trace supplied | Same 2,080 requests | 97.45% | 96.77% | Additional declared cache-work descriptor; not a cache forecast |

The GPU conditional comparison improves over the feature-free 34.63% baseline. It does **not** establish an improvement over the older token-aware v3 simulator, which was not refit in this comparison. The E2E candidate improves over the 65.69% median baseline, with a positive gain in all five instance-grouped folds. These are development comparisons; historical access and model-selection limitations remain disclosed.

The [historical joint check](historical_joint_report.json) joins run, instance and fold identities: 4/819 runs pass all retained CPU tool events, 547/819 pass every GPU proxy request, 647/819 pass E2E, and only **2/819 pass their conjunction**. This is still an incomplete PDF event population: individual atomic CPU operations are absent from that historical tool-wall score. The 96.69% GPU figure must not be presented as overall D9 compliance.

## What the repaired evidence adds

The [evidence bundle](evidence/EVIDENCE_SUMMARY.md) supplies **49 eligible executions / 25 instances / 2,080 physical GPU requests / 8,320 phase rows**, bound to one verified H100 inventory domain. Four final-evaluation cases and two confirmation cases are excluded from fitting. The new cases cover Astropy and Django only.

Raw CPU size/offset/loss metadata was checked for all 49 cases, with one full raw-byte reconstruction. This pass did not rehash every large CPU binary. Forty-three cases have fully valid original ledgers; six have wrapper-observation retry predecessor errors. Independent review verified that their native requests remain complete and exact-joined. They are accepted only for the narrower native modeling scope, not silently declared valid for every analysis.

The [CPU/lifecycle calibration](cpu_lifecycle/README.md) uses only the 43 fully valid cases / 22 instances. It corrects the offline fit-domain grouping: local CPU targets bind to the saved local CPU inventory and matching host/boot/clock, rather than being split by remote GPU profile hashes. This exposes 13 supported command/lifecycle model paths and one rare unsupported control event. Coarse class medians still perform poorly for semantic actions and client processing, so these diagnostics do not replace the historical CPU hybrid.

## Native model decision

The [native comparison](native/README.md) preserves all 2,080 requests with zero missing predictions and five instance-grouped folds. Use the scaled-linear token model when only prompt/output workload descriptors are supplied. Its p95 error is 25.52%, and 7/49 executions pass every native request. The optional cache-trace model reaches p95 15.15%, with 19/49 executions passing every native request; it requires a supplied cache-work trace and is not a prospective cache model. These are GPU-request gates, not full agent-E2E gates.

The 49 executions are not 49 independent samples: two instances have 13 executions each. [Equal-instance diagnostics](native_cluster_diagnostics.json) give 94.86% token-only and 97.86% cache-trace coverage. Only 1/25 and 9/25 instance clusters, respectively, pass every native request across all their executions. Fixed-prediction bootstrap intervals are descriptive development uncertainty, not independent confirmation.

Review rejected an initial log-token/linear-latency design because it could not represent proportional decoding work. The corrected linear design and the rejected diagnostic are retained. This adaptation occurred during development and does not create an untouched holdout claim.

The [request-position diagnostic](native_error_positions.json) finds 24/49 first requests and 83/2,031 later requests miss the token model's 25% threshold. First requests are disproportionately difficult, but later misses remain; position alone does not establish a cold-start cause. No position-specific model or case-specific rule was fitted.

## Runnable simulator and review

See [simulator commands](simulator/README.md) for CPU, request-proxy and conditional E2E prediction, optional existing hardware-parameterized v3 predictions, and strict scoring. Inputs and labels are separate; unsupported/missing predictions stay in denominators; zero targets are treated exactly; scoped identities and overlapping boundaries are checked. The default hardware values are explicitly legacy assumptions, not newly measured specifications.

The [independent review](simulator/REVIEW.md) checks grouped folds, training-only weights, source hashes, CPU/GPU host attribution, scope-limited retries and hardware grouping. The [E2E error figure](e2e/e2e_error_cdf.svg) is generated from saved out-of-fold predictions.

The follow-up [figure packet](figures/REPORT.md) adds 11 SVG/PNG figure pairs and explicit derived tables. D2 retains all 819 timing trajectories. D3 uses only 615 exact run-ID outcome matches (236 resolved), excluding 204 unmatched runs; this mixed development cohort is not a replacement for the full-suite D1 headline. Resolved labels remain observed and unchanged by latency simulation. The four sweep dimensions retain their matched accuracy–latency results, with unsupported ratio panels identified. Historical Step 3 command/request examples remain clearly separated from native GPU and atomic CPU evidence. Root checked the outcome/aggregate totals and all 22 figure hashes, and visually reviewed the corrected D3 axes.

## Remaining D9 gaps

The follow-up [individual CPU-operation experiment](atomic_cpu/REPORT.md) decodes 390,973,200 retained binary bytes from four fixed, distinct training instances, with leave-one-instance-out comparisons. Among 974,169 positive-duration operations, pooled medians cover 29.11% within 25%; operation medians cover 39.70%, entry-requested-size stratification 43.02%, and entry-path stratification 46.32%. This establishes a real operation-level reconstruction and a useful mechanism distinction, but the best candidate still has worst error above 4,000%. All four instances are Astropy; these are bounded development diagnostics, not a representative final evaluation or a D9 pass. Zero-duration record semantics and the complete population accounting are reported separately in the experiment.

- CPU operation-level generalization and the strict all-events condition remain unproven. The bounded operation experiment fails the 25% gate; command wall is not a substitute for an individual read/write/traversal operation model.
- Conditional workload simulation assumes the event/token workload is supplied. It does not forecast future agent actions, output lengths or cache state.
- Native GPU request E2E and outer agent E2E are different targets. Overlapping CPU/runtime/lifecycle spans and GPU phases cannot be indiscriminately summed.
- Hardware knobs and sensitivity calculations do not establish cross-platform accuracy. Independent hardware validation remains necessary.

The [bounded sample plan](next_samples/README.md) separates adaptive calibration from untouched transfer evaluation. It proposes 8–12 optional same-H100 calibration traces, 8 paired second-GPU-domain training traces, and 4–8 protected transfer traces, with a combined hard cap of 28. These are proposed diagnostics, not a promise to close D9: targeted error cases are not representative validation, and a different GPU does not validate workload-CPU hardware scaling. The plan's 34 instance references were checked against the pinned split hash without opening protected outcomes. No additional run is launched by this package.
