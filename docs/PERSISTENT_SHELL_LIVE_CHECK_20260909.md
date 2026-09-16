# Persistent shell live check — 2026-09-09

The canonical final bounded CPU-only live run is retained at

`/tmp/assignment-persistent-shell-live-20260909-run7`

It passed against an actual Docker-backed SWE-ReX persistent bash session with
no explicit target mapping in the collector configuration. Run5 is also
preserved as a passing no-target retry. The earlier accepted run4 artifact at
`/tmp/assignment-persistent-shell-live-20260909-run4` is preserved unchanged;
run4 had 14 actions and 3,275 native records. Run7 has 14 tool actions and
3,279 native records; setup and runtime trace volume varies between runs.

The run used the pinned SWE-agent checkout at commit
`0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`, SWE-agent `1.1.0`, the checkout's
`.venv`, and SWE-ReX `1.4.0`. It used no model inference, GPU inference, or
production SWE-bench case.

The container image was derived from
`swebench/sweb.eval.x86_64.psf_1776_requests-1724@sha256:e369005d38858ea90f843d853cb8427a2681b7513b846d12a34cb8d52c00763e`.
The derived image tag was
`assignment-persistent-shell-swe-rex-1-4-0:20260909`, image ID
`sha256:020f3ef4e19a5597bc4a4de1609c4d408904b3ab307939861cb1397876ef10e8`.
The retained `pinned.Dockerfile` installs and verifies `swe-rex==1.4.0`.

## Approved hook fix

Required collector bootstrap now opens a measured
`persistent_shell_pid_discovery` state-query span before the internal
`env.communicate` PID witness. BPF action calls are suspended only while that
exact span is active and the target does not yet exist. The runtime wrapper
still records the exact discovery command, measured exit code, start/end
times, and the explicit suspension status on the lifecycle terminal row. Once
the target is mapped and persisted, the prior auxiliary span is restored and
the required BPF service starts normally.

The runtime wrapper recognizes the pinned `BashInterruptAction` type and emits
a measured `bash_interrupt_control` lifecycle span. Its action type, class,
session, parent timed-out tool event, and outcome are retained. `command` and
`command_sha256` are explicitly null because SWE-ReX supplies no command for
this control action; it receives no fabricated BPF command identity.

The focused unit coverage is in `tests/telemetry/test_v2.py`:

- bootstrap PID discovery does not call the collector before the target exists,
  preserves the discovery command, records measured lifecycle duration, and
  restores the setup auxiliary span;
- `BashInterruptAction` is recorded as a lifecycle control span with explicit
  no-command identity.

## Evidence exercised

The script sent 14 exact action strings through the existing hook callback
sequence. For each action, the retained `tool_intent`, measured tool terminal,
guarded `actual_action`, runtime command, SHA-256, physical action ID, and BPF
event ID were joined. The action statuses included:

- `export GIT_PAGER=cat PAGER=cat MANPAGER=cat`, followed by a same-shell
  witness of `GIT_PAGER=cat PAGER=cat MANPAGER=cat`.
- `cd /testbed && git log --oneline -n 4` with unpiped output.
- `git log --oneline -n 4 | cat` without another `cd`, with piped output.
- A native `SWEEnv.write_file` of script v1, execution, a shell `printf %b`
  edit to script v2, native post-edit readback, and execution of v2.
- `cd /tmp/.../subdir`, native creation of `relative.py`, and execution of
  `python3 relative.py` from the persisted cwd.
- An intentional Python exit status 7.
- A 0.5-second `CommandTimeoutError`, a successful `BashInterruptAction`, and
  two following commands in the same persistent shell. The first printed
  `recovered cwd=/tmp/.../subdir`; the second returned that directory from
  `pwd`.

The bootstrap lifecycle terminal retained a 155.06389 ms measured interval
with the exact PID witness command, `command_exit_code=0`,
`collection_suspended=true`, and
`work_collection_status=suspended_for_target_discovery`. The persisted witness
records `explicit_target_supplied=false`, container PID `8`, host PID
`1305925`, PID namespace `pid:[4026532803]`, start ticks `28881108`, and mapping source
`swerex_docker_persistent_bash_nspid`.

The interrupt lifecycle terminal is event
`event-0879c9e8b04c3af9c2535a402783cfc8`, parented to the timed-out tool event
`event-fad61ffb72b98a2741f674a60dd8bc6f`, with status `success`, runtime class
`BashInterruptAction`, runtime type `bash_interrupt`, and
`command_identity_available=false`. Its measured control interval was
611.965208 ms; `command` and `command_sha256` are both null.

The existing native container hook emitted three `script_read` records. Their
current prestate contents came from the hook's native `SWEEnv.read_file` calls
and are retained under `telemetry/script_artifacts/`:

| action | generation | cwd witness | bytes | SHA-256 |
| --- | ---: | --- | ---: | --- |
| script v1 execute | 6 | `/testbed` | 48 | `ae38db4ae0fde8c042cc57e603aa410d4ac5b29fbe42baebb6f0f785c8833da7` |
| script v2 execute | 9 | `/testbed` | 48 | `2aad5676ef32610683fc0acda0b9a96030429d8423498f1735300d446977d608` |
| relative script execute | 12 | `/tmp/.../subdir` | 50 | `aff2652b8e21e7004e0fa67ddf94a84c940d5ae58f76f36b01bee330a7698cec` |

The hook records these text artifacts as
`decoded_text_utf8_reencoding` with `byte_exact=false`; the validator checks
that metadata and compares the retained bytes to the expected source. The
post-edit native readback matched v2. The relative execution proves that the
hook used a live `pwd` witness for the current shell cwd.

## BPF capture

The required BCC service attached to the mapped persistent shell after the
hook completed target discovery, rather than to the Docker helper process. The
retained identity is:

- container PID `8`
- host PID `1305925`
- PID namespace `pid:[4026532803]`
- mapping source `swerex_docker_persistent_bash_nspid`
- start ticks `28881108`

The native binary stream contains 3,279 decoded 352-byte records (1,154,208
bytes), SHA-256
`d24b760e676f7e4c5b82e326e8eda8e6ac4fae82a54347d24555379397892143`.
The validator joined 17 BPF action identities: the 14 tool actions and three
hook-generated `pwd` state queries. It observed 34 boundary journal rows, one
collector-stop finalization for the timed-out token, zero perf/map loss fields,
and service return code 0. The discovery command is intentionally outside the
BPF action join because the collector did not yet exist; its measured
lifecycle row is the retained evidence for that interval. The BPF program
SHA-256 is
`bc4409011246d9d88ff8a818a4c4d43658e18656d11379137ad43105f07d53ec`.

Native sink hashes are:

- source: `ccf84ecc934a29f4d6281e27593772d9dd34589b079bb3c73bca6d2d2685ae60`
- library: `ec78760ab8ae392814ac627eabb38809aacf14c900faf3bd8f4fea1d4ea5f561`

Run7 retained hashes include:

- `result.json`: `b180a6b6e4510c3c912f078c9c2d27a06edc61fe9e8a29b9eb206fdeb5c3ee3d`;
- `persistent_shell_witness.json`:
  `fcc1f0dd7fd06dc4ca5f681ae6f553869f021ffcc0a67a22eae6294962d65d25`;
- `telemetry/lifecycle_events.jsonl`:
  `4aeda304efae019b74f5bbf65371800987577b72deb0b83f217cb83179518af3`;
- `linux_work/work_summary.json`:
  `801b75070f52868cddc7c324704c4289cac7feedef0f7781ebf2e6893a7c6571`;
- `linux_work/bpf_collector_manifest.json`:
  `8323d9bcdc8359dd8b03e6e45f6b4db62f79533bb3bcc11789f0b5412ed3a26d`.

## Container resource fixture

The approved adopted `container_resources` context was checked with the owned
validator's short actual-Docker fixture at
`/tmp/assignment-container-resources-live-20260909-fixture2`. It used the same
no-target bootstrap path (`explicit_target_supplied=false`) and pinned
SWE-ReX. The mapped target was container PID `8`, host PID `1314167`, start
ticks `28915091`, namespace `pid:[4026532803]`, and mapping source
`swerex_docker_persistent_bash_nspid`.

The busy action measured 407.838 ms of cgroup `cpu.stat` usage over a 519.609048
ms monotonic wall interval (CPU/wall `0.7848939536`). The sleep action measured
30.902 ms over 540.506926 ms (CPU/wall `0.0571722554`). Both had measured start
and end snapshots, and the busy delta exceeded the sleep delta in this one
short fixture.

Each existing action boundary retained the identity-bound target cgroup
context: `cpu.stat`, `io.stat`, `memory.current`, `memory.max`, `memory.stat`,
`cpu.max`, effective cpuset files, CPU/IO/memory pressure, host load average
and pressure, and the collector's own clock brackets. The context is measured
whole-target-container interval context retained after the event; it is not a
prospective feature input and does not claim per-syscall attribution. The
snapshots use the existing durable boundary and add no extra fsync operation.

The fixture produced 646 native 352-byte records with zero loss fields and raw
stream SHA-256
`193f080242407c510b493a7ce81df869b04d1891cebb90e707d20b9c8a644d3c`.
Its result hash is
`bdb01d98e5cbced5a912f6b95297efc51c1080cf4ec9558c3ce287ad9d299178`.

One preserved retry, `/tmp/assignment-persistent-shell-live-20260909-run6`,
failed before the action sequence during required collector startup with
`BpfProtocolError: 'BpfWorkCollector' object has no attribute
'_container_resources'`. The known cause was the mutable integration checkout:
the call was inserted by one patch and the helper method landed in the next
patch seconds later while the smoke run was already starting. It was not a
missing instance-initialization defect. Run7 passed on the frozen execution
checkout. `bpf_work.py` was not edited in this validation.

## Reproduction

The output directory must not already exist; the script creates it and refuses
to reuse an existing directory.

```bash
PYTHONPATH=/home/riverahernandezjason/agentic-submission-repairs-20260908/src:/home/riverahernandezjason/h100-assignment-work-20260905/repos/SWE-agent \
  /home/riverahernandezjason/h100-assignment-work-20260905/repos/SWE-agent/.venv/bin/python \
  scripts/validation/check_persistent_shell_capture.py \
  --output-dir /tmp/assignment-persistent-shell-live-20260909-new \
  --no-build-image
```

The script can build the same local derived image when `--no-build-image` is
omitted. The required BCC path needs the host's existing privileged BCC setup;
the service launcher uses the existing noninteractive privilege path. The
prior `/tmp/assignment-docker-unpriv-bcc-rerun-ldm7agbb` artifact was left
untouched and is only a nearby one-action reference.

## Root serving glue and limits

The root entrypoint should leave the collector target absent from its config,
let the hook perform this measured bootstrap, and archive both
`persistent_shell_pid_discovery` lifecycle rows and the persisted target
witness. The root join should keep that state query outside command-level BPF
actions while retaining its lifecycle interval and exact command fields. It
should carry `container_resources` from the existing action boundary with its
measured status, target identity, and post-event context markers.
The root interrupt/recovery report should consume the
`bash_interrupt_control` lifecycle row as a control operation and keep its
explicit no-command identity separate from the timed-out command's BPF row.
The runner, proxy, and adapter were not changed for this fix.

This is one bounded live case. It does not establish all acquisition gates,
background descendant behavior, untouched-shell pending reads, all path
classes, all production cases, full interruption/recovery coverage, or a
general guarantee that every interactive program is pager-free. The BPF
collector retains selected raw syscall/process tracepoints and its documented
syscall-facing byte semantics.

## Retained files

- `/tmp/assignment-persistent-shell-live-20260909-run7/result.json` — final
  result, output checks, hook joins, and BPF evidence.
- `/tmp/assignment-persistent-shell-live-20260909-run7/runtime_actions.jsonl` —
  exact commands, outputs, timeout, interrupt, and runtime observations.
- `/tmp/assignment-persistent-shell-live-20260909-run7/telemetry/` — existing
  hook journals and native script-read artifacts.
- `/tmp/assignment-persistent-shell-live-20260909-run7/linux_work/` — BPF
  manifest, action boundaries, aggregates, native binary stream, and service
  lifecycle.
- `/tmp/assignment-persistent-shell-live-20260909-run7/artifact_hashes.json` —
  SHA-256 hashes for retained run files.
- `/tmp/assignment-container-resources-live-20260909-fixture2/` — short busy/sleep
  resource-context evidence and hashes.
- `/tmp/assignment-persistent-shell-live-20260909-run6/fatal_error.txt` —
  preserved collector startup failure.
- `/tmp/assignment-persistent-shell-live-20260909-run5/` — passing no-target
  retry retained for comparison.
- `/tmp/assignment-persistent-shell-live-20260909-run4/` — prior accepted
  14-action/3,275-record evidence, preserved for comparison.
