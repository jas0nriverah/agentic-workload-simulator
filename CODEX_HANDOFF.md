# Codex handoff — next H100 experiment

Updated: 2026-08-22  
Repository: `jas0nriverah/agentic-workload-simulator`  
Branch: `parallel-h100-shards`  
Frozen validated harness commit: `3fcb5f28d3a363483b097a2787c051ca9c5b4a1b`

## Purpose

This is the operational handoff for the next Codex session. It summarizes what
has already been measured, what remains incomplete, and the highest-value
sequence for the next H100 experiment. The assignment PDF is the source of
truth; also read:

- `project/ASSIGNMENT_LOCK.md`
- `docs/assignment_traceability.md`
- `docs/REPORT_TEMPLATE.md`
- `project/PROJECT_STATE.yaml`
- `project/EXPERIMENT_LEDGER.jsonl`

Do not treat this document as permission to change the assignment design or to
invent missing measurements.

## What is already complete

### Repository and reproducibility work

- The assignment configuration is frozen around Qwen3-Coder-30B-A3B
  Instruct BF16, vLLM 0.10.0, SWE-agent 1.1.0, SWE-bench 4.1.0, and the
  recorded dataset/model revisions.
- The repository includes pinned runtime and evaluator contracts, launch gates,
  artifact normalization, provenance manifests, SHA-256 recording, and
  official evaluator integration.
- The latest validated harness commit passed the final local test and shell
  checks. Re-run the checks before changing or launching from a new checkout.
- Raw trajectories, logs, evaluator reports, patches, and manifests are
  evidence. Preserve them byte-for-byte; never rewrite an unsuccessful run.

### Modal H100 measurements

The Modal work is real measured H100 work, not just infrastructure setup:

- Full-prompt Lite control: official evaluator resolved `1/1`; raw model output
  had scratch/debug contamination.
- Verified control: the original full-prompt attempt was unresolved.
- A final highly targeted Verified LLM control produced a clean one-file patch
  and resolved `1/1`; keep it labeled as a targeted control, not as a general
  population rate.
- Additional Lite instances were measured for `astropy__astropy-14182`,
  `astropy__astropy-14995`, `astropy__astropy-6938`, and
  `astropy__astropy-7746`. Raw model outcomes and provenance-preserving clean
  derived submissions are recorded separately.
- The one-instance Lite sweep covers all four frozen parameter endpoint sets:
  call limit, maximum completion tokens, observation length, and temperature.
  Dependency-free SVG figures were generated under `project/figures/`.
- Deep profiles include Lite and Verified samples plus a parallel Lite sample.
  They contain syscall-level file events and aggregate vLLM Prometheus
  snapshots/deltas.
- The Verified deep-profile sample resolved cleanly. The Lite deep-profile
  sample did not produce a clean resolved submission.

Important evidence limitation: the deep profiles provide aggregate vLLM
request/token counters, not lossless per-SWE-agent-request GPU attribution.
Therefore no valid population CPU:GPU ratio, causal telemetry-overhead claim,
or simulator holdout result exists yet.

### GCP preparation

The repository now contains a bounded GCP H100 path:

- `cloud/gcp/RUNBOOK.md`
- `cloud/gcp/create_h100_spot.sh`
- `cloud/gcp/preemption_shutdown.sh`
- `cloud/gcp/instance_manifest.env.example`
- `scripts/observability/request_proxy.py`

The creation script is safe by default and requires `--apply`. The shutdown
hook attempts to preserve artifacts and stop workloads after Spot preemption.
The request proxy records request IDs, timing boundaries, status, body hashes,
sizes, and returned token counts without recording prompts, responses, or API
keys.

The last verified GCP console state, before the user reported fixing it, was:

- H100 regional quota in `us-east4`: `0`
- User-created VM attempt: `instance-20260822-171939`
- Creation error: `GPUS-PER-GPU-FAMILY-per-project-region` exceeded, with
  `gpu_family=NVIDIA_H100`

The user now says the quota issue is fixed. Do not rely on the old browser
state: verify the current quota and VM status read-only before spending GPU
time. Do not submit or modify quota requests unless the user explicitly asks.

## What still needs additional work

These are the meaningful assignment gaps:

1. **Step 1 baseline:** obtain a broader, comparable Lite/Verified sample across
   at least six repositories, with clean artifact provenance and official
   evaluator results.
2. **Step 1 category analysis:** capture paired event boundaries sufficient to
   compute repository-category CPU:model-serving latency ratios and the three
   required figures.
3. **Step 2 population evidence:** the one-instance sweeps are useful but are
   not a population study. Record their exact limitations; only extend them if
   the baseline experiment leaves budget and time.
4. **Step 3 case study:** select a high-ratio trajectory only after request-level
   timing and model-serving correlation are valid. Keep evaluator time separate
   from trajectory time.
5. **Step 4 simulator:** fit the hardware-parameterized simulator from measured
   events, then run a real holdout and report per-event and end-to-end error.
   The <=25% requirement cannot be claimed from aggregate metrics.
6. **Final report:** reconcile manifests, hashes, dataset/evaluator revisions,
   figures, anomalies, and limitations in `docs/REPORT_TEMPLATE.md`. Complete
   secret/path scans and visual QA.
7. **Evaluator provenance:** capture immutable evaluator image digests rather
   than relying on a mutable `latest` tag.
8. **Documentation consistency:** some older handoffs and state files describe
   earlier Lightning or pre-GCP phases. Update them only with verified new
   evidence; do not erase historical results.

## Focus for the next experiment

The next session should prioritize evidence quality and request-level
observability, not more infrastructure.

### Gate 0 — verify the launch target

Before any paid work:

1. Confirm the current GCP project, region/zone, H100 regional quota, global GPU
   quota, billing cap, and Spot/on-demand provisioning choice.
2. Confirm the VM is actually `a3-highgpu-1g` with one H100 and that it is
   `RUNNING`, not merely present as a failed or provisioning row.
3. Verify persistent work/cache/result disks and enough free space.
4. Record the exact image, machine type, provisioning model, VM name, zone, and
   start timestamp in the run manifest.
5. Never put credentials, access tokens, or private authorization files in Git
   or chat.

### Gate 1 — one instrumented smoke trajectory

Run exactly one representative trajectory before the production queue. Use the
request proxy for the model endpoint and verify all of the following:

- vLLM health and model identity
- request ID, method/path, status, byte counts, hashes, and timestamps
- prompt/completion/total token fields when returned
- SWE-agent tool-call timing and exit status
- trajectory start/end and request-boundary monotonic timestamps
- end-to-end reconciliation between request, tool, agent, and evaluator times
- raw `.traj`, prediction/patch, logs, manifest, and evaluator-input persistence
- official evaluator input paths and immutable evaluator identity/digest

If any critical field is missing, malformed, duplicated, or impossible to
reconcile, stop the production queue. Fix or document the telemetry issue
before consuming more H100 time.

### Gate 2 — frozen production baseline

If the smoke gate passes, run the frozen diverse baseline serially on the H100:

- Target 30 Lite plus 30 Verified trajectories.
- Cover at least six repositories, not just Astropy.
- Checkpoint and export artifacts after every trajectory.
- Use per-task timeouts, limited retries, disk checks, vLLM health checks, and a
  progress manifest with resume support.
- A broken task must be skipped and recorded rather than stalling the queue.
- Run detached from the SSH/Cursor session so disconnects and Spot preemption do
  not destroy progress.
- Set an absolute shutdown deadline and preserve/export artifacts before
  termination, even if the runner fails.

Do not repeat completed Astropy sweeps or spend H100 time on plotting and
evaluator work that can be done locally.

### Gate 3 — calibration and selected deep profiles

After the baseline, while vLLM is still warm:

1. Run the planned calibration matrix that maps request-level observations to
   model-serving timing/counters.
2. Check that calibration records are internally consistent and versioned.
3. Run only selected deep profiles if the remaining budget supports them.
4. Use the resulting paired samples for category ratios and simulator
   calibration/holdout.

Do not change the frozen experimental design after the smoke gate without
recording the reason, affected manifests, and interpretation impact.

## Required final experiment report

When the session ends, report:

- successful and failed trajectory counts
- Lite/Verified split and repositories covered
- official resolved rate, with clean-vs-raw provenance clearly separated
- trajectory and evaluator latency distributions
- model-call counts and prompt/completion/total token statistics
- request-level CPU:model-serving timing results and reconciliation status
- calibration matrix and simulator holdout results, including errors
- deep-profile results and known attribution limits
- total H100 runtime and cost
- Spot/preemption, timeout, infrastructure, or artifact anomalies
- recommendation for the next experiment or for final submission

Every number must link to an immutable manifest or derived artifact. If a
measurement is unavailable, say `PENDING` or `NOT MEASURED`; do not estimate it
from aggregate counters.

## Useful validation commands

```bash
git status --short --branch
git rev-parse HEAD
git diff --check
bash -n cloud/gcp/create_h100_spot.sh cloud/gcp/preemption_shutdown.sh
PYTHONPATH=src:. python3 -m unittest discover -s tests
```

The next Codex session should begin with read-only GCP verification and the
single smoke trajectory. It should not launch the 60-trajectory queue until
the smoke telemetry and artifact gates pass.
