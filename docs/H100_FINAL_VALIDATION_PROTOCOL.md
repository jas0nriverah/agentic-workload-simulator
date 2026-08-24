# Final H100 validation protocol

Status: sealed scaffold, not launched (2026-08-24 UTC).

The machine-readable source of truth is
[`configs/h100_final_validation.json`](../configs/h100_final_validation.json).
This protocol is intentionally H100-only. It describes an experiment that may
be run on a fresh, dedicated H100 host after an operator supplies current
authorization. Merely checking in this file or running the entrypoint with
`--dry-run` does not start a VM, allocate a GPU, start vLLM, or spend money.

## Claim and workload boundary

The experiment estimates a fixed feature-to-latency model for the frozen
Qwen/vLLM serving workload. Its claim is limited to the declared H100 hardware,
software pins, serialized request protocol, and the declared token range. The
single-GPU requirement is strict: an allowlisted H100 80 GB device must be the
only visible GPU and no unrelated GPU process may be present. A failed,
contaminated, or incomplete row is retained as `unavailable` with a reason; it
is never silently removed.

The existing H100 evidence remains a separate historical anchor. In
particular, Kineto CUDA interval union, CPU-operation union, sampled NVML
utilization, process attribution, and kernel-duration sums are different
measurements. A kernel-duration sum is not elapsed GPU time, and a CPU-op union
is not all host/tool CPU time. Nsight Compute is supplementary and has no gate
in this protocol.

## Frozen software and controls

The model revision, BF16 precision, 32,768-token context, vLLM 0.10.0 image
digest, `qwen3_coder` parser, tensor parallelism of one, SWE-agent revision,
SWE-bench revision, and agent defaults are copied into the JSON config. They
must be recorded in the run manifest and must match exactly. Requests use one
serialized stream, two warmups, three measured repeats (`r01`, `r02`, `r03`),
temperature 0, seed 0, a fixed prompt/tokenizer, and one-second spacing. The
warmups are outside every denominator.

The preflight must record GPU UUID/SKU/PCI bus, memory, driver/CUDA, power and
clock state, host/boot/kernel identity, and monotonic clock metadata. The
server must pass health, model, and metrics checks before measurement. Request
boundaries, response hashes, actual token usage, CPU-operation intervals,
overlap-aware CUDA intervals, and raw trace references are retained per row.

## Cases and sealed split

There are 24 calibration configurations: the Cartesian grid of input targets
`[128, 512, 1024, 2048, 4096, 8192]` and output targets `[32, 64, 128, 256]`.
Each has three measured repeats after two warmups. These rows alone are
available to fit the model.

There are 12 sealed holdouts, also with three repeats: eight interpolation
cases (`256/48`, `768/96`, `1536/96`, `3072/160`, `6144/192`, `2560/160`,
`5120/96`, `1152/192`) and four extrapolation cases (`12288/64`, `8192/512`,
`16384/256`, `16384/512`), where each pair is input/output token target. The
split manifest is hashed before any run. The raw holdout labels remain sealed
until a prediction artifact has been written, checksummed, and timestamped.

## Features, labels, and fit

The allowed pre-request feature vector is explicit and deterministic:

| Feature | Definition | Fit use |
| --- | --- | --- |
| `prompt_tokens` | Declared input token target | yes |
| `max_output_tokens` | Declared maximum output budget, not actual completion length | yes |
| `context_tokens` | Declared pre-request context size; zero when not separately specified | yes |
| `tool_calls` | Declared tool-call count before execution | yes |
| `hardware_score` | Predeclared relative hardware normalization; 1.0 on calibration H100 | yes |
| `prompt_output_interaction` | Prompt/maximum-output token-million interaction normalized by hardware score | yes |
| `concurrency` | Protocol concurrency, fixed at 1 | constant metadata |
| `warm_state` | Measured rows follow warmup, fixed at `warm` | constant metadata |
| `holdout_kind` | Calibration/interpolation/extrapolation annotation | evaluation only |

`wall_ms` is the primary measured label. CPU-operation union, CUDA-activity
union, kernel-duration sum, and actual prompt/completion usage are retained as
secondary labels/evidence and are never input features. The fixed model family
is `h100_feature_latency_v1`: a regularized nonnegative additive model with a
declared prompt/output interaction, fit to the median successful calibration
repeat per case. `concurrency` and `warm_state` are fixed protocol metadata,
not varying regression columns.
The formula and regularization are selected before sealing and cannot be tuned
from holdouts.

## Leakage and scoring rules

The fitting process must assert disjoint case IDs and a fit-input hash that
contains calibration rows only. It must write predictions and a
`prediction_manifest.json` before it can read or join holdout labels. The reveal
receipt records the split hash, prediction hash, UTC time, and operator. The
holdout join must prove prediction time precedes reveal time.

The primary score is case-median wall-clock MAPE over the 12 holdouts. Also
report MAE, RMSE, repeat-level MAPE, interpolation MAPE, extrapolation MAPE,
p95 absolute percentage error, coverage, and every case/repeat denominator.
The protocol thresholds are 100% declared coverage, primary and interpolation
MAPE at most 25%, and extrapolation MAPE at most 35%. These are acceptance
criteria for this protocol, not a claim that the result will meet them.

Do not use holdout timings, response bodies, actual holdout token counts,
post-fit labels, repeat success/outcome, manual case-specific adjustments, or
any hidden server aggregate as a fit feature. Do not claim broad SWE-agent,
population, energy, cost, or individual-event accuracy from this matrix.

## Artifact and resume contract

The entrypoint writes under `artifacts/h100_final_validation/`. It copies and
hashes the protocol, writes a split manifest and run-state file, and stores each
case/repeat in its own directory. A completed case is never overwritten. A
resume may continue only when the protocol hash, split hash, hardware identity,
and output root agree; otherwise it stops for review. Raw traces and model
weights remain external to Git. Commit compact manifests, normalized rows,
derived predictions/metrics, and checksums only when repository policy permits.

Use the entrypoint as follows on an authorized host:

```bash
scripts/cloud/run_h100_final_validation.sh --dry-run
scripts/cloud/run_h100_final_validation.sh --phase calibration --execute --allow-h100 \
  --runner /absolute/path/to/reviewed_h100_case_runner

# Fit calibration rows offline and persist prediction_manifest.json, then:
scripts/cloud/run_h100_final_validation.sh --phase holdout --execute --allow-h100 \
  --predictions-manifest artifacts/h100_final_validation/derived/prediction_manifest.json \
  --runner /absolute/path/to/reviewed_h100_case_runner --resume
```

The runner is an explicit executable interface, not an interpolated shell
string. It receives `--config`, `--case-id`, `--split`, `--input-tokens`,
`--output-tokens`, `--repeat-id`, and `--output-dir`; it must produce an
immutable, checksummed row manifest. The entrypoint refuses non-H100 hardware,
missing authorization flags, an absent runner, output collisions, changed
config hashes, missing prediction manifests for holdouts, and incomplete
preflight evidence. Use the dry run to inspect the resolved plan before any
paid execution.
