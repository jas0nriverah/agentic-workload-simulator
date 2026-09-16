# D9 repaired production evidence

This bundle is the concrete calibration input for the accepted repaired runs. It
is derived from the pinned production split before event/result labels are read.

The accepted queue contains 55 cases:

- 49 `train_calibration` cases covering 25 distinct instances;
- 4 `final_evaluation` cases;
- 2 `confirmation_development_excluded` cases.

The fitting manifest is [calibration_input_manifest.json](calibration_input_manifest.json).
Its SHA-256 is
`8c4f3a156d90e2d4b6f4c608f49dde647719096f9c059c5889c65a3598588afd`.
The pinned split SHA-256 is
`0b0c37147b45ec824e2af45d82ba20b3ed57ac56b64f58ea07004e0c13bcc99f`.

The native compact data is [native_phase_dataset.jsonl](native_phase_dataset.jsonl),
SHA-256 `f54ebb7b387bb212475295e2e3dc171c169fc52c8c73624fba9b9661b50d41e0`.
It has 2,080 physically joined requests and 8,320 rows (queue, prefill, decode,
and e2e for each request). Every row has finite measured metrics, exact
`physical_request_id` binding to a model start, and complete prompt,
completion, cached, and declared maximum token fields. Token fields are marked
post-event labels/diagnostics and are kept out of the adapter's pre-event feature
map.

Native clock `hostname + boot_id` resolves to exactly one hardware snapshot
profile per case. The static GPU inventory digest is the same H100 domain for all
49 train executions:
`stable_gpu_inventory_v1:6eaded0f88a4d31c620e1a18d9ceb9ea6c6d1185781da0ba0d5f7ad352d11ad9`.
The 20 distinct snapshot profile hashes are retained in
[native_profile_bindings.jsonl](native_profile_bindings.jsonl) for provenance;
they are not used as GPU fit domains because their metadata is mutable and one
legacy hash is also used by the CPU binding. The domain digest uses only static
GPU inventory fields. GPU UUIDs, hostnames, boot IDs, job IDs, and process fields
are provenance-only.

CPU source validity was checked for all 49 train cases from the work summary,
BPF collector manifest, raw aggregate journal, binary size, bounded offsets, and
loss/completeness counters without rehashing or decoding every large raw binary.
The existing full raw-byte validator proof is preserved for
`astropy__astropy-14182` in [representative-astropy-14182](representative-astropy-14182/):
validation report SHA-256
`8f29103a8a490a97625b75969741153da12731da2a3b6ae4f377d54683013fdf`, with 1360
normalized records, 504 targets, 105 bounded CPU actions, and a 70,076,400-byte
raw binary covered without decoder expansion.

The existing validator reports 43 cases as fully valid. Six cases have only
`retry_missing_physical_predecessor` errors on wrapper observation rows. Their
native target rows remain exact-joined and are included through an explicit
scope-limited accepted report that preserves the original errors and source
hashes. No other validator error code was accepted into the fit manifest.

The adapter can be rerun with:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 docs/offline-followup-20260909/calibration/calibrate_repaired_d9.py \
  --manifest docs/d9-salvage-20260910/evidence/calibration_input_manifest.json \
  --output-dir docs/d9-salvage-20260910/evidence/calibration
```

The adapter completed with four native paths fitted on the stable domain. Its
simple feature-only component sum is intentionally not the final token-aware
simulator: the native dataset is the input for conditional token/cache fitting.
