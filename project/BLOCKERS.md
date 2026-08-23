# Cloud-readiness blockers and gates

Technical cloud-readiness is `PASS_gcp_h100_runtime_verified_experiments_in_progress`.
The live target is the GCP `a3-highgpu-1g` 1× H100 VM; these measurements are
GCP-host measurements and remain separate from earlier Lightning evidence.

Measured H100 evidence is now present outside Git under the live VM's
`/home/jasonrivera691/eic-work/artifacts` directory:

- Linux x86-64/H100 preflight and pinned managed-Python bootstrap passed.
- vLLM model fit, normal completion, parsed `qwen3_coder` tool call, native
  `/metrics`, and GPU sampling passed.
- The GCP H100 batches include 31 completed Lite evaluations and 28 completed
  Verified evaluations in the recorded batch set, plus two incomplete Verified
  environment outcomes. Exact per-batch counts are recorded in
  `project/GCP_H100_PROGRESS.md` and `project/GCP_H100_PROGRESS.json`.
- The extra Verified run for `astropy__astropy-12907` completed with a non-empty
  unresolved patch and zero evaluator errors. The Django retry reproduced the
  known `edit_anthropic` environment-install failure and remains incomplete.

Remaining blockers are methodological/data-collection boundaries, not missing
H100 setup:

- `FIRST_CONTROL_PATCH_CLEANLINESS_REQUIRES_REVIEW`: the fixed Lite control
  resolved officially, but its patch included debug/scratch files. Review the
  generated patch and trajectory before treating the one-instance resolution as
  a clean result or changing experimental settings.
- `REQUEST_LEVEL_GPU_ATTRIBUTION_UNAVAILABLE`: a lossless request-boundary
  profile is now measured in `project/GCP_H100_REQUEST_PROFILE_20260823.json`
  (31 real trajectory requests plus a six-cell synthetic matrix). The
  remaining limitation is narrower: vLLM metrics are server-aggregate and no
  per-request GPU time or utilization is assigned. The profile deliberately
  does not turn aggregate counters into device-time claims.
- `CONTAINER_CUDA_KERNEL_PROFILE_UNAVAILABLE`: host Nsight Systems produces a
  valid probe artifact, but wrapping `docker exec` does not expose container
  CUDA kernels to the host trace. Do not call the micro-probe a kernel profile.
- `SIMULATOR_HOLDOUT_NOT_MEASURED`: the assignment simulator still needs
  measured event calibration and a held-out error report.

The GCP VM and vLLM service remain intentionally live under the unattended
execution override. Do not shut them down while any blocker or assignment
requirement remains unfinished.
