# Offline candidate development protocol

This is the newly authorized offline modeling and figure-preparation task after acquisition closure. It changes no acquisition implementation, frozen model, production snapshot, configuration candidate, split, or GPU state. The result is a separate candidate package for later validation, not an adopted production model.

## Data and separation

Fit and score historical model experiments only inside the predeclared `train_calibration` partition. Exclude the 137 protected instance clusters and 451 explicit identities through the pinned scope helper. Keep all suite copies, configurations, retries, and events of an instance in one fold. Preserve the prior-access disclosure: these historical data are development evidence, not a newly blind holdout.

The common training view scans encoded mixed cache structure and decodes identity fields before decoding eligible row targets or actions. It does not deserialize excluded row targets, open excluded raw trajectories, or use them in fits/scores. The view carries labels for scoring; individual predictors must explicitly whitelist their input fields. Source and selected-view hashes bind the experiment.

Use five outer folds with the already established SHA-256 instance rule. If choosing among candidates, use three deterministic instance-grouped inner folds on each outer training partition; choose without the outer test targets. Show fixed-candidate diagnostics separately from the nested selected-procedure estimate. Prior outer-fold exposure prevents claiming a pristine final test, even when the new computation has no cross-fold leakage.

Bound the CPU comparison to at most six simple interpretable candidates. Select on inner within-25% coverage subject to no worse maximum percentage error than the coarse class-median comparator on the same inner predictions; break ties by lower worst error then simpler model. If no alternative meets that rule, keep the comparator. Do not change this rule after inspecting outer scores. Refit the selected candidate on authorized training data only for packaging; cross-hardware validation remains outstanding.

Final bounded extension: after the first five CPU candidates showed improved coverage but heavy-class tail regressions, evaluate one sixth, class-wise composite procedure. Within each outer-training partition, apply the same inner coverage/worst/simplicity rule separately to each original CPU class; an unsupported class keeps the coarse comparator. The outer test never chooses its class model. Record the initial analysis informing this extension and report it as adaptive development with grouped validation, not untouched holdout confirmation. No seventh candidate or new feature search follows.

## Prediction-time contract

CPU inputs may describe the current submitted action, declared operation/runner/scope, and validated state already known before that action. Identity and command hashes are joins, not learned case/command lookup features. Current duration, completed work counters, future file/script state, exit status, and evaluator outcome are forbidden inputs.

GPU inputs may include the prompt length and requested output cap only when bound to the request before generation. Realized output length, native completed cache counts, measured queue/phase timing, and client timing cannot be substituted as prospective features. Historical proxy targets and repaired native-phase targets remain separate. Missing pre-request features make a model unsupported; no guessed feature value silently fills the gap.

Lifecycle models require independently supported, named phase targets and pre-available descriptors. Do not use or fit the measured residual as an input, transfer its observed complement into a prediction, or silently relabel it as startup. If there are too few independent repaired instances to validate lifecycle models, retain an explicit unsupported result rather than fit a constant from the validation fixture.

Event-sum E2E evaluation conditioned on a realized future action/request list is not a prospective start-of-run forecast. Any such diagnostic must be labeled as conditional; the primary prospective E2E claim may use only start-known inputs. Distinguish direct start-known E2E regression from event-composed E2E and report incomplete composition explicitly.

## Reporting and acceptance

Report every supported original event's within-25% indicator, mean/p95/worst absolute percentage error, sample size, mechanism class, instance, and trajectory grouping. Preserve unsupported/censored/missing-event counts. Report CPU sums, GPU sums, E2E within-25%, and the conjunction of every required event and E2E where fully measurable. Coverage percentages cannot replace D9's universal criterion.

The repaired CPU/GPU/lifecycle adapters must preserve semantic actions versus auxiliary runtime commands, physical-request identity, host/clock domain, retries, and overlapping intervals. A replay/smoke result is evidence of adapter correctness only. Repeated fixtures on one instance do not validate generalization. No CPU-frequency, core-count, bandwidth, or GPU scaling law is inferred from one platform.

Corrected D1–D8 outputs are descriptive retained-evidence artifacts with explicit populations, units, denominators and provenance. They are not final production results or a new D8 example selection. Stop when bounded comparisons, a reviewable candidate, and directly useful corrected inputs are complete; do not widen into prompt search or speculative telemetry/model development.
