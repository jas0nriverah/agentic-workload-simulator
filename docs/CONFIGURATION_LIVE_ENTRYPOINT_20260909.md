# Configuration live entrypoint inspection — 2026-09-09

Status: BLOCKED FOR LIVE EXECUTION. This is a read-only inspection of runtime code and existing planning artifacts, plus this report. No inference, endpoint requests, GPU probes, jobs, tunnels, core edits, manifest generation or resealing were performed. The 96-case inventory is present and internally hash-consistent; its confirmation schema is not accepted by the current case runner or scheduler. There is no supported one-command launch of this panel today.

## Artifact roots and exact first case

These are existing paths; subsequent sections explicitly distinguish proposed output paths from existing inputs.

```bash
REPO=/home/riverahernandezjason/agentic-submission-repairs-20260908
WORK=/home/riverahernandezjason/h100-assignment-work-20260905
SNAPSHOT="$WORK/assignment/submission/20260908T140000Z-offline-v2"
PLAN="$SNAPSHOT/live-plan"
PYTHON="$REPO/.venv/bin/python"
```

The authoritative panel is `$SNAPSHOT/configuration-analysis/CONFIGURATION_CONFIRMATION_PANEL.json`. The execution expansion is `$PLAN/configuration_confirmation_execution_plan.json`. It has exactly 96 cases: 24 each of `expanded-call100-input61440`, `expanded-call100-input61440-observation25000`, `expanded-call50-input61440`, and `historical-control-call30-input32768`. Every referenced case-spec and request-config file was rehashed during this inspection; all 192 references matched. This does not certify their runtime compatibility.

| Existing artifact | SHA-256 |
| --- | --- |
| `configuration-analysis/CONFIGURATION_CONFIRMATION_PANEL.json` | `1e7ed714a4d910b5bb17ab5fe596fde57736ea882b8b41cead9e666ebc04e398` |
| `live-plan/configuration_confirmation_execution_plan.json` | `d282607b28ffff76a17c96521adf923acf82fc3b0be3b7bed929f5ae330ba28e` |
| `live-plan/production_candidate_inventory_manifest.json` | `8b8747ff9072b3e74b2a6faf28482adeec57c1bccc6e5d0ca34645fa60ff0d56` |
| `live-plan/configuration-confirmation-case-specs/001-expanded-call100-input61440.json` | `444954091614c6714cc9413236deac6c06b1b4f037ece49cfb119863da56d132` |
| `live-plan/confirmation-request-configs/expanded-call100-input61440.json` | `bd806973acb96beb8fe50220cdf1e9117b78198ebb93699c93b502a4ce67b5fb` |

Use execution index 1 for the first bounded development check, preserving the declared order rather than choosing an instance by observed performance:

- Instance: `django__django-7530`; suite: `verified`; repository: `django/django`.
- Candidate: `expanded-call100-input61440`.
- Case/resume identity: `configuration-confirmation-case-v1:26704e7d91e2df8e0b5536a02d24b1dc5c77f8196ded0af5382a75c3b97d58d7`.
- Panel identity: `configuration-confirmation-panel-v1:00efed54ac41885f1d56f236e9b2a066b5420bcfbeed2794699f465b41608ca1`.
- Seven unchanged settings: call limit 100, max input 61440, max output 2048, observation length 100000, temperature 0.0, top_p 1.0, seed 0.
- Serving: vLLM 0.10.0, max_model_len 65536.
- Expected evaluation image name: `swebench/sweb.eval.x86_64.django_1776_django-7530:latest`; record the actual image identity before execution rather than treating a mutable tag as provenance.

This is an existing development-panel coordinate, not permission to create a 97th confirmation member. If the first run is an integration rehearsal before the source/configuration freeze, label it separately and do not silently count it as frozen confirmation evidence. If it is the first accepted frozen confirmation trajectory, resume with the remaining 95; preserve all attempts and outcomes. The admission rule must be recorded before execution.

## Concrete blockers and required glue

1. **Confirmation case adapter.** `scripts/assignment/sweagent_case_runner.py::load_case` accepts only `assignment-steps-1-3-plan.v1` and `assignment-production-v2-plan.v1`. Loading the actual first spec reproduced `case specification has missing or unknown fields`. Its schema is `assignment.configuration-confirmation-case.v2`. Do not relabel it as historical or production. A reviewed adapter must validate its existing field set, panel/candidate membership, fresh confirmation identity, seven settings, instrumentation and serving descriptor, and source hashes. Execution also currently accesses `variation`, `per_case_deadline_seconds`, and other historical fields that this spec lacks. Those need an explicit execution mapping (including declared concurrency 1 and a reviewed case deadline), not a schema-name substitution. Runtime example deadline is 5400 seconds, but the confirmation spec itself does not declare it.
2. **96-case orchestration.** `run_matrix.py` reads a JSONL plan header followed by cases and validates historical or 1088-case production contracts. The confirmation execution plan is a JSON object with a `cases` array. No confirmation scheduler CLI is implemented. `--max-cases 1` does not bypass its plan validation. Required glue: validate all 96 memberships/hashes before dispatch, stage each exact case, bind endpoint/runtime per attempt, enforce sequential concurrency, maintain durable resume/attempt identity, and stop assignment on evidence/infrastructure failure. No bulk shell loop or invented CLI is presented as a substitute.
3. **Clean reviewed execution source and projects.** The current repository is dirty, so both manifest rendering and case execution refuse it. The ordinary `$WORK/repos/SWE-agent` HEAD matches the pin but currently has untracked `uv.lock`; the runner's clean-project check rejects this too. `$WORK/repos/SWE-bench` is clean and pinned. Root must provide reviewed clean execution checkouts; this inspection did not remove files or commit anything.
4. **Fresh runtime manifest.** No v2 manifests were found in `$WORK/assignment/runtime`. `$PLAN/pin_evidence/runtime_manifest_worker_00.json` is historical: it binds `agentic-workload-simulator`, commit `c8d03ba990cfe2263d5eb3e4b64124c969875301`, and an older worker project, with no telemetry descriptor. Do not reuse it. The example has zero integrity hashes and placeholder hardware paths. Neither is a full-capture runtime configuration.
5. **Request-config reconciliation.** The existing first confirmation request fragment contains only completion max_tokens/seed/top_p. Current `cloud/lambda/sweagent_request.yaml` is JSON despite its extension and additionally declares the reviewed deployment setting and noninteractive pager environment. The case runner materializes `request_config.json` from the manifest-bound template, inserting max_tokens/top_p/seed. Thus the old fragment hash is not the current effective config hash. Preserve old declarations and record a reviewed derived-config binding that retains all seven settings and the intended tool environment; do not silently overwrite the sealed plan. The renderer currently hardcodes the repository request template, not the per-candidate plan fragment.
6. **Serving witness integration.** Full capture needs a real enabled `runner.telemetry.serving_metrics` descriptor plus external request-access evidence. Omission normalizes to disabled, so merely passing the runner's v2 audit does not prove serving attribution. A producer must correlate actual physical request IDs and measurement windows with a dedicated-server access log, and make witnesses available in time for the proxy's checks. The witness CLI exists; an automatic producer/lease binding for this panel was not found.

## Pinned execution and endpoint binding

| Component | Required binding / inspected evidence |
| --- | --- |
| SWE-agent | `$WORK/repos/SWE-agent`, HEAD `0f3acafacabc0def8cc76b4e48acb4b6cf302cb9`; dirty as noted above |
| Official SWE-bench | `$WORK/repos/SWE-bench`, HEAD `726c5461e2ef52d83cf1ea2107870a8bb3328d57`; clean |
| Evaluator Python | `$WORK/venv/bin/python`; its editable `swebench` mapping points to `$WORK/repos/SWE-bench/swebench` |
| Model and tokenizer | Qwen/Qwen3-Coder-30B-A3B-Instruct, revision `b2cff646eb4bb1d68355c01b18ae02e7cf42d120` for both |
| Verified dataset | `$WORK/datasets/SWE-bench_Verified.jsonl`, required SHA `43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21`, revision `91aa3ed51b709be6457e12d00300a6a596d4c6a3` |
| Lite dataset | `$WORK/datasets/SWE-bench_Lite.jsonl`, required SHA `f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b`, revision `69611d31007e1c6731db8bd5b5c3f2d33f5bab6e` |

Dataset hashes above are required configuration values, not a claim that this inspection rehashed the datasets. Pinned model bytes, tokenizer bytes, remote serving flags, Docker readiness and privileges still need fresh runtime verification.

The user's configure-worker report supersedes the earlier allocation/endpoint snapshot: **22 ready workers, 00–10 and 12–22, return HTTP 200**. Workers 20/21/22 bind local ports 18120/18121/18122; worker 11 expired and is excluded. The VPN and relays run on the idle CPU VM; the active Codex VM is untouched. Preserve all connections, jobs and tunnels. These are supplied configure-worker observations, not independent probes by this inspection. They establish the reported resource pool, not completion of the confirmation adapter, source freeze or full-capture gates.

For the first predeclared case, select one of these reported-ready workers and bind its exact served alias, API endpoint, metrics identity/epoch, remote hardware profile and access witness into one runtime manifest. Worker 11 must not enter the dispatch set. Keep first-case concurrency 1; the 22-worker pool does not change the case settings or create confirmation scheduler support. Rechecked after this resource update: neither the case runner nor run_matrix contains confirmation-schema integration; the runner SHA remains the inspection-time value below.

`model.name` must equal that endpoint's advertised served alias, full or short, while `model.revision` remains pinned to the same model. The runner prefixes `hosted_vllm/` for LiteLLM transport. Record both the exact served alias and resulting effective argv. The plan's bare `Qwen/...` argv with placeholders is not the effective runner argv or a proof of alias compatibility. `model.api_base` must bind the corresponding local tunnel `/v1`, and metrics_url its verified metrics endpoint. An endpoint's `/v1/models` response alone does not prove model revision.

Current hardware source handles numeric `processor` indices and prioritizes textual CPU model names. Use the corrected local AMD EPYC 7B12 inventory plus the selected remote H100 profile; do not substitute a remote GPU-host CPU for the actual local tool-execution CPU. The corrected sealed profile's concrete path/hash was not supplied or located in the inspected continuation artifacts, so `PROFILE` below is deliberately unresolved. Record raw local/remote inventories and their distinct roles; model-facing terms remain the reviewed CPU frequency, GPU bandwidth and GPU compute projection.

## Supported commands, with execution boundaries

### Runnable now: offline rejection and artifact verification

This command reads existing files only. It confirms current schema rejection without invoking the case runner main routine (which can write validation artifacts even in validate-only mode).

```bash
cd "$REPO"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" - <<'PY'
import hashlib, json
from pathlib import Path
from scripts.assignment.sweagent_case_runner import load_case, CaseRunnerError
plan_dir = Path('/home/riverahernandezjason/h100-assignment-work-20260905/assignment/submission/20260908T140000Z-offline-v2/live-plan')
plan = json.loads((plan_dir / 'configuration_confirmation_execution_plan.json').read_text())
assert len(plan['cases']) == 96
for case in plan['cases']:
    for key in ('case_spec', 'request_config'):
        ref = case[key]
        assert hashlib.sha256((plan_dir / ref['path']).read_bytes()).hexdigest() == ref['sha256']
print('96 case specs and 96 request references match')
try:
    load_case(plan_dir / plan['cases'][0]['case_spec']['path'])
except CaseRunnerError as exc:
    print('CURRENT BLOCKER:', exc)
else:
    print('Schema accepted: re-review this report against the changed source')
PY
```

### Supported renderer: blocked until clean source and external bindings exist

`CLEAN_REPO`, `TEMPLATE`, `PROFILE`, and `RUNTIME` are required operator inputs, not existing verified artifacts. `TEMPLATE` must be an external copy of the current runtime template with the exact endpoint alias/api_base and enabled serving-metrics descriptor; there are no renderer `--api-base`, `--model-alias`, or `--serving-metrics` flags. `PROFILE` must be the corrected sealed remote descriptor. `RUNTIME` should be a fresh absolute path outside the checkout.

```bash
"$PYTHON" "$CLEAN_REPO/scripts/assignment/render_runtime_manifest.py" \
  --repo-root "$CLEAN_REPO" --work-root "$WORK" --hardware h100 \
  --evaluator-python "$WORK/venv/bin/python" \
  --template "$TEMPLATE" --remote-hardware-profile "$PROFILE" \
  --output "$RUNTIME" --validation-only
```

This prints the manifest without writing it and does not prove dependencies or endpoint readiness. After review, omitting `--validation-only` writes the manifest and sidecar. The renderer sets project paths to `$WORK/repos/SWE-agent` and `$WORK/repos/SWE-bench`; if isolated projects are needed, provide a reviewed work-root layout rather than assuming the archived worker paths will be retained. Use an interpreter/environment bound to the clean source; the command above assumes `$PYTHON` is that reviewed interpreter by execution time.

### Supported single-case CLI: conditional, currently rejected

After the confirmation adapter is implemented/reviewed, root has frozen source/configuration bindings, and the first original case spec is staged byte-for-byte at `$OUT/case_spec.json`, the supported commands are:

```bash
"$PYTHON" "$CLEAN_REPO/scripts/assignment/sweagent_case_runner.py" \
  --case-spec "$OUT/case_spec.json" --output-dir "$OUT" \
  --runtime-manifest "$RUNTIME" --cpu-docker --validate-only
```

`OUT` must be a fresh external development directory containing the spec. Validation writes local validation/lock artifacts, launches no model, and does not verify a live BPF attachment. Do not run it against the immutable planning directory. The exact future live command is:

```bash
"$PYTHON" "$CLEAN_REPO/scripts/assignment/sweagent_case_runner.py" \
  --case-spec "$OUT/case_spec.json" --output-dir "$OUT" \
  --runtime-manifest "$RUNTIME" --cpu-docker --execute
```

This last command is documented only, not executed or authorized by this report. `--cpu-docker` selects local CPU/Docker plus remote H100 inference; it is not a model stub or full hardware-preflight substitute. Do not set workload telemetry activation variables globally: the runner owns child activation and strips those variables from supervisors/evaluator retries.

For the subsequent 96-case stage, repeat the reviewed single-case integration only through the missing confirmation dispatcher described above. There is currently no valid `run_matrix.py --plan configuration_confirmation_execution_plan.json` command. The production finalizer's 1088-case output is not a confirmation substitute, and candidate selection must follow these 96 outcomes, not precede them.

## Evaluator integration already present

The case runner constructs one-row SWE-agent input from the exact source dataset, adds the expected evaluation image, and binds its hash. It runs actual `uv run --project ... sweagent run-batch`, with default agent config and the derived request fragment, through the owned proxy. It creates `runner_attempts/attempt-001/preds.json`, then invokes the manifest-bound evaluator adapter. The configured adapter argv is real:

```text
WORK/venv/bin/python CLEAN_REPO/scripts/assignment/evaluate_swebench_case.py
  --dataset DATASET_PATH --predictions ATTEMPT/preds.json
  --instance-id django__django-7530 --report-dir ATTEMPT/official_evaluator
  --run-id ATTEMPT_BOUND_EVALUATOR_RUN_ID --result ATTEMPT/evaluator_result.json
```

These placeholders are filled by the case runner; do not manually invent a run ID or launch a second evaluator beside it. The adapter invokes the pinned `swebench.harness.run_evaluation`, using the owned-container compatibility wrapper where applicable, max_workers 1, default evaluator timeout 1800 seconds capped by the remaining inherited case deadline. The evaluator interpreter must continue importing the exact clean pinned checkout; checking another directory's HEAD alone is insufficient. Official report, dataset, predictions, run ID, counts and report hash are validated before completion. Zero-request fallback uses the existing bounded evaluator-only retry and strips v2 auto-activation.

Dependencies: working Docker/SWE-ReX persistent shell; corresponding image availability and retained identity; permissions for owned cleanup; evaluator Python's pinned harness and dependencies; sufficient deadline and disk budget. A successful agent exit is not an official resolved outcome.

## Full-capture binding and source provenance

The runtime must require v2 activation, raw request payloads and CPU work. CPU configuration must select `kernel_aggregate` or `bcc`, retained raw individual operations, attach_existing_process=true and require_persistent_runtime_pid=true. The supervisor launches the BCC service with `/usr/bin/python3`; as an ordinary user it uses `sudo -n env ...`. Required dependencies include noninteractive privilege for that owned service, BCC/kernel support, native sink compiler/library dependencies, and container-to-host persistent PID identity mapping. Only the BCC service needs privilege; do not elevate the agent to avoid a dependency failure.

The enabled metrics descriptor needs schema `assignment.serving-metrics-config.v1`, enabled=true, metrics_url, server_identity, counter_epoch, positive timeout_seconds, absolute access_witness_path, access_witness_evidence_kind=`external_access_lease`, and vllm_version=`0.10.0`. The existing witness CLI can validate an already captured independent access log:

```bash
"$PYTHON" "$CLEAN_REPO/scripts/observability/serving_access_witness.py" \
  --access-log "$ACCESS_LOG" --output "$WITNESS" --request-id "$REQUEST_ID" \
  --window-start-monotonic-ns "$START_NS" --window-end-monotonic-ns "$END_NS" \
  --server-identity "$SERVER_ID" --lease-id "$LEASE_ID" \
  --counter-epoch "$COUNTER_EPOCH" --vllm-version 0.10.0 --dry-run
```

This is supported syntax for real externally supplied inputs, not a command to fabricate a lease. Automatic per-request orchestration and the same-server clock/window binding remain required.

Preserve and review the attempt's `request_proxy.jsonl`, request-config/provenance, `telemetry_v2/telemetry_manifest.json`, all four v2 journals, raw request/response payload artifacts, hardware/resource snapshots, `telemetry_v2/linux_work/{work_summary.json,raw_aggregates.jsonl,raw_events.bin,bpf_collector_manifest.json,service_lifecycle.json}`, retained native source/library, activation evidence, official evaluator output, predictions, cleanup and final result inventory. Exact binary/journal paths also come from their descriptors. Check zero loss/map failures, raw-byte hashes, complete nondeferred capture, finalized deferred actions, tool coverage, expected run/attempt/case identity, and readable stopped-service artifacts.

The runtime manifest checks eight explicit source/config paths and hashes (case runner, evaluator adapter, request config, proxy, adaptive wrapper/runtime/protocol, event simulator) and the clean repository commit. A separate complete source bundle must additionally cover imported runner/lifecycle/owned-Docker code, `src/sitecustomize.py`, telemetry hooks/autoinstrument/v2/hardware/process-resources/BPF/native sink/serving metrics, the journal auditor, witness producer, dependency locks, and reviewed configuration/plan bindings. A runner-only SHA is not a freeze of its imported dependencies.

Inspection-time hashes, not a replacement for root's final freeze:

| Source | SHA-256 |
| --- | --- |
| `scripts/assignment/sweagent_case_runner.py` | `3dc1b2b0ebd46d1b2e705b356d3b41b143ae5a5811bc87dea22b0af1e64d6bb6` |
| `scripts/assignment/evaluate_swebench_case.py` | `100e69c9eab43dae8002bd8c78ee53d24ae6d314e8260acef0cf82bda6fc41b2` |
| `scripts/observability/request_proxy.py` | `33d6d1c4be9ffca7b8491c8a413d027bfd63fdf81fb24e6257f761e6a12ea985` |
| `src/agentic_sim/runners/sweagent_runner.py` | `c6b30b19056e6a7f1f56106909008b16ffc9475aac2026758d017c5eeac4d9eb` |
| `cloud/lambda/sweagent_request.yaml` | `5244ca0794c8668211ea4b3182b738f4171e10cd7b22eb2bb304c8735cd159c9` |

The first spec's embedded `case_spec_sha256` is a different upstream binding from its file-byte hash; validate both against their declared origins rather than equating them. Preserve source_case_spec_sha256, task_sha256 and source_manifest_sha256 and trace them through the panel's source case. The current runner checks dataset-file integrity but does not itself establish all these confirmation-panel lineage relationships. The confirmation adapter must.

## Existing preflights and D1–D9 boundary

- `scripts/cloud/lambda_preflight.sh`: general idle-host gate; expects no GPU compute processes and a free serving port. It is not appropriate as-is to assert readiness of already-serving tunneled allocations. Its dry-run only prints intended checks.
- `scripts/cloud/h100_setup_doctor.sh`: supports `--offline` and `--check-server`, but defaults to the older `configs/h100_final_validation.json`/`h100_case_runner.py` contract. Passing it does not validate this confirmation panel.
- `scripts/observability/probe_runtime.py`: `--output PATH --dry-run` documents capability probing; actual probing writes a capability report and does not establish per-request timing.
- `scripts/assignment/d9_live_holdout.py`: historical Docker-tools/local-model-stub harness; not an actual pinned-model full-capture entrypoint.
- `scripts/validation/run_instrumentation_replay.py --manifest FILE --output-dir DIR` supports offline validation; `--execute` launches fixed-work adapters. The separate overhead protocol is four fixtures, 12 AB/BA/AB pairs, 24 condition passes; it is not the 96 confirmations and still requires real adapter bindings.
- `scripts/validation/check_instrumentation_pilot.py EVIDENCE` validates supplied pilot evidence; it does not collect it. The existing workflow requires the natural 16-case pilot and exact reuse conditions; a first development case does not replace it.

All-PDF D1–D9 acquisition remains the parent requirement. D1 needs official outcomes and a declared end-to-end boundary for both suites. D2/D3 and Step-1 D4 need repository categories plus defensible CPU/model timing attribution. Step-2 D4/D5/D6 need the declared independent sweeps and explanatory evidence, not just candidate comparison. D7/D8 need individual CPU operation records, physical inference requests/tokens/context, lifecycle accounting and validated latency attribution; proxy wall time is not automatically GPU kernel time. D9 needs frozen split/model/hardware bindings, prediction-before-label evidence, individual-event and end-to-end error gates of 25%, and the required plots. `--adaptive-runtime-config` is supported by the runner, but requires a separately sealed calibration/split/tokenizer/hardware configuration; full capture alone is not a D9 accuracy pass.

Use `configs/assignment_acquisition_contract.v2.json` and the live-plan workflow's acquisition/regression proof requirements as acceptance inputs. Successful first-case capture, schema support or a healthy endpoint cannot certify all D1–D9 readiness. Completion order is: reviewed confirmation integration and clean source/config binding; one bounded full-stack development proof; required pilot/confirmation/overhead/recovery evidence under declared reuse rules; all acquisition gates; selection and production finalization. This report supplies no production launch authorization.
