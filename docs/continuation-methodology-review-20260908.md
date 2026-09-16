# Continuation review against prior runs and the assignment

Authority: `/home/riverahernandezjason/Coding tests Harrdware (2).pdf`, SHA-256
`02f485cdd8cca4aae242d2f1a5306184a95d7ee0fae42527d7153f48a3424761`.
This is an additive review, not a replacement for the preserved v3 model or reports.

The lead reviewed the messages in these main Codex transcripts, including the
user corrections and final decisions (helper sessions were not treated as main chats):

- `2026/09/07/rollout-2026-09-07T17-32-23-01a07ced-a20f-77c3-b6b0-66f8bd6256a5.jsonl`: reliability and raw-data recovery, notably lines 79, 218, 1239, 2399, and 2469.
- `2026/09/08/rollout-2026-09-08T03-58-42-01a07f2b-09dc-74b0-8bd9-69e9824cdfa1.jsonl`: complete CPU model review; lines 85, 156, 202, 313, 726, 1049, and 1433.
- `2026/09/08/rollout-2026-09-08T05-22-07-01a07f77-6ad3-7d60-a7f2-eaa009ff4835.jsonl`: recovery and D1–D9 audit/repair decisions; lines 341, 549, 633, 950, 1187, and 1355.
- `2026/09/08/rollout-2026-09-08T13-21-11-01a0812e-032d-76a2-9017-83046efeec13.jsonl`: the immediately preceding implementation and full-run authorization, reviewed during initial continuation recovery.

Paths above are relative to `/home/riverahernandezjason/.codex/sessions/`.
Separate Cursor/log analysis is recorded in `cursor-run-failure-review-20260908.md`
when that bounded review completes.

## Findings verified from preserved prediction records

Source: `assignment/submission/20260908T060000Z/d9-cpu-review/predictions_instance_id_grouped_semantic_repo_median.json`
under `/home/riverahernandezjason/h100-assignment-work-20260905/`.
Only its retained calibration run IDs were joined to the older calibration cache;
`sympy__sympy-12481` remains excluded from this analysis.

| Observation | Verified value | Required response |
| --- | ---: | --- |
| CPU events outside 25% | 6,999 / 30,711 | Preserve the improved semantic model as a baseline; capture actual work and script state instead of fitting exact command identities. |
| Failed CPU events classified as shell | 3,932 | Preserve executable, execution mode, pipelines, state, and individual operations. |
| GPU request events outside 25% | 1,505 / 31,541 | Separate request/transport/queue/serving evidence; do not equate proxy latency to GPU kernel time. |
| E2E mass outside observed CPU+GPU sums | 55.253697691% | Measure lifecycle phases and interval complements; an arbitrary E2E multiplier must not absorb the missing events. |
| Unpiped `git log/show/diff` actions over 20 s | 112 / 112 selected matching actions | Confirm pager behavior and test an explicit noninteractive environment in the new configuration. This count alone does not prove the cause of every timeout. |

Raw corroboration: the accepted attempt for `pytest-dev__pytest-10081` is
`full-matrix-recovery-authenticated-resume-20260906T025908Z-16/worker-03/cases/00055/runner_attempts/attempt-002/pytest-dev__pytest-10081/pytest-dev__pytest-10081.traj`.
Action 10 runs pytest with `--pdb` and its observation reports the 30-second
timeout. Action 11 uses a five-second bound and shows an actual `(Pdb)` prompt.
That task concerns debugger behavior: banning `--pdb` would damage legitimate
testing. Any generic agent guidance must preserve necessary test modes and use
bounded input/timeout handling, rather than rewriting actions silently.

## Decisions before the new run

Keep the exact Qwen checkpoint and tokenizer. Use the existing four-candidate,
24-instance confirmation design to select configuration before production;
do not infer an accuracy gain from the old 25,000-observation subset alone.
Generic environment corrections must be explicit, hash-bound, and identical
across confirmation candidates and production, with historical results kept
under their original configuration.

Keep the frozen semantic-median v3 artifact unchanged. New CPU measurements
must retain full per-operation records, descriptor availability, script
prestate, process lineage, and truthful censored operations. Prospective
features cannot include current-event durations or future state. Assignment
workload replay and prospective forecasting remain distinct protocols.

The PDF requires every individual event and E2E error to stay within 25%.
Improved average calibration, a passing reliability canary, or a successful
capture smoke is not that accuracy result. Model revisions should be selected
with grouped calibration, preserve the quarantined holdout/development split,
and report direct event sums plus separately modeled lifecycle costs.
Cross-hardware coefficients require explicit evidence or labeled assumptions;
thread count alone is not a universal CPU speedup factor.

The collector's 100-action native and privilege-separated capture checks pass,
but diagnostic microbenchmarks have not established acceptable full-production
overhead. The final source, live pilot, fixed-work replay, recovery, resource,
and disk proofs remain required before the 1,088-case launch. The user's latest
resource update is 18 GPUs ready and two being prepared; actual worker count
must follow verified GPU allocations and CPU/memory isolation at launch.
