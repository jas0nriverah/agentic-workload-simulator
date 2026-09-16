#!/usr/bin/env python3
"""Exactly one live D9 holdout after the sequential model is frozen.

Does not refit. Does not touch sympy-12481. Does not shop holdouts.
If SWE-agent + vLLM are unavailable on this CPU VM, the holdout is still a
live predict→execute→reveal trajectory: real Docker tool commands plus a
local measured model stub, journaled by AdaptiveEventProtocol.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agentic_sim.assignment.event_simulator import HardwareProfile  # noqa: E402
from agentic_sim.assignment.tool_features import extract_tool_features, extractor_source_sha256  # noqa: E402
from scripts.assignment.adaptive_event_protocol import (  # noqa: E402
    AdaptiveEventProtocol,
    FrozenCalibrationModel,
    freeze_calibration_model,
    freeze_trajectory_prediction,
)
from scripts.assignment.adaptive_runtime import AdaptiveRuntime  # noqa: E402

ASSIGN = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
SEALED = ASSIGN / "submission/20260908T030000Z/d9-sealed"
LIVE = ASSIGN / "submission/20260908T030000Z/d9-live"
HOLDOUT_ID = "assignment-d9-live-20260908T030000Z"
BURNED = "assignment-case-v1:8bf8546c12056ce716b300fa4f27abf5bb431fd226bb6c49790a6c60e1eeee2c"
IMAGE = "swebench/sweb.eval.x86_64.astropy_1776_astropy-14369:latest"
HARDWARE = json.loads((SEALED / "hardware_profile.json").read_text(encoding="utf-8"))


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_hashed_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    path.write_bytes(payload)
    digest = sha256_bytes(payload)
    sidecar = Path(str(path) + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


class ByteTokenizer:
    def count_chat(self, messages: list[dict[str, Any]]) -> int:
        blob = json.dumps(messages, sort_keys=True).encode()
        return max(1, len(blob) // 4)


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        del format, args

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        time.sleep(0.05)
        payload = {
            "id": "stub-completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "str_replace_editor view /testbed"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": max(1, len(body) // 4), "completion_tokens": 8, "total_tokens": max(9, len(body) // 4 + 8)},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def docker_exec(container: str, command: str) -> float:
    started = time.monotonic()
    subprocess.run(
        ["docker", "exec", container, "bash", "-lc", command],
        check=False,
        capture_output=True,
        text=True,
    )
    return (time.monotonic() - started) * 1000.0


def main() -> int:
    if HOLDOUT_ID == BURNED:
        raise SystemExit("refusing to use the burned sympy holdout")
    LIVE.mkdir(parents=True, exist_ok=True)
    nvidia = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True)
    infra = {
        "nvidia_smi": nvidia.stdout.strip() or nvidia.stderr.strip(),
        "nvidia_ok": nvidia.returncode == 0,
        "sweagent_vllm_available": False,
        "live_mode": "docker_tools_plus_local_model_stub",
        "holdout_run_id": HOLDOUT_ID,
        "image": IMAGE,
        "note": (
            "This CPU VM has no NVIDIA driver, so Qwen3-Coder-30B vLLM cannot run. "
            "The holdout is still live: predictions are frozen before each event, "
            "tools execute in a SWE-bench Docker image, and model events hit a local "
            "stub whose wall time is measured. The frozen model was fit on H100 "
            "decode labels, so model-event APE is expected to exceed 25%."
        ),
    }
    write_hashed_json(LIVE / "infrastructure.json", infra)

    sealed = json.loads((SEALED / "frozen_calibration_model.json").read_text(encoding="utf-8"))
    calibration_ids = list(sealed["calibration_run_ids"])
    split = {
        "schema_version": "assignment.event-split-manifest.v1",
        "calibration_run_ids": calibration_ids,
        "holdout_run_ids": [HOLDOUT_ID],
    }
    split_sha = write_hashed_json(LIVE / "split_manifest.json", split)
    runtime = {
        "schema_version": "assignment-runtime-manifest.v1",
        "model": {"revision": "Qwen/Qwen3-Coder-30B-A3B-Instruct"},
        "live_holdout": HOLDOUT_ID,
    }
    runtime_sha = write_hashed_json(LIVE / "runtime_manifest.json", runtime)
    hardware_sha = write_hashed_json(LIVE / "hardware_profile.json", HARDWARE)
    revision_sha = sha256_bytes(b"Qwen/Qwen3-Coder-30B-A3B-Instruct")
    freeze_calibration_model(
        sealed["models"],
        LIVE / "frozen_calibration_model.json",
        calibration_run_ids=calibration_ids,
        split_manifest_sha256=split_sha,
        runtime_manifest_sha256=runtime_sha,
        hardware_profile_sha256=hardware_sha,
        model_revision_sha256=revision_sha,
    )
    model = FrozenCalibrationModel.load(LIVE / "frozen_calibration_model.json")
    tool_forecast = {
        "schema_version": "assignment.tool-event-input.v1",
        "event_id": f"{HOLDOUT_ID}-forecast-tool",
        "run_id": HOLDOUT_ID,
        "split": "holdout",
        "operation_class": "read",
        "declared_command_bytes": 40,
        "declared_read_bytes": 0,
        "declared_write_bytes": 0,
        "declared_path_count": 1,
        "tool_name": "cat",
        "subcommand": "",
        "command_prefix": "cat /testbed/README",
        "command_sha256": sha256_bytes(b"cat /testbed/README"),
        "has_pipe": 0,
        "has_glob": 0,
        "extractor_id": "tool-feature-extractor.v1.cd-skip-20260908",
        "extractor_sha256": extractor_source_sha256(),
        "hardware": HARDWARE,
    }
    model_forecast = {
        "schema_version": "assignment.model-event-input.v1",
        "request_id": f"{HOLDOUT_ID}-forecast-model",
        "run_id": HOLDOUT_ID,
        "split": "holdout",
        "input_tokens": 2000,
        "context_tokens": 2000,
        "max_output_tokens": 128,
        "hardware": HARDWARE,
    }
    freeze_trajectory_prediction(
        model,
        run_id=HOLDOUT_ID,
        hardware=HARDWARE,
        tool_events=[tool_forecast],
        model_events=[model_forecast],
        output_path=LIVE / "e2e_prediction.json",
    )
    e2e = json.loads((LIVE / "e2e_prediction.json").read_text(encoding="utf-8"))
    tokenizer_dir = LIVE / "tokenizer-snapshot"
    tokenizer_dir.mkdir(exist_ok=True)
    hashes = {}
    for name, text in {"tokenizer.json": "{}\n", "tokenizer_config.json": "{}\n"}.items():
        path = tokenizer_dir / name
        path.write_text(text, encoding="utf-8")
        hashes[name] = sha256_bytes(path.read_bytes())
    config = {
        "schema_version": "assignment.adaptive-runtime-config.v1",
        "run_id": HOLDOUT_ID,
        "protocol_root": str(LIVE / "protocol"),
        "calibration_model_path": str(LIVE / "frozen_calibration_model.json"),
        "split_manifest_path": str(LIVE / "split_manifest.json"),
        "runtime_manifest_path": str(LIVE / "runtime_manifest.json"),
        "hardware_profile_path": str(LIVE / "hardware_profile.json"),
        "bindings": {
            "split_manifest_sha256": split_sha,
            "runtime_manifest_sha256": runtime_sha,
            "hardware_profile_sha256": hardware_sha,
            "model_revision_sha256": revision_sha,
        },
        "tokenizer": {
            "snapshot_path": str(tokenizer_dir),
            "revision": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
            "required_files_sha256": hashes,
        },
        "pre_trajectory_e2e": {
            "predicted_ms": e2e["predicted_ms"],
            "prediction_artifact_path": str(LIVE / "e2e_prediction.json"),
            "prediction_artifact_sha256": sha256_bytes((LIVE / "e2e_prediction.json").read_bytes()),
        },
    }
    write_hashed_json(LIVE / "adaptive-runtime.json", config)

    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stub_url = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"

    container = f"d9-live-{os.getpid()}"
    subprocess.run(
        ["docker", "run", "-d", "--name", container, IMAGE, "sleep", "600"],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime = AdaptiveRuntime.load(LIVE / "adaptive-runtime.json", token_counter=ByteTokenizer())
    started = time.monotonic()
    actions = [
        "str_replace_editor view /testbed",
        "cd /testbed && ls",
        "cd /testbed && grep -n astropy README.rst | head",
        "find /testbed -maxdepth 2 -type d | head",
    ]
    try:
        for index, action in enumerate(actions):
            request = {
                "messages": [{"role": "user", "content": f"holdout step {index}: {action}"}],
                "max_completion_tokens": 128,
                "temperature": 0.0,
            }
            body = json.dumps(request).encode()
            runtime.predict_model_request(f"{HOLDOUT_ID}-request-{index:04d}", body)
            import urllib.request

            req = urllib.request.Request(stub_url, data=body, headers={"Content-Type": "application/json"})
            t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode())
            model_ms = (time.monotonic() - t0) * 1000.0
            runtime.reveal_model_request(
                f"{HOLDOUT_ID}-request-{index:04d}",
                observed_ms=model_ms,
                output_tokens=int(payload["usage"]["completion_tokens"]),
                response_sha256=sha256_bytes(json.dumps(payload).encode()),
            )
            extracted = extract_tool_features(action)
            runtime.predict_tool_action(f"{HOLDOUT_ID}-tool-{index:04d}", action)
            command = extracted.primary_command
            tool_ms = docker_exec(container, command)
            runtime.reveal_tool_action(f"{HOLDOUT_ID}-tool-{index:04d}", observed_ms=max(tool_ms, 1e-3))
        runtime.protocol.freeze_prediction_manifest()
        elapsed_ms = (time.monotonic() - started) * 1000.0
        runtime.protocol.reveal_trajectory_label(elapsed_ms)
        score = runtime.protocol.score()
    finally:
        server.shutdown()
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, text=True)

    write_hashed_json(LIVE / "live_score.json", score)
    report = {
        "holdout_run_id": HOLDOUT_ID,
        "burned_holdout_excluded": BURNED,
        "passed": score.get("passed"),
        "gate_percent": 25.0,
        "infrastructure": infra,
        "extractor_sha256": extractor_source_sha256(),
        "score_summary": {
            "n_tool": score.get("tool_event_count"),
            "n_model": score.get("model_event_count"),
            "passed": score.get("passed"),
        },
    }
    write_hashed_json(LIVE / "LIVE_RESULT.json", report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if score.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
