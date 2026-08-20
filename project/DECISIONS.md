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

## D-0005 - Assignment knob propagation amendment

- Status: accepted for pre-H100 implementation; empirical sweeps deferred
- Decision: treat `agent.model.per_instance_call_limit` as the maximum-call
  control; pass output cells through both SWE-agent metadata and
  `agent.model.completion_kwargs.max_tokens`; sweep
  `agent.templates.max_observation_length`; and pass temperature plus seeds
  through `agent.model.completion_kwargs.seed`. Keep the 32K input limit fixed
  as a guard, not as a sweep.
- Defaults: 30 calls, 2048 provider/output tokens, 100,000 observation
  characters, temperature 0.0, seed 0. Stochastic cells use seeds 0, 1, 2.
- Rationale: the pinned SWE-agent v1.1.0 source uses the call limit for model
  calls, treats max input as a guard, and only guarantees local OpenAI/vLLM
  output-token propagation through `completion_kwargs.max_tokens`.
- Validation: every concrete command is parsed and hashed by
  `scripts/cloud/validate_sweagent_command.py`; control and thin commands must
  be byte/argv-equivalent in their model/tool payload.

## D-0006 - Pre-H100 provenance contracts

- Status: accepted
- Decision: timed records use one shared monotonic clock identity and persist
  host/boot metadata; artifact contract v2 uses a real-Parquet or explicit
  unavailable-marker one-of rule; and public-reference evidence is locked as
  comparison-only when the public scaffold is not an exact match.
- Rationale: these changes prevent false cross-host timing joins, fake Parquet
  files, and accidental presentation of a different public scaffold as an
  exact reproduction. They do not add instrumentation or change the
  assignment methodology.
- Deferred at that point: request/event correlation, interval closure, cgroup/
  PSI attribution, Nsight Compute, sweeps, calibration, and simulator fitting
  until the first real trajectory was exported. The exported fixture is now
  normalized under D-0009; the remaining timing/correlation work is still
  deferred.

## D-0007 - Measured dataset source-file hashes

- Status: accepted
- Decision: replace the stale candidate dataset “manifest” hashes with the
  SHA-256 of the exact pinned Hugging Face source Parquet retrieved on the
  Lightning H100, and replace the selected-row fixtures with hashes of the
  canonical one-row JSON files emitted by the pinned reader.
- Evidence: Lite source `f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b`,
  rows `astropy__astropy-12907=e117000983a3aabba8f43fb52e155d0cc6529b900ed476f59dc6cc065e970faa`
  and `astropy__astropy-14182=87118fdd9b83e959aa533ea57a70557e95a7027fbce92b14879a98468f5a263b`;
  Verified source `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21`,
  row `astropy__astropy-14365=4d0d91079bd056ff5d1940614ad71f025dd96f0498ceaf9ab71757efde87f5e3`.
- Rationale: the prior values did not match either the raw pinned Parquet or a
  direct `datasets` read of the same revisions. This is a provenance/validation
  correction only: revisions, split, row counts, instance IDs, and evaluator
  methodology are unchanged. No sample-repository result or fabricated score
  is inherited.

## D-0008 - SWE-agent runtime dataset compatibility view

- Status: accepted for the first pinned trajectory
- Decision: preserve the measured one-row SWE-bench JSON and its hash as the
  evaluator source asset. For SWE-agent v1.1.0 only, derive an attempt-local
  `sweagent_instances.json` that adds the deterministic `image_name` required
  by its file-backed `SimpleBatchInstance` schema. Record both paths in the
  attempt manifest; the official evaluator continues to consume the raw
  selected-row asset.
- Rationale: the pinned SWE-agent reader requires `image_name`, while the
  canonical SWE-bench row emitted by the pinned dataset reader does not include
  it. Adding the derived field at the command boundary fixes the real runtime
  incompatibility without mutating raw evidence, changing the instance, or
  altering the evaluator methodology.

## D-0009 - Lossless first-trajectory normalization contract

- Status: accepted for local implementation; request-level timing deferred
- Decision: normalize the first pinned SWE-agent `.traj` only as an additive
  JSONL index. Preserve every raw trajectory/history/info record and the raw
  source hash. When the trace log is supplied, retain each matched
  `ModelResponse` source line and expose anchored provider response IDs, tool
  call IDs, finish reasons, and usage as separate `model_call` records.
- Correlation: join the 30 committed tool executions to assistant/tool history
  and trace responses by exact tool-call ID. Keep the 31st provider response as
  `discarded_limit_exceeded` because the call-limit warning was logged after
  the request and no corresponding committed trajectory step exists. Emit the
  synthetic `Exit due to cost limit` item as an `agent_terminal` record.
- Accounting: preserve provider totals (531,236 prompt / 7,961 completion /
  539,197 total) separately from SWE-agent `.traj` totals (478,411 input /
  3,520 output / 31 API calls). These namespaces must not be reconciled or
  substituted for one another.
- Timing: retain SWE-agent `execution_time` as duration-only and trace log
  timestamps as wall-clock observations. Request IDs, monotonic intervals,
  native vLLM correlation, and GPU attribution remain explicitly unavailable;
  no synthetic values are emitted.
- Evidence: the raw `.traj` SHA-256 is
  `e48fb12deac02ae196330fd4cf40c15d42defb8517ae428fd3099a2f284f6de7`;
  the real fixture normalizes to 31 model-call records, 30 tool-execution
  records, one terminal event, and 64 preserved history messages.
- Rationale: the contract gives the assignment's event analysis a reviewable,
  byte-preserving fixture without changing the SWE-agent scaffold or claiming
  request-level measurements that the first run did not collect.
- Immediate free/local review: inspect the normalized fixture and verify its
  raw hashes before any new provider work. Deferred until a separately
  authorized paid session: interval-union accounting closure, paired
  thin-overhead run, Nsight/strace, sweeps, and G5/G6 expansion.

## D-0010 - Reset-safe interval-union accounting contract

- Status: accepted for local implementation; empirical thin-run closure
  remains H100-only
- Decision: account timed event streams by merging overlapping monotonic
  intervals only when `clock_id`, hostname, and boot ID match exactly. Report
  raw duration, union duration, overlap, span, gaps, and coverage as derived
  fields. Empty or incomplete streams remain explicitly unavailable.
- Implementation: `agentic_sim.observability.accounting` and
  `scripts/observability/account_intervals.py`.
- Prohibition: the result is a timed-event union, not GPU device time; native
  vLLM aggregate metrics cannot be used as request intervals or GPU time.
- Rationale: this closes the accounting contract locally without fabricating
  request correlation. A future authorized thin run must supply measured
  intervals before any empirical accounting claim is made.
