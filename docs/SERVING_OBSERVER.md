# Serving observer

This is the bounded server-side evidence path for native vLLM attribution. It
is an ASGI middleware that can be loaded by the pinned vLLM 0.10.0 API server
through `--middleware`. It observes the application after the server has
mounted its routes, so `/metrics`, health probes, model routes, validation
failures, streaming responses, and disconnects use the same server clock and
the same append-only journal.

The change in this repository does not deploy the middleware or restart a
remote server. The first controlled rollout remains owned by main after
review, the configuration fingerprint gate, and an overhead check. The
current selected rollout target is worker 22, job 5741123, node
`atl1-1-03-011-28-0`, API server PID 3155525, port 18222. Its observed
22-server fingerprint has `--max-model-len 32768` while the declared value is
65536; that mismatch must be handled by the controlled restart before any
fleet rollout.

## What the middleware records

`agentic_sim.telemetry.serving_observer.ServingObserver` has the constructor
shape expected by vLLM's class middleware loader: `ServingObserver(app)`.
Configuration is supplied through environment variables because vLLM passes no
custom middleware arguments.

For every HTTP request it first writes a durable `request_start` record and
then forwards the original ASGI scope, receive messages, and send messages.
It does not add or remove headers, buffer model bodies, or change body chunks.
Each record contains:

- a generated `observation_id`, including for foreign requests;
- the forwarded `X-EIC-Physical-Request-ID` when it is present and valid;
- optional `X-EIC-Case-ID` and `X-EIC-Attempt-ID` values when a caller forwards
  them, retained as metadata only; request-ID grouping remains authoritative;
- method, path route, request and response SHA-256 hashes, byte counts,
  response status, body completeness, errors, and disconnect state;
- `started_monotonic_ns` and `terminal_monotonic_ns` in the observer's
  selected clock domain; and
- hostname, boot ID, clock ID/source, ASGI server PID and `/proc` start-time
  ticks, server identity, lease ID, counter epoch, and observer source hash.

The request class is `model` only when a physical ID is present on an
inference or other non-read-only route. It is `foreign` when an untrusted or
unknown request lacks that ID. `observer` is reserved for `/metrics` and the
fixed read-only vLLM routes `/health`, `/load`, `/ping`, `/version`,
`/server_info`, `/v1/models`, `/tokenizer_info`, `/docs`, `/redoc`, and
`/openapi.json` when accessed with GET or HEAD. The
`X-EIC-Observer-Request` header cannot downgrade a POST inference route or an
unknown route.

The terminal record is written after response forwarding completes or fails.
Every terminal boundary is followed by a durable `completeness_watermark`
listing all currently pending observation IDs and the sequence it covers. A
start without a terminal, a sequence gap, an observer fatal record, a
non-healthy watermark, or an observer death before the required watermark
leaves the case unavailable. Journal and metrics-artifact write failures mark
the observer fatal; they are never converted into a partial positive record.

## Native metrics capture

There are two explicit capture paths.

1. The preferred path observes the actual `/metrics` ASGI response. A
   complete HTTP 200 response is retained as exact raw Prometheus bytes under
   the artifact directory. The terminal record binds the bytes to a unique
   `scrape_id`, optional `before`/`after` phase, response completeness, the
   server clock window, and the raw SHA-256. The request itself is classified
   `observer`, even if it has no physical request ID.
2. Code that owns the actual vLLM registry can call
   `ServingObserver.capture_registry_snapshot(...)`. If no registry is
   supplied, `make_prometheus_registry_sampler()` resolves
   `vllm.v1.metrics.prometheus.get_prometheus_registry`, the helper used by the
   pinned vLLM `mount_metrics` implementation. It then calls
   `prometheus_client.generate_latest` on that returned registry. It never
   uses the process-global `REGISTRY` as a substitute. If the pinned helper or
   registry is unavailable, the journal receives an explicit unavailable
   snapshot.

The pinned vLLM source mounts `/metrics` with the registry returned by
`get_prometheus_registry` and passes that same registry to
`make_asgi_app(registry=registry)`; see the
[vLLM 0.10.0 source](https://raw.githubusercontent.com/vllm-project/vllm/v0.10.0/vllm/entrypoints/openai/api_server.py#L305-L332).
Its API server accepts a dotted class through `--middleware` and installs it
with `app.add_middleware`; see
[the vLLM middleware loader](https://raw.githubusercontent.com/vllm-project/vllm/v0.10.0/vllm/entrypoints/openai/api_server.py#L1413-L1423).

When the vLLM frontend uses a multiprocess metrics directory, the observed
`/metrics` response is the safest default because it captures the bytes from
the mounted endpoint itself. The in-process helper uses vLLM's registry
factory rather than constructing a second registry or manually merging worker
files. No registry capture sends an inference request or waits for a counter
to become favorable.

The existing proxy-side serving collector supplies these headers on its
before and after metrics GETs:

```text
X-EIC-Scrape-ID: <unique scrape ID>
X-EIC-Scrape-Phase: before | after
X-EIC-Observer-Request: 1
```

It deliberately does not send `X-EIC-Physical-Request-ID` on those GETs. The
local request record and the server observer record are joined by exact scrape
ID and raw SHA-256. A generated server scrape ID is still retained when an
older collector does not send these headers, but the deferred command should
then be given the generated IDs explicitly after checking the journal.

## Launch configuration

The middleware is intentionally strict about durable paths and server binding.
The minimum vLLM environment is:

```text
EIC_SERVING_OBSERVER_JOURNAL=/absolute/run-dir/serving-observer.jsonl
EIC_SERVING_OBSERVER_ARTIFACT_DIR=/absolute/run-dir/serving-observer-artifacts
EIC_SERVER_IDENTITY=worker-22-server
EIC_SERVER_LEASE_ID=worker-22-lease-<attempt>
EIC_COUNTER_EPOCH=worker-22-vllm-<server-start>
EIC_SERVER_DEDICATED=true
```

The observer source must be importable in the vLLM process. The launch entry
then adds:

```text
--middleware agentic_sim.telemetry.serving_observer.ServingObserver
```

For the smallest server installation, copy just
`src/agentic_sim/telemetry/serving_observer.py` into a dedicated immutable
directory and use `PYTHONPATH=/absolute/observer-source` with
`--middleware serving_observer.ServingObserver`. This bypasses the repository's
`agentic_sim.telemetry.__init__` imports and requires no other repository
source on the server. The file imports only the Python standard library;
registry sampling additionally imports the installed `prometheus_client` and
`vllm.v1.metrics.prometheus.get_prometheus_registry`. Use the actual Python 3.11
vLLM environment at
`/home/hice1/jriverah3/scratch/eic-work/vllm-venv/bin/python`. Its package root is
`/home/hice1/jriverah3/scratch/eic-work/vllm-venv/lib/python3.11/site-packages/vllm`.
These are deployment instructions for main, not actions performed by this change.

Use a new journal filename and artifact directory per server process/start;
an existing nonempty journal is refused. The six environment variables above
are the complete minimum for attribution. Postcompletion delays default to
empty, so no background registry samples run unless explicitly configured.
Registry availability is recorded in the header even with sampling disabled.

Run deferred derivation locally from this repository checkout. Its source
dependencies are the observer module, the existing `serving_metrics.py`,
`telemetry/clock.py`, and `observability/vllm_metrics.py`, plus the repository's
package initializers and their imports. Keeping the current `src/agentic_sim`
tree and the CLI at its repository-relative path satisfies those imports;
copying the CLI alone does not. Derivation needs no vLLM/GPU installation or
network access. Retain the raw artifact paths named by the journal, or relocate
an archived copy with an explicit documented path mapping before derivation.

`EIC_SERVER_DEDICATED=true` and a real lease ID are required for a positive
sidecar. If they are omitted, the observer still retains raw request evidence
but the sidecar rejects native attribution. If
`EIC_SERVING_OBSERVER_POSTCOMPLETION_DELAYS` is set to a comma-separated list
such as `0.05,0.25`, the observer resolves the actual vLLM registry when
available and schedules those bounded samples after a model terminal. A
missing registry produces explicit unavailable sample records. The request
path never waits for these tasks, and the next request is never held for a
counter update.

The counter epoch is a server-start binding. It must change when the native
registry is reset or the API server is restarted. A generated observer epoch
is retained for fixture use but a production lease should provide the explicit
epoch in the environment.

## Deferred attribution

The posthoc command writes a new sidecar and never edits the original request
or serving record:

```bash
PYTHONPATH=/path/to/repo/src python3 \
  /path/to/repo/scripts/observability/derive_server_attribution.py \
  --journal /absolute/run-dir/serving-observer.jsonl \
  --request-id <physical-request-id> \
  --before-scrape-id <before-scrape-id> \
  --after-scrape-id <after-scrape-id> \
  --output /absolute/run-dir/server-attribution.jsonl
```

The output is append-only. It appends an unavailable sidecar with a reason
when evidence is insufficient, and returns exit status 3. A measured sidecar
returns status 0. Reusing an output request ID or overwriting an existing
sidecar is rejected.

Before invoking the existing native derivation, the command validates:

- contiguous journal sequence numbers and exact source, clock, boot, process,
  server, epoch, and observer-source identity on every record;
- a complete raw artifact and exact SHA-256 for both selected scrapes;
- increasing, non-overlapping before/after windows in the server clock;
- a target request with exactly one start and terminal, classified `model`,
  with the before scrape ending before ingress and the after scrape starting
  after terminalization;
- every request that entered the full window, including pending starts;
  competing model requests and `foreign` requests fail closed, while only
  classified read-only observer traffic is permitted;
- no unselected or late prior postcompletion metric sample in the window;
- a healthy durable watermark after the after-scrape sequence covering its
  end, with no fatal observer record or early shutdown; and
- a dedicated server lease derived from the observer's own ingress stream.

The command then constructs the existing `AccessWitness` from that server
evidence and calls `derive_serving_metrics`. Consequently every claimed
queue, prefill, decode, and E2E value still requires the existing native
histogram count delta of exactly one, finite non-negative values, unchanged
series labels, no reset, and matching server epoch/clock. A proxy elapsed time
is never substituted. If a delayed publication makes a count delta
ambiguous, the sidecar stays unavailable even when a later access record looks
favorable.

**Remaining attribution limitation:** this middleware has no engine-owned
publication/cancellation acknowledgement. Therefore the bounded CLI rejects
a target if any earlier model or foreign request in the observer epoch ended
before its baseline: HTTP completion and a later count delta of one cannot
prove that prior updates had settled. In practice, subsequent inference
requests in the same epoch remain unavailable until main reviews a real native
publication proof. This restriction does not prevent collecting every request
and scrape, and neither a fabricated flush marker nor a new epoch within the
same live process should be used to bypass it. Missing histogram families on a
first-model baseline likewise remain unavailable under the existing checks.

The local collector must join its selected before/after snapshots by explicit
scrape IDs and compare their raw hashes with the sidecar's `scrapes` fields.
The CLI validates server artifact hashes; it does not read the local proxy
record or compare client and server monotonic timestamps. Per-request full
journal reads are retained for this bounded implementation; batch indexing is
deferred.

## Review and rollout gate

Main should review the artifacts and then perform one controlled endpoint
restart with the actual pinned environment. The first proof should establish
the following on the selected endpoint before extending the change:

1. the launched middleware source hash, server PID/start identity, lease, and
   epoch match the journal header;
2. the real vLLM registry/`/metrics` response is the source of the exact raw
   bytes and the local before/after scrape records join by ID and hash;
3. model streaming, model failure, client disconnect, health probes, unknown
   routes, and a concurrent foreign request all appear in one complete
   sequence; and
4. the observer's CPU, memory, raw-artifact, journal-fsync, and bounded-sample
   overhead passes the existing measurement gate.

Direct backend access must be ruled out or separately observed before calling
the server dedicated. The observer covers the ASGI process that receives the
request; it does not prove that an unobserved socket, another API-server
process, or an aliased endpoint cannot reach the engine. A server restart,
counter reset, missing sequence, observer death, cross-clock artifact, and
foreign overlap must all yield unavailable attribution.

## Opt-in native finished-request adapter

`native_vllm_observer.py` implements the hook supplied by main from the actual
pinned sources in `/tmp/vllm-native-timing-source-details.txt`. Installation
dynamically imports vLLM, checks version `0.10.0` and the synchronous callback
signature, and wraps
`OutputProcessor._update_stats_from_finished(self, req_state, finish_reason, iteration_stats)`.
The original executes exactly once with unchanged arguments. Its result or
exception is preserved. After it returns, capture requires the same
`iteration_stats.finished_requests` list to have grown by exactly one; only
that new `FinishedRequestStats` object is copied.

The native journal retains `req_state.request_id`, the actual parent ID,
`arrival_time`, `queued_ts`, `scheduled_ts`, `first_token_ts`, `last_token_ts`,
and native finished fields `e2e_latency`, `queued_time`, `prefill_time`,
`decode_time`, `inference_time`, prompt/generated token counts,
`max_tokens_param`, and finish reason. Engine timestamps and phase durations
are labelled `engine_core.time.monotonic`; arrival/E2E retain frontend wall
clock semantics. Values are native serving phases, including the source's
preemption semantics, not CUDA kernel time. No cross-domain subtraction is
used in derivation.

Deployment adds the **sibling** `native_vllm_observer.py` beside
`serving_observer.py` in the standalone import directory, or retains both in
the package tree. Both files import stdlib only before installation. Keep the
existing ASGI environment and add:

```text
EIC_NATIVE_VLLM_OBSERVER=true
EIC_NATIVE_VLLM_JOURNAL=/absolute/new-server-run/native-vllm.jsonl
EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256=345bfdf8bff300d8fab6291cf7956add1cf2b7d40bf6b7ff6b1647864ab353cc
EIC_SERVING_OBSERVER_POSTCOMPLETION_DELAYS=
```

An optional `EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256` enforces the exact SHA-256
of `inspect.getsource(OutputProcessor._update_stats_from_finished)` before
patching. The native header always records that source hash, signature/hash,
full output-processor and stats source hashes, and adapter source hash. The
ASGI header binds the native journal instance and those same source hashes.
Startup provenance also records actual frontend `time.get_clock_info` results
for wall and monotonic clocks, frontend PID/host/boot identity, and the installed
stats source hash defining engine event/batch timestamp origins and frontend
E2E calculation. This is source-bound engine clock provenance: the frontend
hook has no engine-process runtime clock probe and explicitly records that
absence. It does not prove engine-host/PID identity or clock alignment. Main's
topology/startup evidence must supply those separately if required.
The startup binding also hashes installed `v1/engine/__init__.py` and names
the confirmed `EngineCoreEvent.new_event` and `EngineCoreOutputs.__post_init__`
`time.monotonic()` creation sites. Main's durable reference is
`N/verification/native-vllm-source-binding.json`; its supplied output-processor
and stats hashes are respectively
`c4452092ae3adad2d2b4e1e7d99b959ed3c9308925eb22bf1d55ed0b2ae7748f`
and `933a0981a1f708788f6ad541659d54e6626795fa6e91421e578293db7c24c91a`.
Native capture is **off by default**. No extra inference or registry polling
is introduced by enabling it. Stats must already be enabled in the actual
serving process; an absent `iteration_stats` produces native unavailability.

Native records use the separate schema `assignment.native-vllm-observer.v1`:
`native_header`, `native_finished`, `native_http_terminal`, `native_watermark`,
`native_error`, and `native_shutdown`. Raw scalar projections are canonical JSON inline in each
finished record with their own hash/byte count. Sequence and every record's
server PID/start, observer ID, epoch, and capture-clock identity are bound to
the ASGI owner. A watermark links the native durable sequence to an ASGI
sequence. Capture errors invalidate native evidence without replacing the
original callback output or stopping HTTP observation. The hook is restored
when its owning observer closes.

Every non-observer HTTP terminal also gets `native_http_terminal` reconciliation
with its exact ASGI terminal sequence/IDs and an unverified native status.
Aborts, disconnects, validation failures, and disabled iteration stats may
never yield a native finished record. Reconciliation alone never counts as a
native completion; deferred derivation explicitly reports unavailable for a
missing or invalid matching finish. This observer does not claim all engine
work is captured by the finish callback.

Main's proxy must forward both `X-EIC-Physical-Request-ID` and
`X-Request-Id` with the same physical ID for model calls. The observer records
the exact latter header and vLLM's actual `request_metadata.request_id` from
the request scope. Initial native derivation supports POST
`/v1/chat/completions` only and requires all three identities to agree with
the pinned `chatcmpl-<physical-ID>` construction. It requires exactly one
unparented native engine completion with that exact ID. Unknown/foreign IDs
and all parented or duplicate/multiple completion records are retained but
cannot produce a positive join. No fuzzy prefix or timestamp matching occurs.

```bash
python3 scripts/observability/derive_server_attribution.py \
  --journal /absolute/new-server-run/serving-observer.jsonl \
  --native-journal /absolute/new-server-run/native-vllm.jsonl \
  --request-id <physical-request-id> \
  --output /absolute/analysis/native-attribution.jsonl
```

This explicit direct-native mode binds both full journal hashes, the raw
native projection hash/sequence, source binding, and both completeness
watermarks. It validates a complete successful HTTP target, native ID
multiplicity, ingress/foreign/pending overlap, capture errors, and clock/epoch
identity. Native values are converted from seconds to finite milliseconds;
their `count_delta` is null and their source is
`vllm_v1_finished_request_stats`. No scrape pair or aggregate attribution is
required in this mode. Existing Prometheus records and unavailable aggregate
sidecars remain separate and unchanged.

The finished hook precedes `logger_manager.record` in the supplied source;
therefore it does **not** prove aggregate histogram publication. The aggregate
mode's prior-publication rejection remains in force. Direct request-bound
native values can recover successive serial requests without that aggregate
assumption. The bounded implementation does not generalize multi-child or
beam-search timing, infer cancellation completion, or repair missing native
records. Failed/disconnected HTTP targets remain unavailable.

Recovery uses a fresh sidecar output path because `append_sidecar` rejects a
second result for an existing physical request ID. Preserve the previous
unavailable sidecar; downstream selection must name the exact native evidence,
not choose whichever result is measured. Use one writer per output: the
existing duplicate-ID precheck occurs before its file lock. Batch indexing
and a revision framework remain outside this implementation.

ASGI scrape brackets now equal the durable request ingress and terminal
bounds, with `captured_monotonic_ns` equal to the successful final-send time.
Watermark coverage cannot exceed the watermark's own timestamp. All registry
sample paths reject returned data exceeding `max_metrics_bytes` before
archiving; this bounds retained bytes, not `generate_latest` allocation/time
or the number of outstanding optional sampler tasks. Keep optional registry
sampling disabled for rollout. Main's controlled restart and live overhead
validation are still required; the local native tests use signature-compatible
fixtures, not an installed GPU server.

## Proxy `native_deferred` mode

The proxy's `serving_metrics` descriptor accepts an optional `mode`:
`per_request_scrape` (default when omitted; existing config hashes are
unchanged) or `native_deferred`. In `native_deferred` the proxy performs **no**
`/metrics` scrape before or after the physical request and does not read the
access-witness file, so the measured request window carries no extra remote
round trips. It still binds the physical ID in `X-Request-Id`, writes the
original serving record as `status=unavailable` with
`attribution_mode=native_deferred`, `capture.scrape_count=0`, `witness=null`
and `witness_source=null`, and keeps the v2 terminal row's serving fields
unavailable. Attribution is derived afterwards from the server archive by
`derive_server_attribution(..., native_journal=...)`; the fixed-work adapter's
`_model_native_results` accepts such originals without a scrape bracket
because the native derivation already requires an ASGI and a native
completeness watermark past the target terminal, exact body-hash joins, cold
reset proof and no overlapping ingress. An original that claims
`native_deferred` but records a non-zero scrape count, or a measured status,
is rejected. This is the accepted production serving-measurement path; the
per-request scrape mode remains available for comparison and for servers
without the native adapter.
