#!/usr/bin/env python3
"""Reconcile a sealed assignment plan against canonical measured trajectories.

This command is read-only with respect to its inputs and never executes a
workload.  A plan case is removed from the remaining plan only when exactly
one completed measured (or derived-from-measured) canonical trajectory has an
exact, conflict-free identity match.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.schema import TRAJECTORY_FIELDS  # noqa: E402


PLAN_SCHEMA = "assignment-steps-1-3-plan.v1"
RECONCILIATION_SCHEMA = "assignment-plan-reconciliation.v1"
REPORT_SCHEMA = "assignment-plan-reconciliation-report.v1"
ALLOWED_SUITES = {"lite", "verified"}
ELIGIBLE_PROVENANCE = {"measured", "derived_from_measured"}


class ReconciliationError(ValueError):
    """The plan, sidecar, table, or requested outputs are unsafe."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReconciliationError(message)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReconciliationError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _verify_plan_sidecar(plan: Path, sidecar: Path) -> str:
    digest = _file_sha256(plan)
    expected = f"{digest}  {plan.name}\n".encode("utf-8")
    try:
        actual = sidecar.read_bytes()
    except OSError as exc:
        raise ReconciliationError(f"cannot read plan sidecar {sidecar}: {exc}") from exc
    _require(actual == expected, "plan SHA-256 sidecar does not exactly match the plan bytes and filename")
    return digest


def _read_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReconciliationError(f"cannot read plan {path}: {exc}") from exc
    _require(len(lines) >= 2, "plan must contain one header and at least one case")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        _require(bool(line.strip()), f"blank plan line at {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReconciliationError(f"invalid plan JSON at line {line_number}: {exc}") from exc
        _require(isinstance(row, dict), f"plan line {line_number} is not an object")
        rows.append(row)

    header, cases = rows[0], rows[1:]
    _require(header.get("record_type") == "plan", "first plan record must be the header")
    _require(header.get("schema_version") == PLAN_SCHEMA, "unsupported plan schema_version")
    _require(header.get("planning_only") is True, "plan header must be planning_only")
    _require(header.get("concurrency") == 1, "plan header must enforce concurrency=1")
    _require(header.get("execution_case_count") == len(cases), "plan execution_case_count mismatch")
    plan_id = header.get("plan_id")
    _require(isinstance(plan_id, str) and bool(plan_id), "plan_id is required")

    resume_keys: set[str] = set()
    identities: set[tuple[str, str, str]] = set()
    baseline_settings: Mapping[str, Any] | None = None
    for line_number, case in enumerate(cases, 2):
        _require(case.get("record_type") == "case", f"invalid case record at line {line_number}")
        _require(case.get("schema_version") == PLAN_SCHEMA, f"invalid case schema at line {line_number}")
        _require(case.get("plan_id") == plan_id, f"case plan_id mismatch at line {line_number}")
        suite = case.get("suite")
        instance_id = case.get("instance_id")
        cell_id = case.get("cell_id")
        _require(suite in ALLOWED_SUITES, f"invalid case suite at line {line_number}")
        _require(isinstance(instance_id, str) and bool(instance_id), f"missing instance_id at line {line_number}")
        _require(isinstance(cell_id, str) and bool(cell_id), f"missing cell_id at line {line_number}")
        identity = (suite, instance_id, cell_id)
        _require(identity not in identities, f"duplicate plan case identity at line {line_number}")
        identities.add(identity)
        resume_key = case.get("resume_key")
        _require(
            isinstance(resume_key, str) and bool(resume_key) and resume_key not in resume_keys,
            f"missing or duplicate resume_key at line {line_number}",
        )
        resume_keys.add(resume_key)
        _require(case.get("concurrency") == 1, f"case does not enforce concurrency=1 at line {line_number}")
        _require(isinstance(case.get("settings"), dict), f"case settings must be an object at line {line_number}")
        variation = case.get("variation")
        if cell_id == "shared-baseline":
            _require(variation is None, f"shared-baseline variation must be null at line {line_number}")
            if baseline_settings is None:
                baseline_settings = case["settings"]
            _require(case["settings"] == baseline_settings, "shared-baseline settings conflict across plan cases")
        else:
            _require(
                isinstance(variation, dict) and set(variation) == {"knob", "value"},
                f"sweep variation must contain exactly knob/value at line {line_number}",
            )
            knob = variation["knob"]
            value = variation["value"]
            _require(isinstance(knob, str) and bool(knob), f"invalid sweep knob at line {line_number}")
            _require(not isinstance(value, (dict, list)), f"non-scalar sweep value cannot map to CSV at line {line_number}")
            _require(cell_id == f"{knob}={_canonical_json(value)}", f"cell_id/variation conflict at line {line_number}")
            _require(case["settings"].get(knob) == value, f"settings/variation conflict at line {line_number}")
    return header, cases


def _load_trajectories(path: Path) -> list[dict[str, str]]:
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ReconciliationError(f"cannot read trajectories CSV {path}: {exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        _require(reader.fieldnames == list(TRAJECTORY_FIELDS), "trajectories.csv header is not the canonical trajectory schema")
        rows = list(reader)
    seen_run_ids: set[str] = set()
    for line_number, row in enumerate(rows, 2):
        _require(None not in row, f"trajectory row has extra unnamed columns at line {line_number}")
        _require(
            row["schema_version"] == "assignment.trajectory.v1",
            f"unsupported trajectory schema at line {line_number}",
        )
        for field in ("run_id", "suite", "instance_id", "config_id", "repeat_id", "status", "provenance"):
            _require(bool(row[field]), f"trajectory {field} is empty at line {line_number}")
        _require(row["suite"] in ALLOWED_SUITES, f"invalid trajectory suite at line {line_number}")
        _require(row["status"] in {"completed", "failed", "timeout", "unavailable"},
                 f"invalid trajectory status at line {line_number}")
        _require(row["provenance"] in {"measured", "derived_from_measured", "unavailable"},
                 f"invalid trajectory provenance at line {line_number}")
        _require(row["run_id"] not in seen_run_ids, f"duplicate trajectory run_id at line {line_number}")
        seen_run_ids.add(row["run_id"])
        if row["status"] == "completed" and row["provenance"] in ELIGIBLE_PROVENANCE:
            _validate_completed_csv_row(row, line_number)
    return rows


def _validate_completed_csv_row(row: Mapping[str, str], line_number: int) -> None:
    _require(row["submitted"] in {"true", "false"}, f"completed trajectory lacks submitted outcome at line {line_number}")
    _require(row["official_resolved"] in {"true", "false"},
             f"completed trajectory lacks official outcome at line {line_number}")
    try:
        e2e = float(row["e2e_wall_ms"])
        tool = float(row["tool_wall_ms"])
        model = float(row["model_wall_ms"])
        ratio = float(row["tool_model_ratio"])
        tool_count = int(row["tool_event_count"])
        model_count = int(row["model_event_count"])
    except ValueError as exc:
        raise ReconciliationError(f"completed trajectory has malformed measured fields at line {line_number}") from exc
    _require(all(math.isfinite(value) for value in (e2e, tool, model, ratio)),
             f"completed trajectory has non-finite measured fields at line {line_number}")
    _require(e2e > 0 and tool >= 0 and model > 0, f"completed trajectory has invalid phase timing at line {line_number}")
    _require(tool_count > 0 and model_count > 0, f"completed trajectory lacks measured events at line {line_number}")
    _require(math.isclose(ratio, tool / model, rel_tol=1e-9, abs_tol=1e-12),
             f"completed trajectory ratio mismatch at line {line_number}")
    _require(row["unavailable_reason"] == "", f"completed trajectory has unavailable_reason at line {line_number}")


def _csv_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)
    raise ReconciliationError("plan sweep value cannot be represented safely in canonical CSV")


def _case_identity(case: Mapping[str, Any]) -> dict[str, str]:
    return {
        "resume_key": case["resume_key"],
        "suite": case["suite"],
        "instance_id": case["instance_id"],
        "cell_id": case["cell_id"],
    }


def _trajectory_identity(row: Mapping[str, str]) -> dict[str, str]:
    return {
        "run_id": row["run_id"],
        "repeat_id": row["repeat_id"],
        "config_id": row["config_id"],
        "sweep_parameter": row["sweep_parameter"],
        "sweep_value": row["sweep_value"],
        "status": row["status"],
        "provenance": row["provenance"],
    }


def _identity_relation(case: Mapping[str, Any], row: Mapping[str, str]) -> str:
    """Return exact, conflict, or unrelated for a same-task trajectory."""
    cell_id = case["cell_id"]
    parameter = row["sweep_parameter"]
    value = row["sweep_value"]
    if cell_id == "shared-baseline":
        if row["config_id"] != cell_id:
            return "unrelated"
        return "exact" if parameter == "" and value == "" else "conflict"

    variation = case["variation"]
    expected_parameter = variation["knob"]
    expected_value = _csv_scalar(variation["value"])
    config_matches = row["config_id"] == cell_id
    variation_matches = parameter == expected_parameter and value == expected_value
    if config_matches and variation_matches:
        return "exact"
    if config_matches or variation_matches:
        return "conflict"
    return "unrelated"


def reconcile(
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    trajectories: list[dict[str, str]],
    *,
    original_plan_sha256: str,
    trajectories_sha256: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_task: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in trajectories:
        by_task.setdefault((row["suite"], row["instance_id"]), []).append(row)

    matched: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    rejected_or_ambiguous: list[dict[str, Any]] = []
    rejected_trajectories: list[dict[str, Any]] = []
    remaining: list[dict[str, Any]] = []
    rejected_seen: set[tuple[str, str]] = set()

    for case in cases:
        task_rows = by_task.get((case["suite"], case["instance_id"]), [])
        exact: list[dict[str, str]] = []
        conflicts: list[dict[str, str]] = []
        for row in task_rows:
            relation = _identity_relation(case, row)
            if relation == "exact":
                exact.append(row)
            elif relation == "conflict":
                conflicts.append(row)
        eligible = [
            row for row in exact
            if row["status"] == "completed" and row["provenance"] in ELIGIBLE_PROVENANCE
        ]
        ineligible = [row for row in exact if row not in eligible]
        for row in ineligible:
            marker = (case["resume_key"], row["run_id"])
            if marker not in rejected_seen:
                rejected_seen.add(marker)
                rejected_trajectories.append({
                    **_case_identity(case),
                    **_trajectory_identity(row),
                    "reason": "exact identity has non-completed or non-measured provenance",
                })

        if len(eligible) == 1 and not conflicts:
            matched.append({
                **_case_identity(case),
                "run_id": eligible[0]["run_id"],
                "repeat_id": eligible[0]["repeat_id"],
                "provenance": eligible[0]["provenance"],
            })
            continue

        remaining.append(case)
        if conflicts or len(eligible) > 1:
            reasons: list[str] = []
            if conflicts:
                reasons.append("partial config/variation identity conflict")
            if len(eligible) > 1:
                reasons.append("multiple completed measured trajectories match one plan case")
            rejected_or_ambiguous.append({
                **_case_identity(case),
                "reason": "; ".join(reasons),
                "eligible_matches": [_trajectory_identity(row) for row in eligible],
                "conflicts": [_trajectory_identity(row) for row in conflicts],
            })
        elif exact:
            rejected_or_ambiguous.append({
                **_case_identity(case),
                "reason": "exact identity exists but no completed measured/derived_from_measured trajectory qualifies",
                "eligible_matches": [],
                "conflicts": [],
            })
        else:
            unmatched.append({**_case_identity(case), "reason": "no exact canonical trajectory identity"})

    reconciliation = {
        "schema_version": RECONCILIATION_SCHEMA,
        "original_plan_sha256": original_plan_sha256,
        "trajectories_sha256": trajectories_sha256,
        "original_case_count": len(cases),
        "matched_case_count": len(matched),
        "unmatched_case_count": len(unmatched),
        "remaining_case_count": len(remaining),
        "rejected_or_ambiguous_case_count": len(rejected_or_ambiguous),
        "rejected_trajectory_count": len(rejected_trajectories),
        "matching_contract": {
            "eligible_status": "completed",
            "eligible_provenance": sorted(ELIGIBLE_PROVENANCE),
            "required_match_count": 1,
            "shared_baseline_identity": "config_id=shared-baseline and empty sweep_parameter/sweep_value",
            "sweep_identity": "config_id=cell_id and exact sweep_parameter/sweep_value",
            "unrepresented_plan_settings": "not inferred from trajectories.csv",
        },
    }
    remaining_header = dict(header)
    remaining_header["execution_case_count"] = len(remaining)
    remaining_header["reconciliation"] = reconciliation
    remaining_rows = [remaining_header, *remaining]
    remaining_payload = _render_jsonl(remaining_rows)
    report = {
        **reconciliation,
        "schema_version": REPORT_SCHEMA,
        "remaining_plan_sha256": _sha256_bytes(remaining_payload),
        "matched": matched,
        "unmatched": unmatched,
        "rejected_or_ambiguous": rejected_or_ambiguous,
        "rejected_trajectories": rejected_trajectories,
    }
    return remaining_rows, report


def _render_jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return ("\n".join(_canonical_json(row) for row in rows) + "\n").encode("utf-8")


def _render_report(report: Mapping[str, Any]) -> bytes:
    return (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _stage(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_outputs(
    remaining_output: Path,
    remaining_sidecar: Path,
    report_output: Path,
    remaining_rows: list[dict[str, Any]],
    report: Mapping[str, Any],
    *,
    force: bool,
) -> str:
    paths = [remaining_output, remaining_sidecar, report_output]
    _require(len({path.resolve() for path in paths}) == len(paths), "output paths must be distinct")
    if not force:
        for path in paths:
            _require(not path.exists(), f"refusing to overwrite {path}; pass --force")
    remaining_payload = _render_jsonl(remaining_rows)
    digest = _sha256_bytes(remaining_payload)
    _require(report.get("remaining_plan_sha256") == digest, "internal remaining-plan digest mismatch")
    payloads = [
        remaining_payload,
        f"{digest}  {remaining_output.name}\n".encode("utf-8"),
        _render_report(report),
    ]
    staged: list[tuple[str, Path]] = []
    try:
        for path, payload in zip(paths, payloads):
            staged.append((_stage(path, payload), path))
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _path in staged:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return digest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--plan-sha256", type=Path, help="default: <plan>.sha256")
    parser.add_argument("--trajectories", required=True, type=Path)
    parser.add_argument("--remaining-output", required=True, type=Path)
    parser.add_argument("--remaining-sha256", type=Path, help="default: <remaining-output>.sha256")
    parser.add_argument("--report-output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    plan_sidecar = args.plan_sha256 or Path(f"{args.plan}.sha256")
    remaining_sidecar = args.remaining_sha256 or Path(f"{args.remaining_output}.sha256")
    try:
        input_paths = {args.plan.resolve(), plan_sidecar.resolve(), args.trajectories.resolve()}
        output_paths = {args.remaining_output.resolve(), remaining_sidecar.resolve(), args.report_output.resolve()}
        _require(input_paths.isdisjoint(output_paths), "outputs must not overwrite or alias inputs")
        original_plan_sha256 = _verify_plan_sidecar(args.plan, plan_sidecar)
        header, cases = _read_plan(args.plan)
        trajectories_sha256 = _file_sha256(args.trajectories)
        trajectories = _load_trajectories(args.trajectories)
        remaining_rows, report = reconcile(
            header,
            cases,
            trajectories,
            original_plan_sha256=original_plan_sha256,
            trajectories_sha256=trajectories_sha256,
        )
        digest = _write_outputs(
            args.remaining_output,
            remaining_sidecar,
            args.report_output,
            remaining_rows,
            report,
            force=args.force,
        )
        print(
            "assignment plan reconciliation: PASS "
            f"matched={len(report['matched'])} remaining={len(remaining_rows) - 1} sha256={digest}"
        )
        return 0
    except ReconciliationError as exc:
        print(f"assignment plan reconciliation: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
