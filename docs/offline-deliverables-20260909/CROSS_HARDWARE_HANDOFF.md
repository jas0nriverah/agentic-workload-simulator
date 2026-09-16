# Candidate handoff for later hardware validation

This packet is an offline development candidate and control, not a production replacement or D9 acceptance result. Acquisition remains closed. No GPU inference or hardware trial was used to build it.

## What is ready

The package contains a serialized CPU tool-duration predictor, a feature-free historical request-wall baseline, and a direct start-known historical E2E baseline. Coefficients are fitted only on the declared training partition. Per-event and per-run grouped predictions are retained. The native adapter has been exercised against 40 real physical requests; the semantic action/runtime population split is explicit.

Historical CPU tool wall, historical request-proxy wall, native queue/prefill/decode service, auxiliary runtime commands, and individual CPU operations are different targets. The package does not silently apply a fitted historical proxy coefficient to a native phase or syscall. Native-phase, atomic-operation and lifecycle coefficients are not fitted: the inspected repaired real attempts repeat a confirmation-excluded instance and provide no independent training/validation cohort for them.

## Bind before opening destination labels

1. Fix the candidate bundle hash, prediction code/extractor hashes, target boundaries, instance partition, and feature whitelist. Keep every attempt/configuration of an instance in its assigned partition. Preserve the earlier historical-access disclosure.
2. Record source and destination host/device/model/tokenizer/serving fingerprints. Treat unmodeled hardware change as an explicit transfer test of unchanged coefficients, not an inferred frequency/core/bandwidth scaling law. The current candidate supplies a hardware-unaware control; it is not a validated hardware-specific simulator.
3. Record each prediction with case, attempt, action or physical-request identity, component target, feature values and their pre-event provenance, model hash, source hardware identity, destination hardware identity, and prediction timestamp. Never attach the current observed latency, generated length, completed cache/work counter, future state, or residual as an input.
4. For CPU predictions, derive the current action descriptors through the saved extractor. For the selected request baseline, no prompt/output feature is required. Prompt-based alternatives remain diagnostics until a pre-request count is demonstrably available; do not substitute the terminal usage value. Future events may use only already-revealed earlier information under a separately declared sequential contract.
5. Score only after predictions are committed. Publish per-event absolute percentage error, within-25% indicators, worst error, per-class support/missing/censoring counts, E2E error, and the full event/E2E conjunction. Unsupported required targets remain unsupported; they are never dropped to improve the denominator.

## Composition and stopping conditions

Use a disjoint component ledger with physical request IDs, retries, worker/endpoint/host identity, and clock domains. Component-class presence alone is not proof of interval disjointness or complete coverage. The adapter smoke checks the interface; it does not certify a future execution's ledger. Native and client wrappers must not count the same physical request twice.

The retained event-sum diagnostic is conditional on the realized event inventory and lacks a calibrated lifecycle component. It is not a start-of-run forecast. The direct E2E baseline uses only start-known information and predicts the total directly; it is not an explanation of lifecycle time. Neither path inserts measured residual time.

The present candidate fails universal D9 accuracy and cannot be labeled D9-compliant. The next useful evidence is independently partitioned repaired execution data from the already declared process. Repeatedly tuning centers against this historical cache cannot identify missing hardware or lifecycle laws. This offline pass stops at the bounded comparisons and reviewable artifacts; it initiates no new campaign.
