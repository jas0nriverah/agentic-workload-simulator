"""Resolve and validate the immutable vLLM instance-manifest contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Mapping


VLLM_DEFAULTS: Mapping[str, str] = {
    "VLLM_MODEL": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "VLLM_MODEL_REVISION": "b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
    "VLLM_IMAGE": "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271",
    "VLLM_IMAGE_PLATFORM": "linux/amd64",
    "VLLM_VERSION": "0.10.0",
    "VLLM_TOOL_PARSER": "qwen3_coder",
    "VLLM_MAX_MODEL_LEN": "32768",
    "VLLM_HEALTH_CONTEXT": "8192",
    "VLLM_GPU_MEMORY_UTILIZATION": "0.90",
    "VLLM_TENSOR_PARALLEL_SIZE": "1",
    "VLLM_PORT": "8000",
}
_HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


class VLLMConfigError(ValueError):
    """The instance manifest cannot produce a safe vLLM launch."""


def read_instance_manifest(path: str | Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is None:
        return values
    manifest = Path(path)
    if not manifest.is_file():
        return values
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _positive_int(values: Mapping[str, str], key: str) -> int:
    try:
        value = int(values[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise VLLMConfigError(f"{key} must be a positive integer") from exc
    if value <= 0:
        raise VLLMConfigError(f"{key} must be a positive integer")
    return value


def _memory_fraction(values: Mapping[str, str]) -> float:
    try:
        value = float(values["VLLM_GPU_MEMORY_UTILIZATION"])
    except (KeyError, TypeError, ValueError) as exc:
        raise VLLMConfigError("VLLM_GPU_MEMORY_UTILIZATION must be a number in (0, 1)") from exc
    if not 0.0 < value < 1.0:
        raise VLLMConfigError("VLLM_GPU_MEMORY_UTILIZATION must be in (0, 1)")
    return value


def resolve_vllm_config(
    manifest_path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve environment defaults, then let the instance manifest win.

    The result contains normalized values used by both the launcher and the
    health check.  No shell file is sourced, so values cannot execute code.
    """

    merged = dict(VLLM_DEFAULTS)
    source_env = os.environ if environ is None else environ
    for key in merged:
        if source_env.get(key) is not None:
            merged[key] = str(source_env[key])
    merged.update(read_instance_manifest(manifest_path))
    model = merged.get("VLLM_MODEL", "").strip()
    revision = merged.get("VLLM_MODEL_REVISION", "").strip()
    image = merged.get("VLLM_IMAGE", "").strip()
    parser = merged.get("VLLM_TOOL_PARSER", "").strip()
    image_platform = merged.get("VLLM_IMAGE_PLATFORM", "").strip()
    if not model or any(char.isspace() for char in model) or model.lower().endswith(":latest"):
        raise VLLMConfigError("VLLM_MODEL must be an immutable non-empty model identifier")
    if not _HEX40.fullmatch(revision):
        raise VLLMConfigError("VLLM_MODEL_REVISION must be a 40-hex immutable revision")
    if "@" not in image or not _DIGEST.fullmatch(image.rsplit("@", 1)[1]):
        raise VLLMConfigError("VLLM_IMAGE must include an immutable @sha256:<64 hex> digest")
    if merged.get("VLLM_IMAGE_DIGEST") and merged["VLLM_IMAGE_DIGEST"] != image.rsplit("@", 1)[1]:
        raise VLLMConfigError("VLLM_IMAGE_DIGEST does not match VLLM_IMAGE")
    if parser != "qwen3_coder":
        raise VLLMConfigError("VLLM_TOOL_PARSER must be qwen3_coder")
    if image_platform != "linux/amd64":
        raise VLLMConfigError("VLLM_IMAGE_PLATFORM must be linux/amd64")
    tp = _positive_int(merged, "VLLM_TENSOR_PARALLEL_SIZE")
    if tp != 1:
        raise VLLMConfigError("VLLM_TENSOR_PARALLEL_SIZE must be 1 for the frozen H100 launch")
    version = merged.get("VLLM_VERSION", "").strip()
    if version != "0.10.0":
        raise VLLMConfigError("VLLM_VERSION must be 0.10.0")
    return {
        "model": model,
        "model_revision": revision.lower(),
        "image": image,
        "image_digest": image.rsplit("@", 1)[1].lower(),
        "image_platform": image_platform,
        "version": version,
        "parser": parser,
        "max_model_len": _positive_int(merged, "VLLM_MAX_MODEL_LEN"),
        "health_context": _positive_int(merged, "VLLM_HEALTH_CONTEXT"),
        "gpu_memory_utilization": _memory_fraction(merged),
        "tensor_parallel_size": tp,
        "port": _positive_int(merged, "VLLM_PORT"),
    }


def validate_server_manifest(server_path: str | Path, config: Mapping[str, Any]) -> None:
    """Require the generated server manifest to match resolved launch values."""

    try:
        server = json.loads(Path(server_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VLLMConfigError(f"invalid vLLM server manifest: {server_path}") from exc
    expected = {
        "model": config["model"],
        "model_revision": config["model_revision"],
        "vllm_image": config["image"],
        "port": config["port"],
        "tool_parser": config["parser"],
        "max_model_len": config["max_model_len"],
        "gpu_memory_utilization": config["gpu_memory_utilization"],
        "tensor_parallel_size": config["tensor_parallel_size"],
    }
    mismatches = {
        key: {"expected": value, "actual": server.get(key)}
        for key, value in expected.items()
        if server.get(key) != value
    }
    if mismatches:
        raise VLLMConfigError("vLLM server manifest does not match instance manifest: " + json.dumps(mismatches, sort_keys=True))


def _shell(config: Mapping[str, Any]) -> str:
    names = {
        "VLLM_RESOLVED_MODEL": config["model"],
        "VLLM_RESOLVED_MODEL_REVISION": config["model_revision"],
        "VLLM_RESOLVED_IMAGE": config["image"],
        "VLLM_RESOLVED_IMAGE_DIGEST": config["image_digest"],
        "VLLM_RESOLVED_PARSER": config["parser"],
        "VLLM_RESOLVED_MAX_MODEL_LEN": config["max_model_len"],
        "VLLM_RESOLVED_HEALTH_CONTEXT": config["health_context"],
        "VLLM_RESOLVED_GPU_MEMORY_UTILIZATION": config["gpu_memory_utilization"],
        "VLLM_RESOLVED_TENSOR_PARALLEL_SIZE": config["tensor_parallel_size"],
        "VLLM_RESOLVED_PORT": config["port"],
    }
    return "\n".join(f"{key}={shlex.quote(str(value))}" for key, value in names.items())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--server-manifest", type=Path)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args(argv)
    config = resolve_vllm_config(args.manifest)
    if args.server_manifest:
        validate_server_manifest(args.server_manifest, config)
    if args.format == "shell":
        print(_shell(config))
    else:
        print(json.dumps(config, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["VLLMConfigError", "VLLM_DEFAULTS", "read_instance_manifest", "resolve_vllm_config", "validate_server_manifest"]
