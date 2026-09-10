# D9 offline simulator results — 2026-09-10

[Download the runnable bundle](d9-offline-20260910.tar.gz) · [Report](REPORT.md) · [Validation receipt](bundle_receipt.json)

The bundle contains source, coefficients, grouped predictions, compact retained evidence, reconstruction provenance, and tests. Extract it and start with `D9_BUNDLE_README.md`.

- Historical conditional GPU proxy: 96.69% of 23,868 requests within 25%.
- Repaired native GPU request E2E: 94.86% of 2,080 requests within 25%; 97.45% when a cache-work trace is explicitly supplied.
- Historical conditional outer E2E: 79.00% of 819 runs within 25%, up from 65.69% for the median baseline.
- All repetitions of an instance remain together in validation. These are development comparisons, not untouched final-holdout results.

**Literal D9 is not passed.** CPU operation-level accuracy, every-event acceptance and cross-hardware transfer remain unvalidated. Conditional token/cache descriptors are supplied workload inputs, not forecasts of future agent behavior. Native request E2E is distinct from outer agent E2E. Full Steps 1–3 plotting with the newly selected candidate has not been revalidated by this bounded pass.

Validation: 29 focused tests passed. A separate extraction verified all 331 packaged source/result hashes, four prediction smokes, and 17 simulator integration tests. Large CPU raw binaries remain in the retained archives rather than this compact bundle. No new inference or frozen acquisition change was made.

Archive SHA-256: `0032d0eedb4d51d45df08c01732b30c3de3ca66f1b40a2cb919aba34d66428bf`
