#!/usr/bin/env python3
"""Execute the sealed A100 workflow with a bounded wall-clock deadline."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.analysis.feature_validation import load_protocol  # noqa: E402

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


def write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                    prefix=path.name + ".", delete=False) as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def record_row(state, state_path, split, case_id, repeat_id, status):
    if status not in {"completed", "unavailable"}:
        raise RuntimeError("row status is not terminal: {}".format(status))
    bucket = state[status]
    entry = {"split": split, "case_id": case_id, "repeat_id": repeat_id}
    if entry not in bucket:
        bucket.append(entry)
        bucket.sort(key=lambda item: (item["split"], item["case_id"], item["repeat_id"]))
        write_json_atomic(state_path, state)


def row_status(row):
    try:
        data = json.loads(row.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read immutable row: {}".format(row)) from exc
    if data.get("schema_version") != "a100-final-row.v1":
        raise RuntimeError("immutable row schema mismatch: {}".format(row))
    status = data.get("status")
    if status not in {"completed", "unavailable"}:
        raise RuntimeError("immutable row is not terminal: {}".format(row))
    return status


def run_rows(split, root, values, deadline, resume, state, state_path):
    phase = "calibration" if split == "calibration" else "holdout"
    trace_root = Path(values["TRACE_ROOT"]).resolve()
    for case in cases(split):
        for repeat in ("r01", "r02", "r03"):
            check_deadline(deadline)
            output = root / phase / case["case_id"] / repeat
            row = output / "row.json"
            if row.exists():
                if not resume:
                    raise RuntimeError("immutable row exists; use --resume: {}".format(row))
                record_row(state, state_path, split, case["case_id"], repeat, row_status(row))
                continue
            trace_output = trace_root / phase / case["case_id"] / repeat
            if trace_output.exists():
                raise RuntimeError("immutable trace workspace exists; inspect before recovery: {}".format(trace_output))
            trace_output.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env.update({"A100_TRACE_PROVIDER": str(PROVIDER),
                        "BACKEND": "docker",
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
                       "--output-dir", str(trace_output)]
            try:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise RuntimeError("hard A100 wall-clock deadline reached before request")
                subprocess.run(command, check=True, env=env, timeout=min(600.0, remaining))
                if time.time() >= deadline:
                    raise RuntimeError("hard A100 wall-clock deadline reached after request")
                if not (trace_output / "row.json").is_file():
                    raise RuntimeError("runner did not create {}".format(trace_output / "row.json"))
            except Exception:
                if trace_output.exists() and not output.exists():
                    output.parent.mkdir(parents=True, exist_ok=True)
                    trace_output.replace(output)
                raise
            if output.exists():
                raise RuntimeError("canonical output would be overwritten: {}".format(output))
            output.parent.mkdir(parents=True, exist_ok=True)
            trace_output.replace(output)
            record_row(state, state_path, split, case["case_id"], repeat, row_status(output / "row.json"))


def audit_calibration(root):
    for case in cases("calibration"):
        for repeat in ("r01", "r02", "r03"):
            path = root / "calibration" / case["case_id"] / repeat / "row.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") != "completed" or data.get("schema_version") != "a100-final-row.v1":
                raise RuntimeError("calibration integrity failure: {}".format(path))


def sealed_identity(root):
    """Recompute identities from bytes and validate the split's actual case lists."""
    protocol, protocol_hash, split_hash = load_protocol(CONFIG)
    split_path = root / "split_manifest.json"
    split = json.loads(split_path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": "a100-final-split.v1",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_hash,
        "split_sha256": split_hash,
        "sealed": True,
        "calibration_case_ids": [row["case_id"] for row in protocol["calibration_configs"]],
        "sealed_holdout_case_ids": [row["case_id"] for row in protocol["sealed_holdouts"]],
    }
    if any(split.get(key) != value for key, value in expected.items()):
        raise RuntimeError("sealed split/case manifest mismatch")
    if sha(root / "protocol.config.json") != protocol_hash:
        raise RuntimeError("sealed protocol hash mismatch")
    if (root / "protocol.sha256").read_text().split() != [protocol_hash, "protocol.config.json"]:
        raise RuntimeError("sealed protocol sidecar mismatch")
    return {"protocol_sha256": protocol_hash, "split_sha256": split_hash,
            "split_manifest_sha256": sha(split_path)}


def verify_before_reveal(root):
    """Fail closed on missing, legacy, or stale pre-reveal authorization."""
    proof = json.loads((root / "pre_reveal_proof.json").read_text(encoding="utf-8"))
    identity = sealed_identity(root)
    prediction = root / "derived/prediction_manifest.json"
    identity["prediction_manifest_sha256"] = sha(prediction)
    if (proof.get("schema_version") != "a100-pre-reveal-proof.v2"
            or proof.get("holdout_rows_seen") is not False
            or proof.get("holdout_labels_seen") is not False
            or any(proof.get(key) != value for key, value in identity.items())):
        raise RuntimeError("pre-reveal proof does not match current sealed artifacts")
    data = json.loads(prediction.read_text(encoding="utf-8"))
    if (data.get("protocol_sha256") != identity["protocol_sha256"]
            or data.get("split_manifest_sha256") != identity["split_sha256"]):
        raise RuntimeError("prediction protocol/split hash mismatch")
    if (root / "derived/prediction_manifest.sha256").read_text().split() != [
            identity["prediction_manifest_sha256"], "prediction_manifest.json"]:
        raise RuntimeError("prediction sidecar mismatch")
    return identity


def prove_before_reveal(root):
    if (root / "pre_reveal_proof.json").exists():
        verify_before_reveal(root)
        return
    identity = sealed_identity(root)
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
    proof = {**identity, "schema_version": "a100-pre-reveal-proof.v2",
             "prediction_manifest_sha256": sha(prediction),
             "holdout_rows_seen": False, "holdout_labels_seen": False,
             "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with (root / "pre_reveal_proof.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(proof, indent=2, sort_keys=True) + "\n")
    verify_before_reveal(root)


def reveal_holdout(root):
    prediction = root / "derived/prediction_manifest.json"
    split_manifest = json.loads((root / "split_manifest.json").read_text(encoding="utf-8"))
    receipt = {"schema_version": "a100-holdout-reveal.v1",
               "protocol_sha256": sha(CONFIG),
               "split_manifest_sha256": split_manifest["split_sha256"],
               "prediction_manifest_sha256": sha(prediction),
               "labels_were_unavailable_to_fit": True,
               "revealed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (root / "holdout_reveal_receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def adversarial_audit_and_freeze(root):
    prediction = root / "derived/prediction_manifest.json"
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


def load_or_create_state(root, phase, max_wall, resume):
    if (root / "pre_reveal_proof.json").exists():
        verify_before_reveal(root)
    if (root / "split_manifest.json").exists():
        sealed_identity(root)
    state_path = root / "run_state.json"
    deadline_path = root / "deadline.json"
    if state_path.exists():
        if not resume:
            raise RuntimeError("run_state exists; use --resume for an immutable recovery")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            deadline_record = json.loads(deadline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("resume state/deadline is unreadable") from exc
        if state.get("schema_version") != "a100-run-state.v1" or deadline_record.get("schema_version") != "a100-deadline.v1":
            raise RuntimeError("resume state/deadline schema mismatch")
        if state.get("protocol_sha256") != sha(CONFIG):
            raise RuntimeError("resume protocol hash mismatch")
        if state.get("status") == "completed":
            raise RuntimeError("run is already completed; refusing to resume")
        try:
            started = int(state["started_epoch"])
            deadline = int(state["deadline_epoch"])
            deadline_started = int(deadline_record["started_epoch"])
            deadline_value = int(deadline_record["deadline_epoch"])
            recorded_max = int(deadline_record["max_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("resume state/deadline is incomplete") from exc
        if (deadline_started, deadline_value, recorded_max) != (started, deadline, max_wall):
            raise RuntimeError("resume deadline does not match the original run")
        if not isinstance(state.get("completed"), list) or not isinstance(state.get("unavailable"), list):
            raise RuntimeError("resume row checkpoints are malformed")
        if time.time() >= deadline:
            raise RuntimeError("hard A100 wall-clock deadline reached before resume")
        state["status"] = "running"
        state["phase"] = phase
        state["resumed_epoch"] = int(time.time())
        write_json_atomic(state_path, state)
        return state, deadline, state_path
    if resume:
        raise RuntimeError("--resume requires an existing run_state.json")
    started = int(time.time())
    deadline = started + max_wall
    state = {"schema_version": "a100-run-state.v1", "status": "running",
             "phase": phase, "protocol": str(CONFIG), "protocol_sha256": sha(CONFIG),
             "started_epoch": started,
             "deadline_epoch": deadline, "completed": [], "unavailable": []}
    write_json_atomic(deadline_path, {"schema_version": "a100-deadline.v1", "started_epoch": started,
                                      "deadline_epoch": deadline, "max_seconds": max_wall,
                                      "set_before_first_calibration_request": True})
    write_json_atomic(state_path, state)
    return state, deadline, state_path


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
    state, deadline, state_path = load_or_create_state(root, args.phase, max_wall, args.resume)
    try:
        run_analysis("seal", root)
        if args.phase in ("all", "calibration") and not (root / "pre_reveal_proof.json").exists():
            run_rows("calibration", root, values, deadline, args.resume, state, state_path)
            audit_calibration(root)
            run_analysis("fit", root)
            prove_before_reveal(root)
        if args.phase in ("all", "holdout"):
            if not (root / "pre_reveal_proof.json").is_file():
                raise RuntimeError("holdout blocked until pre-reveal proof exists")
            verify_before_reveal(root)
            run_rows("sealed_holdout", root, values, deadline, args.resume, state, state_path)
            verify_before_reveal(root)
            reveal_holdout(root)
            run_analysis("score", root)
            adversarial_audit_and_freeze(root)
        print("A100 validation workflow complete: {}".format(root))
        state["status"] = "completed"
        state["finished_epoch"] = int(time.time())
        write_json_atomic(state_path, state)
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
        write_json_atomic(state_path, state)
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
