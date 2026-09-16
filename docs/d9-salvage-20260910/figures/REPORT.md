# Bounded D9 Steps 1–3 figure packet

This packet renders saved train-calibration and out-of-fold artifacts. It does not fit a model, open excluded evaluation labels, or regenerate a broad raw dataset.

## Scope and numerical checks

- Historical proxy view: 819 retained train-calibration trajectories and 12 retained category groups.
- D2 ratio: one dot per retained run, summed recorded tool-event wall divided by observed completed model-request proxy wall. It is not a CPU/GPU hardware ratio.
- D3 accuracy view: 615/819 trajectories have exact run-ID matches to the retained outcome source; official resolved labels are plotted only for that subset. The 204 unmatched runs are excluded, with no instance-level fallback.
- D3 category resolution: 236/615 exact-matched runs resolved (38.374%). The latency simulation does not modify these labels.
- Historical direct E2E values in the joint OOF table exactly cross-check against the retained train-view observed E2E field for all 819 run IDs.
- Step 2: 12 retained matched-pair rows, three settings for each of call limit, max output tokens, observation length, and temperature; every row has 18 pairs across 17 instance clusters.
- Step 3 selected run: `matplotlib__matplotlib-13989` with observed tool/model proxy ratio `15.3048`; `7` CPU tool rows and `8` model-request rows were joined to saved OOF predictions.
- Native direct request OOF: token candidate `94.855769%` within 25% and cache-trace candidate `97.451923%`; both recomputed metrics match `native/report.json`.

## Exact source hash anchors

- `training_manifest`: `1afd9afb8b552f84242f7e6b8b6e342f9b6f8ded5cd45a1d040b6bcf3f263341`
- `trajectories`: `803ad4554b36695704a2a96080404044c553c9bc8d46fd668a3942fc69a3e63a`
- `historical_joint_predictions`: `026ed8bd769caa7079714291cfc4798c5e27c0c4a7bbdc051aac61ba691717a6`
- `cpu_oof_predictions`: `00ea9ea334507d96cd358c45144f7bd4b2eb4dd0bcb5e914e22009959b931d74`
- `gpu_proxy_oof_predictions`: `248929129de0fcd5be56bca251e2be375d80247d691f056e83985b3f51cea683`
- `sweep_summary`: `86aec80801323b843fe81225c1a9938c9efa2a1fa1301c24387475cce17dc2db`
- `native_predictions`: `6d1f57a158fb945f26307bf4078bf1878bd88da9b985f96768e91c08024ec98b`
- `native_report`: `e572bd74d340fd64e0d4ef7f583a67624501825e93c1dff1ba1ff7921920ca7d`
- `native_fit_artifact`: `1398a24df8f5e2e8581c8c7f1f8b6fa402594058933a1c0d8337fe19aa736ebe`
- `historical_outcome_source`: `/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/figures-input/trajectories.csv`; `569d9bf3c92f0165db2c06180fe34f7435d6d62b9e6e07cd4e227ef5888946ff`
- `d3_outcome_join`: `595403fceed319ea6032b00f0b6290359e972fe967e08d327f8bfd07da4d8fdb`
- `d3_scope_manifest_list`: `7f7a48f840eda37bf402c181f0f8859126c4ea12240ee665d47d08fd0fd34600`

## Figure inventory

- `d2_cpu_gpu_proxy_ratio_by_repository.png`
- `d2_cpu_gpu_proxy_ratio_by_repository.svg`
- `d3_category_tradeoffs.png`
- `d3_category_tradeoffs.svg`
- `d9_latency_model_coverage_diagnostic.png`
- `d9_latency_model_coverage_diagnostic.svg`
- `native_request_oof_quality.png`
- `native_request_oof_quality.svg`
- `step2_call_limit.png`
- `step2_call_limit.svg`
- `step2_hyperparameter_tradeoffs.png`
- `step2_hyperparameter_tradeoffs.svg`
- `step2_max_output_tokens.png`
- `step2_max_output_tokens.svg`
- `step2_observation_length.png`
- `step2_observation_length.svg`
- `step2_temperature.png`
- `step2_temperature.svg`
- `step3_high_ratio_breakdown.png`
- `step3_high_ratio_breakdown.svg`
- `step3_high_ratio_events.png`
- `step3_high_ratio_events.svg`

## Exact PDF plot gaps

- Step 1/D2 is supported only at the historical tool/model proxy boundary. The saved train view has no native GPU-kernel timing or independent category label, so repository is shown as the category-like grouping.
- Step 1/D3 is supported on the exact run-ID outcome join (615 plotted runs; 204 train-view runs omitted because no exact outcome row is retained). The first two panels use official resolved rate; the third uses the historical tool/model proxy boundary. The separate D9 diagnostic remains model coverage, not task accuracy.
- Step 2/D4 and D5 have resolved-rate versus average E2E points from the retained matched-pair summary. The PDF's CPU/GPU-latency panels are explicit unavailable panels because the retained sweep summary does not contain matched tool/model event walls; no ratio is fabricated.
- Step 3/D7 and D8 have a complete historical proxy event log for one high-ratio trajectory, including operation classes and input/output/context descriptors. Native queue/prefill/decode and atomic CPU operation rows are not silently substituted into that breakdown.
- No figure adds a residual to an E2E prediction or sums native E2E with native phase rows. Tool wall, model proxy wall, and outer E2E are shown as separate boundaries because their interval disjointness is not proven.
- Accuracy labels are copied from the identity-gated retained source and are never recomputed from, or changed by, latency simulation.

The packet is evidence for bounded simulator review, not a literal D9 acceptance result. Cross-hardware transfer and the all-event within-25% gate remain unproven.
