# Offline CPU capture overhead extraction — 2026-09-09

All six existing pairs / 12 conditions were extracted without running a
workload. The 64.17% file and 68.50% test median paired overheads reproduce.
The strongest timing evidence points to extra script-prestate queries and
fixed tracepoint detachment. Pure fsync, native-flush, and Docker-stop costs
were not separately timed and cannot honestly be assigned exact durations.
The previously used 5%/10% thresholds are internal targets, not immutable PDF
requirements. The original 64–69% measurements remain intact; no waiver or new
acceptance decision is made here. Main will decide acceptance after optimization
and review of individual-event distribution representativeness, not percentage
alone. This extraction does not evaluate that representativeness criterion.

Source: `20260908T140000Z-offline-v2/verification/fixed-work-fixtures-20260909T022827Z-cpu-subset/handoff.json`.
Its referenced CPU evidence SHA is
`15d0a0fdd0bcea9bd0fef5e013d3c5496f4b2671c1b711914e8fe3a68a3dc225`.

New durable evidence directory:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/verification/cpu-capture-overhead-extraction-20260909-lunamax-rmv2jsgc
```

`analysis.json` SHA:
`6b06c3aec258d7f4bb24f90347731f02a9957b17df92a0fa0e3ecd92054fbab2`.
It retains input hashes, all condition metrics/counts, and exact action-ID,
command-hash, hook pre-event, terminal-event, and BPF-token joins. The directory
also contains `paired_calls.csv` (24 paired declared commands),
`timeline.jsonl` (native-clock intervals, with nested spans explicitly named),
the ten hash-matching execution-source files, analyzer/tests, and an artifact
hash manifest. Native packet bytes were decoded offline and checked against
per-token required/stored counts; no capture stream was changed.

## Measured timings

Numbers below are medians across three repeats, in milliseconds. Independent
medians are not an additive decomposition. The driver interval begins after
the pre-action callbacks and includes terminal callbacks before recording;
it excludes the extra prestate query and the runtime-action journal append.

| Quantity | File: 2 commands | Test/edit/subprocess: 6 commands |
| --- | ---: | ---: |
| Control work | 630.317 | 1358.173 |
| Instrumented work | 1034.770 | 2338.585 |
| Paired added work | 404.453 | 927.713 |
| Script-prestate spans (1 / 4 queries) | 137.573 | 547.823 |
| Nested `pwd` runtime-wrapper intervals | 117.670 | 465.004 |
| Tracepoint detach interval | 132.960 | 147.242 |
| Declared-command driver interval increase | 37.106 | 107.595 |
| Tool start → runtime start, summed | 13.994 | 41.660 |
| Runtime end → tool end, summed | 21.051 | 63.501 |
| All cgroup/host-resource snapshot brackets, nested | 8.341 | 26.853 |
| Entire instrumented teardown | 524.382 | 543.828 |
| Collector startup, **outside work** | 1598.560 | 1596.857 |

The script spans enclose fresh cwd observation, native script reads, script
artifact handling, and collector work. The nested runtime interval for `pwd`
includes collector startup for that auxiliary action; it is not pure command
execution. The resource-snapshot times are nested in boundaries, not additional
terms to add to the table. The median paired residual after subtracting query
spans, detachment, and driver increases is approximately 98/153 ms. This is
unallocated bookkeeping/teardown/scheduling/clock-domain residual, not fsync time.

| Fixture / repeat / order | Off work | On work | Added work | Queries | Detach |
| --- | ---: | ---: | ---: | ---: | ---: |
| File / 0 / off-on | 630.467 | 1021.829 | 391.361 | 137.573 | 122.224 |
| File / 1 / on-off | 630.317 | 1034.770 | 404.453 | 137.267 | 132.960 |
| File / 2 / off-on | 626.465 | 1100.418 | 473.953 | 140.247 | 134.398 |
| Test / 0 / off-on | 1410.872 | 2338.585 | 927.713 | 547.823 | 147.242 |
| Test / 1 / on-off | 1358.173 | 2381.444 | 1023.271 | 550.223 | 153.442 |
| Test / 2 / off-on | 1339.747 | 2257.410 | 917.664 | 537.719 | 145.117 |

The wall-clock marker written before the closed collector manifest separates
teardown approximately into 153/169 ms before that marker and 370/375 ms after.
The latter includes manifest/summary writes, service exit/join, and environment
shutdown; it is **not a measured Docker-stop-only interval**. Control work minus
its driver intervals is approximately 364/382 ms using the separate medians,
so much of the tail may be shared container/adapter work. Control has no
separate teardown timer to prove the exact increment. There are zero deferred
action finalizations in all six captures. Normal stop still detaches, drains,
closes the native sink, hashes bytes, and persists final summaries.

## Actual counts and matched call examples

| Per instrumented condition | File | Test |
| --- | ---: | ---: |
| Tool intents / paired tool spans | 2 / 2 | 6 / 6 |
| Paired lifecycle spans / client spans within them | 10 / 4 | 21 / 12 |
| V2 records: whole condition / work phase | 26 / 19 | 60 / 53 |
| BPF actions: declared plus `pwd` | 3 | 10 |
| Durable boundary / aggregate rows | 6 / 3 | 20 / 10 |
| Native stored packets | 1230 | 4001 |
| Lost packets / callback errors / deferred finalizations | 0 / 0 / 0 | 0 / 0 / 0 |

The pinned driver invokes five hook methods per declared command: step start,
actions generated, action started, action executed, step done. This gives
10/30 **source-inferred calls**, corroborated by the journals, not separately
timed function-entry records. Native accepted sample callbacks are inferred
from one accepted 352-byte packet per callback and zero errors. Perf poll
wakeups and kernel callback counts are not recorded.

The pinned v2 writer fsyncs every appended row: 26/60 whole-condition calls,
19/53 within work, inferred from source plus completed rows. Raw-event action
durability syncs occur 3/10 times. Their durations are unknown. Runtime-action
record fsyncs occur in both controls and treatments. The native sink buffers
packets and does not fsync per packet. In this exact baseline,
`_start_raw_event_sync` already overlaps the raw sync with the boundary-journal
write and joins before returning. Proposing that overlap again is redundant.

Representative pairs are selected by median **paired overhead**, not fastest
runtime: file repeat 1 and test repeat 2. These are driver intervals plus
separately identified prestate queries, in ms; full commands/hashes and event
IDs remain in the CSV and JSON.

| Pair/action | Off driver | On driver | Earlier prestate query |
| --- | ---: | ---: | ---: |
| File 1/0: `python3 file_traversal.py` | 149.661 | 168.706 | 137.267 |
| File 1/1: find/sort/wc | 117.637 | 134.501 | 0 |
| Test 2/0: `python3 run_fixture.py` | 144.136 | 157.825 | 135.470 |
| Test 2/1: `python3 edit_fixture.py` | 137.824 | 151.357 | 133.429 |
| Test 2/2: `python3 run_fixture.py` after edit | 144.248 | 156.959 | 132.436 |
| Test 2/3: unittest discovery | 260.549 | 268.241 | 0 |
| Test 2/4: find/sort/wc | 117.684 | 133.187 | 0 |
| Test 2/5: `python3 subprocess_fixture.py` | 155.801 | 174.325 | 136.384 |

For example file 1/0 joins hook pre-event
`event-c3b0b93719ac9d2ff06d497aa2905627` to BPF token
`16751878998296391944` and exactly 750 packets. Its separate cwd query joins
`event-8824fc6e21f71b1d7baacdfa989ba4e5` to token
`8049341344482379175` and 42 packets. Query work is not attributed to the
declared Python command. Each of the four test cwd queries also has 42 packets.

## Ranked bounded implementation candidates for owner review

1. **Reduce internal cwd-query round trips while retaining fresh prestate.**
   The measured `pwd` wrapper occupies about 118 ms once or 465 ms across four
   queries; this is an opportunity bound, not promised savings. A concrete
   candidate is same-boundary `/proc/<mapped-host-shell-pid>/cwd` readlink with
   PID start-tick/namespace checks before and after, through the existing
   collector identity. Retain the raw cwd witness and measured state-query span,
   read script contents freshly through the native API, and retain measured
   `pwd` fallback if identity/access checks fail. Do not cache cwd across actions
   or reuse pre-edit contents. This targets the extra shell round trip while
   preserving the observation contract.
2. **Optimize the four serial tracepoint detach operations.** Their aggregate
   122–153 ms is directly timed and paid once per condition. A bounded candidate
   is overlapping independent detach requests only after confirming BCC handle
   safety, with all detach completions joined before final draining/map release.
   Keep producer freeze, complete record accounting, durability, and the existing
   work-end boundary. The journals do not prove which individual detach is slow
   or that concurrency is safe; Carson's separate profile should decide this
   implementation. No detach may be skipped merely because pending count is zero.
3. **Reduce repeated durable-writer bookkeeping, preserving every sync point.**
   Keep append descriptors open safely, retain interprocess locking, and avoid
   repeatedly encoding immutable metadata. There are 19/53 work-phase v2 appends
   and roughly 35/105 ms in the tool pre/post-runtime brackets, which also include
   RPC, maps, snapshots and serialization. These are bounds on mixed work, not
   predicted fsync savings. Dropping records or weakening durability is unsupported.
4. **Overlap only independent final work after producer freeze.** Final
   checksum/summary/service completion may overlap environment shutdown if the
   owner retains all raw evidence, joins both branches before `work_ended`, and
   preserves failure propagation. The combined tail is measured; its pure
   components and achievable savings are not. Ordinary Docker teardown also
   exists in controls, so this ranks below the proven extra query/detach costs.

Cgroup resource reads are measured but small (8/27 ms nested totals). Native
packet reduction, removing fsyncs, moving finalization outside the measured
window, and changing acquisition settings are not justified by this evidence.
Collector startup matters to startup latency but cannot explain these reported
work-overhead percentages.

## Reproduction and limits

The new standard-library helper is
`scripts/validation/analyze_cpu_capture_overhead.py`:

```text
python3 -B scripts/validation/analyze_cpu_capture_overhead.py
  --handoff <original-cpu-subset>/handoff.json
  --source-snapshot <new-evidence>/captured_source
  --output-dir <new-nonexistent-directory>
```

Four focused tests pass: exact pre-event pairing, incomplete/duplicate spans,
clock mismatch rejection, and binary count/envelope checks without summing
overlapping events. The helper refuses existing output directories, verifies
source/result/raw hashes, and rereads consumed inputs to detect mutation.

Telemetry uses `CLOCK_MONOTONIC_RAW`; native event/detach timestamps use
`CLOCK_MONOTONIC`; driver calls use realtime; outer work uses perf-counter
durations. Absolute times from unlike domains are never subtracted. Wall-marker
splits and residual comparisons are labeled approximate, with millisecond UTC
resolution and no assertion that clocks have identical rate. Nested intervals
must not be summed as independent costs. No exact syscall-fsync duration,
callback CPU time, post-detach native-flush time, or pure container teardown is
available. The analysis supplies bounded targets for Carson/Main, not a new
methodology or a claim that the overhead gate passes. No GPU work, new replay,
profile execution, core-source change, or configuration change was performed.
