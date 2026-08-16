# Decisions

## D-0001 - Minimal G0 bootstrap

- Status: accepted
- Decision: start with dependency-light JSON configuration and a lossless event envelope; resolve Linux/CUDA dependencies before selecting the final runtime stack.
- Rationale: local host is macOS arm64 and does not prove Lambda Linux/CUDA compatibility.
- Assignment impact: none; no final empirical result exists.

## D-0002 - Lambda target

- Status: proposed
- Decision: prepare for one Lambda H100 PCIe 80 GB instance.
- Rationale: enough VRAM for the preferred Qwen configuration candidate, with final fit tested on the actual host.
- Approval needed: user billing authorization before launch.

## D-0003 - Cloud-readiness runtime pins

- Status: accepted for local preparation; H100 acceptance pending
- Decision: keep `Qwen/Qwen3-Coder-30B-A3B-Instruct` in BF16 at revision
  `b2cff646eb4bb1d68355c01b18ae02e7cf42d120`; use vLLM `0.10.0` source commit
  `6d8d0a24c02bfd84d46b3016b865a44f048ae84b`, the amd64 image
  `vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271`,
  and parser `qwen3_coder`; use SWE-agent v1.1.0 commit
  `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`; use SWE-bench v4.1.0 commit
  `726c5461e2ef52d83cf1ea2107870a8bb3328d57`.
- Rationale: the pinned vLLM source contains both the selected Qwen3-MoE
  architecture and the matching Qwen3-Coder parser. The sample's v0.26.0 /
  Qwen3-Coder-Next FP8 combination is reference-only and is not silently
  inherited.
- Dataset decision: use candidate immutable Lite revision
  `69611d31007e1c6731db8bd5b5c3f2d33f5bab6e` and Verified revision
  `91aa3ed51b709be6457e12d00300a6a596d4c6a3`, with the sample's manifest
  hashes retained as candidate representation hashes pending a Linux-side
  content verification.
- H100-only acceptance: model fit, one normal completion, one parsed tool
  call, `/metrics` scrape, official evaluator container architecture/digest,
  and gold-patch smokes.

## D-0004 - Bounded observability upgrade

- Status: accepted for local implementation; empirical H100 portions pending
- Scope accepted now: lossless pinned-vLLM Prometheus parsing with labels and
  metric types; per-attempt cumulative start/end snapshots and explicit
  aggregate deltas; provenance/hardware sidecars; paired control/thin
  overhead summaries; optional capability probes; dependency-free NVTX no-op
  ranges; separate `strace`/Nsight command preparation; bounded memory
  estimation; and deterministic Perfetto-compatible post-processing export.
- Scope accepted later: separate Linux `strace` and Nsight Systems attempts,
  direct GPU-time reconciliation, vLLM `bench serve` calibration, observed
  peak-memory validation, and DCGM only if H100 capability evidence shows a
  missing field.
- Rejected or deferred: OTLP/OpenTelemetry in the default run, because the
  pinned vLLM 0.10.0 default v1 engine rejects it; runtime NVML/DCGM/NVTX
  dependencies; py-spy; synthetic GPU-time reconstruction; runtime Perfetto
  SDKs; and any new benchmark traffic in the first SWE-agent trajectory.
- Semantics: native vLLM metrics are `server_aggregate` and cumulative;
  interval deltas are `aggregate_delta`, never per-request. Only direct
  profiler intervals may use `measured_gpu`; service metrics remain
  `derived_model_service` and estimates remain `estimated`.
- Rationale: these additions directly support the assignment's CPU/GPU
  boundary, Step 3 event analysis, and later simulator inputs without changing
  the frozen model, runtime, SWE-agent command, control path, or methodology.
