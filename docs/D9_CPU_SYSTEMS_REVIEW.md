# D9 CPU and simulator architecture review

This review continues the Grok calibration checkpoint. The previous Cursor
conversation, current dirty worktree, source, and CPU calibration artifacts were
inspected before implementation. Grok's source snapshots and the new evidence
are under `assignment/submission/20260908T060000Z/d9-cpu-review/` in the assignment
workspace. Previous reports and the sealed online experiment remain historical
evidence. No holdout evaluation or GPU execution is part of this review.

## Cohort and validation boundary

The original cache contains 30,723 positive-duration tool events and 1,084
trajectories. It excludes the designated holdout run, but contains one other run
with the same instance identity. New fitting and scoring quarantine that instance
entirely: 1,083 trajectories and 30,711 tool events remain. The historical 73.2%
result is preserved; new candidates must be compared with a baseline rerun on
the identical retained cohort.

Only the calibration allowlist may open raw trajectories. Recovered actions
must match both the cached action SHA-256 and event duration; raw source paths
and hashes are recorded. Runtime labels and observations are not descriptors.
An observation may establish a diagnostic cause, but may not select a prediction
mode. Repository identity is a logged descriptor; it is not a measured file count
or a portable description of the installed environment.

Original hash-by-run folds provide historical comparability. Repeated sweep
instances must also be grouped together in the primary generalization analysis.
Repository-grouped folds test transfer to repositories absent from training.
All are calibration experiments, not fresh holdouts. Candidate comparisons on
these reused data do not become independent confirmation after model selection.

## Findings and model decision

The current hierarchy merges execution mechanisms, not merely noisy samples of
one workload. Its `find` keys omit `-exec`: in calibration Django plain walks have
a median near 256 ms, versus 6.1 seconds for per-match subprocess searches.
Declared path count is the number of parsed command operands, not the number of
visited files or launched children. A recursive walk with one operand can launch
thousands of children. Pipes can terminate work early or disable a pager; a single
Boolean cannot describe both effects.

Git's 31–32 second cluster is consistent with pager-sensitive execution reaching
the harness's 30-second command timeout plus interruption. The harness source
measures interruption in tool execution time. Piped `git show` examples are near
127 ms, but four such examples cannot meet the old hierarchy's support threshold,
so they inherit an unpiped subcommand median. A pager-aware fallback across
`log`, `show`, and `diff` is more defensible than that fallback or a duration cap.
Pager susceptibility is inferred from command syntax; it is not proof that any
particular future command will time out.

Test classification currently checks for substrings such as `pytest` before
checking the actual executable. Editor operations on pytest files and searches
for unittest text become tests. Conversely, recognized project test runners are
sometimes labeled shell. `python -m` loses the module in its subcommand key.
Actual runner/module, selected test scope, and repository environment matter:
calibration Django `python -m pytest` examples around 150 ms report that pytest
is not installed. They do not measure a fast successful test suite.

Python script invocations omit the script's changing contents and imported work.
This does not mean those contents are always unavailable: a conservative
chronological replay of prior logged editor `create` and unambiguous
`str_replace` actions reconstructs a declared body for 4,513 of 4,789 Python
script calls (4,487 bodies parse as Python). This replay uses only earlier
actions, not subsequent observations or labels. The current stateless event
interface does not carry that state. Supporting it safely requires explicit
invalidation after arbitrary shell writes and a shared train/serve state contract;
replaying only recognized editor operations is not proof that a body is current.
For frequently repeated identical action/repository keys (at least five samples),
the retrospective best fixed point covers only about 71% of heavy-class events
within 25%. This is a diagnostic ceiling for those fixed keys, not a theorem
about richer simulators. It explains why exact command hashing is not a remedy.

The selected architectural change is a support-aware semantic hierarchy for
the original shell/test/traversal families. Compact original classes retain the
existing predictor. Semantic keys describe executable/runner/module, operand
bucket, traversal subprocess mode, recursion, coarse pipeline/redirect mode,
inline Python imports, and pager exposure. The shared extractor also exposes
test/path scope, depth, traversal predicates, and pipeline stages; the selected
compact keys deliberately omit those finer partitions to limit sparsity. Their
availability is not a claim that this comparison establishes a scope- or
volume-proportional physical cost law. Conditional
repository estimates require support from several distinct instances. Sparse
keys back off to execution semantics before broad class averages. No exact
commands, paths, task identities, elapsed times, observed failure modes, or
evaluator outcomes are prediction keys. Median and fixed-25%-gate representatives
are explicit ablations of this architecture, not a change to the requirement.

This remains an empirical conditional latency model. A physical equation in
files, bytes, test cases, and subprocesses cannot be identified from command
length and operand counts. Adding invented work volumes would obscure that
limitation. A stateful declared-script descriptor is a plausible next extension,
using available earlier actions where validity can be established. Actual file
counts, bytes, installed runner availability, and realized subprocess counts
would require additional legitimate inputs or measurements. These extensions
are not included in this bounded comparison, and the fixed-key ceiling is not
an upper bound on their potential accuracy.

## Train/serve equivalence

The old calibration cache derives flags from full actions, but serving reconstructs
them from a three-token prefix. Segment counts differ on 11,854 original events,
pipe counts on 1,855, and recursion on four. The selected hierarchical median
does not use the segment or pipe counts; only recursion affects its keys.
On the retained cohort, only four predictions change, both routes retain exactly
73.2050405% within 25%, and their total predicted mass differs by about 1.06 ms.
Thus this mismatch does not materially invalidate the reported baseline. It would
invalidate a new model relying on the richer fields unless train and serve share
the complete extractor. Full-action input and deterministic extraction are part
of the new contract; a missing action must use an explicit legacy fallback.

## E2E is not currently a sum of the measured event labels

The runner summary measures the SWE-agent subprocess lifetime. Tool labels wrap
`communicate` and timeout interruption, ending before the subsequent `get_state`.
GPU/model labels measure successful proxy request boundaries. Agent startup,
client/agent processing, state queries, retry gaps, and teardown are outside those
event boundaries. The official evaluator is timed separately and excluded.

An allowlisted retained-cohort proxy audit finds 340 failed chat requests across
80 runs, totaling 2.765 million ms. These are absent from the completed-model
cache. They explain some, but not most, of the E2E residual. Report conditional
all-scored-event gates and coverage-aware gates separately; missing requests
cannot be assumed to pass. No observed request failure is used as a feature.

Source conservation has a further limitation: rebuilt tool sums disagree with
the older protocol sums by more than 0.001 ms on 30 runs (177,183 ms total
absolute discrepancy). On 28 of those runs, 573 cached GPU request IDs are
absent from the first allowlisted proxy source. All 28 are already in the
incomplete-request set. The union of incomplete-request and tool-incoherent
runs is 93, leaving 990 potentially coverage-eligible trajectories. These are
source-identity/coverage cautions, not grounds for relabeling or correcting
individual CPU durations: recovered CPU actions match their cached hashes and
durations. Report the same E2E predictions on the source-consistent subset as
a sensitivity check, without refitting or hiding the full-cohort result. The
current overhead calibration includes these rows and remains provisional.

On the original cache, even the sum of *observed* CPU and model labels is within
25% of observed E2E on only 5/1,084 trajectories. The unassigned residual totals
88.62 million ms, 55.3% of E2E mass, with a median of 58.7 seconds per trajectory.
Its detailed causal partition is not logged and must not be invented.

There is also a strict feasibility check: if all event predictions pass, their
sum cannot exceed 1.25 times the observed event sum. For 882/1,083 retained
trajectories, that upper bound is still below 0.75 times observed E2E. Thus a
CPU+GPU-only sum cannot simultaneously meet the individual-event and E2E gates
on those trajectories, regardless of estimator quality. This is a consequence
of the timing boundaries, not evidence for relaxing 25%.

The old E2E ridge gives independent coefficients to predicted CPU/GPU sums and
event counts. Its accuracy does not certify event-sum accuracy; it can compensate
for missing overhead and biased predictors. It is retained only as a labeled
historical comparison. The new default composition is predicted event sums plus
a separate nonnegative runner-overhead estimate fitted from training residuals
and event counts. No CPU prediction errors enter the overhead target. Reports
must give direct sums, overhead-aware E2E, and legacy regression separately.
Overhead prediction is empirical and environment-specific, not GPU execution.

## Hardware portability is not established by this calibration

`cpu_threads * cpu_base_ghz` is not an appropriate speed law for serial subprocess
latency. It predicts a twofold speedup from doubling SMT threads, including for
filesystem and timeout waits. The cache supplies one hardcoded reference hardware
profile, not a multi-platform intervention separating CPU work, launch overhead,
storage work, and waiting. No measured read/write volumes are available.

The new policy removes automatic thread-count speedup, treats learned pager
timeout-like modes as wall-clock waits, and exposes serial-frequency sensitivity
as an explicit configurable assumption. Its serial fraction is not estimated
from this one-platform dataset. Storage bandwidth effects and parallel speedups
remain uncalibrated. This is a sensitivity model, not validated cross-server
accuracy, and cannot by itself satisfy the lab's portability requirement.

The GPU formulation remains frozen: logged input/output/context tokens and GPU
bandwidth/compute parameters. Input and context counts are identical in all cached
model events, so that design has rank three, not four; their separate coefficients
are not identifiable. The reference fit also has a small negative intercept.
Good reference-platform prediction does not validate prefill/decode resource
decomposition, GPU-count linear scaling, or transfer to another GPU. The accepted
use of logged output tokens is unchanged.

## Acceptance criteria

Report every candidate's event within-25 rate; original-class and semantic-class
metrics; mean/median/p95/max APE; per-trajectory tool-sum error and aggregate bias;
GPU and each E2E metric; and trajectories with every required recorded event and
E2E within 25%. Preserve exclusions and missing-event coverage limitations.
Report paired instance-bootstrap uncertainty and unseen-repository performance.
Keep 25% unchanged. Do not recommend a fresh holdout merely because a mean improves:
the all-event gate, measurement coverage, and hardware transfer limitations must
support that decision.

## Reviewed calibration result and decision

Select the repository-conditional **semantic median**, not the gate-optimized
center. On the primary instance-grouped folds it improves CPU within-25 from
72.772% to **77.210%** (+4.438 percentage points; paired instance-bootstrap
95% interval +3.696 to +5.160 points). The gate center reaches 77.510%, but its
additional 0.300 points are uncertain under paired instance resampling and it
worsens mean event APE (28.014% versus 26.974%) and trajectory tool-sum mean APE
(23.136% versus 21.105%). No support thresholds were tuned to these results.
The median is the smaller, better-balanced change; the gate remains a reported
ablation, not a relaxed requirement.

| Original class | Matched instance-fold baseline within 25% | Selected within 25% | Selected mean APE | Median APE | p95 APE | Max APE |
|---|---:|---:|---:|---:|---:|---:|
| read | 94.82% | 94.82% | 11.76% | 10.77% | 25.20% | 76.42% |
| search | 88.76% | 88.76% | 8.88% | 3.44% | 44.32% | 92.82% |
| patch | 90.26% | 90.26% | 13.19% | 11.99% | 28.33% | 74.49% |
| write | 88.66% | 88.66% | 14.05% | 11.72% | 31.76% | 283.41% |
| traversal | 63.03% | 79.14% | 41.21% | 12.47% | 78.13% | 4432.96% |
| test | 46.27% | 66.44% | 40.85% | 14.20% | 107.79% | 4052.88% |
| shell | 38.87% | 42.51% | 52.98% | 31.15% | 211.54% | 1952.31% |
| overall | 72.77% | **77.21%** | **26.97%** | **12.15%** | **77.17%** | **4432.96%** |

These are original-class slices so reclassification cannot artificially improve
the comparison by moving difficult events out of a denominator. Semantic-class
metrics are reported separately in the numerical artifact.

| Primary calibration quantity | Within 25% | Mean APE | Median APE | p95 APE | Max APE |
|---|---:|---:|---:|---:|---:|
| Per-trajectory CPU sum | 68.98% | 21.10% | 15.55% | 65.45% | 213.36% |
| Frozen-form GPU events | 95.23% | 8.43% | 6.22% | 24.53% | 97.25% |
| Direct CPU+GPU E2E | 0.09% | 52.10% | 50.68% | 78.17% | 99.26% |
| CPU+GPU+runner-overhead E2E | 59.37% | 23.92% | 20.36% | 53.76% | 93.54% |
| Legacy trajectory-ridge E2E | 60.76% | 33.42% | 18.02% | 113.58% | 430.22% |

CPU aggregate signed bias is -2,844,512 ms (-13.99%); trajectory-sum WAPE is
23.84%. This is distinct from mean trajectory APE and from an absolute signed
aggregate bias. Restricting the same overhead-E2E predictions to the 1,053
tool-source-consistent runs gives 60.02% within 25% and 23.41% mean APE; it does
not repair the measurement contract. Only **1/1,083 trajectories (0.092%)**
passes every recorded CPU event, GPU event, and overhead-aware E2E gate; that
run is coverage-eligible, so the conservative coverage-aware numerator remains
one. Direct E2E plus all event gates passes zero trajectories.
Without the E2E condition, 12/1,083 trajectories pass every scored CPU/GPU
event; only 6/1,083 (0.554%) have sufficiently complete/coherent coverage to
count as passing every required CPU/GPU event. Unknown or omitted events are
not silently treated as correct.

Original run-hash folds, for comparison with Grok, give 73.205% baseline and
77.754% selected median. The stricter instance-grouped figures above should
lead the claim. With whole repositories absent from training, the baseline
falls to 66.628% and the selected median reaches only 67.595%. Repository context
is useful within this logged environment but is not a transferable physical
description of repository work.

The largest remaining selected-model tails are narrow/selective `find -exec`
operations inheriting the repository's broad per-match work estimate: for
example a 136 ms command receives about 6,145 ms. This is a different failure
from the old unpiped-git timeout backoff. No duration clipping conceals it.
Script state, visited/matched-file volume, test availability, and measured
runner boundaries are the next defensible inputs to investigate. More scalar
centers or exact-command memorization cannot substitute for those inputs.

**D9 remains failed at the unchanged gate. Do not score a fresh holdout.**
Accept this implementation as a materially improved calibration checkpoint,
not as a validated all-events or cross-hardware simulator. Stop this bounded
model comparison rather than expanding an adaptive search across the same
calibration trajectories. The next stage should first establish coherent event
coverage and a valid state/work-volume input contract; further modeling can
then be evaluated under a prespecified grouped protocol. No holdout or 1,088-case
GPU matrix execution is authorized or recommended by this result.

## Implementation and reproducibility

The shared extractor and support-aware CPU estimator are in
`src/agentic_sim/assignment/semantic_cpu_model.py`. Full-action inputs, v3 model
serialization, explicit CPU scaling assumptions, and the separated E2E
composition are in `src/agentic_sim/assignment/workload_simulator.py`.
The preserved legacy predictor remains available; old reports are unchanged.

The full numerical comparison, semantic-class slices, bootstrap intervals,
coverage conjunctions, and source-consistent E2E sensitivity are in
[D9_CPU_REVIEW_RESULTS.md](/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T060000Z/d9-cpu-review/D9_CPU_REVIEW_RESULTS.md),
with machine-readable JSON and SHA-256 sidecars beside it. `execution_manifest.json`
records the cohort, exclusions, fold rules, fixed candidate settings, source
hashes, and interpreter. Eleven prediction artifacts preserve event-level OOF
results across all completed protocols.

`scripts/assignment/d9_cpu_review.py` is the calibration-only comparison entry
point; it must not be replaced by the original evaluator's main/holdout path.
`scripts/assignment/d9_cpu_fit_reviewed.py --center median --output <new-json-path>`
fits the selected production artifact on the retained calibration data and checks
both feature-route and serialization parity. This final fit is not an additional
accuracy evaluation. `scripts/assignment/d9_cpu_review_report.py --center median
--output <new-markdown-path>` reconstructs the report from completed prediction
artifacts without reading raw trajectories or refitting models.

The selected calibration fit is frozen as
[reviewed-workload-model.json](/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T060000Z/d9-cpu-review/reviewed-workload-model.json),
schema `assignment.workload-simulator.v3`. Its adjacent manifest records
**zero calibration-versus-serving mismatches across all 30,711 CPU events**
(maximum delta 0 ms). Reloaded CPU predictions, 31,541 GPU predictions, event
sums, and all 1,083 E2E predictions also match exactly. These are parity checks,
not in-sample accuracy claims. The production regression suite passed 88 tests;
calibration/report-focused tests and `git diff --check` also passed. The root
review independently checked manifest contents and artifact hashes. Grok's
original cache and family-comparison artifact hashes still verify.

The later coverage post-audit is diagnostic-only: original OOF comparison
outputs were not rerun or rewritten. The execution manifest distinguishes the
actually executed harness hash from the post-audit diagnostic source hash.
