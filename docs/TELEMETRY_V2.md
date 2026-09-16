# Assignment telemetry v2

Telemetry v2 is an append-only, boundary-level measurement contract. It is
ready for offline instrumentation and journal audits; it is not a newly fitted
latency model and it does not establish live acceptance.

`agentic_sim.telemetry.v2.TelemetryV2` writes these files under one attempt
directory:

| File | Contents |
| --- | --- |
| `telemetry_manifest.json` | exact schema, run/attempt identity, clock identity, stream names, feature builder, and hardware policy |
| `lifecycle_events.jsonl` | outer, setup, startup, client processing, state query, retry, failure, teardown, reconciliation, and explicit unknown residual events |
| `tool_events.jsonl` | generated action intent plus the actual pre-execution and terminal tool rows |
| `model_events.jsonl` | logical client-call rows and physical proxy request/retry rows, including token counts and serving timing availability |
| `hardware_snapshots.jsonl` | raw descriptive inventory and the model-facing hardware projection |
| `script_artifacts/` | bounded retained source representations read through the native container `read_file` API, addressed by SHA-256 |
| `request_payloads/` | exact request and response body bytes for each v2 physical request, with explicit completeness flags; headers are omitted |

Every action and request writes a pending row before execution and an immutable
terminal row after success, failure, timeout, or unavailable completion. Rows
carry `run_id`, `attempt_id`, `case_id`, `instance_id`, `writer_role`,
`span_id`, `event_id`, monotonic start/end values, duration, status, error,
provenance, availability, and clock identity. `writer_role` is part of event
identity so an agent runner and request proxy can append to the same directory
without colliding IDs. The proxy continues to emit its historical
`observability.request-proxy.v1` journal as well.

The pinned SWE-agent hook observes `on_model_query`, `on_actions_generated`,
`on_action_started`, `on_action_executed`, `on_step_done`, setup callbacks,
and `get_state`. Generated actions are recorded as `tool_intent` before
blocklist or exit filtering; the measured tool span begins only at
`on_action_started`. The guarded command sent to the environment is retained
alongside the original intent. The hook captures the actual SWE-ReX
`BashObservation.exit_code` and consumes an explicit runtime child/work probe
when available; bytes, files, and subprocess counts remain null with
`unavailable` provenance when that probe is absent. The environment hook wraps the real `SWEEnv.close`, so
teardown covers deployment shutdown and the post-shutdown callback. The direct
runner starts an outer span around the process and marks the generic process
wrapper as excluded from useful attribution. A logical model call is recorded
as `phase: model_client_call`; each request proxy dispatch is a separate
`phase: model_request` physical row linked by logical ID, client span, and
parent event ID. The request proxy begins its model row after request bytes are
read, archives the exact request body durably before upstream dispatch, then
adds the response body without transport/authentication headers and closes the
row for every transport outcome. A successful serving usage `prompt_tokens` value is exposed on the
terminal row as `context_tokens` with
`context_tokens_provenance: derived_alias_of_prompt_tokens`; it is not copied
back into the prospective feature envelope.

For executable `bash run.sh`, `python run.py`, or directly invoked script
actions, the hook first queries the persistent shell's actual `pwd` through the
native environment API, then reads only paths whose cwd and top-level shell
ordering are provable. It reads each candidate through pinned
`SWEEnv.read_file` inside a `state_query` span. The pinned API returns decoded
text and has no server-side byte limit, so the configured 256 KiB limit is a
retention cap: the artifact is labelled
`hash_basis: decoded_text_utf8_reencoding` and `byte_exact: false`. Failed,
oversized, or conditionally ordered reads remain unavailable and are never
represented by a fabricated truncated hash. After potentially mutating
actions the ledger is invalidated until a fresh native read establishes the
next generation. An injected kernel work collector receives the same guarded
command and tool pre-event ID at `start_action`/`end_action`; its persistent
PID-bound summary remains separate evidence for measured filesystem and
subprocess work. The manifest keeps this interface backend-neutral: diagnostic
`strace` is supported by the collector interface but is diagnostic only and is
not frozen as the production backend. The reviewed kernel aggregate backend
must retain every selected filesystem/process operation as an individual raw
event in addition to its derived counters. Missing, dropped, or unbound raw
events make the required evidence unavailable.

`reconcile_e2e()` computes the union of measured terminal intervals inside the
outer interval. Overlapping client, model, tool, and setup spans count once.
The complement is emitted as one or more `unknown_residual` intervals with
their actual monotonic boundaries; no residual is placed at a fabricated
contiguous offset and no broad wrapper is counted as improved phase coverage.
Its returned summary contains:

```text
e2e_duration_ms
measured_phase_union_ms
unknown_residual_ms
measured_coverage_percent
unknown_residual_percent
closure_error_ms
tolerance_ms
within_tolerance
phase_union_ms
measured_intervals
unknown_intervals
outer_wrapper_excluded
unknown_residual_policy
```

Tool and model envelopes retain stable identities and the exact pre-execution
input. `tool_model_vector()` and `model_vector()` are the explicit learned
vector projections: they exclude IDs, raw timestamps, source event IDs,
availability strings, status, duration, realized output, and future script
state. `serialize_tool_vector()` and `serialize_model_vector()` provide the
same canonical JSON bytes for historical training and serving. Realized labels
are available only through `build_conditional_replay_features()` and are
marked `mode: conditional_replay`.

Queue, prefill, and decode timings are null with `unavailable` availability
unless a serving stack exposes reliable values. Byte, file, subprocess, and
dynamic `find -exec` counts follow the same rule: intended paths and syntax do
not become measured work volumes. The production kernel collector retains
individual filesystem/process operation records and a loss report; a
counter-only aggregate is insufficient for D7–D9. A runtime child probe may
supplement those records through `agentic_sim.telemetry.work`, but it cannot
replace them or infer bytes from syscall-line counts. Kernel
`CLOCK_MONOTONIC` event timestamps remain separate from host
`CLOCK_MONOTONIC_RAW` action boundaries and are never subtracted across clock
domains.

The v2 hardware policy exposes only the terms currently consumed by the
feature/model policy: `cpu_frequency_hz`,
`gpu_memory_bandwidth_bytes_per_s`, and `gpu_compute_tflops`, together with
their source, availability, and clock assumptions. CPU thread counts, GPU
identity/VRAM/precision/weight metadata, capabilities, and storage inventory
remain in `raw_hardware` and are descriptive. Storage bandwidth cannot enter a
model projection without a measured work volume. The recorder does not fit or
claim a final latency model from these descriptors. Production manifests carry
this contract under `runner.telemetry`: v2 activation, pre-dispatch raw request
capture, backend-neutral CPU collector settings, and a hash-bound remote
hardware profile are required before a case can be accepted. Missing or corrupt
evidence is an infrastructure stop condition for the matrix.
