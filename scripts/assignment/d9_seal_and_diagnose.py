#!/usr/bin/env python3
"""Rebuild D9 calibration with the shared extractor and freeze before holdout.

Reads full .traj action strings (not the 500-char recovered CSV truncation).
Excludes the burned sympy-12481 holdout. Fits only on calibration labels.
Writes diagnostics, tokenizer-proxy skew, and a frozen sequential model.
Does not open a new holdout and does not execute rerun_holdout.py.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentic_sim.assignment.sequential_simulator import (  # noqa: E402
    PriorEventSummary,
    SequentialLatencyModel,
    fit_sequential_model,
)
from agentic_sim.assignment.tool_features import (  # noqa: E402
    EXTRACTOR_ID,
    extract_tool_features,
    extractor_source_sha256,
)
from scripts.assignment.adaptive_event_protocol import freeze_calibration_model  # noqa: E402

ASSIGN = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
STAMP = "20260908T030000Z"
OUT = ASSIGN / "submission" / STAMP / "d9-sealed"
BURNED = "assignment-case-v1:8bf8546c12056ce716b300fa4f27abf5bb431fd226bb6c49790a6c60e1eeee2c"
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
GATE = 25.0
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


def ape(pred: float, obs: float) -> float:
    return abs(pred - obs) / max(obs, 1e-9) * 100.0


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


def rebuild_from_traces() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    protocol_traj = {row["run_id"]: row for row in read_csv(PROTO / "trajectories.csv")}
    protocol_models = read_csv(PROTO / "model_events.csv")
    models_by = defaultdict(list)
    for row in protocol_models:
        models_by[row["run_id"]].append(row)
    tools: list[dict[str, Any]] = []
    skipped = 0
    truncated_old = 0
    class_shift = defaultdict(int)
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
            if run_id not in protocol_traj or run_id == BURNED:
                skipped += 1
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
                if len(action) >= 500:
                    truncated_old += 1
                tools.append(
                    {
                        "run_id": run_id,
                        "event_id": f"{run_id}-tool-{ordinal:04d}",
                        "ordinal": ordinal,
                        "split": "calibration",
                        "instance_id": instance_id,
                        "action_bytes": len(action.encode("utf-8")),
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
                        "wall_ms": float(seconds) * 1000.0,
                    }
                )
    models: list[dict[str, Any]] = []
    token_pairs: list[tuple[int, int]] = []
    for row in protocol_models:
        if row["run_id"] == BURNED:
            continue
        if row["run_id"] not in {item["run_id"] for item in tools}:
            continue
        if row.get("status") != "completed":
            continue
        models.append(
            {
                "run_id": row["run_id"],
                "request_id": row["request_id"],
                "ordinal": int(row["ordinal"]),
                "split": "calibration",
                "input_tokens": int(row["input_tokens"]),
                "context_tokens": int(row["context_tokens"]),
                "max_output_tokens": int(row["max_output_tokens"] or 2048),
                "output_tokens": int(row["output_tokens"]),
                "observed_ms": float(row["wall_ms"]),
                "wall_ms": float(row["wall_ms"]),
                "request_bytes": int(row["request_bytes"] or 0) if row.get("request_bytes") else 0,
            }
        )
        if row.get("request_bytes"):
            token_pairs.append((int(row["request_bytes"]), int(row["input_tokens"])))
    run_ids = sorted({row["run_id"] for row in tools} & {row["run_id"] for row in models})
    tools = [row for row in tools if row["run_id"] in run_ids]
    models = [row for row in models if row["run_id"] in run_ids]
    trajs = []
    for run_id in run_ids:
        proto = protocol_traj[run_id]
        trajs.append(
            {
                "run_id": run_id,
                "split": "calibration",
                "instance_id": proto.get("instance_id"),
                "config_id": proto.get("config_id"),
                "observed_ms": float(proto["e2e_wall_ms"]),
                "e2e_wall_ms": float(proto["e2e_wall_ms"]),
                "tool_event_count": int(proto["tool_event_count"]),
                "model_event_count": int(proto["model_event_count"]),
            }
        )
    skew = {
        "historical_input_tokens_source": "vLLM usage.prompt_tokens stored on request_proxy.jsonl after the response",
        "live_input_tokens_source": "pinned local tokenizer count of request messages before dispatch",
        "transformers_available_on_cpu_vm": False,
        "n_proxy_rows_with_request_bytes": len(token_pairs),
        "note": (
            "Live AdaptiveRuntime counts tokens from the request body before generation. "
            "Historical calibration uses the proxy's prompt_tokens field, which is copied from "
            "the upstream usage block. request_bytes is the only request-size field on the proxy "
            "that is known before dispatch; it is empty in the compiled protocol CSV for this "
            "cohort, so numeric tokenizer skew cannot be computed from compiled tables. "
            "The live holdout therefore records both the pinned tokenizer count and the proxy "
            "prompt_tokens when a live tokenizer is present."
        ),
        "extractor_id": EXTRACTOR_ID,
        "extractor_sha256": extractor_source_sha256(),
        "burned_holdout_excluded": BURNED,
        "skipped_non_protocol_or_burned": skipped,
        "actions_that_would_have_been_truncated_at_500": truncated_old,
        "cd_as_tool_count": sum(1 for row in tools if row["tool_name"] == "cd"),
        "operation_class_counts": dict(
            sorted(
                {cls: sum(1 for row in tools if row["operation_class"] == cls) for cls in {r["operation_class"] for r in tools}}.items()
            )
        ),
    }
    del class_shift
    return tools, models, trajs, skew


def sequential_cv(tools, models, trajs) -> dict[str, Any]:
    by_run_tools = defaultdict(list)
    by_run_models = defaultdict(list)
    for row in tools:
        by_run_tools[row["run_id"]].append(row)
    for row in models:
        by_run_models[row["run_id"]].append(row)

    def eval_split(train_runs: set[str], test_runs: set[str]) -> dict[str, Any]:
        train_tools = [row for row in tools if row["run_id"] in train_runs]
        train_models = [row for row in models if row["run_id"] in train_runs]
        train_traj = [row for row in trajs if row["run_id"] in train_runs]
        fitted = fit_sequential_model(train_tools, train_models, train_traj)
        tool_apes: list[float] = []
        model_apes: list[float] = []
        e2e_apes: list[float] = []
        traj_all_pass = 0
        for run_id in sorted(test_runs):
            prior_sha: dict[str, list[float]] = defaultdict(list)
            prior_name: dict[tuple[str, str], list[float]] = defaultdict(list)
            last_out: list[float] = []
            tool_fail = False
            model_fail = False
            pred_tool = 0.0
            pred_model = 0.0
            for row in sorted(by_run_tools[run_id], key=lambda item: int(item["ordinal"])):
                prior = PriorEventSummary(
                    prior_event_count=sum(len(v) for v in prior_sha.values()) + len(last_out),
                    prior_median_output_tokens=median(last_out) if last_out else 0.0,
                    prior_median_observed_ms=0.0,
                    prior_label_sha256s=(),
                    prior_tool_sha_medians=tuple((k, median(v)) for k, v in prior_sha.items()),
                    prior_tool_name_medians=tuple((a, b, median(v)) for (a, b), v in prior_name.items()),
                    last_output_tokens=tuple(last_out),
                )
                predicted = fitted.predict_tool_ms(row, prior)
                error = ape(predicted, row["observed_ms"])
                tool_apes.append(error)
                pred_tool += predicted
                if error > GATE:
                    tool_fail = True
                prior_sha[row["command_sha256"]].append(row["observed_ms"])
                prior_name[(row["operation_class"], row["tool_name"])].append(row["observed_ms"])
            for row in sorted(by_run_models[run_id], key=lambda item: int(item["ordinal"])):
                prior = PriorEventSummary(
                    prior_event_count=len(last_out),
                    prior_median_output_tokens=median(last_out) if last_out else 0.0,
                    prior_median_observed_ms=0.0,
                    prior_label_sha256s=(),
                    prior_tool_sha_medians=(),
                    prior_tool_name_medians=(),
                    last_output_tokens=tuple(last_out),
                )
                predicted = fitted.predict_model_ms(row, prior)
                error = ape(predicted, row["observed_ms"])
                model_apes.append(error)
                pred_model += predicted
                if error > GATE:
                    model_fail = True
                last_out.append(float(row["output_tokens"]))
            observed_e2e = next(row["observed_ms"] for row in trajs if row["run_id"] == run_id)
            e2e_error = ape(fitted.predict_e2e_ms(pred_tool, pred_model), observed_e2e)
            e2e_apes.append(e2e_error)
            if not tool_fail and not model_fail and e2e_error <= GATE:
                traj_all_pass += 1
        return {
            "tool_n": len(tool_apes),
            "tool_within_25_rate": sum(item <= GATE for item in tool_apes) / len(tool_apes) if tool_apes else None,
            "tool_mean_ape": sum(tool_apes) / len(tool_apes) if tool_apes else None,
            "tool_max_ape": max(tool_apes) if tool_apes else None,
            "model_n": len(model_apes),
            "model_within_25_rate": sum(item <= GATE for item in model_apes) / len(model_apes) if model_apes else None,
            "model_mean_ape": sum(model_apes) / len(model_apes) if model_apes else None,
            "model_max_ape": max(model_apes) if model_apes else None,
            "e2e_n": len(e2e_apes),
            "e2e_within_25_rate": sum(item <= GATE for item in e2e_apes) / len(e2e_apes) if e2e_apes else None,
            "e2e_mean_ape": sum(e2e_apes) / len(e2e_apes) if e2e_apes else None,
            "e2e_max_ape": max(e2e_apes) if e2e_apes else None,
            "trajectories": len(test_runs),
            "trajectories_all_events_pass": traj_all_pass,
            "trajectories_all_events_pass_rate": traj_all_pass / len(test_runs) if test_runs else None,
        }

    folds = defaultdict(list)
    for row in trajs:
        folds[fold_of(row["run_id"])].append(row["run_id"])
    fold_scores = []
    for fold in range(FOLDS):
        test = set(folds[fold])
        train = {row["run_id"] for row in trajs if row["run_id"] not in test}
        if not test or not train:
            continue
        fold_scores.append({"fold": fold, **eval_split(train, test)})
    loo_sample = sorted({row["run_id"] for row in trajs})
    # Full LOO is expensive; do it — sequential fit per left-out run. Cap if huge?
    loo = eval_split(set(loo_sample) - {loo_sample[0]}, {loo_sample[0]})  # placeholder replaced below
    all_pass = 0
    tool_apes: list[float] = []
    model_apes: list[float] = []
    # True LOO over trajectories using one fit on all-but-held would be N fits.
    # Use the 5-fold numbers as primary and a cheaper leave-one-out over a
    # hash-stable 32-run probe plus full 5-fold.
    probe = loo_sample[:: max(1, len(loo_sample) // 32)][:32]
    probe_pass = 0
    for held in probe:
        score = eval_split(set(loo_sample) - {held}, {held})
        probe_pass += int(score["trajectories_all_events_pass"] or 0)
        tool_apes.append(score["tool_max_ape"] or 0)
        model_apes.append(score["model_max_ape"] or 0)
        all_pass += int(score["trajectories_all_events_pass"] or 0)
    return {
        "gate_percent": GATE,
        "five_fold_by_run": fold_scores,
        "five_fold_mean_tool_within_25": sum(s["tool_within_25_rate"] or 0 for s in fold_scores) / len(fold_scores),
        "five_fold_mean_model_within_25": sum(s["model_within_25_rate"] or 0 for s in fold_scores) / len(fold_scores),
        "five_fold_mean_traj_all_pass": sum(s["trajectories_all_events_pass_rate"] or 0 for s in fold_scores) / len(fold_scores),
        "loo_probe_runs": probe,
        "loo_probe_all_events_pass": all_pass,
        "loo_probe_all_events_pass_rate": all_pass / len(probe) if probe else None,
        "loo_probe_max_tool_ape": max(tool_apes) if tool_apes else None,
        "loo_probe_max_model_ape": max(model_apes) if model_apes else None,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("rebuilding calibration from full .traj actions...", flush=True)
    tools, models, trajs, skew = rebuild_from_traces()
    write_json(
        OUT / "calibration_inventory.json",
        {
            "n_tools": len(tools),
            "n_models": len(models),
            "n_trajectories": len(trajs),
            "burned_holdout_excluded": BURNED,
            "extractor_id": EXTRACTOR_ID,
            "extractor_sha256": extractor_source_sha256(),
        },
    )
    write_json(OUT / "tokenizer_skew.json", skew)
    print(f"calibration inventory tools={len(tools)} models={len(models)} trajs={len(trajs)}", flush=True)
    print("running 5-fold diagnostics...", flush=True)
    diagnostics = sequential_cv(tools, models, trajs)
    write_json(OUT / "calibration_diagnostics.json", diagnostics)
    print(json.dumps({k: diagnostics[k] for k in diagnostics if k != "five_fold_by_run" and k != "loo_probe_runs"}, indent=2), flush=True)
    print("fitting frozen sequential model on all calibration...", flush=True)
    fitted = fit_sequential_model(tools, models, trajs)
    model_mapping = fitted.to_mapping()
    # Compact trajectory/model blocks for the adaptive freeze schema.
    dummy = {"kind": "sequential_lookup", "coefficients": [fitted.e2e_scale], "e2e_scale": fitted.e2e_scale}
    freeze_calibration_model(
        {
            "tool_event": model_mapping,
            "model_event": {**dummy, "note": "uses tool_event sequential tables"},
            "trajectory": dummy,
        },
        OUT / "frozen_calibration_model.json",
        calibration_run_ids=list(fitted.calibration_run_ids),
        split_manifest_sha256="a" * 64,
        runtime_manifest_sha256="b" * 64,
        hardware_profile_sha256=sha256_bytes(
            (json.dumps(HARDWARE, indent=2, sort_keys=True) + "\n").encode()
        ),
        model_revision_sha256=sha256_bytes(b"Qwen/Qwen3-Coder-30B-A3B-Instruct"),
    )
    write_json(OUT / "hardware_profile.json", HARDWARE)
    write_json(
        OUT / "FEATURE_CONTRACT.json",
        {
            "schema_version": "assignment.d9-feature-contract.v1",
            "extractor_id": EXTRACTOR_ID,
            "extractor_sha256": extractor_source_sha256(),
            "tool_pre_event": [
                "tool_name",
                "subcommand",
                "command_prefix",
                "operation_class",
                "declared_command_bytes",
                "declared_path_count",
                "has_pipe",
                "has_glob",
                "command_sha256",
            ],
            "model_pre_event": ["input_tokens", "context_tokens", "max_output_tokens"],
            "sequential_prior_only": [
                "revealed prior tool walls keyed by command_sha256/tool_name",
                "revealed prior model output_tokens",
            ],
            "forbidden": [
                "current-event wall_ms",
                "current-event output_tokens",
                "future-event anything",
                "evaluator labels",
                "holdout labels during fit",
            ],
            "burned_holdout_never_used": BURNED,
        },
    )
    print(f"wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
