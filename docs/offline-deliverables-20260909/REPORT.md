# Offline models and corrected retained-evidence figures

The bounded pass is complete. It produced a reviewable candidate package and corrected D1–D8 inputs without changing acquisition, the frozen production implementation, configuration selection, or GPU state. **D9 accuracy is still not sufficient for acceptance.**

## Best defensible candidate from this pass

Training data: **545 independent instances, 819 historical runs, 23,245 CPU tool events and 23,868 completed request-proxy events**. Every repeated instance remains together in five outer folds; model selection uses three inner instance-grouped folds. All fits use the declared training partition. The later class-wise CPU extension is explicitly adaptive development, not untouched holdout confirmation. [Protocol](PROTOCOL.md), [independent review](reviewer.md).

| Target / selected procedure | Within 25% | Worst error | Interpretation |
|---|---:|---:|---|
| CPU class-wise hybrid | **68.51%** | **481.08%** | Historical tool-duration events; +0.76 percentage points versus coarse medians |
| GPU request-proxy baseline | **34.63%** | **234.83%** | Prospective, feature-free baseline; not native GPU phase prediction |
| Direct start-known E2E baseline | **65.69%** | **168.55%** | 538/819 runs; predicts total duration directly, not lifecycle composition |

The CPU hybrid eliminates **177** event misses, lowers mean error **37.35% → 36.37%** and p95 **153.07% → 136.33%**, and leaves the worst error unchanged. Its final fitted mapping uses the coverage-centered estimator for **patch and read** actions and coarse medians for the other classes. In grouped outer predictions, read coverage improves **94.35% → 95.36%** and worst error **70.20% → 55.19%**. The outer-fold selection procedure also sometimes selected alternatives for other classes; that procedure estimate is distinct from the final fitted mapping.

Broader mechanism stratification improved average coverage but damaged tails: the unconstrained coverage-centered CPU model reached **74.19%**, with worst error **20,437%**. It was rejected. Prompt-length/output-cap GPU stratification reached **36.36%**, with worst error **325.51%**, and was also rejected. More complex repository E2E models failed the same tail constraint. These comparisons are against the stated fresh baselines, **not a demonstrated improvement over frozen v3**. [CPU results](cpu/comparison.json), [hybrid results](cpu/class_hybrid_comparison.json), [GPU results](gpu_lifecycle/gpu_proxy_results.json), [E2E results](e2e/comparison.json).

## What repaired evidence supports, and what remains unvalidated

The real retained confirmation fixture reconstructs **40 request starts joined exactly to 40 native physical requests**, the separate **40 semantic actions / 60 auxiliary runtime commands**, and lifecycle intervals. It verifies the adapter and target boundaries. It is one confirmation-excluded instance, so these events cannot establish independently validated native-phase, atomic-operation or lifecycle models. No calibration was taken from that fixture. The composition smoke is an interface check; its ledger declaration alone does not prove complete disjoint timing coverage. [Native reconstruction smoke](gpu_lifecycle/real_native_smoke.json).

Request-start records expose the generation cap but no prompt-token count. Prompt-length models therefore remain conditional on a separately proven pre-request count; terminal usage counts were not smuggled into prospective predictions. Realized output tokens, measured residuals, completed work counters, current-event latency and future state were excluded from predictor inputs. Sequential features were omitted because the historical training view did not prove within-run event order.

The selected CPU/GPU predictions join exactly across all **819** run/instance/fold identities. CPU sums pass in **271/819** runs; GPU-proxy sums in **24/819**. Their partial event sum passes E2E in **0/819**, and the conjunction of every retained CPU/request event with direct E2E passes in **0/819**. This historical completed-event population is not the complete PDF-required population, and no lifecycle or observed residual term was inserted to make it pass. **The literal event-and-E2E gate remains unproven.** [Joined predictions and metrics](joint_hybrid_metrics.json).

There is a concrete feature limitation: **2,714** repeated prompt-length/cap groups contain incompatible ≤25% prediction intervals. A deterministic model using only those two fields cannot pass every event in those groups. This is a descriptive limitation of that feature representation, not a claim that all prospective models are impossible. It supports stopping further center tuning. [Counts and definition](feature_ambiguity.json).

The [candidate API](predict_candidate.py) exposes historical CPU tool wall, request-proxy wall and start-known E2E as separate targets; it rejects unsupported native/lifecycle targets. The [hardware handoff](CROSS_HARDWARE_HANDOFF.md) fixes feature provenance, prediction-before-label ordering, identities, denominators and target boundaries for later validation. These coefficients provide an unchanged-model control; no hardware scaling law or hardware-transfer accuracy is claimed.

## Corrected D1–D8 inputs

The [figure packet](figures/README.md) contains four SVG/PNG figures and CSV inputs for D1 headlines, D4 paired accuracy/latency evidence, D8 command boundaries, and cache-aware native phases, plus train-only repository/category summaries for D2–D7.

- **D8 split:** semantic actions **18.491612 s**, runtime commands **14.142780 s**, combined command union **32.634391 s**. Figures distinguish command wall, native service wall and overlapping lifecycle phases. Existing D8 examples are not replaced.
- **D4 dependence:** **18 matched pairs / 17 independent instance clusters** per setting. The 72 retained baseline-coordinate appearances are dependent plotting copies.
- **D1:** published historical Lite **100/300**, Verified **198/500**; original accepted-attempt mean E2E **160.962 s / 147.331 s**. These remain historical results, not the upcoming production outcomes.
- **D2–D7:** the derived repository/category tables explicitly cover the **819-run training subset**, report execution and independent-instance counts, and retain historical tool/model proxy definitions. They are not full-suite or native-hardware results.

Source hashes, fold audits, per-event predictions and access disclosures are retained. The independent review checked selection arithmetic, grouping, forbidden-input poisoning, prediction exports and model reloads. No further model search, acquisition change or new inference run is initiated by this packet.

NO FROZEN-ACQUISITION CHANGE NEEDED
