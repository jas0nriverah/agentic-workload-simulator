# Pinned dataset materialization verifier — 2026-09-09

The approved Parquet-to-JSONL binding repair now has an executable, offline
verifier: `scripts/assignment/verify_dataset_materialization.py`. Both real
suites passed: 300 Lite rows and 500 Verified rows, in source order, with every
field unchanged. No dataset version or dataset file changed.

The verifier checks each immutable Parquet SHA before decoding those same bytes.
It requires unique instance identities, exact row order and field sets, equality
of every canonical field value, exact canonical JSONL bytes, and the separately
pinned derived SHA. It rereads both inputs to detect mutation during the check.
The new proof retains both file hashes and agreed per-row/per-field hashes;
it does not print raw public patches or test values. Existing outputs cannot be
overwritten. PyArrow is required only by this verification interpreter.

| Suite | Original Parquet SHA-256 | Derived JSONL SHA-256 |
| --- | --- | --- |
| Lite | `f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b` | `7f54792b83bf491c0a905770a00ce7fa28836552d37c7ea0e9e2bae4c53f33fb` |
| Verified | `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21` | `52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da` |

The existing HF revisions remain Lite
`69611d31007e1c6731db8bd5b5c3f2d33f5bab6e` and Verified
`91aa3ed51b709be6457e12d00300a6a596d4c6a3`. The renderer owner has integrated
JSONL `sha256` and original `source_parquet_sha256`. This helper does not edit
runtime manifests or relax any file-hash check. Its import interface is
`verify_materialization(dataset_root: Path, suites=("lite", "verified"))`;
`write_proof(new_path, proof)` returns the new proof SHA. Constants are exported
as `DATASET_REVISIONS`, `SOURCE_PARQUET_HASHES`, and `JSONL_HASHES`.

Reproduce into a **new** output path:

```bash
/home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909/venv/bin/python \
  scripts/assignment/verify_dataset_materialization.py \
  --dataset-root /home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909/datasets \
  --output /absolute/new-verification-directory/materialization.json
```

The completed proof is at:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/dataset-materialization-verified-20260909-w4eb6sxa/materialization.json
SHA-256 6c4729c1cbd7f7cf7b7e5bca09c08cd159dc2f1ed7e58118793469086f26e2b7
```

Seven focused tests passed under the prepared venv with PyArrow, without skips.
They cover a valid pair, original-source pin rejection before decoding, order
and duplicate identities, changes to arbitrary/base/patch/test-patch fields,
noncanonical bytes and wrong derived pins, missing/duplicate fields, and output
overwrite refusal. The durable verification directory retains source, tests,
test logs and artifact hashes.

This establishes local identity against the existing declared acquisition pins.
It does not establish a new remote fetch or broader serving/acquisition gates.
The original investigation remains in `DATASET_IDENTITY_PROOF_20260909.md`.
