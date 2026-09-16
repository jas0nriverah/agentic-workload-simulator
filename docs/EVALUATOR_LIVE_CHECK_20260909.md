# Live Verified Django7530 development fixture — 2026-09-09

The authorized real evaluator fixture passed: adapter exit 0,
`official_resolved: true`, one completed instance, zero evaluation errors and
zero empty patches. Total fixture wall time was 57.738 seconds. This records
development-fixture validation only. No agent or GPU inference ran, and this
result is not a candidate-selection or holdout result.

All new evidence is retained without overwriting prior artifacts:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/evaluator-live-django7530-worker22-20260909-if4_mnb6
```

## Exact inputs and execution

The dataset helper proved both original Parquet pins and all 800 ordered rows
and fields against their exact canonical JSONL. Its proof SHA is
`6c4729c1cbd7f7cf7b7e5bca09c08cd159dc2f1ed7e58118793469086f26e2b7`;
see `DATASET_MATERIALIZATION_CHECK_20260909.md`. This fixture consumed Verified
JSONL SHA `52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da`.

The unchanged, existing development prediction passed the frozen historical
identity eligibility check before it was read. Selection used the declared
instance and permitted prediction path, without consulting historical outcomes.
`input_provenance.json` retains that scope, source path, and hashes:

| Input | Value |
| --- | --- |
| Instance | `django__django-7530` (Verified) |
| Prediction file SHA-256 | `c638404075cc9cf122531d5ace9c9abdbe4fce9f99d61aa4917aafef3b12a732` |
| Nonempty patch size | 10,991 UTF-8 bytes |
| Patch SHA-256 | `ba48d61ea414b93c3e36fc9b24729bad2ae96d9a3801cfa80ba2c0acfc4446eb` |
| Fresh run ID | `fixture-django7530-3a8e1635bf34404f` |
| Fresh owner | `ed5c3a93a2a247a4b82062e45c0b4633` |

The released adapter and package were copied into `execution_source`, with all
56 files checked before/after copying and after execution. The adapter SHA is
`598d006c1cc79f0ce4fb4ddb2bd16e3e64a73f7bbd1ab9c2301a1104fdd4b83e`;
CPU policy SHA is
`f18946899f9a19639fb82d869d3be9901661bbfbf6d18cd2f4323e023c9f7d28`.
No mutable integration source was executed directly.

From the evidence directory, the exact outer command executed was:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  /home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909/venv/bin/python \
  run_live_fixture.py
```

`run_live_fixture.py` is a retained one-shot execution record; it refuses reuse
of its existing outputs. `launch.json` records the complete adapter argv and
required environment, including the absolute minimal runtime path/hash, owner,
600-second deadline, queue identity `worker-22`, prepared SWE-bench `PYTHONPATH`,
and explicit `--evaluator-project`/`--evaluator-revision`. Any subsequent run
requires fresh IDs and a new directory.

## Prepared evaluator and actual CPU placement

The evaluator imported 56 recorded SWE-bench modules from the prepared checkout
at `/home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909/repos/SWE-bench`,
exact clean revision `726c5461e2ef52d83cf1ea2107870a8bb3328d57`. Module paths and
hashes were independently rechecked after execution, and the checkout stayed
clean. The venv executable symlink was invoked as given, preserving its env.

The already-cached linux/amd64 image was reused:
`swebench/sweb.eval.x86_64.django_1776_django-7530:latest`, with exact image ID
`sha256:843450ff83f444aa4626d05cffc0c912b79eb26045f2f625a4f2dd2b58d627f4`.
The nonempty prediction applied cleanly and the real pinned harness ran.

The fresh minimal runtime bound CPU policy worker22 (worker CPU 26) to the
frozen policy source and hash. The fixture launched an evaluator only; it did
not launch or measure a worker container. Both the outer control process and
adapter inherited the evaluator control pool. For the actual evaluator
container, 96 samples independently confirmed Docker construction cpuset,
cgroup effective cpuset, and live PID affinity all equal `11-15,27-31`. PID
start ticks and cgroup membership bind those observations to the owned
container. Applicable ancestor `cpu.max` values were unlimited, with no Docker
quota, nano-CPU limit, or sampling errors.

Across the sampled 30.898-second interval, cgroup CPU usage increased by
12,498,841 microseconds; throttled time and throttling count both increased by
zero. These are sampled-interval deltas, not full-run accounting or an overhead
measurement. Pressure and raw `cpu.stat` samples remain in the JSONL evidence.

Owner-scoped cleanup completed. The single container
`075245b3feb71c904721f1d38e6f5eaa57e2367b9fed5f3a95d922bcbbfaf716`
is stopped and retained with its logs. Its final PID is zero, exit code 137,
and `OOMKilled` is false. The official stdout's retained-object count includes
`Unstopped containers: 1`; actual final inspection and cleanup evidence show
the owned object stopped. The harness process itself exited successfully.

## Evidence and limitations

| Artifact | SHA-256 |
| --- | --- |
| `live_result.json` | `4b501ae60a61e3ed2926f37d333f2a0091e50ce8a31f8d8fe3bbaf1bb98cae58` |
| `evaluator_result.json` | `f70e78ccc6dce7c6335902ff32d2deeecbb2d76a7059a239b4262541aeb13e0b` |
| `fixture_cpu_runtime.json` | `14e833520cf747cb7d7853dd40cb8ed441c9c2e82ada94b7591dbb14cb5428e1` |
| `container_cpu_samples.jsonl` | `2b7ae226e839873bb648ba367aa021f37685cdecf33c62d3ca8efba95bf44f33` |
| `official_evaluator/evaluator_import_binding.json` | `2258082d7559742ebb98e079e3097ac8bcb75bb30e1ec3d6251425d1cd6336a4` |
| Official aggregate report | `ddead1c330f07e99a2aaa7ffb0a0e92e21f12fa0a1994bf3eabbfc09b2102c11` |

The directory also retains literal predictions, generated evaluator inputs and
wrapper, full stdout/stderr, per-instance patch/eval/test logs and reports,
source/import bindings, process/container placement, cleanup inspection,
`post_execution_verification.json`, and an artifact hash manifest.

An earlier unlaunched staging directory
`evaluator-live-django7530-20260909-j2mf9izv` is preserved. Its attempted CPU unit
test command failed at importing pytest, which is absent in the prepared venv;
that was not a CPU implementation failure. The released owner's separate
154-test/133-subtest pass and busy/sleep evidence are documented in
`cpu-policy-implementation-20260909/FINAL_HANDOFF.md`. This fixture relies on
the released source and the actual observations above. The existing upstream
runpy already-imported warning and patch whitespace warnings remain in logs.

No runner, renderer, proxy, adapter, CPU policy, case pin, candidate, relay, or
serving configuration was edited by this task. The minimal runtime is explicitly
fixture-only and `production_launchable: false`. This proves one successful
real evaluation and its source/CPU bindings; it does not prove timeout/recovery,
22-worker contention, all acquisition gates, or final production readiness.
Main still owns release hash resealing and the broader launch gates.
