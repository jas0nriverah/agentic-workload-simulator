# Bounded offline improvements

This directory adds paired uncertainty estimates, validation of retained execution ledgers, separate repaired-event calibration paths, and reproducible table/figure regeneration. All implementation here is offline; acquisition and production configuration are unchanged.

Run from the repository root, using a **new** output directory:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 docs/offline-followup-20260909/run_pipeline.py --output-dir /tmp/offline-followup-results
```

The default plan inventories retained case-spec identities and validates `combined-case-v8/attempt-001` for descriptive reconstruction. The pinned partition gate excludes confirmation instances from fitting and prohibits final-evaluation journal access. A future explicit `--manifest plan.json` accepts `inventory_case_roots` and `cases`, each with `case_root`, optional `attempt_id`, and `purpose` (`calibration`, or explicitly `descriptive_confirmation`). Inventory alone does not select among configurations or retries. List eligible training cases explicitly when they arrive. Multiple attempts require an accepted-attempt binding or explicit selection.

The pipeline stops on invalid journals or calibration contracts and leaves diagnostics. Outputs from a failed invocation are not accepted. Successful output includes source-bound normalized ledgers, calibration status/model artifacts, uncertainty tables, historical D1–D8 inputs and four SVG/PNG figures, current-case semantic/runtime/native boundary tables and an SVG, plus SHA-256 manifests. Current-case evaluator scores are omitted unless patch/evaluator binding is proven. Observed native service durations are not GPU kernel timings; overlapping boundaries are never added into a claimed E2E decomposition.

The CPU improvement is supported conditionally on retained out-of-fold predictions: event-weighted within-25% coverage rises from 67.7436% to 68.5051% (+0.7615 percentage points, paired 95% instance-bootstrap interval +0.4462 to +1.1288). Equal-instance weighting gives +0.5645 points (+0.3713 to +0.7639). There are 23,245 events, 819 runs and 545 independent instances. Bootstrap replicates hold predictions fixed; they do not repeat adaptive model selection or establish blind-test accuracy. Worst observed CPU error remains 481.08%; no instance passes the strict all-retained-CPU-events gate.

Current metadata identifies 28 repaired case specs across three instances, all in the excluded confirmation partition. Therefore actual repaired-event fitting is pending independent training evidence. Synthetic fitting tests demonstrate software behavior only. Unknown hardware bindings and incomplete E2E composition remain unsupported; there is no asserted cross-hardware scaling law or D9 compliance.

This validator's accepted reconstruction scope is completed cases with a native record for each physical model request. A failed request without native evidence is rejected for this reconstruction path; that does not by itself establish an acquisition defect. CPU binary integrity is checked against retained action ranges and loss counters. This command does not expand the entire binary stream into individual CPU-operation modeling targets. Native phase-sum prediction is reported separately from outer agent E2E, which remains unproven.

Test the bounded adapters with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/offline-followup-20260909 -p 'test_pipeline.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/offline-followup-20260909/ledger/tests
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/offline-followup-20260909/calibration -p 'test_*.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s docs/offline-followup-20260909/statistics -p 'test_*.py'
```
