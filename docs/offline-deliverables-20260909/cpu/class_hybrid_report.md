# Class-specific hybrid CPU diagnostic

After the five fixed candidate diagnostics, this is one bounded sixth
composite procedure permitted by the offline protocol. It leaves those fixed
diagnostics and the global selected procedure unchanged. For each original
class, it selects one of the five base candidates using only three inner
instance folds, maximizing within-25%
coverage subject to that class's coarse-median worst APE, then lower worst APE
and simpler candidate. The five outer folds score the resulting per-class
choice without using outer labels for selection.

The final all-training inner map is:

| Original class | Base candidate |
|---|---|
| patch | coverage_center |
| read | coverage_center |
| search | coarse_class_median |
| shell | coarse_class_median |
| test | coarse_class_median |
| traversal | coarse_class_median |
| write | coarse_class_median |

Across the five outer folds, the hybrid reaches 68.5051% event coverage within
25%, versus 67.7436% for the global coarse comparator. Its worst APE remains
481.08%, p95 APE falls to 136.33% from 153.07%, mean APE falls to 36.37% from
37.35%, and CPU absolute error falls from 9,778,654.906 ms to 9,738,676.553
ms. The gain is concentrated in patch (91.3850% within 25%) and read
(95.3627%); shell, test and traversal retain the coarse route because their
inner tail constraint rejects the higher-coverage alternatives.

The hybrid CPU sums are 6,984,260.409 ms predicted versus 14,636,931.413 ms
observed. The all-event pass rate is 0/545 instance trajectories (0.0000) and
4/819 run trajectories (0.0049); these CPU-only trajectory figures do not
stand in for a full E2E gate.

The hybrid remains an offline candidate package only. All 23,245 retained
events and 545 observed instances stay in the denominator, unsupported rows
remain visible, and no E2E or cross-hardware claim is inferred. Detailed fold
and per-class selection metrics are in `class_hybrid_comparison.json`; the
predictive class map and fitted base models are in `class_hybrid_model.json`.
