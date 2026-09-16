# SWE-bench score/configuration opportunity review — 2026-09-09

This is a bounded, report-only score review. It reuses the published historical 800-row baseline and 288-row settings sweep, derives the frozen identity boundary before eligibility decisions, and inspects one accepted newer configuration attempt. It does not run inference or an evaluator, fit a model, read hidden test patches, access GPU/network state, choose a configuration, or modify frozen implementation, telemetry, architecture, or evidence.

The scope gate derives `frozen_scope()` from the five pinned manifests first. The script then parses the saved 800-row classification CSV and aggregate termination metadata, checks identity eligibility before retaining or using any labels/outcomes, and opens no per-case raw result before that check. Parsing mixed summary metadata does not restore blindness: the prior forensic pass had already touched aggregate rows containing evaluation clusters. That disclosure remains part of the evidence boundary.

## Historical score signal

The eligible subset contains 647 original baseline rows: 244 officially resolved and 403 officially unresolved (37.7% resolved). The corrected patch taxonomy is:

| Primary class | Rows | Resolved |
| --- | ---: | ---: |
| Officially resolved | 244 | 244 |
| Evaluator completed, unresolved | 356 | 0 |
| Recovered proxy error | 36 | 0 |
| Empty patch | 8 | 0 |
| Empty patch with editor/tool-install failure | 3 | 0 |

Native termination is a more useful opportunity partition than treating every terminal row as a failure. Call-boundary exits are 179/511 resolved (35.0%), and context exits are 14/63 resolved (22.2%). `exit_error` is 0/10, `exit_format` is 1/1, and submitted rows without a subtype are 50/62. Thus extra calls or context may recover some cases, while the termination label alone cannot prove that either change caused a result. The Django-10097 original empty-patch row remains separate from its later coverage trajectory.

The filtered historical paired table has 216 rows: 18 pairs across 17 instance clusters for each of the 12 old settings. Every treatment has zero paired wins. Losses are 13 for call limit 10, 2 for call limit 20, 0 for call limit 50, 5/1/2 for output caps 512/1024/4096, 3/0/1 for observation caps 10000/25000/50000, and 1/4/3 for temperatures 0.2/0.5/0.8. Historical settings comparable to a current one-factor question are call limit, output cap, observation cap, and temperature. The old sweep has no clean client input-guard, pager, server/runtime, retry, loop-guidance, or lifecycle setup comparison. These rows screen harms and motivate the predeclared confirmation; they do not select a current winner after runtime changes.

The strongest retained recommendation is to keep the declared candidates and the fixed resolution rule. Keep output 2048 and temperature 0 in the current panel: smaller output, nonzero temperatures, and reduced calls supplied historical losses. Treat call limit 50 and observation length 25000 as zero-discordance historical screens only; 17 independent clusters and no observed discordance do not establish equivalence. Do not add a fifth prompt, loop-kill rule, or test-answer intervention from the diagnostics. Exact repeated actions occur in 197/860 eligible trajectories at least three times (458 later occurrences; 21 adjacent identical pairs), but official-resolution stratification is not established.

The eligible cross-tabs confirm what is available for score reasoning and what is only an association:

| Dimension | Eligible signal | Officially resolved | Reading limit |
| --- | ---: | ---: | --- |
| Editor/install flag | 168 | 63 | Flags co-occur; no causal attribution; flagged rows can resolve |
| Tree-sitter flag | 3 | 0 | Small, environment-specific slice |
| Python/pip flag | 14 | 6 | Association only |
| Broad function-calling/parser flags | 647 / 647 | 244 / 244 | These saved flags are universal and cannot diagnose malformed patches |
| Evaluator missing/corrupt/error | 0 | 0 | No saved evaluator-record defect in eligible rows |
| Proxy remote disconnect | 15 | 2 | Runtime metadata, not proof of task failure |
| Prompt maximum 0–8191 / 8192–16383 / 16384–32767 / 32768+ | 1 / 46 / 598 / 2 | 0 / 15 / 227 / 2 | Joined from identity-only termination metadata; not an intervention |
| Context-truncation flag | 114 | 45 | This overlapping flag is not the 63-row native `exit_context` class; native context exits can still resolve |
| Original retry lineage | 647 (`repeat_id=r0`) | 244 | No quality retry variation |

Loop counts and long-setup phase data are not jointly available with historical official outcomes. The accepted current attempt supplies one phase decomposition (startup 31170 ms, setup 327 ms), while historical baseline classifications expose total duration only. This prevents a defensible long-setup threshold or retry/loop score estimate.

No native deadline/timeout class appears in the eligible termination table, and no malformed-patch class is supported by these metadata. `recovered_proxy_error` is a 36-row primary classification, whereas the 15-row proxy-remote-disconnect signal is a separate nonexclusive runtime flag. Neither should be relabeled as a task cause.

## One accepted newer comparison

The current queue contains one accepted quality-bearing case. It is `expanded-call100-input61440` on `django__django-7530`, with official resolution. Its same-template eligible historical baseline is `assignment-case-v1:10cfac47...`, which is officially unresolved, has a zero-byte empty patch, and carries editor-install, tree-sitter, Python/pip, function-calling, and parser flags. The accepted candidate used 100 calls, a 61440 client input guard, observation 100000, output 2048, temperature 0, and serving max length 65536. The historical baseline used 30 calls, a 32768 guard, observation 100000, output 2048, and temperature 0.

| Accepted comparison | Official outcome | Physical requests | Native evidence | Outer wall | Unknown complement |
| --- | --- | ---: | ---: | ---: | ---: |
| `django__django-7530`, 100 calls / 61440 guard | resolved | 35 | 35 measured, 0 unavailable | 183267 ms | 7021 ms |
| Same-template historical baseline | unresolved | 0 captured model requests | empty patch; editor/tool-install failure | 36373 ms summary | unavailable |

The one accepted result is useful as a lineage and instrumentation check: it shows that this candidate can produce an officially resolved patch on an eligible identity with complete native request count for that attempt. It cannot rank the candidate. The outcome is confounded by the joint call and input-guard changes, newer source/runtime/evaluator state, and the baseline editor/tool-install failure. The candidate's startup phase was 31170 ms and setup phase 327 ms; no historical setup phase is available for an outcome comparison, so these are descriptive values rather than a long-setup effect.

The queue has 1 accepted, 10 blocked, and 85 pending cases. All 10 blocked attempts are `capture_integrity` failures from native serving archive acquisition; the recorded halt reason is SSH permission denied. They contribute no quality outcome and cannot be treated as unresolved SWE-bench cases. Original historical rows are `repeat_id=r0`; saved audit metadata reports the same 20 infrastructure retries, 10-second minimum wait, and 120-second maximum wait for 798 canonical rows. There is no retry variation from which to estimate score gain.

## What the 96-case comparison still resolves

The panel is 24 fixed identities × 4 declared candidates. Only one of the 96 cases currently has an accepted quality-bearing result; 95 remain unfinished (10 blocked and 85 pending). Once accepted attempts exist, the panel resolves paired official wins and losses under the same model, source, evaluator, and runtime pins:

| Contrast | Identifiable question | Remaining ambiguity |
| --- | --- | --- |
| 30 calls / 32768 guard vs 50 calls / 61440 guard | Joint expanded configuration effect | Calls cannot be separated from the input guard |
| 50 vs 100 calls, both 61440 guard and observation 100000 | Call-budget effect | Requires accepted outcomes on both sides |
| 100 calls / 100000 observation vs 100 calls / 25000 observation | Observation-cap effect | Requires accepted outcomes on both sides |

The first contrast is not a call-only test. The last two are the high-value dimensions the historical sweep did not identify. For each candidate, root should report paired resolved count, wins, losses, native termination classes, failures/context exits, budget exhaustion, and resource cost on the shared identities. The current one accepted case cannot fill those cells; the common archive failure prevents using the other 95 queue rows as quality evidence.

## Reproduction and limits

Run the bounded script from the repository root:

```text
python3 docs/retained-analysis-20260909/score/score_opportunities.py \
  --out-dir docs/retained-analysis-20260909/score
```

It writes `score_evidence.json` and `score_evidence.csv`. The JSON records scope counts, source hashes, eligible taxonomy, historical paired wins/losses, current queue status, accepted-attempt provenance, and the exact comparison contrasts. The CSV is a compact taxonomy and paired-summary table. Current queue hashes are snapshot values and may change if root resumes dispatch.

The historical classes and flags are associations. Environment flags can co-occur; evaluator/runtime flags describe saved metadata, not causal failure. Prompt growth, repeated actions, retries, and phase durations are not prospective features and were not fitted. Historical predictions were fitted before the broader exclusion and are diagnostic only. No final 800/288 or 1088 outcome is inferred from this report.
