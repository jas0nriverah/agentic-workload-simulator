# Bounded offline follow-up review

The individual CPU-operation experiment is accepted as a development diagnostic, not as D9 compliance. Main-agent review and an independent Luna review checked the final helper, fit artifact, report and tests. The independent review found no concrete correctness objection.

- Four distinct, fully valid training instances were selected by ordinal, without ranking outcomes or errors. All four are Astropy, limiting generalization.
- All four binary hashes match their recorded hashes. The 977,433 decoded records have complete action ranges, token membership and record counts, with zero recorded perf/map loss or callback errors.
- The 974,169 positive-duration operations remain modeling targets, including failed syscalls. The 3,264 instantaneous fork/thread lineage records are explicitly separated. No zero-duration operation, negative target or censored target occurs in this sample.
- Each fold excludes its held-out instance. Predictor keys use entry syscall identity, requested-size buckets or entry path classes, never return values, observed duration or result status.
- Seven focused tests pass, including held-out-duration independence and invalid range/token rejection. The saved artifact's script hash matches the final helper.
- The best fixed comparison covers 46.320197% of operations within 25%, with worst error 4,152.24%. This is insufficient for the PDF's all-events criterion. No model is promoted to a validated production CPU predictor on this evidence.

The optional sample plan passes its identity and pinned split-hash checks for 34 references. Root review required separating adaptive error-targeted calibration from protected transfer evaluation, and GPU transfer from workload-CPU transfer. No runs were launched and no acquisition changes were made.

Figure review rejected substituting latency-model coverage for task resolved rate, clipping high CPU/model ratios, inferring event disjointness from aggregate sums, and transferring outcomes between runs merely because their instance IDs match. Final figures must preserve exact identity joins and disclose unsupported panels and proxy timing boundaries.
