# Handoff

Phase: post-G3A Lightning control / G4 local normalization — local PASS
Last verified result: local core/cloud tests, source hygiene, Linux rehearsal, release checks, independent review, and the first paid Lightning H100 control passed; the control evaluator measured unresolved/empty-patch
Current task: G4 local normalized-fixture, interval-accounting, and first-control summary contracts are complete; Lambda-target validation and the next empirical phase remain
Frozen decisions: assignment lock, Lambda 1× H100 PCIe 80 GB target, Qwen3-Coder-30B-A3B BF16, pinned vLLM/SWE-agent/SWE-bench/datasets/images, no automatic thin rerun, no unreviewed automatic merge
Running jobs: none
Blockers: thin-telemetry/G5/G6 empirical validation and explicit user-paid-session authorization remain; no local technical blocker

Provenance note: `cloud/lambda/first_experiment.yaml:git_commit` and
`project/PROJECT_STATE.yaml:last_verified_commit` identify the last reviewed
runtime-source freeze, not a self-referential Git hash. The exact repository
transfer commit is the 40-hex value captured by `git rev-parse HEAD` immediately
before creating `lambda-ready-<commit>.tar.zst`; its filename and checksum are
the authoritative fresh-host bundle identity.

The release bundle is generated from the exact final Git commit with
`git archive`, then hashed and tested after clean extraction. The accompanying
first-control report records the immutable commit, bundle filename, and
SHA-256 for each handoff; a new bundle is required after any source change.
The compact measured control record is tracked at
`project/FIRST_CONTROL_MEASURED.json` and appended to
`project/EXPERIMENT_LEDGER.jsonl`; raw trajectories and the self-contained
export remain outside Git and are referenced by their SHA-256 values.

Next three actions:

1. Retain the normalized first-control JSONL, validator PASS, and raw/trace SHA-256 values; do not rerun the completed control.
2. Verify the Lightning Studio is stopped/terminated in the provider UI if it remains active, and retain the verified exported archive.
3. Only after a fresh explicit authorization, use the next gate for a paired thin-telemetry run; do not expand to G5/G6 until that review passes.

Read next: `cloud/lambda/RUNBOOK.md`, `project/PROJECT_STATE.yaml`, and worker task reports.
