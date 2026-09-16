# Remaining D9 opportunity scan — September 14

No new inference, acquisition changes or protected evaluation access. The bounded
screen uses the same 43 valid CPU cases / 22 instance groups and original five
instance folds. Baseline metrics reproduce the saved CPU/lifecycle artifact.
Case/split gates, raw lifecycle hashes, target/start identities and host/clock
joins are checked before deriving features. Each fitted stratum requires 25
events from three training instances; otherwise it backs off to the training
median. No timing, outcome, exit status or instance ID is a predictor.

| Priority | Screened opportunity | Within 25% before → after | Worst error before → after |
|---|---|---:|---:|
| 1 | Runtime commands: restored start-time semantic class, operation and executable; 2,730 events | 78.94% → **87.77%** | 99.2722% → 99.2945% |
| 2 | Client processing: preceding recorded lifecycle event kind; 7,128 events | 23.64% → **25.95%** | 372.37% → **338.39%** |

Runtime equal-instance coverage improves 80.63% → 87.62%. The worst-error
regression is small but real; do not claim a uniform improvement or silently
apply a no-tail-regression acceptance rule. This is the highest-value next
candidate for serving parity, class-slice review and uncertainty checks.

Client equal-instance coverage improves 24.87% → 27.68%. The previous kind is
recorded before the client interval starts in the same clock domain, rather
than inferred from the interval's duration or eventual next action. It is a
sequential-state input: a simulator must reproduce that state or receive it in
the workload description. It is not evidence that a context-free client model
can achieve those metrics. Both candidates still have zero instances with all
their events within 25%.

Neither candidate was promoted or fitted into the serving artifact. The scan
retains metrics and category counts in `remaining_scan_20260914.json`; rerun
`python3 docs/d9-salvage-20260910/scan_remaining_20260914.py` to reproduce.

Further source inspection confirms the broad atomic CPU experiment does not
use retained mmap protection/flags or pread/pwrite offsets. Openat flags were
already tested separately and should not be rediscovered. The remaining scalar
fields are untested hypotheses, not measured gains; first establish event
counts and support before fitting. Queue timing is still poorly explained by
token/cache descriptors; another generic token-regression sweep is not justified.

The concrete new priorities are therefore runtime-command descriptors, then
client-state stratification. Do not repeat the rejected script-syntax,
cache-hit/miss or first-request GPU experiments.
