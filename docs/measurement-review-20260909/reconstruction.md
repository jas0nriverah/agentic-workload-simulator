# Saved-artifact reconstruction review

Scope: a bounded, read-only reconstruction of the closed `combined-case-v8`
integration fixture. The fixture is useful proof that the acquisition path
survives loss of the H100 allocation; it is not configuration-selection or
production data. The literal coding-test PDF requires (D7–D8) individual CPU
and GPU events and an E2E explanation, and (D9) event labels, hardware-aware
inputs, and an E2E label that can be evaluated after collection. This review
checks whether those quantities are reconstructible from saved bytes.

Frozen evidence root: `astra-combined-preflight-20260909-9cP7NP` (abbreviated
as `$P` below). All commands read `$P` and wrote only temporary state. The
source decoder was `$P/source-v8/src/agentic_sim/telemetry/bpf_work.py`.

## Reconstructed evidence

| Required quantity | Reconstructed source and result | Status |
| --- | --- | --- |
| Individual CPU operations | `$P/combined-case-v8/.../linux_work/raw_events.bin`, SHA-256 `937ccb24b2d1262b270ccc34e54d3cb1f2707b242fc4829e3f428f53601a3d95`, plus manifest and aggregate journal. A bounded decode of the first action's exact 0–56,000 byte range (v3 ABI, 400 bytes/record) yielded 140 records for its exact action token, equal to both saved range count and aggregate required count. The full file hash was recomputed. | Proven for a representative saved action; the fixture's strict audit reports 846,533 records. |
| CPU loss and action identity | `work_summary.json` says 100 action rows are `event_records_complete`, with 846,533 required and decoded raw events. Summed `perf_lost_events`, `lost_event_records`, `lost_path_records`, `lost_pending_records`, and `lineage_map_failures` are all zero. Each action is bound to run/attempt/case/container/pid/start-ticks. | Proven. |
| CPU event semantics | Decoded rows retain token, sequence, syscall kind/status, return value, kernel start/end, inline path status/value, and scalar arguments. Independent bounded decoding also recovered `openat` packets with pathname, arguments, status, time, and action token. Kernel times are `CLOCK_MONOTONIC`; they are not used in host E2E arithmetic. | Proven, with the stated collector limit: return bytes are syscall-facing, not physical disk bytes. |
| Native GPU/request event | Raw `native-vllm.jsonl` has 1 header, 40 `native_http_terminal`, 40 `native_finished`, and 82 watermarks. Fresh reconstruction found 40 physical model requests, 40 exact native finished joins, no unmatched finished records, and zero recomputed native raw-hash mismatches. | Proven. |
| Request/retry/identity join | The fresh reconstruction uses only `physical_request_id -> exact ASGI observation -> chatcmpl-<physical_request_id> -> raw.engine_request_id`; it has no timestamp or ordinal fallback. A representative request has exact chain status, retry index 0, and identical native raw bytes/hash. Its model row has prompt/context 1,410 and output 100; native measurement has prompt 1,410, completion 100, cache 0, queue 0.074 ms, prefill 1,269.020 ms, decode 1,062.033 ms, native E2E 2,360.073 ms. | Proven. |
| CPU/GPU host separation and clocks | Tool/lifecycle/model rows share one exact execution-host `CLOCK_MONOTONIC_RAW` descriptor and run/attempt/case identity. Native server rows retain their separate host, process start ticks, server identity, counter epoch, native clock declarations, and raw phase values. The reconstruction explicitly performs no cross-host or cross-clock subtraction. | Proven. |
| E2E decomposition | Fresh host-clock audit reconstructed outer E2E 153,117.581 ms, exclusive attributed union 145,814.381 ms, unknown residual 7,303.200 ms, and zero closure error. It retains 335 nonspan points and excludes outer wrappers. The union is not a sum of overlapping phases, and BPF kernel timestamps are excluded. | Proven. |
| D1–D6 representative row | Freshly recomputed from `case_spec.json`, `case_result.json`, raw native journal, and `merged-v2-proof.json`: Verified `django__django-7530`, `django/django`, seven effective settings, official resolved=true, 40 physical requests, tool union 32,634.391 ms, native prefill/decode/queue/E2E sums 3,407.418 / 74,575.329 / 1.922 / 78,120.326 ms, ratio 0.4184821984. All five quantities compared equal to `reconstruction-v8/representative-deliverable-input.json`. | Proven; fixture only. |
| Evaluator/attempt/patch provenance | Exact case-result to direct attempt evaluator result has submitted=true and official_resolved=true. Two saved patch paths have the same SHA-256 `5355e0b218dbd1d2b3c5c823ee980cce20a4144f5a48b6034218016571e055c7`. | Proven. |

## Commands and bounded checks

The CPU check used `iter_bpf_events` on one action's manifest-bounded byte
range and token filter, after hashing the entire binary. It did not emit a
large decoded export. Native, request, and clock checks invoked the frozen
`reconstruct_saved_evidence_v2.py` functions over the 0.96 MB model journal,
0.27 MB native journal, 0.22 MB observer journal, and host journals. The
reconstructor's clock policy is explicit: native request identity is joined by
IDs, while native and host clocks remain separate.

The retained compact helper and result are
`reconstruction/reconstruct_bounded.py` (SHA-256
`8caef5599a86e0c9dfec0d144ae358dbee260276536d1824fd25cac64ff5aaf4`) and
`reconstruction/reconstruction-result-v2.json` (SHA-256
`f0d635f3591b414a87b51e092e8778497061aa31fc545ff8fd33fb6ace74f3c2`). They
are reproducible with:

```
python3 docs/measurement-review-20260909/reconstruction/reconstruct_bounded.py \
  --evidence-root "$P" \
  --output docs/measurement-review-20260909/reconstruction/reconstruction-result-rerun.json
```

The compact result retains actual read, write, and `getdents64` rows selected
from their own manifest-bounded action ranges. Each has the action token,
status, syscall-return byte count, kernel start/end and duration, and scalar
arguments. The saved examples are: read return 1 byte / 10,680 ns; write
return 14 bytes / 13,970 ns; `getdents64` return 680 bytes / 198,230 ns.
These are syscall events, not physical-storage measurements.

The negative suite was also rerun read-only:

```
python3 "$P/reconstruction-tools-v2/test_reconstruction_negatives_v2.py" \
  --helper "$P/reconstruction-tools-v2/reconstruct_saved_evidence_v2.py" \
  --native-fixture "$V/acquisition-evidence-export-20260909-v9/source_artifacts/source-44c3e66768f74b6b3e45" \
  --lifecycle-fixture "$V/astra-bpf-v3-live-v5"
```

Result: `3 bounded raw-evidence negative checks passed`. The established
falsification artifacts also reject wrong case identity and a corrupted,
re-hashed archive member. This confirms that the reconstruction does not
silently accept those two relevant provenance failures.

## Interpretation of the apparent zero counters

`action_finalizations=0` is not a count of CPU events. It means this closed
fixture needed no deferred descendant finalization rows: all 100 raw boundaries
had deferred=0, in-flight=0, and incomplete=0. `path_records=0` is likewise
not a count of paths or decoded operations. It counts a duplicate sidecar that
is unnecessary when descriptors remain inline in the raw packets; independent
bounded decoding recovered `openat` pathname/argument/status/time/action-token
records. The raw action rows still contain 846,533 complete, lossless
individual syscall records. Neither zero invalidates the raw-event acquisition
path.

Path-specific explanations must nevertheless be conditional: an individual
future action whose inline path is unknown or truncated may support an
operation-type and latency claim, but cannot support a claim about a particular
file or physical storage traffic. That is a reporting constraint, not an
acquisition repair. The PDF requires event logging; it does not require
unmeasured physical-disk attribution.

## Production consequence

No concrete acquisition defect was found in this representative saved-byte
path. The final matrix still must preserve, for every included case: closed
case/attempt/evaluator artifacts; v2 lifecycle/tool/model journals; raw BPF
binary + manifest + aggregate journal; archived native/observer journals; and
hardware/runtime manifests. The per-case closure gate must reject missing or
ambiguous physical-request joins, nonzero loss/censoring where individual-event
claims would be made, clock/identity conflicts, or missing evaluator linkage.

The saved data are sufficient to construct D7–D8 event tables and E2E
breakdowns, and to form D9 observed event/E2E labels and provenance after GPU
release. They do not establish D9's 25% per-event or E2E criterion; that
remains a final held-out evaluation obligation. Hardware inventory and the
saved request features support hardware-parameterized modeling, while native
phase clocks and host-clock spans must remain separately attributed.
