# Acquisition trace review — 2026-09-09

Scope: the literal coding-test PDF at `/tmp/coding-test-literal-20260909.txt`, the frozen `source-v8` acquisition path, the prepared 96-case configuration queue, and the intended 1,088-case production plan. This is a measurement-completeness review, not a claim that D9 accuracy has already passed.

## What the PDF requires and the irreversible evidence

| PDF result | Derived quantity | Raw evidence that must survive production | Acquisition path and identity |
|---|---|---|---|
| D1: Lite and Verified resolved rate and mean E2E latency | suite denominator, accepted terminal outcome, outer elapsed time | frozen dataset row/hash, fresh case ID, all attempt results, evaluator input/prediction/report/result, outer lifecycle interval | production case specification and queue lease; `sweagent_case_runner.py` retains evaluator and attempt artifacts. Join on `case_id`, `attempt_id`, `run_id`, `instance_id`, suite and prediction/report hashes. |
| D2–D4: category ratio, figures, and explanation | repository/category label; CPU-tool wall and model-serving wall; named nonoverlapping lifecycle complements | repository/instance metadata; action boundaries; CPU BPF operation stream; model request/client spans; lifecycle intervals | `telemetry_v2/{tool_events,model_events,lifecycle_events}.jsonl`, BPF binary plus aggregate/action journal, and raw request payloads. The defensible ratio is declared tool-execution wall divided by native prefill+decode wall; it is not CPU busy time divided by CUDA occupancy. |
| D5–D6: four four-value accuracy/latency sweeps and combined plot | seven-key settings, each cell's official outcome and E2E, matrix membership | immutable case settings and cell ID, fresh case identity, evaluator outcome and lifecycle | `production_run_manifest.v2.json` specifies call limit, max output tokens, observation length and temperature grids; 800 Step-1 rows plus 288 Step-2 rows total 1,088. The queue retains settings in case bytes and each attempt lease. |
| D7–D8: high-ratio breakdown and individual CPU/GPU events | per-action wall; per-syscall type, duration, return-size/path descriptor availability; request input/output/context/cache and native queue/prefill/decode/E2E durations | append-only action boundaries and BPF packets; raw request/response payloads; ASGI observer and vLLM finished-request journal | BPF v3 retains a binary packet per operation, action token, process/namespace/start-ticks identity, scalar syscall arguments, return value/duration and explicit loss/censor state. Native server records join physical request ID to vLLM engine ID; cache zero is captured from `EngineCoreOutput.num_cached_tokens`, never inferred. |
| D9: event models, hardware substitution, figures and 25% gates | training/holdout membership, pre-event features, observed event and outer labels, hardware descriptors, error by individual event and E2E | all prior rows plus model/tokenizer/weights-size metadata, CPU-host and GPU-server inventories, clock/boot/process/lease identities, rejected/failed events retained | hardware profiles distinguish the CPU/Docker recorder host from the remote GPU server; the simulator may expose alternate parameters after collection. The strict per-event/E2E 25% calculation remains a future holdout evaluation, not a field inferred from this audit. |

## Proven acquisition path

The final plan's stated denominator is 300 Lite + 500 Verified baseline instances and 288 independent sweep instances. Its four candidates have all seven requested settings frozen; `configuration-v1/prepared-queue-v2-receipt.json` binds 96 outcome-blind confirmation cases. The production plan is still pending selection and has not been treated as observed data.

For a run, the queue atomically leases one case to one worker endpoint. `shared_case_queue.py` has unique active-case, active-worker, and active-endpoint indexes, and retains attempt history rather than reusing a timed-out lease. Each worker fingerprint binds its endpoint, `server_identity`, hardware profile, vLLM source and serving epoch. The server records its dedicated lease and all observed ingress; a production case must be rejected if the archived stream contains an unexpected/inflight foreign request rather than relying only on this declared topology.

CPU and GPU data are deliberately separate. The combined evidence records the Docker persistent shell's host PID, PID namespace, start ticks, cgroup and actual CPU affinity for BPF collection. It retains per-action cgroup `cpu.stat`/I/O context for userspace work. `resource.getrusage` snapshots describe the recorder process only and must not be substituted for tool-child CPU time. BPF kernel timestamps use `CLOCK_MONOTONIC` only for a syscall's internal duration; action/lifecycle spans use the CPU host's `CLOCK_MONOTONIC_RAW`, and the implementation explicitly forbids subtracting those domains.

Remote profiles identify the API process, vLLM engine child, GPU UUID/PCI bus, H100 SKU, clocks, driver, model-serving argv, CPU affinity/cgroup, server lease and counter epoch. Native vLLM durations are server-engine durations and token/cache measurements; they are **not** CUDA-kernel timings. Their remote clock is preserved with its own host and boot ID, so no cross-host timestamp subtraction is available or needed for the event model.

## Representative saved-only reconstruction

`combined-case-v8` is excluded from candidate selection and production statistics, but is a safe joint fixture. It contains one complete official-evaluator run, 40 terminal tool events, 40 physical model requests, 846,533 BPF operation packets, and the remote ASGI/native journals. `reconstruction-v8/merged-v2-proof.json` passed with 153,117.581093 ms outer wall, 145,814.380856 ms attributed union, explicit 7,303.200237 ms unknown wall, zero missing terminal tools/requests, zero duplicate IDs, zero feature-parity mismatches and zero future-feature violations. The native evidence manifest reports all 40 physical requests measured and zero unavailable.

I also ran the production exporter over that saved case only. It wrote a status-pass bundle before the deliberately stopped redundant standalone validation pass. Its compact manifest and assertions are retained beside this review:

- `acquisition-trace-export-manifest.json`: 100 BPF actions, 846,533 individual BPF operations, 80 model-attempt rows, 80 complete payload rows, 568 lifecycle intervals, one E2E reconstruction, and 355 explicit unknown joins.
- `acquisition-trace-reconstruction-assertions.jsonl`: source-hash, source-local-join, declared-artifact, non-aggregate-operation, and payload-roundtrip assertions pass.

This proves that the raw CPU stream, raw model payloads, lifecycle data and identity joins can be materialized after serving ends. The temporary full export is intentionally left in `/tmp/acquisition-trace-export-20260909` for the root owner; it is not an input to production.

## Findings and closure conditions

No concrete irreversible acquisition deficiency was found in the inspected `source-v8` path or its representative end-to-end evidence.

The following are required interpretation/acceptance conditions, not new telemetry work:

1. Preserve unavailable fields. A missing BPF path descriptor, unsupported descriptor transition, censored syscall, absent model context at a prospective boundary, or unavailable native component must remain unavailable; it cannot be synthesized from aggregates, prompt length, or a later request.
2. Treat BPF syscall duration, CPU action wall, server-engine duration, client request wall, and outer E2E as distinct intervals. Compose only declared nonoverlapping unions; never convert native prefill/decode to CUDA kernel occupancy or use remote timestamps in local E2E arithmetic.
3. The completed run must retain every failed/retried physical request and every attempt's evaluator result. A successful retry is not a reason to drop the prior physical request or its terminal failure.
4. Final per-case acceptance must check the actual queue lease, worker/endpoint/server identity, ASGI completeness watermark and native reconciliation. The prepared worker topology alone is not evidence that the executed endpoint was exclusive.
5. D9 fitting and the literal 25% individual-event and E2E gates remain post-collection evaluation obligations. The raw evidence is sufficient to perform them; this review does not declare their numerical success.

The historical 55.98% unexplained-E2E finding, shell/test/traversal misses, and prior D9 failures are addressed by retaining the full lifecycle complement, raw per-operation stream, action/container context and exact physical-request/native-server reconciliation. No residual multiplier, thread-count scaling, or realized-output feature may be used to conceal a miss. Realized output tokens are an observed event descriptor for retrospective Step-3/D9 analysis; they are unavailable at the prospective request boundary and must not be used by a pre-dispatch predictor.

**Acquisition disposition: PASS, conditional on the existing per-case integrity gates above.**
