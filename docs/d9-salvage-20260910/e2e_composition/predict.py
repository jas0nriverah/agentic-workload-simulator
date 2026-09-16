"""Raw-free inference for the retained conditional E2E candidate."""

from __future__ import annotations

import argparse
import json
import importlib.util
from pathlib import Path
from typing import Any

_spec = importlib.util.spec_from_file_location('repaired_e2e_fit_dependency', Path(__file__).with_name('fit.py'))
_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fit)
ACTION_CLASSES, CPU_CLASSES, MODEL, predict = _fit.ACTION_CLASSES, _fit.CPU_CLASSES, _fit.MODEL, _fit.predict


def design(request: dict[str, Any]) -> tuple[list[float], list[float], list[float]]:
    required = {'event_counts', 'semantic_action_class_counts', 'request_count', 'input_tokens', 'output_tokens'}
    if not isinstance(request, dict) or not required <= request.keys() or request.keys() - required - {'cached_tokens'}:
        raise ValueError('request requires declared workload counts only; unknown fields are rejected')
    counts = request["event_counts"]
    action_counts = request["semantic_action_class_counts"]
    if not isinstance(counts, dict) or not isinstance(action_counts, dict):
        raise ValueError('event and action counts must be mappings')
    if counts.keys() - set(CPU_CLASSES) or action_counts.keys() - set(ACTION_CLASSES):
        raise ValueError('unknown event or action class')
    request_count = request["request_count"]
    input_tokens = request["input_tokens"]
    output_tokens = request["output_tokens"]
    cached_tokens = request.get("cached_tokens", 0)
    values = [*counts.values(), *action_counts.values(), request_count, input_tokens, output_tokens, cached_tokens]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("all supplied counts must be nonnegative integers")
    if cached_tokens > input_tokens:
        raise ValueError('cached tokens cannot exceed input tokens')
    cpu = (
        [1.0]
        + [counts.get(name, 0) / 100.0 for name in CPU_CLASSES]
        + [action_counts.get(name, 0) / 100.0 for name in ACTION_CLASSES]
    )
    gpu = [1.0, request_count / 100.0, input_tokens / 1e6, output_tokens / 1e4, cached_tokens / 1e6]
    remainder = [
        1.0,
        request_count / 100.0,
        counts.get("semantic_action", 0) / 100.0,
        counts.get("runtime_command", 0) / 100.0,
        input_tokens / 1e6,
        output_tokens / 1e4,
    ]
    return cpu, gpu, remainder


def predict_request(request: dict[str, Any], artifact: dict[str, Any]) -> dict[str, float | str]:
    cpu, gpu, remainder = design(request)
    coefficients = artifact["coefficients"]
    scales = artifact["training_only_coverage_scales"]
    components = {
        "cpu_union_ms": predict(coefficients["cpu_union"], cpu),
        "native_ms": predict(coefficients["native"], gpu),
        "remainder_ms": predict(coefficients["remainder"], remainder),
    }
    composed = sum(components.values()) * float(scales["composed"])
    direct = predict(coefficients["direct"], remainder) * float(scales["direct"])
    return {
        **components,
        "composed_e2e_ms": composed,
        "selected_direct_e2e_ms": direct,
        "selected_model": artifact["selected_e2e_model"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("request", type=Path)
    parser.add_argument("--model", type=Path, default=MODEL)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    artifact = json.loads(args.model.read_text(encoding="utf-8"))
    print(json.dumps(predict_request(request, artifact), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
