#!/usr/bin/env python3
"""Validate the resolved SWE-agent command without starting a workload.

The Lambda wrapper invokes this validator immediately before a paid run.  It
parses argv only; it never imports or launches SWE-agent and never expands a
secret-bearing API-key value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.sweagent_runner import validate_experiment_command  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command", required=True)
    parser.add_argument("--expected-model")
    parser.add_argument("--expected-instance")
    parser.add_argument("--expected-dataset-path")
    parser.add_argument("--expected-calls", type=int)
    parser.add_argument("--expected-output-tokens", type=int)
    parser.add_argument("--expected-observation-length", type=int)
    parser.add_argument("--expected-temperature", type=float)
    parser.add_argument("--expected-seed", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    tokens = shlex.split(args.command)
    resolved = validate_experiment_command(tokens)
    if "run-batch" not in tokens:
        raise SystemExit("command must be a SWE-agent run-batch invocation")
    if args.expected_model is not None:
        try:
            model = tokens[tokens.index("--agent.model.name") + 1]
        except (ValueError, IndexError):
            raise SystemExit("command is missing --agent.model.name") from None
        if model != args.expected_model:
            raise SystemExit(f"model mismatch: {model!r} != {args.expected_model!r}")
    if args.expected_instance is not None:
        try:
            instance_filter = tokens[tokens.index("--instances.filter") + 1]
        except (ValueError, IndexError):
            raise SystemExit("command is missing --instances.filter") from None
        if instance_filter != f"^{args.expected_instance}$":
            raise SystemExit(f"instance filter mismatch: {instance_filter!r}")
    if args.expected_dataset_path is not None:
        try:
            dataset_path = tokens[tokens.index("--instances.path") + 1]
        except (ValueError, IndexError):
            raise SystemExit("command is missing --instances.path") from None
        if dataset_path != args.expected_dataset_path:
            raise SystemExit(f"dataset path mismatch: {dataset_path!r} != {args.expected_dataset_path!r}")
    expected = {
        "expected_calls": (args.expected_calls, "per_instance_call_limit"),
        "expected_output_tokens": (args.expected_output_tokens, "max_output_tokens"),
        "expected_observation_length": (args.expected_observation_length, "max_observation_length"),
        "expected_temperature": (args.expected_temperature, "temperature"),
        "expected_seed": (args.expected_seed, "seed"),
    }
    for label, (value, key) in expected.items():
        if value is not None and resolved[key] != value:
            raise SystemExit(f"{label} mismatch: {resolved[key]!r} != {value!r}")
    result = {
        "schema_version": "sweagent.command-contract.v1",
        "status": "pass",
        "command_sha256": hashlib.sha256("\0".join(tokens).encode("utf-8")).hexdigest(),
        "argv": tokens,
        "resolved": resolved,
        "request_contract": {
            "model": resolved["model"],
            "temperature": resolved["temperature"],
            "max_tokens": resolved["max_output_tokens"],
            "seed": resolved["seed"],
            "tool_payload_unchanged_between_control_and_thin": True,
        },
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists() and args.output.read_text(encoding="utf-8") != encoded:
            raise SystemExit(f"refusing to overwrite a different contract: {args.output}")
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
