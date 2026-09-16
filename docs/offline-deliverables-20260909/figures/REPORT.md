# Corrected retained figure inputs

This packet provides bounded offline inputs for D1–D8. It does not change frozen implementation or acquisition, run a new experiment, fit an inference model, make a new D8 selection, or claim a production result.

## D1

The published historical aggregates remain Lite **100/300 = 33.333333%**, with mean original accepted-case E2E **160.961968 s**, and Verified **198/500 = 39.600000%**, with mean original accepted-case E2E **147.330762 s**. The means use the original accepted-case timing, exclude evaluator time, and are distinct from trace-coverage reruns. `d1_historical_headline_table.csv` and `d1_historical_headlines.svg` carry those numerators, denominators, units, and population definition.

## D2–D7

`d1_d8_evidence_index.csv` records the retained input status for every D. D2/D3/D5/D6/D7 now have repository and category-style summaries from the existing 819-run, 545-instance `train_calibration` view. They report E2E, tool wall, model-proxy wall, and two tool/model proxy ratios. This view is incomplete historical proxy evidence, never a production or whole-baseline population. Its ratios are not CPU/GPU ratios and do not provide native device timing. D4 filters sweep metadata by frozen identity before it uses matching outcome or duration cells; the mixed sweep CSV is structurally parsed to find run IDs, but only eligible rows have outcome/wall fields decoded numerically or used.

## D4

The D4 table contains three settings for each of call limit, maximum output tokens, observation length, and temperature. Every setting has **18 matched pairs across 17 independent instance clusters**. It records `n_pairs=18`, `n_instance_clusters=17`, individual redacted pair rows, per-setting means and medians of paired E2E changes, and wins/losses/ties. Each parameter has its own 18 baseline coordinates. Their 72 appearances are dependent parameter-panel copies across those 17 clusters and must never be treated as 72 independent pooled baseline observations. The figure presents pair trajectories and per-setting means descriptively; it does not establish causal configuration effects or hardware scaling.

## D8

The corrected retained-confirmation-fixture boundary is descriptive only and excluded from fitting:

| Quantity | Value | Population / meaning |
| --- | ---: | --- |
| Semantic agent actions | 18.491612 s | 40 terminal semantic actions |
| Auxiliary runtime commands | 14.142780 s | 60 terminal runtime commands |
| Tool-execution union | 32.634391 s | merged-clock union of those disjoint command classes in this serial fixture |
| Native prefill service | 3.407418 s | summed over 40 physical requests |
| Native decode service | 74.575329 s | summed over 40 physical requests |
| Native E2E service | 78.120326 s | summed over 40 physical requests |

The semantic/native prefill+decode proxy is **0.237124**; the full tool-execution/native proxy is **0.418482**. Neither is a CPU/GPU ratio: their numerator is host command wall and their denominator is native engine service wall, not GPU-kernel time. The cache-aware request input records 0–35,264 cached tokens, so total context growth is not represented as fresh prefill work.

The fixture has 153.117581 s outer E2E and 7.303200 s of retained unknown lifecycle gaps. The packet leaves their cause unknown and never stacks lifecycle phase unions that may overlap. It provides explanatory evidence for this retained confirmation fixture only; it cannot repair event/work evidence for the older D8 examples or select a new one.

`provenance.json` binds the source paths and SHA-256 digests, units, numerator/denominator definitions, scope gate, overlap rule, and nonclaims. Run `python3 build_figures.py` from this directory's repository root to reproduce the derived tables and figures.
