# Shared case queue

## Main acceptance update, 02:50 UTC

The four reproduced correctness defects and the final malformed-terminal-field
defect are closed. Supplied halt fields require strict booleans; non-null
integrity metadata requires an object and a string status when supplied.
The common result preparation path applies before acceptance, replay, recovery
and audit. Negatives retain zero accepted cases and a persistent dispatch halt.
Final focused verification: **25 tests and 29 subtests passed in 9.47 seconds**.
Astra reviewed the final hunk and accepted it without repeating the suite.
Reviewed queue SHA-256:
`68f4d9521fae85473dd0c0de9221dc7db91fe08443ad4a9908a3f0a6a2f9fedc`.
Test SHA-256:
`a2a1cd6769f1f4e61cec94a7f22d75e26ab428e10bd62de49d5fba0398e46ee9`.

This accepts the component fixes; actual effective worker bindings, measured
storage budgeting, runtime/source freeze and live gates remain prerequisites
for dispatch. No queue has launched inference or a production case.

Status at repair handoff, 2026-09-09: **awaiting main's production acceptance**.
The adopted capture, replay, evidence-audit, and child-launch defects are fixed
in the queue, with bounded regression tests and real CPU child processes.
A measured storage policy now gates each production claim. Main owns final
review, signing, runtime manifests, capacity policy, deployment, and launch.
No GPU, production queue, tunnel, job, or existing case runner was changed or
launched by this work. Main has updated the component runner hash and owns
the final matrix/confirmation/runner verification and freeze.

This is the preparation boundary for a durable 22-worker queue. It does not
start inference, inspect endpoints, create VPN or relay connections, submit
jobs, or edit the existing case runner. The production supervisor requires
both `--execute` and `--acknowledge-paid-gpu-work`. Tests exercise that CLI
only with temporary CPU fixture executables that make no model requests.

The implementation is [scripts/assignment/shared_case_queue.py](../scripts/assignment/shared_case_queue.py).
Each queue directory contains `queue.sqlite3` and an `artifacts/` tree. SQLite
uses WAL journaling, foreign keys, a 30 second busy timeout, and
`synchronous=FULL`. A claim is one `BEGIN IMMEDIATE` transaction that selects
the smallest pending ordinal, creates an attempt row, writes the exact staged
case bytes and sidecars, and records the lease. Partial unique indexes enforce
one active case, worker, and endpoint at a time.

This revision requires `assignment.shared-case-queue.v2` / SQLite user version
2. Old v1 databases are refused, without an automatic migration: their missing
launch intent cannot prove that a child never started. Preserve old databases
and artifacts for main's reconciliation; creating another queue is not a way
to release an old endpoint. Attempt file locks live outside the evidence tree
and serialize completion/recovery writes across processes.

The ready dispatch set is exactly:

```text
worker-00 worker-01 worker-02 worker-03 worker-04 worker-05 worker-06
worker-07 worker-08 worker-09 worker-10 worker-12 worker-13 worker-14
worker-15 worker-16 worker-17 worker-18 worker-19 worker-20 worker-21 worker-22
```

`worker-11` is rejected even when a caller supplies a short numeric spelling.
Production initialization also rejects a custom worker set. A prepared,
registered worker may claim before every peer is registered; a partial pool at
first dispatch is recorded once as the `worker_pool_partial` advisory in
`status()["advisories"]` and the event log. This disclosure is not a halt.

## Admission gate

Production initialization requires the immutable plan and records a
root-controlled fingerprint gate. The authoritative allocation-scoped
discovery artifact is:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/worker-fingerprint-allocation-v4/fingerprints.json
sha256 2c9c45d9ab5f15b5e7a9eda19a4a782934a43018fcd2677a98d01fe0908aa34a
```

Its 24 allocation records include 22 serving processes, all with discovered
`--max-model-len` 32768. The earlier `worker-fingerprint-discovery-v1` node
probe had invalid GPU binding and is superseded for allocation/GPU evidence.
Both artifacts are discovery-only; neither certifies effective rollout
configuration, observer readiness, or permission to lease a case. The queue's
discovery parser checks endpoint count and context, not the complete
allocation-to-PID-to-GPU relationship.

Importing it leaves `dispatch_halted=true` with `fingerprint_gate_status` set
to `discovery_only`. Health responses cannot clear that state.

The queue currently expects the following separate effective-artifact schema.
This is an adapter contract for main to review against the later root rollout,
not a claim that such an authoritative rollout artifact already exists:

```json
{
  "schema_version": "assignment.worker-fingerprint-effective.v1",
  "server_max_model_len": 65536,
  "workers": [
    {
      "worker_id": "worker-00",
      "endpoint_id": "reviewed-endpoint-00",
      "server_max_model_len": 65536,
      "effective_config_fingerprint": "<64 lowercase hex characters>"
    }
  ],
  "observer_rollout": {
    "leases_cases": false,
    "active_trajectory_allowed": false
  }
}
```

The `workers` array must contain all 22 exact worker IDs, unique endpoint IDs,
and a nonzero 64-character configuration fingerprint for every row. The queue
rehashes the artifact before every claim and checks its endpoint IDs against
registered workers. Bind it only after root review:

```bash
QUEUE=/reviewed/external/queue
EFFECTIVE=/reviewed/external/effective-worker-fingerprints.json

"$REPO/.venv/bin/python" "$REPO/scripts/assignment/shared_case_queue.py" \
  bind-effective-fingerprints --queue-dir "$QUEUE" \
  --effective-fingerprints "$EFFECTIVE" \
  --effective-fingerprints-sha256 "$EFFECTIVE_SHA256" \
  --review-note "root-controlled 65536 rollout and observer binding reviewed"
```

The observer is kept in an `observers` table and has a database check that
`can_lease_cases` is zero. An observer row is never eligible for `claim` and a
worker registration cannot reuse its endpoint identity. The effective artifact
must repeat the non-leasing assertion even when no observer process has yet
been registered.

Binding effective fingerprints does **not** clear a dispatch halt. After
reviewing both effective configuration and storage, main must explicitly use
`clear-halt --queue-dir "$QUEUE" --review-note "..."`. That operation rechecks
both gates, so a configuration update cannot erase a capture or storage halt.

## Measured storage reserve

No scheduler free-space threshold was found in the existing matrix/runner.
The queue uses a separate immutable, SHA-bound policy instead of modifying
worker runtimes or adding an archiver. Production claims require it; missing
measurements never receive a default allowance. Main's latest capacity report
is **local 141 GB free and remote 224 GiB free user quota**. The earlier 3.9 PB
figure was filesystem-wide capacity, not the user's allowance. Neither
reported amount becomes an arbitrary reserve default. The current adapter uses `statvfs` and
rejects unknown or enforced user quotas; main needs quota-aware integration
before admitting a filesystem whose user allowance is not represented by
that probe.

Policy contract (placeholders below must become reviewed integers and hashes):

```json
{
  "schema_version": "assignment.queue-storage-policy.v1",
  "case_count": 1088,
  "worker_count": 22,
  "pilot_evidence": {"path": "/reviewed/pilot-storage.json", "sha256": "<sha256>"},
  "filesystems": [
    {
      "path": "/reviewed/local-volume",
      "roles": ["queue", "artifacts", "container_storage"],
      "pilot_case_peak_bytes": "<measured positive integer>",
      "final_total_estimate_bytes": "<reviewed positive integer>",
      "safety_reserve_bytes": "<reviewed positive integer>",
      "quota_scope": "no_user_quota"
    }
  ]
}
```

Use `case_count=96` for the confirmation queue and main's reviewed pilot
estimates/evidence before that run. After measuring that panel, bind a new 1088 policy to the separate
production queue. The 96 policy also requires evidence; the queue does not
invent a pilot bootstrap allowance. The pilot file is hash-verified, while
the estimate derivation and the no-user-quota assertion are main's review
responsibility. Include retained Docker writable layers/containers: stopping
owned containers does not reclaim their storage. Consolidate roles sharing a
filesystem into one budget; each role must occur once. The queue checks that
the queue/artifact role paths cover the actual output filesystems. Main must
verify the declared container-storage root against the actual Docker daemon.

Before **every** claim, under the same SQLite ownership transaction:

```text
required_free_bytes = safety_reserve_bytes
                    + 22 * pilot_case_peak_bytes
                    + ceil(final_total_estimate_bytes * unfinished_cases / case_count)
available_bytes = statvfs(path).f_bavail * statvfs(path).f_frsize
```

The additional peak reserve remains for all 22 slots even near the end of the
run. Already accepted output occupies disk; the final estimate term covers
unfinished cases. This is deliberately conservative admission, not a kernel
reservation against unrelated writers or an enforcement mechanism for a case
that exceeds its measured bound. Main must review estimates and margins after
the 96 measurements. No deletion, archiving, container cleanup, or quota probe
is added.

```bash
"$PYTHON" "$REPO/scripts/assignment/shared_case_queue.py" \
  bind-storage-policy --queue-dir "$QUEUE" \
  --storage-policy "$STORAGE_POLICY" \
  --storage-policy-sha256 "$STORAGE_POLICY_SHA256" \
  --review-note "measured pilot, final raw/overlay estimate, quota and reserve reviewed"
```

Binding preserves existing halts and refuses a policy change while any active
or orphaned case is held. Low capacity, policy/pilot hash drift, mount identity
change, and probe errors commit a **global halt before creating a lease**.
Restarting Codex or a supervisor does not remove that halt. `clear-halt`
requires both restored capacity and explicit review. A queue with **no bound
storage policy** is not halted: the planning formula is advisory, and its
absence is disclosed once as the `storage_policy_missing` advisory. Only a
bound policy's demonstrated shortfall, hash drift, identity change, or probe
error halts dispatch. `--allow-partial-workers` is never a production
invocation.

## Initialize and bind workers

`--allow-partial-workers` is intended for unit and local recovery tests. It
does not make a production queue claimable without the effective fingerprint
artifact when a discovery artifact is bound.

The worker manifest must provide a regular file or directory and its declared
SHA-256 for each `inventory`, `runtime`, and `source` binding. The endpoint
contains an explicit `endpoint_id`, HTTP(S) `api_base`, and stable
`server_identity`:

```json
{
  "schema_version": "assignment.worker-pool-manifest.v1",
  "workers": [
    {
      "worker_id": "worker-00",
      "role": "worker",
      "can_lease_cases": true,
      "endpoint": {
        "endpoint_id": "reviewed-endpoint-00",
        "api_base": "https://reviewed-relay.invalid/worker-00/v1",
        "server_identity": "reviewed-server-00"
      },
      "inventory": {"path": "/reviewed/inventory-00.json", "sha256": "<sha256>"},
      "runtime": {"path": "/reviewed/runtime-00.json", "sha256": "<sha256>"},
      "source": {"path": "/reviewed/source-00", "sha256": "<sha256>"}
    },
    {
      "observer_id": "observer-rollout",
      "role": "observer",
      "can_lease_cases": false,
      "endpoint": {
        "endpoint_id": "reviewed-observer",
        "api_base": "https://reviewed-relay.invalid/observer/v1",
        "server_identity": "reviewed-observer-server"
      }
    }
  ]
}
```

The observer example is metadata only; it is not a worker slot. Register the
manifest after initialization. The command records the manifest hash and
registers observer rows separately:

```bash
"$REPO/.venv/bin/python" "$REPO/scripts/assignment/shared_case_queue.py" \
  register-workers --queue-dir "$QUEUE" --worker-manifest "$WORKER_MANIFEST"
```

For the current discovery gate, initialization is expected to report a halt:

```bash
"$REPO/.venv/bin/python" "$REPO/scripts/assignment/shared_case_queue.py" \
  init --queue-dir "$QUEUE" --plan "$PLAN" \
  --plan-sha256 "$PLAN_SHA256" \
  --fingerprints \
  /home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/worker-fingerprint-allocation-v4/fingerprints.json \
  --fingerprints-sha256 2c9c45d9ab5f15b5e7a9eda19a4a782934a43018fcd2677a98d01fe0908aa34a \
  --worker-pool "$WORKER_MANIFEST"
```

No claim should follow that output until main closes the review blockers,
the effective and measured-storage bindings have passed, and
the queue reports `fingerprint_gate_status=effective_ready` and
`dispatch_halted=false`. Workers that are not yet registered do not block the
prepared ones; the `advisories` field records any partial pool or missing
storage policy so the run's contention/placement disclosure stays complete.

## Confirmation inventory adapter

The separate `import-confirmation` command is the adapter boundary for the
reviewed configuration confirmation plan. It requires:

- the original staged case-spec files referenced by the plan;
- the exact plan SHA-256 and its `.sha256` sidecar;
- a fresh runtime manifest with its `.sha256` sidecar; and
- the current existing runner's `load_case` function.

It requires `assignment.configuration-confirmation-execution-plan.v2` and
exactly 96 entries. It calls `load_case` once for every original spec with
`--confirmation-plan`, the supplied `--confirmation-plan-sha256`, and the
runtime manifest mapping. It then stores each spec's original bytes and hash in
SQLite. It does not canonicalize, rewrite, relabel, or add a case. A changed
plan, runtime manifest, case spec, or runner source aborts the import.

The reviewed plan and hash supplied for this panel are:

```text
PLAN=/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/live-plan/configuration_confirmation_execution_plan.json
PLAN_SHA256=d282607b28ffff76a17c96521adf923acf82fc3b0be3b7bed929f5ae330ba28e
```

The runtime manifest path is deliberately a main-owned input. Do not reuse
the historical `pin_evidence/runtime_manifest_worker_00.json` as a fresh v2
runtime binding. The importer verifies its sidecar and supplies its JSON to
`load_case`; it does not call the runner's full `load_manifest` or execution
integrity validation. The invocation contract has this form:

```bash
"$REPO/.venv/bin/python" "$REPO/scripts/assignment/shared_case_queue.py" \
  import-confirmation --queue-dir "$QUEUE" \
  --confirmation-plan "$PLAN" \
  --confirmation-plan-sha256 d282607b28ffff76a17c96521adf923acf82fc3b0be3b7bed929f5ae330ba28e \
  --runtime-manifest "$RUNTIME_MANIFEST" \
  --fingerprints "$DISCOVERY_FINGERPRINTS" \
  --fingerprints-sha256 "$DISCOVERY_SHA256" \
  --adapter-manifest "$ADAPTER_MANIFEST" \
  --adapter-manifest-sha256 "$ADAPTER_SHA256"
```

The resulting queue is still halted when the discovery artifact is the 32768
artifact. The importer is a validation and durable-staging operation; it is
not a confirmation of runtime readiness.

## Runner adapter and extra argv

For a compatible case, `supervise` invokes the existing runner with the
attempt-local exact case, the worker's hash-bound runtime manifest, and
`--execute`. Confirmation cases additionally require the explicit
confirmation plan and exact plan hash. The queue never synthesizes a
confirmation or production schema and does not alter case settings.

Reviewed extra runner arguments may be supplied by an exact-hash manifest:

```json
{
  "schema_version": "assignment.queue-adapter-manifest.v1",
  "name": "reviewed-runner-extension",
  "argv": ["--cpu-docker"]
}
```

The queue appends `argv` as an argument vector, never through a shell, records
the manifest path and SHA in the queue and attempt command artifact, and
rehashes it immediately before launch. The production initialization path
requires the manifest SHA or sidecar. `--runner-arg` remains a compatibility
escape hatch for local adapter testing and is not a source binding.

The launch-shaped command is intentionally shown for review only:

```bash
"$REPO/.venv/bin/python" "$REPO/scripts/assignment/shared_case_queue.py" \
  supervise --queue-dir "$QUEUE" --worker-id worker-00 \
  --runner "$REPO/scripts/assignment/sweagent_case_runner.py" --cwd "$REPO" \
  --confirmation-plan "$PLAN" \
  --confirmation-plan-sha256 d282607b28ffff76a17c96521adf923acf82fc3b0be3b7bed929f5ae330ba28e \
  --extra-argv-manifest "$ADAPTER_MANIFEST" \
  --extra-argv-manifest-sha256 "$ADAPTER_SHA256" \
  --execute --acknowledge-paid-gpu-work
```

The current task did not run this command. Main must review and resign the
runner and the full runtime/source manifest before any live use. The importer
records the runner SHA used for its 96 validation so a later source change is
visible during that review. `REPO` here must be main's reviewed clean source
tree. The importer defaults to the case runner beside the queue module; its
CLI has no `--case-runner-path` switch. Run the queue module from that reviewed
tree. The guardian executes `--runner` directly, so the runner's executable
mode, shebang interpreter/PATH, imports, and dependencies must also be bound.

`register-workers` supplies each slot's exact runtime path/hash. The
`supervise` CLI has no `--runtime-manifest`; it forwards the registered
worker's `runtime.path`. The import-time runtime validates the 96 case
coordinates but does not register workers or validate all 22 runtime
manifests. Main must establish those per-worker bindings separately.

## Invocation contract for 96 and 1088 cases

These commands describe the interfaces for main's integration review; they
are not a launch recipe approved by the passing unit suite. Use different
queue directories for the confirmation panel and the subsequently frozen
production inventory.

| Scope | Queue input | Existing runner receives |
| --- | --- | --- |
| 96 confirmation cases | `import-confirmation`, original object plan and exact `d282607b28ffff76a17c96521adf923acf82fc3b0be3b7bed929f5ae330ba28e` hash, original referenced case bytes, reviewed import-time runtime | Attempt-local original case, worker runtime, explicit confirmation plan and hash, `--execute`, reviewed extra argv |
| 1088 production cases | `init`, main's separately finalized `frozen_for_execution` JSONL plan and exact hash/sidecar | Attempt-local production case, worker runtime, `--execute`, reviewed extra argv; no confirmation-plan arguments |

The generic queue `init` does **not** enforce the production finalizer's
1088-case contract, frozen status, or execution binding. It can parse candidate
inventories too. Main must not pass `candidate_pending_selection` inventory as
production. Before importing the separately finalized plan, the existing
read-only validator is `scripts.assignment.run_matrix.load_plan(Path(...))`;
main must additionally require `header['status'] == 'frozen_for_execution'`,
1088 cases, and the signed finalizer/config/runtime/source bindings. No frozen
production plan path or hash is invented here.

For that reviewed input, the production queue interface is:

```bash
"$PYTHON" "$REPO/scripts/assignment/shared_case_queue.py" \
  init --queue-dir "$PRODUCTION_QUEUE" --plan "$FROZEN_PLAN" \
  --plan-sha256 "$FROZEN_PLAN_SHA256" \
  --fingerprints "$DISCOVERY_FINGERPRINTS" \
  --fingerprints-sha256 "$DISCOVERY_SHA256" \
  --worker-pool "$PRODUCTION_WORKER_MANIFEST" \
  --adapter-manifest "$ADAPTER_MANIFEST" \
  --adapter-manifest-sha256 "$ADAPTER_SHA256"

# Bind the separately reviewed storage policy for this 1088-case queue:
"$PYTHON" "$REPO/scripts/assignment/shared_case_queue.py" \
  bind-storage-policy --queue-dir "$PRODUCTION_QUEUE" \
  --storage-policy "$PRODUCTION_STORAGE_POLICY" \
  --storage-policy-sha256 "$PRODUCTION_STORAGE_POLICY_SHA256" \
  --review-note "post-96 measured capacity and retained overlays reviewed"

# Main-owned launch interface, after configuration/storage review and clear-halt:
"$PYTHON" "$REPO/scripts/assignment/shared_case_queue.py" \
  supervise --queue-dir "$PRODUCTION_QUEUE" --worker-id "$WORKER_ID" \
  --runner "$REPO/scripts/assignment/sweagent_case_runner.py" --cwd "$REPO" \
  --extra-argv-manifest "$ADAPTER_MANIFEST" \
  --extra-argv-manifest-sha256 "$ADAPTER_SHA256" \
  --execute --acknowledge-paid-gpu-work
```

`WORKER_ID` is one of the exact 22 slots. Each supervisor serially consumes the
shared queue and immediately requests its next case after completion. Main
owns deployment of those processes outside Codex; this preparation installed
no service. Use a single local Linux host/PID namespace and one queue database
for the fleet. WAL on a network filesystem, independent queue databases, or
supervisors on other hosts do not provide shared endpoint ownership.

## Ownership, retries, and artifacts

There is no wall-clock lease expiry. Heartbeats are observational. Before
`Popen`, the supervisor commits a launch intent and exact argv hash. Its Linux
guardian registers its own PID/start/boot identity exactly once before
starting the runner. It acts as a child subreaper, waits for the runner and
adopted descendants (including detached sessions), and commits an exit receipt
only after reaping them and checking the local session. The supervisor must
observe the guardian's exit before finishing or retrying the case.

An owner crash before launch intent can become an orphan once the owner is
proved dead. An owner crash after launch can recover a durable completed
result when the guardian independently finishes and leaves valid exit proof.
A missing PID, missing exit receipt, inaccessible `/proc` identity, or killed
guardian with potentially surviving children remains **identity_unknown**.
Intent alone is not proof that `Popen` failed. Such attempts stay active and
hold the case, worker, and endpoint; neither a review-note string nor a
timeout permits retry. The CLI deliberately provides no force-expiry override.

`reconcile` marks an orphan only after proving owner and guarded execution
dead, rechecking within the ownership transaction. `reconcile --attempt-id ...
--action inspect|accept|retry` then explicitly resolves that orphan. The lease
sidecar supplies the original capability for tokenless recovery; missing or
mismatched ownership proof fails closed. Finish/fail/retry also check child
proof themselves. The old external `bind-pid` workflow is no longer a launch
protocol: only the session-leading guardian may consume a persisted intent.

Attempt directories retain the artifacts produced before termination: lease,
exact case bytes, supervisor command/exit records, logs, runner output, and
result or queue failure. Normal finalization produces an artifact manifest;
early crashes can leave it absent. Retry provenance is stored in SQLite and
events. The next attempt's `retry_of_attempt_id` points to the previous attempt.
Sealing fsyncs inventoried files and directories; fsync/walk errors halt
assignment. Identical completion and failure replays preserve timestamps,
bytes, and their one SQLite outcome. Conflicting replay halts dispatch while
preserving an already committed completion and its evidence. A crash between
manifest sealing and SQLite commit is recoverable without rewriting that seal.

An intact `status=completed` result is an accepted queue completion when its
evaluator says `official_resolved=false`; that outcome is recorded in the
acceptance event and cannot be sent through a quality retry. Capture or
evidence failure signals on **all** terminal results, including `infrastructure_evidence` and
model transport/server infrastructure classifications, block the attempt and
set the global dispatch halt. A completed result plus nonzero process exit
also halts; it cannot be requeued as an infrastructure/quality retry. A human
review note and passing configuration/storage gates are required to clear a
halt. Capture semantics beyond the supplied runner's terminal flags remain
the reviewed runner adapter's responsibility.

`audit` takes a consistent ownership snapshot and verifies that every imported
case is accepted exactly once. It checks case/attempt/result pointers,
original case bytes, lease/token identity, worker/endpoint/binding identity,
SQLite outcome and manifest bindings, launch receipt, and **every** sealed
file hash/size and symlink target, including prior failed/requeued attempts.
Added, removed, or changed evidence fails the audit and commits a global halt.
Symlink targets are inventoried as links, not followed as external evidence.
A non-pass audit or any pending, orphaned,
retry-waiting, or blocked case is an explicit review state; the supervisor
does not silently skip it.

The focused queue tests use real forked processes and cover concurrent atomic
ownership, unique accepted coverage, official-unresolved acceptance, crashed
owner reconciliation and resume, endpoint/observer exclusion, discovery
versus effective fingerprint admission, capture halting, and 96-case byte
preservation. They do not contact an endpoint or launch inference.

## Focused verification and main review

Final focused command, after the adopted fixes and storage gate:

```bash
cd /home/riverahernandezjason/agentic-submission-repairs-20260908
.venv/bin/python -m pytest -q tests/assignment/test_shared_case_queue.py
```

Result: **25 passed, 23 subtests passed in 8.12s**. Before the fixes, the six
`-k regression` methods exposed completed capture acceptance, concurrent replay
failure, failure-receipt replay failure, changed evidence auditing as pass,
permission-denied `/proc` being called dead, and a real live child incorrectly
becoming an orphan after its owner's spawn/bind-gap crash. The corrected
spawn-gap reproducer failed on `state == orphaned` before the implementation
changed. These regressions now pass.

Coverage includes four real workers sharing 32 cases, three real processes
replaying one completion, completed and failed receipts sealed before a forced
owner crash, independent guardian completion after owner death, a detached
descendant surviving guardian termination while its case remains held, and a
separate process persisting a low-storage global halt with zero leases. Two
temporary CPU runner cases also exercise the supervisor's immediate next-case
loop. The guardian, intent, and evidence tests never start the real case runner.

Before main took ownership of the broader component rerun, this command also
passed on the same final queue source:

```bash
.venv/bin/python -m pytest -q tests/assignment/test_shared_case_queue.py tests/assignment/test_confirmation_case_runner.py
```

Result: **37 passed, 135 subtests passed in 10.02s**. An earlier three-file run
had **8 matrix failures, 45 passed, 131 subtests passed** because the runner's
approved hash was stale. Main subsequently reviewed the component changes and
updated that pin to `ce0371c19e46a9cfbb242a4c59ae360696bbc4be025f37ff7f97f8475128f5e6`;
main is running matrix/confirmation/runner tests. That rerun is intentionally
not duplicated here, and the pin update is not a final production freeze.

The queue's 96-case byte-preservation test mocks `load_case`; companion runner
tests use portable confirmation fixtures. No authoritative 96 import with
fresh production runtimes, 22-supervisor deployment, or 1088 execution was
performed.

Additional offline checks imported the actual sibling runner's `load_case`,
validated allocation-v4 (22 endpoints; contexts `[32768]`), and created a
temporary queue with 22 synthetic worker bindings. Both `claim` and
`clear-halt` rejected discovery-only state, leaving zero active attempts.
An explicit adapter SHA preserved `['--cpu-docker']`; changing the manifest
then raised `ArtifactIntegrityError`. No endpoint was contacted.
Those discovery/adapter checks were read-only preparation checks. Both
discovery artifacts remain insufficient to open the effective gate.

Source hashes at this verification point (inspection hashes, not new signing):

| File | SHA-256 |
| --- | --- |
| `scripts/assignment/shared_case_queue.py` | `2a271daff52ebdd36d0294576ce534e4d133fc07fbf666dc6c6a231518a5f455` |
| `tests/assignment/test_shared_case_queue.py` | `19b991af24f7c54f6e3b388b853122dc0d449da30cf6801b9794b11022f31c40` |
| `scripts/assignment/sweagent_case_runner.py` (main-owned) | `ce0371c19e46a9cfbb242a4c59ae360696bbc4be025f37ff7f97f8475128f5e6` |
| `scripts/assignment/run_matrix.py` (main-owned) | `1232c9287e04f319088316fe49fa6f13176bf463559495bfae9c8e78db064b25` |

The adopted defects are implemented, with these reviewable outcomes:

| Finding | Evidence and implication |
| --- | --- |
| Completed-result integrity | Capture/halt signals take precedence over completed status; malformed terminal metadata halts. Completed plus nonzero exit is held for review and cannot be retried. |
| Completion replay | Per-attempt process locks, stable original timestamps/bytes, idempotent completed/failed outcomes, conflict halting without rewriting terminal evidence, and sealed-before-commit crash recovery. |
| Archived evidence audit | Complete sealed-tree rehash, pointer/identity/SQLite binding checks, and global halt on changed, added, missing, or rebound evidence. Directory and file fsync errors propagate. |
| Child launch/recovery | Durable intent before spawn, one guardian registration before runner launch, Linux descendant reaping and durable exit proof; uncertain child execution remains held and cannot finish/fail/retry. |
| Storage admission | Hash-bound pilot/reserve policy, 22-slot peak reserve plus unfinished-run estimate, pre-claim free-space check, durable global halt, and no automatic clearing on configuration rebinding. |

Remaining integration/acceptance boundaries for main:

- Bind the effective 65536 rollout and actual non-leasing observer evidence;
  health and allocation-v4 discovery alone never suffice. Root-only operation
  and access control are deployment requirements, not CLI OS-role enforcement.
- Review 96/1088 estimates, Docker storage roots, and reserves using the latest
  **141 GB local / 224 GiB remote quota** capacities. Enforced remote quota needs
  a current quota-aware capacity adapter; the bounded core currently refuses
  it. No signed capacity policy or numerical production threshold is invented.
- The guardian proves local descendant exit, not the state of daemon-managed
  containers or remote model requests. Main must review the runner's cleanup
  and endpoint-inflight contract. Missing guardian proof stays held regardless.
- Inventory/runtime/source hashes bind bytes but do not prove that runtime
  `model.api_base` matches the endpoint or that a source-manifest file's members
  are recursively verified. Main must bind the exact tree, interpreter, imports,
  current runner, and runtime. Import-time runner SHA is recorded, not enforced
  as the supervisor's source pin.
- Generic extra argv can override queue-owned flags; review the exact argv
  and always supply the explicit bound adapter SHA shown above. An explicit
  adapter path without a SHA is not a safe production invocation because that
  compatibility path does not enforce the stored SHA. The bounded queue fixes
  do not substitute for main's command/source adapter review.
- Generic `init` still needs the frozen-1088 validator boundary described
  above. Exact case semantics and successful capture completeness remain the
  existing runner's responsibility. SQLite and evidence use local durability
  barriers, but not every power-loss/initialization boundary is fault-injected.
  Main must accept the local filesystem and single-database deployment.

Main retains final production acceptance and launch. Neither discovery,
component signing, passing CPU tests, nor a capacity report opens that gate.
