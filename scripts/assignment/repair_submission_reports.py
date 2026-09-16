#!/usr/bin/env python3
"""Generate the evidence-only 2026-09-08 submission repair package.

This script owns the report and D1 metadata repair boundary.  It intentionally
does not call the figure generator, model code, evaluator, simulator, or any
experiment runner.  The canonical trajectory, sweep, tool-event, and
model-event CSVs are read-only inputs.  The D1 headline uses the accepted-case
``summary.duration_ms`` source; the canonical trajectory duration remains
available as the trace-overlay metric for event-coverage figures.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path("/home/riverahernandezjason")
ASSIGNMENT_BASE = ROOT / "h100-assignment-work-20260905" / "assignment"
REPAIR_WORKTREE = ROOT / "agentic-submission-repairs-20260908"
SOURCE_WORKTREE = ROOT / "agentic-reliability-zero-proxy-fix-20260907"
AUTHORITY_PDF = ROOT / "Coding tests Harrdware (2).pdf"
PRIOR_SNAPSHOT = ASSIGNMENT_BASE / "submission" / "20260908T020000Z"
AUDIT_DIR = ASSIGNMENT_BASE / "submission" / "20260908T070000Z" / "final-review"

PROTECTED_INPUT_NAMES = (
    "trajectories.csv",
    "sweep_runs.csv",
    "tool_events.csv",
    "model_events.csv",
    "evaluator_provenance.csv",
    "reconciliation.json",
    "step3_selection.json",
)

# Parallel figure-generation work may add these files to figures-input. They
# are additive metadata, not replacements for the copied canonical inputs.
ADDITIVE_INPUT_NAMES = (
    "sweep_metadata.jsonl",
)

BASELINE_FIGURE_NAMES = (
    "step1_repository_ratio.svg",
    "step1_accuracy_vs_latency.svg",
    "step1_accuracy_vs_ratio.svg",
    "step1_sample_latency_vs_ratio.svg",
    "step2_call-limit.svg",
    "step2_max-output-tokens.svg",
    "step2_observation-length.svg",
    "step2_temperature.svg",
    "step2_combined.svg",
    "step3_latency_breakdown.svg",
    "step3_tool_events.svg",
    "step3_model_tokens_vs_latency.svg",
)
PREDICTED_FIGURE_NAMES = BASELINE_FIGURE_NAMES

EXPECTED_HEADLINE_OVERRIDES = {
    "django__django-10097": 37959.573949,
    "django__django-7530": 36372.668687,
    "psf__requests-1724": 43831.059926,
}

DEFAULT_OVERRIDE_SPECS = (
    "django__django-10097=/home/riverahernandezjason/h100-assignment-work-20260905/assignment/remaining-failed-06d0167-cpu-docker-20260907T193200Z-16/worker-14/cases/00000/runner_attempts/attempt-001/reviewed_runner_artifacts/data/raw/assignment-6a83c23b078a6cb5/verified/django__django-10097/attempt-001/summary.json",
    "django__django-7530=/home/riverahernandezjason/h100-assignment-work-20260905/assignment/remaining-failed-06d0167-cpu-docker-20260907T193200Z-16/worker-00/cases/00000/runner_attempts/attempt-001/reviewed_runner_artifacts/data/raw/assignment-4a6b175cf1ae7e9e/verified/django__django-7530/attempt-001/summary.json",
    "psf__requests-1724=/home/riverahernandezjason/h100-assignment-work-20260905/assignment/remaining-failed-c19-rebalance-20260907T194200Z-16/worker-05/cases/00002/runner_attempts/attempt-001/reviewed_runner_artifacts/data/raw/assignment-280d4cbeff2ab416/verified/psf__requests-1724/attempt-001/summary.json",
)

TRAJECTORY_COLUMNS = (
    "run_id",
    "suite",
    "repository",
    "category",
    "instance_id",
    "config_id",
    "repeat_id",
    "status",
    "submitted",
    "official_resolved",
    "e2e_wall_ms",
    "tool_wall_ms",
    "model_wall_ms",
    "tool_model_ratio",
    "tool_event_count",
    "model_event_count",
    "provenance",
    "sweep_parameter",
    "sweep_value",
)
SWEEP_COLUMNS = (
    "run_id",
    "status",
    "sweep_parameter",
    "sweep_value",
    "official_resolved",
    "e2e_wall_ms",
    "tool_wall_ms",
    "model_wall_ms",
)
TOOL_COLUMNS = ("event_id", "run_id", "status", "operation_class", "wall_ms")
MODEL_COLUMNS = (
    "request_id",
    "run_id",
    "status",
    "input_tokens",
    "output_tokens",
    "context_tokens",
    "wall_ms",
)
EVALUATOR_COLUMNS = (
    "run_id",
    "suite",
    "instance_id",
    "config_id",
    "repeat_id",
    "submitted",
    "official_resolved",
    "eval_source_kind",
    "eval_source_path",
    "eval_source_sha256",
)


class RepairContractError(RuntimeError):
    """Raised when preserved evidence does not satisfy the repair contract."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RepairContractError(f"cannot read JSON evidence {path}: {exc}") from exc


def read_csv(path: Path, expected_columns: Iterable[str]) -> list[dict[str, str]]:
    expected = tuple(expected_columns)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != expected:
                raise RepairContractError(
                    f"{path}: expected columns {expected!r}, got {tuple(reader.fieldnames or ())!r}"
                )
            return list(reader)
    except OSError as exc:
        raise RepairContractError(f"cannot read CSV evidence {path}: {exc}") from exc


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise RepairContractError(f"{label} is missing: {path}")
    return path


def parse_bool(raw: str, field: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise RepairContractError(f"{field}: expected boolean, got {raw!r}")


def finite_float(raw: str | int | float, field: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RepairContractError(f"{field}: expected number, got {raw!r}") from exc
    if not math.isfinite(value):
        raise RepairContractError(f"{field}: number is not finite: {raw!r}")
    return value


def positive_float(raw: str | int | float, field: str) -> float:
    value = finite_float(raw, field)
    if value <= 0:
        raise RepairContractError(f"{field}: expected positive number, got {value}")
    return value


def integer(raw: str | int | float, field: str) -> int:
    value = finite_float(raw, field)
    if not value.is_integer() or value < 0:
        raise RepairContractError(f"{field}: expected non-negative integer, got {raw!r}")
    return int(value)


def sidecar_digest(path: Path) -> str | None:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.is_file():
        return None
    line = sidecar.read_text(encoding="utf-8").strip().split()
    if not line:
        raise RepairContractError(f"empty SHA-256 sidecar: {sidecar}")
    digest = line[0]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RepairContractError(f"invalid SHA-256 sidecar {sidecar}: {digest!r}")
    return digest


def verify_sidecar(path: Path, *, required: bool = True) -> str:
    actual = sha256_file(path)
    declared = sidecar_digest(path)
    if declared is None:
        if required:
            raise RepairContractError(f"missing SHA-256 sidecar for {path}")
        return actual
    if declared != actual:
        raise RepairContractError(
            f"SHA-256 sidecar mismatch for {path}: declared {declared}, actual {actual}"
        )
    return actual


def write_bytes(path: Path, payload: bytes, *, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing repair output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def write_text(path: Path, payload: str, *, force: bool) -> None:
    write_bytes(path, payload.encode("utf-8"), force=force)


def write_json(path: Path, value: Any, *, force: bool) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", force=force)


def write_hash_sidecar(path: Path, *, force: bool) -> Path:
    sidecar = path.with_name(path.name + ".sha256")
    write_text(sidecar, f"{sha256_file(path)}  {path.name}\n", force=force)
    return sidecar


def command_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def git_metadata(worktree: Path) -> dict[str, Any]:
    if not (worktree / ".git").exists():
        return {"path": str(worktree), "available": False}
    head = command_output(["git", "rev-parse", "HEAD"], worktree)
    branch = command_output(["git", "branch", "--show-current"], worktree)
    status = command_output(["git", "status", "--porcelain=v1"], worktree) or ""
    return {
        "path": str(worktree),
        "available": head is not None,
        "head": head,
        "branch": branch,
        "dirty": bool(status),
        "status_entry_count": len([line for line in status.splitlines() if line.strip()]),
        "status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def prior_run_provenance(report_path: Path) -> dict[str, Any]:
    text = report_path.read_text(encoding="utf-8")
    commit_match = re.search(r"Repository commit\s*\|\s*`([0-9a-f]{40})`", text)
    branch_match = re.search(r"Repository commit\s*\|\s*`[0-9a-f]{40}`\s*\(`([^`]+)`\)", text)
    dirty_match = re.search(r"Worktree dirty\s*\|\s*[*` ]*(yes|no)", text, re.IGNORECASE)
    return {
        "source_report": str(report_path),
        "source_report_sha256": sha256_file(report_path),
        "commit": commit_match.group(1) if commit_match else None,
        "branch": branch_match.group(1) if branch_match else None,
        "worktree_dirty_at_collection": (
            dirty_match.group(1).lower() == "yes" if dirty_match else None
        ),
        "interpretation": "historical run provenance; it is not the current repair source state",
    }


def file_record(path: Path, *, root: Path | None = None, role: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
    if root is not None:
        try:
            record["relative_path"] = str(path.relative_to(root))
        except ValueError:
            pass
    if role is not None:
        record["role"] = role
    return record


def parse_override_specs(specs: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for spec in specs:
        instance_id, separator, raw_path = spec.partition("=")
        if not separator or not instance_id or not raw_path:
            raise RepairContractError(
                f"--headline-latency-source must be INSTANCE_ID=PATH, got {spec!r}"
            )
        if instance_id in result:
            raise RepairContractError(f"duplicate headline latency override: {instance_id}")
        result[instance_id] = require_file(Path(raw_path), "headline latency source")
    if set(result) != set(EXPECTED_HEADLINE_OVERRIDES):
        raise RepairContractError(
            "headline latency sources must cover exactly the three audited coverage keys: "
            + ", ".join(sorted(EXPECTED_HEADLINE_OVERRIDES))
        )
    return result


def verify_copied_inputs(snapshot_dir: Path, prior_snapshot: Path) -> dict[str, str]:
    current_dir = require_file(snapshot_dir / "figures-input" / "trajectories.csv", "snapshot input")
    del current_dir
    before: dict[str, str] = {}
    for name in PROTECTED_INPUT_NAMES:
        current = require_file(snapshot_dir / "figures-input" / name, "new canonical input")
        prior = require_file(prior_snapshot / "figures-input" / name, "prior canonical input")
        current_digest = verify_sidecar(current)
        prior_digest = verify_sidecar(prior)
        if current_digest != prior_digest or current.read_bytes() != prior.read_bytes():
            raise RepairContractError(
                f"new snapshot canonical input differs from preserved 02:00Z input: {name}"
            )
        before[name] = current_digest
    for name in ADDITIVE_INPUT_NAMES:
        additive = snapshot_dir / "figures-input" / name
        if additive.exists():
            verify_sidecar(additive)
    return before


def validate_protected_inputs(snapshot_dir: Path, before: dict[str, str]) -> None:
    for name, digest in before.items():
        current = snapshot_dir / "figures-input" / name
        actual = sha256_file(current)
        if actual != digest:
            raise RepairContractError(
                f"protected canonical input changed during report generation: {name}"
            )
        verify_sidecar(current)


def load_evidence(snapshot_dir: Path, prior_snapshot: Path, overrides: dict[str, Path]) -> dict[str, Any]:
    figures_input = snapshot_dir / "figures-input"
    input_digests = verify_copied_inputs(snapshot_dir, prior_snapshot)
    trajectories = read_csv(figures_input / "trajectories.csv", TRAJECTORY_COLUMNS)
    sweeps = read_csv(figures_input / "sweep_runs.csv", SWEEP_COLUMNS)
    tool_events = read_csv(figures_input / "tool_events.csv", TOOL_COLUMNS)
    model_events = read_csv(figures_input / "model_events.csv", MODEL_COLUMNS)
    evaluator_rows = read_csv(figures_input / "evaluator_provenance.csv", EVALUATOR_COLUMNS)
    reconciliation = json_load(figures_input / "reconciliation.json")
    selection = json_load(figures_input / "step3_selection.json")
    compile_summary_path = prior_snapshot / "compile_summary.json"
    prior_report_path = prior_snapshot / "ASSIGNMENT_REPORT.md"
    prior_figure_json_path = prior_snapshot / "figures" / "assignment_report.json"
    compile_summary = json_load(require_file(compile_summary_path, "compile summary"))
    prior_report = require_file(prior_report_path, "prior assignment report")
    prior_figure_json = json_load(require_file(prior_figure_json_path, "prior figure report JSON"))

    if len(trajectories) != 800:
        raise RepairContractError(f"expected 800 canonical baseline trajectories, got {len(trajectories)}")
    run_ids: set[str] = set()
    instance_keys: set[tuple[str, str]] = set()
    for row_number, row in enumerate(trajectories, 2):
        run_id = row["run_id"]
        instance_id = row["instance_id"]
        identity_key = (row["suite"], instance_id)
        if run_id in run_ids or identity_key in instance_keys:
            raise RepairContractError(f"duplicate canonical identity at trajectories row {row_number}")
        run_ids.add(run_id)
        instance_keys.add(identity_key)
        if row["status"] != "completed" or row["config_id"] != "shared-baseline":
            raise RepairContractError(f"D1 input row {row_number} is not completed shared-baseline")
        if not parse_bool(row["submitted"], f"trajectories row {row_number} submitted"):
            raise RepairContractError(f"D1 input row {row_number} is not submitted")
        positive_float(row["e2e_wall_ms"], f"trajectories row {row_number} e2e_wall_ms")
        positive_float(row["tool_wall_ms"], f"trajectories row {row_number} tool_wall_ms")
        positive_float(row["model_wall_ms"], f"trajectories row {row_number} model_wall_ms")
    suite_counts = Counter(row["suite"] for row in trajectories)
    if suite_counts != Counter({"lite": 300, "verified": 500}):
        raise RepairContractError(f"unexpected D1 suite counts: {suite_counts}")

    evaluator_by_run: dict[str, dict[str, str]] = {}
    evaluator_by_instance: dict[tuple[str, str], dict[str, str]] = {}
    for row_number, row in enumerate(evaluator_rows, 2):
        identity_key = (row["suite"], row["instance_id"])
        if row["run_id"] in evaluator_by_run or identity_key in evaluator_by_instance:
            raise RepairContractError(f"duplicate evaluator provenance identity at row {row_number}")
        evaluator_by_run[row["run_id"]] = row
        evaluator_by_instance[identity_key] = row
    if set(evaluator_by_run) != run_ids:
        raise RepairContractError("evaluator provenance does not join one-to-one with trajectories")
    for row in trajectories:
        provenance = evaluator_by_run[row["run_id"]]
        if provenance["instance_id"] != row["instance_id"]:
            raise RepairContractError(f"evaluator instance mismatch for {row['run_id']}")
        if parse_bool(provenance["official_resolved"], "evaluator official_resolved") != parse_bool(
            row["official_resolved"], "trajectory official_resolved"
        ):
            raise RepairContractError(f"evaluator label mismatch for {row['instance_id']}")

    expected_d1 = compile_summary["d1_unique_accepted"]
    expected_suite = {
        "lite": (expected_d1["lite"], 300, 100),
        "verified": (expected_d1["verified"], 500, 198),
    }
    for suite, (expected, expected_count, expected_resolved) in expected_suite.items():
        members = [row for row in trajectories if row["suite"] == suite]
        resolved = sum(parse_bool(row["official_resolved"], "trajectory official_resolved") for row in members)
        if len(members) != expected_count or resolved != expected_resolved:
            raise RepairContractError(f"canonical {suite} labels disagree with compile summary")
        measured_mean = sum(finite_float(row["e2e_wall_ms"], "trajectory e2e_wall_ms") for row in members) / len(members)
        if suite == "verified":
            expected_mean = float(
                prior_figure_json["suite_headline_metrics"][suite]["average_completed_e2e_wall_ms"]
            )
        else:
            expected_mean = float(expected["mean_e2e_s"]) * 1000.0
        if not math.isclose(measured_mean, expected_mean, rel_tol=0, abs_tol=1e-6):
            raise RepairContractError(
                f"canonical {suite} trace mean {measured_mean} does not match compile source {expected_mean}"
            )

    override_values: dict[str, dict[str, Any]] = {}
    for instance_id, path in overrides.items():
        payload = json_load(path)
        source_instance = payload.get("instance_id")
        if source_instance != instance_id:
            raise RepairContractError(f"override {path} has instance_id {source_instance!r}, expected {instance_id!r}")
        duration_ms = positive_float(payload.get("duration_ms"), f"{path} duration_ms")
        expected = EXPECTED_HEADLINE_OVERRIDES[instance_id]
        if not math.isclose(duration_ms, expected, rel_tol=0, abs_tol=1e-9):
            raise RepairContractError(f"override {instance_id} has unexpected duration_ms {duration_ms}")
        override_values[instance_id] = {
            "path": path,
            "sha256": sha256_file(path),
            "duration_ms": duration_ms,
        }

    selected_run_id = selection["selected"]["run_id"]
    selected_rows = [row for row in trajectories if row["run_id"] == selected_run_id]
    if len(selected_rows) != 1:
        raise RepairContractError(f"sealed Step 3 selection does not join one trajectory: {selected_run_id}")
    selected_row = selected_rows[0]
    if selected_row["instance_id"] != selection["selected"]["instance_id"]:
        raise RepairContractError("sealed Step 3 selection instance mismatch")
    selected_tools = [row for row in tool_events if row["run_id"] == selected_run_id]
    selected_models = [row for row in model_events if row["run_id"] == selected_run_id]
    if len(selected_tools) != integer(selection["selected"]["tool_event_count"], "selection tool_event_count"):
        raise RepairContractError("sealed Step 3 tool event count mismatch")
    if len(selected_models) != integer(selection["selected"]["model_event_count"], "selection model_event_count"):
        raise RepairContractError("sealed Step 3 model event count mismatch")
    for row in selected_tools:
        if row["status"] != "completed":
            raise RepairContractError("selected tool ledger contains a non-completed event")
        positive_float(row["wall_ms"], "selected tool wall_ms")
    for row in selected_models:
        if row["status"] != "completed":
            raise RepairContractError("selected model ledger contains a non-completed request")
        integer(row["input_tokens"], "selected input_tokens")
        integer(row["output_tokens"], "selected output_tokens")
        integer(row["context_tokens"], "selected context_tokens")
        positive_float(row["wall_ms"], "selected model wall_ms")
    tool_sum = sum(finite_float(row["wall_ms"], "tool event wall_ms") for row in selected_tools)
    model_sum = sum(finite_float(row["wall_ms"], "model event wall_ms") for row in selected_models)
    if not math.isclose(tool_sum, finite_float(selected_row["tool_wall_ms"], "selected trajectory tool_wall_ms"), rel_tol=0, abs_tol=1e-6):
        raise RepairContractError("selected tool ledger sum disagrees with trajectory")
    if not math.isclose(model_sum, finite_float(selected_row["model_wall_ms"], "selected trajectory model_wall_ms"), rel_tol=0, abs_tol=1e-6):
        raise RepairContractError("selected model ledger sum disagrees with trajectory")

    return {
        "snapshot_dir": snapshot_dir,
        "prior_snapshot": prior_snapshot,
        "figures_input": figures_input,
        "input_digests": input_digests,
        "trajectories": trajectories,
        "sweeps": sweeps,
        "tool_events": tool_events,
        "model_events": model_events,
        "evaluator_rows": evaluator_rows,
        "evaluator_by_run": evaluator_by_run,
        "reconciliation": reconciliation,
        "selection": selection,
        "selected_row": selected_row,
        "selected_tools": selected_tools,
        "selected_models": selected_models,
        "compile_summary": compile_summary,
        "prior_report_path": prior_report,
        "prior_figure_json_path": prior_figure_json_path,
        "prior_figure_json": prior_figure_json,
        "override_values": override_values,
        "authority_pdf": AUTHORITY_PDF,
    }


def suite_metrics(rows: list[dict[str, str]], suite: str, latency_key: str) -> dict[str, Any]:
    selected = [row for row in rows if row["suite"] == suite]
    submitted = [row for row in selected if parse_bool(row["submitted"], "submitted")]
    completed = [row for row in selected if row["status"] == "completed"]
    resolved = [row for row in completed if parse_bool(row["official_resolved"], "official_resolved")]
    average = sum(finite_float(row[latency_key], latency_key) for row in completed) / len(completed)
    return {
        "selected": {"count": len(selected), "denominator": len(selected)},
        "submitted": {"count": len(submitted), "denominator": len(selected)},
        "completed": {"count": len(completed), "denominator": len(selected)},
        "resolved": {"count": len(resolved), "denominator": len(completed)},
        "resolved_rate": {
            "numerator": len(resolved),
            "denominator": len(completed),
            "percent": 100.0 * len(resolved) / len(completed) if completed else None,
        },
        "average_completed_e2e_wall_ms": average,
        "average_completed_e2e_wall_s": average / 1000.0,
    }


def aggregate_categories(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[row["category"]].append(row)
    result = []
    for category in sorted(groups):
        members = groups[category]
        tool_ms = sum(finite_float(row["tool_wall_ms"], "tool_wall_ms") for row in members)
        model_ms = sum(finite_float(row["model_wall_ms"], "model_wall_ms") for row in members)
        resolved = sum(parse_bool(row["official_resolved"], "official_resolved") for row in members)
        result.append(
            {
                "category": category,
                "count": len(members),
                "resolved_count": resolved,
                "accuracy_percent": 100.0 * resolved / len(members),
                "average_e2e_wall_ms": sum(finite_float(row["e2e_wall_ms"], "e2e_wall_ms") for row in members) / len(members),
                "tool_wall_ms": tool_ms,
                "model_wall_ms": model_ms,
                "tool_model_ratio": tool_ms / model_ms,
            }
        )
    return result


def value_sort(value: str) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value)


def downstream_presence(snapshot_dir: Path) -> dict[str, Any]:
    figures_dir = snapshot_dir / "figures"
    predicted_dir = snapshot_dir / "d9-predicted" / "figures"
    configuration_path = snapshot_dir / "configuration-analysis" / "CONFIGURATION_ANALYSIS.md"

    def group(directory: Path, names: tuple[str, ...]) -> dict[str, Any]:
        present = [name for name in names if (directory / name).is_file()]
        if len(present) == len(names):
            status = f"complete ({len(present)}/{len(names)} expected files present)"
        elif not present:
            status = f"pending (0/{len(names)} expected files present)"
        else:
            status = f"partial ({len(present)}/{len(names)} expected files present)"
        return {
            "directory": str(directory.relative_to(snapshot_dir)),
            "expected": list(names),
            "present": present,
            "present_count": len(present),
            "expected_count": len(names),
            "status": status,
        }

    return {
        "figure_generation": group(figures_dir, BASELINE_FIGURE_NAMES),
        "prediction_export": group(predicted_dir, PREDICTED_FIGURE_NAMES),
        "configuration_analysis": {
            "path": str(configuration_path.relative_to(snapshot_dir)),
            "present": configuration_path.is_file(),
            "status": "complete (file present)" if configuration_path.is_file() else "pending (file absent)",
        },
        "sweep_metadata": {
            "path": "figures-input/sweep_metadata.jsonl",
            "present": (snapshot_dir / "figures-input" / "sweep_metadata.jsonl").is_file(),
            "sidecar_present": (snapshot_dir / "figures-input" / "sweep_metadata.jsonl.sha256").is_file(),
        },
    }


def prediction_coverage(snapshot_dir: Path) -> dict[str, Any]:
    path = snapshot_dir / "d9-predicted" / "d9_predicted_manifest.json"
    if not path.is_file():
        return {"available": False, "description": "Prediction coverage is pending export; no supported-row count is asserted."}
    manifest = json_load(path)
    coverage = manifest["coverage"]
    excluded = manifest["holdout_exclusion"]["baseline_source_rows_excluded"]
    predicted = coverage["baseline_predicted_runs"]
    unknown = coverage["baseline_unknown_runs"]
    source = coverage["baseline_source_runs"]
    if predicted + unknown + excluded != source:
        raise RepairContractError("prediction manifest baseline coverage does not close")
    return {
        "available": True,
        "baseline_prediction_scope": f"{predicted}/{source} canonical baseline rows",
        "missing_support_rows": unknown,
        "excluded_holdout_rows": excluded,
        "derived_sweep_baseline_rows": coverage["sweep_derived_predicted_rows"],
        "manifest_sha256": sha256_file(path),
        "description": (
            f"Prediction export covers {predicted}/{source} canonical baseline rows; "
            f"{unknown} rows remain unresolved for local exact workload support and {excluded} holdout rows are excluded. "
            "Unresolved local support does not establish remote artifact loss. "
            f"The {coverage['sweep_derived_predicted_rows']} derived sweep-baseline coordinates reuse their source predictions, "
            "not new prediction calls or measurements. Timing is predicted conditional on the realized workload; accuracy remains observed evaluator outcome."
        ),
    }


def aggregate_sweeps(rows: list[dict[str, str]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["status"] != "completed":
            raise RepairContractError("sweep input contains a non-completed row")
        groups[(row["sweep_parameter"], row["sweep_value"])].append(row)
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    baseline_values = {
        ("call_limit", "30"),
        ("max_output_tokens", "2048"),
        ("observation_length", "100000"),
        ("temperature", "0.0"),
    }
    for (parameter, value), members in sorted(groups.items()):
        tool_ms = sum(finite_float(row["tool_wall_ms"], "sweep tool_wall_ms") for row in members)
        model_ms = sum(finite_float(row["model_wall_ms"], "sweep model_wall_ms") for row in members)
        resolved = sum(parse_bool(row["official_resolved"], "sweep official_resolved") for row in members)
        result[parameter].append(
            {
                "parameter": parameter,
                "value": value,
                "count": len(members),
                "resolved_count": resolved,
                "accuracy_percent": 100.0 * resolved / len(members),
                "average_e2e_wall_ms": sum(finite_float(row["e2e_wall_ms"], "sweep e2e_wall_ms") for row in members) / len(members),
                "tool_model_ratio": tool_ms / model_ms,
                "row_source": "derived shared-baseline coordinate" if (parameter, value) in baseline_values else "measured non-baseline cell",
            }
        )
    return {parameter: sorted(settings, key=lambda item: value_sort(item["value"])) for parameter, settings in sorted(result.items())}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    denominator_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    denominator_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if denominator_x == 0 or denominator_y == 0:
        return None
    return numerator / (denominator_x * denominator_y)


def selected_event_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    row = evidence["selected_row"]
    tools = evidence["selected_tools"]
    models = evidence["selected_models"]
    e2e_ms = finite_float(row["e2e_wall_ms"], "selected e2e_wall_ms")
    tool_sum = sum(finite_float(item["wall_ms"], "selected tool wall_ms") for item in tools)
    model_sum = sum(finite_float(item["wall_ms"], "selected model wall_ms") for item in models)
    residual = e2e_ms - tool_sum - model_sum
    class_groups: dict[str, list[float]] = defaultdict(list)
    for item in tools:
        class_groups[item["operation_class"]].append(finite_float(item["wall_ms"], "tool wall_ms"))
    tool_classes = [
        {
            "operation_class": operation,
            "events": len(values),
            "sum_wall_ms": sum(values),
            "share_of_tool_wall_percent": 100.0 * sum(values) / tool_sum,
        }
        for operation, values in sorted(class_groups.items())
    ]
    input_tokens = [integer(item["input_tokens"], "input_tokens") for item in models]
    output_tokens = [integer(item["output_tokens"], "output_tokens") for item in models]
    context_tokens = [integer(item["context_tokens"], "context_tokens") for item in models]
    request_wall = [finite_float(item["wall_ms"], "model wall_ms") for item in models]
    tool_ledger = []
    for ordinal, item in enumerate(tools, 1):
        wall = finite_float(item["wall_ms"], "tool wall_ms")
        tool_ledger.append(
            {
                "ordinal": ordinal,
                "event_id": item["event_id"],
                "operation_class": item["operation_class"],
                "status": item["status"],
                "wall_ms": wall,
                "share_of_tool_wall_percent": 100.0 * wall / tool_sum,
            }
        )
    model_ledger = []
    for ordinal, item in enumerate(models, 1):
        model_ledger.append(
            {
                "ordinal": ordinal,
                "request_id": item["request_id"],
                "status": item["status"],
                "input_tokens": integer(item["input_tokens"], "input_tokens"),
                "output_tokens": integer(item["output_tokens"], "output_tokens"),
                "context_tokens": integer(item["context_tokens"], "context_tokens"),
                "wall_ms": finite_float(item["wall_ms"], "model wall_ms"),
            }
        )
    return {
        "run_id": row["run_id"],
        "instance_id": row["instance_id"],
        "suite": row["suite"],
        "category": row["category"],
        "e2e_wall_ms": e2e_ms,
        "tool_wall_ms": tool_sum,
        "model_wall_ms": model_sum,
        "tool_model_ratio": tool_sum / model_sum,
        "residual_wall_ms": residual,
        "residual_share_of_e2e_percent": 100.0 * residual / e2e_ms,
        "tool_share_of_e2e_percent": 100.0 * tool_sum / e2e_ms,
        "model_share_of_e2e_percent": 100.0 * model_sum / e2e_ms,
        "tool_event_count": len(tools),
        "model_event_count": len(models),
        "tool_classes": tool_classes,
        "input_token_total": sum(input_tokens),
        "input_token_mean": sum(input_tokens) / len(input_tokens),
        "input_token_min": min(input_tokens),
        "input_token_max": max(input_tokens),
        "output_token_total": sum(output_tokens),
        "output_token_mean": sum(output_tokens) / len(output_tokens),
        "output_token_min": min(output_tokens),
        "output_token_max": max(output_tokens),
        "context_token_total": sum(context_tokens),
        "context_token_mean": sum(context_tokens) / len(context_tokens),
        "context_token_min": min(context_tokens),
        "context_token_max": max(context_tokens),
        "model_wall_min": min(request_wall),
        "model_wall_max": max(request_wall),
        "context_wall_pearson": pearson([float(value) for value in context_tokens], request_wall),
        "output_wall_pearson": pearson([float(value) for value in output_tokens], request_wall),
        "tool_ledger": tool_ledger,
        "model_ledger": model_ledger,
    }


def build_d1_mapping(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    mapping: list[dict[str, Any]] = []
    trajectory_path = evidence["figures_input"] / "trajectories.csv"
    for row_number, row in enumerate(evidence["trajectories"], 2):
        instance_id = row["instance_id"]
        canonical_ms = finite_float(row["e2e_wall_ms"], "trajectory e2e_wall_ms")
        override = evidence["override_values"].get(instance_id)
        if override is None:
            headline_ms = canonical_ms
            source_kind = "canonical_trajectory_e2e_wall_ms_compiled_from_accepted_summary"
            headline_source_path = str(trajectory_path)
            headline_source_sha256 = sha256_file(trajectory_path)
            headline_source_detail = "canonical row; original accepted summary source was used by the frozen compile"
        else:
            headline_ms = override["duration_ms"]
            source_kind = "original_accepted_case_summary_duration_ms"
            headline_source_path = str(override["path"])
            headline_source_sha256 = override["sha256"]
            headline_source_detail = "coverage rerun supplies trace coverage only; accepted-case headline retains summary.duration_ms"
        evaluator = evidence["evaluator_by_run"][row["run_id"]]
        mapping.append(
            {
                "trajectory_row_number": row_number,
                "suite": row["suite"],
                "instance_id": instance_id,
                "run_id": row["run_id"],
                "config_id": row["config_id"],
                "repeat_id": row["repeat_id"],
                "submitted": row["submitted"],
                "official_resolved": row["official_resolved"],
                "evaluator_source_kind": evaluator["eval_source_kind"],
                "evaluator_source_path": evaluator["eval_source_path"],
                "evaluator_source_sha256": evaluator["eval_source_sha256"],
                "headline_e2e_wall_ms": headline_ms,
                "headline_e2e_wall_s": headline_ms / 1000.0,
                "headline_latency_source_kind": source_kind,
                "headline_latency_source_path": headline_source_path,
                "headline_latency_source_sha256": headline_source_sha256,
                "headline_latency_source_detail": headline_source_detail,
                "trace_overlay_e2e_wall_ms": canonical_ms,
                "trace_overlay_e2e_wall_s": canonical_ms / 1000.0,
                "trace_overlay_delta_ms": canonical_ms - headline_ms,
                "trace_overlay_definition": "canonical trajectory e2e_wall_ms used for recorded event-coverage figures",
                "is_coverage_rerun_key": "true" if instance_id in EXPECTED_HEADLINE_OVERRIDES else "false",
            }
        )
    return mapping


def csv_text(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
    from io import StringIO

    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def fmt(value: float | int | None, digits: int = 3) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def pct(value: float | None, digits: int = 3) -> str:
    return "UNKNOWN" if value is None else f"{value:.{digits}f}%"


def link(path: str) -> str:
    return f"[{path}]({path})"


def build_sources(evidence: dict[str, Any], audit_dir: Path, authority_pdf: Path) -> dict[str, Any]:
    figures_input = evidence["figures_input"]
    sources: dict[str, Any] = {
        "authority_pdf": file_record(authority_pdf, role="authoritative assignment specification"),
        "canonical_inputs": [
            file_record(figures_input / name, root=evidence["snapshot_dir"], role="preserved canonical input")
            for name in PROTECTED_INPUT_NAMES
        ],
        "compile_summary": file_record(evidence["prior_snapshot"] / "compile_summary.json", role="D1 accepted-case arithmetic source"),
        "prior_assignment_report": file_record(evidence["prior_report_path"], role="preserved historical report"),
        "prior_figure_report_json": file_record(evidence["prior_figure_json_path"], role="trace-overlay metric source"),
        "audits": [
            file_record(audit_dir / "D1_RECOVERED_AUDIT.md", role="D1 recovered audit"),
            file_record(audit_dir / "FINAL_D1_D9_AUDIT_AND_PLAN.md", role="final audit and plan"),
            file_record(audit_dir / "FIGURES_SWEEPS_RECOVERED_AUDIT.md", role="D2-D8 recovered audit"),
            file_record(audit_dir / "CONFIGURATION_SWEEP_EVIDENCE.md", role="existing sweep evidence"),
        ],
        "original_headline_latency_override_summaries": [
            file_record(item["path"], role="accepted-case duration_ms override")
            for _, item in sorted(evidence["override_values"].items())
        ],
    }
    return sources


def build_provenance(
    *,
    evidence: dict[str, Any],
    audit_dir: Path,
    authority_pdf: Path,
    script_path: Path,
    prior_report_path: Path,
    source_worktree: Path,
    repair_worktree: Path,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "recorded_run": prior_run_provenance(prior_report_path),
        "current_source_worktree": git_metadata(source_worktree),
        "current_repair_worktree": git_metadata(repair_worktree),
        "repair_generator": {
            "path": str(script_path),
            "sha256": sha256_file(script_path),
            "generated_at_utc": generated_at,
        },
        "evaluator_label_source_chain": [
            "attempt/evaluator_result.json",
            "case-root evaluator_result.json",
            "case_result.evaluator",
        ],
        "headline_latency_rule": "unique accepted shared-baseline cases use original accepted-case summary.duration_ms; coverage reruns provide trace coverage only",
        "trace_overlay_rule": "canonical trajectory e2e_wall_ms is retained for event-coverage figures and is never substituted into the D1 headline",
        "authority_pdf_sha256": sha256_file(authority_pdf),
        "audit_scope": "read-only reuse of existing non-holdout evidence",
        "new_experiments": False,
        "new_pilot": False,
        "new_fullrun": False,
        "holdout_accessed_by_repair": False,
        "frozen_v3_modified_by_repair": False,
        "prior_snapshots_modified_by_repair": False,
        "this_script_scope": {
            "generated_d1_metadata_and_reports": True,
            "did_not_edit_generator_model_cli_in_this_agent_scope": True,
        },
        "new_worktree_overall_scope": {
            "parallel_renderer_adapter_changes_allowed": True,
            "overall_source_mutation_status": "not_asserted_by_this_repair",
        },
        "configuration_analysis_expected_path": "configuration-analysis/CONFIGURATION_ANALYSIS.md",
        "configuration_analysis_written_by_repair": False,
        "audit_dir": str(audit_dir),
    }


def build_d1_json(
    *,
    evidence: dict[str, Any],
    mapping: list[dict[str, Any]],
    mapping_path: Path,
    sources: dict[str, Any],
    provenance: dict[str, Any],
    script_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    trajectories = evidence["trajectories"]
    # The explicit D1 metrics are computed from the mapping, which is the only
    # place where the three accepted-case latency overrides are applied.
    headline_rows = []
    trace_rows = []
    for trajectory, mapped in zip(trajectories, mapping):
        merged = dict(trajectory)
        merged["headline_e2e_wall_ms"] = str(mapped["headline_e2e_wall_ms"])
        headline_rows.append(merged)
        trace_rows.append(trajectory)
    suites = {suite: suite_metrics(headline_rows, suite, "headline_e2e_wall_ms") for suite in ("lite", "verified")}
    trace_suites = {suite: suite_metrics(trace_rows, suite, "e2e_wall_ms") for suite in ("lite", "verified")}
    expected_lite = 160961.9677790433
    expected_verified = 147330.76228760785
    if not math.isclose(suites["lite"]["average_completed_e2e_wall_ms"], expected_lite, rel_tol=0, abs_tol=1e-9):
        raise RepairContractError("D1 Lite corrected headline arithmetic drifted")
    if not math.isclose(suites["verified"]["average_completed_e2e_wall_ms"], expected_verified, rel_tol=0, abs_tol=1e-9):
        raise RepairContractError("D1 Verified corrected headline arithmetic drifted")
    if not math.isclose(trace_suites["verified"]["average_completed_e2e_wall_ms"], 147814.15038279598, rel_tol=0, abs_tol=1e-9):
        raise RepairContractError("Verified trace-overlay arithmetic drifted")
    return {
        "schema_version": "assignment.d1-headline-metrics.v1",
        "latency_definition": "D1 headline is the mean original accepted-case summary.duration_ms over unique accepted, completed, shared-baseline cases; evaluator time is excluded; the three coverage reruns are not substituted into this headline.",
        "sources": sources,
        "provenance": provenance,
        "suites": suites,
        "trace_overlay_suite_metrics": {
            "latency_definition": "Trace-overlay metric is the mean canonical trajectory e2e_wall_ms used by recorded event-coverage figures. It retains coverage-rerun durations and is reported separately from D1.",
            "suites": trace_suites,
        },
        "case_source_mapping": {
            "path": str(mapping_path.relative_to(evidence["snapshot_dir"])),
            "sha256": sha256_file(mapping_path),
            "rows": len(mapping),
            "join_key": ["suite", "instance_id", "run_id", "config_id", "repeat_id"],
        },
        "headline_values_seconds": {
            "lite": 160.9619677790433,
            "verified": 147.33076228760785,
        },
        "trace_overlay_values_seconds": {
            "lite": trace_suites["lite"]["average_completed_e2e_wall_s"],
            "verified": 147.81415038279598,
        },
        "coverage_override_instances": sorted(EXPECTED_HEADLINE_OVERRIDES),
        "repair_generator_sha256": sha256_file(script_path),
        "generated_at_utc": generated_at,
    }


def build_assignment_report(
    *,
    evidence: dict[str, Any],
    d1_metrics: dict[str, Any],
    categories: list[dict[str, Any]],
    sweeps: dict[str, list[dict[str, Any]]],
    selected: dict[str, Any],
    snapshot_dir: Path,
) -> str:
    presence = downstream_presence(snapshot_dir)
    lines = [
        "# Agentic Workload Simulator — repaired evidence report",
        "",
        "This is an evidence-only repair package generated on 2026-09-08 from the preserved non-holdout inputs. No experiment, pilot, full run, model run, evaluator run, or holdout access was performed in this phase.",
        "",
        "The authoritative requirements are in `/home/riverahernandezjason/Coding tests Harrdware (2).pdf`. The canonical trajectory, sweep, tool-event, and model-event CSVs are byte-preserved copies of the 02:00Z snapshot. This repair adds the D1 headline metadata and per-case source mapping; additive sweep metadata, when present, remains permitted metadata and is not a replacement for canonical inputs.",
        "",
        "## D1 corrected headline",
        "",
        "D1 uses the original accepted-case `summary.duration_ms` definition. The evaluator is excluded from wall time. Three coverage reruns supply event traces only, so their canonical trace-overlay durations are not substituted into the headline.",
        "",
        "| Suite | Resolved | Resolved rate | Headline mean E2E (s) | Trace-overlay mean E2E (s) |",
        "|---|---:|---:|---:|---:|",
    ]
    for suite in ("lite", "verified"):
        headline = d1_metrics["suites"][suite]
        trace = d1_metrics["trace_overlay_suite_metrics"]["suites"][suite]
        lines.append(
            f"| {suite.title()} | {headline['resolved']['count']}/{headline['resolved']['denominator']} | "
            f"{headline['resolved_rate']['percent']:.12f}% | {headline['average_completed_e2e_wall_s']:.13f} | "
            f"{trace['average_completed_e2e_wall_s']:.13f} |"
        )
    lines.extend(
        [
            "",
            "Lite is 100/300 (33.333333333333%) at 160.9619677790433 s. Verified is 198/500 (39.600000000000%) at 147.33076228760785 s. The separate Verified trace-overlay value is 147.814150382796 s; it is a coverage figure metric, not the D1 outcome headline.",
            "",
            "D1 status: headline consistency is repaired. Full D1 remains partial because a same-configuration, dated scoreboard accuracy/E2E comparator is unresolved; these local cohort values do not substitute for that comparator.",
            "",
            f"The machine-readable contract is `{(snapshot_dir / 'figures-input' / 'd1_headline_metrics.json').relative_to(snapshot_dir)}` and the 800-row mapping is `{(snapshot_dir / 'figures-input' / 'd1_headline_case_mapping.csv').relative_to(snapshot_dir)}`.",
            "",
            "Evaluator labels use the recorded fallback chain `attempt/evaluator_result.json -> case-root evaluator_result.json -> case_result.evaluator`. The corrected fallback yields 800/800 one-to-one identity joins with no label mismatches.",
            "",
            "The dated official scoreboard review is recorded in [SCOREBOARD_COMPARISON.md](scoreboard-reference/SCOREBOARD_COMPARISON.md). The retrieved table has no matching SWE-agent plus selected-Qwen entry and no E2E latency field. Other-agent Qwen scores are context, not same-configuration reproductions; the local cohort rates and evaluator-excluded E2E means remain local measurements.",
            "",
            "## Scope and provenance",
            "",
            "The historical run commit and the current repair source are recorded separately in `provenance/repair_manifest.json`. The preserved run report recorded commit `d96f7ddcaa40851a7a28d724220eb07c2af3b403` on `reliability/zero-proxy-events-complete-20260907` with a clean worktree at collection. The current repair source is the dirty checkpoint at commit `2cbfbce2800892ae101460bdaac8835817cea858`; that current state is report-generation provenance and is not relabeled as the original run commit.",
            "",
            "## D4 — Step 1 observations",
            "",
            "The category field is the repository grouping used by the preserved figure inputs. The ratio is the exact recorded definition `sum(tool event wall ms) / sum(model request wall ms)`. It is a CPU/tool-to-GPU/request proxy ratio, not direct CPU device time divided by CUDA device time.",
            "",
            "| Category | N | Resolved | Accuracy | Mean E2E (ms) | Tool/model ratio |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for category in categories:
        lines.append(
            f"| {category['category']} | {category['count']} | {category['resolved_count']} | {category['accuracy_percent']:.3f}% | "
            f"{category['average_e2e_wall_ms']:.3f} | {category['tool_model_ratio']:.6f} |"
        )
    lines.extend(
        [
            "",
            "The observed category differences are compatible with different operation mixes, traversal-heavy commands, trajectory lengths, task composition, and unassigned runner/agent overhead. They do not identify a causal category law. The sealed high-ratio Django case shows the mechanism at the measured proxy level: repeated traversal events dominate its recorded tool wall while the request spans are comparatively short.",
            "",
            "A category ratio can rise through longer tool spans or shorter model requests. Repository traversal and broad test discovery can increase tool wall; long generated patches or growing prompts can increase request wall and lower the ratio even if tool work is unchanged. Libraries with numerical tests, web frameworks with setup/database work, and documentation projects with builds plausibly have different operation mixes, but these mechanisms are hypotheses here: category membership alone does not measure subprocesses, files traversed, cache state, or test scope. The table aggregates sums, so long trajectories have more influence than short ones; suite composition and early termination also change the aggregate. UNKNOWN residual is excluded from this ratio and must not be reinterpreted as CPU work.",
            "",
            f"## Figure-generation paths ({presence['figure_generation']['status']})",
            "",
            f"These links point to the expected baseline figure outputs. Current presence: {presence['figure_generation']['status']}.",
            "",
            *[f"- {link(name)}" for name in (
                "figures/step1_repository_ratio.svg",
                "figures/step1_accuracy_vs_latency.svg",
                "figures/step1_accuracy_vs_ratio.svg",
                "figures/step1_sample_latency_vs_ratio.svg",
                "figures/step2_call-limit.svg",
                "figures/step2_max-output-tokens.svg",
                "figures/step2_observation-length.svg",
                "figures/step2_temperature.svg",
                "figures/step2_combined.svg",
                "figures/step3_latency_breakdown.svg",
                "figures/step3_tool_events.svg",
                "figures/step3_model_tokens_vs_latency.svg",
            )],
            "",
            f"## Simulator-predicted figure paths ({presence['prediction_export']['status']})",
            "",
            f"These links point to the expected prediction-export figures. Current presence: {presence['prediction_export']['status']}.",
            "",
            *[f"- {link('d9-predicted/figures/' + name)}" for name in (
                "step1_repository_ratio.svg",
                "step1_accuracy_vs_latency.svg",
                "step1_accuracy_vs_ratio.svg",
                "step1_sample_latency_vs_ratio.svg",
                "step2_call-limit.svg",
                "step2_max-output-tokens.svg",
                "step2_observation-length.svg",
                "step2_temperature.svg",
                "step2_combined.svg",
                "step3_latency_breakdown.svg",
                "step3_tool_events.svg",
                "step3_model_tokens_vs_latency.svg",
            )],
            "",
            prediction_coverage(snapshot_dir)["description"],
            "",
            f"Configuration-analysis path {link('configuration-analysis/CONFIGURATION_ANALYSIS.md')} status: {presence['configuration_analysis']['status']}.",
            "",
            "## D4 — Step 2 plots",
            "",
            "The sweep table below is the numeric source for the Step 2 plots. The four sweeps are one-factor cells on a 24-task paired panel. The baseline coordinate in each parameter is a derived view of the shared-baseline cases, not an additional execution. The pooled ratio is the sum of tool wall divided by the sum of model wall within each cell.",
            "",
            "| Parameter | Value | Row source | N | Resolved | Accuracy | Mean E2E (s) | Tool/model ratio |",
            "|---|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for parameter, settings in sweeps.items():
        for setting in settings:
            lines.append(
                f"| {parameter} | {setting['value']} | {setting['row_source']} | {setting['count']} | "
                f"{setting['resolved_count']} | {setting['accuracy_percent']:.3f}% | "
                f"{setting['average_e2e_wall_ms'] / 1000.0:.3f} | {setting['tool_model_ratio']:.6f} |"
            )
    lines.extend(
        [
            "",
            "## D6 — Step 2 observations",
            "",
            "On this small paired panel, `call_limit=10` resolves 0/24 tasks; 20 resolves 13/24, and 30 and 50 each resolve 16/24, while 50 costs more wall time. The outcome rows alone do not prove the exact termination cause of every failed task. `max_output_tokens=512` is the weak accuracy cell; larger values trade latency and resolution. Observation length shows a lower mean at 25,000 characters with the same observed resolved count as the baseline panel, but the paired median improvement is much smaller than the mean, so the result is tail-sensitive. Temperature varies over a single run per task and is weak evidence of a setting effect.",
            "",
            "Mechanistically, the call limit bounds opportunities for reasoning, repair, and verification; extra turns can add both tool work and model requests without guaranteeing a better patch. The output cap limits generation per request, and may shorten patches or commands as well as decode time; the cap is not the actual output-token count. Observation truncation changes both prompt volume and the information available to the agent, potentially changing later actions. Temperature changes sampling and therefore the whole trajectory, not a hardware speed parameter. These explanations are conditional, not causal estimates from one run per cell. The original 100,000-character observation baseline remains fixed for instrumentation validation; the separate 25,000-character analysis is not adopted as a new baseline.",
            "",
            "The one-factor results support bounded observations only. There are no repeats per cell, the panel is category-imbalanced, early termination can reduce wall time, and a combined setting has not been measured. These limitations are carried forward from `final-review/CONFIGURATION_SWEEP_EVIDENCE.md`; no combined configuration is claimed.",
            "",
            "## D7 recorded timing coverage and D8 selected-instance explanation",
            "",
            f"The sealed selection is `{selected['instance_id']}` (`{selected['suite']}`, `{selected['category']}`), with {selected['tool_event_count']} recorded tool events and {selected['model_event_count']} recorded model requests. The exact event ledger and token rows are reproduced below and in `TIMING_COVERAGE.md`.",
            "",
            "### E2E accounting",
            "",
            "| Component | Wall time (ms) | Share of E2E |",
            "|---|---:|---:|",
            f"| Recorded tool-event spans | {selected['tool_wall_ms']:.6f} | {selected['tool_share_of_e2e_percent']:.6f}% |",
            f"| Recorded model-request spans | {selected['model_wall_ms']:.6f} | {selected['model_share_of_e2e_percent']:.6f}% |",
            f"| UNKNOWN residual E2E (`E2E - tool - model`) | {selected['residual_wall_ms']:.6f} | {selected['residual_share_of_e2e_percent']:.6f}% |",
            f"| E2E total | {selected['e2e_wall_ms']:.6f} | 100.000000% |",
            "",
            "The residual is explicitly unknown. It is not assigned to CPU, GPU, CUDA, Docker, SWE-agent, evaluator, or any other phase because those boundaries were not recorded for this row. The event rows are request/tool proxy spans; they are not a CUDA timeline or a measurement of GPU device time. This historical subtraction is a bookkeeping remainder: without interval endpoints it cannot establish an overlap-free lifecycle partition. New v2 closure must use interval unions and must report measured phase coverage separately from unknown residual.",
            "",
            "### Recorded tool-event ledger",
            "",
            "| Ordinal | Event ID | Operation | Status | Wall (ms) | Share of tool wall |",
            "|---:|---|---|---|---:|---:|",
        ]
    )
    for item in selected["tool_ledger"]:
        lines.append(
            f"| {item['ordinal']} | `{item['event_id']}` | {item['operation_class']} | {item['status']} | {item['wall_ms']:.6f} | {item['share_of_tool_wall_percent']:.6f}% |"
        )
    lines.extend(
        [
            "",
            "| Operation class | Events | Sum wall (ms) | Share of tool wall |",
            "|---|---:|---:|---:|",
        ]
    )
    for item in selected["tool_classes"]:
        lines.append(
            f"| {item['operation_class']} | {item['events']} | {item['sum_wall_ms']:.6f} | {item['share_of_tool_wall_percent']:.6f}% |"
        )
    lines.extend(
        [
            "",
            "### Recorded model-request/token ledger",
            "",
            "| Ordinal | Request ID | Status | Input tokens | Output tokens | Context tokens | Wall (ms) |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in selected["model_ledger"]:
        lines.append(
            f"| {item['ordinal']} | `{item['request_id']}` | {item['status']} | {item['input_tokens']} | {item['output_tokens']} | {item['context_tokens']} | {item['wall_ms']:.6f} |"
        )
    lines.extend(
        [
            "",
            f"The recorded model requests contain {selected['input_token_total']} input tokens total (mean {selected['input_token_mean']:.3f}, range {selected['input_token_min']}–{selected['input_token_max']}) and {selected['output_token_total']} output tokens total (mean {selected['output_token_mean']:.3f}, range {selected['output_token_min']}–{selected['output_token_max']}). Context tokens have the same recorded range here. Request wall ranges from {selected['model_wall_min']:.6f} to {selected['model_wall_max']:.6f} ms. Context/request-wall Pearson correlation is {selected['context_wall_pearson']:.6f}; output/request-wall correlation is {selected['output_wall_pearson']:.6f}. These are descriptive values for one trajectory, not a fitted latency law.",
            "",
            "### D8 — CPU operation interpretation",
            "",
            "Seven recorded traversal-class commands account for 40,639.533016 ms (98.069394% of tool wall); three read-class commands account for 555.297428 ms (1.340017%); two search-class commands account for 244.737437 ms (0.590589%). The individual durations above identify which observed operations dominate. A traversal command may include enumeration, metadata lookup, process launch, or work invoked through `find -exec`; its wall span is not a measured per-file traversal cost. Read/search spans may include interpreter and shell startup and cached I/O. There is no separately recorded write-class event for this selection, which does not prove no file was written inside another command. Without measured bytes/files and chronological script state, splitting these spans into filesystem bandwidth, read/write service time, or subprocess counts would invent work volumes. The 53,007.194846 ms UNKNOWN remainder remains outside this operation attribution.",
            "",
            "### D8 — GPU tokens, context, model size, and bandwidth",
            "",
            "Input tokens describe prompt processing; output tokens describe generated decoding steps. Context tokens represent the recorded input-side context in this historical ledger and are not an additional disjoint token volume to add to input tokens. During autoregressive decoding, the attention history grows with generated tokens. Longer prompts can increase prefill work; longer output can add serial decode steps; longer attention history can increase KV-cache traffic. The ledger correlations above cannot separate these effects from queueing, batching, prefix caching, transport, or client overhead, because only total request spans were recorded. Output length is an observed workload descriptor for conditional replay, not information available before a new request completes. A prospective predictor must use an explicit output-length forecast or cap and cannot silently substitute realized holdout output lengths.",
            "",
            "For a conditional bandwidth sanity check, let P be parameter count, b bytes per stored weight, and B_eff effective memory bandwidth. Weight storage is approximately P*b; a simplified weight-streaming decode scale is W_touched/B_eff per token. Under the historical illustrative BF16 assumption, 30 billion total parameters require about 60 GB of weights, whereas a nominal 3 billion active-parameter count corresponds to about 6 GB. These are different quantities: mixture-of-experts residency includes inactive experts, while weights touched per token depend on routing, shared layers, caching, and batching. KV cache and serving buffers add VRAM beyond weights. Precision affects weight bytes and available compute kernels; neither the actual runtime dtype nor active traffic is measured by these CSVs.",
            "",
            "Using the historical illustrative H100 peak of 3.35 TB/s gives 6 GB / 3.35 TB/s = 1.79 ms, or 60 GB / 3.35 TB/s = 17.91 ms, for the respective weight-streaming assumptions. Peak bandwidth is not measured effective bandwidth, and these scales are not predictions of a whole request. A request model needs queue time plus prefill plus decode plus client/transport effects; a roofline-style service approximation considers max(FLOPs/effective compute, bytes/effective bandwidth), with compute and traffic changing across prefill and decode. Inferring bandwidth by dividing assumed weight traffic by request wall conflates all these phases and is not a hardware measurement. No CUDA kernel timing, reliable queue/prefill/decode decomposition, device interval, dtype verification, or physical bandwidth measurement is present. This detailed discussion satisfies the explanatory scope only; empirical attribution and portability remain incomplete.",
            "",
            "## D9 limitations carried into compliance",
            "",
            "The frozen v3 checkpoint remains a conditional calibration reference. Its reported semantic-median result is 77.210% of CPU events within 25%, overhead-aware E2E is 59.37% within 25%, and only 1/1,083 retained trajectories passes every recorded CPU event, GPU event, and overhead-aware E2E gate. The reviewed evidence also attributes 55.3% of E2E mass to unassigned residual timing. These figures do not satisfy the assignment's universal 25% requirement; a cohort average or an individual passing trajectory cannot replace the every-event and E2E gate.",
            "",
            "Portability is not established by one H100 environment. The CPU model is an empirical conditional model, the GPU formulation relies on logged token descriptors, and storage, serial execution, runner availability, and hardware transfer effects are not independently identified across platforms. The universal 25% threshold is frozen and unchanged.",
            "",
            "## Repair artifacts",
            "",
            f"- {link('COMPLIANCE.md')}",
            f"- {link('TIMING_COVERAGE.md')}",
            f"- {link('provenance/repair_manifest.json')}",
            f"- {link('figures-input/d1_headline_metrics.json')}",
            f"- {link('figures-input/d1_headline_case_mapping.csv')}",
            "",
        ]
    )
    return "\n".join(lines)


def build_timing_report(selected: dict[str, Any]) -> str:
    lines = [
        "# Timing coverage report",
        "",
        "This report describes the recorded event coverage for the sealed Step 3 trajectory. It reuses the new snapshot's canonical event CSVs and does not add timing measurements.",
        "",
        *[
            f"- Instance: `{selected['instance_id']}`",
            f"- Run: `{selected['run_id']}`",
            f"- Recorded tool events: {selected['tool_event_count']}/{selected['tool_event_count']}",
            f"- Recorded model requests: {selected['model_event_count']}/{selected['model_event_count']}",
            "- E2E source: canonical trajectory `e2e_wall_ms`",
            "- Official evaluator: excluded from E2E wall time",
        ],
        "",
        "## Accounting",
        "",
        "| Component | Wall time (ms) | Share of E2E |",
        "|---|---:|---:|",
        f"| Recorded tool/proxy spans | {selected['tool_wall_ms']:.6f} | {selected['tool_share_of_e2e_percent']:.6f}% |",
        f"| Recorded model/request-proxy spans | {selected['model_wall_ms']:.6f} | {selected['model_share_of_e2e_percent']:.6f}% |",
        f"| UNKNOWN residual (`E2E - recorded tool - recorded model`) | {selected['residual_wall_ms']:.6f} | {selected['residual_share_of_e2e_percent']:.6f}% |",
        f"| E2E total | {selected['e2e_wall_ms']:.6f} | 100.000000% |",
        "",
        "## Coverage interpretation",
        "",
        "The 12 tool rows and 12 model rows are complete relative to the selected CSV ledgers. That is a ledger-completeness statement, not proof that every actual CPU or GPU event occurred inside those spans. Tool `wall_ms` values are recorded operation/proxy spans. Model `wall_ms` values are request/proxy wall times with input, output, and context token fields. They are not CUDA event time, GPU device utilization, or a kernel timeline.",
        "",
        "The residual is intentionally UNKNOWN. It may contain agent processing, state queries, request gaps, startup/teardown, proxy and container overhead, or other phases, but this artifact does not partition it. No residual component is invented or assigned to a processor.",
        "",
        "## Recorded operation classes",
        "",
        "| Operation class | Events | Sum wall (ms) | Share of tool wall |",
        "|---|---:|---:|---:|",
    ]
    for item in selected["tool_classes"]:
        lines.append(
            f"| {item['operation_class']} | {item['events']} | {item['sum_wall_ms']:.6f} | {item['share_of_tool_wall_percent']:.6f}% |"
        )
    lines.extend(
        [
            "",
            "## GPU wording boundary",
            "",
            "The report uses `model/request-proxy` wording throughout. A high request wall or a token correlation can motivate a conditional inference about model service behavior, but it cannot be presented as measured GPU execution. The frozen evidence has no CUDA/Kineto device intervals for this selected row.",
            "",
        ]
    )
    return "\n".join(lines)


def build_compliance_report(
    evidence: dict[str, Any], selected: dict[str, Any], snapshot_dir: Path
) -> str:
    presence = downstream_presence(snapshot_dir)
    figure_status = presence["figure_generation"]["status"]
    prediction_status = presence["prediction_export"]["status"]
    configuration_status = presence["configuration_analysis"]["status"]
    return "\n".join(
        [
            "# Compliance and residual blockers",
            "",
            "This matrix reports the repaired evidence package against the authoritative assignment. It records bounded evidence status and does not claim full compliance where the literal requirement remains unmet.",
            "",
            "| Deliverable | Status | Evidence and limitation |",
            "|---|---|---|",
            "| D1 | Partial — headline consistency repaired; scoreboard comparator unresolved | Corrected local cohort headlines are Lite 100/300 at 160.9619677790433 s and Verified 198/500 at 147.33076228760785 s. Full D1 remains partial because a same-configuration, dated scoreboard accuracy/E2E comparator is absent. |",
            f"| D2 | {figure_status} | The D1 JSON and 800-row mapping are ready for the optional `--d1-headline-metrics` input. The ratio remains the recorded tool/request proxy ratio. |",
            f"| D3 | {figure_status} | Figure paths are linked in `ASSIGNMENT_REPORT.md`; category grouping and individual samples remain tied to canonical rows. |",
            "| D4 (Step 1 observations) | Evidence repaired with bounded claims | Category observations are computed from existing rows. Operation mix, task composition, trajectory length, and UNKNOWN residual are discussed as explanations or limitations, not causal measurements. |",
            f"| D4 (Step 2 plots) | {figure_status} | The four one-factor plot inputs use the preserved sweep rows; no additional executions are implied. |",
            f"| D5 | {figure_status} | The combined one-factor summary remains descriptive; derived baseline coordinates are not extra runs and combined settings are not claimed. |",
            "| D6 (Step 2 observations) | Evidence repaired with bounded claims | Existing paired sweep evidence is reported with no-repeat, small-panel, early-termination, tail-sensitivity, and no-combined-setting limitations. |",
            "| D7 | Partial | Every recorded tool/model ledger row for the sealed selected instance is reproduced. This does not establish exhaustive actual CPU/GPU event capture; UNKNOWN E2E residual is explicit. |",
            "| D8 | Detailed discussion complete; empirical attribution partial | Individual recorded CPU operations and input/output/context, model size, precision, and effective-bandwidth effects are discussed. The unmeasured E2E residual prevents a complete empirical explanation. Bandwidth arithmetic is explicitly conditional; the PDF does not separately require a CUDA kernel trace or a physical bandwidth benchmark. |",
            f"| D9 | Not compliant at frozen gate; prediction export {prediction_status} | The universal 25% requirement remains unchanged. Frozen v3 evidence reports 77.210% of CPU events and 59.37% of overhead-aware E2E predictions within 25% error, and 1/1,083 all-gate trajectories; these are accuracy rates, not proof of event capture coverage. Portability across hardware is not validated. {prediction_coverage(snapshot_dir)['description']} |",
            "",
            "## Exact residual measurement blockers",
            "",
            f"- Selected D8 row `{selected['instance_id']}` has {selected['residual_wall_ms']:.6f} ms ({selected['residual_share_of_e2e_percent']:.6f}%) of E2E wall outside recorded tool and model/request-proxy sums. Its causal partition is UNKNOWN.",
            "- The event CSVs do not establish exhaustive CPU/GPU lifecycle coverage, request-gap coverage, CUDA device time, or kernel-level GPU attribution.",
            "- The original sweep has one observation per task/setting on a small, category-imbalanced panel; selection and tail sensitivity remain unresolved.",
            "- The dated official scoreboard review found no matching SWE-agent/selected-Qwen entry or published E2E field; see [scoreboard reference](scoreboard-reference/SCOREBOARD_COMPARISON.md). A different agent score or cost is not an E2E reproduction target.",
            "- The frozen v3 model does not meet the universal per-event plus E2E 25% gate, and a single H100 environment does not establish portability.",
            "- " + prediction_coverage(snapshot_dir)["description"],
            f"- Configuration-analysis artifact status at generation was {configuration_status}; the expected path is `configuration-analysis/CONFIGURATION_ANALYSIS.md`.",
            "",
            "## Preservation boundary",
            "",
            "Preservation covers prior worktrees, prior snapshots and reports, the frozen v3 checkpoint, holdout artifacts, and the copied canonical trajectory/sweep/event inputs. The new repair worktree permits parallel renderer, adapter, figure, prediction, configuration-analysis, and report edits. This manifest records this task's output boundary and does not assert the overall new-worktree mutation state.",
            "",
            "## Downstream artifact links",
            "",
            "Statuses below are derived from files present at report generation; a pending status means the expected artifact was absent at that refresh.",
            "",
            f"- [Baseline figure contract](figures/step1_repository_ratio.svg) — {figure_status}.",
            f"- [Predicted figure contract](d9-predicted/figures/step1_repository_ratio.svg) — {prediction_status}.",
            f"- [Configuration analysis contract](configuration-analysis/CONFIGURATION_ANALYSIS.md) — {configuration_status}.",
            "",
        ]
    )


def build_manifest(
    *,
    evidence: dict[str, Any],
    snapshot_dir: Path,
    generated_at: str,
    script_path: Path,
    output_paths: list[Path],
    sources: dict[str, Any],
    provenance: dict[str, Any],
    command: list[str],
) -> dict[str, Any]:
    generated_files = [file_record(path, root=snapshot_dir, role="repair output") for path in output_paths]
    downstream = downstream_presence(snapshot_dir)
    return {
        "schema_version": "assignment.repair-manifest.v1",
        "generated_at_utc": generated_at,
        "command": command,
        "write_scope": {
            "repair_worktree": str(REPAIR_WORKTREE),
            "snapshot": str(snapshot_dir),
            "this_task_outputs_only": True,
            "parallel_new_worktree_edits_allowed": True,
            "overall_new_worktree_mutation_status": "not_asserted_by_this_repair_manifest",
            "protected_prior_snapshot": str(evidence["prior_snapshot"]),
            "holdout_accessed": False,
            "frozen_v3_modified": False,
        },
        "generated_files": generated_files,
        "manifest_self_hash": {
            "path": "provenance/repair_manifest.json",
            "sidecar": "provenance/repair_manifest.json.sha256",
            "included_in_sidecar_only": True,
        },
        "protected_input_sha256_before_and_after": evidence["input_digests"],
        "canonical_inputs_match_prior_0200z": True,
        "additive_metadata": [
            file_record(snapshot_dir / "figures-input" / name, root=snapshot_dir, role="allowed additive metadata")
            for name in ADDITIVE_INPUT_NAMES
            if (snapshot_dir / "figures-input" / name).is_file()
        ],
        "sources": sources,
        "provenance": provenance,
        "downstream_interfaces": {
            "figure_generation": {
                "input": "figures-input/d1_headline_metrics.json",
                "optional_cli_flag": "--d1-headline-metrics",
                "headline_object": "suites",
                "trace_overlay_object": "trace_overlay_suite_metrics",
                "status": downstream["figure_generation"],
            },
            "prediction_export": {
                "output_directory": "d9-predicted/",
                **prediction_coverage(snapshot_dir),
                "derived_sweep_baseline_rule": "reuse the corresponding source baseline prediction",
                "status": downstream["prediction_export"],
            },
            "configuration_analysis": {
                "expected_report": "configuration-analysis/CONFIGURATION_ANALYSIS.md",
                "status": downstream["configuration_analysis"],
            },
        },
        "validation": {
            "trajectory_rows": len(evidence["trajectories"]),
            "sweep_rows": len(evidence["sweeps"]),
            "tool_event_rows": len(evidence["tool_events"]),
            "model_event_rows": len(evidence["model_events"]),
            "evaluator_provenance_rows": len(evidence["evaluator_rows"]),
            "selected_instance": evidence["selected_row"]["instance_id"],
            "selected_tool_events": len(evidence["selected_tools"]),
            "selected_model_events": len(evidence["selected_models"]),
            "d1_override_instances": sorted(EXPECTED_HEADLINE_OVERRIDES),
            "scoreboard_comparator": "unresolved; no same-configuration dated comparator in evidence",
            "new_modeling_runs": False,
        },
        "script": file_record(script_path, role="repair generator"),
    }


def output_paths(snapshot_dir: Path) -> dict[str, Path]:
    return {
        "d1_json": snapshot_dir / "figures-input" / "d1_headline_metrics.json",
        "d1_csv": snapshot_dir / "figures-input" / "d1_headline_case_mapping.csv",
        "assignment_report": snapshot_dir / "ASSIGNMENT_REPORT.md",
        "compliance": snapshot_dir / "COMPLIANCE.md",
        "timing": snapshot_dir / "TIMING_COVERAGE.md",
        "manifest": snapshot_dir / "provenance" / "repair_manifest.json",
    }


def check_generated_outputs(snapshot_dir: Path, prior_snapshot: Path) -> None:
    paths = output_paths(snapshot_dir)
    for path in paths.values():
        require_file(path, "generated repair output")
    for path in (paths["d1_json"], paths["d1_csv"]):
        verify_sidecar(path)
    verify_sidecar(paths["manifest"])
    d1 = json_load(paths["d1_json"])
    if d1.get("schema_version") != "assignment.d1-headline-metrics.v1":
        raise RepairContractError("generated D1 JSON schema version mismatch")
    if d1["suites"]["lite"]["resolved"] != {"count": 100, "denominator": 300}:
        raise RepairContractError("generated Lite D1 resolved metric mismatch")
    if d1["suites"]["verified"]["resolved"] != {"count": 198, "denominator": 500}:
        raise RepairContractError("generated Verified D1 resolved metric mismatch")
    if not math.isclose(d1["suites"]["verified"]["average_completed_e2e_wall_s"], 147.33076228760785, rel_tol=0, abs_tol=1e-12):
        raise RepairContractError("generated Verified headline latency mismatch")
    mapping = read_csv(paths["d1_csv"], tuple(build_d1_mapping_columns()))
    if len(mapping) != 800:
        raise RepairContractError(f"generated D1 mapping row count mismatch: {len(mapping)}")
    verify_copied_inputs(snapshot_dir, prior_snapshot)


def build_d1_mapping_columns() -> list[str]:
    return [
        "trajectory_row_number",
        "suite",
        "instance_id",
        "run_id",
        "config_id",
        "repeat_id",
        "submitted",
        "official_resolved",
        "evaluator_source_kind",
        "evaluator_source_path",
        "evaluator_source_sha256",
        "headline_e2e_wall_ms",
        "headline_e2e_wall_s",
        "headline_latency_source_kind",
        "headline_latency_source_path",
        "headline_latency_source_sha256",
        "headline_latency_source_detail",
        "trace_overlay_e2e_wall_ms",
        "trace_overlay_e2e_wall_s",
        "trace_overlay_delta_ms",
        "trace_overlay_definition",
        "is_coverage_rerun_key",
    ]


def generate(args: argparse.Namespace) -> None:
    snapshot_dir = require_file(args.snapshot_dir / "figures-input" / "trajectories.csv", "new snapshot")
    del snapshot_dir
    snapshot_dir = args.snapshot_dir
    prior_snapshot = args.prior_snapshot
    audit_dir = args.audit_dir
    authority_pdf = require_file(args.authority_pdf, "authority PDF")
    require_file(audit_dir / "D1_RECOVERED_AUDIT.md", "D1 audit")
    require_file(audit_dir / "FINAL_D1_D9_AUDIT_AND_PLAN.md", "final audit")
    require_file(audit_dir / "FIGURES_SWEEPS_RECOVERED_AUDIT.md", "figures audit")
    require_file(audit_dir / "CONFIGURATION_SWEEP_EVIDENCE.md", "configuration evidence")
    overrides = parse_override_specs(args.headline_latency_source)
    evidence = load_evidence(snapshot_dir, prior_snapshot, overrides)
    categories = aggregate_categories(evidence["trajectories"])
    sweeps = aggregate_sweeps(evidence["sweeps"])
    selected = selected_event_evidence(evidence)
    generated_at = utc_now()
    script_path = Path(__file__).resolve()
    paths = output_paths(snapshot_dir)
    mapping = build_d1_mapping(evidence)
    mapping_columns = tuple(build_d1_mapping_columns())
    mapping_payload = csv_text(mapping, mapping_columns)
    if args.reuse_d1_metadata:
        verify_sidecar(paths["d1_csv"])
        verify_sidecar(paths["d1_json"])
        if paths["d1_csv"].read_text() != mapping_payload:
            raise RepairContractError("existing D1 mapping no longer matches source evidence")
        d1_json = json_load(paths["d1_json"])
        if d1_json.get("repair_generator_sha256") != sha256_file(script_path):
            raise RepairContractError("D1 metadata predates current report generator; regenerate once before exporting figures")
        if d1_json["case_source_mapping"]["sha256"] != sha256_file(paths["d1_csv"]):
            raise RepairContractError("existing D1 metadata has a stale mapping hash")
        d1_csv_sidecar = paths["d1_csv"].with_name(paths["d1_csv"].name + ".sha256")
        d1_json_sidecar = paths["d1_json"].with_name(paths["d1_json"].name + ".sha256")
    else:
        write_text(paths["d1_csv"], mapping_payload, force=args.force)
        d1_sources = build_sources(evidence, audit_dir, authority_pdf)
        d1_provenance = build_provenance(
            evidence=evidence,
            audit_dir=audit_dir,
            authority_pdf=authority_pdf,
            script_path=script_path,
            prior_report_path=evidence["prior_report_path"],
            source_worktree=args.source_worktree,
            repair_worktree=args.repair_worktree,
            generated_at=generated_at,
        )
        d1_json = build_d1_json(
            evidence=evidence,
            mapping=mapping,
            mapping_path=paths["d1_csv"],
            sources=d1_sources,
            provenance=d1_provenance,
            script_path=script_path,
            generated_at=generated_at,
        )
        write_json(paths["d1_json"], d1_json, force=args.force)
        d1_csv_sidecar = write_hash_sidecar(paths["d1_csv"], force=args.force)
        d1_json_sidecar = write_hash_sidecar(paths["d1_json"], force=args.force)
    report_payload = build_assignment_report(
        evidence=evidence,
        d1_metrics=d1_json,
        categories=categories,
        sweeps=sweeps,
        selected=selected,
        snapshot_dir=snapshot_dir,
    )
    write_text(paths["assignment_report"], report_payload, force=args.force)
    compliance_payload = build_compliance_report(evidence, selected, snapshot_dir)
    write_text(paths["compliance"], compliance_payload, force=args.force)
    timing_payload = build_timing_report(selected)
    write_text(paths["timing"], timing_payload, force=args.force)
    output_for_manifest = [
        paths["d1_json"],
        d1_json_sidecar,
        paths["d1_csv"],
        d1_csv_sidecar,
        paths["assignment_report"],
        paths["compliance"],
        paths["timing"],
    ]
    sources = build_sources(evidence, audit_dir, authority_pdf)
    provenance = build_provenance(
        evidence=evidence,
        audit_dir=audit_dir,
        authority_pdf=authority_pdf,
        script_path=script_path,
        prior_report_path=evidence["prior_report_path"],
        source_worktree=args.source_worktree,
        repair_worktree=args.repair_worktree,
        generated_at=generated_at,
    )
    manifest = build_manifest(
        evidence=evidence,
        snapshot_dir=snapshot_dir,
        generated_at=generated_at,
        script_path=script_path,
        output_paths=output_for_manifest,
        sources=sources,
        provenance=provenance,
        command=sys.argv,
    )
    write_json(paths["manifest"], manifest, force=args.force)
    manifest_sidecar = write_hash_sidecar(paths["manifest"], force=args.force)
    validate_protected_inputs(snapshot_dir, evidence["input_digests"])
    print(json.dumps({
        "status": "generated",
        "d1_json": str(paths["d1_json"]),
        "d1_case_mapping": str(paths["d1_csv"]),
        "assignment_report": str(paths["assignment_report"]),
        "compliance": str(paths["compliance"]),
        "timing_coverage": str(paths["timing"]),
        "repair_manifest": str(paths["manifest"]),
        "repair_manifest_sha256_sidecar": str(manifest_sidecar),
        "headline": {
            "lite": d1_json["headline_values_seconds"]["lite"],
            "verified": d1_json["headline_values_seconds"]["verified"],
        },
        "trace_overlay_verified_seconds": d1_json["trace_overlay_values_seconds"]["verified"],
    }, indent=2, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--prior-snapshot", type=Path, default=PRIOR_SNAPSHOT)
    parser.add_argument("--audit-dir", type=Path, default=AUDIT_DIR)
    parser.add_argument("--authority-pdf", type=Path, default=AUTHORITY_PDF)
    parser.add_argument("--repair-worktree", type=Path, default=REPAIR_WORKTREE)
    parser.add_argument("--source-worktree", type=Path, default=SOURCE_WORKTREE)
    parser.add_argument(
        "--headline-latency-source",
        action="append",
        default=None,
        metavar="INSTANCE_ID=PATH",
        help="accepted-case summary.json for each audited coverage-rerun identity",
    )
    parser.add_argument("--force", action="store_true", help="overwrite only this script's repair outputs")
    parser.add_argument("--reuse-d1-metadata", action="store_true", help="refresh reports after figure export without rewriting verified D1 metadata/source hashes")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate generated repair outputs and preserved canonical inputs without writing",
    )
    args = parser.parse_args(argv)
    if args.headline_latency_source is None:
        args.headline_latency_source = list(DEFAULT_OVERRIDE_SPECS)
    if args.check_only:
        verify_copied_inputs(args.snapshot_dir, args.prior_snapshot)
        check_generated_outputs(args.snapshot_dir, args.prior_snapshot)
        print(json.dumps({"status": "check-passed", "snapshot_dir": str(args.snapshot_dir)}, indent=2))
        return args
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if not args.check_only:
            generate(args)
        return 0
    except (RepairContractError, FileExistsError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
