#!/usr/bin/env python3
"""Prove SWE-agent's pinned LiteLLM request kwargs without network traffic."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

def _manifest_command(path: Path) -> str:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.startswith("SWE_AGENT_COMMAND="):
            return line.split("=", 1)[1]
    raise RuntimeError(f"manifest does not define SWE_AGENT_COMMAND: {path}")


def _cli_config(source_root: Path, command_text: str) -> tuple[dict[str, Any], list[str], list[str]]:
    """Resolve the exact manifest argv through pinned SWE-agent's parser.

    Lambda paths are intentionally absolute in the committed example manifest.
    A rehearsal checkout lives under a temporary runner path, so only a missing
    `--config` path is mapped to the detached checkout's equivalent default
    config; all model, agent, request, and observation flags are passed
    unchanged to the pinned CLI.
    """
    tokens = shlex.split(command_text)
    if "run-batch" not in tokens:
        raise RuntimeError("manifest command is not a run-batch invocation")
    executable = Path(tokens[0])
    if not executable.is_file():
        executable = Path(shutil.which("sweagent") or "")
    if not executable.is_file():
        for candidate in (source_root / ".venv/bin/sweagent", source_root / "venv/bin/sweagent"):
            if candidate.is_file():
                executable = candidate
                break
    if not executable.is_file():
        raise RuntimeError("pinned SWE-agent executable is unavailable")
    argv = list(tokens)
    argv[0] = str(executable)
    rewrites: list[str] = []
    try:
        config_index = argv.index("--config")
        config_path = Path(argv[config_index + 1])
    except (ValueError, IndexError):
        raise RuntimeError("manifest command must include --config") from None
    if not config_path.is_file():
        replacement = source_root / "config/default.yaml"
        if not replacement.is_file():
            raise RuntimeError(f"pinned SWE-agent default config is unavailable: {replacement}")
        rewrites.append(f"--config:{config_path}->{replacement}")
        argv[config_index + 1] = str(replacement)
    env = os.environ.copy()
    env["VLLM_API_KEY"] = "fixture"
    completed = subprocess.run(
        [*argv, "--print_config"], cwd=source_root, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    if completed.returncode:
        raise RuntimeError(f"pinned SWE-agent rejected manifest command: {(completed.stdout + completed.stderr)[-4000:]}")
    try:
        import yaml  # type: ignore
        config = yaml.safe_load(completed.stdout)
    except Exception as exc:
        raise RuntimeError(f"pinned SWE-agent --print_config did not emit parseable YAML: {exc}") from exc
    if not isinstance(config, dict) or not isinstance(config.get("agent"), dict):
        raise RuntimeError("pinned SWE-agent --print_config omitted the agent configuration")
    return config, argv, rewrites


def verify(source_root: Path, command_text: str | None = None) -> dict[str, Any]:
    if not source_root.is_dir():
        return {"schema_version": "litellm-request-check.v1", "status": "capability", "detail": "SWE-agent source root not supplied"}
    sys.path.insert(0, str(source_root))
    try:
        import sweagent.agent.models as models
        from sweagent.agent.models import GenericAPIModelConfig, LiteLLMModel
    except Exception as exc:  # pinned dependencies are Linux-only in this rehearsal
        return {"schema_version": "litellm-request-check.v1", "status": "capability", "detail": f"SWE-agent imports unavailable: {type(exc).__name__}: {exc}"}

    if not command_text:
        return {"schema_version": "litellm-request-check.v1", "status": "capability", "detail": "exact SWE-agent manifest command not supplied"}
    config_dict, resolved_argv, path_rewrites = _cli_config(source_root, command_text)
    model_dict = dict(config_dict["agent"].get("model", {}))
    if not model_dict.get("name"):
        raise RuntimeError("pinned SWE-agent config did not resolve agent.model.name")
    # Pydantic's SecretStr is intentionally masked by some YAML serializers;
    # restore the literal environment reference from the reviewed command.
    model_dict["api_key"] = "$VLLM_API_KEY"
    model_config = GenericAPIModelConfig.model_validate(model_dict)
    templates = config_dict["agent"].get("templates", {})
    captured: dict[str, Any] = {}
    original_completion = models.litellm.completion
    original_counter = models.litellm.utils.token_counter
    original_cost = models.litellm.cost_calculator.completion_cost
    original_model_cost = models.litellm.model_cost
    os.environ.setdefault("VLLM_API_KEY", "fixture")

    def completion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        message = SimpleNamespace(content="fixture response", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    try:
        models.litellm.completion = completion
        models.litellm.utils.token_counter = lambda **_kwargs: 1
        models.litellm.cost_calculator.completion_cost = lambda _response: 0.0
        models.litellm.model_cost = {}
        fake_tools = SimpleNamespace(use_function_calling=False, tools=[])
        LiteLLMModel(model_config, fake_tools)._single_query([{"role": "user", "content": "fixture"}])
    finally:
        models.litellm.completion = original_completion
        models.litellm.utils.token_counter = original_counter
        models.litellm.cost_calculator.completion_cost = original_cost
        models.litellm.model_cost = original_model_cost

    observed = {
        "model": captured.get("model"),
        "temperature": captured.get("temperature"),
        "completion_kwargs.max_tokens": captured.get("max_tokens"),
        "completion_kwargs.seed": captured.get("seed"),
    }
    expected = {
        "model": model_config.name,
        "temperature": model_config.temperature,
        "completion_kwargs.max_tokens": model_config.completion_kwargs.get("max_tokens"),
        "completion_kwargs.seed": model_config.completion_kwargs.get("seed"),
    }
    expected_capture = {
        "model": expected["model"],
        "temperature": expected["temperature"],
        "completion_kwargs.max_tokens": expected["completion_kwargs.max_tokens"],
        "completion_kwargs.seed": expected["completion_kwargs.seed"],
    }
    if observed != expected_capture:
        raise RuntimeError(f"LiteLLM request kwargs mismatch: observed={observed}")
    return {
        "schema_version": "litellm-request-check.v1", "status": "pass", "observed": observed,
        "resolved_agent_model": {
            "name": model_config.name,
            "per_instance_call_limit": model_config.per_instance_call_limit,
            "temperature": model_config.temperature,
            "max_input_tokens": model_config.max_input_tokens,
            "max_output_tokens": model_config.max_output_tokens,
            "completion_kwargs": dict(model_config.completion_kwargs),
        },
        "resolved_observation_length": templates.get("max_observation_length"),
        "manifest_command_sha256": hashlib.sha256("\0".join(resolved_argv).encode()).hexdigest(),
        "path_rewrites": path_rewrites,
        "network_called": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swe-agent-root", type=Path)
    parser.add_argument("--command", help="exact SWE_AGENT_COMMAND text from the reviewed manifest")
    parser.add_argument("--manifest", type=Path, help="read SWE_AGENT_COMMAND without sourcing the manifest")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    try:
        command = args.command
        if args.manifest:
            command = _manifest_command(args.manifest)
        result = verify(args.swe_agent_root.resolve() if args.swe_agent_root else Path(""), command)
    except Exception as exc:
        result = {"schema_version": "litellm-request-check.v1", "status": "fail", "detail": str(exc)}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result["status"] == "fail" or (args.strict and result["status"] == "capability") else 0


if __name__ == "__main__":
    raise SystemExit(main())
