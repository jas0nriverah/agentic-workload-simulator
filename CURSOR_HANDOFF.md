# Cursor handoff — EIC assignment completion

Read this file together with `Coding tests Harrdware.pdf`,
`docs/assignment_traceability.md`, and `docs/REPORT_TEMPLATE.md`. The PDF is
the source of truth. This handoff describes the current repository state; it
does not authorize a new paid session by itself.

## Current state

- Branch: `parallel-h100-shards`
- Commit: `2e2969929a9885119a26ae4098f730fd7cc51fe6`
- PR: https://github.com/jas0nriverah/agentic-workload-simulator/pull/8
- Local tests: 110 passed; shell syntax and `git diff --check` passed.
- Infrastructure/readiness is substantially complete. The empirical assignment
  is only partially complete.
- Measured provider is Lightning, one H100 80 GB per Studio. Do not describe
  these as Lambda measurements.
- Repository state intentionally records `paid_session_authorized: false` and
  `safe_to_launch: false` until the user creates a current authorization file.

## Measured evidence

The authoritative measured summary is
`project/PAID_SESSION_MEASUREMENTS.json`; the run ledger is
`project/EXPERIMENT_LEDGER.jsonl`.

Runtime used Qwen/Qwen3-Coder-30B-A3B-Instruct BF16, vLLM 0.10.0, pinned
SWE-agent/SWE-bench revisions, and official evaluator return code 0 on the
recorded runs. Trajectory normalization and artifact validation passed.

Observed task outcomes:

- Lite `astropy__astropy-12907`: officially resolved twice, but both generated
  patches contained scratch/debug files. These are not clean-submission or
  benchmark resolved-rate claims.
- Lite `astropy__astropy-14182`: non-empty patch, unresolved.
- Verified `astropy__astropy-14365`: non-empty patch, unresolved.
- Earlier empty-patch runs were diagnostic runs made before the repository-name
  fix and must not be mixed into model-quality analysis.

Do not invent a resolved rate, average latency, CPU/GPU ratio, sweep effect,
or simulator accuracy from these results.

## Important execution clarification

The recorded Lightning work was real paid H100 execution, not merely setup or
smoke validation. Eleven workload attempts launched SWE-agent, produced real
trajectories/patches, and ran the official evaluator successfully. The reason
the assignment is incomplete is evidence quality and coverage, not absence of
execution:

- Two Lite `astropy__astropy-12907` attempts officially resolved, but their
  patches contained debug/scratch files and therefore are not clean-submission
  benchmark evidence.
- Lite `astropy__astropy-14182` and Verified `astropy__astropy-14365` produced
  non-empty patches but remained unresolved.
- Earlier attempts were also real runs, but are diagnostic because a missing
  repository-path parameter caused empty patches.
- The next phase must therefore prioritize clean generated patches, broader
  Lite/Verified coverage, and the required sweeps—not repeat setup-only checks.

Do not describe the existing measured runs as “only smoke tests.” Do describe
their limitations precisely: one-instance/low-coverage evidence, scratch-file
contamination in the resolved patches, and no valid aggregate assignment
statistics yet.

## Remaining assignment work, in order

1. Reproduce clean generated patches. Remove/avoid debug and scratch files in
   the SWE-agent workspace, while preserving the raw prior artifacts.
2. Run the required Lite and Verified baseline sample and compute the official
   evaluator resolved rate plus comparable end-to-end latency.
3. Run all four frozen sweeps, recording every cell, seed, command/config hash,
   evaluator result, and failure:
   - `agent.model.per_instance_call_limit`: 10, 20, 30, 50
   - `completion_kwargs.max_tokens`: 512, 1024, 2048, 4096
   - `agent.templates.max_observation_length`: 10000, 25000, 50000, 100000
   - `agent.model.temperature`: 0.0, 0.2, 0.5, 0.8
4. Capture valid event-level CPU/GPU timings and produce the three Step 1
   figures, four per-sweep figures, combined figure, and observations.
5. Select the high CPU:GPU-ratio case study and keep evaluator time separate
   from trajectory time.
6. Fit and hold out-test the hardware-parameterized simulator; report whether
   per-event and end-to-end errors are within 25%.
7. Fill `docs/REPORT_TEMPLATE.md` only with immutable evidence links/hashes,
   render the figures, visually inspect them, and update traceability/state.

## Execution rules

- Preserve assignment methodology and the pinned model/runtime. Do not adopt
  Hermes, LangGraph, SWE-bench Pro, or sample-repository claims.
- Use the official SWE-bench evaluator only.
- Keep control and thin telemetry prompts/tool payloads identical. Do not call
  telemetry overhead causal without paired timing boundaries and correlation.
- Preserve raw `.traj`, logs, evaluator reports, manifests, and hashes
  byte-for-byte. Never rewrite a failed run to make it pass.
- Before any new billed work, verify a fresh user-filled
  `cloud/lightning/cloud_session.yaml` and run:

  ```bash
  scripts/cloud/lambda_session_gate.sh \
    --session cloud/lightning/cloud_session.yaml --gate G3A
  ```

  Stop if the gate, preflight, bootstrap, vLLM health, or evaluator fails.
- Keep the Studio stopped when idle and terminate it after exporting artifacts.
- Do not commit provider-local authorization files, tokens, rendered manifests,
  caches, or generated environment directories.
- Commit and push only source code, documentation, manifests of measured
  evidence, and reproducible report inputs. Do not merge PR #8 automatically.

## Useful commands

```bash
cd agentic-workload-simulator
git switch parallel-h100-shards
git pull --ff-only origin parallel-h100-shards
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 scripts/validation/normalize_sweagent_trajectory.py --help
sed -n '1,220p' docs/assignment_traceability.md
sed -n '1,240p' docs/REPORT_TEMPLATE.md
```

At the end, report three separate statuses: infrastructure readiness, paid
authorization, and empirical assignment completion. A successful evaluator
run is evidence of one run, not evidence that the full assignment is done.
