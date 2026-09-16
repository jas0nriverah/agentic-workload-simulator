# Linux CPU work collector

`agentic_sim.telemetry.linux_work` records host work for an already-running
runtime process.  The runtime supplies an explicit host PID (and, when the
runtime is containerized, the corresponding container PID and PID namespace).
The collector attaches `strace -ff -ttt -T -yy` to that process, keeps the raw
per-PID files, and derives action rows from explicit pre-action and post-action
boundaries.

The collector does not launch a replacement shell or reconstruct work from a
command string.  The hook must call `on_action_started(event_id, command)`
before the real action and `on_action_executed(event_id, status=...)` after it.
The command is retained for identity and audit; syscall counts and paths come
only from the raw trace.  `LinuxWorkHookAdapter` is the small adapter used by
the runtime instrumentation layer.

## Deployment and permissions

Attach is fail-closed.  It requires Linux, an executable `strace`, readable
`/proc` identity and I/O files, and a successful kernel `TracerPid` binding to
the collector's own tracer PID.  A live `strace` process is not treated as an
attach acknowledgement.  On hosts with Yama `ptrace_scope=1`, launch the
collector with the runtime's approved privileged deployment, for example a
root-owned service or a narrowly scoped executable carrying
`CAP_SYS_PTRACE` and the required `/proc` access.  Do not change the host
ptrace policy for a measurement.  The CLI can be invoked through an explicit
supervisor prefix such as `sudo -n` when that is the deployment policy:

```text
sudo -n python -m agentic_sim.telemetry.linux_work attach \
  --pid HOST_BASH_PID --trace-dir /run/agentic-work/case-001 \
  --run-id RUN --attempt-id ATTEMPT --case-id CASE \
  --container-pid CONTAINER_BASH_PID --pid-namespace NS_INODE
```

PID mappings are configuration inputs, not discovered by scanning unrelated
containers.  The attach manifest binds PID, `/proc` start ticks, boot ID,
PID-namespace inode, run, attempt, and case identity.  The target binding is
checked at every action boundary.  The tracer PID has its own start-ticks,
boot-ID, and namespace binding; `stop_session` validates that binding before
signaling it, so a stale manifest cannot signal a reused PID.

## CLI lifecycle

```text
python -m agentic_sim.telemetry.linux_work attach ...
python -m agentic_sim.telemetry.linux_work start-action \
  --session /run/agentic-work/case-001/collector_manifest.json \
  --event-id ACTION --command 'python -m pytest tests/test_example.py'
python -m agentic_sim.telemetry.linux_work end-action \
  --session /run/agentic-work/case-001/collector_manifest.json \
  --event-id ACTION --status success
python -m agentic_sim.telemetry.linux_work stop \
  --session /run/agentic-work/case-001/collector_manifest.json
```

`collector_manifest.json` records the trace command and clock contract.
`action_boundaries.jsonl` is append-only and fsynced at each boundary.
`strace.*` files are retained as the raw evidence; `work_summary.json` is a
deterministic derived view.  `parse --session` replays the retained evidence
without attaching to a process.

## Reported work and limits

The parser handles complete, unfinished/resumed, failed, timeout/detached,
and process-exit rows.  It reports syscall/open/stat/getdents counts, observed
paths, fork/exec/thread counts, and returned read/write bytes split among
path-backed descriptors, pipes, sockets, other descriptors, and unknown
descriptors.  A `-yy` path annotation does not prove inode mode, so
path-backed bytes are not labeled regular-file bytes.  `recvmmsg` and
`sendmmsg` returns are message counts, not byte counts.  `splice` and
`copy_file_range` classify source and destination descriptors separately.

The `/proc` delta retains target and observed-descendant user/system CPU ticks,
waited-child ticks, thread counts, and `/proc/<pid>/io` deltas.  `rchar` and
`wchar` are syscall-facing bytes and include cached files and pipes; separate
`read_bytes` and `write_bytes` are physical-storage counters when the kernel
exposes them.  They are never substituted for one another.  CPU ticks have
kernel tick resolution, and children that exit before the end snapshot can be
missing from the `/proc` delta even though their raw trace remains.

`strace -ttt` uses Unix epoch (`CLOCK_REALTIME`) timestamps.  Boundary records
also retain a monotonic clock for lifecycle diagnostics; the two domains are
never mixed when slicing actions.  Attach can miss syscalls before the kernel
acknowledges the tracer or after a detach, so raw diagnostics and availability
status remain visible.

## Probe overhead

The `overhead` command runs paired control/instrumented direct-exec fixtures.
Each fixture reaches a ready barrier before the instrumented process is
attached and the action start boundary is recorded; only then is the command
released in the same PID.  The output reports total wall time, setup time
(attach plus boundary setup), execution time, CPU rusage, and per-action start
and end boundary costs.  The BCC instrumented total includes the Unix-socket
client round trips for action start/end, perf draining, binary-stream
flush/fsync, map snapshot, and durable boundary writes; one-time BCC
compile/attach and service startup are reported separately.  If attach
permissions or `strace` are unavailable, instrumented rows are `unavailable`
and no zero or synthetic work value is emitted.

```text
sudo -n python -m agentic_sim.telemetry.linux_work overhead \
  --output-dir /run/agentic-work/overhead --repeats 3 -- \
  /usr/bin/python3 -c 'import os; os.stat("/etc/hosts")'
```

The `linux_work` path above remains the diagnostic `strace` backend.  The
privileged BCC candidate is exposed as `agentic_sim.telemetry.bpf_work`; it
attaches to an explicitly mapped persistent PID and receives action boundaries
over the root-owned Unix-socket service when the runtime is unprivileged:

```text
sudo -n python -m agentic_sim.telemetry.bpf_work serve \
  --pid HOST_BASH_PID --socket /run/agentic-work/case-001/bpf.sock \
  --trace-dir /run/agentic-work/case-001/bpf \
  --run-id RUN --attempt-id ATTEMPT --case-id CASE
```

The BCC program uses raw syscall tracepoints plus the process-fork tracepoint.
It emits one compact perf-buffer record for every selected filesystem, metadata
mutation, descriptor, message, mmap/msync, exec, and process/thread event.  Each
record retains the action token, syscall number, PID/TID, parent PID, kernel
start/end timestamps, return value/status, descriptor, and observed path
status/bytes.  Rename records retain separate source and destination path
fields; unknown or truncated paths remain explicit.  Kernel maps derive counts
and syscall latency sums, while the append-only raw action row stores the
individual records for replay.  Perf loss, pending-map loss, lineage-map loss,
callback errors, or a required-versus-decoded count mismatch marks that action
`unavailable`; no synthetic work value is emitted.

The BCC clock is `CLOCK_MONOTONIC` from `bpf_ktime_get_ns`.  Caller action
boundaries retain their own wall and monotonic anchors plus a RAW-before,
MONOTONIC, RAW-after bracket and its uncertainty.  The paired samples support
bounded placement of kernel timestamps while native clocks remain separate and
are never directly subtracted.  Returned read/write values are syscall-facing bytes;
path-backed descriptors are reported without claiming regular-file or physical
disk traffic.  When an action closes while a persistent shell or descendant is
still active, the raw row records the in-flight count and mapped processes
removed at cleanup.  Already-mapped descendants retain the original action
token and continue into the durable stream until quiescence or collector stop.
At collector stop, pending calls retain their kernel start and censor boundary
without an invented duration; a finalization row reports them separately from
transport loss.  Post-boundary work is never reassigned to a later action.  The
BCC candidate therefore requires privileged BPF deployment
and a measured overhead check with full perf-record capture enabled.  The
`strace` backend remains available for diagnostics and parser replay, but its
measured overhead must not be treated as the production BCC overhead.
