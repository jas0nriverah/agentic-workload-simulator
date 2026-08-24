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
- `HISTORICAL_SWE_AGENT_KERNEL_ATTRIBUTION_UNAVAILABLE`: a lossless request-boundary
  profile is now measured in `project/GCP_H100_REQUEST_PROFILE_20260823.json`
  (31 real trajectory requests plus a six-cell synthetic matrix). The
  profile still cannot be assigned exact historical device time. A separate
  controlled serialized vLLM matrix now has direct Kineto CUDA-activity timing
  in `project/GCP_H100_KINETO_SIMULATOR_20260824.json`; it is not transferred
  retroactively to SWE-agent trajectories.
  A direct high-resolution follow-up is recorded in
  `project/GCP_H100_CPU_GPU_CASE_STUDY_HIRES_20260823.json`; it improves
  temporal resolution but retains the same non-attribution boundary.
- `GPU_HARDWARE_COUNTERS_UNAVAILABLE`: host Nsight Systems wrapping
  `docker exec` did not expose container CUDA kernels and NCU remains blocked
  by `ERR_NVGPUCTRPERM`. Direct CUDA activity timestamps are now measured by
  container-native Kineto for one real trajectory, but no SM-seconds or
  privileged hardware-counter metrics are available.
- Controlled simulator calibration/holdout is now measured from four Kineto
  calibration rows and two predeclared holdouts. The measured holdout MAPE is
  10.7156%, limited to the serialized synthetic serving matrix; broad
  SWE-agent simulator generalization remains unmeasured.

The GCP VM and vLLM service remain intentionally live under the unattended
execution override. Do not shut them down while any blocker or assignment
requirement remains unfinished.
