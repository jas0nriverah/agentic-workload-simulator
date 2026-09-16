# Retained work/state experiments — September 14

Completed the four requested follow-ups offline. No new inference, final-holdout
fitting, acquisition changes or default-model promotion. The results do not
satisfy D9's literal every-event within-25% criterion.

## Results and decisions

| Target / change | Population | Baseline within25 | Candidate within25 | Baseline → candidate worst error | Decision |
|---|---:|---:|---:|---:|---|
| Client / preceding response bytes, median | 7,128 events, 43 cases, 22 instances | 25.95% | 26.14% | 338.39% → 347.53% | Reject |
| Client / preceding request bytes, median | same | 25.95% | 26.75% | 338.39% → 399.77% | Retain diagnostic, no promotion |
| Client / preceding request bytes, coverage center | same | 29.70% | 30.25% | 245.93% → 549.62% | Reject replacement |
| Client / preceding response bytes, coverage center | same | 29.70% | 28.84% | 245.93% → 303.05% | Reject |
| openat / entry flags and generic path group | 916,968 events, 8 instances | 21.86% | 25.31% | 1,825.35% → 3,676.41% | Coverage/tail tradeoff, no promotion |
| mmap / protection, flags, offset bucket | 431,654 events, 8 instances | 20.46% | 18.24% | 247.10% → 254.68% | Reject |
| pread64 / offset bucket and page alignment | 54,073 events, 8 instances | 1.70% | 1.67% | 138.81% → 138.81% | Reject |
| Native queue / predicted-service backlog | 2,080 requests, 49 cases, 25 instances | 42.02% | 42.02% | 98.73% → 98.73% | No benefit on this cohort |

The larger CPU population differs from the earlier four-Astropy experiment;
these numbers must not be presented as a decline on the original population.
Equal-instance coverage and per-instance CPU results are in the JSON reports.
No tested model has an instance with every target event within25.

## Client work inputs

The client spans have no direct observation-size field. The offline adapter
joins the latest preceding complete model payload, requiring identical case,
attempt and clock, with the proxy completion no later than the client start.
It verifies the payload's retained byte count and SHA256. All 7,128 targets
stay in the denominator: 7,085 have a preceding request, 43 have none and use
an explicit unknown feature. Prior request size is only a proxy for history
size, not an assertion of exact current history. No future request content,
elapsed time, measured residual or cumulative process counters enter a model.

The median history-size gain is +0.80 percentage points (fixed-prediction
instance-bootstrap 95% interval +0.13 to +1.50 points), but worsens the tail.
The history-size coverage-center gain over the previous center is +0.55 points,
with interval -0.49 to +2.38 points, and more than doubles worst error.
Intervals exclude model-selection uncertainty. The extra center combinations
are exploratory follow-ups, not independent confirmatory evidence.

All candidates use the original five instance-grouped folds, fixed log2 byte
buckets and at least 25 training events from three training instances per
stratum, with descriptor backoff. Serialized tables and event-level predictions
are retained in `client_fits.json` and `client_predictions.jsonl`.

## Broader atomic CPU check

The first eight distinct fully valid training instances by queue ordinal
comprise four Astropy and four Django instances. Read 1,746,862,000 retained
raw bytes directly; verified full stream hashes, complete action ranges,
zero recorded drops, and every raw record's action-token/range membership.
Selected 1,402,695 operations with no censored/nonpositive targets in these
classes. There were no pwrite64 targets; no pwrite improvement is claimed.

The comparison leaves one whole instance out at a time. Fits use only entry
arguments, with support of 20 events from two training instances and baseline
backoff. The openat baseline uses coarse path class; mmap and pread64 use
requested-size buckets. This is not an all-CPU model or hardware-transfer test.
`cpu.json` retains per-instance scores, worst-event raw offsets/tokens/sequences,
and source hashes; `cpu_fits.json` retains fitted tables. Avoid a large decoded
export: the checked raw streams and deterministic script reconstruct the test.

## Queue mechanism check

All 2,080 native requests join their retained engine arrivals and archived
native journals. No overlapping external finished requests or active finished
peers were found at those arrivals. The median measured native queue duration
is only 0.04795 ms (25% tolerance about 12 microseconds), with p95 0.08935 ms
and maximum 3.34328 ms. This makes scheduler/dispatch variation a plausible
explanation; the experiment does not establish its exact causal source.

The backlog test fits service models on other instances, then advances queue
state using predicted prefill/decode service only. It produces no additional
backlog and exactly reproduces baseline queue predictions. It is a bounded
single-server hypothesis, not a faithful model of vLLM continuous batching.
Measured peer completion times are used only to inspect traffic boundaries,
never as prediction inputs. Finished journals alone do not prove absence of
unfinished external traffic. Recorded engine arrival times and token/cache
work are explicit supplied inputs: this is conditional replay, not a forecast
of an agent's future request schedule. No queue improvement is claimed.

## Runtime alternative preserved

`runtime_alternatives.json` retains both descriptor median and coverage-center
fits. The previously evaluated coverage tradeoff is 87.77% → 91.03%, with worst
error 99.294% → 99.325%. `runtime_candidate.py:predict` offers explicit opt-in
`candidate='descriptor_gate'`, while retaining median as the default. It reads
only fitted artifacts, checks the CPU domain, and accepts exactly class,
operation and executable. It rejects timing labels and unsupported domains.
No main simulator default was changed and this is not a new validation result.

## Reproduction and validation

From the repository root:

```sh
.venv/bin/python docs/d9-salvage-20260910/work_state_experiments.py client
.venv/bin/python docs/d9-salvage-20260910/work_state_experiments.py cpu
.venv/bin/python docs/d9-salvage-20260910/work_state_experiments.py queue
.venv/bin/python docs/d9-salvage-20260910/work_state_experiments/runtime_candidate.py
.venv/bin/python -m pytest -q -p no:cacheprovider tests/assignment/test_d9_work_state_experiments.py tests/assignment/test_d9_lifecycle_refinement.py tests/assignment/test_d9_repaired_integration.py
```

**20 focused tests passed.** Tests cover chronology/clock/attempt isolation,
independent-instance support, label-independent predictions, estimator backoff,
queue advancement and idle gaps, invalid queue inputs, runtime export parity,
hardware-domain rejection, and existing repaired simulator integration.

Implemented only the offline experiment script, runtime alternative interface,
tests and derived evidence. Keep the current selected models. These tests
narrow the useful hypotheses; they do not justify another broad search or
expensive run, and they do not establish that all possible models are exhausted.
