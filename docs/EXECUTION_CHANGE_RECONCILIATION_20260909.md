# Current-byte execution reconciliation

This finite register records the intended recovered implementation before the
combined trajectory. It does not reclassify the PDF or change old evidence.
The exact included/excluded path lists, byte hashes and current/snapshot equality
are in the external `source_manifest.json` receipt. The final verification table
and snapshot test outputs live beside that receipt, so this document does not
create a self-referential source hash.

The original PDF remains the sole assignment authority. The reviewed 24 literal
A and 10 derived B register is retained. Previous pilot sizes, exact worker
counts, mandatory clean Git, 5%/10% perturbation limits, fsync-per-record and
96/1,088-case proposals are not adopted as assignment requirements. The isolated
snapshot is checkpointed only to satisfy the existing runtime identity contract;
the authoritative VM working tree is not reset or required to be clean.

| Expected work | Exact current implementation / retained evidence | Verification to bind in snapshot receipt |
| --- | --- | --- |
| PDF authority and provenance | `configs/pdf_normative_requirements.v1.json`, `configs/requirements_provenance_register.v1.json`, `docs/PDF_NORMATIVE_REQUIREMENTS.md`, `docs/REQUIREMENTS_PROVENANCE_REGISTER.md` | Exact file hashes; already reviewed PDF SHA |
| Historical 800 baseline + 288 sweep executions and corrected results | Retained `project/`, `SNAP/` and external historical evidence; `docs/HISTORICAL_IMPROVEMENT_REVIEW_20260909.md` | Preserve original bytes and historical labels; no new-measurement claim |
| Dataset serialization/hash reconciliation | `configs/assignment_runtime_manifest.example.json`, `scripts/assignment/render_runtime_manifest.py`; `docs/DATASET_IDENTITY_PROOF_20260909.md` | Existing exact 800-row identity proof; renderer tests |
| Evaluator ingestion, patch/attempt/source identity | `scripts/assignment/evaluate_swebench_case.py`, `scripts/assignment/sweagent_case_runner.py`, `src/agentic_sim/runners/sweagent_runner.py` | Runner/evaluator tests; retained live Django7530 official pass; new combined result required |
| Individual CPU events and integrity failures | `src/agentic_sim/telemetry/bpf_work.py`, `native_bpf_sink.py`, `native_bpf_sink.c`; `scripts/assignment/sweagent_case_runner.py` | BPF/runner audit tests; real v3 Docker proof, 3,149 records, zero losses |
| Overhead thresholds advisory, corruption still fatal | `scripts/validation/check_instrumentation_pilot.py` | `tests/assignment/test_instrumentation_pilot_gate.py`; historical diagnostic rows untouched |
| Procfs cwd and cd compatibility | `src/agentic_sim/telemetry/bpf_work.py`, `sweagent_hooks.py`, `script_state.py`; `scripts/validation/fixed_work_adapter.py`, `check_persistent_shell_capture.py` | Cwd/hook/adapter tests; retained paired profile 341.195 ms added work, 1,188 records, zero losses |
| Post-completion streaming cleanup | `src/agentic_sim/telemetry/serving_observer.py` | Observer tests; original serial archive preserved |
| Native-deferred production serving | `scripts/observability/request_proxy.py`, `scripts/assignment/sweagent_case_runner.py`, `scripts/observability/derive_server_attribution.py`, `src/agentic_sim/telemetry/native_vllm_observer.py` | Proxy/native tests; exact server/epoch/body/case/attempt joins, zero proxy scrapes/witness reads; new live trajectory required |
| Cache and native/API token consistency | Same runner/native files; `src/agentic_sim/telemetry/v2.py` | Explicit cache zero distinct from unavailable; missing usage and inconsistent native counts rejected |
| Serving fingerprint and controlled restart | `scripts/validation/serving_fingerprint.py`, `collect_worker_fingerprints.py`, `controlled_vllm_restart.py` | Parser/restart tests; refreshed live endpoint/model/GPU/epoch/source binding required |
| Queue partial pool/storage advisories, ownership safeguards | `scripts/assignment/shared_case_queue.py` | Queue tests; demonstrated shortfall/probe failure/identity drift remain fatal; 22-worker barrier not required |
| CPU placement and resource lifetime | `src/agentic_sim/telemetry/cpu_policy.py`, `container_resources.py`; runner/evaluator placement | CPU policy/container tests; tool worker22 CPU26 distinct from remote serving CPUs24–31 |
| CPU clock/topology/source retention | `src/agentic_sim/telemetry/hardware.py` | Hardware tests, infinity rejection and exact cpuinfo bytes/hash; host archive `astra-cpu-host-20260909-v1` |
| Mount namespaces and actual container context | `src/agentic_sim/telemetry/container_resources.py`, `bpf_work.py` | Raw mount/source lifetime tests; cgroup CPU/I/O/fault/pressure context is not per-syscall physical traffic |
| Exact model/tokenizer/template inputs | `scripts/validation/capture_acquisition_inputs.py`; `astra-model-inputs-20260909-v1`, `model-snapshot-verified-v1` | Six exact metadata files retained; 16-shard size identity preserved without recopying weights |
| Full current-byte source snapshot | `scripts/validation/materialize_execution_snapshot.py`; new runner `--execution-source-manifest` | Snapshot utility tests; every tracked/untracked file hash checked; runtime/source receipts retained inside case result artifacts |
| Lossless raw reconstruction | `scripts/validation/export_acquisition_evidence.py` | Eight tests; saved-only five-source component proof: 4,467 CPU operations, two model attempts, 49 lifecycle intervals; new combined proof required |
| Environment-only Studio fixture failure | `tests/cloud/test_render_studio_manifest.py` | Fixture selects the actual pytest interpreter; five recovered tests pass |
| Storage and retention | Queue storage checks; historical evidence directories unchanged | Current real capacity/quota checked for execution, not filesystem-total inference; no historical Docker bulk deletion |

## Additional intended work found beyond the initial checklist

All these files are included, not discarded because they are uncommitted:

- `cloud/lambda/sweagent_request.yaml`: noninteractive pager environment.
- `pyproject.toml`: packaged native sink C source.
- `sweagent_case_runner.py`: one-row runtime dataset materialization without
  changing official dataset bytes; pinned evaluator imports; owned Docker
  cleanup, empty-patch/zero-request failure behavior; partial native archive on
  transport failure; direct immutable execution-input binding.
- `src/agentic_sim/runners/sweagent_runner.py`, telemetry hooks and journals:
  setup/state/model-client/retry/teardown spans and durable physical requests.
- `bpf_work.py` plus every decoder consumer: v3 raw scalar ABI, explicit v2
  compatibility and per-CPU scratch buffers avoiding the actual kernel stack
  overflow. No syscall argument or individual row removed.
- `sweagent_hooks.py`: owned short transient IPC socket for long durable paths.
- `scripts/assignment/generate_step_figures.py`: recovered headline/category/
  sweep figure fixes. Historical simulator/protocol changes remain available;
  they are not evidence that the new D9 model already meets its error target.
- Snapshot/export regression tests and this takeover's hardware, acquisition,
  source, native and socket negative tests.

Only `.git` metadata and explicitly listed ignored caches/environments are
excluded from the source copy. Required external environments, pinned SWE-agent
and evaluator checkouts, datasets, model metadata and remote runtime evidence
remain separately hash-bound acquisition inputs. Generated Git-visible `SNAP`
and project evidence are retained and labeled historical, not used as substitute
current source. No raw evidence is rewritten to improve a status.

Source equality and tests do not themselves establish D1–D9 measurement
completeness. That decision remains open until the combined saved artifacts
and independent falsification demonstrate the required reconstruction chain.
