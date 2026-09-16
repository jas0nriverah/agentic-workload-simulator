# Native repaired D9 comparison

Run the bounded comparison from the repository root with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 docs/d9-salvage-20260910/native/run_native_comparison.py \
  --output-dir docs/d9-salvage-20260910/native
```

The script verifies the evidence manifest and dataset hashes before reading the
8,320 native rows, groups them into all 2,080 physical requests, and keeps the
25 instance IDs intact across five folds. It reuses the dependency-free NNLS
solver in `docs/d9-salvage-20260910/e2e/compare.py`. The only fit candidates
are:

1. `hardware_domain_median`;
2. `relative_nnls_token`, using the scaled linear design
   `[1, prompt_tokens/1000, completion_tokens/1000]`;
3. `relative_nnls_token_cache`, using uncached prompt work and a
   prompt-completion interaction. Its prefill design is
   `[1, uncached_prompt/1000, prompt/1000]` and its decode design is
   `[1, completion/1000, prompt*completion/1e6]`.

The primary target is direct native e2e. Queue, prefill, and decode are separate
diagnostics and are never summed into e2e. Token and cache candidates are
conditional on a supplied, already realized workload trace. Completion tokens
and cached tokens are therefore not prospective request inputs. No measured
latency, phase duration, residual, status, or outcome is a feature.

All 2,080 requests receive predictions for every candidate and phase; the
missing prediction report is empty. The primary out-of-fold results are:

| Candidate | E2E within 25% | E2E p95 APE | E2E worst APE | Runs with every request within 25% |
|---|---:|---:|---:|---:|
| Hardware-domain median | 21.06% | 161.64% | 495.74% | 0/49 |
| Relative NNLS token | **94.86%** | **25.52%** | **96.68%** | 7/49 |
| Relative NNLS token + cache trace | **97.45%** | **15.15%** | **96.77%** | 19/49 |

The cache candidate is explicitly conditional on the finished-request cache
trace. It improves the direct e2e development score, while its phase-specific
cache terms remain a conditional replay result. Diagnostic within-25% coverage
for the token candidate is 39.66% queue, 35.58% prefill, and 98.89% decode;
the cache candidate is 42.02%, 83.70%, and 98.94% respectively. The strict
request-level conjunction across all four phases passes for 733 of 2,080
requests with the cache candidate; this is a descriptive development result,
not a D9 acceptance claim.

The initial log1p token diagnostic is retained in
`rejected_log_diagnostic.json`. It reached only 32.50% direct e2e within 25%
and was rejected after design review because the transform underfit the nearly
linear completion-token workload. The scaled linear correction is disclosed
here rather than presented as an untouched model selection.

`predictions.jsonl` retains one compact row per physical request. Each row has
observed and predicted values for all phases/candidates, token descriptors,
fold, hardware domain, and any missing prediction reasons. `fit_artifact.json`
contains full-training conditional models for an optional simulator adapter.
`missing_predictions.json` is the explicit coverage ledger. `report.json`
contains fold support, primary/diagnostic metrics, nearest-rank p95, worst error,
per-case all-request pass counts, and limitations.

This comparison uses one static H100 inventory domain. Cross-hardware accuracy is
unvalidated. It uses the repaired `train_calibration` population in grouped
development folds, not a sealed evaluator holdout, and does not close the
literal universal D9 all-event gate.
