# Current Qwen configuration opportunities — September 14

Bounded review of 49 repaired train-calibration cases / 25 instances, gated by
the pinned partition and case specifications before trajectory access. No
held-out outcomes, GPU inference, acquisition changes or configuration changes.
Reproduce with `python3 docs/d9-salvage-20260910/config_review_20260914.py`
(system Python has PyYAML). Counts and source hashes are retained in
`config_review_20260914.json`. This is a retained accepted cohort, not a random
whole-suite estimate or a claim about all failed infrastructure attempts.

## 1. Effective sampling

Executed client configurations explicitly set top_p=1 for all 49 cases;
temperature is 0 in 43, and 0.2/0.5/0.8 in two each. Completion kwargs set
the output budget and seed=0. Proxy records show no request mutation.

Counter-epoch-matched serving fingerprints and hash-verified startup logs
establish chat defaults top_k=20 and repetition_penalty=1.05 for 24 cases.
Those defaults are already present, not novel treatments. At temperature zero,
top-k/top-p do not introduce stochastic sampling. The corresponding defaults
for 25 cases are unavailable in the matched fingerprint locations used here;
do not fill them from another worker or an earlier server epoch.

The retained model generation_config also contains temperature=.7, top_p=.8,
top_k=20 and repetition_penalty=1.05. Its presence alone does not prove runtime
application; the verified startup records provide the stronger evidence.
The vLLM 0.10 documentation explains repository generation-config defaults:
https://docs.vllm.ai/en/v0.10.0/serving/openai_compatible_server.html

## 2. Recommended sampling combination

Qwen's model card recommends .7/.8/20/1.05:
https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct#best-practices

None of the 49 repaired training cases uses that combination. The retained
historical paired table varies temperature alone at .2/.5/.8; it does not
establish the effect of this combination. Its corresponding resolved counts
are 12/9/10 versus baseline 13 on 18 matched pairs / 17 instance clusters.
This argues against simply increasing temperature, but does not reject a
different joint sampling policy.

Disposition: retain as one optional H100 development comparison. Explicitly
set all four sampling parameters in both arms; hold the checkpoint, agent,
100-call budget, 61,440 input guard, 65,536 server context, 2,048 output cap,
25,000 observation cap and history handling fixed. Establish parameter
forwarding with the existing request/serving evidence before any inference.
Use eligible paired development instances and report stochastic variation;
do not tune on protected final cases. No experiment has been launched and no
score benefit is claimed. This is not a prerequisite for finishing D7/D8.

## 3. History and context

All 49 executed configurations have only `cache_control`, targeting the last
two user/tool messages. No last-N observation elision or summarization
processor is configured. An observation-length cap truncates individual
observations; it is not a rolling history-length cap.

Retained exit statuses: 44 submitted, four submitted(exit_cost), one
submitted(exit_format), zero exit_context. All four exit_cost records belong
to 20/30-call arms (21/31 recorded calls), not the current 100-call cap. Exit
status is not evaluator resolution. Four cases have a successful prompt count
at least 90% of the client guard; all four submitted. Server-reported prompt
counts and client estimates need not be equal, and successful-request counts
do not measure pre-dispatch failures.

Disposition: do not add history compression, raise context or increase the
100-call budget based on this sample. There is no demonstrated current
context-termination problem to fix. A future history treatment should require
measured repeated-observation/token overhead or context failures, then a paired
check for lost useful information. Public history-processor documentation:
https://swe-agent.com/1.0/reference/history_processor_config/

## Decision

Keep the current configuration. The only surviving new score hypothesis from
these three checks is the exact Qwen sampling combination, with unknown benefit.
H100 D7/D8 assembly and existing D9 integration remain useful offline work now.
A100 stays deferred. Do not reopen acquisition or repeat the old broad sweeps.
