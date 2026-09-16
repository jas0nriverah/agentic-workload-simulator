# Evaluator read-only integration check — 2026-09-09

Evaluator imports and host dependencies pass, and the exact image for
`django__django-7530` is locally cached. The prepared dataset files fail the
current runtime byte-hash pins, so this is not a first-case readiness pass.

Follow-up acquisition-identity proof now identifies the cause: the runtime
assigned source Parquet hashes to JSONL paths. Both original Parquet pins
match, and all 800 public source rows reproduce the existing JSONL bytes
exactly. See `docs/DATASET_IDENTITY_PROOF_20260909.md` for the verified
materialization and root's narrow binding fix. The runtime still needs that
reviewed repair. The command below has also been corrected to Verified:
`django__django-7530` is absent from Lite and present in Verified. The earlier
durable handoff retains its original Lite command as superseded evidence.

Evidence is preserved in the new directory:

```text
/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260909T000000Z-resume/verification/evaluator-readonly-django7530-20260909-ay52g8wl
```

The prepared work root is
`/home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909`.
Checks used its `venv/bin/python` symlink directly, with bytecode writes disabled
and Hugging Face offline settings. Each subprocess was bounded by 30–40 seconds;
Docker SDK reads used a 10-second timeout. No inference, live evaluation,
container creation/start/exec, image pull/build, prediction reading, label
inspection, or candidate/holdout changes occurred. Dataset checks read opaque
bytes for SHA-256 only.

## Proven results

| Check | Evidence and result |
| --- | --- |
| Venv preserved | Python 3.11.16; `sys.prefix` is the prepared work-root `venv`, distinct from the base Python prefix. Main's `test_explicit_venv_python_retains_its_environment` passed. |
| Exact SWE-bench pin | `726c5461e2ef52d83cf1ea2107870a8bb3328d57`; original and prepared checkouts both clean at observation. |
| Prepared import binding | With `PYTHONPATH` set to the prepared `repos/SWE-bench`, `swebench`, official evaluation, Docker build, and test-spec modules import from that checkout. Paths and file hashes are in `prepared_checkout_bound.json`. |
| Default host dependencies | 77 installed distributions traversed from SWE-bench's default requirements; no missing requirements or version conflicts. This checks metadata with default extras, not optional inference packages or in-container imports. |
| Native evaluator CLI | Pinned `python -m swebench.harness.run_evaluation --help` exited 0 and exposed all required adapter flags. |
| Actual adapter wrapper CLI | The literal `PODMAN_COMPAT_WRAPPER` from the current adapter was archived and executed with `--help`; exited 0 with all required flags. No evaluation arguments or case labels were supplied. |
| Docker access | SDK `ping()` passed and the exact instance image was found through a local inspect. |
| Template handoff | 22 profiles, 22 templates, 22 unique GPU UUIDs; 94 archived files verified against v3; 2 focused tests passed. |

The pinned `TestSpec.instance_image_key` property, applied only to image-name
inputs, gives:

```text
swebench/sweb.eval.x86_64.django_1776_django-7530:latest
```

Its observed local image ID and repository digest are:

```text
sha256:843450ff83f444aa4626d05cffc0c912b79eb26045f2f625a4f2dd2b58d627f4
swebench/sweb.eval.x86_64.django_1776_django-7530@sha256:843450ff83f444aa4626d05cffc0c912b79eb26045f2f625a4f2dd2b58d627f4
```

The image reports Linux/amd64, `/testbed/`, ten filesystem layers, and
1,211,737,920 bytes. The existing adapter supplies namespace `swebench` and tag
`latest`. The pinned remote-image branch uses the cached image when found;
separate base/environment images are not required by that branch. This proves
cache availability at observation, not in-container Python/dependency execution
or successful evaluation.

Both help invocations emitted the upstream runpy warning that the evaluation
module was already imported by the package before execution. Both exited 0;
the exact warning is retained in their stderr logs. The prepared checkout
remained clean. Adapter/renderer snapshots were byte-stable during these help
checks; the integration checkout as a whole is mutable and is not a frozen
execution checkout.

## Concrete blockers and root handoff

The prepared `datasets` symlink points to the main work-root `datasets`
directory. The renderer and example carry the following byte-hash pins:

| Dataset | Expected runtime SHA-256 | Actual prepared SHA-256 |
| --- | --- | --- |
| Lite | `f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b` | `7f54792b83bf491c0a905770a00ce7fa28836552d37c7ea0e9e2bae4c53f33fb` |
| Verified | `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21` | `52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da` |

`sweagent_case_runner.py` compares `sha256_file(instances_path)` with the
manifest value and fails with `dataset instances_path SHA-256 mismatch`.
The two existing archived directories `pace-source-c8d03ba-20260905` and
`pace-source-exact-20260905-01` have the same observed hashes, so neither is a
byte-matching replacement. `dataset_readiness.json` records all six file checks.
Root needs to reconcile the frozen dataset-byte provenance and runtime pins
before the live command is used. No reason for the mismatch is inferred, and
no dataset, symlink, pin, or runner was changed here.

There is also a concrete import-binding distinction. Without explicit
`PYTHONPATH`, the report-directory interpreter imports the editable original
`/home/riverahernandezjason/h100-assignment-work-20260905/repos/SWE-bench`.
It is currently clean at the exact same pin, so this is not a wrong-version
failure. However, the manifest's prepared `evaluator.project` check alone does
not bind the child interpreter's imports: the adapter runs its child from the
report directory. The command below explicitly binds the prepared checkout;
root should preserve that binding in the final execution environment and
verify the imported path/hash at the frozen entrypoint.

In-container dependencies and the official evaluator's completion/report
behavior remain untested under this read-only authorization. A live check
requires an approved existing nonempty prediction for this declared instance;
an empty patch follows the adapter's no-harness short circuit and cannot prove
live evaluator execution. CPU placement belongs to root's other agent and is
not validated or changed here. The existing 65K source/observer and frozen
runtime gates remain pending as recorded in the worker templates.

## Live-validation command for after gates

This is a handoff command, not an executed validation or a final runtime
manifest. Root supplies the frozen source root, approved one-instance
prediction, a fresh report/result directory, run ID, and the real case
ownership/deadline environment through the approved CPU placement entrypoint.
The prepared dataset path below must first pass the reconciled frozen byte
pin. No replacement prediction or synthetic patch is generated by this check.

```bash
EVAL_WORK=/home/riverahernandezjason/h100-assignment-work-20260905/preflight-work-20260909
: "${FROZEN_REPOSITORY_ROOT:?bind the approved clean frozen source checkout}"
: "${APPROVED_DJANGO7530_PREDICTIONS:?bind the approved existing single-instance prediction}"
: "${NEW_EVALUATOR_REPORT_DIR:?choose a fresh report directory}"
: "${NEW_EVALUATOR_RESULT_PATH:?choose a fresh result path}"
: "${EVALUATOR_VALIDATION_RUN_ID:?supply the approved run identity}"
: "${ASSIGNMENT_CASE_OWNER:?inherit the real case owner from the launcher}"
: "${ASSIGNMENT_CASE_DEADLINE_MONOTONIC_NS:?inherit the real bounded case deadline}"

env PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$EVAL_WORK/repos/SWE-bench:$FROZEN_REPOSITORY_ROOT/src" \
  "$EVAL_WORK/venv/bin/python" \
  "$FROZEN_REPOSITORY_ROOT/scripts/assignment/evaluate_swebench_case.py" \
  --evaluator-python "$EVAL_WORK/venv/bin/python" \
  --dataset "$EVAL_WORK/datasets/SWE-bench_Verified.jsonl" \
  --predictions "$APPROVED_DJANGO7530_PREDICTIONS" \
  --instance-id django__django-7530 \
  --report-dir "$NEW_EVALUATOR_REPORT_DIR" \
  --run-id "$EVALUATOR_VALIDATION_RUN_ID" \
  --result "$NEW_EVALUATOR_RESULT_PATH" \
  --timeout-seconds 1800
```

The adapter derives the child timeout from the existing monotonic deadline,
uses one harness worker, and validates a newly created official report. This
check does not establish that outcome; the command is retained for root's
subsequent authorized validation.
