# Final bounded pre-freeze decisions

Main-agent decision record. This continues the repaired September 8 checkout;
it does not replace the literal PDF, predeclared panels, final-run design or
historical artifacts. No production launch or final freeze has occurred.

User clarification at 02:51 UTC: the historical 5%/10% perturbation thresholds
are internal safeguards, not assignment requirements. Their failed results
remain retained. Current 64–69% perturbation is not waived. Main will set and
document a defensible criterion after measured optimization, using event
fidelity and workload representativeness; no new threshold is accepted yet.
The literal ≤25% per-individual-event and E2E prediction-error requirement is
unchanged. No required raw measurement may be sacrificed for the overhead gate.

The execution target is 22 H100 workers, 00–10 and 12–22; worker 11 is
excluded. The configure-worker report establishes current availability.
Final endpoint/GPU/process/source bindings and measurement isolation still
require preflight evidence. Preserve existing VPN and SSH relays.

## Fixed scope and stopping rule

Keep Qwen3-Coder-30B-A3B-Instruct and the four predeclared configurations,
24 development instances and 96 confirmation trajectories. Keep the exact
800 baseline plus 288 independent sweep executions. No final/evaluation
outcome tuning and no optional A100/H200 campaign. Historical development
mining must use the five-manifest exclusion union before source/label access;
retain the earlier access disclosure. Subagents recommend; the main agent
accepts changes and owns selection, methodology and freeze.

Close the finite evidence-backed defects below, validate the actual stack,
execute the declared comparison, select and freeze, then launch. An additional
change requires a demonstrated quality or rerun-prevention defect and a
bounded validation path; general possible improvements are insufficient.

## Decision register

| Issue | Evidence | Expected impact | Risk | Implementation | Validation | Decision |
|---|---|---|---|---|---|---|
| Confirmation plan cannot execute | Actual first saved v2 confirmation spec is rejected by the current runner; 96 spec/reference hashes match | Enable declared comparison without changing coordinates | Incorrect identity/config mapping | Dedicated schema adapter and dispatcher binding original declarations | Negative identity/hash/settings cases, actual first development execution | Adopt adapter; review implementation before use |
| Collector discovery recurses into required capture before attachment | Actual pinned Docker hook probe needed an explicit target to avoid discovery interception | Prevent all live cases failing before capture starts | Unmeasured discovery or broad bypass | Narrow bootstrap sequencing with retained lifecycle timing | Repeat real Docker probe without supplied target; no required action loss | Adopt bounded sequencing fix |
| Overhead adapter omits deferred completion time and production hooks | Source review found work timer stopped before service finalization; direct Bash path bypasses some production hooks | Prevent falsely passing perturbation gate | Understated timing or fake full-capture flag | Charge completion; require actual full hook path for acceptance, keep component diagnostics labeled | Full on/off immutable fixtures; coverage and durability checks | Adopt correction; component-only results cannot pass full gate |
| Metric snapshots lack fetch brackets | ServingSnapshot stored only one response-side timestamp; network/parser interval absent | Preserve conservative measurement window for independent attribution | Small timestamp overhead, schema consumers | Add scrape start/end/phase; reject clock mismatch, reversed and overlapping windows | 40 serving/proxy/witness tests plus two subtests passed; live proof pending | Adopt |
| Independent serving witness not yet produced on real server | Existing witness producer consumes a strict external log; no deployed observer binding found | Obtain defensible per-request native queue/prefill/decode attribution | False exclusivity, mixed clocks, asynchronous witness race | Pending main decision on actual server observer and deferred derivation | Unrelated concurrent request, failure/retry, clock/epoch and missing log tests; actual vLLM fixture | Unresolved; do not invent lease proof |
| Parallel assignment ownership/recovery | Existing serial scheduler does not implement requested persistent shared queue | Keep 22 endpoints occupied without duplicate acceptance or skipped cases | Concurrent claims, orphaned live child, lost durable output | Durable shared queue and endpoint ownership; preserve runner attempts | Multiprocess claims, crash/reconcile, corruption, no quality retry | Adopt queue requirement; implementation under review |
| Numeric CPU index recorded as model name | Actual /proc/cpuinfo starts with processor:0 before AMD model name | Correct D8/D9 hardware provenance | Low | Prioritize model name/hardware/textual Processor | Four platform fixtures passed; archived live AMD EPYC 7B12 inventory | Adopted |
| Budget termination misread as unresolved | Bound panel outcomes include two resolved additions; eligible historical report separates outcome/exit reason | Avoid unsupported blanket limit increases or panel reselection | Extra-call cost/regressions remain unknown | Correct description, keep IDs and four candidates | Existing 24-panel disjointness; official paired comparison pending | Adopt correction; defer final config choice |
| Running server context differs from predeclaration | Read-only fingerprints on 11 Slurm nodes found all 22 serving processes using --max-model-len 32768; comparison requires 65536 | Avoid systematic context failures and invalid comparison | Restart/loading and VRAM capacity | Controlled one-endpoint correction to declared 65536, then fleet rollout after proof; preserve model and relay settings | Native model/context response, long-context fixture, GPU/process binding, effective argv and raw config hash | Adopt correction; health checks alone cannot close this gate |
| Per-case dependency resolution mutates execution environment | Current pinned SWE-agent checkout has uv-generated untracked lock; default project run synchronizes dependencies | Stable dependency/source behavior across parallel cases | Reused environment imports wrong checkout | Set UV_NO_SYNC for reviewed v2 agent; prepend validated project path; clean isolated pinned checkout | Actual clean-checkout import remained clean; 15 runner integration tests passed | Adopt; package/environment fingerprints still required |
| Tool-host CPU terms can come from remote GPU host | Acquisition review P3 finds remote CPU frequency enters local tool model projection | Prevent false CPU platform identity/transfer | Unavailable local clocks | Use actual tool-host provenance or explicit unavailable frequency | Deliberately conflicting local/remote frequency test | Adopt source-binding fix, pending implementation |
| Actual container CPU/I/O/throttling context absent | P2/P5: existing resource sampler is recorder RUSAGE_SELF, filesystem syscall bytes do not measure physical device traffic | Explain test/script compute and shared-CPU contention without another run | Extra sampling cost; wrong cgroup attribution | Bounded identity-bound cgroup CPU/io/pressure and effective quota/affinity samples | Busy/sleep/cached-read fixture; identity negatives; include overhead | Adopt bounded context capture, pending implementation |

## Required synthesis before freeze

Current component disposition, superseding earlier pending implementation rows:

| Issue | Evidence | Impact | Corrective action | Validation | Remaining risk |
|---|---|---|---|---|---|
| Incomplete/ambiguous queue completion | Reproduced capture acceptance, replay, archive audit, child ownership and malformed metadata defects | Prevent accepted corrupt cases and duplicate execution | Common strict classification, immutable evidence checks and retained uncertain ownership | 25 tests, 29 subtests; final Astra review accepted | Actual worker/storage/runtime bindings remain required |
| Dataset hash attached to wrong serialization | Every pinned Parquet row reproduces exact retained JSONL bytes | Avoid false pin failure without changing tasks | Bind runtime JSONL hash, retain separate Parquet hash | All 800 rows verified | Source files must remain retained |
| Live evaluator and CPU placement | Existing permitted Django7530 patch evaluated by clean pinned official harness | Prove evaluator imports, outcome and actual container affinity | Explicit project/pin/import binding and control-pool Docker cpuset | Live resolved result, 57.74 seconds, retained logs and CPU samples | Single fixture does not certify every repository image |
| Full CPU collector perturbation | Fixed file/test pairs show 64.17% / 68.50% median overhead, zero record loss | Launch remains blocked by perturbation | Profile exact full-hook path and adopt only measured reductions | Twelve original conditions retained; diagnostics in progress | 5%/10% gate remains unchanged and unmet |
| Worker22 replacement startup | vLLM 0.10.0 rejected UUID CUDA selector; finite recovery hostname assertion then failed | Prevent repeated fleet startup failures | Preserve original numeric CUDA selector, eight-CPU affinity and exact checkpoint FQDN | Binding regressions: two tests, eight subtests; v4 model loaded, readiness pending | Twenty-one other endpoints await reviewed rollout |

Append accepted/rejected findings from the bounded development scan,
acquisition review and production-systems tests. Report official paired
wins/losses and regressions for all four complete candidates before selecting.
Keep historical D9 v3 as the failed benchmark, missing E2E phases explicit,
and prospective versus observed-work replay features separate. No fitted
residual may substitute for missing evidence. D9 final accuracy is measured
after acquisition, not declared successful by these prelaunch tests.

All D1–D9 final summaries, figures and explanations require collected results;
their raw inputs and executable export paths must be proven before launch.
See ALL_DELIVERABLE_READINESS_20260909.md for the literal PDF mapping,
including both uses of D4 and the figure examples on page 2.

## Main-accepted DEV and execution decisions — 2026-09-09

Main-accepted decisions. The completed historical scan supplies the evidence
below; its historical rows are not a new mining task. Live acquisition validation
is separate and is not a launch or freeze approval. The DEV decisions authorize
observational capture; the scan's proposed recovery, replanning and continuation
interventions do not authorize new agent behavior.

Historical-access disclosure remains: “Initial forensic pass accessed
evaluation-cluster historical rows; filtering does not restore blindness.”
The five-manifest exclusion union still applies before further historical
source or label access. Evidence below reuses the
[completed bounded DEV scan](FINAL_DEVELOPMENT_FAILURE_SCAN_20260909.md),
[queue review](SHARED_CASE_QUEUE.md) and accepted serving review findings.

| Issue | Evidence | Impact | Action accepted by main | Validation | Risk |
|---|---|---|---|---|---|
| DEV-01: editor errors/no-op edits | 91 events in 67 cases, including 15 resolved controls | Distinguish failed editing from final repair outcome | Capture editor errors and edit outcomes; reject prompt changes and forced fresh views | Completed scan retains cited source/action/output hashes and resolved controls; validate new raw capture through live fixtures | Recoverable editor errors must not become capability-failure labels |
| DEV-02: repeated actions | 21 repeat-signal cases: 11 unresolved, 10 resolved | Expose repeated work without interrupting legitimate testing | Capture action plus observation hashes and intervening mutation evidence; reject automatic loop-kill or forced replanning | Completed scan includes resolved repeat controls; repetition remains a derived signal, not an intervention | Identical actions alone do not prove a loop |
| DEV-03: output truncation | Six length markers across five cases, including two resolved controls | Attribute incomplete provider output | Capture exact finish_reason=length with physical request/token lineage; preserve output limit and existing behavior | Completed scan retains six markers and separate outcomes; raw response/finish-reason linkage requires live proof | Truncation does not establish that a higher output limit improves resolution |
| DEV-04: budget/context termination | Two client-limit/context-exit examples officially resolved | Keep termination separate from quality | Capture budget crossings, exit reason and retained patch/evaluator outcome; reject unsupported blanket budget increases | Completed scan and corrected panel description separate exit from outcome; live official evaluator passed | More budget has unmeasured costs and regressions |
| DEV-05: late positive local tests | 26 officially unresolved cases contain late positive local-test output | Preserve local/official disagreement | Capture command, validation scope, output hash and official outcome; these cases are not proven near resolution | Completed scan retains the cited local/official disagreement; no further historical reread needed | Local success text cannot prove proximity to resolution or support candidate selection |
| DEV-06: retries/infrastructure | 2xx prefixes followed by 4xx or disconnections; a resolved control ends in 400 | Separate infrastructure attempts from patch quality | Capture logical/physical request and retry-parent lineage, status, error phase and duration; retain declared retry policy | Completed scan retains failed sequences and resolved control; bounded live retry fixture remains required | A transport error is not a model-failure label |
| DEV-07: timeout attribution | 69 broad timeout flags; zero strict structured worker-timeout events | Avoid remediation based on source-text mentions | Capture structured timeout kind/phase, command, elapsed time, exit code and request/case identity; reject unsupported blanket timeout raises | Completed scan reports zero strict events; persistent-shell live timeout/interrupt/recovery fixture passed | Zero strict events does not prove no timeout occurred |
| Shared queue correctness | Review reproduced completion-integrity, replay and archived-member audit failures; ownership/binding gaps also identified | Prevent duplicate execution or acceptance of incomplete evidence | Accept bounded queue fixes; validation remains pending | Reproduced negatives, crash/child fencing, source/endpoint binding, immutable coverage and retry-policy checks | Earlier passing tests do not close newly reproduced defects |
| Serving evidence correctness | HTTP-failed metrics produced measured values; contradictory capture/watermark times passed; parser bracket and optional-registry bounds were incomplete | Prevent false native attribution and understated overhead | Accept scrape-failure/bracket and journal-consistency fixes; keep optional registry sampling disabled; native per-request hook implementation remains under review | Pending: exact regressions, raw/identity/clock bindings, hook completeness and bounded worker-22 proof | Delayed aggregate publication remains ambiguous; native serving durations are not GPU kernel time |
| Fixed tool-CPU policy | 22 GPU workers share a VM with 16 physical cores/32 logical CPUs | Fix placement consistently across comparisons | Workers 00–10 → CPUs 0–10; workers 12–22 → CPUs 16–26; control/eval → 11–15,27–31. No new CFS quota or thread multiplier; same policy for confirmation, overhead and production | Pending: actual container affinity/cgroup evidence and implementation review | Worker pairs share SMT cores; disjoint logical CPUs do not provide exclusive physical cores |
| Mandatory experiment scope | Four declared candidates, 24 development instances, 96 confirmation trajectories; 800 baseline + 288 sweep executions | Preserve the declared comparison and production coverage | Keep model, panel, settings and 96/1088 counts; reject a fifth candidate, prompt changes and unsupported blanket budget/output/timeout increases | Official paired comparison, selection/freeze and complete execution coverage remain pending | Capture improvements do not establish resolution gains or D1–D9 completion |
