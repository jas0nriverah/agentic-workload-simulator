"""Raw-free inference for the retained conditional E2E candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fit import ACTION_CLASSES, CPU_CLASSES, MODEL, predict


def design(request: dict[str, Any]) -> tuple[list[float], list[float], list[float]]:
    counts = request["event_counts"]
    action_counts = request["semantic_action_class_counts"]
    request_count = int(request["request_count"])
    input_tokens = int(request["input_tokens"])
    output_tokens = int(request["output_tokens"])
    cached_tokens = int(request.get("cached_tokens", 0))
    values = [*counts.values(), *action_counts.values(), request_count, input_tokens, output_tokens, cached_tokens]
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("all supplied counts must be nonnegative integers")
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
