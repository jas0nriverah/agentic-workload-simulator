# Retry reliability investigation — 2026-09-07

This investigation uses an isolated checkout at
`/home/riverahernandezjason/agentic-reliability-20260907`, based on
`a653ec205ef8111f74527f4dbbea88671555c29c`. No investigation command modified
the original checkout, assignment data, monitor, live matrix, VPN, tunnels,
inference endpoints, or pre-existing containers. Existing uncommitted changes
in the original checkout were left in place and were not adopted as the patch
baseline.

## Read-only findings

The matrix processes launch from `/home/riverahernandezjason/agentic-workload-simulator`.
Its Git HEAD is a653ec2, but all five requested source files have existing
uncommitted changes. Worker 00's matrix state records case-runner SHA-256
`0bdec6be9c6eb45b6be206893010c3a1cc08e6dd81bc94a95c617b7d3de2b7bf`, matching
the a653ec2 blob. The selected attempt records proxy SHA-256
`7bc737a8d9d41b25bc894e63ea10075fd99c1197245cc4adb143f12463b65f74`, also matching
a653ec2. At inspection the on-disk runner hash was
`c3e8fb80fd2b5c36acbff7029e869731174ba6e8a3a02a9b6250a22f7af363ce` and proxy hash
`c0c0172fe80bb05cf741070a8ce719896aa689e3c862c14c36837d09f6b6c2f9`.
These establish historical versus current disk provenance; they do not prove
the loaded bytes of every already-running Python process.

Evidence root:
`/home/riverahernandezjason/h100-assignment-work-20260905/assignment/remaining-failed-486-cpu-docker-20260907T012757Z-16`.

Selected case: `worker-00/cases/00023`, instance
`scikit-learn__scikit-learn-25747`, raw attempt `runner_attempts/attempt-004`.

| Evidence | Observation |
| --- | --- |
| `request_proxy.jsonl`, rows 1–5 | Five 200 responses, 15:23:49.869–15:23:58.825 UTC |
| Row 6 | `request-b9cd77c29b5243d380937413c8145c7f`, `RemoteDisconnected`, no status, recorded 15:27:59.149 UTC, duration 239955.602506 ms |
| Rows 7–27 | 21 `ConnectionRefusedError` events through 15:44:10.273 UTC |
| `request_proxy_provenance.json` | Upstream `http://127.0.0.1:18000/v1`; timeout 5400 seconds |
| Case `runner.stderr.log` | `NOT_READY: request proxy event 6 is unsuccessful` among the accumulated retry reasons |
| Attempt's nested `eval.json` | Evaluator failed, return code 1, 15:44:12.297–15:44:12.512 UTC |
| Attempt's nested `evaluator.stderr.log` | Refused to overwrite the case-root `evaluator_result.json` |
| Case-root `evaluator_result.json` | Binds attempt 002 predictions; SHA-256 `39a60c6ed1528c9c56a58b9fb8da0d04b487220bc9a76bf9f19998eca546c711` |
| Worker `run_state.json` | `invalid_result` because `case_result.json` is missing, return code 1 |

High confidence: the strict proxy validator raises before the final case result
is written. An actual failed request must remain rejected. A second, independent
retry defect reuses the case-root evaluator result path, so later attempts
collide with an earlier result even after network recovery.

Exact a653ec2 source references (line numbers before this patch):

- `scripts/assignment/sweagent_case_runner.py:936`: strict all-2xx/error-free gate;
  `:1173`: validation raises before inventory; `:1268`: final result write.
- `scripts/assignment/sweagent_case_runner.py:1125`: evaluator template values
  reuse the case-root output/report directory; `:1139`: relative result resolution.
- `scripts/assignment/run_matrix.py:595`: isolated case session; `:600`: only
  its PGID is terminated; `:783`: previous result overwritten on repeated retries;
  `:806`: missing result mapped to invalid_result.
- `src/agentic_sim/runners/sweagent_runner.py:335` and `:368`: agent and evaluator
  each start a new session; `:337` and `:370`: independent full timeout waits.
- `scripts/assignment/evaluate_swebench_case.py:202`: another new session;
  `:209`: communicate waits; `:216`: stdout is persisted only afterward.
- `scripts/observability/request_proxy.py:143`: upstream response headers;
  `:148`: connection.close only on success; `:181`: error delivery can raise;
  `:194`: event append occurs after those error paths rather than in finally.

High confidence: the process hierarchy has nested `start_new_session=True`
boundaries. Killing the matrix's direct case process group cannot clean those
separate groups. The runner grants independent full waits to the agent and
evaluator rather than sharing the outer deadline.

Independent cleanup evidence: `worker-11/cases/00027`, instance
`sympy__sympy-15875`, attempt 001 evaluator ran from 12:36:46.374 UTC to
13:06:47.025 UTC and wrote `official evaluator timed out after 1800s`.
Container ID `70706f6af085bbb3f0623eac6b0ddffc695bd3e1d5acae148860ce6d64ac6b72`,
exact name `sweb.eval.sympy__sympy-15875.assignment-a8a534859e1e1ac3`, was observed
running hours later; creation was 12:37:07.692711228 UTC. The case was not active.
A later read-only `docker logs` call returned `No such container`: external
activity changed it during the investigation. No container was stopped by the
investigation. Historical Docker output could consequently not be retrieved.

Network attribution remains unresolved. At inspection, local port 18000 was
owned by SSH PID 2922735, forwarding to the remote host's port 18000. Its stdout
and stderr were `/dev/null`. Read-only SSH with BatchMode and the existing
tunnel's identity options was denied; no credentials were requested or changed.
The successful connection followed by local connection refusals is evidence
of transport unavailability, not proof of vLLM failure. No remote vLLM log or
request-ID match was available. A later read-only `/v1/models` call returned 200
and `Qwen/Qwen3-Coder-30B-A3B-Instruct`; this does not establish historical health.

Some other ports use `/tmp/remaining486-tcp-bridge.py`. Its source retains a
10-second socket timeout and swallows pipe exceptions. This is an additional
transport concern, not established as the cause of the selected port-18000
failure; those bridges were not modified.

The user's 936 accepted / 152 remaining count is an initial observation, not a
count frozen by this investigation. The independent live monitor continues.

## Contract and preservation

Completed results keep `assignment-case-result.v1` and the existing strict
all-2xx, error-free request acceptance check. Network errors are never converted
into successes or hidden by proxy retries.

`assignment-case-failure.v2` is a distinct failure-only schema: status is failed,
timeout, or unavailable; `accepted` is false. It preserves the reason, failure
type and timestamp, artifact inventory, and explicit inventory errors. Regular
files carry hashes, sizes, and modification timestamps; symbolic links are
recorded without traversal. A failure record and its sidecar are excluded from
their own inventory to avoid a recursive hash. Files still changing during an
inventory are identified as such. The result sidecar hashes the result itself.
Failure records must never enter accepted results or normalization as completed
cases.

Each evaluator invocation receives an attempt-specific output directory and
unique evaluator run ID. The stable case/agent/adaptive identity remains
unchanged. Historical evaluator outputs are retained. Mutable case-root
metadata is copied to a unique history directory before a retry replaces it.
No archival process follows symlinks or deletes raw attempts.

## Integration and canary status

Integration checks passed: 77 tests across the lifecycle, owned Docker,
SWE-agent runner, proxy, case-runner, matrix, and evaluator suites. The command:

```sh
PYTHONPATH=src:. python3 -m unittest tests.runtime.test_case_lifecycle tests.runtime.test_owned_docker tests.integration.test_sweagent_runner tests.observability.test_request_proxy tests.assignment.test_sweagent_case_runner tests.assignment.test_run_matrix tests.assignment.test_evaluate_swebench_case -q
```

The test owner also compiled 75 Python files and checked all shell scripts with
`bash -n`; final changed production modules compile and `git diff --check` passes.
Read-only checks against the installed pinned SWE-bench dependency verified that
container creation uses the owner-label hook, both evaluation and build cleanup
use retention, and shared image cleanup uses retention. This caught and fixed
eagerly imported cleanup aliases that otherwise bypassed the wrapper.

Integration also corrected mixed MONOTONIC_RAW/MONOTONIC comparisons: measurement
timestamps remain raw, while a single CLOCK_MONOTONIC deadline covers all stages.
Direct library calls establish that deadline too. The process helper launches a
dedicated Linux subreaper and signals proven descendants with pidfds, avoiding
unrelated sibling processes and PID reuse. Immediate double-forks and stubborn
nested sessions are covered by synthetic tests.

The new `owned_docker.py` helper verifies full IDs and exact UUID ownership labels,
captures selected non-secret metadata and logs, stops only owned containers, and
retains them. The case runner proves the pinned SWE-ReX Docker defaults before
adding labels and disabling automatic container removal. Custom Docker arguments
fail closed rather than being overwritten. Failure evidence capture or incomplete
cleanup prevents acceptance. Normal proxy shutdown drains handlers and reports
failure if draining is incomplete; no guarantee is made for an externally forced
SIGKILL before an in-flight request can finalize.

No rollout is authorized by a passing synthetic test alone. A real recovery canary must use fresh output,
the unchanged model/settings, verified versioned code, strict acceptance, and
provable ownership of every process/container it may terminate. Any missing
ownership or provenance is a rollout blocker, not permission for broad cleanup.

## Recovery canary — passed

The isolated recovery of `scikit-learn__scikit-learn-25747` ran once, from
18:14:02.927084 to 18:16:14.350140 UTC on 2026-09-07, using sealed code commit
`a42a9932cbd96f23d03773c4d5f18722e88c8c50`. Output is preserved at
`/home/riverahernandezjason/reliability-canary-20260907`; it was not imported
into or used to overwrite any live assignment output.

- Result: `assignment-case-result.v1`, completed, return code 0, no timeout.
- Proxy: 31 events, 31 distinct request IDs, all HTTP 200, zero errors;
  graceful proxy shutdown returned 0. The strict gate was unchanged.
- Official evaluator: one completed instance, zero evaluator errors,
  `official_resolved=false`. Reliability success does not mean the model solved
  the benchmark case.
- Artifacts: 114 inventory entries, zero inventory errors; a separate reread
  found zero SHA-256 mismatches. Raw trajectory, requests, predictions, evaluator
  report, process metadata, container logs and inspect evidence remain present.
- Process supervisor: 17 observed processes, cleanup complete, no survivors.
- Docker: exactly two UUID-owned containers, both stopped and retained; a
  subsequent read-only query confirmed both exited. No pre-existing container
  was stopped by this canary.
- Case SHA-256:
  `94f6db58700ba8ea4127ec98f02d5577c17d6f24beff8054701cc4dfa2138988`.
- Versioned manifest SHA-256:
  `21c57c57d20cc94a21c0e72baa4c5325505e0ded16043dd9172f66a959f1373e`.
- Receipt SHA-256:
  `9f2adeebc49201755eefa584734f130a8eba5bb8e48888090b5f75847ae221c4`.

The Qwen model, model revision, vLLM endpoint, dataset, agent/evaluator dependency
pins, case settings and acceptance contract were unchanged. Only versioned code
paths/hashes, ownership/lifecycle metadata and fresh attempt output paths changed.
Dependency syncing was disabled. The live matrix and monitor were not interrupted.

## Rollout handoff — not executed

Blocker: there is no reconciled, immutable retry-tail plan excluding cases now
accepted or still active in the independently progressing live matrix, and no
matching fresh shard manifests pinned to the canary-tested code. The initial
152 count must not be treated as a current executable work list. The old matrix
state binds the old runner and runtime-manifest hashes; do not bypass its identity
checks or point patched `--resume` at old output directories.

Prepare a new recovery run only after reconciling current completed and active
resume keys with the original assignment plan, retaining source result hashes
and every historical attempt. Use fresh output directories and manifests pinned
to a42a993, preserve all case/model/dependency settings, validate every shard
without execution, and then launch only the non-overlapping outstanding cases.
No exact execution command is supplied until those concrete inputs exist.
Transport attribution remains unresolved without historical tunnel/vLLM logs;
the patch preserves and rejects transport failures rather than masking them.
