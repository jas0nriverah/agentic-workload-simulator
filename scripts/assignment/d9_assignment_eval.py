#!/usr/bin/env python3
"""Evaluate the assignment-level D9 event-latency simulator on held-out data.

Step 3 logs CPU events and GPU events (input tokens, output tokens, context
length). Step 4 uses those event models with configurable hardware parameters.
This script fits that formulation on calibration trajectories, then scores a
single designated holdout once. The 25% per-event and E2E gate is unchanged.

Does not execute rerun_holdout.py. Does not modify the sealed online experiment.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentic_sim.assignment.event_simulator import (  # noqa: E402
    ModelEventInput,
    ToolEventInput,
)
from agentic_sim.assignment.tool_features import extract_tool_features  # noqa: E402
from agentic_sim.assignment.workload_simulator import (  # noqa: E402
    GATE_PERCENT,
    WorkloadSimulator,
    score_channel,
)

ASSIGN = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
STAMP = "20260908T041000Z"
OUT = ASSIGN / "submission" / STAMP / "d9-assignment"
HOLDOUT_RUN = "assignment-case-v1:8bf8546c12056ce716b300fa4f27abf5bb431fd226bb6c49790a6c60e1eeee2c"
PROTO = ASSIGN / "submission/20260908T020000Z/protocol-input"
REC_ROOTS = (
    ASSIGN / "full-matrix-recovery-authenticated-resume-20260906T025908Z-16",
    ASSIGN / "remaining-failed-486-cpu-docker-20260907T012757Z-16",
    ASSIGN / "remaining-failed-a42a993-cpu-docker-20260907T182200Z-16",
    ASSIGN / "remaining-failed-471a3bf-cpu-docker-20260907T184200Z-16",
    ASSIGN / "remaining-failed-06d0167-cpu-docker-20260907T193200Z-16",
    ASSIGN / "remaining-failed-c19-rebalance-20260907T194200Z-16",
    ASSIGN / "remaining-failed-c19-finish-last-20260907T201600Z",
)
FOLDS = 5
HARDWARE = {
    "schema_version": "assignment.hardware-profile.v1",
    "hardware_id": "h100-80gb-pace",
    "architecture": "x86_64-h100-80gb",
    "cpu_cores": 16,
    "cpu_threads": 32,
    "cpu_base_ghz": 2.8,
    "system_memory_gib": 128.0,
    "storage_read_mbps": 500.0,
    "storage_write_mbps": 500.0,
    "gpu_count": 1,
    "gpu_compute_capability": 9.0,
    "gpu_memory_gib": 80.0,
    "gpu_memory_bandwidth_gbps": 3350.0,
    "gpu_bf16_tflops": 989.4,
}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    path.write_bytes(payload)
    digest = sha256_bytes(payload)
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def fold_of(run_id: str) -> int:
    return int(hashlib.sha256(run_id.encode()).hexdigest(), 16) % FOLDS


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_case_results(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    for index in range(32):
        worker = root / f"worker-{index:02d}"
        cases = worker / "cases"
        if cases.exists():
            found.extend(sorted(cases.glob("*/case_result.json")))
    found.extend(sorted(root.glob("overflow/runs/*/cases/*/case_result.json")))
    found.extend(sorted(root.glob("overflow-on-*/cases/*/case_result.json")))
    if (root / "cases").exists():
        found.extend(sorted((root / "cases").glob("*/case_result.json")))
    return found


def latest_attempt(case_dir: Path, result: dict[str, Any]) -> Path | None:
    runner = result.get("runner") if isinstance(result.get("runner"), dict) else {}
    output_dir = runner.get("output_dir")
    if isinstance(output_dir, str) and output_dir:
        path = case_dir / output_dir
        if path.is_dir():
            return path
    attempts = sorted((case_dir / "runner_attempts").glob("attempt-*"), reverse=True)
    return attempts[0] if attempts else None


def find_traj(attempt: Path, instance_id: str) -> Path | None:
    direct = attempt / instance_id / f"{instance_id}.traj"
    if direct.is_file():
        return direct
    hits = list(attempt.glob(f"**/{instance_id}.traj"))
    return hits[0] if hits else None


def rebuild_events() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    protocol_traj = {row["run_id"]: row for row in read_csv(PROTO / "trajectories.csv")}
    protocol_models = read_csv(PROTO / "model_events.csv")
    tools: list[dict[str, Any]] = []
    for root in REC_ROOTS:
        for case_path in iter_case_results(root):
            result = load_json(case_path)
            spec_path = case_path.parent / "case_spec.json"
            if not spec_path.is_file():
                continue
            spec = load_json(spec_path)
            instance_id = spec.get("instance_id")
            if not isinstance(instance_id, str):
                continue
            attempt = latest_attempt(case_path.parent, result)
            if attempt is None:
                continue
            traj_path = find_traj(attempt, instance_id)
            if traj_path is None:
                continue
            resume_key = result.get("resume_key") or spec.get("resume_key") or ""
            run_id = str(resume_key)
            if run_id not in protocol_traj:
                continue
            trajectory = load_json(traj_path)
            steps = trajectory.get("trajectory")
            if not isinstance(steps, list):
                continue
            for ordinal, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                action = step.get("action")
                seconds = step.get("execution_time")
                if not isinstance(action, str) or not action.strip():
                    continue
                if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
                    continue
                extracted = extract_tool_features(action)
                tools.append(
                    {
                        "run_id": run_id,
                        "event_id": f"{run_id}-tool-{ordinal:04d}",
                        "split": "holdout" if run_id == HOLDOUT_RUN else "calibration",
                        "tool_name": extracted.tool_name,
                        "subcommand": extracted.subcommand,
                        "command_prefix": extracted.command_prefix,
                        "operation_class": extracted.operation_class,
                        "declared_command_bytes": extracted.declared_command_bytes,
                        "declared_path_count": extracted.declared_path_count,
                        "has_pipe": extracted.has_pipe,
                        "has_glob": extracted.has_glob,
                        "command_sha256": extracted.command_sha256,
                        "extractor_id": extracted.extractor_id,
                        "extractor_sha256": extracted.extractor_sha256,
                        "observed_ms": float(seconds) * 1000.0,
                    }
                )
    models: list[dict[str, Any]] = []
    skipped_tokens = 0
    tool_runs = {row["run_id"] for row in tools}
    for row in protocol_models:
        if row["run_id"] not in tool_runs:
            continue
        if row.get("status") != "completed":
            continue
        try:
            output_tokens = int(row["output_tokens"])
            input_tokens = int(row["input_tokens"])
            context_tokens = int(row["context_tokens"])
        except (TypeError, ValueError):
            skipped_tokens += 1
            continue
        models.append(
            {
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "split": "holdout" if row["run_id"] == HOLDOUT_RUN else "calibration",
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "context_tokens": context_tokens,
                "max_output_tokens": int(row["max_output_tokens"] or 2048),
                "observed_ms": float(row["wall_ms"]),
            }
        )
    run_ids = sorted({row["run_id"] for row in tools} & {row["run_id"] for row in models})
    tools = [row for row in tools if row["run_id"] in run_ids]
    models = [row for row in models if row["run_id"] in run_ids]
    unique_tools: dict[tuple[str, str], dict[str, Any]] = {}
    for row in tools:
        unique_tools[(row["run_id"], row["event_id"])] = row
    tools = list(unique_tools.values())
    unique_models: dict[tuple[str, str], dict[str, Any]] = {}
    for row in models:
        unique_models[(row["run_id"], row["request_id"])] = row
    models = list(unique_models.values())
    trajs = []
    for run_id in run_ids:
        proto = protocol_traj[run_id]
        trajs.append(
            {
                "run_id": run_id,
                "split": "holdout" if run_id == HOLDOUT_RUN else "calibration",
                "instance_id": proto.get("instance_id"),
                "observed_ms": float(proto["e2e_wall_ms"]),
            }
        )
    inventory = {
        "n_tools": len(tools),
        "n_models": len(models),
        "n_trajectories": len(trajs),
        "holdout_run_id": HOLDOUT_RUN,
        "holdout_instance_id": protocol_traj.get(HOLDOUT_RUN, {}).get("instance_id"),
        "holdout_present": HOLDOUT_RUN in run_ids,
        "skipped_model_rows_missing_tokens": skipped_tokens,
        "cd_as_tool_count": sum(1 for row in tools if row["tool_name"] == "cd"),
        "formulation": "assignment_pdf_step3_event_models_plus_hardware",
        "gate_percent": GATE_PERCENT,
    }
    return tools, models, trajs, inventory


def tool_input(row: dict[str, Any]) -> ToolEventInput:
    return ToolEventInput.from_mapping(
        {
            "schema_version": "assignment.tool-event-input.v1",
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "split": row["split"],
            "operation_class": row["operation_class"],
            "declared_command_bytes": int(row["declared_command_bytes"]),
            "declared_read_bytes": 0,
            "declared_write_bytes": 0,
            "declared_path_count": int(row["declared_path_count"]),
            "hardware": HARDWARE,
            "tool_name": row["tool_name"],
            "subcommand": row["subcommand"],
            "command_prefix": row["command_prefix"],
            "command_sha256": row["command_sha256"],
            "has_pipe": int(row["has_pipe"]),
            "has_glob": int(row["has_glob"]),
            "extractor_id": row["extractor_id"],
            "extractor_sha256": row["extractor_sha256"],
        }
    )


def model_input(row: dict[str, Any]) -> ModelEventInput:
    return ModelEventInput.from_mapping(
        {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": row["request_id"],
            "run_id": row["run_id"],
            "split": row["split"],
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "context_tokens": int(row["context_tokens"]),
            "max_output_tokens": int(row["max_output_tokens"]),
            "hardware": HARDWARE,
        }
    )


def subset(
    tools: list[dict[str, Any]],
    models: list[dict[str, Any]],
    trajs: list[dict[str, Any]],
    run_ids: set[str],
    split: str,
) -> tuple[
    list[tuple[ToolEventInput, float]],
    list[tuple[ModelEventInput, float]],
    list[tuple[str, float]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    tool_rows = [dict(row, split=split) for row in tools if row["run_id"] in run_ids]
    model_rows = [dict(row, split=split) for row in models if row["run_id"] in run_ids]
    traj_rows = [dict(row, split=split) for row in trajs if row["run_id"] in run_ids]
    return (
        [(tool_input(row), float(row["observed_ms"])) for row in tool_rows],
        [(model_input(row), float(row["observed_ms"])) for row in model_rows],
        [(row["run_id"], float(row["observed_ms"])) for row in traj_rows],
        tool_rows,
        model_rows,
        traj_rows,
    )


def evaluate(
    simulator: WorkloadSimulator,
    tool_rows: list[dict[str, Any]],
    model_rows: list[dict[str, Any]],
    traj_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    tool_pairs: list[tuple[float, float]] = []
    model_pairs: list[tuple[float, float]] = []
    tool_detail = []
    model_detail = []
    by_run: dict[str, dict[str, float]] = defaultdict(
        lambda: {"tool_ms": 0.0, "model_ms": 0.0, "tools": 0.0, "models": 0.0}
    )
    for row in tool_rows:
        features = tool_input(row)
        predicted = simulator.predict_tool_ms(features)
        observed = float(row["observed_ms"])
        ape = abs(predicted - observed) / max(observed, 1e-9) * 100.0
        tool_pairs.append((predicted, observed))
        tool_detail.append(
            {
                "event_id": row["event_id"],
                "run_id": row["run_id"],
                "operation_class": row["operation_class"],
                "predicted_ms": predicted,
                "observed_ms": observed,
                "ape": ape,
                "within_25": ape <= GATE_PERCENT,
            }
        )
        by_run[row["run_id"]]["tool_ms"] += predicted
        by_run[row["run_id"]]["tools"] += 1.0
    for row in model_rows:
        features = model_input(row)
        predicted = simulator.predict_model_ms(features)
        observed = float(row["observed_ms"])
        ape = abs(predicted - observed) / max(observed, 1e-9) * 100.0
        model_pairs.append((predicted, observed))
        model_detail.append(
            {
                "request_id": row["request_id"],
                "run_id": row["run_id"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "context_tokens": row["context_tokens"],
                "predicted_ms": predicted,
                "observed_ms": observed,
                "ape": ape,
                "within_25": ape <= GATE_PERCENT,
            }
        )
        by_run[row["run_id"]]["model_ms"] += predicted
        by_run[row["run_id"]]["models"] += 1.0
    e2e_pairs: list[tuple[float, float]] = []
    e2e_detail = []
    all_event_pass = 0
    for row in traj_rows:
        item = by_run[row["run_id"]]
        predicted = simulator.predict_e2e_ms(
            item["tool_ms"], item["model_ms"], item["tools"], item["models"]
        )
        observed = float(row["observed_ms"])
        ape = abs(predicted - observed) / max(observed, 1e-9) * 100.0
        e2e_pairs.append((predicted, observed))
        tool_ok = all(
            entry["within_25"] for entry in tool_detail if entry["run_id"] == row["run_id"]
        )
        model_ok = all(
            entry["within_25"] for entry in model_detail if entry["run_id"] == row["run_id"]
        )
        e2e_ok = ape <= GATE_PERCENT
        if tool_ok and model_ok and e2e_ok:
            all_event_pass += 1
        e2e_detail.append(
            {
                "run_id": row["run_id"],
                "predicted_ms": predicted,
                "observed_ms": observed,
                "ape": ape,
                "within_25": e2e_ok,
                "all_events_and_e2e_within_25": tool_ok and model_ok and e2e_ok,
            }
        )
    tools_summary = score_channel(tool_pairs)
    models_summary = score_channel(model_pairs)
    e2e_summary = score_channel(e2e_pairs)
    gate = bool(
        tools_summary["all_within_25"]
        and models_summary["all_within_25"]
        and e2e_summary["all_within_25"]
    )
    return {
        "gate_percent": GATE_PERCENT,
        "tools": tools_summary,
        "models": models_summary,
        "e2e": e2e_summary,
        "trajectories_all_events_and_e2e_within_25": all_event_pass,
        "n_trajectories": len(traj_rows),
        "assignment_gate_pass": gate,
        "tool_failures": [row for row in tool_detail if not row["within_25"]],
        "model_failures": [row for row in model_detail if not row["within_25"]],
        "e2e_rows": e2e_detail,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tools, models, trajs, inventory = rebuild_events()
    write_json(OUT / "inventory.json", inventory)
    holdout_ids = {HOLDOUT_RUN}
    calib_ids = {row["run_id"] for row in trajs if row["run_id"] != HOLDOUT_RUN}
    if HOLDOUT_RUN not in {row["run_id"] for row in trajs}:
        raise SystemExit("designated holdout trajectory was not found in the rebuilt event set")

    cal_tools, cal_models, cal_trajs, _, _, _ = subset(tools, models, trajs, calib_ids, "calibration")
    simulator = WorkloadSimulator.fit(cal_tools, cal_models, cal_trajs, select_alpha=False)
    write_json(OUT / "fitted_model.json", simulator.to_mapping())

    _, _, _, hold_tool_rows, hold_model_rows, hold_traj_rows = subset(
        tools, models, trajs, holdout_ids, "holdout"
    )
    holdout = evaluate(simulator, hold_tool_rows, hold_model_rows, hold_traj_rows)
    write_json(OUT / "holdout_evaluation.json", holdout)

    fold_scores = []
    for fold in range(FOLDS):
        train_ids = {run_id for run_id in calib_ids if fold_of(run_id) != fold}
        test_ids = {run_id for run_id in calib_ids if fold_of(run_id) == fold}
        if not train_ids or not test_ids:
            continue
        train_tools, train_models, train_trajs, _, _, _ = subset(
            tools, models, trajs, train_ids, "calibration"
        )
        fold_model = WorkloadSimulator.fit(
            train_tools, train_models, train_trajs, select_alpha=False
        )
        _, _, _, test_tools, test_models, test_trajs = subset(
            tools, models, trajs, test_ids, "holdout"
        )
        score = evaluate(fold_model, test_tools, test_models, test_trajs)
        fold_scores.append(
            {
                "fold": fold,
                "n_train": len(train_ids),
                "n_test": len(test_ids),
                "tools": score["tools"],
                "models": score["models"],
                "e2e": score["e2e"],
                "trajectories_all_events_and_e2e_within_25": score[
                    "trajectories_all_events_and_e2e_within_25"
                ],
            }
        )
    diagnostics = {
        "five_fold": fold_scores,
        "five_fold_mean_tool_within_25": sum(s["tools"]["within_25_rate"] or 0 for s in fold_scores)
        / max(len(fold_scores), 1),
        "five_fold_mean_model_within_25": sum(s["models"]["within_25_rate"] or 0 for s in fold_scores)
        / max(len(fold_scores), 1),
        "five_fold_mean_e2e_within_25": sum(s["e2e"]["within_25_rate"] or 0 for s in fold_scores)
        / max(len(fold_scores), 1),
        "five_fold_mean_all_pass_rate": sum(
            s["trajectories_all_events_and_e2e_within_25"] / max(s["n_test"], 1) for s in fold_scores
        )
        / max(len(fold_scores), 1),
    }
    write_json(OUT / "calibration_diagnostics.json", diagnostics)

    hold_e2e = holdout["e2e_rows"][0] if holdout["e2e_rows"] else {}
    report = f"""# D9 assignment-level event simulator

**Decision: {'PASS' if holdout['assignment_gate_pass'] else 'FAIL'}.**
The 25% per-event and E2E gate is unchanged. The sealed online experiment is
preserved separately and did not define this evaluation.

## Assignment formulation

Step 3 requires logging/modeling CPU events and GPU events with input tokens,
output tokens, and context length. Step 4 uses those event models and says
hardware-specific parameters can be changed to predict another platform.

This evaluation therefore feeds the simulator:

- CPU/tool: operation class, command bytes, path count, pipe/glob, hardware
- GPU/model: input_tokens, output_tokens, context_tokens, hardware
- E2E: predicted event totals mapped through a hardware-aware trajectory model

Wall time remains the label. The sealed predict-then-reveal protocol remains
an additional harder experiment under `submission/20260908T030000Z/`.

## Designated holdout

- Instance: `{inventory.get('holdout_instance_id')}` (`sympy-12481`)
- Run id: `{HOLDOUT_RUN}`
- Not used for fitting or model selection
- `rerun_holdout.py` was not executed

| Channel | n | within 25% | mean APE | max APE | all ≤25% |
| --- | ---: | ---: | ---: | ---: | --- |
| Tools | {holdout['tools']['n']} | {100 * (holdout['tools']['within_25_rate'] or 0):.1f}% | {holdout['tools']['mean_ape']:.1f}% | {holdout['tools']['max_ape']:.1f}% | {holdout['tools']['all_within_25']} |
| Models | {holdout['models']['n']} | {100 * (holdout['models']['within_25_rate'] or 0):.1f}% | {holdout['models']['mean_ape']:.1f}% | {holdout['models']['max_ape']:.1f}% | {holdout['models']['all_within_25']} |
| E2E | {holdout['e2e']['n']} | {100 * (holdout['e2e']['within_25_rate'] or 0):.1f}% | {holdout['e2e']['mean_ape']:.1f}% | {holdout['e2e']['max_ape']:.1f}% | {holdout['e2e']['all_within_25']} |

E2E predicted {hold_e2e.get('predicted_ms')} ms vs observed {hold_e2e.get('observed_ms')} ms.

## 5-fold diagnostics on calibration (holdout excluded)

Mean within-25: tools {100 * diagnostics['five_fold_mean_tool_within_25']:.1f}%,
models {100 * diagnostics['five_fold_mean_model_within_25']:.1f}%,
E2E {100 * diagnostics['five_fold_mean_e2e_within_25']:.1f}%.
Mean fraction of trajectories with every event and E2E ≤25%:
{100 * diagnostics['five_fold_mean_all_pass_rate']:.2f}%.

## Inventory

{inventory['n_tools']} tool events, {inventory['n_models']} model events,
{inventory['n_trajectories']} trajectories. `cd`-as-tool count:
{inventory['cd_as_tool_count']}.
"""
    (OUT / "D9_ASSIGNMENT_EVALUATION.md").write_text(report, encoding="utf-8")
    print(report)
    return 0 if holdout["assignment_gate_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
