# Assignment traceability and pre-H100 evidence map

The attached coding-test PDF is authoritative. This matrix maps each required
deliverable to the first command/artifact that will produce evidence. A
`pending_h100` entry is an explicit validation boundary, not a result.

| Assignment requirement | Future command / source of truth | Artifact or table | Report destination | Local acceptance |
| --- | --- | --- | --- | --- |
| Step 1 baseline, Lite and Verified | `lambda_run_first_experiment.sh --mode uninstrumented`; `swebench.harness.run_evaluation` | `run_manifest.json`, `.traj`, `preds.json`, official evaluation report | Baseline resolved rate/E2E table and plots | Command contract, evaluator isolation, archive round-trip |
| Four hyperparameter sweeps | Resolved SWE-agent `run-batch` commands after the G6 pilot | One run manifest per cell with call limit, `completion_kwargs.max_tokens`, observation length, temperature, and seed | Per-parameter plots plus consolidated comparison | `validate_sweagent_command.py`; Linux `--print_config` rehearsal |
| CPU/GPU event-level analysis | Thin observer plus reviewed normalizer and reset-safe interval accounting | `events.jsonl`, normalized model/tool index, interval-union report, Prometheus snapshots, GPU samples | Event taxonomy, CPU/GPU boundary table, selected case study | Normalized fixture/interval validators; unified clock identity; no cross-host/boot merge |
| Detailed high CPU-to-GPU case study | Separate, authorized `strace`/Nsight Systems attempt after first fixture | Raw profiler output and provenance sidecars | Step 3 case-study section | Capability probing and safe command dry-runs only pre-H100 |
| Hardware-parameterized simulator | Calibration and normalized real events after Step 3 | Simulator inputs, predictions, latency-error evaluation | Step 4 model and error plots | Deferred until real fixture and calibration |
| Gold-patch smokes | `lambda_run_gold_smoke.sh --suite lite|verified` | Gold smoke reports and image/dataset hashes | Runtime/evaluator validation appendix | Static official command validation; H100-only execution |
| Publication-quality report | Post-experiment aggregation and plotting | Immutable manifests, figures, tables, narrative | Final submission write-up | Provenance and secret/path scans |

## Frozen execution boundaries

- Model/configuration: `Qwen/Qwen3-Coder-30B-A3B-Instruct`, BF16, 32K
  context guard, vLLM 0.10.0 with `qwen3_coder`, SWE-agent 1.1.0, and
  SWE-bench 4.1.0. Fit and tool-call health were observed on Lightning; the
  Lambda-target equivalence remains empirical.
- The control command and thin command carry identical model/tool payloads;
  thin mode only observes the running process.
- Native vLLM metrics remain server-aggregate. They are not assigned to a
  model request without a later calibration/correlation record.
- A first paid session stops after gold smokes, one uninstrumented Lite
  trajectory, official generated-patch evaluation, export/checksum, and
  termination. Thin telemetry is a separately authorized later session.
- The original H100 control and gold-smoke outcomes are measured artifacts,
  but the original control's empty patch was traced to an omitted
  `repo_name` compatibility field. Fixed follow-up Lite/Verified evaluations
  are recorded separately in `project/PAID_SESSION_MEASUREMENTS.json`; two
  Lite runs resolved but included scratch/debug files, while the fixed Lite
  gold and Verified runs were unresolved. No Lambda-host result, resolved-rate
  claim, thin-overhead result, sweep result, latency claim, or simulator result
  is claimed by this document.
