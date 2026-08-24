# H100 vLLM calibration (2026-08-23E)

Measured serving calibration for the pinned vLLM 0.10.0 server and Qwen3-Coder revision. No SWE-agent methodology was changed.

- GPU: NVIDIA H100 80GB HBM3
- Model revision: b2cff646eb4bb1d68355c01b18ae02e7cf42d120
- vLLM: 0.10.0
- Precision: BF16
- Prompts per condition: 16
- Concurrency: 1
- Output length: 64 tokens
- GPU-time claim: none (NVML/device attribution unavailable)

| Input tokens | Duration (s) | Request tok/s | Output tok/s | Median TTFT (ms) | Median TPOT (ms) | P99 ITL (ms) |
|---:|---:|---:|---:|---:|---:|---:|
| 128 | 6.75 | 2.37 | 151.68 | 24.84 | 6.33 | 6.57 |
| 512 | 6.49 | 2.47 | 157.84 | 33.8 | 5.94 | 6.23 |
| 2048 | 7.05 | 2.27 | 145.19 | 62.84 | 6.03 | 6.37 |

## Interpretation

These points provide measured TTFT/TPOT/ITL and throughput anchors for later simulator fitting. They are not a fit or holdout result: the simulator still requires independently measured gpu_seconds_at_reference, and aggregate NVML utilization cannot be relabeled as that quantity. Held-out error remains blocked until defensible GPU-time attribution is available.

Raw logs and service manifests remain on the VM under ~/eic-work/artifacts/calibration/gcp-h100-vllm-calibration-20260823e/; only this compact manifest is tracked.
