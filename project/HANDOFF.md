# Handoff

Phase: G4 local first-trajectory normalization — local PASS
Last verified result: local core/cloud tests, source hygiene, Linux rehearsal, release checks, independent review, and the first paid H100 control passed; the control evaluator measured unresolved/empty-patch
Current task: G4 local normalized-fixture, interval-accounting, and first-control summary contracts are complete; H100-only validation remains the next empirical phase
Frozen decisions: assignment lock, Lambda 1× H100 PCIe 80 GB target, Qwen3-Coder-30B-A3B BF16, pinned vLLM/SWE-agent/SWE-bench/datasets/images, no automatic thin rerun, no merge
Running jobs: none
Blockers: thin-telemetry/G5/G6 empirical validation and explicit user-paid-session authorization remain; no local technical blocker

Provenance note: `cloud/lambda/first_experiment.yaml:git_commit` and
`project/PROJECT_STATE.yaml:last_verified_commit` identify the last reviewed
runtime-source freeze, not a self-referential Git hash. The exact repository
transfer commit is the 40-hex value captured by `git rev-parse HEAD` immediately
before creating `lambda-ready-<commit>.tar.zst`; its filename and checksum are
the authoritative fresh-host bundle identity.

Current release identity: commit
`aa84e8e69a6c5207a2b379cd7d7d9952e885c6a7`; bundle
`lambda-ready-aa84e8e69a6c5207a2b379cd7d7d9952e885c6a7.tar.zst`; SHA-256
`39b112e323dae1d16cf560d4f0fb07f5a2c578bf5ec3b44537936b06697bd7ee`. The
bundle was extracted and tested in a fresh directory on the Lightning Linux
x86-64/H100 host with no vLLM or model workload started.

Next three actions:

1. Retain the normalized first-control JSONL, validator PASS, and raw/trace SHA-256 values; do not rerun the completed control.
2. Stop/terminate the existing Lightning Studio in the provider UI and retain the verified exported archive.
3. Only after a fresh explicit authorization, use the next gate for a paired thin-telemetry run; do not expand to G5/G6 until that review passes.

Read next: `cloud/lambda/RUNBOOK.md`, `project/PROJECT_STATE.yaml`, and worker task reports.
