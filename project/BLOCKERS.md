# Cloud-readiness blockers and gates

Technical cloud-readiness is `PASS_g3a_lightning_h100_control_complete`.
The Lambda 1× H100 PCIe remains the target; Lightning measurements below are
not Lambda-host measurements.

Measured H100 evidence is now present outside Git under the local
`h100-artifacts/` directory and in the verified Lightning export:

- Linux x86-64/H100 preflight and pinned managed-Python bootstrap passed.
- vLLM model fit, normal completion, parsed `qwen3_coder` tool call, native
  `/metrics`, and GPU sampling passed.
- Lite and Verified gold smokes completed the official evaluator. The pre-fix
  replicas had empty patches because `repo_name` was omitted; after the runtime
  fix, Lite and Verified generated non-empty patches but both remained
  officially unresolved. No resolved score is claimed.
- The first uninstrumented Lite trajectory and subsequent fixed Lite/Verified
  trajectories and official evaluators completed. The fixed runs generated
  non-empty patches but were unresolved; this is an outcome, not a harness
  failure or a fabricated success.
- The trajectory inventory and self-contained export checksum passed; the
  archive contains the raw control data, evaluator report, derived row,
  trajectory, logs, and inventory (39 verified files, no large-file
  exclusions). vLLM was stopped and an independent post-stop GPU sample
  recorded 0 MiB used, 0% utilization, no Docker processes, and no GPU lock.

Remaining blockers are methodological or authorization boundaries, not missing
H100 setup:

- `FIRST_CONTROL_PATCH_CLEANLINESS_REQUIRES_REVIEW`: the fixed Lite control
  resolved officially, but its patch included debug/scratch files. Review the
  generated patch and trajectory before treating the one-instance resolution as
  a clean result or changing experimental settings.
- `PAID_SESSION_AUTHORIZATION_REQUIRED_FOR_ANY_FUTURE_LAUNCH`: the current
  untracked authorization file is not committed. Refresh it explicitly before
  any new paid run; do not infer authorization from available credits.

No additional workload is recorded as running. Verify the Lightning Studio is
stopped/terminated in its UI when no further work is needed because stopping a
container does not stop provider billing.

The measured export is outside Git at the local handoff path
`h100-artifacts/lambda-results-first-lite-astropy__astropy-12907-control-self-contained.tar.gz`
with SHA-256
`45f1fd6d328eb2d4c2626ca42a68c36ee8b40fef7afc20ddad59f5040f399ed5`.
