"""Pinned vLLM health job on one Modal H100.

Assignment G1 on Modal: nvidia-smi, model fit, one completion, one parsed
qwen3_coder tool call, and native /metrics. Not a Lambda result. Does not
start SWE-agent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import modal

MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
IMAGE = "vllm/vllm-openai:v0.10.0@sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
REQUIRED_METRICS = (
    "vllm:request_success_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
)

app = modal.App("eic-h100-vllm-health")
hf = modal.Volume.from_name("eic-hf-cache", create_if_missing=True)
vllm_cache = modal.Volume.from_name("eic-vllm-cache", create_if_missing=True)
results = modal.Volume.from_name("eic-results", create_if_missing=True)
image = (
    modal.Image.from_registry(IMAGE)
    .entrypoint([])
    .run_commands("ln -sf $(command -v python3) /usr/local/bin/python && python --version")
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@app.function(
    image=image,
    gpu="H100!",
    timeout=90 * 60,
    memory=65536,
    cpu=8,
    retries=0,
    volumes={
        "/root/.cache/huggingface": hf,
        "/root/.cache/vllm": vllm_cache,
        "/results": results,
    },
)
def vllm_health() -> dict:
    import os
    import platform
    import subprocess
    import time
    import urllib.request

    os.environ["HF_HUB_CACHE"] = "/root/.cache/huggingface"
    gpu = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader",
        ],
        text=True,
    ).strip()
    machine = platform.machine()
    if machine not in {"x86_64", "AMD64"}:
        raise RuntimeError(f"expected linux/amd64 host, observed {machine}")

    # Same argv tail as cloud/lambda/lambda_start_vllm.sh after the pinned image.
    cmd = [
        "python3",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        MODEL,
        "--revision",
        REVISION,
        "--served-model-name",
        MODEL,
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--dtype",
        "bfloat16",
        "--max-model-len",
        "32768",
        "--gpu-memory-utilization",
        "0.90",
        "--tensor-parallel-size",
        "1",
        "--enable-auto-tool-choice",
        "--tool-call-parser",
        "qwen3_coder",
    ]
    log_path = "/tmp/vllm.log"
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    deadline = time.time() + 50 * 60
    started = utc_now()
    while time.time() < deadline:
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5)
            break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError(open(log_path, encoding="utf-8", errors="replace").read()[-6000:])
            time.sleep(5)
    else:
        proc.kill()
        raise TimeoutError(open(log_path, encoding="utf-8", errors="replace").read()[-6000:])

    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            "http://127.0.0.1:8000" + path,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode())

    models = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/v1/models").read().decode())
    model_id = models["data"][0]["id"]
    completion = post(
        "/v1/chat/completions",
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "Reply READY."}],
            "max_tokens": 8,
            "temperature": 0,
        },
    )
    if not completion.get("choices"):
        raise RuntimeError("normal completion returned no choices")
    tool = post(
        "/v1/chat/completions",
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "Call ping."}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Return pong",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "ping"}},
            "max_tokens": 32,
            "temperature": 0,
        },
    )
    tool_calls = tool.get("choices", [{}])[0].get("message", {}).get("tool_calls")
    metrics = urllib.request.urlopen("http://127.0.0.1:8000/metrics").read().decode()
    missing = [name for name in REQUIRED_METRICS if name not in metrics]
    if "vllm:e2e_request_latency_seconds" not in metrics:
        missing.append("vllm:e2e_request_latency_seconds")
    gpu_after = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader",
        ],
        text=True,
    ).strip()
    proc.terminate()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()

    if tool_calls and not missing:
        status = "PASS"
    elif not tool_calls:
        status = "TOOL_PARSER_FAIL"
    else:
        status = "METRICS_FAIL"
    payload = {
        "schema_version": "modal-vllm-health.v1",
        "status": status,
        "provenance": "measured",
        "provider": "modal",
        "gpu_request": "H100!",
        "gpu_before": gpu,
        "gpu_after": gpu_after,
        "machine": machine,
        "model": MODEL,
        "model_revision": REVISION,
        "vllm_image": IMAGE,
        "model_id": model_id,
        "max_model_len": 32768,
        "tool_parser": "qwen3_coder",
        "completion_preview": completion.get("choices", [{}])[0].get("message", {}).get("content"),
        "tool_calls_present": bool(tool_calls),
        "missing_metrics": missing,
        "started_at_utc": started,
        "ended_at_utc": utc_now(),
    }
    os.makedirs("/results/vllm-health", exist_ok=True)
    out = f"/results/vllm-health/health-{payload['ended_at_utc'].replace(':', '')}.json"
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    results.commit()
    payload["result_path"] = out
    return payload


@app.local_entrypoint()
def main() -> None:
    print(json.dumps(vllm_health.remote(), indent=2))
