#!/usr/bin/env python3
"""CLI for the offline D9 salvage simulator and strict scorer.

Examples:

    python3 run.py predict --request example_request.json
    python3 run.py predict --request request.json --hardware-profile profile.json \
        --event-id event-1 --trajectory-id run-1
    python3 run.py score --predictions predictions.jsonl --labels labels.jsonl \
        --required-target cpu_tool --required-target gpu_request --required-target e2e
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from d9_simulator import D9Simulator, PredictionContractError  # noqa: E402
from hardware_profile import HardwareProfile  # noqa: E402
from strict_metrics import MetricsError, score_bundle  # noqa: E402


def _json(path: Path) -> Any:
    try:
        text = sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def _rows(path: Path, key: str) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        result = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        value = json.loads(text)
        if isinstance(value, dict) and isinstance(value.get(key), list):
            result = value[key]
        elif isinstance(value, list):
            result = value
        else:
            raise ValueError(f"{path} must contain a list or an object with {key}")
    if not all(isinstance(row, dict) for row in result):
        raise ValueError(f"{path} rows must be JSON objects")
    return result


def _write(value: Any, path: Path | None) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _predict(args: argparse.Namespace) -> int:
    request = _json(args.request)
    if not isinstance(request, dict):
        raise ValueError("prediction request must be a JSON object")
    profile = None
    if args.hardware_profile:
        profile = HardwareProfile.from_mapping(_json(args.hardware_profile))
    simulator = D9Simulator(
        artifact_root=args.artifact_root,
        hardware=profile,
        workload_model_path=args.workload_model,
        conditional_gpu_model_path=args.conditional_gpu_model,
        native_fit_artifact_path=args.native_fit_artifact,
    )
    result = simulator.predict_request(request)
    for name in ("event_id", "trajectory_id", "case_id", "attempt_id"):
        value = getattr(args, name)
        if value is not None:
            result[name] = value
    _write(result, args.output)
    return 0


def _score(args: argparse.Namespace) -> int:
    predictions = _rows(args.predictions, "predictions")
    labels = _rows(args.labels, "labels")
    report = score_bundle(
        predictions,
        labels,
        required_target_kinds=tuple(args.required_target),
    )
    _write(report, args.output)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline D9 salvage simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    predict = sub.add_parser("predict", help="freeze one target prediction")
    predict.add_argument("--request", type=Path, required=True)
    predict.add_argument("--hardware-profile", type=Path)
    predict.add_argument("--artifact-root", type=Path)
    predict.add_argument("--workload-model", type=Path)
    predict.add_argument("--conditional-gpu-model", type=Path)
    predict.add_argument("--native-fit-artifact", type=Path)
    predict.add_argument("--event-id")
    predict.add_argument("--trajectory-id")
    predict.add_argument("--case-id")
    predict.add_argument("--attempt-id")
    predict.add_argument("--output", type=Path)
    predict.set_defaults(handler=_predict)

    score = sub.add_parser("score", help="score frozen predictions against labels")
    score.add_argument("--predictions", type=Path, required=True)
    score.add_argument("--labels", type=Path, required=True)
    score.add_argument("--required-target", action="append", default=[])
    score.add_argument("--output", type=Path)
    score.set_defaults(handler=_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (PredictionContractError, MetricsError, ValueError, OSError) as exc:
        print(f"d9-simulator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
