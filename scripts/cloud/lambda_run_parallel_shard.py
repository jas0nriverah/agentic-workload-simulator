#!/usr/bin/env python3
"""Run one disjoint control shard on one GPU-isolated Lightning Studio.

The script is intentionally separate from ``lambda_run_first_experiment.sh``.
The latter is frozen as the single-instance control fixture.  This worker
runner consumes a previously verified ``batch_manifest.json``, rewrites only
dataset/output/evaluator paths, and keeps each worker's logs and predictions
under an immutable attempt directory.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.parallel import (  # noqa: E402
    ParallelBatchError,
    build_worker_agent_command,
    build_worker_evaluator_command,
    command_hash,
    expand_runtime_environment,
    file_sha256,
    load_instance_rows,
    row_hash,
    validate_batch_manifest,
    write_json,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def manifest_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name == key:
            return value
    return ""


def run_limited(argv: list[str], *, log_path: Path, timeout_seconds: int, cwd: Path, environment: dict[str, str]) -> tuple[int, bool]:
    """Run an argv list without a shell and terminate its process group on timeout."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds), False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return 124, True


def image_name(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True, type=Path, help="Rendered instance manifest; never sourced")
    result.add_argument("--batch-manifest", required=True, type=Path)
    result.add_argument("--worker-index", required=True, type=int)
    result.add_argument("--dataset-path", type=Path, help="Override the source dataset path recorded in the batch manifest")
    result.add_argument("--batch-root", type=Path, help="Override the directory containing worker-NN/ directories")
    result.add_argument("--work-root", type=Path, help="Used only for the default batch root")
    result.add_argument("--timeout-seconds", type=int, default=7200)
    result.add_argument("--evaluator-timeout-seconds", type=int, default=1800)
    result.add_argument("--resume", action="store_true", help="Skip this worker only when its attempt completed successfully")
    result.add_argument("--force-retry", action="store_true", help="Create the next retry attempt after a failed attempt")
    result.add_argument("--dry-run", action="store_true", help="Print exact commands without writing files or starting processes")
    return result


def load_batch(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParallelBatchError(f"batch manifest is not readable JSON: {path}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "parallel-batch.v1":
        raise ParallelBatchError("batch manifest schema_version must be parallel-batch.v1")
    validate_batch_manifest(value)
    return value


def select_worker(batch: dict[str, Any], index: int) -> dict[str, Any]:
    workers = batch.get("workers")
    if not isinstance(workers, list) or not workers:
        raise ParallelBatchError("batch manifest has no workers")
    if index < 0 or index >= len(workers):
        raise ParallelBatchError(f"worker-index must be within [0, {len(workers)})")
    worker = workers[index]
    if not isinstance(worker, dict) or worker.get("shard_index") != index:
        raise ParallelBatchError("worker entries are not ordered by shard_index")
    return worker


def verify_rows(source_path: Path, worker: dict[str, Any], expected_source_sha: str) -> list[dict[str, Any]]:
    actual_source_sha = file_sha256(source_path)
    if actual_source_sha != expected_source_sha:
        raise ParallelBatchError(
            f"source dataset hash mismatch: expected {expected_source_sha}, observed {actual_source_sha}"
        )
    rows = load_instance_rows(source_path)
    by_id = {str(row["instance_id"]): row for row in rows}
    ids = worker.get("instance_ids")
    hashes = worker.get("row_sha256")
    if not isinstance(ids, list) or not isinstance(hashes, list) or len(ids) != len(hashes):
        raise ParallelBatchError("worker manifest has invalid instance_ids/row_sha256")
    selected: list[dict[str, Any]] = []
    for instance_id, expected_hash in zip(ids, hashes):
        row = by_id.get(str(instance_id))
        if row is None:
            raise ParallelBatchError(f"worker instance is absent from source dataset: {instance_id}")
        observed_hash = row_hash(row)
        if observed_hash != expected_hash:
            raise ParallelBatchError(f"row hash mismatch for {instance_id}: expected {expected_hash}, observed {observed_hash}")
        selected.append(dict(row))
    return selected


def choose_attempt(worker_root: Path, *, force_retry: bool) -> Path:
    first = worker_root / "attempt-001"
    if not first.exists():
        return first
    if not force_retry:
        return first
    index = 2
    while (worker_root / f"attempt-{index:03d}").exists():
        index += 1
    return worker_root / f"attempt-{index:03d}"


def _status_is_complete(path: Path) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(value, dict) and value.get("status") == "completed" and value.get("agent_returncode") == 0 and value.get("evaluator_returncode") == 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.timeout_seconds <= 0 or args.evaluator_timeout_seconds <= 0:
            raise ParallelBatchError("timeouts must be positive")
        batch = load_batch(args.batch_manifest)
        worker = select_worker(batch, args.worker_index)
        source_value = str(batch.get("source_dataset", ""))
        source_path = args.dataset_path or (Path(source_value) if source_value else None)
        if source_path is None:
            raise ParallelBatchError("source dataset path is missing; pass --dataset-path")
        selected_rows = verify_rows(source_path, worker, str(batch.get("source_dataset_sha256", "")))
        batch_root = args.batch_root or args.batch_manifest.parent
        worker_root = batch_root / str(worker["worker"])
        attempt_root = choose_attempt(worker_root, force_retry=args.force_retry)
        status_path = attempt_root / "worker_status.json"
        if status_path.is_file() and _status_is_complete(status_path):
            if args.resume:
                print(f"parallel worker: SKIP (completed) {status_path}")
                return 0
            raise ParallelBatchError(f"successful attempt exists; pass --resume or use a new batch: {status_path}")
        if status_path.exists() and not args.force_retry:
            raise ParallelBatchError(f"attempt exists but is not complete; pass --force-retry: {attempt_root}")
        agent_command = manifest_value(args.manifest, "SWE_AGENT_COMMAND")
        evaluator_command = manifest_value(args.manifest, "EVALUATE_COMMAND")
        if not agent_command or not evaluator_command:
            raise ParallelBatchError("manifest must define SWE_AGENT_COMMAND and EVALUATE_COMMAND")
        runtime_rows = []
        for row in selected_rows:
            runtime = dict(row)
            expected_image = image_name(str(row["instance_id"]))
            if runtime.get("image_name") not in (None, expected_image):
                raise ParallelBatchError(f"dataset image_name conflicts for {row['instance_id']}")
            runtime["image_name"] = expected_image
            runtime_rows.append(runtime)
        runtime_path = attempt_root / "sweagent_instances.json"
        source_snapshot_path = attempt_root / "evaluator_instances.json"
        agent_output = attempt_root / "sweagent_output"
        report_dir = attempt_root / "evaluator_report"
        predictions_path = agent_output / "preds.json"
        run_id = f"{batch['experiment_id']}-{batch['batch_id']}-{worker['worker']}"
        agent_tokens = build_worker_agent_command(
            agent_command,
            instances_path=runtime_path,
            output_dir=agent_output,
            num_workers=1,
        )
        evaluator_tokens = build_worker_evaluator_command(
            evaluator_command,
            dataset_path=source_snapshot_path,
            predictions_path=predictions_path,
            instance_ids=[str(item) for item in worker["instance_ids"]],
            report_dir=report_dir,
            run_id=run_id,
        )
        if "--instances.filter" in agent_tokens:
            raise ParallelBatchError("parallel agent command still contains an instance filter")
        if "--num_workers" not in agent_tokens or agent_tokens[agent_tokens.index("--num_workers") + 1] != "1":
            raise ParallelBatchError("each parallel worker must use --num_workers 1")
        if args.dry_run:
            print(f"parallel worker: DRY-RUN worker={worker['worker']} instances={len(selected_rows)}")
            print(f"agent_sha256={command_hash(agent_tokens)}")
            print(f"agent={' '.join(shlex.quote(token) for token in agent_tokens)}")
            print(f"evaluator_sha256={command_hash(evaluator_tokens)}")
            print(f"evaluator={' '.join(shlex.quote(token) for token in evaluator_tokens)}")
            print(f"attempt_root={attempt_root}")
            return 0
        if not selected_rows:
            raise ParallelBatchError("empty shards are planning-only; do not start a worker for one")
        attempt_root.mkdir(parents=True, exist_ok=True)
        write_json(runtime_path, runtime_rows)
        write_json(source_snapshot_path, selected_rows)
        environment = dict(os.environ)
        runtime_agent = expand_runtime_environment(agent_tokens, environment)
        runtime_evaluator = expand_runtime_environment(evaluator_tokens, environment)
        root_cwd = Path(manifest_value(args.manifest, "AGENTIC_SOURCE_ROOT") or ROOT)
        if not root_cwd.is_dir():
            root_cwd = ROOT
        started = utc_now()
        agent_rc, agent_timed_out = run_limited(
            runtime_agent,
            log_path=attempt_root / "agent.log",
            timeout_seconds=args.timeout_seconds,
            cwd=root_cwd,
            environment=environment,
        )
        evaluator_rc = None
        evaluator_timed_out = False
        if agent_rc == 0:
            evaluator_rc, evaluator_timed_out = run_limited(
                runtime_evaluator,
                log_path=attempt_root / "evaluator.log",
                timeout_seconds=args.evaluator_timeout_seconds,
                cwd=root_cwd,
                environment=environment,
            )
        else:
            (attempt_root / "evaluator.log").write_text("not started: agent failed\n", encoding="utf-8")
        ended = utc_now()
        if agent_rc == 0 and evaluator_rc == 0:
            status = "completed"
        elif agent_timed_out or evaluator_timed_out:
            status = "timeout"
        elif agent_rc != 0:
            status = "runner_failed"
        else:
            status = "evaluation_failed"
        write_json(
            status_path,
            {
                "schema_version": "parallel-worker.v1",
                "status": status,
                "provenance": "measured",
                "batch_id": batch["batch_id"],
                "experiment_id": batch["experiment_id"],
                "dataset": batch["dataset"],
                "worker": worker["worker"],
                "shard_index": worker["shard_index"],
                "shard_count": worker["shard_count"],
                "instance_ids": worker["instance_ids"],
                "source_dataset_sha256": batch["source_dataset_sha256"],
                "runtime_dataset_sha256": file_sha256(runtime_path),
                "evaluator_dataset_sha256": file_sha256(source_snapshot_path),
                "agent_command_sha256": command_hash(runtime_agent, environment=environment),
                "evaluator_command_sha256": command_hash(runtime_evaluator, environment=environment),
                "agent_returncode": agent_rc,
                "evaluator_returncode": evaluator_rc,
                "started_at_utc": started,
                "ended_at_utc": ended,
                "attempt_root": str(attempt_root),
                "evaluator_runtime_excluded_from_trajectory": True,
            },
        )
        print(f"parallel worker: {status} worker={worker['worker']} instances={len(selected_rows)}")
        return 0 if status == "completed" else (124 if status == "timeout" else 1)
    except ParallelBatchError as exc:
        print(f"parallel worker: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
