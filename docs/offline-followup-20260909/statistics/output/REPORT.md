# Bounded uncertainty analysis for fixed CPU OOF predictions

This follow-up pairs the completed coarse and class-hybrid OOF files by exact `event_id`, `run_id`, `instance_id`, and `fold`. The common training manifest matches all 23,245 paired events and 545 observed instances.

The analysis holds the existing predictions fixed. It uses 5,000 paired nonparametric bootstrap replicates, sampling instances with replacement (seed 20260909); every event and run belonging to a sampled instance is carried together. Percentile 95% intervals describe the hybrid-minus-coarse coverage difference conditional on these OOF predictions.

It is **not** a bootstrap of the full nested fit or selection procedure, and it is **not** a blind holdout estimate: the hybrid was an adaptive bounded sixth diagnostic. These intervals therefore do not repair that selection limitation.

| Measure | Coarse | Hybrid | Hybrid − coarse | 95% CI for delta |
|---|---:|---:|---:|---:|
| Event-weighted within-25% coverage | 67.7436% | 68.5051% | +0.7615 pp | [0.4462, 1.1288] pp |
| Instance-weighted within-25% coverage | 68.5605% | 69.1251% | +0.5645 pp | [0.3713, 0.7639] pp |
| Strict all-events instance gate | 0.0000% | 0.0000% | +0.0000 pp | [0.0000, 0.0000] pp |

There are 545 instance clusters: 109 improved, 39 worsened, and 397 tied by each instance's event coverage. The strict all-events gate is reported separately and remains a conjunction across every event in an instance; it is not replaced by the average coverage metric. This 0/545 strict result is only for the retained CPU-event population, not a literal all-required-PDF gate.

Worst APE is an observed maximum only: coarse 481.081% and hybrid 481.081%. No bootstrap interval here claims that tails are bounded.

`coverage_ci.csv` contains the overall and original-class CIs. `fold_deltas.csv` contains event- and instance-weighted deltas for every fixed outer fold and original class.
