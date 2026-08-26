# Assignment traceability and evidence boundaries

The coding-test PDF is authoritative. This file maps its exact requirements to
the evidence currently preserved in the repository and to the remaining work.
Historical evidence is retained even when it does not satisfy the final
assignment contract.

## Metric contract

The primary CPU-to-GPU latency ratio is:

```text
sum(tool-call wall_ms) / sum(model-request wall_ms)
```

Tool calls and model requests must come from the same measured SWE-agent
trajectory. CPU activity, CUDA activity, kernel-duration sums, Kineto/Nsight
data, NVML samples, process overlap, and vLLM counters are secondary
diagnostics. None may replace either side of the primary phase-latency ratio.

For the assignment figures, **category means repository**. SWE-bench Lite and
Verified do not publish one uniform issue-type taxonomy, and the supplied
reference implementation groups samples by repository while labeling that
axis as the repository category. We therefore preserve the exact repository
identity instead of inventing an unreviewed bug/feature taxonomy.

## Requirement map

| Assignment requirement | Current status | Preserved evidence | What the evidence can establish | Remaining requirement |
| --- | --- | --- | --- | --- |
| Step 1: SWE-agent + Qwen + vLLM on Lite and Verified | partial measured cohort | Pinned runtime manifests and `project/h100_results/population_runs.csv` | 64 selected population outcomes under the recorded H100 setup | Recover or rerun complete assignment-normalized trajectories for the declared manifests |
| Deliverable 1: Lite resolved rate | measured for selected cohort | 32 selected/completed, 8 resolved | 25.0% for this 32-task cohort | Full-suite result only if the declared manifest is the full pinned Lite suite |
| Deliverable 1: Verified resolved rate | measured for selected cohort | 32 selected, 30 submitted, 29 officially completed, 10 resolved | 34.4828% of completed cases and 31.25% of selected cases | Preserve both denominators; complete the declared manifest before a full-suite claim |
| Deliverable 1: average E2E latency | missing from compact population export | `project/h100_results/exclusions.csv`; candidate raw VM paths | Individual retained trajectories have timing, but the compact 64-row population table does not | Recover worker state/trajectories/logs or rerun unmatched baselines |
| Deliverable 2: categorize repositories and plot sample ratio | partial | `repository_coverage.csv`; one detailed Astropy trajectory | Repository coverage and one direct case study | Complete tool/model wall timing across the declared category sample |
| Deliverable 3: three category figures | tooling present, data incomplete | `scripts/assignment/generate_step_figures.py` | Can generate resolved-rate vs E2E, resolved-rate vs primary ratio, and E2E vs primary ratio from canonical tables | Supply complete canonical baseline rows and validate category labels |
| Deliverable 4: explain category differences | not population-supported | Kineto, strace, process attribution, and one detailed trajectory | Mechanistic observations for limited cases | Base category claims on multiple complete trajectories, not one Astropy case |
| Step 2: four hyperparameter sweeps | historical single-instance sample plus deterministic plan | `project/h100_results/sweep_results.csv`; `configs/assignment_steps_1_3.json` | 16 measured one-instance cells and a reproducible future matrix | Execute/recover the plan-matched shared baselines and missing 288 non-baseline cells |
| Step 2: per-parameter three-view figures | tooling present, historical scope limited | Existing sweep table and assignment figure generator | Limited one-instance sensitivity figures | Populate plan-matched multi-task sweep rows |
| Deliverable 5: combined sweep figure | three-view tooling present, data incomplete | Assignment figure generator | Produces the three required combined accuracy/latency/ratio views | Complete and compile all claimed sweep cells |
| Deliverable 6: sweep observations | partial | Historical one-instance sweeps | Narrow sensitivity observations | Avoid general claims until plan-matched multi-task results exist |
| Step 3 / Deliverable 7: high-ratio E2E breakdown | one case study available, selection not population-complete | `project/GCP_H100_KINETO_TRAJECTORY_20260824.json`, strace profile, observability summary | Detailed 31-request Astropy trajectory with secondary diagnostics | Select the highest eligible case using the primary ratio after complete Step 1 acquisition |
| Deliverable 7: each tool event | partial | SWE-agent trajectory/strace sources where retained | Some operation timing/provenance | Normalize every tool event for the selected high-ratio case |
| Deliverable 7: each model event and tokens/context | measured for one case | 31 request records and token counts | Request-level behavior for one Astropy case | Confirm complete same-trajectory pairing with tool events and E2E timing |
| Deliverable 8: explain one instance | offline partial | Same detailed H100 sources | A bounded case study | Reframe around the highest primary-ratio eligible case or state the selection limitation |
| Deliverable 9: hardware-parameterized event simulator | offline implementation present; live acceptance pending | `src/agentic_sim/assignment/event_simulator.py` plus strict event schemas | Calibration-only tool, model-request, and E2E prediction with explicit CPU/GPU/storage parameters | Fit and evaluate on complete held-out assignment trajectories |
| Deliverable 9: regenerate Steps 1-3 figures | tooling present | Plan, ingest, compile, and figure scripts | Offline deterministic generation from canonical tables | Complete the underlying live data and end-to-end contract test |
| Deliverable 9: every individual event within 25% | offline evaluator and adaptive ordering safeguards present; not measured | Frozen-manifest evaluators, adaptive pre-event journal, and leakage/resume tests | Can enforce the per-event 25% gate once valid labels exist | Freeze each prediction before its event runs, reveal its label afterward, then score every held-out tool/model event |
| Deliverable 9: E2E within 25% | offline evaluator present; not measured for assignment trajectories | Frozen-manifest E2E evaluator and calibration-derived, hash-bound pre-trajectory forecast artifact; earlier request-only experiment remains separate | Can enforce the every-trajectory 25% gate once valid labels exist | Freeze a recomputable calibration-derived E2E forecast before trajectory-end reveal and evaluate complete held-out SWE-agent trajectories |
| Cross-platform A100 validation | no assignment-normalized live artifacts claimed | A100 setup/protocol scaffolding | Offline readiness and protocol design only | Acquire 24 complete assignment-normalized A100 baseline trajectories and evaluate frozen predictions |

## Adaptive live-evidence order

The adaptive live path is deliberately stronger than a post-hoc train/test
split. For each hardware target, it must preserve this sequence:

```text
calibration collection
  -> calibration integrity audit
  -> calibration-only fit/selection
  -> durable feature-only prediction before target event execution
  -> target event execution
  -> label reveal after the event
  -> frozen E2E manifest before trajectory-end reveal
  -> scoring
  -> independent adversarial completion audit
```

Only calibration labels may inform fitting or model selection. Holdout
predictions may use declared pre-execution request/tool/hardware features, but
may not consume measured holdout wall time, CPU time, CUDA time, Kineto/Nsight
timing, actual output tokens, response content/size, or another
target-derived value. The prediction journal and its SHA-256 chain must prove
that each prediction was durable before its corresponding label appeared.

This workflow is implemented and covered by offline tests, but no H100 or A100
completion is implied unless the resulting live artifacts include complete
trajectories, frozen manifests, revealed labels, score reports, and a passing
independent audit.

## Offline-complete versus live-GPU-required evidence

Offline work currently supports deterministic assignment planning, recovery
audit, reconciliation, runtime/hardware binding, canonical normalization and
figure generation, feature-leakage rejection, calibration-only fitting,
prediction freezing, resume integrity, scoring, and adversarial audit logic.
That establishes reproducibility and guard behavior only.

Live GPU evidence remains required for all performance claims: the actual
SWE-agent trajectory; official evaluator outcome; complete tool/model timing
and token records; E2E wall time; adaptive prediction/reveal records; frozen
E2E manifest; score report; and completion audit. Passing tests, a dry run, a
runtime preflight, a Docker image pull, or an installed A100/H100 environment
does not by itself establish a hardware result.

## Historical H100 evidence that must remain preserved

- `project/h100_results/population_runs.csv`: 64 selected rows across Lite and
  Verified.
- `project/h100_results/sweep_results.csv`: 16 measured one-instance sweep
  cells.
- `project/GCP_H100_KINETO_TRAJECTORY_20260824.json`: one detailed real
  SWE-agent trajectory with 31 serialized model requests.
- H100 strace/tool profiling, process/GPU attribution, service calibration,
  controlled Kineto rows, and raw-source hashes.
- The sealed 24-calibration/12-holdout feature-only experiment.

These sources are not useless. They support provenance, narrow results, parser
development, diagnostics, and recovery. They do not by themselves provide the
population tool/model phase ratio or Deliverable 9 event-level accuracy.

## Historical recovery status

The historical VM disk is confirmed wiped. The manifests recorded this VM and
source root:

```text
instance-20260822-182111
/home/jasonrivera691/eic-work
```

Batch roots begin under:

```text
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-next-6
/home/jasonrivera691/eic-work/artifacts/batches/lite-diverse-batch03-6
/home/jasonrivera691/eic-work/artifacts/batches/verified-diverse-batch03-6
```

`project/GCP_H100_PROGRESS.json` lists the historical batch IDs, and
`project/GCP_H100_MEASUREMENTS.json` records historical trajectory and status
paths. These paths are not present in the checkout. Audit them only if an
independent snapshot, object-storage export, or backup exists:

```bash
python3 scripts/assignment/audit_h100_recovery.py \
  --root /home/jasonrivera691/eic-work \
  --json-out /tmp/h100-recovery-audit.json
```

If an external backup is found, potentially recoverable evidence includes E2E
timing from worker state/logs, tool events from trajectories and timestamped
logs, model timing from request-proxy or detailed vLLM logs, and token counts
from request-aware runs. Without such a backup, the raw GCP telemetry must be
recollected. A complete population tool/model ratio cannot be claimed unless
both phase sums are recovered for the same run.

## Frozen runtime identity of historical evidence

- Model: `Qwen/Qwen3-Coder-30B-A3B-Instruct`.
- Model/tokenizer revision:
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`.
- vLLM: 0.10.0 with the pinned image digest recorded in the H100 manifests.
- SWE-agent revision: `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`.
- SWE-bench revision: `726c5461e2ef52d83cf1ea2107870a8bb3328d57`.
- Historical hardware: one Google Cloud H100 80 GB environment; provider-
  specific Modal sweep evidence remains labeled separately.

New acquisition may extend the project, but it must not overwrite or relabel
these historical sources.

## Remaining acquisition, exactly

### H100

Because the historical VM disk is wiped, recollection is required unless an
external backup supplies exact matching rows. Step 1 must cover every task in
the declared Lite and Verified manifests for a full-scoreboard claim. Step 2
contributes exactly 288 non-baseline rows in the current plan. Step 3 selects
the highest-ratio eligible baseline only after complete Step 1 acquisition and
requires a targeted repeat if that case lacks complete event evidence.

### A100

For cross-platform validation, A100 receives its own calibration-only fit and
sealed holdout validation: 24 deterministic A100 calibration trajectories and
12 predeclared A100 holdouts (8 interpolation and 4 extrapolation), each with
three measured repeats and the same workload construction. Each row needs the
official outcome, E2E wall time, every tool event, every model request and
token count, hardware/runtime metadata, and hashes. Do not use H100 latency
labels or coefficients as A100 training targets. No valid
assignment-normalized A100 trajectory is currently claimed.

## Local command sequence

The exact planning, optional backup audit, reconciliation, execution, ingestion,
compilation, plotting, and event/E2E evaluation commands are maintained in
`docs/ASSIGNMENT_COMPLETION_RUNBOOK.md`. The legacy aggregate evaluator remains
separate and is not evidence that Deliverable 9's event-level 25% gate passes.
