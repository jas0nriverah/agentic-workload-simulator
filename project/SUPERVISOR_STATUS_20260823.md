> **Historical record — not active instructions.** Current state and execution policy: [CURRENT_STATE.json](CURRENT_STATE.json). Earlier plans, counts, and authorizations below describe their original session.

# Supervisor status — 2026-08-23

Last checked: 2026-08-23 20:19 UTC

## Current state

- Branch `parallel-h100-shards` is clean and synchronized with
  `origin/parallel-h100-shards`.
- Current checkpoint: `0fe8a5a` (`Align project state with profiling
  checkpoint`).
- Codex application services are present locally, but no active experiment
  child process, SSH session, vLLM server, SWE-agent runner, or request proxy
  was detected.
- `project/PROJECT_STATE.yaml` reports `active_experiments: []` and
  `active_agent_tasks: []`. The latest recorded task is the G10 profiled
  request-diversity checkpoint.
- The direct GCP details page confirms
  `instance-20260822-182111` is currently `Running` in `us-central1-a` with
  `a3-highgpu-1g`, one NVIDIA H100 80GB, Spot provisioning, and external IP
  `34.132.119.55`. The VM list itself is suffering a console loading issue.
- Direct SSH from the local host was denied by public key. SSH-in-browser could
  be opened, but its authorization flow ended with `Failed to get access
  token`; no remote process inspection was possible.

## Completed evidence since the earlier handoff

- GCP H100 control and paired thin run: Lite `astropy__astropy-12907`
  resolved `1/1`.
- Lite and Verified gold smokes resolved `1/1`.
- Two-row Lite batch completed with successful agent/evaluator return codes.
- Request timing, CUDA calibration boundary, direct aggregate GPU case study,
  CPU/tool syscall profile, Flask diversity trajectory, and
  `psf/requests-2317` request/profile trajectories were recorded.
- Latest evidence and limitations are summarized in
  `project/GCP_H100_GAP_AUDIT_20260823.md`.

## Active limitations

- Per-request GPU seconds are still unavailable.
- Container-native kernel profiling is blocked by `ERR_NVGPUCTRPERM`.
- Simulator calibration/holdout is explicitly blocked by missing paired
  CPU/GPU decomposition.
- The six-repository baseline and full population sweep remain incomplete.
- Do not convert aggregate dmon, vLLM, or CUDA microbenchmark data into
  per-request GPU time.

## Supervisor decision

No expensive run should be restarted from this host. The H100 is live but its
workload is unverified, so treat it as potentially idle and do not assume that
Codex is consuming it productively. If Codex resumes or SSH access is repaired,
inspect processes, `nvidia-smi`, logs, and artifact timestamps before starting
anything. The next useful action is a specifically named gap-closing operation
capable of producing paired CPU/model-serving/GPU evidence—not another
completed sweep or plotting pass. Preserve all remote artifacts before any VM
shutdown.

This file is an operational observation log, not experimental evidence.
