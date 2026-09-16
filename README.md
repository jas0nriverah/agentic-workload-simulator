# Agentic Workload Simulator

The assignment authority is **Coding tests Harrdware (2).pdf**. Both pages,
including the figure examples, were checked on September 14, 2026.
Start with the [current project status](docs/current/STATUS.md) and
[PDF contract](docs/current/PDF_CONTRACT.md).
Historical snapshots and dated reports are evidence, not current execution instructions.

## Current implementation

- Historical Lite/Verified accuracy, latency and sweep evidence is retained.
- Repaired CPU/lifecycle and native GPU models have offline development evaluations.
- The reference-platform prediction CLI is `docs/d9-salvage-20260910/simulator/run.py`.
- A new component-composition check uses the existing repaired evidence, with
  instance-grouped fitting, explicit inclusive event nesting and no measured-gap correction.
- **D9 accuracy and hardware transfer have not passed.** No document here should
  be read as a guarantee of evaluator performance or a statement of impossibility.

The assignment is interpreted as simulation of a supplied Step-3 workload on
specified hardware. Measured target latencies are labels, never prediction inputs.
This interpretation is distinct from the earlier prospective agent-forecast experiments.

## Offline reconstruction

Use the existing development environment and retained evidence paths:

```sh
.venv/bin/python scripts/assignment/reconstruct_repaired_composition.py
.venv/bin/python -m pytest -q -p no:cacheprovider tests/assignment/test_event_composition.py
```

The reconstruction writes `docs/current/composition/`. It does not launch a
workload, change collection, open protected final-evaluation cases, or apply
unvalidated hardware scaling. It is an integration diagnostic, not the complete
D9 submission. Its interval hierarchy is supplied trace topology; timestamps
and measured unaccounted time do not enter latency predictors.

## Repository layout

- `src/`, `scripts/`, `configs/`, `tests/`: implementation and validation.
- `docs/current/`: current contract and reconstruction results.
- `docs/d9-salvage-20260910/`: retained model experiments and fitted artifacts.
- `project/`, `results/`, `work/`: historical evidence and preservation records.
- `docs/history/`: superseded entry-point documentation.

Historical counts such as 602/486 and the repaired 55-case inventory describe
different dated datasets. They are not a live worker or GPU status report.
The PDF does not prescribe the project's 96- or 1,088-run designs or an A100 run.

Old setup/branch instructions are retained in
[the archived README](docs/history/README_before_pdf_reset_20260914.md) for
reproducibility; they are not a current launch plan. Acquisition remains unchanged.
