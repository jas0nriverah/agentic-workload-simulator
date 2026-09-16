# Verified D9 state and next action — September 13, 2026

This supersedes conversational guesses about the recent run count and how much
of the repaired evidence the models use. It reconciles the current queue with
the saved manifests, inspects training-only raw tool journals and script
artifacts, checks model input joins, and recomputes selected saved prediction
metrics. It does not rerun inference or fit a new model.

## Exact repaired production inventory

Read-only SQL against the production queue finds **55 accepted cases and 1,033
pending**, with 55 accepted attempt rows and 59 requeued attempt rows. The set
of accepted case/attempt identities exactly equals the September 10 saved
inventory. There is no unexplained 56th accepted case in this queue.

The pinned partition divides the accepted cases into:

- 49 training/calibration executions across 25 instances;
- 4 final-evaluation executions;
- 2 confirmation executions excluded from development fitting.

Native fitting uses all 49 training cases / 2,080 requests. CPU/lifecycle fitting
uses 43 cases / 22 instances. The six omitted cases have only the original
`retry_missing_physical_predecessor` validator code: ordinals 8, 9, 26, 31, 39,
50, respectively with 8, 4, 2, 2, 6, 2 errors. Their native acceptance is
explicitly narrower than whole-ledger acceptance. This review has not promoted
them into CPU fitting or established that the errors can be dismissed.

Sources: [inventory](d9-salvage-20260910/evidence/accepted_case_inventory.json),
[training manifest](d9-salvage-20260910/evidence/calibration_input_manifest.json),
and [new checked counts and per-case findings](d9-salvage-20260910/current_state_review_20260913.json).

## What has actually been modeled

| Work | Verified status | Remaining limit |
|---|---|---|
| Historical semantic command model | Implemented, fit, and serving parity tested previously. 77.21% within25 on 30,711 tool-duration events; detailed parser/runner/find/pager distinctions already exist. | Different targets and population from repaired syscall experiment. Does not use captured script contents. |
| Repaired CPU/lifecycle models | Fitted on 43 cases. Semantic-action coverage 53.54%, runtime-command 78.94%, client-processing 23.64%. | Compact input allowlist drops full action and script state; coarse class models are not the full old semantic model plus repaired evidence. |
| Repaired native models | Recomputed from saved predictions: token E2E 1,973/2,080 (94.86%); cache E2E 2,027/2,080 (97.45%). | Cache queue 42.02%, prefill 83.70%, decode 98.94%; request E2E does not establish every phase's accuracy. Conditional supplied token/cache trace. |
| Atomic CPU models | Four distinct Astropy training cases, plus one Django transfer case; operation/size/path and openat flags examined. | ~46% operation coverage is a bounded experiment, not exhaustive modeling across all repaired cases. |
| Repaired E2E | Recomputed 42/43 within25 for both direct and calibrated aggregate composition; worst 28.9580% / 31.1678%. All 22 instance IDs stay within one fold. | Aggregate CPU/native/remainder regressions. No per-action/script or atomic-model composition; arithmetic remainder is not a hardware-transfer law. |
| Simulator integration | Common CLI handles historical models, native request E2E and historical conditional E2E; v4 E2E has a separate prediction CLI. | Newer experiments are not all integrated into one consistent end-to-end model/figure path. |

The old 77%, later historical 68%, atomic 46%, and repaired E2E 97.7% are not
successive accuracy measurements of the same model or population.

## Concrete newly verified unused evidence

All 49 raw training tool journals match the hashes in their original ledger
reports. They contain 2,063 action-start records. The raw `actual_features`
payload preserves full commands, runner/module, pipeline/scope descriptors,
and script state; a top-level `script_state` check alone misses that nesting.

Across all 49 training cases:

- 340 action starts have script-path snapshots; all 340 have an earlier/equal
  snapshot timestamp, an existing source lifecycle record, and a matching
  hostname/boot/clock identity.
- 325 content references resolve and verify against their saved SHA-256;
  they reference 266 distinct retained files across all 25 training instances.
- None of these 325 references is truncated; 323 parse as Python. The content
  is explicitly decoded text re-encoded as UTF-8, not byte-exact filesystem data.
- 15 path snapshots lack a content reference. Missing/invalidated state stays
  unknown; it must not be fabricated. State availability across all tools is
  not a Python-script coverage denominator, because many tools need no script.

Within the already-admitted 43 CPU cases, there are 305 script-path snapshots,
292 verified content references (291 Python-parseable), and 13 missing content
references. All **1,780 modeled semantic-action targets** join exactly through
`pre_event_id` to their raw start record. Yet **zero** preserve `script_state`
and **zero** preserve the full `action` in their fitted feature mapping.

The source explains why: `_prospective_features` in
`docs/offline-followup-20260909/ledger/validate_ledger.py` intentionally emits only
tool operation/class/traversal-mode fields. `cpu_lifecycle/build.py` rebinds the
CPU hardware domain but does not restore the richer descriptors. The old
semantic model's feature function consumes command text, not script snapshots.

This is an incomplete offline modeling input path, not a new acquisition defect.
We have not yet shown that script descriptors improve accuracy; now we have
concrete proof they are available for a bounded test. AST syntax descriptors
cannot alone establish imported dependency behavior, files visited, or bytes
actually transferred. Snapshot freshness under arbitrary mutations still needs
to be respected when building the prediction contract.

## Recommended next action

**One bounded offline CPU integration/comparison on the 43 already-admitted
cases is the next step. No new GPU runs are needed first.**

1. Build a separate modeling adapter that joins the raw action-start descriptors
   and verified pre-action script content by identity. Keep the original
   validator/ledger artifacts intact; do not re-open acquisition work.
2. On the same fixed instance-grouped folds, compare the existing coarse
   repaired baseline, the existing full-command semantic model, and one small
   general script/work-descriptor extension. Preserve missing state and support
   thresholds; exclude measured latency, residual, outcome, script-hash/case
   memorization and future state. Report all-event coverage, tail errors, class
   slices, and equal-instance results. Repeated use of these development folds
   is not untouched final validation.
3. Accept a richer model only with measurable benefit, then integrate it into
   the simulator and inspect CPU/E2E errors on the same cohort. Give each
   target its explicit timing boundary; do not add nested span predictions.
4. Review the six excluded ledgers separately if it can enlarge independent
   support through a valid offline correction. This does not block step 1.

Do not restart parser research, generic center sweeps, the rejected first-GPU-
request indicator, or the already-resolved request identity investigation.
Distinct-hardware validation remains necessary to establish transfer; its
experiment should follow model/interface freeze, not precede this offline work.

Detailed earlier experiments and transcript anchors are in
[D9_PRIOR_EXPERIMENTS_20260913.md](D9_PRIOR_EXPERIMENTS_20260913.md). **D9 is not
passed.** Acquisition was repaired; the repaired information has not all been
carried through modeling and simulator integration.

## Literal PDF check — recommendation refined

Read both pages of `Coding tests Harrdware (2).pdf`, including the embedded
figure examples, on September 13. This section qualifies the next-action
recommendation above: richer semantic CPU inputs are a useful bounded
experiment, but cannot by themselves finish D9.

The PDF requires:

- Step 1: SWE-agent, a Qwen model, vLLM, both Lite and Verified; resolved rate
  and mean E2E; category/sample CPU-to-GPU ratio plots and explanations. The
  three D3 illustrations pair accuracy with average latency, accuracy with
  CPU–GPU latency, and sample latency with CPU–GPU latency. The schematic
  axis says `CPU-GPU Latency`; the surrounding text establishes ratio as the
  organizing quantity. Document that interpretation in the final figures.
- Step 2: four hyperparameter sweeps, the illustrated three-panel relationships
  for each, a combined summary, and explanations. The PDF repeats the D4
  label; neither its Step-1 explanation nor Step-2 sweep requirement disappears.
- Step 3: high-CPU/GPU-ratio examples, E2E breakdown, individual CPU operations
  (reads, writes, traversal) and GPU workload descriptors; one detailed event
  explanation relating latency to CPU operations and GPU model size/effective
  bandwidth, tokens and context.
- Step 4 / D9: reuse event models on another platform by changing hardware
  parameters; generate all Step-1–3 figures; each individual event and E2E
  must meet the 25% error criterion on the evaluator's server.

Consequences for the next work:

1. Define the Step-3 event boundaries and simulator input contract using the
   already retained examples. Keep semantic actions, runtime spans, atomic
   operations and GPU phases distinct. Do not redefine an event after seeing
   its error, pool away failed operations, or add overlapping durations.
2. Run the proposed 43-case descriptor comparison as one bounded component of
   that contract. Better whole-command predictions do not establish accuracy
   for individual reads/writes/traversal. Reuse existing atomic-operation and
   native GPU experiments when evaluating those separate targets.
3. Prioritize connecting supported event models to meaningful hardware
   parameters and a consistent E2E composition. Merely recording a hardware
   profile or fitting an aggregate arithmetic remainder cannot establish
   transfer. Single-platform evidence does not identify arbitrary scaling
   laws; keep assumptions and unsupported mechanisms explicit.
4. Connect the resulting model outputs and retained measured accuracy to the
   Step-1–3 figure pipeline. The PDF does not require the latency simulator to
   invent resolved outcomes; supplied evaluator results can remain measured
   workload metadata. Keep measured and simulated quantities visibly distinct.
5. Evaluate instance-grouped development errors per event and E2E, with
   missing predictions counted, worst errors and all-events-per-trajectory
   conjunctions. Preserve final holdouts. Within-25% coverage is a diagnostic,
   not a replacement for the literal each-event requirement.

The PDF logs token counts before describing simulation. A supplied workload
trace is therefore a defensible interpretation of simulator input; it does
not explicitly require predicting agent actions or output length from the
initial issue alone. Label this as conditional trace simulation. Target-machine
latencies, measured residuals, and realized target cache behavior cannot be
smuggled in as hardware-independent inputs. Cache assumptions need an explicit
state model or a clearly limited supplied-state contract.

**Best immediate move: bounded offline event-model integration, including the
verified descriptor recovery, hardware contract and figure path—not another
broad collection campaign or script-only accuracy optimization.** The PDF
specifies neither 96 nor 1,088 executions; those are project designs. Additional
runs should address a demonstrated modeling/validation gap. A small different-
hardware validation after interface/model freeze is high value; the PDF itself
says the evaluator will test on its server, rather than prescribing a second
local training platform. No present result proves that test will pass.
