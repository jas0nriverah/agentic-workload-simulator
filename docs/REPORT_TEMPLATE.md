# Agentic Workload Simulator — Results Report

This template is intentionally incomplete until the required Lite/Verified
runs, sweeps, profiling, and simulator holdout are measured. Every numeric
claim must cite an immutable run manifest, official evaluator report, or
derived artifact. Replace `PENDING` only when the referenced evidence exists;
never fill a missing result with a placeholder number.

## 1. Scope and reproducibility

| Field | Value |
| --- | --- |
| Repository commit | `d512f1eb12b6c6763e9096d39b6cd124c9965f0c` (`parallel-h100-shards`) |
| Model and revision | `Qwen/Qwen3-Coder-30B-A3B-Instruct` / `b2cff646eb4bb1d68355c01b18ae02e7cf42d120` |
| vLLM image/revision | `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271` |
| SWE-agent revision | `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9` (1.1.0) |
| SWE-bench revision | `726c5461e2ef52d83cf1ea2107870a8bb3328d57` (4.1.0) |
| Hardware/provider | Modal H100 (`H100!`), one GPU per trajectory |
| Dataset source/revision/hash | Intended: Lite `69611d31007e1c6731db8bd5b5c3f2d33f5bab6e`; Verified `91aa3ed51b709be6457e12d00300a6a596d4c6a3`. Harness-observed: Lite `b0dde1093fe417d83b7184254edf8199c1f0dff5`; Verified `78f471bf655a3137b2e8a75af1501690ec009ec3` |
| Evaluator image digests | `PENDING` (image tag was used; immutable evaluator digest was not captured) |

Record the exact command/config hash for every condition. Keep control and
thin telemetry payloads identical; report telemetry as observation overhead
only when the paired timing boundary and provenance support that comparison.

### 1.1 GCP H100 measured evidence

The first Google Cloud H100 session is recorded in
`project/GCP_H100_MEASUREMENTS.json`. It contains one genuine uninstrumented
Lite trajectory (`astropy__astropy-12907`) resolved by the official evaluator,
a paired thin-telemetry trajectory that also resolved, Lite and Verified gold
smokes that resolved, and a two-worker/two-row Lite batch whose workers and
official evaluators both completed successfully. The service calibration
record reports measured request/TTFT/TPOT/ITL values but deliberately makes no
GPU-time claim. The GCP sample is evidence of a working end-to-end pipeline,
not a six-repository population baseline. Request-level CPU:model-serving
correlation, simulator holdout error, and the assignment's population claims
remain `PENDING` until the required event-level data exists.

## 2. Step 1 — baseline and repository categories

### 2.1 Accuracy and end-to-end latency (Deliverable 1)

| Dataset | Instances | Resolved | Resolved rate | Mean E2E latency (s) | Evidence |
| --- | ---: | ---: | ---: | ---: | --- |
| Lite | 1 | 1 | 1.0 | 973 | `project/MODAL_LITE_CONTROL_FULL_PROMPT_MEASURED.json` |
| Verified | 1 | 0 | 0.0 | 211 | `project/MODAL_VERIFIED_CONTROL_FULL_PROMPT_MEASURED.json` |

These are one-instance controls, not scoreboard reproduction claims. Latency
is trajectory wall-clock from agent start/end and excludes the separate
official evaluator: Lite evaluator time was 77.58 s and Verified evaluator time
was 73.78 s. The raw Lite result resolved with a reproduction script; the
source-only derived Lite submission also resolved (86.76 s evaluator time) and
is recorded separately in `project/MODAL_LITE_CONTROL_CLEAN_MEASURED.json`.

Two additional Verified controls were run to test whether the unresolved result
was sensitive to budget: `max_tokens=4096` remained unresolved after 196 s of
trajectory time, and `call_limit=50, max_tokens=4096` remained unresolved after
416 s. Both completed the official evaluator without errors and are recorded in
`project/MODAL_VERIFIED_CONTROL_TOKENS4096_MEASURED.json` and
`project/MODAL_VERIFIED_CONTROL_CALLS50_MEASURED.json`; neither is treated as a
successful or clean patch.
As a diagnostic, a separate reference-completion transform added the missing
case-insensitive `NO` token comparison from upstream PR #14365 to the measured
source-only patch; that derived patch resolved officially in 72.37 s. It is
reported as reference validation, not as an additional LLM success, in
`project/MODAL_VERIFIED_REFERENCE_COMPLETION_MEASURED.json`.
A refined 50-call/4096-token LLM run that explicitly named both parsing paths
found both locations, but over-edited the regex definitions and introduced a
comma-delimiter regression; official evaluation recorded 8 passing and 1
failing test. This measured failure is preserved in
`project/MODAL_VERIFIED_REFINED_PROMPT_MEASURED.json`.
A final targeted LLM control with the two exact minimal edits requested
produced a clean one-file patch and resolved the Verified instance (`1/1`,
evaluator time 73.98 s). Its immutable evidence is
`project/MODAL_VERIFIED_EXACT_FIX_MEASURED.json`; this is the preferred
Verified patch for the submission, subject to the documented evaluator
revision limitation.

An independent Lite instance (`astropy__astropy-14182`) was also run on an
H100 with the same 30-call/2048-token budget. The measured LLM output was
unresolved and non-clean: it added scratch files and missed the dynamic
three-header-row behavior. A provenance-preserving reference completion of
that measured output resolved the hidden round-trip test in 70.85 s, but is
reported as derived reference validation rather than an LLM success. Evidence
is recorded in `project/MODAL_LITE_SECOND_INSTANCE_MEASURED.json`,
`project/MODAL_LITE_SECOND_INSTANCE_TARGETED_MEASURED.json`, and
`project/MODAL_LITE_SECOND_INSTANCE_REFERENCE_COMPLETION_MEASURED.json`.

An additional independent Lite H100 run (`astropy__astropy-14995`) resolved
officially with a 218-second trajectory and 72.65-second evaluator. Its raw
patch included two helper files, so the production-only derived submission was
re-evaluated and also resolved in 72.19 seconds. This provides a clean,
independently validated Lite source patch; both provenance levels are recorded
in `project/MODAL_LITE_THIRD_INSTANCE_MEASURED.json` and
`project/MODAL_LITE_THIRD_INSTANCE_CLEAN_MEASURED.json`.

An additional independent Lite H100 run (`astropy__astropy-6938`) also
resolved officially (`1/1`) with a 190-second trajectory and 101.15-second
evaluator. The raw output included helper files, while the production-only
clean re-evaluation resolved in 40.28 seconds. These results are recorded in
`project/MODAL_LITE_FOURTH_INSTANCE_MEASURED.json` and
`project/MODAL_LITE_FOURTH_INSTANCE_CLEAN_MEASURED.json`.

A fifth independent Lite H100 run (`astropy__astropy-7746`) tested empty-array
WCS transformations. The measured LLM patch remained unresolved because it
missed one of the two required guards. A derived completion adding that
missing guard resolved officially in 44.35 seconds; it is retained as
reference validation, not an LLM success, in
`project/MODAL_LITE_FIFTH_INSTANCE_REFERENCE_COMPLETION_MEASURED.json`.

### 2.2 Category ratio plot (Deliverable 2)

`PENDING`: no paired event-level CPU/GPU latency measurements are available
from the controls, so no category ratio or plot is claimed.

### 2.3 Three required figures and observations (Deliverables 3–4)

`PENDING`: the three assignment figures require the unavailable paired
CPU/GPU event table.

## 3. Step 2 — four hyperparameter sweeps

The frozen one-instance Lite sweep is recorded in
`project/MODAL_LITE_SWEEP_MEASURED.json`. It covers every endpoint:

1. `agent.model.per_instance_call_limit`: 10, 20, 30, 50
2. `completion_kwargs.max_tokens`: 512, 1024, 2048, 4096
3. `agent.templates.max_observation_length`: 10000, 25000, 50000, 100000
4. `agent.model.temperature`: 0.0, 0.2, 0.5, 0.8

### 3.1 Per-parameter plots (Deliverable 4)

Derived, dependency-free SVG plots are now generated from
`project/MODAL_LITE_SWEEP_MEASURED.json` by
`scripts/analysis/generate_sweep_figures.py`. The four individual figures are
in `project/figures/lite-sweep-{calls,tokens,observation,temperature}.svg`.
They plot measured agent trajectory wall time and label each point with the
official evaluator outcome. The two temperature cells that failed before
producing a trajectory remain explicitly annotated as infrastructure failures.

### 3.2 Combined figure and observations (Deliverables 5–6)

The self-contained combined figure is
`project/figures/lite-sweep-combined.svg`; its source and panel metadata are in
`project/figures/summary.json`. Across this one-instance sweep, the strongest
measured outcome is resolved with call limit 50, max tokens 4096, observation
lengths 25k/50k/100k, and temperature 0.5. This is descriptive evidence only:
the cells are one-at-a-time parameter changes, not a factorial experiment, and
the evaluator used the observed latest dataset revision rather than enforcing
the intended revision.

## 4. Step 3 — high CPU-to-GPU ratio case study

Phase-level evidence is available in
`project/MODAL_LITE_PROFILE_MEASURED.json`: vLLM readiness 225094.37 ms,
repository preparation 13335.03 ms, and SWE-agent execution 118187.29 ms on
an H100. Syscall-level file events, per-request GPU token timing, and request
correlation remain unavailable, so no high-ratio category case study or
causal vLLM attribution is claimed.

The corrected deep profile
(`project/MODAL_LITE_DEEP_PROFILE_MEASURED.json`) adds 248,249 measured
`strace` file events and lossless before/after vLLM snapshots. The server-level
counter deltas were 348,426 prompt tokens, 4,372 generation tokens, and 31
successful requests on an H100. These are aggregate counters with no
SWE-agent request identity; they support instrumentation validation but do not
justify per-request CPU:GPU ratios.
The derived CPU trace summary parses 209,746 of 248,249 syscall lines and
reports 6.577734 aggregate syscall-seconds; unmatched multi-line/interrupted
strace records are retained in the raw trace and excluded from the summary.
A second deep profile on the Verified task is recorded in
`project/MODAL_VERIFIED_DEEP_PROFILE_MEASURED.json`: it resolved cleanly,
captured 112,082 parsed file events from 144,735 raw lines, and measured
189,786 prompt tokens, 2,867 generation tokens, and 22 successful requests.
The parallel Lite profile provides a second Lite trace with 316,682 parsed
events and 41 successful aggregate vLLM requests
(`project/MODAL_LITE_DEEP_PROFILE_PARALLEL_MEASURED.json`).
The derived comparison figure is
`project/figures/deep-profile-comparison.svg`, generated by
`scripts/analysis/generate_profile_figure.py`; its numeric source summary is
`project/figures/deep-profile-summary.json`.

## 5. Step 4 — hardware-parameterized simulator

`PENDING`: there are no measured event-level calibration/holdout samples yet,
so the 25% simulator threshold cannot be evaluated honestly.

## 6. Limitations and provenance

List host/provider differences, missing clocks or counters, rejected samples,
failed evaluator runs, scratch-file quality issues, and any unavailable
metrics. The evaluator dataset revision was not enforced by the current
name-based harness command; this is recorded in each Modal manifest and
prevents these results from being presented as strictly revision-pinned.
Link each table/figure to immutable input manifests and checksums.

## 7. Visual-quality checklist

- [ ] Fonts, axes, tick labels, titles, frames, and legends are bold and legible.
- [ ] Figure dimensions and resolution are publication-quality.
- [ ] Captions state dataset, sample count, timing scope, and exclusions.
- [ ] No chart claims a result absent from an official evaluator or measured
      artifact.
- [ ] Rendered figures were visually inspected before submission.
