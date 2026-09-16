# Independent simulator and evidence review

Reviewed 2026-09-10. Scope was the bounded direct-E2E candidate in
`../e2e/compare.py` and its `report.json`/`model.json`, plus the accepted-case
and native hardware evidence produced by `../evidence/build_d9_evidence.py`.
No rerun or source-data mutation was needed.

## Conditional E2E candidate

The fold construction is sound for the stated development comparison. There
are 819 trajectory rows from 545 instances in five folds. Every instance maps
to one fold, every tool/model row joins an existing trajectory run, and each
test fold excludes its instances from the corresponding training rows. The
source file digests in the report and model match the three output hashes in
the retained training-view manifest. Recomputing the 25% APE and nearest-rank
p95 from `predictions.jsonl` reproduces the report: 647/819 (78.9988%) within
25%, p95 49.8846%, worst 94.9995% for relative NNLS.

The inverse-squared target weighting (`1 / observed_ms**2`) is computed from
training labels inside each fold. It is a training objective, not a predictor
feature, and there is no test-label leakage in the fold loop. The feature
allowlist uses only tool count, request count, input-token sum, and
output-token sum; observed timing fields present in the source rows are not
read by `design()`.

Two limitations matter for delivery. First, the relative candidate was chosen
after comparing the same OOF development predictions, so the bootstrap interval
(+10.52 to +16.15 percentage points) is conditional on fixed predictions and
does not cover model-selection uncertainty or an untouched holdout. The
full-training coefficients in `model.json` are appropriate for deployment
after that selection, but the artifact does not itself carry a machine-readable
`selected_model` field. The simulator binds the intended relative candidate
explicitly; other consumers should do the same.

Second, `output_tokens` are realized historical values in the training view.
Using their aggregate as an input is valid only for the declared full-trace
conditional replay contract. It is not a prospective agent-time feature. The
report and README state this boundary, but a caller must continue to enforce it
when constructing requests. The model records input hashes, but not a hash of
the producer script, so those input hashes establish data provenance rather
than complete code provenance.

The strongest current E2E result is therefore a useful conditional replay
candidate, not literal D9 completion. It predicts direct historical E2E and
does not establish individual CPU/GPU event accuracy, event composition, or
hardware transfer.

## Accepted-case partition and retry scope

The evidence inventory has 55 accepted queue attempts with no duplicate
`(instance_id, case_id, attempt_id)` rows. Partition derivation happens from
the pinned `case_spec.instance_id` before result, validation, or target reads:

| partition | accepted cases | instances | use |
|---|---:|---:|---|
| train calibration | 49 | 25 | native calibration input |
| final evaluation | 4 | 4 | excluded from calibration |
| confirmation/development excluded | 2 | 2 | excluded from calibration |

All 49 training cases have completed results and acceptable validation status
checks. Forty-three original validator reports are clean. Six reports have
only `retry_missing_physical_predecessor`, 24 error rows in total. Independent
inspection of every error event shows `record_role=OBSERVATION` with
`event_class=model_client_call` or `model_request`; none is a native target.
The calibration adapter therefore has a defensible narrow exception for the
native target scope, while retaining the original invalid reports and error
codes.

The exception must remain narrow. Each training case still has exact native
physical-request joins and four finite measured targets (`queue`, `prefill`,
`decode`, `e2e`) per physical request: 2,080 requests and 8,320 native target
rows overall. Per-case checks show each physical request occurs once in each
of the four phase populations. This does not repair the missing wrapper
predecessors, certify every lifecycle/CPU operation, or authorize treating all
telemetry rows as mandatory D9 events. Native phase and direct E2E populations
must stay explicit target boundaries in scoring.

The CPU integrity substitution in the builder checks bounded counts, offsets,
and loss counters for these 49 cases and deliberately skips raw-byte hashing;
the full raw-byte proof is only representative. That is acceptable provenance
for this bounded native fit, but it is another reason not to describe the
bundle as a complete literal-D9 validation.

## Native hardware binding

The builder joins each native clock identity to the remote hardware snapshot
by exact `hostname + boot_id`, and fails on ambiguity within a case. All 49
binding records report one distinct profile binding. Across the training
bundle, the native rows use 9 exact host/boot pairs and 20 raw profile hashes
(20 server identities); all resolve to one static inventory digest:

`stable_gpu_inventory_v1:6eaded0f88a4d31c620e1a18d9ceb9ea6c6d1185781da0ba0d5f7ad352d11ad9`

All 2,080 native rows have a matching host/boot pair. The raw hashes are kept
as provenance; hostname, boot ID, server identity, GPU UUID, job ID, and counter
epoch are excluded from the fit digest. This establishes the observed H100
fit domain and exact binding, not a validated law for scaling to a different
GPU, host, clock, or storage profile. The calibration report's four fitted
native paths are consequently same-domain development models, and their OOF
numbers should not be presented as transfer validation.

## Corrected native scaled-linear model review

The corrected `native/fit_artifact.json` keeps `native:e2e` as the primary
request target and stores queue, prefill, and decode as separate diagnostic
phases. I independently recomputed the metrics from
`native/predictions.jsonl`. The 2,080 request rows have 25 instances, five
instance-grouped folds, and request counts `1303, 175, 310, 131, 161`; every
instance maps to exactly one fold. The dataset, calibration manifest, and NNLS
dependency hashes in the report match the files on disk.

The default `relative_nnls_token` model uses the nonnegative linear design
`[1, prompt_tokens/1000, completion_tokens/1000]`. The cache candidate uses
`[1, uncached_prompt_tokens/1000, completion_tokens/1000,
prompt_tokens*completion_tokens/1e6]` for E2E and queue. Its prefill and decode
diagnostics use the phase-specific designs declared by the native comparison
script. Coefficients are finite and nonnegative, and the design routine reads
only token descriptors; observed timings, residuals, status, and outcome fields
are excluded.

The recomputed primary metrics match the report exactly: the token candidate
has 1,973/2,080 requests within 25% (94.8558%), p95 APE 25.5160%, worst APE
96.6848%, and 7/49 cases with every request within 25%. With an explicit
realized cache trace, the cache candidate has 2,027/2,080 (97.4519%), p95 APE
15.1533%, worst APE 96.7683%, and 19/49 all-request cases. These are grouped
development OOF values; the serialized full-training coefficients are for
conditional replay and do not turn the comparison into a sealed holdout.

The native artifact's one verified static H100 inventory domain and its
`cross_hardware_status: unvalidated` contract are preserved by the simulator.
The default simulator hardware profile is provenance metadata only. The native
adapter does not rescale for it. Completion tokens, and cached tokens when
selected, describe a realized request trace; they are not prospective online
inputs. The cache model is therefore opt-in and must not be described as
predicting future cache state.

## Review disposition

The packaged simulator can safely expose the relative conditional E2E
candidate and the separately scoped native targets with strict identity and
target-population checks. Keep the explicit unsupported status for lifecycle
and uncalibrated transfer requests. No evidence reviewed here supports a D9
PASS claim.

## CPU/lifecycle binding addendum

The local CPU builder was also checked read-only. It binds only the 43
original-valid calibration cases (22 instances), so the six
`retry_missing_physical_predecessor` cases are excluded rather than silently
accepted. For every emitted row, `bind()` verifies the normalization-spec
hostname, boot ID, and clock ID, requires the
`local_proc_sysfs_inventory` source, and replaces the mixed source fingerprint
with a digest of only `architecture`, `logical_cpu_count`, `model_name`,
`kernel_release`, and `system`. The old fingerprint is retained as
`original_profile_fingerprint` provenance. Native rows are explicitly filtered
out before binding.

Independent checks found 14,068 emitted CPU/lifecycle targets, all with
`record_role=TARGET`, one host/boot clock identity, one static local CPU
domain, and no native event class. The 43 normalization specs produce the same
static CPU inventory values (`x86_64`, 32 logical CPUs, AMD EPYC 7B12,
Linux 6.8.0-1066-gcp); no GPU profile field is used as the fitted CPU domain.
The two focused binding tests pass. This supports the binding correctness and
the absence of GPU-to-CPU attribution, while the resulting paths remain
same-domain CPU/lifecycle models and do not certify individual atomic CPU
operations or the complete D9 population.

The assignment target boundary treats native per-request E2E as the primary
GPU target; queue, prefill, and decode remain component diagnostics unless
explicitly declared as the scored population. The earlier log-token native fit
is rejected and is not used by the simulator. Cache variation remains an
explicit conditional feature or limitation, not an implicit transfer claim.
