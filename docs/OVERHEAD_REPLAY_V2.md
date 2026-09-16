# Existing four-fixture preflight: executor contract

`scripts/validation/run_instrumentation_replay.py` accepts the approved four
fixture identities under `assignment.instrumentation-replay-manifest.v2` and
namespace `instrumentation-pilot-overhead-v2`. Legacy v1 retains 16 cases.
The common fixture fields, immutable payload hashes, snapshot restoration and
explicit adapter argv are unchanged. V2 requires exactly:

- `cpu-file-traversal-v1`
- `cpu-test-script-subprocess-v1`
- `model-short-request-v1`
- `model-long-context-request-v1`

Each runs three pairs in off/on, on/off, off/on order: 12 pairs, 24 conditions.
Validation-only runs cannot pass the perturbation gate.

The adapter emits `assignment.instrumentation-replay-result.v2` with all v1
identity/workload fields and these additional measurements:

- `work_wall_ms`: identical fixed-work interval in both modes, including all
  per-action/request capture and durable-boundary costs in instrumented mode.
- `startup_wall_ms`: measured setup before that interval. It is reported
  separately, never relabeled as useful work or hidden from the final pilot.
- `serving_and_cache_policy_sha256`: hash of the actual common configuration;
  the pair rejects policy differences.
- `capture`: exact fields `full_production_capture_enabled`,
  `individual_cpu_operation_records`, `physical_requests`,
  `raw_model_request_records`, `dropped_cpu_records`,
  `cpu_capture_map_failures`, `missing_raw_request_bodies`.

Counters must be derived from actual raw journals/audits. On-mode requires
full capture, zero loss, positive individual CPU counts for CPU fixtures, and
one raw model record per physical request for model fixtures. CPU-only
fixtures use `output_token_provenance: no_model_requests` and zero requests
and output tokens. Model fixtures use measured response usage. Realized token
or request-count differences invalidate the pair.

Reported phase durations cannot exceed measured adapter process wall time.
The adapter process wall duration is retained too. The reviewer must still
verify the adapter's interval boundaries and counters against raw evidence;
JSON declarations alone are not independent proof of capture completeness.

The threshold remains median <=5%, nearest-rank p95 <=10% across the four
per-fixture medians, with absolute timings retained. No production pass is
claimed by adding executor support. Live adapters and immutable fixture
payloads must be bound before actual preflight execution.
