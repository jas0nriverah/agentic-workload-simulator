# D9 next-sample plan

This is a metadata-only plan for the remaining D9 work. It does not launch runs, open raw binaries, change the queue, change archives, or read labels from protected partitions. The machine-readable contract is [sample_plan.json](sample_plan.json), and [validate_plan.py](validate_plan.py) checks its instance and partition references against the frozen split metadata.

The present repaired production evidence is enough to fit the same-H100 model now: 49 accepted training cases, 25 instances, 2,080 native requests, and 8,320 phase rows. The corrected scaled token model covers 1,973/2,080 native e2e requests within 25% (94.8558%); the cache-aware candidate covers 2,027/2,080 (97.4519%), but that candidate is conditional on a realized cache trace. The remaining same-domain weakness is concentrated in queue/prefill and warm/setup behavior. CPU semantic actions cover 953/1,780 events within 25%, and the historical Python/test and find-exec paths still lack declared work descriptors.

The smallest useful optional acquisition is **8 same-H100 train traces**: two complete repetitions each for:

- `django__django-11964`, the current native long-tail case;
- `django__django-11742`, the weakest current per-instance native case;
- `matplotlib__matplotlib-20488`, a pinned train instance from the historically weak repository;
- `pydata__xarray-7233`, a pinned train instance from the other historically weak repository.

These are explicitly **adaptive calibration cases**, chosen from current tails and historical repository weakness. They are not a representative validation sample and cannot support a generalization claim. Existing `astropy__astropy-6938` and `django__django-11815` already have 13 accepted traces each and remain repeat controls. Add at most one third repetition for each targeted ID, for a hard same-H100 cap of 12 new traces. A fallback list is recorded in the JSON only for a missing natural mechanism stratum; it must not be used to fabricate a case or tune to an outlier.

The four accepted `final_evaluation` metadata IDs remain untouched and are scored only after the fit is frozen: `django__django-11019`, `django__django-11848`, `django__django-11905`, and `django__django-12125`. Confirmation-development cases and the sealed `sympy__sympy-12481` cluster remain outside fitting and selection. The plan preserves the frozen split manifest (`0b0c3714…bcc99f`) and does not inspect protected outcomes.

Cross-hardware evidence is separate from same-host improvement. First, up to 8 paired traces (two each of the four adaptive train IDs) can measure a hardware shift on seen workloads; those traces are not untouched validation. A frozen transfer evaluation then needs at least one trace for each of the four protected final IDs (`django__django-11019`, `django__django-11848`, `django__django-11905`, `django__django-12125`) on the second **static GPU inventory domain**, with a preferred two each. Only that protected stage can support an untouched-instance cross-hardware statement. A new host, boot, or UUID with the same stable H100 inventory digest does not satisfy this requirement. Hardware availability is currently unknown, so no cross-hardware transfer claim is made. If no distinct domain can be verified, stop and report the limitation rather than substituting CPU or a second H100 boot.

The second-GPU stage tests GPU transfer only when the workload CPU host/configuration is held constant where possible. CPU hardware scaling remains unvalidated and must not be conflated with the remote server CPU or the workload CPU.

The current candidate contract proves prompt/completion tokens and an explicitly conditional cache trace. Request ordinal, warm/cold state, admission/concurrency, executable/module, test scope, and find work counts are not proven model inputs in this pass. Do not require a collector change for them: use a field only if the standard producer already emits it, otherwise leave that stratum unsupported. Observed timings, residuals, status, and outcomes remain excluded from features. Every accepted trace still needs exact physical request joins, finite phase targets, and bounded loss-free CPU source evidence.

Validate the metadata-only plan with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/d9-salvage-20260910/next_samples/validate_plan.py
```
