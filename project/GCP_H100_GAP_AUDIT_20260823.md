# GCP H100 gap audit (2026-08-23)

This is a measured-state audit after the final planned call-limit condition. It
does not claim that the assignment is complete or that unresolved predictions
are resolved.

## Requirements covered by measured evidence

- Pinned Qwen/vLLM/SWE-agent/SWE-bench runtime: verified on one GCP H100.
- Lite and Verified official evaluator paths: exercised with retained worker,
  evaluator, and provenance manifests; incomplete and unresolved outcomes are
  preserved.
- Control and thin telemetry paths: paired control/thin record exists with
  vLLM metric families and a real unavailable-counters marker.
- Request timing: a 31-event SWE-agent request-boundary profile exists, plus a
  direct four-request CPU/wall versus aggregate-H100 case study in
  `GCP_H100_CPU_GPU_CASE_STUDY_20260823.json`.
- Profiling: syscall, passive dmon, Nsight probe metadata, and a container CUDA
  micro-probe are retained. The host Nsight wrapper did not observe CUDA kernels
  inside the vLLM container.
- Hyperparameter evidence: one temperature point (0.2) and one call-limit
  point (20) were measured. Per the deadline policy, no additional sweep
  conditions are queued unless a deliverable is otherwise unsupported.

## Remaining gaps and claim boundaries

1. **Per-request GPU attribution:** not measured. The direct case study is
   aggregate dmon evidence and uses `CLOCK_MONOTONIC`; the trajectory profile
   uses `CLOCK_MONOTONIC_RAW`. Do not merge their intervals or label dmon
   samples as request GPU time.
2. **Container-native kernel profiling:** host Nsight did not cross the vLLM
   container namespace. A container-native profile would close this gap, but
   no new profiling run should be launched unless it can be collected without
   disrupting active work and produces report-ready evidence.
3. **Simulator calibration/holdout:** the standalone CUDA matmul benchmark is a
   real GPU measurement but is not a vLLM/SWE-agent calibration set. No measured
   paired CPU/GPU decomposition exists, so no simulator fit or held-out error
   claim is permitted.
4. **Repository/category diversity:** current GCP production evidence is
   dominated by Astropy rows. The recorded batches are not an assignment-wide
   population and must not be generalized to one.
5. **Full four-point sweep:** only the measured temperature and call-limit
   conditions are available. This is intentional under the current stop-sweep
   instruction; the report must label the sweep partial.
6. **Outcome quality:** several generated patches are unresolved, and at least
   one Verified run is incomplete due to an environment-build failure. These
   outcomes are part of the result, not data to be removed.

## Next action order

Preserve and push the direct case-study manifest and this audit; continue only
with a named gap-closing operation: container-native profiling if feasible,
otherwise offline simulator-contract/holdout preparation and report aggregation
from immutable manifests. Do not launch more hyperparameter conditions merely to
consume GPU time. Keep the VM and vLLM service available while useful authorized
work remains, and do not infer completion from this audit alone.

