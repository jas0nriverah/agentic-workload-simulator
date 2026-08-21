# Handoff

Phase: post-G3A Lightning control / G4 measured thin-and-gold trajectories — structural PASS
Last verified result: local core/cloud tests, source hygiene, Linux rehearsal, release checks, independent review, and the first paid Lightning H100 control passed; the control evaluator measured unresolved/empty-patch
Current task: G4 local contracts plus paired Lightning thin runs and one Lite/Verified gold-row trajectory each are recorded; Lambda-target validation and the next empirical phase remain
Frozen decisions: assignment lock, Lambda 1× H100 PCIe 80 GB target, Qwen3-Coder-30B-A3B BF16, pinned vLLM/SWE-agent/SWE-bench/datasets/images, no automatic thin rerun, no unreviewed automatic merge
Running jobs: vLLM remains loaded in the two Lightning Studios; no SWE-agent job is required by this handoff
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

Measured cloud evidence: `project/PAID_SESSION_MEASUREMENTS.json` records the paired thin runs and distinct Lite/Verified trajectories. Every listed evaluator returned `rc=0`, every listed normalizer passed, and the pre-fix runs were empty because the runtime omitted `repo_name`. After that runner fix, Lite and Verified runs generated non-empty patches but remained officially unresolved; no resolved-rate or model-quality claim is permitted.

Next three actions:

1. Retain the normalized first-control, paired thin, Lite-gold, and Verified-gold JSONL plus their raw/trace SHA-256 values; do not rerun them solely to chase a resolved patch.
2. Review the empty-patch outcomes and preserve the raw cloud export before terminating the Studios in the provider UI.
3. Only after a fresh explicit authorization, proceed to G5/G6 or a scientifically justified configuration experiment; do not present these runs as resolved-rate evidence.

Read next: `cloud/lambda/RUNBOOK.md`, `project/PROJECT_STATE.yaml`, and worker task reports.
