# Retained D1–D8 figure evidence

This directory is a bounded offline evidence packet. It does not alter frozen acquisition or implementation, run an experiment, fit a model, mine held-out case labels, or select a new D8 example.

`build_figures.py` applies the frozen historical identity gate to sweep metadata before using matching outcome and wall-time cells. The mixed sweep CSV is structurally parsed to locate run IDs, but only eligible rows have outcome/wall cells decoded numerically or used. The D4 outputs contain redacted pair IDs, not case labels. D1 uses the already-published full-suite aggregate only.

The four representative figures are:

- `d1_historical_headlines.{svg,png}` — published Lite and Verified headline arithmetic.
- `d4_paired_sweep_e2e.{svg,png}` — 18 eligible matched pairs across 17 independent instance clusters per setting. Baselines are repeated only inside their own parameter panel; the 72 coordinate copies are never pooled as independent observations.
- `d8_current_command_boundary.{svg,png}` — corrected distinction between 40 semantic actions (18.491612 s), 60 runtime commands (14.142780 s), their 32.634391 s tool-execution union, and native service phases.
- `d8_current_cache_aware_phases.{svg,png}` — 40 physical requests with fresh-token prefill and generated-token decode plots.

`d2_d7_train_repository_summary.csv` and `d2_d7_train_category_summary.csv` are descriptive tables from the pre-built 819-run, 545-instance train-calibration view. They are incomplete historical proxy summaries, not production or whole-baseline estimates. `ratio_definitions_and_populations.csv` is the canonical ratio guardrail. No exported ratio is a CPU/GPU ratio: historical values are tool/model proxies, and current native values are native engine service walls rather than GPU kernel time. `provenance.json` records absolute source paths, hashes, units, population definitions, denominators, baseline handling, and the no-overlap rule.

The D8 retained confirmation fixture is descriptive only and excluded from fitting. It improves explanation for retained evidence only, is not a replacement for the previously selected D8 example, and makes no claim about other cases, platforms, or a full production result.
