#!/usr/bin/env python3
"""Reviewed, serialized request runner for the sealed H100 validation matrix.

The runner deliberately does not start a server.  It connects to an already
reviewed vLLM server, uses the locally pinned tokenizer, performs the two
declared warmups once per case, and then performs exactly one measured request
for the supplied repeat.  Production CPU/CUDA/kernel measurements are supplied
by an explicit arm/collect/abort trace provider through
``H100_TRACE_PROVIDER``; this runner never turns missing telemetry into a
successful row.

Production environment:

``H100_VLLM_BASE_URL``
    Existing vLLM OpenAI-compatible server, default ``http://127.0.0.1:8000``.
``H100_MODEL_SNAPSHOT``
    Local Hugging Face snapshot directory whose final path component is the
    exact model revision from the sealed protocol.
``H100_TRACE_PROVIDER``
    Reviewed executable.  It receives the request metadata and must write
    ``trace_summary.json`` under its supplied ``--output-dir``.  The summary
    schema is documented in ``TRACE_SUMMARY_SCHEMA`` below.

``H100_RUNNER_TEST_MODE`` is intentionally test-only.  It replaces the GPU
and tokenizer adapters with deterministic fixtures and is rejected unless
the caller also supplies the fake-server test environment.  It must never be
used for a validation run.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


ROW_SCHEMA = "h100-final-row.v1"
TRACE_SCHEMA = "h100-trace-summary.v1"
FIXED_PROMPT_TEMPLATE = "fixed_tokenizer_stable_prompt_v1"
FIXED_PROMPT_TOKENIZER = "frozen_model_tokenizer_at_model_revision"
FIXED_PROMPT_SEED = "H100 sealed final validation fixed prompt token "
EXPECTED_SPLITS = {"calibration", "sealed_holdout"}
EXPECTED_REPEATS = {"r01", "r02", "r03"}
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
VLLM_IMAGE = "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
TEST_MODE_ENV = "H100_RUNNER_TEST_MODE"
TRACE_PROVIDER_ENV = "H100_TRACE_PROVIDER"
BASE_URL_ENV = "H100_VLLM_BASE_URL"
SNAPSHOT_ENV = "H100_MODEL_SNAPSHOT"
REQUEST_TIMEOUT_SECONDS = 300.0
TRACE_TIMEOUT_SECONDS = 120.0

TRACE_SUMMARY_SCHEMA = {
    "schema_version": TRACE_SCHEMA,
    "provenance": "measured",
    "clock_id": "CLOCK_MONOTONIC_RAW",
    "cuda_union_rule": "overlap_aware_request_window",
    "cpu_activity_union_ms": "finite non-negative number",
    "cuda_activity_union_ms": "finite non-negative number",
    "kernel_duration_sum_ms": "finite non-negative number",
    "raw_artifacts": [
        {"path": "path under request output directory", "sha256": "64-hex", "kind": "trace"}
    ],
}


class RunnerError(RuntimeError):
    """A fail-closed runner error with a stable reason code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _test_mode() -> bool:
    return os.environ.get(TEST_MODE_ENV) == "1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _raw_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    """Create an artifact once and refuse any overwrite."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RunnerError("artifact_exists", f"refusing to overwrite immutable artifact: {path}")
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise RunnerError("artifact_exists", f"refusing to overwrite immutable artifact: {path}") from exc


def _write_json_exclusive(path: Path, value: Any) -> None:
    _write_exclusive(path, _dump(value))


def _write_digest_sidecar(path: Path) -> str:
    digest = _sha_file(path)
    _write_exclusive(path.with_name(path.name + ".sha256"), f"{digest}  {path.name}\n".encode("utf-8"))
    return digest


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunnerError("trace_invalid", f"trace field {name} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise RunnerError("trace_invalid", f"trace field {name} must be finite and non-negative")
    return value


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RunnerError("protocol_invalid", f"{name} must be an object")
    return value


def _load_protocol(path: Path) -> dict[str, Any]:
    try:
        protocol = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("config_invalid", f"cannot read protocol config: {path}") from exc
    if protocol.get("schema_version") != "h100-final-validation.v1":
        raise RunnerError("config_invalid", "unsupported final-validation schema")
    if protocol.get("launch_authorized") is not False:
        raise RunnerError("config_invalid", "launch_authorized must remain false")
    hardware = _require_mapping(protocol.get("hardware"), "hardware")
    names = {str(name) for name in hardware.get("gpu_name_allowlist", [])}
    if hardware.get("gpu_family") != "H100" or not any("H100" in name for name in names):
        raise RunnerError("config_invalid", "protocol is not H100-only")
    request = _require_mapping(protocol.get("request_protocol"), "request_protocol")
    if request.get("concurrency") != 1:
        raise RunnerError("protocol_invalid", "request concurrency must be exactly one")
    if request.get("warmup_requests") != 2:
        raise RunnerError("protocol_invalid", "request protocol requires exactly two warmups")
    if request.get("measured_repetitions_per_case") != 3:
        raise RunnerError("protocol_invalid", "request protocol requires exactly three repeats")
    if request.get("repetition_ids") != ["r01", "r02", "r03"]:
        raise RunnerError("protocol_invalid", "request repetition IDs are not sealed")
    if request.get("sampling") != {"temperature": 0.0, "top_p": 1.0, "seed": 0}:
        raise RunnerError("protocol_invalid", "sampling parameters are not sealed")
    if request.get("mode") != "serialized_single_request":
        raise RunnerError("protocol_invalid", "request mode is not serialized_single_request")
    if request.get("prompt_template") != FIXED_PROMPT_TEMPLATE:
        raise RunnerError("protocol_invalid", "prompt template is not the sealed template")
    if request.get("prompt_tokenizer") != FIXED_PROMPT_TOKENIZER:
        raise RunnerError("protocol_invalid", "prompt tokenizer is not the sealed tokenizer")
    if request.get("health_checks") != ["/health", "/v1/models", "/metrics"]:
        raise RunnerError("protocol_invalid", "health-check endpoints are not sealed")
    if request.get("request_spacing_seconds") != 1.0:
        raise RunnerError("protocol_invalid", "request spacing is not the sealed one-second interval")
    if request.get("clock") != "CLOCK_MONOTONIC_RAW_joined_to_utc":
        raise RunnerError("protocol_invalid", "CLOCK_MONOTONIC_RAW protocol is missing")
    software = _require_mapping(protocol.get("frozen_software"), "frozen_software")
    expected_software = {
        "model_revision": MODEL_REVISION,
        "precision": "bfloat16",
        "context_tokens": 32768,
        "vllm_version": "0.10.0",
        "vllm_image": VLLM_IMAGE,
        "vllm_tool_parser": "qwen3_coder",
        "tensor_parallel_size": 1,
    }
    for key, expected in expected_software.items():
        if software.get(key) != expected:
            raise RunnerError("protocol_invalid", f"sealed software pin differs for {key}")
    features = protocol.get("features")
    if not isinstance(features, list):
        raise RunnerError("protocol_invalid", "feature definitions are missing")
    feature_names = {item.get("name") for item in features if isinstance(item, Mapping)}
    required_features = {
        "prompt_tokens",
        "max_output_tokens",
        "context_tokens",
        "tool_calls",
        "hardware_score",
        "prompt_output_interaction",
        "concurrency",
        "warm_state",
    }
    if not required_features.issubset(feature_names):
        raise RunnerError("protocol_invalid", "feature-only definitions are incomplete")
    return protocol


def _case_from_args(protocol: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if args.split not in EXPECTED_SPLITS:
        raise RunnerError("case_invalid", f"invalid split: {args.split}")
    if args.repeat_id not in EXPECTED_REPEATS:
        raise RunnerError("case_invalid", f"invalid repeat ID: {args.repeat_id}")
    rows = protocol["calibration_configs"] if args.split == "calibration" else protocol["sealed_holdouts"]
    row = next((item for item in rows if item.get("case_id") == args.case_id), None)
    if row is None:
        raise RunnerError("case_invalid", f"case is not in the sealed {args.split} matrix: {args.case_id}")
    if int(row["input_tokens"]) != args.input_tokens or int(row["output_tokens"]) != args.output_tokens:
        raise RunnerError("case_invalid", "CLI token targets do not match the sealed case")
    return dict(row)


def _base_url() -> str:
    value = os.environ.get(BASE_URL_ENV, "http://127.0.0.1:8000").rstrip("/")
    if not re.match(r"^https?://[^/]+(?::[0-9]+)?$", value):
        raise RunnerError("server_invalid", "H100_VLLM_BASE_URL must be an http(s) origin")
    return value


def _model_name(protocol: Mapping[str, Any]) -> str:
    model = _require_mapping(protocol["frozen_software"], "frozen_software").get("model")
    if not isinstance(model, str) or not model:
        raise RunnerError("protocol_invalid", "frozen model name is missing")
    configured = os.environ.get("H100_VLLM_MODEL")
    if configured is not None and configured != model:
        raise RunnerError("server_invalid", "H100_VLLM_MODEL differs from the sealed model")
    return model


def _resolve_snapshot() -> Path:
    value = os.environ.get(SNAPSHOT_ENV)
    if not value:
        raise RunnerError("model_missing", "H100_MODEL_SNAPSHOT is required in production mode")
    snapshot = Path(value).expanduser().resolve()
    if snapshot.name != MODEL_REVISION or not snapshot.is_dir():
        raise RunnerError("model_invalid", "model snapshot is not the sealed revision directory")
    for required in ("config.json", "tokenizer_config.json", "tokenizer.json"):
        if not (snapshot / required).is_file():
            raise RunnerError("model_invalid", f"pinned tokenizer file is missing: {required}")
    return snapshot


class PromptBuilder:
    """Build a deterministic token-ID prompt without using measured labels."""

    def __init__(self, maximum_tokens: int):
        self.maximum_tokens = maximum_tokens
        if _test_mode():
            if os.environ.get("H100_TEST_SERVER_URL") is None:
                raise RunnerError("test_mode_invalid", "test mode requires H100_TEST_SERVER_URL")
            self.snapshot = None
            self.tokenizer_revision = "test-fixture"
            self._pool = [1000 + (index % 10000) for index in range(maximum_tokens)]
            return
        self.snapshot = _resolve_snapshot()
        try:
            try:
                from transformers import AutoTokenizer
            except ModuleNotFoundError:
                from tokenizers import Tokenizer

                self.tokenizer = Tokenizer.from_file(str(self.snapshot / "tokenizer.json"))
            else:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    str(self.snapshot),
                    local_files_only=True,
                    revision=MODEL_REVISION,
                    trust_remote_code=False,
                )
        except Exception as exc:  # transformers reports several dependency-specific exceptions
            raise RunnerError("tokenizer_invalid", "could not load the pinned local tokenizer") from exc
        self.tokenizer_revision = MODEL_REVISION
        pool: list[int] = []
        repetition = 0
        while len(pool) < maximum_tokens:
            repetition += 1
            text = FIXED_PROMPT_SEED + str(repetition) + " "
            try:
                encoded = self.tokenizer.encode(text, add_special_tokens=False)
                if hasattr(encoded, "ids"):
                    encoded = encoded.ids
            except Exception as exc:
                raise RunnerError("tokenizer_invalid", "pinned tokenizer failed to encode fixed prompt") from exc
            if not encoded:
                raise RunnerError("tokenizer_invalid", "pinned tokenizer produced no prompt tokens")
            pool.extend(int(token) for token in encoded)
        self._pool = pool[:maximum_tokens]

    def token_ids(self, token_count: int) -> list[int]:
        if token_count < 1 or token_count > self.maximum_tokens:
            raise RunnerError("case_invalid", "input token target is outside prompt builder range")
        return list(self._pool[:token_count])


def _validate_hardware_record(data: Mapping[str, Any], protocol: Mapping[str, Any]) -> dict[str, Any]:
    hardware = _require_mapping(protocol.get("hardware"), "hardware")
    allowlist = {str(name) for name in hardware.get("gpu_name_allowlist", [])}
    name = data.get("gpu_name")
    if name not in allowlist:
        raise RunnerError("hardware_invalid", "GPU name is not in the sealed allowlist")
    try:
        memory_mib = int(data.get("memory_total_mib"))
    except (TypeError, ValueError) as exc:
        raise RunnerError("hardware_invalid", "GPU memory is not numeric") from exc
    if memory_mib < int(hardware.get("minimum_memory_mib", 80000)):
        raise RunnerError("hardware_invalid", "GPU memory is below the sealed minimum")
    if str(data.get("compute_capability")) != str(hardware.get("required_compute_capability")):
        raise RunnerError("hardware_invalid", "GPU compute capability is not the sealed value")
    return dict(data)


def _reviewed_profiled_server_allows_gpu_processes(processes: str) -> bool:
    """Accept GPU activity only from the explicitly reviewed profiled server."""

    container = os.environ.get("H100_EXPECTED_SERVER_CONTAINER") or os.environ.get(
        "H100_NSYS_CONTAINER"
    )
    session = os.environ.get("H100_NSYS_SESSION", "h100-final-validation")
    if not container:
        return False
    try:
        running = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        image = subprocess.run(
            ["docker", "inspect", "--format", "{{.Config.Image}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        command = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Cmd}}", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        container_pids = subprocess.run(
            ["docker", "top", container, "-eo", "pid"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
        session_list = subprocess.run(
            [
                "docker",
                "exec",
                container,
                os.environ.get("H100_NSYS_BIN", "/host-cuda/bin/nsys"),
                "sessions",
                "list",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    required = (
        f"--session-new={session}",
        "--trace=cuda,osrt",
        "--cuda-event-trace=false",
        "vllm.entrypoints.openai.api_server",
        MODEL_REVISION,
    )
    if running != "true" or image != VLLM_IMAGE or any(item not in command for item in required):
        return False
    allowed_pids = {
        line.strip().split()[0]
        for line in container_pids.splitlines()[1:]
        if line.strip() and line.strip().split()[0].isdigit()
    }
    gpu_pids = {line.split(",", 1)[0].strip() for line in processes.splitlines() if line.strip()}
    return bool(gpu_pids) and gpu_pids.issubset(allowed_pids) and session in session_list


def _hardware_metadata(protocol: Mapping[str, Any]) -> dict[str, Any]:
    if _test_mode():
        raw = os.environ.get("H100_TEST_HARDWARE_JSON")
        if not raw:
            raise RunnerError("hardware_unavailable", "test hardware fixture is missing")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RunnerError("hardware_invalid", "test hardware fixture is invalid") from exc
        return _validate_hardware_record(_require_mapping(data, "test hardware fixture"), protocol)
    query = (
        "name,uuid,pci.bus_id,memory.total,driver_version,power.limit,power.draw,"
        "clocks.current.graphics,clocks.current.sm,clocks.current.memory,compute_cap"
    )
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError("hardware_unavailable", "nvidia-smi GPU query failed") from exc
    if len(rows) != 1:
        raise RunnerError("hardware_invalid", f"expected one GPU, found {len(rows)}")
    fields = [part.strip() for part in rows[0].split(",")]
    if len(fields) != 11:
        raise RunnerError("hardware_invalid", "nvidia-smi GPU query returned an unexpected schema")
    name, uuid, pci, memory, driver, power_limit, power_draw, graphics, sm, memory_clock, compute_cap = fields
    try:
        memory_mib = int(float(memory))
    except ValueError as exc:
        raise RunnerError("hardware_invalid", "GPU memory is not numeric") from exc
    hardware = _require_mapping(protocol.get("hardware"), "hardware")
    allowlist = {str(item) for item in hardware.get("gpu_name_allowlist", [])}
    if name not in allowlist or memory_mib < int(hardware.get("minimum_memory_mib", 80000)):
        raise RunnerError("hardware_invalid", "visible GPU is not an allowlisted H100 80GB")
    if compute_cap != str(hardware.get("required_compute_capability")):
        raise RunnerError("hardware_invalid", "GPU compute capability is not 9.0")
    try:
        query_result = subprocess.run(
            ["nvidia-smi", "-q", "-i", "0"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError("hardware_unavailable", "nvidia-smi detail query failed") from exc
    driver_match = re.search(r"^Driver Version\s*:\s*(\S+)", query_result, re.MULTILINE)
    cuda_match = re.search(r"^CUDA Version\s*:\s*(\S+)", query_result, re.MULTILINE)
    if not driver_match or not cuda_match:
        raise RunnerError("hardware_invalid", "nvidia-smi detail query lacks driver/CUDA identity")
    processes = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout
    if any(line.strip() for line in processes.splitlines()) and not _reviewed_profiled_server_allows_gpu_processes(
        processes
    ):
        raise RunnerError("hardware_busy", "GPU compute processes are present")
    return {
        "gpu_name": name,
        "gpu_uuid": uuid,
        "pci_bus_id": pci,
        "memory_total_mib": memory_mib,
        "compute_capability": compute_cap,
        "driver": driver_match.group(1),
        "cuda": cuda_match.group(1),
        "power_limit_w": power_limit,
        "power_draw_w": power_draw,
        "application_clocks": {"graphics_mhz": graphics, "sm_mhz": sm, "memory_mhz": memory_clock},
        "host": platform.node(),
        "kernel": platform.release(),
        "boot_id": _boot_id(),
    }


def _boot_id() -> str | None:
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _clock_metadata(start_ns: int | None = None, end_ns: int | None = None) -> dict[str, Any]:
    try:
        resolution_ns = int(time.clock_getres(time.CLOCK_MONOTONIC_RAW) * 1_000_000_000)
    except (AttributeError, OSError) as exc:
        raise RunnerError("clock_invalid", "CLOCK_MONOTONIC_RAW is unavailable") from exc
    result: dict[str, Any] = {
        "clock_id": "CLOCK_MONOTONIC_RAW",
        "monotonic_resolution_ns": resolution_ns,
        "clock_source": "time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)",
    }
    if start_ns is not None:
        result["start_mono_ns"] = start_ns
    if end_ns is not None:
        result["end_mono_ns"] = end_ns
    return result


def _http_request(url: str, payload: bytes | None = None) -> tuple[int, bytes, int, int, str, str]:
    method = "POST" if payload is not None else "GET"
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"} if payload is not None else {},
        method=method,
    )
    start_ns = _raw_ns()
    start_utc = _utc_now()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read()
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = int(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RunnerError("server_request_failed", f"{method} request failed") from exc
    end_ns = _raw_ns()
    end_utc = _utc_now()
    return status, body, start_ns, end_ns, start_utc, end_utc


def _health_check(protocol: Mapping[str, Any], base_url: str, model: str, case_root: Path) -> None:
    marker = case_root / "server_checks.json"
    if marker.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError("server_check_invalid", "existing server check marker is invalid") from exc
        if existing.get("model") != model or existing.get("base_url") != base_url:
            raise RunnerError("server_check_invalid", "server check marker does not match pinned server")
        return
    checks: list[dict[str, Any]] = []
    for endpoint in ("/health", "/v1/models", "/metrics"):
        try:
            status, body, _, _, _, _ = _http_request(base_url + endpoint)
        except RunnerError:
            raise
        if status != 200:
            raise RunnerError("server_check_failed", f"server health check failed: {endpoint}")
        entry: dict[str, Any] = {"endpoint": endpoint, "status": status, "body_sha256": _sha_bytes(body)}
        if endpoint == "/v1/models":
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RunnerError("server_check_failed", "vLLM model endpoint is not JSON") from exc
            data = payload.get("data")
            model_ids = [item.get("id") for item in data] if isinstance(data, list) else []
            if model not in model_ids:
                raise RunnerError("server_check_failed", "pinned model is absent from /v1/models")
            entry["model_ids"] = model_ids
        checks.append(entry)
    _write_json_exclusive(
        marker,
        {
            "schema_version": "h100-server-checks.v1",
            "provenance": "observed",
            "base_url": base_url,
            "model": model,
            "checks": checks,
            "checked_at_utc": _utc_now(),
        },
    )


@contextmanager
def _case_lock(case_root: Path) -> Iterator[None]:
    case_root.mkdir(parents=True, exist_ok=True)
    lock_path = case_root / ".runner.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _respect_spacing(case_root: Path, spacing_seconds: float) -> None:
    marker = case_root / "last_request.json"
    if marker.exists():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            previous_end = int(previous["end_mono_ns"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise RunnerError("clock_invalid", "last request clock marker is invalid") from exc
        remaining = spacing_seconds - ((_raw_ns() - previous_end) / 1_000_000_000)
        if remaining > 0:
            time.sleep(remaining)


def _record_last_request(case_root: Path, end_ns: int) -> None:
    path = case_root / "last_request.json"
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_bytes(_dump({"end_mono_ns": end_ns}))
    temporary.replace(path)


def _request_payload(model: str, prompt_ids: Sequence[int], output_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "prompt": list(prompt_ids),
        "max_tokens": output_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": 0,
        "stream": False,
    }


def _write_request(path: Path, payload: Mapping[str, Any]) -> tuple[bytes, str]:
    body = _dump(payload)
    _write_exclusive(path, body)
    return body, _sha_bytes(body)


def _response_artifact(request_dir: Path, body: bytes, suffix: str = "response.json") -> tuple[Path, str]:
    path = request_dir / suffix
    _write_exclusive(path, body)
    return path, _sha_bytes(body)


def _trace_provider_command(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    request_dir: Path,
    phase: str,
    action: str,
    start_ns: int,
    end_ns: int,
) -> list[str]:
    provider = os.environ.get(TRACE_PROVIDER_ENV)
    if not provider or not os.access(provider, os.X_OK):
        raise RunnerError("trace_provider_missing", "H100_TRACE_PROVIDER is not an executable reviewed provider")
    command = [
        provider,
        "--config",
        str(args.config.resolve()),
        "--case-id",
        str(case["case_id"]),
        "--split",
        str(args.split),
        "--input-tokens",
        str(args.input_tokens),
        "--output-tokens",
        str(args.output_tokens),
        "--repeat-id",
        str(args.repeat_id),
        "--phase",
        phase,
        "--start-mono-ns",
        str(start_ns),
        "--end-mono-ns",
        str(end_ns),
        "--output-dir",
        str(request_dir.resolve()),
    ]
    # The fixture provider intentionally retains its original, collect-only
    # interface and is reachable only from H100_RUNNER_TEST_MODE.  Production
    # providers must implement the explicit arm/collect/abort protocol.
    if not _test_mode():
        command.insert(command.index("--start-mono-ns"), "--action")
        command.insert(command.index("--action") + 1, action)
    return command


def _run_trace_provider_action(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    request_dir: Path,
    phase: str,
    action: str,
    start_ns: int,
    end_ns: int,
) -> subprocess.CompletedProcess[str]:
    command = _trace_provider_command(
        protocol, case, args, request_dir, phase, action, start_ns, end_ns
    )
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=TRACE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError("trace_provider_failed", "trace provider could not be invoked") from exc


def _arm_trace_provider(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    request_dir: Path,
    phase: str,
) -> None:
    """Arm the provider immediately before a serialized request.

    Production providers use this hook to begin an external CUPTI/Nsight
    capture before the request starts.  The existing provider interface only
    collected after a request, which cannot recover historical GPU activity.
    """

    request_dir.mkdir(parents=True, exist_ok=True)
    if _test_mode():
        return
    completed = _run_trace_provider_action(
        protocol, case, args, request_dir, phase, "arm", 0, 0
    )
    if completed.returncode != 0:
        raise RunnerError("trace_provider_failed", "trace provider could not arm capture")


def _abort_trace_provider(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    request_dir: Path,
    phase: str,
) -> None:
    """Stop a provider capture after a request-side failure.

    Cleanup is best effort: the original request failure remains the terminal
    row reason, while the provider is given a chance to release its session.
    """

    if _test_mode():
        return
    try:
        _run_trace_provider_action(protocol, case, args, request_dir, phase, "abort", 0, 0)
    except RunnerError:
        return


def _invoke_trace_provider(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    request_dir: Path,
    phase: str,
    start_ns: int,
    end_ns: int,
) -> dict[str, Any]:
    completed = _run_trace_provider_action(
        protocol, case, args, request_dir, phase, "collect", start_ns, end_ns
    )
    if completed.returncode != 0:
        raise RunnerError("trace_provider_failed", "trace provider returned nonzero")
    summary_path = request_dir / "trace_summary.json"
    if not summary_path.is_file():
        raise RunnerError("trace_invalid", "trace provider did not write trace_summary.json")
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("trace_invalid", "trace summary is not valid JSON") from exc
    if summary.get("schema_version") != TRACE_SCHEMA or summary.get("provenance") != "measured":
        raise RunnerError("trace_invalid", "trace summary schema/provenance is invalid")
    if summary.get("clock_id") != "CLOCK_MONOTONIC_RAW":
        raise RunnerError("trace_invalid", "trace summary is not on CLOCK_MONOTONIC_RAW")
    if summary.get("cuda_union_rule") != "overlap_aware_request_window":
        raise RunnerError("trace_invalid", "CUDA union rule is not overlap-aware request-window union")
    values = {
        key: _finite_nonnegative(summary.get(key), key)
        for key in ("cpu_activity_union_ms", "cuda_activity_union_ms", "kernel_duration_sum_ms")
    }
    raw_artifacts = summary.get("raw_artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise RunnerError("trace_invalid", "trace summary has no raw artifact references")
    refs: list[dict[str, Any]] = []
    request_root = request_dir.resolve()
    for item in raw_artifacts:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise RunnerError("trace_invalid", "trace raw artifact reference is malformed")
        raw_path = Path(item["path"])
        if not raw_path.is_absolute():
            raw_path = request_dir / raw_path
        raw_path = raw_path.resolve()
        try:
            relative = raw_path.relative_to(request_root)
        except ValueError as exc:
            raise RunnerError("trace_invalid", "trace artifact escapes request output directory") from exc
        if not raw_path.is_file():
            raise RunnerError("trace_invalid", f"trace artifact is missing: {relative}")
        digest = _sha_file(raw_path)
        if item.get("sha256") != digest:
            raise RunnerError("trace_invalid", f"trace artifact checksum mismatch: {relative}")
        refs.append({"kind": str(item.get("kind", "trace")), "path": str(relative), "sha256": digest})
    summary_digest = _sha_file(summary_path)
    refs.append({"kind": "trace_summary", "path": "trace_summary.json", "sha256": summary_digest})
    return {**values, "raw_artifacts": refs, "trace_summary_sha256": summary_digest}


def _parse_usage(body: bytes) -> tuple[dict[str, Any], int, int]:
    try:
        response = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RunnerError("response_invalid", "vLLM response is not JSON") from exc
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        raise RunnerError("response_invalid", "vLLM response lacks usage")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        isinstance(prompt, bool)
        or not isinstance(prompt, int)
        or prompt < 0
        or isinstance(completion, bool)
        or not isinstance(completion, int)
        or completion < 0
    ):
        raise RunnerError("response_invalid", "vLLM usage counts are invalid")
    return response, prompt, completion


def _request_once(
    base_url: str,
    payload: Mapping[str, Any],
    request_dir: Path,
) -> dict[str, Any]:
    request_body, request_sha = _write_request(request_dir / "request.json", payload)
    status, body, start_ns, end_ns, start_utc, end_utc = _http_request(
        base_url + "/v1/completions", request_body
    )
    response_path, response_sha = _response_artifact(request_dir, body)
    if status != 200:
        raise RunnerError("server_response_failed", f"vLLM request returned HTTP {status}")
    response, prompt_count, completion_count = _parse_usage(body)
    return {
        "response": response,
        "response_path": response_path,
        "response_sha256": response_sha,
        "request_sha256": request_sha,
        "http_status": status,
        "start_mono_ns": start_ns,
        "end_mono_ns": end_ns,
        "start_utc": start_utc,
        "end_utc": end_utc,
        "actual_prompt_tokens": prompt_count,
        "actual_completion_tokens": completion_count,
    }


def _run_warmups(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    case_root: Path,
    builder: PromptBuilder,
    base_url: str,
    model: str,
    spacing_seconds: float,
) -> None:
    warmup_path = case_root / "warmups.json"
    if warmup_path.exists():
        try:
            value = json.loads(warmup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError("warmup_invalid", "existing warmup manifest is invalid") from exc
        if value.get("status") != "completed" or value.get("warmup_count") != 2:
            raise RunnerError("warmup_invalid", "existing warmup manifest is not terminal")
        records = value.get("records")
        if not isinstance(records, list) or len(records) != 2:
            raise RunnerError("warmup_invalid", "existing warmup manifest has incomplete records")
        for record in records:
            if not isinstance(record, Mapping) or not isinstance(record.get("raw_artifacts"), list):
                raise RunnerError("warmup_invalid", "existing warmup raw-artifact references are missing")
            _validate_artifact_refs(case_root, record["raw_artifacts"])
        sidecar = warmup_path.with_name(warmup_path.name + ".sha256")
        if not sidecar.is_file() or sidecar.read_text(encoding="utf-8").split()[0] != _sha_file(warmup_path):
            raise RunnerError("warmup_invalid", "existing warmup checksum is invalid")
        return
    warmup_root = case_root / "warmups"
    records: list[dict[str, Any]] = []
    for index in range(2):
        request_dir = warmup_root / f"warmup-{index + 1:02d}"
        _respect_spacing(case_root, spacing_seconds)
        prompt_ids = builder.token_ids(int(case["input_tokens"]))
        payload = _request_payload(model, prompt_ids, int(case["output_tokens"]))
        try:
            _arm_trace_provider(protocol, case, args, request_dir, f"warmup-{index + 1:02d}")
            result = _request_once(base_url, payload, request_dir)
        except RunnerError:
            _abort_trace_provider(protocol, case, args, request_dir, f"warmup-{index + 1:02d}")
            raise RunnerError("warmup_failed", "declared warmup failed; no warmup retry performed")
        try:
            trace = _invoke_trace_provider(
                protocol,
                case,
                args,
                request_dir,
                f"warmup-{index + 1:02d}",
                result["start_mono_ns"],
                result["end_mono_ns"],
            )
        except RunnerError:
            _abort_trace_provider(protocol, case, args, request_dir, f"warmup-{index + 1:02d}")
            raise RunnerError("warmup_failed", "declared warmup failed; no warmup retry performed")
        try:
            raw_artifacts = _row_artifacts(request_dir, result, trace)
            case_relative_artifacts = [
                {
                    **artifact,
                    "path": str(Path("warmups") / request_dir.name / artifact["path"]),
                }
                for artifact in raw_artifacts
            ]
            records.append(
                {
                    "warmup_id": index + 1,
                    "request_sha256": result["request_sha256"],
                    "response_sha256": result["response_sha256"],
                    "trace_summary_sha256": trace["trace_summary_sha256"],
                    "actual_prompt_tokens": result["actual_prompt_tokens"],
                    "actual_completion_tokens": result["actual_completion_tokens"],
                    "raw_artifacts": case_relative_artifacts,
                }
            )
            _record_last_request(case_root, result["end_mono_ns"])
        except RunnerError:
            raise RunnerError("warmup_failed", "declared warmup failed; no warmup retry performed")
    _write_json_exclusive(
        warmup_path,
        {
            "schema_version": "h100-warmups.v1",
            "provenance": "measured",
            "status": "completed",
            "case_id": case["case_id"],
            "split": args.split,
            "warmup_count": 2,
            "excluded_from_denominators": True,
            "records": records,
        },
    )
    _write_digest_sidecar(warmup_path)


def _row_artifacts(output_dir: Path, request_result: Mapping[str, Any], trace: Mapping[str, Any]) -> list[dict[str, Any]]:
    refs = [
        {
            "kind": "request",
            "path": "request.json",
            "sha256": _sha_file(output_dir / "request.json"),
        },
        {
            "kind": "response",
            "path": "response.json",
            "sha256": str(request_result["response_sha256"]),
        },
    ]
    refs.extend(trace["raw_artifacts"])
    return refs


def _validate_artifact_refs(root: Path, refs: Sequence[Mapping[str, Any]]) -> None:
    for ref in refs:
        relative = ref.get("path")
        expected = ref.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise RunnerError("artifact_invalid", "artifact reference is malformed")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise RunnerError("artifact_invalid", "artifact reference escapes output directory") from exc
        if not path.is_file() or _sha_file(path) != expected:
            raise RunnerError("artifact_invalid", f"artifact checksum mismatch: {relative}")


def validate_row_artifacts(row_path: Path) -> None:
    """Validate the row sidecar and every raw-artifact checksum.

    This public helper is used by the offline contract tests and by the runner
    immediately after writing a completed row.  It never opens a holdout
    label or a prediction artifact.
    """

    try:
        row = json.loads(row_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError("artifact_invalid", "row.json is not valid JSON") from exc
    sidecar = row_path.with_name(row_path.name + ".sha256")
    if not sidecar.is_file():
        raise RunnerError("artifact_invalid", "row checksum sidecar is missing")
    sidecar_text = sidecar.read_text(encoding="utf-8").split()
    if not sidecar_text or sidecar_text[0] != _sha_file(row_path):
        raise RunnerError("artifact_invalid", "row checksum sidecar does not match row.json")
    refs = row.get("raw_artifacts")
    if not isinstance(refs, list) or not refs:
        raise RunnerError("artifact_invalid", "row has no raw-artifact references")
    _validate_artifact_refs(row_path.parent, refs)


def _directory_artifact_refs(root: Path) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name in {"row.json", "row.json.sha256"}:
            continue
        refs.append(
            {
                "kind": "raw",
                "path": str(path.relative_to(root)),
                "sha256": _sha_file(path),
            }
        )
    return refs


def _completed_row(
    protocol: Mapping[str, Any],
    case: Mapping[str, Any],
    args: argparse.Namespace,
    output_dir: Path,
    request_result: Mapping[str, Any],
    trace: Mapping[str, Any],
    hardware: Mapping[str, Any],
    builder: PromptBuilder,
    model: str,
    base_url: str,
) -> dict[str, Any]:
    start_ns = int(request_result["start_mono_ns"])
    end_ns = int(request_result["end_mono_ns"])
    if end_ns <= start_ns:
        raise RunnerError("clock_invalid", "request end precedes request start")
    prompt_ids = builder.token_ids(args.input_tokens)
    payload = _request_payload(model, prompt_ids, args.output_tokens)
    request_sha = str(request_result["request_sha256"])
    if _sha_bytes(_dump(payload)) != request_sha:
        raise RunnerError("artifact_invalid", "request checksum changed before row creation")
    artifacts = _row_artifacts(output_dir, request_result, trace)
    return {
        "schema_version": ROW_SCHEMA,
        "provenance": "measured",
        "status": "completed",
        "case_id": args.case_id,
        "split": args.split,
        "holdout_kind": case.get("holdout_kind"),
        "repeat_id": args.repeat_id,
        "declared_features": {
            "prompt_tokens": args.input_tokens,
            "max_output_tokens": args.output_tokens,
            "context_tokens": int(case.get("context_tokens", 0)),
            "tool_calls": int(case.get("tool_calls", 0)),
            "hardware_score": 1.0,
            "concurrency": 1,
            "warm_state": "warm",
        },
        "frozen_software": dict(protocol["frozen_software"]),
        "request": {
            "base_url": base_url,
            "model": model,
            "endpoint": "/v1/completions",
            "prompt_template": FIXED_PROMPT_TEMPLATE,
            "prompt_tokenizer": FIXED_PROMPT_TOKENIZER,
            "tokenizer_revision": builder.tokenizer_revision,
            "prompt_token_count": len(prompt_ids),
            "max_tokens": args.output_tokens,
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": 0,
            "stream": False,
            "serialized_concurrency": 1,
            "request_sha256": request_sha,
        },
        "clock": {
            **_clock_metadata(start_ns, end_ns),
            "start_utc": request_result["start_utc"],
            "end_utc": request_result["end_utc"],
        },
        "wall_ms": (end_ns - start_ns) / 1_000_000.0,
        "http_status": int(request_result["http_status"]),
        "actual_prompt_tokens": int(request_result["actual_prompt_tokens"]),
        "actual_completion_tokens": int(request_result["actual_completion_tokens"]),
        "cpu_activity_union_ms": trace["cpu_activity_union_ms"],
        "cuda_activity_union_ms": trace["cuda_activity_union_ms"],
        "kernel_duration_sum_ms": trace["kernel_duration_sum_ms"],
        "hardware": dict(hardware),
        "trace": {
            "clock_id": "CLOCK_MONOTONIC_RAW",
            "cuda_union_rule": "overlap_aware_request_window",
            "trace_summary_sha256": trace["trace_summary_sha256"],
        },
        "raw_artifacts": artifacts,
        "raw_artifact_sha256": str(request_result["response_sha256"]),
        "source_paths": [item["path"] for item in artifacts],
        "recorded_at_utc": _utc_now(),
    }


def _failure_artifact(output_dir: Path, error: RunnerError) -> dict[str, Any]:
    path = output_dir / "failure.json"
    payload = {
        "schema_version": "h100-runner-failure.v1",
        "provenance": "unavailable",
        "error_code": error.code,
        "error": error.message,
        "recorded_at_utc": _utc_now(),
    }
    _write_json_exclusive(path, payload)
    return {"kind": "failure", "path": path.name, "sha256": _sha_file(path)}


def _unavailable_row(
    protocol: Mapping[str, Any] | None,
    args: argparse.Namespace,
    output_dir: Path,
    error: RunnerError,
) -> None:
    row_path = output_dir / "row.json"
    if row_path.exists():
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    failure = _failure_artifact(output_dir, error)
    artifacts = _directory_artifact_refs(output_dir)
    if not any(item["path"] == failure["path"] for item in artifacts):
        artifacts.append(failure)
    row: dict[str, Any] = {
        "schema_version": ROW_SCHEMA,
        "provenance": "unavailable",
        "status": "unavailable",
        "case_id": args.case_id,
        "split": args.split,
        "repeat_id": args.repeat_id,
        "declared_features": {
            "prompt_tokens": args.input_tokens,
            "max_output_tokens": args.output_tokens,
            "concurrency": 1,
            "warm_state": "warm",
        },
        "unavailable_reason": {"code": error.code, "message": error.message},
        "raw_artifacts": artifacts,
        "raw_artifact_sha256": failure["sha256"],
        "recorded_at_utc": _utc_now(),
    }
    if protocol is not None:
        software = protocol.get("frozen_software", {})
        row["request"] = {
            "model": software.get("model"),
            "prompt_template": FIXED_PROMPT_TEMPLATE,
            "temperature": 0.0,
            "top_p": 1.0,
            "seed": 0,
            "serialized_concurrency": 1,
        }
    _write_json_exclusive(row_path, row)
    _write_digest_sidecar(row_path)


def _run_case(protocol: Mapping[str, Any], args: argparse.Namespace, case: Mapping[str, Any]) -> None:
    output_dir = args.output_dir.resolve()
    case_root = output_dir.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    row_path = output_dir / "row.json"
    if row_path.exists():
        raise RunnerError("artifact_exists", f"row already exists: {row_path}")
    base_url = _base_url()
    model = _model_name(protocol)
    max_input = max(int(row["input_tokens"]) for row in protocol["calibration_configs"] + protocol["sealed_holdouts"])
    builder = PromptBuilder(max_input)
    hardware = _hardware_metadata(protocol)
    spacing = float(_require_mapping(protocol["request_protocol"], "request_protocol")["request_spacing_seconds"])
    if _test_mode() and os.environ.get("H100_TEST_REQUEST_SPACING_SECONDS") is not None:
        spacing = float(os.environ["H100_TEST_REQUEST_SPACING_SECONDS"])
    if spacing < 0:
        raise RunnerError("protocol_invalid", "request spacing must be non-negative")
    with _case_lock(case_root):
        _health_check(protocol, base_url, model, case_root)
        _run_warmups(protocol, case, args, case_root, builder, base_url, model, spacing)
        request_dir = output_dir
        _respect_spacing(case_root, spacing)
        prompt_ids = builder.token_ids(args.input_tokens)
        payload = _request_payload(model, prompt_ids, args.output_tokens)
        try:
            _arm_trace_provider(protocol, case, args, request_dir, "measured")
            request_result = _request_once(base_url, payload, request_dir)
        except RunnerError:
            _abort_trace_provider(protocol, case, args, request_dir, "measured")
            raise
        try:
            trace = _invoke_trace_provider(
                protocol,
                case,
                args,
                request_dir,
                "measured",
                int(request_result["start_mono_ns"]),
                int(request_result["end_mono_ns"]),
            )
        except RunnerError:
            _abort_trace_provider(protocol, case, args, request_dir, "measured")
            raise
        _record_last_request(case_root, int(request_result["end_mono_ns"]))
        row = _completed_row(
            protocol, case, args, output_dir, request_result, trace, hardware, builder, model, base_url
        )
        _validate_artifact_refs(output_dir, row["raw_artifacts"])
        _write_json_exclusive(row_path, row)
        _write_digest_sidecar(row_path)
        validate_row_artifacts(row_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--split", required=True, choices=sorted(EXPECTED_SPLITS))
    parser.add_argument("--input-tokens", required=True, type=int)
    parser.add_argument("--output-tokens", required=True, type=int)
    parser.add_argument("--repeat-id", required=True, choices=sorted(EXPECTED_REPEATS))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="validate the sealed CLI/case contract without contacting a server or writing artifacts",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    protocol: dict[str, Any] | None = None
    try:
        protocol = _load_protocol(args.config)
        case = _case_from_args(protocol, args)
        if args.input_tokens <= 0 or args.output_tokens <= 0:
            raise RunnerError("case_invalid", "token targets must be positive")
        if args.validate_only:
            print(
                f"VALIDATE-ONLY: {args.case_id} {args.split} {args.input_tokens}/{args.output_tokens} "
                f"{args.repeat_id}; no server, GPU, trace provider, or artifact access"
            )
            return 0
        if _test_mode() and os.environ.get("H100_TEST_SERVER_URL") is None:
            raise RunnerError("test_mode_invalid", "test mode requires a fake local server")
        _run_case(protocol, args, case)
        return 0
    except RunnerError as exc:
        try:
            if not args.validate_only:
                _unavailable_row(protocol, args, args.output_dir.resolve(), exc)
        except (OSError, RunnerError):
            pass
        print(f"ERROR: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except Exception:
        error = RunnerError("runner_internal", "runner failed without completing the row")
        try:
            if not args.validate_only:
                _unavailable_row(protocol, args, args.output_dir.resolve(), error)
        except (OSError, RunnerError):
            pass
        print(f"ERROR: {error.code}: {error.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
