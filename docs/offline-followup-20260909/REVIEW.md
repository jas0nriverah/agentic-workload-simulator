# Independent offline follow-up review

Scope: the new offline pipeline and its actual fixed inputs. This is not a new acquisition audit. The review does not open raw held-out labels, acquire GPU data, or change frozen artifacts.

## Statistics

The fixed CPU uncertainty implementation pairs events on event, run, instance, and fold identity, verifies matching targets/classes, and uses common instance draws for both predictors. Every run and event within a drawn instance travels together. The event-weighted and instance-weighted metrics have distinct denominators; the strict gate remains a conjunction over retained CPU events. CSV/report deltas and intervals are percentage points. The report explicitly limits inference to the completed OOF predictions and states the adaptive hybrid selection limitation.

The two statistics regression tests passed. The generated fixed-data report contains 23,245 events and 545 instances, event-weighted delta +0.7615 pp with conditional 95% interval [0.4462, 1.1288] pp, and strict gate 0/545 for both predictors. This does not establish a literal all-required-event gate or bounded worst-case error.

## Integration review

The initial ledger/calibration interfaces disagreed on validation status, source-hash shape, feature provenance, native component names, generated output locations, and which normalized rows are targets. The revised calibrator reads the nested validation status and hash list, accepts generated output files after the identity/partition gate, and selects explicit `TARGET` rows. It pins the split-file SHA-256 before opening case metadata. Unknown hardware profiles remain unsupported and every fitted path requires sufficient independent training support within its grouped folds. Native component-sum metrics are explicitly separate from native and outer E2E.

The four root pipeline tests and five calibration tests passed during final review. They cover rejection before protected journal access, failed-ledger stopping before figures/statistics, explicit target roles, actual ledger-shaped rows, feature leakage rejection, and honest confirmation-only pending behavior. Root orchestration validates selected ledgers before rendering; inventory metadata alone cannot authorize journal reads. The renderer reports separate overlapping boundaries and emits no unproven evaluator score.

The real repaired-case branch must remain pending: the known three independent repaired instances belong to confirmation/excluded partitions, leaving no eligible training cases. Partition and case-spec checks must precede calibration event-file reads. Synthetic fit fixtures establish code behavior only, not actual repaired-domain calibration.

The ledger validation scope is completed native-backed requests, requiring a physical/native bijection. It is not a general proof that failed-before-server attempts have native records or that every required atomic event is normalized. CPU raw coverage is bounded artifact integrity; full atomic BPF normalization remains explicitly unsupported. A clock-domain union does not establish complete decomposition of outer E2E.

Final ledger review confirmed the retry self-reference guard excludes absent retry IDs, native phases reject missing, nonfinite, negative, and boolean values, and required terminals cannot pass when their entire start population is absent. Paired records require consistent repeated start timestamps. Multiple-attempt selection uses the exact accepted-attempt field. CPU hardware features require a matching host/clock snapshot completed before the event; native hardware features require remote profile host and boot identity. All 11 ledger regression tests passed independently.

The final real-input regeneration in `output-v4` completed successfully with `analysis_complete_calibration_pending`, one validated case, zero eligible inventory cases, and no acquisition or GPU inference. Its calibration report has no input errors and excludes the confirmation case before calibration event reads. Earlier failed or interrupted output directories are not accepted results. The scoped offline implementation review passes. This scoped review does not reopen or replace the prior closed measurement-completeness decision; repaired-domain fitting, cross-hardware scaling, blind-test accuracy and D9 compliance remain unproven here.
