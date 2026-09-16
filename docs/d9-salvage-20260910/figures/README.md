# Bounded D9 figure packet

Run the renderer from the repository root:

```bash
python3 docs/d9-salvage-20260910/figures/build_figures.py
```

It reads only the retained `train_calibration` view, saved grouped out-of-fold predictions, the retained matched sweep summary, and the repaired native request artifacts. It writes SVG figures and PNG copies when `rsvg-convert` is installed, plus compact CSV/JSON inputs, `REPORT.md`, `provenance.json`, and `figure_manifest.json`.

The historical timing cohort has 819 runs. D2 and the separate D9 coverage diagnostic use all 819. D3 official resolution uses 615 exact `run_id` joins to the retained historical figure input; 204 train-view runs have no exact retained outcome row and are omitted from the D3 accuracy cohort. The packaged `d3_outcome_join.csv` contains only those 615 admitted rows. It has no instance-level fallback, and `d3_outcome_join_manifest.json` records the source, train-view, scope, and join hashes.

D3’s first two panels use unchanged retained `official_resolved` values. The third panel and D2 use measured tool/model proxy walls; those are not native GPU-kernel timing. The D9 diagnostic’s within-25% coverage is model coverage, not task accuracy. Step 2 shows resolved-rate/E2E evidence and marks missing event-level ratio views unavailable. Step 3 keeps outer E2E, tool wall, completed-request proxy, component sums, and direct E2E predictions as separate boundaries; no residual or overlapping phase sum is plotted.

The native figure is direct `native:e2e` request OOF only. Queue/prefill/decode rows remain diagnostics, cache points use the supplied realized cache trace, and cross-hardware transfer is unvalidated. These artifacts support simulator review and do not establish a literal D9 acceptance result.

Focused checks:

```bash
python3 -m unittest docs/d9-salvage-20260910/figures/test_build_figures.py -v
```
