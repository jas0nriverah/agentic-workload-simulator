# Measured limitations and remaining offline gaps

H100 runtime and data acquisition are complete. These entries are claim
boundaries or offline-analysis gaps; none justifies an automatic new H100 run.

## Closed runtime questions

- Linux/H100 bootstrap, vLLM model fit, parsed `qwen3_coder` tool calls, native
  metrics, SWE-agent execution, and the official evaluator were exercised.
- The canonical evaluated populations are 32/32 completed Lite and 29/32
  completed Verified. Verified also contains one empty patch and two retained
  pre-generation failures.
- Four endpoints exist for each of the four assignment sweep axes.
- Direct Kineto CUDA activity exists for one real 31-request Astropy
  trajectory and a controlled four-calibration/two-holdout serving matrix.

## `GPU_HARDWARE_COUNTERS_UNAVAILABLE`

The bounded NCU probe failed with `ERR_NVGPUCTRPERM` and produced no report.
Container-native Kineto still supplies CUDA activity timing, but no NCU SM
occupancy, bandwidth, or privileged hardware-counter values may be claimed.
Do not repeat this probe without a deliberately reconfigured, isolated host.

## `HISTORICAL_SWE_AGENT_KERNEL_ATTRIBUTION_UNAVAILABLE`

Historical NVML/process samples cannot be converted into exact request GPU
seconds. The real Kineto case study provides direct activity timing for one
Astropy trajectory only and cannot be applied retroactively to other runs.

## `DIRECT_MULTI_REPOSITORY_CPU_GPU_RATIO_UNAVAILABLE`

Outcome coverage spans 11 Lite and 12 Verified repositories, but direct Kineto
CPU/CUDA decomposition is one-repository evidence. Repository-category
CPU:GPU population ratios and causal category comparisons remain unsupported.

## `POPULATION_AVERAGE_E2E_LATENCY_NOT_IN_COMPACT_SUMMARIES`

The compact official evaluator summaries contain outcomes and identifiers, not
per-instance E2E duration or a complete price ledger. Do not infer population
average latency, energy, cost, or efficiency from these files. Search retained
raw exports before considering any new acquisition.

## `INDIVIDUAL_EVENT_SIMULATOR_VALIDATION_UNAVAILABLE`

The 10.715632% result uses each holdout's measured CPU-exclusive and
CUDA-union durations plus a residual fitted on four calibration cases. It is a
two-row controlled E2E phase-reconstruction check, not feature-only unseen-work
prediction and not individual-event error validation.

## Raw-trace portability

The compact Kineto manifests record raw paths and SHA-256 hashes. The 1.26 GB
real-trajectory trace and matrix raw traces are not tracked in Git, so the
compact metrics are auditable but the parser cannot be replayed locally unless
the raw exports are recovered.

## Acquisition decision

**H100 DATA ACQUISITION CLOSED.** Another H100 would be justified only by a
new, predeclared experiment targeting a specific unsupported claim—most
plausibly multi-repository direct Kineto timing—not by an arbitrary 30th
Verified completion or repeat sweep/profiler work.
