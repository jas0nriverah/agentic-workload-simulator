# Current assignment contract — September 14, 2026

Authority: **Coding tests Harrdware (2).pdf**, two pages, reread as text and
rendered images. SHA256:
`02f485cdd8cca4aae242d2f1a5306184a95d7ee0fae42527d7153f48a3424761`.
This document replaces historical handoffs as the current requirements/status
entry point. It does not replace the PDF or current user instructions.

## Required submission

Repository link and a write-up, with polished figures: bold, readable fonts,
axes, ticks, titles and frames. Use SWE-agent, a Qwen model and vLLM on both
SWE-bench Lite and Verified. The PDF permits any starting CPU/GPU combination.

| Requirement | Literal output | Verified position / work left |
|---|---|---|
| Step 1 prerequisite | Reproduce public-scoreboard accuracy and E2E | Our own benchmark results exist; matched scoreboard reproduction is not established. |
| D1 | Text: resolved rate and mean E2E for both suites | Historical tables: Lite 100/300, 160.961968 s; Verified 198/500, 147.330762 s. These are the historical configuration's results. |
| D2 | Category rows, CPU/GPU latency ratio x-axis, one dot per sample | Historical proxy-boundary figures exist; final category mapping and timing interpretation must be explicit. |
| D3 | Accuracy vs average latency; accuracy vs CPU–GPU latency; sample latency vs CPU–GPU latency, grouped by category | Figures exist on a documented exact outcome-join subset; complete final packet not established. |
| D4 (Step 1) | Explain category ratio differences | Measured evidence exists; final explanation needs consolidation. |
| D4 (Step 2) | Four hyperparameter sweeps, each with the illustrated three relationships | Four sweeps exist. The current retained packet lacks matched CPU/GPU-ratio panels. Recover from raw evidence where supported. |
| D5 | Combined hyperparameter figure | Summary exists; final combined view needs the same scope as individual panels. |
| D6 | Explain hyperparameter effects | Tie explanations to measured changes and sampling uncertainty. |
| D7 | High CPU/GPU-ratio instances, E2E breakdown and individual CPU/GPU event logs | Repaired event reconstruction and historical high-ratio examples exist. One complete final example/packet must preserve exact event and host boundaries. |
| D8 | Detailed single-instance CPU operation and GPU token/context/model-size/effective-bandwidth explanation | Evidence exists; a measured bandwidth relationship must not be replaced by nominal bandwidth asserted as effective bandwidth. |
| D9 | Configurable hardware event models, all Step 1–3 plots, individual-event and E2E errors within 25% on evaluator server | Current models fail. Transfer, complete composition and plot integration remain unfinished/unproven. |

The PDF repeats D4: both requirements count. In the schematic figures, the
axis says `CPU-GPU Latency`; the surrounding text specifies CPU-to-GPU latency
ratio. Use that interpretation explicitly, not an unlabeled invented metric.

## Simulator implementation contract

The background describes a serial reason/action loop in the latency-bound
single-trajectory regime. Step 3 supplies workload observations; Step 4 changes
hardware parameters. Implement conditional simulation of supplied event/work
descriptors and declared execution dependencies. This is a defensible reading,
not a claim that the evaluator has agreed to an exact JSON input interface.

Measured target durations, measured residuals, outcome labels and target-host
realized cache behavior are not generic hardware-independent predictors.
Token counts can describe a supplied workload. State-dependent inputs require
an explicit supplied-state contract or a predictive state model.

Keep semantic actions, runtime operations, individual file operations, native
GPU requests/phases and lifecycle wrappers distinct. A parent includes its
children; predict/score children individually but do not add them again to
the parent's E2E contribution. Unaccounted time is a diagnostic gap, not an
input or free correction. Observed interval containment can support an
accounting reconstruction; it does not by itself prove causal dependencies
that remain unchanged across hardware.

The PDF does not precisely define event granularity. Preserve the Step 3
operation inventory, including reads/writes/traversal. Do not merge away
difficult events after seeing errors, nor assert that every instrumentation
subspan is explicitly named by the PDF. Report boundaries and limitations.

## What can and cannot be concluded

The implementation requirements can be built. No retained result proves a
25% pass, and unsuccessful categorical fits do not prove impossibility.
Reference-hardware component models are useful evidence. A profile attached
as metadata is not a hardware-transfer model; merely changing coefficients
by an unvalidated nominal ratio does not establish transfer accuracy.

The PDF requires no specific 96/1,088-run design and no A100 specifically.
Those are project decisions. Live endpoint health has not been checked during
this offline task; dated inventories must not be presented as live status.

## Work implemented in this reset

- Removed stale current-count, old-branch and historical authorization claims
  from the active root README and project state entry point; originals remain
  under `docs/history/` as evidence.
- Added `src/agentic_sim/assignment/event_composition.py`: explicit inclusive
  parent accounting, missing-event reporting and graph/label validation.
- Added `scripts/assignment/reconstruct_repaired_composition.py`: integrate
  the repaired CPU/lifecycle and token-conditioned client-call components on
  a common instance-grouped development split. Preserve all admitted cases;
  unsupported fits and partial overlaps remain visible.
- Outputs go to `docs/current/composition/`. They are reference-platform
  integration diagnostics, not a completed simulator, hardware-transfer test,
  D7/D8 replacement, or final-evaluation acceptance.

Continue with the concrete reconstruction findings, hardware-supported model
integration and missing figure paths. Do not restart a broad acquisition audit
or infer a need for a full GPU matrix from these gaps.
