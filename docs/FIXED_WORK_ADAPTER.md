# Fixed work adapter

`scripts/validation/fixed_work_adapter.py` executes one condition of the
four fixture v2 replay. The replay runner restores the hash bound snapshot and
owns the three paired repetitions. The adapter rechecks the immutable action
and request JSONL, runs the selected condition, and writes one
`assignment.instrumentation-replay-result.v2` result.

The v2 gate is evaluated over the four fixture level paired medians. It uses
median relative overhead at most 5% and nearest rank p95 at most 10%. A tiny
diagnostic command that is slow at a filesystem sync boundary does not itself
fail this gate; it is evidence about that fixture's measured fixed work only.

The manifest entry must bind the absolute action fixture, request fixture and
pretrajectory snapshot paths, their SHA-256 hashes, the workload hash, and a
non empty `serving_and_cache_policy`. A v2 case can add the following sidecar
at `<fixture_dir>/fixed_work_fixture.json` (the root v2 manifest remains
unchanged):

```json
{
  "fixture_id": "cpu-file-traversal-v1",
  "fixture_kind": "cpu_filesystem_traversal",
  "serving_and_cache_policy": {
    "endpoint": "fixture://fixed-work",
    "cache_policy": {"mode": "disabled", "key": "fixture-body"},
    "payload_identity": "immutable-jsonl"
  },
  "cpu_collector": {
    "backend": "bcc",
    "attach_existing_process": true,
    "require_persistent_runtime_pid": true,
    "trace_format": "bcc raw individual syscall and process events plus action aggregates v2"
  },
  "swe_runtime": {
    "enabled": true,
    "swe_agent_root": "/home/riverahernandezjason/h100-assignment-work-20260905/repos/SWE-agent",
    "base_image": "swebench/sweb.eval.x86_64.psf_1776_requests-1724@sha256:e369005d38858ea90f843d853cb8427a2681b7513b846d12a34cb8d52c00763e",
    "image": "assignment-persistent-shell-swe-rex-1-4-0:20260909",
    "no_build_image": true,
    "action_timeout_seconds": 25
  }
}
```

The `swe_runtime` descriptor is the explicit opt in for the production CPU
adapter. It loads SWE-agent commit
`0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`, SWE-agent `1.1.0`, and SWE-ReX
`1.4.0`, verifies the pinned Docker image, creates `SWEEnv` with
`DockerDeployment`, uploads the orchestrator supplied scratch snapshot, and
changes the persistent shell to that uploaded directory. Every fixture action
then goes through `check_persistent_shell_capture.run_action`, including the
real SWE-agent action and script state hooks.

The off and on CPU conditions use the same pinned image, uploaded snapshot,
container working directory, and immutable action strings. Off uses the same
SWE-ReX action driver with telemetry callbacks disabled and must leave no
`telemetry` or `linux_work` directory. On starts the existing BCC service only
after the hook resolves the Docker persistent shell through
`swerex_docker_persistent_bash_nspid`.

`full_production_capture_enabled` becomes true only after all of these
observations validate:

* measured terminal lifecycle and tool hook journals, with exact intent,
  actual action, runtime command, action hash, and collector event joins;
* complete lossless BPF binary ranges and action finalizations, with zero
  event, pending, path, lineage map, perf, and callback loss;
* paired BPF boundary snapshots containing measured container cgroup
  `cpu.stat`, `cpu.max`, `io.stat`, `cpu.pressure`, `io.pressure`, and
  `memory.pressure`, with stable PID, boot, cgroup path, device, and inode
  identities at both boundaries; and
* every observed script state read retaining its measured cwd, state
  generation, path hash, size, and complete content artifact.

The BPF cgroup samples are existing durable start/end snapshots. The adapter
does not add an fsync or turn those whole container interval values into
per syscall features. A raw host Bash condition has no container identity, so
it remains diagnostic and cannot set the full capture flag. If the Docker
runtime, hook journal, BPF join, or cgroup context is unavailable, the
condition is blocked with `adapter_error.json` and no value is imputed.

`work_wall_ms` starts after Docker upload, hook setup, and collector setup. It
ends only after all fixture actions, collector deferred finalization and
service stop, Docker environment close, and the outer telemetry durable close
complete. `startup_wall_ms` reports setup separately. Action output and hook
errors remain in the runtime action journal and retained output artifacts.

Model proxy and native serving capture remain pending until an explicit
serving endpoint, stable cache policy, complete serving metrics configuration,
and independent access witness are supplied. The adapter tests launch no
inference or GPU job.

The 2026-09-09 bounded fixture recovery found only declarations in the
submission's `live-plan/overhead_replay_plan.v2.json`: all twelve pairs are
`pending_live_fixture_capture`, with null workload/snapshot hashes and
timings. No immutable action/request JSONL or pretrajectory snapshot archive
was found in that submission. All four gate baseline sample counts are zero;
their durations and action/request counts remain unknown. Historical CPU
diagnostic timings have no immutable binding to these four fixtures.

Preparation is retained under the submission's
`verification/fixed-work-gate-preparation-20260909T015245Z/`. It contains the
original fixture declarations, two CPU runtime-only sidecars, the original
24-condition order and pass identities, source/artifact hashes, and a
`validate_when_bound.sh` command. The wrapper performs validation only and
exits 2 until the actual manifest and SHA-256 sidecar exist. No replacement
workload or empty snapshot was created. The CPU sidecars still require the
original payloads, snapshots, and explicit common policy. Runtime CPU/SMT
placement belongs to its separate owner; model execution and deferred server
witness integration remain pending main review.

Main subsequently adopted materializing those four workload classes. The new
builder and executable recipe are documented in [FIXED_WORK_FIXTURES.md](FIXED_WORK_FIXTURES.md).
The active input bundle is the submission's
`verification/fixed-work-fixtures-20260909T022827Z/`; it contains actual
JSONLs, reset snapshots, verified pinned tokenizer bytes, and 24-condition
commands. The earlier preparation directory remains an accurate record of
the missing-payload state before that adoption. CPU sidecars now require
the existing runtime-policy binding; the adapter verifies actual Docker and
host-process CPU placement before actions and supports deep durable output
paths through a short temporary BPF socket path.

Its sibling `fixed-work-fixtures-20260909T022827Z-cpu-subset/` retains twelve
actual CPU conditions and six valid pairs. Full hook capture and CPU placement
passed with zero loss; paired median overhead was 64.17% for file/traversal and
68.50% for test/script/subprocess. This misses the overhead targets and is not
a completed four-fixture gate. No model condition or cache reset was executed.
