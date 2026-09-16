# Repaired D9 calibration adapter

`calibrate_repaired_d9.py` is callable as `calibrate(normalized_manifest,
output_dir)`. Its `inventory(case_roots)` helper returns case-spec identity and
the pinned derived partition only; it never opens event, result, or validation
files. The CLI is:

```bash
python3 docs/offline-followup-20260909/calibration/calibrate_repaired_d9.py \
  --manifest /path/to/pipeline-manifest.json --output-dir /new/output-directory
```

It trusts neither the caller's partition nor its identity proof. Before it
opens a normalized event file it checks the case root and `case_spec.json`,
derives the instance partition from the pinned production split manifest (with
required SHA-256 `0b0c37147b45ec824e2af45d82ba20b3ed57ac56b64f58ea07004e0c13bcc99f`), and
admits only `train_calibration` instances. Confirmation and other excluded
cases produce a pending report without their event journals being opened.

The ledger event input is JSONL with an explicit `record_role`; only the exact
value `TARGET` is eligible for fitting. Other normalized observation/context
rows are preserved but never inferred to be labels. TARGET rows contain
`instance_id`, `case_id`, `attempt_id`,
`event_id`, `event_class`, `target_boundary`, `observed_ms`, start-only
`features`, `feature_provenance`, `host_id`, `clock_id`, optional
`physical_request_id`, and `partition`. Each pipeline case also supplies
`events_sha256` and `validation_report_sha256`; both are verified after
eligibility and before parsing. `observed_ms` is the target, never a
feature. The model allows only bounded, pre-event categorical feature buckets.
The current ledger emits this same allowlist directly. For an older compatible
ledger record, `mode: prospective` is retained as a contract check and
discarded, while pre-event `input_tokens` is converted to a fixed prompt-token
bucket. Any other feature is rejected without a partial fit.
It produces separate median paths for each event-class/target-boundary and
hardware-fingerprint domain. `host_id` never defines a hardware domain.

A path needs at least 25 events from three independent instances. Evaluation
uses instance-grouped folds. Zero targets remain zeros: a zero target passes
the 25% test only when the prediction is zero; otherwise the report records an
infinite APE explicitly and does not epsilon-smooth it. A complete queue,
prefill, and decode set produces `native_component_sum_oof`, which is not
native E2E or outer E2E. `outer_e2e_oof` remains unsupported until a separate,
eligible outer-E2E target path exists.

Current retained proof cases are confirmation-development-excluded, so the
real invocation is expected to emit `pending_no_fit` until independent repaired
`train_calibration` cases arrive through the ledger pipeline.

The current ledger feature provenance establishes pre-event availability but
does not provide a native hardware-profile fingerprint. Its targets therefore
parse under `profile_domain_unknown` and cannot be fit or pooled across hosts.
The report records this as unsupported; a future fit needs a declared native
hardware fingerprint bound to every target row.
