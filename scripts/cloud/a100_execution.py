#!/usr/bin/env python3
"""Execute the sealed A100 workflow with a bounded wall-clock deadline."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/a100_final_validation.json"
RUNNER = ROOT / "scripts/cloud/a100_case_runner.py"
PROVIDER = ROOT / "scripts/cloud/a100_nsight_trace_provider.py"
FORBIDDEN = {"wall_ms", "cpu_activity_union_ms", "cuda_activity_union_ms",
             "kernel_duration_sum_ms", "actual_prompt_tokens",
             "actual_completion_tokens", "observed_seconds"}


def manifest(path):
    result = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise RuntimeError("invalid manifest line {}".format(number))
        key, value = line.split("=", 1)
        if key in result:
            raise RuntimeError("duplicate manifest key {}".format(key))
        result[key] = value
    return result


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_analysis(command, root):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    subprocess.run([sys.executable, str(ROOT / "scripts/analysis/feature_validation.py"),
                    command, "--config", str(CONFIG), "--artifact-root", str(root)],
                   check=True, env=env)


def cases(split):
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    return data["calibration_configs" if split == "calibration" else "sealed_holdouts"]


def check_deadline(deadline):
    if time.time() >= deadline:
        raise RuntimeError("hard A100 wall-clock deadline reached")


def run_rows(split, root, values, deadline, resume):
    phase = "calibration" if split == "calibration" else "holdout"
    for case in cases(split):
        for repeat in ("r01", "r02", "r03"):
            check_deadline(deadline)
            output = root / phase / case["case_id"] / repeat
            row = output / "row.json"
            if row.exists():
                if not resume:
                    raise RuntimeError("immutable row exists; use --resume: {}".format(row))
                continue
            output.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({"A100_TRACE_PROVIDER": str(PROVIDER),
                        "A100_MODEL_SNAPSHOT": values["MODEL_SNAPSHOT"],
                        "A100_VLLM_BASE_URL": "http://127.0.0.1:{}".format(values["VLLM_PORT"]),
                        "A100_VLLM_MODEL": values["VLLM_MODEL"],
                        "A100_TRACE_MOUNT_ROOT": values["TRACE_ROOT"],
                        "A100_NSYS_CONTAINER": values["A100_CONTAINER"],
                        "A100_NSYS_SESSION": values["A100_NSYS_SESSION"],
                        "A100_NSYS_VERSION": values.get("NSYS_VERSION_PREFIX", "declared-by-startup-manifest")})
            command = [str(RUNNER), "--config", str(CONFIG), "--case-id", case["case_id"],
                       "--split", split, "--input-tokens", str(case["input_tokens"]),
                       "--output-tokens", str(case["output_tokens"]), "--repeat-id", repeat,
                       "--output-dir", str(output)]
            subprocess.run(command, check=True, env=env, timeout=600)
            if not row.is_file():
                raise RuntimeError("runner did not create {}".format(row))


def audit_calibration(root):
    for case in cases("calibration"):
        for repeat in ("r01", "r02", "r03"):
            path = root / "calibration" / case["case_id"] / repeat / "row.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") != "completed" or data.get("schema_version") != "a100-final-row.v1":
                raise RuntimeError("calibration integrity failure: {}".format(path))


def prove_before_reveal(root):
    prediction = root / "derived/prediction_manifest.json"
    if not prediction.is_file() or any((root / "holdout").rglob("row.json")):
        raise RuntimeError("pre-reveal proof failed")
    data = json.loads(prediction.read_text(encoding="utf-8"))

    def walk(value):
        if isinstance(value, dict):
            if FORBIDDEN.intersection(value):
                raise RuntimeError("prediction contains measured label")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    proof = {"schema_version": "a100-pre-reveal-proof.v1",
             "prediction_manifest_sha256": sha(prediction),
             "holdout_rows_seen": False, "holdout_labels_seen": False,
             "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (root / "pre_reveal_proof.json").write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def reveal_and_audit(root):
    prediction = root / "derived/prediction_manifest.json"
    receipt = {"schema_version": "a100-holdout-reveal.v1",
               "prediction_manifest_sha256": sha(prediction),
               "labels_were_unavailable_to_fit": True,
               "revealed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (root / "holdout_reveal_receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = json.loads((root / "derived/holdout_metrics.json").read_text(encoding="utf-8"))
    predictions = json.loads(prediction.read_text(encoding="utf-8"))
    proof = json.loads((root / "pre_reveal_proof.json").read_text(encoding="utf-8"))
    if len(predictions.get("predictions", [])) != 12 or metrics.get("coverage_percent") != 100.0:
        raise RuntimeError("A100 adversarial audit failed")
    if proof.get("holdout_labels_seen") is not False:
        raise RuntimeError("pre-reveal proof is invalid")
    (root / "adversarial_audit.json").write_text(
        json.dumps({"schema_version": "a100-adversarial-audit.v1", "status": "PASS",
                    "prediction_before_reveal": True, "coverage_percent": 100.0},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "freeze_decision.json").write_text(
        json.dumps({"schema_version": "a100-freeze-decision.v1",
                    "status": "READY_TO_FREEZE", "h100_artifacts_touched": False,
                    "a100_only_calibration_fit": True},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--phase", choices=("all", "calibration", "holdout"), default="all")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    values = manifest(args.manifest)
    root = Path(values["ARTIFACT_ROOT"]).resolve()
    max_wall = int(values["MAX_WALL_CLOCK_SECONDS"])
    if max_wall != 14400:
        raise RuntimeError("A100 hard wall must be exactly 14400 seconds")
    root.mkdir(parents=True, exist_ok=True)
    started = int(time.time())
    deadline = started + max_wall
    state_path = root / "run_state.json"
    if state_path.exists() and not args.resume:
        raise RuntimeError("run_state exists; use --resume for an immutable recovery")
    state = {"schema_version": "a100-run-state.v1", "status": "running",
             "phase": args.phase, "protocol": str(CONFIG), "started_epoch": started,
             "deadline_epoch": deadline, "completed": [], "unavailable": []}
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "deadline.json").write_text(
        json.dumps({"schema_version": "a100-deadline.v1", "started_epoch": started,
                    "deadline_epoch": deadline, "max_seconds": max_wall,
                    "set_before_first_calibration_request": True},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        run_analysis("seal", root)
        if args.phase in ("all", "calibration"):
            run_rows("calibration", root, values, deadline, args.resume)
            audit_calibration(root)
            run_analysis("fit", root)
            prove_before_reveal(root)
        if args.phase in ("all", "holdout"):
            if not (root / "pre_reveal_proof.json").is_file():
                raise RuntimeError("holdout blocked until pre-reveal proof exists")
            run_rows("sealed_holdout", root, values, deadline, args.resume)
            reveal_and_audit(root)
            run_analysis("score", root)
        print("A100 validation workflow complete: {}".format(root))
        state["status"] = "completed"
        state["finished_epoch"] = int(time.time())
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except Exception as exc:
        recovery = Path(values["RECOVERY_ROOT"]).resolve()
        recovery.mkdir(parents=True, exist_ok=True)
        (recovery / "failure.json").write_text(
            json.dumps({"schema_version": "a100-recovery.v1", "status": "preserved",
                        "artifact_root": str(root), "reason": str(exc),
                        "labels_copied": False, "h100_artifacts_touched": False},
                       indent=2, sort_keys=True) + "\n", encoding="utf-8")
        state["status"] = "failed"
        state["failure"] = str(exc)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        raise
    finally:
        subprocess.run(["docker", "stop", values["A100_CONTAINER"]], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print("A100 validation failed: {}".format(exc), file=sys.stderr)
        raise SystemExit(1)
