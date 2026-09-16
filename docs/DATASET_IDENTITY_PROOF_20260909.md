# Public dataset acquisition identity proof — 2026-09-09

The prepared datasets are correct canonical JSONL conversions of the exact
pinned Parquet sources. The preflight failure is a format-binding defect:
`render_runtime_manifest.py` assigns the source Parquet SHA-256 to
`instances_path`, which names a JSONL file. This is not a dataset-version,
suite-content, row-order, base-commit, or patch-content difference.

Evidence and a concrete unapplied fix proposal are retained in this new
durable verification directory:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/dataset-identity-proof-20260909-1j7gs0aa
```

`identity_proof.json` SHA-256:
`60e7d4178511cf438145ff76b0ff4dff165c6fc5104b24b30103ea845636fcb0`.
`root_fix_proposal.json` SHA-256:
`342e5cf7a46d21bf328a70e8b4de4b8d8ca5d6ca88b0dd59b08fe1e2cf872bcf`.

## Origin and comparison

The declared manifest and bootstrap refer to
`SWE-bench/SWE-bench_Lite@69611d31007e1c6731db8bd5b5c3f2d33f5bab6e`
and
`SWE-bench/SWE-bench_Verified@91aa3ed51b709be6457e12d00300a6a596d4c6a3`,
split `test`. `lambda_download_assets.sh` downloads
`data/test-00000-of-00001.parquet` at each immutable revision and records
`source_file_sha256`. `lambda_bootstrap.sh` explicitly requires a `.parquet`
source and verifies these source hashes.

Git commit `745b086f8cffa5150ec89f437e5fc7f1621cf298` documents the hashes
as source Parquet pins. The runtime example introduced in
`d48e55712f00d52801b8e0728f7f7ab78398d413` assigns those same hashes to
JSONL paths. Both historical source snapshots are archived. The existing
`datasets/dataset_conversion.json` already records the distinction; this
check independently verified its contents against the actual files.

| Suite | Rows | Pinned source Parquet SHA-256, matched | Derived JSONL SHA-256, reproduced exactly |
| --- | ---: | --- | --- |
| Lite | 300 | `f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b` | `7f54792b83bf491c0a905770a00ce7fa28836552d37c7ea0e9e2bae4c53f33fb` |
| Verified | 500 | `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21` | `52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da` |

The exact source copies already exist:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/datasets/raw/lite-test-69611d31007e1c6731db8bd5b5c3f2d33f5bab6e.parquet
/home/riverahernandezjason/h100-assignment-work-20260905/datasets/raw/verified-test-91aa3ed51b709be6457e12d00300a6a596d4c6a3.parquet
```

The comparison used PyArrow 25.0.1 under the prepared Python 3.11.16 venv. It
decoded each source once, compared every row and every field against the
prepared JSONL, and reserialized in source order using:

```python
json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
```

The rederived bytes equal the existing files exactly, not merely after
normalization. Both suites have unique instance IDs, identical membership and
order, and zero differing fields. All 300 Lite rows match in all 12 fields;
all 500 Verified rows match in all 13 fields. Public expected-test fields and
all other public values were compared opaquely by hash, with no outcome
analysis. The three selected public-row hashes in the original bootstrap
also match their declared pins. Raw public problem/patch/test contents are
not printed in the evidence; per-row field hashes are preserved in
`lite_row_identity_hashes.jsonl` and `verified_row_identity_hashes.jsonl`.

## First declared case

`django__django-7530` belongs to Verified, at zero-based source row 251. It
does not occur in Lite. My earlier evaluator handoff used the wrong suite
in its proposed command; the current command is corrected, and the prior
archive remains intact as superseded evidence.

| Identity field | Verified value |
| --- | --- |
| Repository/version | `django/django`, `1.11` |
| Base commit | `f8fab6f90233c7114d642dfe01a4e6d4cb14ee7d` |
| Environment setup commit | `3545e844885608932a692d952c12cd863e2320b5` |
| Canonical whole-row SHA-256 | `e33a1c9a9aec9e97fb7c1454991822e3f972a6d094488d73e1a49b4339ca50ff` |
| Public problem UTF-8 SHA-256 | `660d95fad484d129e2943cf8276d081cfceb02bada155f6ddf2e5e1ce4af5d39` |
| Public patch UTF-8 SHA-256 | `0db3b098b00197f8cd6165912e2d60150ae4213338e605188990c00bb52a4029` |
| Public test-patch UTF-8 SHA-256 | `219369b7b0af514fac6bb6243693eec9b14be6288b09b248d043f7119a400402` |

## Exact action for root

No download or dataset replacement is needed. The existing raw sources match
the immutable source pins, and the existing prepared JSONL files are the
clean deterministic materializations. Root can retain these files for the
clean execution workspace, binding each format to its own proven hash.

The narrowly scoped proposed repair is:

1. Preserve both repository/revision pins and the existing Parquet hashes as
   source-acquisition pins. Verify the raw source bytes against them.
2. Verify the deterministic conversion and retain its materialization
   manifest/hash in the durable acquisition evidence.
3. Assign the proven derived JSONL hash to `datasets[suite].sha256`, because
   that field is compared with `instances_path` bytes. Preserve the runner's
   strict file-hash comparison. Do not substitute whichever hash happens to
   be on disk; validate against this pinned-source derivation first.
4. Use `SWE-bench_Verified.jsonl` for the declared Django case. Preserve the
   explicit prepared `repos/SWE-bench` binding in evaluator `PYTHONPATH`.

`root_fix_proposal.json` supplies the exact before/source and proposed runtime
dataset sections for both suites, with the proof hash. It is marked
`review_only_not_applied` and `launchable: false`; it is not a final runtime.
This proposal changes the association between a format and its hash, while
retaining the immutable source pins and every dataset row. Pointing the
JSONL loader at the Parquet bytes would not be a valid repair.

The comparison took a bounded offline pass over the two source files and
their JSONL materializations. No sealed artifacts, evaluated outcomes,
inference, candidate lists, runner code, dataset files, symlinks, or pins were
changed. The proof establishes local identity against the existing declared
revision/source-hash contract; it does not claim a new remote registry fetch.
Main subsequently adopted this fix. The implemented verifier, actual 800-row
proof, and renderer integration contract are documented in
`DATASET_MATERIALIZATION_CHECK_20260909.md`. The broader source/observer 65K and
final frozen-runtime gates remain separate.
