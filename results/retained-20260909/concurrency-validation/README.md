# Bounded collector concurrency validation

Twenty repaired collectors passed a CPU-only simultaneous-start and simultaneous-finalization stress test: 10,948,899 complete raw records, zero observed transport/map loss, valid identity/lineage joins, and 20 clean exits. All workloads overlapped for 17.387 seconds; boundary finalization and stop also overlapped. This is a bounded topology result, not full production/GPU-fleet or D9 accuracy validation.

The isolated repair gives the native stream its intended explicit 1 MiB buffer, removes fsync from the callback mutex while retaining durable cutoffs, and fixes the control deadline/disconnected-client failure demonstrated by an earlier simultaneous test. That earlier test retained all records but failed control completion and was not accepted. Frozen source-v8 remains unchanged.

At comparison resumption, 62 queue-accepted outcomes plus three individually reconstructed blocked originals gave 65/96 validated outcomes, with 31 remaining. Case64 remains unresolved and includes a legitimate HTTP400 context-limit rejection before model admission. Two client-to-physical retry links were reconstructed in memory from retained explicit identities; no case was rerun to improve its outcome.

Three closed-evidence compression batches reclaimed approximately 7.0 GiB of observed filesystem space. Read-only compressed images remain at original artifact paths, with separately verified images and retention manifests on PACE. Raw traces are not stored in Git. These compact receipts retain source paths and hashes.
