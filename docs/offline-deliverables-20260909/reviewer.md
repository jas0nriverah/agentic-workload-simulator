# Independent offline methodology review

Reviewed protocol, common-view builder and hashes/folds, start-known E2E implementation/output structure, GPU comparison and adapter, figure derivation, and CPU predictor as it arrived. No acquisition audit, raw trajectory access, GPU work, or new research was performed.

The common view has 23,245 CPU events, 23,868 historical GPU-proxy events and 819 trajectories, each spanning 545 observed instance clusters. Its manifest declares 546 training clusters. Independently recomputed output SHA-256 hashes and every instance outer-fold assignment match. The builder selects authorized identities before decoding complete target/action records; the scope helper binds the production split manifest. These are historical development data, not a pristine holdout.

Start-known E2E uses only repository at inference. Its inner selection operates on outer-training instances, and all instance copies stay together. Component wall times, realized future event counts and residuals do not enter its predictor. It correctly packages direct E2E regression separately from event composition. The completed-cache intersection cannot establish the original all-required-event denominator.

Findings sent immediately to integration:

- GPU ridge initially added regularization to entire non-intercept rows of the Gram matrix. The source was subsequently corrected to diagonal-only non-intercept regularization; results must be regenerated from that version.
- GPU initial outputs lacked original-event predictions/indicators and a refitted candidate package. Those are necessary for the protocol's reporting/package requirement.
- Historical GPU prompt-token availability is assumed, not established by the repaired fixture. Prompt-based results must remain conditional historical-proxy diagnostics unless pre-request provenance is independently supported. Native phase/lifecycle accuracy remains unsupported.
- CPU `metric_rows` initially recursed indefinitely when computing its own `by_original_class` subreports. A one-row synthetic call reproduced `RecursionError`. CPU selection also initially inserted p95 before complexity, contrary to the locked protocol. Both require correction before comparison output is accepted.
- CPU draft documentation initially called its final package production and referenced a missing training-view protocol path; it must describe an offline candidate and the actual parent protocol. Preceding-action features must reset by run/trajectory and require adjacency, not carry across copies of an instance.
- Figure derivation initially materialized all `sweep_runs.csv` rows before filtering eligible run IDs, inconsistent with its strict identity-before-label parsing claim. Eligibility must gate target-row decoding or the access disclosure must describe that limitation accurately.

The figure packet correctly distinguishes command wall from native engine service wall, discloses D4 matched populations and repeated panel baseline coordinates, and does not claim kernel timing, hardware transfer, or a new D8 selection. D2/D3/D5/D6/D7 are provenance pointers, not newly corrected case-level recomputations.

Composition adapter is a contract smoke, not proof of complete event coverage: its `status=proven` ledger flag is supplied by the caller and its presence-of-class checks do not independently validate interval disjointness or enumerate missing required events. Do not use synthetic composition totals as measured model accuracy. No reviewed predictor uses a measured residual as an input.

Review remains contingent on integration's corrected CPU driver/results and regeneration/package checks. No D9 universal-accuracy acceptance is established by this packet.

## Integration follow-up

GPU ridge correction and full-training grouped-inner selection are present. The regenerated 23,868-row OOF export has unique run/request keys; its per-event indicators and aggregate coverage match independently recomputed values. The selected global median package is correctly feature-free, has an explicit historical proxy target, source hash, hardware limitation and offline-only status. Its prediction API rejects forbidden target inputs. Four E2E candidate tables likewise reproduce their reported counts, coverage and worst APE.

CPU metric recursion and protocol tie-breaking are corrected. The optimized coverage-center initially omitted observation candidates; that was corrected and independently checked against exhaustive scoring on 100 deterministic small fixtures, including repeated unit targets. The new CPU driver uses grouped inner selection on each outer training set and again on the full training partition for packaging. It explicitly disables the preceding-mechanism feature because sequence order is unsupported; the named prior candidate therefore aliases the static mechanism comparator. No residual, future action or current-event target enters the reviewed predictor paths.

Figure source now explicitly discloses structural CSV parsing before eligible-only numeric conversion, and labels the current native fixture as confirmation-excluded/descriptive only. This is an accurate access limitation, not restored blindness. No new raw trajectory was accessed for this review.

## Final CPU output check

The final CPU nested export contains 23,245 unique original event IDs. Independently recomputed per-event indicators, coverage, worst APE and predicted duration sum match `comparison.json`. Every outer-fold selected candidate and the full-training selected candidate match the fixed selection rule applied to their recorded inner metrics. All 23,245 actions have supported extracted features; missing original events outside the completed cache remain unknowable, not zero.

The packaged CPU candidate is `coarse_class_median`; nested CPU coverage is 67.7436% and worst APE is 481.0806%. Four retained runs have every recorded CPU event within 25%, which does not establish complete original-event or E2E success. GPU nested proxy coverage is 34.6321%; direct E2E nested coverage is 65.6899%. None meets a universal event-level requirement.

No blocking unresolved implementation issue was found in the final reviewed model outputs. The packet remains an offline historical candidate comparison, with native lifecycle accuracy, complete event-composed E2E accuracy and cross-hardware transfer unsupported. This review is bounded to the files and checks described above, not an acquisition audit or production acceptance.

## Final bounded class-hybrid extension

Reviewed the sixth adaptive class-specific procedure and its separate outputs. Each outer fold's class map is selected exclusively from grouped inner predictions on that outer training partition; each chosen base model is refit on outer training before predicting outer test. The final packaged class map is selected from grouped inner predictions over the full authorized training partition. No outer-test labels choose that fold's class map.

Independently reproduced every outer and final class choice from its recorded inner metrics, verified every exported event's candidate routing, and recomputed the 23,245 unique-event export's within-25 indicators, coverage and worst APE. Coverage is 68.50505485%, a 0.76145408 percentage-point gain over the coarse comparator, with unchanged worst APE of 481.08059438%. The full-training map uses coverage-center for patch/read and coarse medians for the other five classes. Reloading serialized component models routes all seven classes correctly; poisoning target, output, future-state and residual labels leaves predictions unchanged.

This procedure was added after the initial diagnostics. Its grouped nested computation avoids direct cross-fold selection leakage, but its development score is not an untouched confirmation estimate. Root's protocol explicitly discloses that adaptive extension. The hybrid remains an offline candidate only; the same historical target, incomplete-denominator and hardware limits apply. No seventh candidate or additional data acquisition was reviewed or requested.
