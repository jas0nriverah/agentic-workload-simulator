# Full-matrix recovery snapshot

This directory preserves the compact, machine-readable outcome of the
authenticated 16-worker recovery run for the 1,088-case assignment plan.

## Result

- 602 completed `case_result.json` records
- 486 failed ledger entries
- 0 pending case records
- 16 terminal worker state files
- 68 assigned cases per worker, concurrency 1

The worker state value `status: failed` means the worker process reached a
terminal state; it does not mean every case assigned to that worker failed.
The per-worker completed/failed counts in `summary.json` sum to the totals
above.

## Preserved files

- `summary.json` — aggregate counts, per-worker counts, execution policy, and
  source-evidence hashes.
- `worker-*/run_state.json` — the exact worker ledgers used for the totals.
- `worker-*/cases/*/case_result.json` — all 602 accepted result records.
- `RECOVERY_WAVE_03_START.json`, `RESUME_PROVENANCE.json`,
  `RECOVERY_DIAGNOSTIC_SNAPSHOT.json`, and `LIVE_STATUS.json` — key source
  manifests copied from the recovery root.
- `SHA256SUMS` — checksums for every archived file in this directory.

The source recovery tree contained about 5.1 GiB, mostly raw runner logs,
trajectories, evaluator outputs, and traces. Those bulky raw artifacts remain
on the VM; this Git archive deliberately preserves the result contracts,
failure ledgers, provenance, and integrity evidence needed to recover the
state without committing multi-gigabyte runtime output.

The sealed run recorded runner hash
`84895565f651516159c83a07625b40d5242bc16812b939b8f1d5cc9213e075f6`.
The current repository runner hash is recorded separately in `summary.json`
because the repository later merged CPU-Docker continuation support.
