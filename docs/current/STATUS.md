# Project status — September 16, 2026

The [PDF contract](PDF_CONTRACT.md) defines the required work. This status is
based on locally retained evidence verified today. Remote main was checked
before publication; live GPU health was not checked. Machine-readable details: [STATUS.json](STATUS.json).

## What we have

| Deliverable | Evidence and remaining work |
|---|---|
| D1 | Historical full-suite results: Lite 100/300 resolved (33.33%), mean E2E 160.96 s; Verified 198/500 (39.60%), 147.33 s. Matched public-scoreboard reproduction is not established. |
| D2–D3 | Category/tradeoff figure drafts and exact outcome joins exist. Final grouping, timing-boundary descriptions and subset disclosures need consolidation. |
| D4–D6 | Four historical hyperparameter sweeps and accuracy–latency summaries exist. Required CPU/GPU-ratio sweep panels are still missing from the current packet. |
| D7–D8 | Repaired event evidence, detailed examples and reconstruction outputs exist. The final high-ratio example, figures and event-based explanation are not yet one submission packet. |
| D9 | Reference-platform event models and a runnable composition diagnostic exist. Literal accuracy, hardware transfer and complete figure integration have not passed. |

The retained repaired inventory has **55 accepted executions**: 49 training,
4 protected final-evaluation, and 2 confirmation executions excluded from fitting.
CPU/lifecycle development uses 43 eligible cases; native development uses 49.
These are distinct from historical full-suite/sweep runs and the September 6
602/486 snapshot. They are not evidence that 1,088 new runs are required by the PDF.

## Latest D9 result

The [component-composition implementation](composition/README.md) evaluates
15,766 original targets and 43 explicit startup/setup accounting envelopes.
It preserves individual targets while preventing double-counting of inclusive
parent durations. Startup and setup overlap in the saved traces; the joint
accounting envelope handles that overlap without using measured test residuals.

- Component sum within25 of outer E2E: **31/43 cases**; worst error **41.93%**.
- Every modeled event plus E2E within25: **0/43 cases**.
- One rare interrupt event has no out-of-instance training support and remains
  explicitly unpredicted.
- Median measured unaccounted time is **6.08% of E2E**; it is a diagnostic, not
  a correction supplied to the predictor.
- Earlier direct aggregate E2E regression: 42/43 within25. This is a different
  model and does not establish component accuracy or correct composition.
- Hardware transfer is unvalidated. Current reference-domain fits are not a
  demonstrated hardware-parametric solution.

Today all saved composition artifact hashes matched, all **43/43** saved graphs
replayed exactly without raw journals, and **29 focused tests passed**. These
checks establish software/evidence consistency, not a new accuracy result.

## Repository cleanup and publication state

Five old root handoffs now point here; original text is preserved under
`docs/history/root-handoffs/`. Removed the empty stray `???` file and 645
disposable cache files, about 15.7 MB. Details: [cleanup log](CLEANUP_20260916.json).
Raw evidence, `SNAP`, result archives, experimental code and worktrees remain intact.

The canonical checkout is now:

```
/home/riverahernandezjason/agentic-results-publish-20260909
branch: main
```

The September 16 publication imports the reviewed development source and saved
analysis into `main`, preserving existing published result archives. The old
`agentic-submission-repairs-20260908` checkout is the retained import source;
use this `main` checkout for continued work. Conflicting older result snapshots
are preserved under `results/development-snapshot-20260916/`, not overwritten
onto published archives. See [publication manifest](PUBLICATION_20260916.json).
Git history records the publication commit; the import manifest records its parent.

## Remaining work in priority order

1. Finish D1–D8 as a coherent report/figure packet; recover missing sweep panels
   only where exact retained evidence supports them.
2. Identify and model supported execution overhead; improve complete event/E2E
   composition without measured residual correction.
3. Connect meaningful hardware parameters to the supported models and validate
   their sensitivities. Metadata-only hardware profiles are insufficient.
4. Complete simulator-driven figure generation, freeze the candidate, and then
   evaluate the protected final cases. Report failures and missing predictions.
5. Keep the final report, simulator and evidence manifest reproducible on `main`.

The project has useful, reproducible results but is **not submission-complete**.
Current failures do not prove that D9 is impossible; neither do they justify
promising a pass or launching another full GPU matrix without a specific need.
