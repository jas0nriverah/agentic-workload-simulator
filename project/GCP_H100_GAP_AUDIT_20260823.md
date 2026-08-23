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
  `GCP_H100_CPU_GPU_CASE_STUDY_20260823.json`. A follow-up six-request probe
  aligns request intervals and 50-ms aggregate GPU samples on
  `CLOCK_MONOTONIC_RAW` in `GCP_H100_ALIGNED_REQUEST_GPU_20260823.json`.
- Profiling: syscall, passive dmon, Nsight probe metadata, and a container CUDA
  micro-probe are retained. The host Nsight wrapper did not observe CUDA kernels
  inside the vLLM container.
- CPU/tool profiling: one real 115-second SWE-agent trajectory was captured
  under file/process/network `strace` with 31 successful request boundaries and
  835 concurrent H100 samples. This closes the existence/provenance gap for a
  CPU/tool case study, but not per-request device seconds.
- Container-native NCU capability: measured and blocked by the host GPU
  performance-counter policy. The container NCU microbenchmark connected and
  executed matrix sizes 1024/2048/4096, but emitted `ERR_NVGPUCTRPERM` and
  produced no report. No kernel metrics are claimed; see
  `GCP_H100_CONTAINER_NCU_CAPABILITY_20260823.json`.
- Hyperparameter evidence: one temperature point (0.2) and one call-limit
  point (20) were measured. Per the deadline policy, no additional sweep
  conditions are queued unless a deliverable is otherwise unsupported.

## Remaining gaps and claim boundaries

1. **Per-request GPU attribution:** not measured. The aligned follow-up shares
   `CLOCK_MONOTONIC_RAW` and reports aggregate utilization overlap per request,
   but it does not assign device seconds to a request. Its vLLM counter parser
   also did not produce per-record deltas; validated aggregate server deltas
   remain in `GCP_H100_SERVICE_CALIBRATION_REQUEST_METRICS_20260823.json`.
2. **Container-native kernel profiling:** host Nsight did not cross the vLLM
   container namespace, and the measured container-native NCU capability probe
   is blocked by `ERR_NVGPUCTRPERM`. No kernel metrics are available from this
   VM under the current permission policy.
3. **Simulator calibration/holdout:** the standalone CUDA matmul benchmark is a
   real GPU measurement but is not a vLLM/SWE-agent calibration set. The aligned
   probe supplies a predeclared calibration/holdout split and aggregate GPU
   overlap, but no measured per-request GPU-seconds decomposition exists, so no
   simulator fit or held-out error claim is permitted. The explicit status is retained in
   `GCP_H100_SIMULATOR_HOLDOUT_STATUS_20260823.json`.
4. **Repository/category diversity:** one additional non-Astropy Lite trajectory
   is now recorded for `pallets/flask` (`pallets__flask-5063`) in
   `GCP_H100_DIVERSITY_FLASK_5063_20260823.json`. It was officially evaluated
   and unresolved. The GCP production evidence is still not an assignment-wide
   population and must not be generalized to one.
5. **Full four-point sweep:** only the measured temperature and call-limit
   conditions are available. This is intentional under the current stop-sweep
   instruction; the report must label the sweep partial.
6. **Outcome quality:** several generated patches are unresolved, and at least
   one Verified run is incomplete due to an environment-build failure. These
   outcomes are part of the result, not data to be removed.
7. **Profile-only trajectory outcome:** the new strace trajectory exited
   `exit_cost` with an empty patch. It is retained as measured profiling
   evidence and must not be counted as a resolved SWE-bench result.

## Next action order

### CUDA-event/utilization calibration result

The isolated H100 probe completed successfully and provides CUDA-event ground truth plus contemporaneous aggregate utilization samples. Because the kernels were shorter than the 50 ms sampler period, utilization overlap was zero/near-zero; this exposes sampler-resolution limits rather than providing a usable conversion. Per-request vLLM GPU seconds and held-out simulator error remain open. Do not treat this artifact as an E2E calibration row.

Preserve and push the direct case-study manifest and this audit; continue only
with a named gap-closing operation: container-native profiling if feasible,
otherwise offline simulator-contract/holdout preparation and report aggregation
from immutable manifests. Do not launch more hyperparameter conditions merely to
consume GPU time. Keep the VM and vLLM service available while useful authorized
work remains, and do not infer completion from this audit alone.
