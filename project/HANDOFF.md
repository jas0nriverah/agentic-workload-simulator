# Handoff

Phase: CR13 pre-H100 hardening — local PASS
Last verified result: local core/cloud tests, source hygiene, Linux rehearsal, release checks, and independent review passed; no paid compute has run
Current task: transfer the frozen release bundle only if the user later authorizes a paid session; H100-only validation remains the next empirical phase
Frozen decisions: assignment lock, Lambda 1× H100 PCIe 80 GB target, Qwen3-Coder-30B-A3B BF16, pinned vLLM/SWE-agent/SWE-bench/datasets/images, no paid compute, no automatic thin rerun, no merge
Running jobs: none
Blockers: H100/Linux empirical validation and explicit user-paid-session authorization remain; no local technical blocker

Provenance note: `cloud/lambda/first_experiment.yaml:git_commit` and
`project/PROJECT_STATE.yaml:last_verified_commit` identify the last reviewed
runtime-source freeze, not a self-referential Git hash. The exact repository
transfer commit is the 40-hex value captured by `git rev-parse HEAD` immediately
before creating `lambda-ready-<commit>.tar.zst`; its filename and checksum are
the authoritative fresh-host bundle identity.

Next three actions:

1. Transfer the locally verified bundle to a fresh Lambda Ubuntu x86-64/H100 host only after the user completes the paid-session gate.
2. Run preflight, pinned bootstrap, vLLM health, Lite/Verified gold smokes, then one uninstrumented first Lite trajectory and its official evaluation.
3. Export/checksum the control artifacts, stop workloads, and terminate the VM in the provider console. Implement and review the lossless trajectory normalizer locally before any separately authorized thin-telemetry run or G5/G6 expansion.

Read next: `cloud/lambda/RUNBOOK.md`, `project/PROJECT_STATE.yaml`, and worker task reports.
