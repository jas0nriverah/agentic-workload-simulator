# H100 request profile E

- Run: gcp-request-profile5 / nvml-10
- Instance: astropy__astropy-12907
- Status: measured; SWE-agent and official evaluator completed (resolved=0).
- Aligned proxy requests: 31; proxy token sum: 423806.
- NVML samples: 81113; vLLM metric snapshots: 3940.
- Aggregate NVML utilization-overlap estimate: 25.060305 GPU-active seconds across aligned request intervals; mean sampled utilization 7.263%.
- Cumulative vLLM deltas are retained in the JSON as aggregate server metrics.
- Clock: CLOCK_MONOTONIC_RAW; host instance-20260822-182111; boot 741f21cd-b530-4306-b4ee-a01e70fb33c0.

## Boundary

This closes the request-level same-clock evidence gap only partially: records align proxy CPU/model request intervals with aggregate NVML utilization and cumulative vLLM metrics. It does not provide exact per-request GPU/kernel attribution, and the aggregate estimate must not be used as gpu_seconds_at_reference for simulator fitting. NCU remains blocked by container permissions.
