# Assignment traceability and pre-H100 evidence map

The attached coding-test PDF is authoritative. This matrix maps each required
deliverable to the first command/artifact that will produce evidence. A
`pending_h100` entry is an explicit validation boundary, not a result.

| Assignment requirement | Status | Future command / source of truth | Artifact or table | Report destination | Local acceptance |
| --- | --- | --- | --- | --- | --- |
| Step 1 / Deliverable 1: Lite and Verified resolved rate and average E2E latency | measured_partial | `lambda_run_first_experiment.sh --mode uninstrumented`; official evaluator | `run_manifest.json`, `.traj`, `preds.json`, evaluator report; supplementary measured/clean Lite instances in `project/MODAL_LITE_SECOND_INSTANCE_*`, `project/MODAL_LITE_THIRD_INSTANCE_*`, `project/MODAL_LITE_FOURTH_INSTANCE_*`, and `project/MODAL_LITE_FIFTH_INSTANCE_*` | Baseline accuracy/latency table | Command contract, evaluator isolation, archive round-trip |
| Step 1 / Deliverable 2: repository categories vs CPU:GPU latency ratio | pending_h100 | Aggregator over valid Lite/Verified trajectories | Categorized ratio dataset and publication-quality scatter plot | Step 1 category-ratio figure | Schema/provenance validation; no ratio until paired event boundaries exist |
| Step 1 / Deliverable 3: three categorized figures | pending_h100 | Same frozen aggregation input | Accuracy vs average latency; accuracy vs CPU-GPU latency; per-point latency/accuracy category figure | Step 1 figure panel | Figure schema and source-manifest checks |
| Step 1 / Deliverable 4: observations explaining category differences | pending_h100 | Reviewed aggregate tables plus event evidence | Category summary table and signed narrative inputs | Step 1 observations section | No causal prose without measured event support |
| Step 2: four hyperparameter sweeps | measured_partial | `cloud/modal/lite_control.py` with explicit runtime config | `project/MODAL_LITE_SWEEP_MEASURED.json` plus Modal-volume artifacts | Per-parameter accuracy-latency plots | Knob propagation and cell completeness checks |
| Step 2 / Deliverable 4: per-parameter trade-off plots | measured_partial | `scripts/analysis/generate_sweep_figures.py` over the sweep manifest | Four measured wall-time/outcome SVG panels in `project/figures/` | Step 2 figure section | CPU-GPU axes remain unavailable; deterministic aggregation and visual QA |
| Step 2 / Deliverable 5: one combined parameter figure | measured_partial | Same sweep result table | Self-contained `project/figures/lite-sweep-combined.svg` | Step 2 summary figure | CPU-GPU axes remain unavailable; deterministic aggregation and legend validation |
| Step 2 / Deliverable 6: sweep observations | measured_partial | `project/MODAL_LITE_SWEEP_MEASURED.json` | Cell outcomes and timing-scope notes | Step 2 observations section | One-instance scope; no population claim |
| Step 3 / Deliverable 7: high-ratio E2E breakdown and CPU/GPU event log | measured_partial | Explicit deep-profiled Modal control | Deep-profile manifests, `cpu_file_events.strace`, vLLM snapshots/delta, `events.jsonl`, `hardware.txt`, `project/figures/deep-profile-comparison.svg` | Step 3 breakdown figure/table | File syscalls and aggregate token/request counters measured; per-request GPU attribution remains unavailable |
| Step 3 / Deliverable 8: single-instance latency explanation | pending_h100 | Selected high-ratio instance and lossless event record | Event-level case-study table and narrative | Step 3 case-study section | No fabricated request timestamps or GPU attribution |
| Step 4 / Deliverable 9: hardware-parameterized simulator and <=25% error validation | pending_h100 | Calibrated real events after Step 3 | Simulator inputs, predictions, per-event/end-to-end error report | Step 4 model and validation figures | Holdout evaluation and explicit error thresholds |
| Gold-patch smokes | measured_lightning | `lambda_run_gold_smoke.sh --suite lite|verified` | Gold smoke reports and image/dataset hashes | Runtime/evaluator validation appendix | Static official command validation; H100-only execution |
| Publication-quality report | scaffold_only | Post-experiment aggregation and plotting | Immutable manifests, figures, tables, narrative | Final submission write-up | Provenance, secret/path scans, visual QA |

## Frozen execution boundaries

- Model/configuration: `Qwen/Qwen3-Coder-30B-A3B-Instruct`, BF16, 32K
  context guard, vLLM 0.10.0 with `qwen3_coder`, SWE-agent 1.1.0, and
  SWE-bench 4.1.0. Fit and tool-call health were observed on Lightning; the
  Lambda-target equivalence remains empirical.
- The control command and thin command carry identical model/tool payloads;
  thin mode only observes the running process.
- Native vLLM metrics remain server-aggregate. They are not assigned to a
  model request without a later calibration/correlation record.
- Modal controls and sweep cells use the same pinned model/tool payload and
  official Docker-enabled evaluator; each cell is separately rooted and hashed.
  Thin telemetry remains a separately authorized later session.
- The original H100 control and gold-smoke outcomes are measured artifacts,
  but the original control's empty patch was traced to an omitted
  `repo_name` compatibility field. Fixed follow-up Lite/Verified evaluations
  are recorded separately in `project/PAID_SESSION_MEASUREMENTS.json`; the
  Modal full-prompt Lite control resolved 1/1, the Verified control resolved
  0/1, and all four sweep endpoint sets are covered for one Lite instance.
  These are not scoreboard or population resolved-rate claims. No paired
  CPU/GPU latency, thin-overhead, or simulator holdout result is claimed by
  this document.
