# D9 feasibility reassessment against the original PDF

Authority: `/home/riverahernandezjason/Coding tests Harrdware (2).pdf`, both
pages reread September 14. This reassessment changes no data, acquisition,
model default, event denominator or acceptance result.

## Verdict

Current candidates do not meet D9. The evidence does **not** establish that
D9 is impossible, or that the retained evidence cannot support a better
assignment-level simulator. Do not describe unsuccessful categorical/median
experiments as a proof of impossibility.

## Literal requirements and interpretation limits

Step 4 follows the Step 3 CPU/GPU event analysis: update hardware parameters
to characterize another platform. D9 requires configurable relevant CPU/GPU
parameters, generation of all Step 1–3 figures, individual-event error within
25%, and E2E error within 25% on the evaluator's server.

The PDF does not mandate 96/1,088 executions, an A100 specifically, or forecasting
future agent actions/output length from the initial issue. A supplied workload
trace is a defensible interpretation, not an explicit evaluator-interface
agreement. Target-machine measured latency, residuals, and realized cache state
cannot silently become hardware-independent inputs.

Event granularity is not defined precisely. Step 3 explicitly includes individual
file reads, writes and traversal and GPU token/context descriptors. The PDF does
not explicitly require a separate prediction for every telemetry span or GPU
queue/prefill/decode subspan. Their bad coverage alone is not proof that D9 is
impossible. Conversely, regrouping operations or deleting difficult events after
seeing errors cannot establish compliance. Keep the original Step 3 event
definitions visible and report operation/subphase diagnostics alongside them.

## Actual implementation gap

`docs/d9-salvage-20260910/simulator/d9_simulator.py` serves the repaired semantic,
native phase and conditional E2E models. The repaired paths reject changed
hardware profiles/domains. This is honest behavior but does not implement the
required transfer behavior for those models.

`src/agentic_sim/assignment/workload_simulator.py` already contains a distinct
hardware-aware path: GPU token terms scaled by bandwidth/compute, legacy CPU
work terms, and semantic CPU scaling through an assumed serial fraction and
frequency ratio. These are existing assumptions, not established transfer
accuracy. The common simulator's assignment targets require a supplied v3
artifact. The recent repaired component improvements are not one demonstrated
hardware-transfer/event-composition/figure-generation solution.

The 42/43 repaired E2E result is a direct conditional regression, not proof that
individual event models compose correctly. Atomic/lifecycle experiments remain
separate; the latest entry-feature checks did not close their errors. Even a
more permissive event interpretation does not automatically make CPU action
predictions pass.

## Bounded next completion test

1. Bind one existing repaired Step 3 example to an explicit CPU/GPU operation
   inventory and disjoint E2E dependency accounting; retain all underlying raw
   operation/subphase diagnostics. Do not select event definitions by error.
2. Reconcile the existing hardware-aware workload path with the repaired
   component fits. Identify which work descriptors and hardware sensitivities
   are actually supported; do not fit interchangeable hardware effects from
   one fixed platform and call them identified.
3. Reconstruct that example through the simulator from workload descriptors
   and hardware inputs, without measured target durations or residual. Verify
   train/serve parity and event/E2E/figure outputs, then evaluate on the existing
   grouped development population rather than reporting one favorable example.
4. Freeze a coherent candidate before opening protected final-evaluation cases.
   Request additional measurements only for a concrete unresolved mechanism
   or hardware sensitivity; another full production matrix is not justified by
   this reassessment.

This is a route to testing the assignment formulation properly, not a promise
of a 25% pass. Poor CPU predictions, hardware sensitivity, and complete plot
generation remain real gaps. A new GPU platform alone will not fix them.
