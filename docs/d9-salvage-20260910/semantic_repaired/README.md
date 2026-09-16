# Repaired semantic CPU candidate — September 13

This completes the bounded command/script comparison proposed after rereading
both pages of `Coding tests Harrdware (2).pdf`. It improves the semantic-action
model used toward D7–D9; it is not a replacement for individual file-operation
models or a D9 acceptance claim. Frozen acquisition and original ledgers are unchanged.

## Implemented and measured

`compare.py` gates access using the pinned instance split and case specifications,
verifies normalized inputs and original tool/lifecycle journal hashes, and joins
targets to starts using case, attempt, event, timestamp, host, boot and clock.
Only known, pre-action, hash-verified script snapshots yield static syntax
descriptors. Missing/invalidated state stays unknown. No script is executed;
identifiers, literals, script hashes, measured durations and outcomes are not
script model keys. Original semantic-action labels and folds are preserved.

The 43 training cases contain 1,780 semantic actions across 22 instances. The
baseline reproduces the prior 53.5393% coverage exactly.

| Candidate | Within 25% | Equal-instance coverage | Worst error |
|---|---:|---:|---:|
| Existing coarse repaired model | 53.54% | 56.23% | 2,974.57% |
| Existing semantic model refit with restored commands | **57.75%** | **60.51%** | **2,779.45%** |
| Small static script-syntax extension | 57.47% | 60.49% | 2,779.45% |

Select the command semantic model as a **development candidate**. Its paired
instance-bootstrap coverage gain is +4.21 percentage points, conditional 95%
interval +1.44 to +6.32 points. This bootstrap uses fixed development predictions
and does not account for adaptive selection. The script extension loses to the
command model and is not selected. No instance has every semantic event within
25%; the literal PDF gate remains unmet.

`fit_artifact.json` contains the full-training selected model, its source
provenance, reference CPU domain and development metrics. `predict.py` performs
raw-free inference with exactly `action`, `repository`, `operation_class`, and
`hardware_domain`. The operation class is the recorded pre-action class, not a
new parser's reconstruction: reclassification initially caused 146 mismatches;
preserving the declared class gives **0/1,780 fit/serve mismatches**. Different
CPU domains are rejected because no transfer law has been established.

Reproduce the development experiment from retained training evidence:

```sh
.venv/bin/python docs/d9-salvage-20260910/semantic_repaired/compare.py
```

Raw-free prediction:

```sh
.venv/bin/python docs/d9-salvage-20260910/semantic_repaired/predict.py request.json
```

## Before the A100

The PDF requires hardware-parametric event models, individual-event and E2E
accuracy, and generation of Steps 1–3 figures. This change establishes one
better reference-CPU candidate; it does not complete that integration. Existing
atomic CPU/native phase/lifecycle candidates still need a consistent simulator
path, and GPU hardware-sensitive predictions must be frozen before an A100
measurement can test them. CPU remains on its reference host when testing GPU
transfer alone. Do not pretend this CPU candidate transfers by changing a label.

The existing conditional E2E CLI was also repaired to reject extra/label fields,
unknown event classes, coerced fractional/string/boolean counts and impossible
cached-token totals. Existing valid request predictions remain unchanged.
