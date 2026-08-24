# Assignment traceability and pre-H100 evidence map

The attached coding-test PDF is authoritative. This matrix maps each required
deliverable to the first command/artifact that will produce evidence. A
`pending_h100` entry is an explicit validation boundary, not a result.

| Assignment requirement | Status | Future command / source of truth | Artifact or table | Report destination | Local acceptance |
| --- | --- | --- | --- | --- | --- |
| Step 1 / Deliverable 1: Lite and Verified resolved rate and average E2E latency | measured_sampled | GCP batch manifests and official evaluator logs summarized by `project/GCP_H100_PROGRESS.json` | 32 Lite trajectories (32 officially completed) across 11 repositories; 29 Verified trajectories (28 officially completed) across 12 repositories, with two retained pre-generation failures | Baseline accuracy/latency table | Command contract, evaluator isolation, archive round-trip |
| Step 1 / Deliverable 2: repository categories vs CPU:GPU latency ratio | partially_satisfied | One real Astropy trajectory has paired direct device timing; broader batch outcomes span 11–12 repositories without Kineto | `project/GCP_H100_KINETO_TRAJECTORY_20260824.json` plus sampled baseline manifests | Step 1 category-ratio figure | No population category ratio from a one-repository direct profile |
| Step 1 / Deliverable 3: three categorized figures | partially_satisfied_offline | Accuracy/latency figures can use sampled baseline; CPU/GPU category axes remain limited to one direct case plus labeled proxies | Existing batch and Kineto manifests | Step 1 figure panel | Figures must distinguish direct timing, proxies, and unavailable population fields |
| Step 1 / Deliverable 4: observations explaining category differences | partially_satisfied_offline | Reviewed sampled aggregate tables plus the direct Astropy case study | Category summary table and signed narrative inputs | Step 1 observations section | No causal population prose from the one direct profile |
| Step 2: four hyperparameter sweeps | measured_partial | `cloud/modal/lite_control.py` with explicit runtime config | `project/MODAL_LITE_SWEEP_MEASURED.json` plus Modal-volume artifacts | Per-parameter accuracy-latency plots | Knob propagation and cell completeness checks |
| Step 2 / Deliverable 4: per-parameter trade-off plots | measured_partial | `scripts/analysis/generate_sweep_figures.py` over the sweep manifest | Four measured wall-time/outcome SVG panels in `project/figures/` | Step 2 figure section | CPU-GPU axes remain unavailable; deterministic aggregation and visual QA |
| Step 2 / Deliverable 5: one combined parameter figure | measured_partial | Same sweep result table | Self-contained `project/figures/lite-sweep-combined.svg` | Step 2 summary figure | CPU-GPU axes remain unavailable; deterministic aggregation and legend validation |
| Step 2 / Deliverable 6: sweep observations | measured_partial | `project/MODAL_LITE_SWEEP_MEASURED.json` | Cell outcomes and timing-scope notes | Step 2 observations section | One-instance scope; no population claim |
| Step 3 / Deliverable 7: high-ratio E2E breakdown and CPU/GPU event log | measured_case_study | Real frozen SWE-agent Kineto trajectory plus existing CPU/tool strace and proxy evidence | `project/GCP_H100_KINETO_TRAJECTORY_20260824.json`; 31 requests with direct CUDA activity union, tokens, raw trace and hashes | Step 3 breakdown figure/table | Direct activity timing is measured; hardware counters remain unavailable |
| Step 3 / Deliverable 8: single-instance latency explanation | measured_case_study | Same Kineto trajectory and official evaluator | Per-request timing/device table; unresolved official outcome retained | Step 3 case-study section | One Astropy instance only; no category-population generalization |
| Step 4 / Deliverable 9: hardware-parameterized simulator and <=25% error validation | measured_limited_scope | `scripts/observability/run_kineto_matrix.py`; `scripts/observability/derive_kineto_request_attribution.py`; `scripts/analysis/evaluate_simulator.py` | `project/GCP_H100_KINETO_SIMULATOR_20260824.json`; four calibration and two holdout rows; 10.7156% holdout MAPE | Step 4 model and validation figures | Controlled serialized vLLM matrix only; no broad SWE-agent generalization claim |
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
- The planned GCP path is one `a3-highgpu-1g` Spot VM with a persistent data
  disk. `cloud/gcp/create_h100_spot.sh` is safe by default and requires
  `--apply`; `cloud/gcp/preemption_shutdown.sh` is the best-effort export hook.
  The current project quota does not expose an adjustable H100 request, so no
  GCP measurement is claimed yet.
- `scripts/observability/request_proxy.py` records request IDs, timing
  boundaries, hashes, sizes, status, and returned token counts without storing
  prompts or responses. It is reserved for a separate profiled attempt and
  does not itself establish GPU device time.
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
