# Agentic Workload Simulator — Results Report

This template is intentionally incomplete until the required Lite/Verified
runs, sweeps, profiling, and simulator holdout are measured. Every numeric
claim must cite an immutable run manifest, official evaluator report, or
derived artifact. Replace `PENDING` only when the referenced evidence exists;
never fill a missing result with a placeholder number.

## 1. Scope and reproducibility

| Field | Value |
| --- | --- |
| Repository commit | `PENDING` |
| Model and revision | `PENDING` |
| vLLM image/revision | `PENDING` |
| SWE-agent revision | `PENDING` |
| SWE-bench revision | `PENDING` |
| Hardware/provider | `PENDING` |
| Dataset source/revision/hash | `PENDING` |
| Evaluator image digests | `PENDING` |

Record the exact command/config hash for every condition. Keep control and
thin telemetry payloads identical; report telemetry as observation overhead
only when the paired timing boundary and provenance support that comparison.

## 2. Step 1 — baseline and repository categories

### 2.1 Accuracy and end-to-end latency (Deliverable 1)

| Dataset | Instances | Resolved | Resolved rate | Mean E2E latency (s) | Evidence |
| --- | ---: | ---: | ---: | ---: | --- |
| Lite | `PENDING` | `PENDING` | `PENDING` | `PENDING` | `PENDING` |
| Verified | `PENDING` | `PENDING` | `PENDING` | `PENDING` | `PENDING` |

State explicitly whether latency is trajectory-reported, wall-clock,
evaluator-excluded, or unavailable. Do not compare unlike scopes.

### 2.2 Category ratio plot (Deliverable 2)

Define the repository categories before inspecting outcomes. The source table
must contain repository, category, CPU latency, GPU latency, ratio, instance
ID, dataset, and evidence hashes. Plot CPU:GPU latency ratio on the x-axis and
category on the y-axis with a visible legend, uncertainty or sample counts,
bold readable labels, and a caption explaining exclusions.

### 2.3 Three required figures and observations (Deliverables 3–4)

Include the three assignment figures: accuracy vs average latency, accuracy vs
CPU-GPU latency, and per-point latency/accuracy by category. Follow them with
observations supported by the category table; distinguish measured association
from causal explanation.

## 3. Step 2 — four hyperparameter sweeps

Use the frozen four knobs and record every cell, seed, evaluator outcome, and
timing scope:

1. `agent.model.per_instance_call_limit`: 10, 20, 30, 50
2. `completion_kwargs.max_tokens`: 512, 1024, 2048, 4096
3. `agent.templates.max_observation_length`: 10000, 25000, 50000, 100000
4. `agent.model.temperature`: 0.0, 0.2, 0.5, 0.8

### 3.1 Per-parameter plots (Deliverable 4)

For each knob, show the accuracy-latency trade-off and identify fixed
settings, seeds, sample counts, and unavailable cells. Do not pool conditions
with different model revisions or evaluator contracts.

### 3.2 Combined figure and observations (Deliverables 5–6)

Provide one consolidated figure and a concise evidence-backed interpretation.
Report uncertainty and failed/aborted cells instead of silently dropping them.

## 4. Step 3 — high CPU-to-GPU ratio case study

Select the instance using the predeclared ratio rule. Include an end-to-end
latency breakdown, CPU events (file reads/writes/traversal), GPU events (input
tokens/output tokens/context), clock identity, and profiler provenance. Keep
evaluator time separate from trajectory time. Do not assign server-aggregate
vLLM metrics to individual requests without a measured correlation/calibration.

## 5. Step 4 — hardware-parameterized simulator

Document every exposed CPU/GPU hardware parameter, calibration split, holdout
split, model form, and fitting procedure. Report per-event and end-to-end
prediction errors with the assignment threshold (within 25%) evaluated on
held-out measured events. Include failure cases and prediction intervals.

## 6. Limitations and provenance

List host/provider differences, missing clocks or counters, rejected samples,
failed evaluator runs, scratch-file quality issues, and any unavailable
metrics. Link each table/figure to immutable input manifests and checksums.

## 7. Visual-quality checklist

- [ ] Fonts, axes, tick labels, titles, frames, and legends are bold and legible.
- [ ] Figure dimensions and resolution are publication-quality.
- [ ] Captions state dataset, sample count, timing scope, and exclusions.
- [ ] No chart claims a result absent from an official evaluator or measured
      artifact.
- [ ] Rendered figures were visually inspected before submission.
