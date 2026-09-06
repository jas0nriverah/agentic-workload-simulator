# Repository repair verification — 2026-09-06

Current experiment state is maintained only in [project/CURRENT_STATE.json](../project/CURRENT_STATE.json).
This report describes software repairs and verification; it does not authorize execution.

## Preservation

Before any implementation change, the existing snapshot SHA256SUMS passed in full.
All 16 worker ledgers contain 68 cases each: 602 unique completed IDs and 486
unique failed IDs, 1,088 total, with no overlap and zero pending ledger records.
There are exactly 602 archived case_result.json files. The focused manifest
records every ID/status/ledger and every completed result's path, size and SHA-256.
It also preserves the existing 627-file snapshot checksum inventory and SHA256SUMS itself.
The verifier pins the focused manifest's digest and checks all 628 baseline files,
result-to-ledger hashes, case identities, worker counts, and the exact result-file set.
The original snapshot (including README and summary) has no modifications.

Run from the repository root:

```sh
python scripts/validation/verify_preservation.py
(cd project/h100_results/full_matrix_recovery_20260906 && sha256sum -c SHA256SUMS)
```

## Changes and protected invariants

- Pytest defaults to tests/ so historical SWE-bench output cannot be collected.
  A regression places invalid Python and doctest output in a temporary archive
  and confirms only the intended test is collected.
- CI explicitly runs pytest, Ruff, compile/import sanity, preservation and
  ShellCheck, while retaining the original pinned Linux rehearsal steps.
- CURRENT_STATE.json is the sole current-state authority. Historical handoff,
  recovery, supervisor and old state documents carry supersession notices.
- A100 proof v2 binds actual prediction, protocol and split-manifest bytes plus
  the protocol-derived split digest. Resume verifies the proof before state
  mutation or analysis; execution verifies again before holdout requests and
  reveal. Split validation compares actual ordered case-ID lists with protocol
  definitions. Prediction and protocol sidecars must match. Existing proofs
  are verified and never overwritten; legacy unbound proofs fail closed.
  A valid existing proof avoids refitting calibration during an all-phase resume.
- Search-only .ignore patterns separate raw evidence from ordinary code searches;
  README explains how to explicitly inspect archived evidence. Nothing is moved
  or deleted. Local editable-install metadata is ignored by Git.

## Verification

- Before: root collection failed on an archived SWE-bench test_output.txt;
  412 software tests were identified. Explicit tests/ execution initially had
  411 passes and one interpreter-environment failure. The shell renderer used
  system Python while pytest used a venv. Activating the venv resolved the
  existing renderer test without changing source or tests.
- After: root pytest passed 422 tests and 90 subtests, including 10 new regression
  tests. Focused A100/feature/preservation tests passed 36 tests and 6 subtests.
- Ruff passed for src/, scripts/ and tests/.
- Compile sanity passed for 160 Python files; all 31 library modules imported.
  Python 3.14 emits a pre-existing invalid-escape warning in a simulator docstring;
  it is not a compile or import failure and simulator code was left unchanged.
- ShellCheck passed for start.sh and all cloud/observability shell scripts.
- All 18 existing cloud rehearsal dry-runs passed in an isolated clean copy.
  The actual working tree correctly refuses the startup dry-run because it is
  dirty; this guard was retained. No commit was made in the working repository.
- The existing rehearsal static check passed in the isolated clean copy:
  unittest core plus tests/cloud discovery, compile (93 active source files),
  Ruff, ShellCheck, and clean-diff validation.
- After adding the final CI preservation check, the preservation and rehearsal
  regression subset passed 18 tests and 6 subtests.
- Full Ubuntu 22.04/Python 3.11 CI with external pinned SWE-agent/SWE-bench and
  registry checks was not executed on this macOS host. Those steps remain in CI.
- git diff --check passed. Final preservation verification passed.

## Comparability and deferrals

No workload, prompt, case input, model configuration, protocol definition,
measurement method, timing rule, evaluator, result record or simulator equation
changed. The new integrity checks affect authorization of stale/invalid runs,
not valid experiment measurements. No experiment cases were executed.

Trajectory-level stacked cross-validation/cross-fitting and clipped non-negative
regression remain intentionally deferred to a separate versioned experiment:
changing these now could alter predictions, coefficients or result interpretation.
Legacy A100 proofs require explicit recovery review; this repair does not re-seal
or upgrade historical evidence automatically. The original 486 failed cases
remain for later execution; the original 602 completed cases remain immutable.

## Files changed

- `.github/workflows/linux-rehearsal.yml`
- `.gitignore`
- `.ignore`
- `A100_FINAL_VM_HANDOFF.md`
- `CODEX_HANDOFF.md`
- `CURSOR_HANDOFF.md`
- `H100_FINAL_VM_HANDOFF.md`
- `LIGHTNING_CODEX_HANDOFF.md`
- `README.md`
- `RUN_DATA_ARCHIVE.md`
- `docs/REPOSITORY_REPAIR_20260906.md`
- `project/CURRENT_STATE.json`
- `project/HANDOFF.md`
- `project/INTERFACE_HANDOFF.md`
- `project/PROJECT_STATE.yaml`
- `project/README_STATE.md`
- `project/SUPERVISOR_STATUS_20260823.md`
- `project/preservation/full_matrix_20260906.json`
- `pyproject.toml`
- `scripts/analysis/feature_validation.py`
- `scripts/cloud/a100_execution.py`
- `scripts/validation/software_sanity.py`
- `scripts/validation/verify_preservation.py`
- `tests/test_repair_integrity.py`
