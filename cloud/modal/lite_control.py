"""Run one pinned Lite control trajectory on a Modal H100.

The agent uses SWE-agent's local deployment inside the GPU container because
Modal Functions do not provide a Docker daemon. The official SWE-bench
evaluator is then run separately in a VM-runtime Sandbox with Docker. This
keeps the model-serving and evaluation boundaries explicit while preserving
the assignment's pinned model and command settings.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import modal

MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
VLLM_IMAGE = (
    "vllm/vllm-openai:v0.10.0@"
    "sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271"
)
SWE_AGENT_REVISION = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
SWE_BENCH_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
DATASET_REVISION = "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e"
INSTANCE_ID = os.environ.get("EIC_INSTANCE_ID", "astropy__astropy-12907")
BASE_COMMIT = os.environ.get("EIC_BASE_COMMIT", "d16bfe05a744909de4b27f5875fe0d4ed41ce607")
DATASET_REVISION = os.environ.get("EIC_DATASET_REVISION", DATASET_REVISION)
DATASET_NAME = os.environ.get("EIC_DATASET_NAME", "SWE-bench/SWE-bench_Lite")
RESULT_ROOT = os.environ.get("EIC_RESULT_ROOT", "lite-control-full-prompt")
RUN_ID = os.environ.get("EIC_RUN_ID", "modal-lite-control-full-prompt")
CALL_LIMIT = int(os.environ.get("EIC_CALL_LIMIT", "30"))
COMPLETION_MAX_TOKENS = int(os.environ.get("EIC_COMPLETION_MAX_TOKENS", "2048"))
MAX_OBSERVATION_LENGTH = int(os.environ.get("EIC_MAX_OBSERVATION_LENGTH", "100000"))
TEMPERATURE = float(os.environ.get("EIC_TEMPERATURE", "0.0"))
PROFILE = os.environ.get("EIC_PROFILE", "0") == "1"
DEEP_PROFILE = os.environ.get("EIC_DEEP_PROFILE", "0") == "1"

PROBLEM_STATEMENT = (
    "Modeling's `separability_matrix` does not compute separability correctly for "
    "nested CompoundModels.\n\n"
    "Consider the following model:\n\n"
    "from astropy.modeling import models as m\n"
    "from astropy.modeling.separable import separability_matrix\n\n"
    "cm = m.Linear1D(10) & m.Linear1D(5)\n\n"
    "Its separability matrix is the expected diagonal. If the model is made more "
    "complex, `m.Pix2Sky_TAN() & m.Linear1D(10) & m.Linear1D(5)` also gives the "
    "expected independent blocks. However, nesting these compound models as "
    "`m.Pix2Sky_TAN() & cm` incorrectly marks the two Linear1D inputs as coupled. "
    "Correctly calculate the separability matrix for nested compound models, "
    "especially when a CompoundModel is the right operand of `&`. Add the minimal "
    "source change needed for the behavior and do not modify tests."
)
if INSTANCE_ID == "astropy__astropy-14365":
    PROBLEM_STATEMENT = (
        "ascii.qdp Table format assumes QDP commands are upper case.\n\n"
        "ascii.qdp assumes that commands in a QDP file are upper case, for example, "
        "for errors they must be `READ SERR 1 2` whereas QDP itself is not case "
        "sensitive and can use `read serr 1 2`. As many QDP files are created by "
        "hand, the expectation that all commands be all-caps should be removed.\n\n"
        "The expected behavior is that a QDP file containing `read serr 1 2` and "
        "numeric data reads into an Astropy Table with errors instead of raising "
        "an unrecognized-line error. Make the minimal source-only change needed "
        "and do not modify tests."
    )
if os.environ.get("EIC_PROBLEM_STATEMENT"):
    PROBLEM_STATEMENT = os.environ["EIC_PROBLEM_STATEMENT"]

app = modal.App("eic-lite-control")
results = modal.Volume.from_name("eic-results", create_if_missing=True)

agent_image = (
    modal.Image.from_registry(VLLM_IMAGE)
    .entrypoint([])
    .run_commands("ln -sf $(command -v python3) /usr/local/bin/python")
    .apt_install("git", "curl", "strace")
    .run_commands(
        "python3 -m venv --system-site-packages /opt/eic-agent-venv "
        "&& /opt/eic-agent-venv/bin/python -m pip install --no-cache-dir "
        "beautifulsoup4==4.15.0 chardet==7.6.0 datasets==5.0.1 "
        "flask==3.1.3 flask-cors==6.0.5 flask-socketio==5.6.1 "
        "ghapi==2.1.2 gitpython==3.1.59 litellm==1.97.0 "
        "pydantic-settings==2.15.0 python-dotenv==1.2.2 rich==15.0.0 "
        "rich-argparse==1.8.0 ruamel-yaml==0.19.1 simple-parsing==0.1.9 "
        "swe-rex==1.4.0 tabulate==0.10.0 tenacity==9.1.4 "
        "textual==8.2.8 unidiff==1.0.0"
    )
    .run_commands(
        f"git clone --filter=blob:none https://github.com/SWE-agent/SWE-agent.git /opt/SWE-agent "
        f"&& git -C /opt/SWE-agent fetch --depth 1 origin {SWE_AGENT_REVISION} "
        f"&& git -C /opt/SWE-agent checkout --detach {SWE_AGENT_REVISION} "
        "&& /opt/eic-agent-venv/bin/python -m pip install --no-cache-dir --no-deps -e /opt/SWE-agent",
    )
)

evaluator_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates", "docker.io", "git")
    .pip_install(
        "beautifulsoup4==4.15.0",
        "chardet==7.6.0",
        "datasets==5.0.1",
        "docker==7.2.0",
        "ghapi==2.1.2",
        "gitpython==3.1.59",
        "modal==1.5.4",
        "python-dotenv==1.2.2",
        "requests==2.34.2",
        "rich==15.0.0",
        "tenacity==9.1.4",
        "tqdm==4.70.0",
        "unidiff==1.0.0",
    )
    .run_commands(
        f"git clone --filter=blob:none https://github.com/SWE-bench/SWE-bench.git /opt/SWE-bench "
        f"&& git -C /opt/SWE-bench fetch --depth 1 origin {SWE_BENCH_REVISION} "
        f"&& git -C /opt/SWE-bench checkout --detach {SWE_BENCH_REVISION} "
        "&& python -m pip install --no-cache-dir --no-deps -e /opt/SWE-bench",
    )
)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@app.function(
    image=agent_image,
    gpu="H100!",
    timeout=2 * 60 * 60,
    memory=65536,
    cpu=8,
    retries=0,
    volumes={
        "/results": results,
        "/root/.cache/huggingface": modal.Volume.from_name("eic-hf-cache", create_if_missing=True),
        "/root/.cache/vllm": modal.Volume.from_name("eic-vllm-cache", create_if_missing=True),
    },
)
def run_agent(runtime_config: dict | None = None) -> dict:
    import os
    import subprocess
    import time
    import urllib.request

    config = {
        "instance_id": INSTANCE_ID,
        "base_commit": BASE_COMMIT,
        "dataset_revision": DATASET_REVISION,
        "problem_statement": PROBLEM_STATEMENT,
        "result_root": RESULT_ROOT,
        "run_id": RUN_ID,
        "call_limit": CALL_LIMIT,
        "completion_max_tokens": COMPLETION_MAX_TOKENS,
        "max_observation_length": MAX_OBSERVATION_LENGTH,
        "temperature": TEMPERATURE,
        "profile": PROFILE,
        "deep_profile": DEEP_PROFILE,
    }
    if runtime_config:
        config.update(runtime_config)
    instance_id = config["instance_id"]
    base_commit = config["base_commit"]
    dataset_revision = config["dataset_revision"]
    problem_statement = config["problem_statement"]
    result_root = config["result_root"]
    run_id = config["run_id"]
    profile = bool(config["profile"])
    deep_profile = bool(config.get("deep_profile", False))

    root = Path("/results") / result_root
    output_dir = root / "sweagent-output"
    root.mkdir(parents=True, exist_ok=True)
    events_path = root / "events.jsonl"

    def record_event(event_type: str, start_ns: int, end_ns: int, source: str, payload: dict) -> None:
        if not profile:
            return
        event = {
            "schema_version": "modal-profile-event.v1",
            "event_id": f"{run_id}:{event_type}:{start_ns}",
            "run_id": run_id,
            "instance_id": instance_id,
            "event_type": event_type,
            "start_time_ns": start_ns,
            "end_time_ns": end_ns,
            "duration_ms": (end_ns - start_ns) / 1_000_000,
            "provenance": "measured",
            "source": source,
            "payload": payload,
        }
        with events_path.open("a", encoding="utf-8") as events:
            events.write(json.dumps(event, sort_keys=True) + "\n")

    def capture_vllm_metrics(path: Path) -> None:
        if not deep_profile:
            return
        try:
            metrics = urllib.request.urlopen("http://127.0.0.1:8000/metrics", timeout=10).read()
            path.write_bytes(metrics)
        except Exception as exc:
            path.write_text(f"capture_error={type(exc).__name__}: {exc}\n", encoding="utf-8")

    os.environ["HF_HUB_CACHE"] = "/root/.cache/huggingface"
    # Do not let a host-level VLLM_API_KEY turn on server authentication
    # without also adding an Authorization header to the readiness probe.
    os.environ.pop("VLLM_API_KEY", None)
    os.environ["ENV_DIR"] = "/"
    os.environ["REPO_BASE_DIR"] = "/"

    log_path = root / "vllm.log"
    vllm_cmd = [
        "python3",
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        MODEL,
        "--revision",
        MODEL_REVISION,
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
    vllm = None
    started = _now()
    try:
        with log_path.open("w", encoding="utf-8") as log:
            vllm = subprocess.Popen(vllm_cmd, stdout=log, stderr=subprocess.STDOUT)
        vllm_start_ns = time.monotonic_ns()
        deadline = time.time() + 55 * 60
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:8000/v1/models", timeout=5).read()
                break
            except Exception:
                if vllm.poll() is not None:
                    raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace")[-12000:])
                time.sleep(5)
        else:
            raise TimeoutError(log_path.read_text(encoding="utf-8", errors="replace")[-12000:])
        record_event(
            "vllm_service_ready",
            vllm_start_ns,
            time.monotonic_ns(),
            "vllm_readiness_probe",
            {"model": MODEL, "metrics_scope": "server_aggregate"},
        )
        if profile:
            hardware = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version,memory.total",
                    "--format=csv,noheader",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            (root / "hardware.txt").write_text(
                hardware.stdout or hardware.stderr,
                encoding="utf-8",
            )
        capture_vllm_metrics(root / "vllm_metrics_before.prom")

        repo = Path("/testbed")
        clone_start_ns = time.monotonic_ns()
        subprocess.run(
            ["git", "clone", "--filter=blob:none", "https://github.com/astropy/astropy.git", str(repo)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        subprocess.run(["git", "-C", str(repo), "fetch", "--depth", "1", "origin", base_commit], check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", base_commit], check=True)
        record_event(
            "repository_prepare",
            clone_start_ns,
            time.monotonic_ns(),
            "git_clone_fetch_checkout",
            {"base_commit": base_commit},
        )

        # The agent file is intentionally limited to the selected row. The
        # official evaluator later loads the immutable full dataset revision.
        instance = {
            "instance_id": instance_id,
            # LocalDeploymentConfig rejects Docker image names. The official
            # evaluator derives the immutable image name from the full row.
            "image_name": "",
            "problem_statement": problem_statement,
            "repo_name": "testbed",
            "base_commit": base_commit,
        }
        instance_path = root / "instance.json"
        instance_path.write_text(json.dumps([instance], sort_keys=True) + "\n", encoding="utf-8")
        request_path = root / "sweagent_request.yaml"
        request_path.write_text(
            "agent:\n  model:\n    completion_kwargs:\n"
            f"      max_tokens: {config['completion_max_tokens']}\n"
            "      seed: 0\n",
            encoding="utf-8",
        )

        command = [
            "/opt/eic-agent-venv/bin/sweagent",
            "run-batch",
            "--config",
            "/opt/SWE-agent/config/default.yaml",
            "--config",
            str(request_path),
            "--instances.type",
            "file",
            "--instances.path",
            str(instance_path),
            "--instances.filter",
            f"^{instance_id}$",
            "--instances.deployment.type",
            "local",
            "--agent.model.name",
            f"openai/{MODEL}",
            "--agent.model.api_base",
            "http://127.0.0.1:8000/v1",
            "--agent.model.api_key",
            "modal-local-only-key",
            "--agent.model.total_cost_limit",
            "0",
            "--agent.model.per_instance_cost_limit",
            "0",
            "--agent.model.per_instance_call_limit",
            str(config["call_limit"]),
            "--agent.model.temperature",
            str(config["temperature"]),
            "--agent.model.max_input_tokens",
            "32768",
            "--agent.model.max_output_tokens",
            str(config["completion_max_tokens"]),
            "--agent.templates.max_observation_length",
            str(config["max_observation_length"]),
            "--output_dir",
            str(output_dir),
            "--num_workers",
            "1",
        ]
        command_text = " ".join(shlex.quote(item) for item in command)
        agent_command = command_text
        if deep_profile:
            agent_command = " ".join(
                shlex.quote(item)
                for item in [
                    "strace",
                    "-f",
                    "-ttt",
                    "-T",
                    "-e",
                    "trace=file",
                    "-o",
                    str(root / "cpu_file_events.strace"),
                    "bash",
                    "-lc",
                    command_text,
                ]
            )
        (root / "command.txt").write_text(agent_command + "\n", encoding="utf-8")
        agent_env = os.environ.copy()
        agent_env["VLLM_API_KEY"] = "modal-local-only-key"
        agent_start_ns = time.monotonic_ns()
        with (root / "agent.log").open("w", encoding="utf-8") as log:
            agent = subprocess.run(
                agent_command,
                shell=True,
                executable="/bin/bash",
                env=agent_env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=70 * 60,
                check=False,
            )
        capture_vllm_metrics(root / "vllm_metrics_after.prom")
        record_event(
            "sweagent_trajectory",
            agent_start_ns,
            time.monotonic_ns(),
            "sweagent_subprocess",
            {
                "returncode": agent.returncode,
                "call_limit": config["call_limit"],
                "completion_max_tokens": config["completion_max_tokens"],
                "max_observation_length": config["max_observation_length"],
                "temperature": config["temperature"],
            },
        )

        predictions = list(output_dir.rglob("preds.json"))
        trajectories = list(output_dir.rglob("*.traj"))
        if predictions:
            canonical_prediction = root / "preds.json"
            canonical_prediction.write_bytes(predictions[0].read_bytes())
        if trajectories:
            (root / "trajectory.traj").write_bytes(trajectories[0].read_bytes())
        record_event(
            "prediction_collection",
            agent_start_ns,
            time.monotonic_ns(),
            "artifact_collection",
            {"prediction_found": bool(predictions), "trajectory_found": bool(trajectories)},
        )
        payload = {
            "schema_version": "modal-lite-control.v1",
            "status": "agent_completed" if agent.returncode == 0 else "agent_failed",
            "provenance": "measured",
            "provider": "modal",
            "gpu_request": "H100!",
            "model": MODEL,
            "model_revision": MODEL_REVISION,
            "vllm_image": VLLM_IMAGE,
            "swe_agent_revision": SWE_AGENT_REVISION,
            "swe_bench_revision": SWE_BENCH_REVISION,
            "dataset_revision": dataset_revision,
            "instance_id": instance_id,
            "base_commit": base_commit,
            "started_at_utc": started,
            "ended_at_utc": _now(),
            "agent_returncode": agent.returncode,
            "prediction_path": str(root / "preds.json") if predictions else None,
            "trajectory_path": str(root / "trajectory.traj") if trajectories else None,
            "run_id": run_id,
            "call_limit": config["call_limit"],
            "completion_max_tokens": config["completion_max_tokens"],
            "max_observation_length": config["max_observation_length"],
            "temperature": config["temperature"],
            "profile": profile,
            "deep_profile": deep_profile,
            "command_sha256": __import__("hashlib").sha256(command_text.encode()).hexdigest(),
        }
        (root / "agent_result.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        results.commit()
        return payload
    finally:
        if vllm is not None and vllm.poll() is None:
            vllm.terminate()
            try:
                vllm.wait(timeout=30)
            except subprocess.TimeoutExpired:
                vllm.kill()


@app.function(image=modal.Image.debian_slim(), volumes={"/results": results})
def store_evaluation_output(output: str, result_root: str) -> None:
    path = Path("/results") / result_root / "evaluator.stdout.log"
    path.write_text(output, encoding="utf-8")
    results.commit()


@app.local_entrypoint()
def main() -> None:
    runtime_config = {
        "instance_id": INSTANCE_ID,
        "base_commit": BASE_COMMIT,
        "dataset_revision": DATASET_REVISION,
        "problem_statement": PROBLEM_STATEMENT,
        "result_root": RESULT_ROOT,
        "run_id": RUN_ID,
        "call_limit": CALL_LIMIT,
        "completion_max_tokens": COMPLETION_MAX_TOKENS,
        "max_observation_length": MAX_OBSERVATION_LENGTH,
        "temperature": TEMPERATURE,
        "profile": PROFILE,
        "deep_profile": DEEP_PROFILE,
    }
    print(f"Launching pinned {DATASET_NAME} control trajectory on Modal H100...")
    result = run_agent.remote(runtime_config)
    print(json.dumps(result, indent=2))
    if result.get("agent_returncode") != 0 or not result.get("prediction_path"):
        print("Agent did not produce a prediction; official evaluation was not started.")
        return

    eval_command = (
        "set -Eeuo pipefail; "
        f"mkdir -p /results/{RESULT_ROOT}/evaluation; "
        "dockerd --host=unix:///var/run/docker.sock >/tmp/dockerd.log 2>&1 & "
        "for i in $(seq 1 180); do docker info >/dev/null 2>&1 && break; sleep 1; done; "
        "docker info >/dev/null 2>&1; "
        f"cd /results/{RESULT_ROOT}/evaluation; "
        "python -m swebench.harness.run_evaluation "
        f"--dataset_name {DATASET_NAME} "
        "--split test "
        f"--predictions_path /results/{RESULT_ROOT}/preds.json "
        f"--instance_ids {INSTANCE_ID} "
        "--max_workers 1 --timeout 1800 --cache_level instance --clean False "
        f"--run_id {RUN_ID} --namespace swebench --instance_image_tag latest "
        f"--report_dir /results/{RESULT_ROOT}/evaluation"
    )
    print("Starting official evaluator in Docker-enabled Modal VM Sandbox...")
    sandbox = modal.Sandbox.create(
        "bash",
        "-lc",
        eval_command,
        app=app,
        image=evaluator_image,
        timeout=90 * 60,
        idle_timeout=90 * 60,
        cpu=8,
        memory=32768,
        volumes={"/results": results},
        experimental_options={"vm_runtime": True},
    )
    output = sandbox.stdout.read()
    stderr = sandbox.stderr.read()
    sandbox.wait(raise_on_termination=False)
    combined = output + ("\nSTDERR:\n" + stderr if stderr else "")
    print(combined)
    store_evaluation_output.remote(combined, RESULT_ROOT)
    print(f"Evaluator sandbox exit code: {sandbox.returncode}")
