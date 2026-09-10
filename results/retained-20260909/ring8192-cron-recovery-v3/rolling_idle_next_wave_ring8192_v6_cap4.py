#!/usr/bin/env python3
"""Stage one bounded idle-to-next-wave continuation for root review.

The command joins the reviewed retention helper, direct storage admission and
one-image-at-a-time immutable prefetch, a fresh read-only health refresh, and
the reviewed rolling-wave launcher.  Missing tags use supplied immutable
receipts or the pinned read-only registry resolver, and every pull is followed
by a new admission report.  It has no retry path, never edits the queue or
frozen inputs, and stops before launch when any gate is unresolved.
Without ``--execute-next-wave`` it performs only the read-only preflight and
prints the launch gate.  The reviewed launcher owns its batch lock; this
driver releases the shared lock before invoking it to avoid lock nesting.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
PROD = HERE.parent / "final-production-v1"
RETENTION_STAGE = HERE / "rolling_idle_maintenance_v1.py"
CONTROLLER = HERE / "production_storage_controller_v2.py"
LAUNCHER = PROD / "launch_rolling_wave_v3_cap4.py"
HEALTH = PROD / "refresh_health_v1.py"
QUOTA_REFRESH = PROD / "refresh_quota_root_v1.py"
RESOLVER = HERE / "resolve_registry_digest_v1.py"
WORKER_POOL = PROD / "ring8192-runtime-v1/worker-pool-manifest.json"
BATCH_LOCK = HERE / "production-rolling-batch.lock"
CONTROLLER_SHA256 = "08bd4c2cfe75b0c49bb987cd3993c5438c710f7c9f6cf12fa3c6ddc2b1bdf208"
LAUNCHER_SHA256 = "24cf4a3fecf5b6b63418af4e2e931a5d100540062eacb3bfe62124e545eaac1c"
HEALTH_SHA256 = "46e9849006bf89a553a815deb6562fd5d2c688d13865794791fb87a9c7a393f5"
QUOTA_REFRESH_SHA256 = "36bb1aa20f54a03efc3a8754f9fbc7ce642d8880ce24a047071bdbcb2b47b4c8"
RESOLVER_SHA256 = "e7e288dab32357597d37aa6192688577a7e0391f575c2d028f192aba19e7d78e"
RETENTION_STAGE_SHA256 = "1c7b3f641f983ad369e6027680c69d10fe74fadb9b9f0b20f295107e5ffd7b1c"
EXECUTOR_MONITOR_SHA256 = "848398e4592b95c776847aaa5d1644c21e9cac61863e8583d71a7de088321c32"
CANDIDATE_SOURCE = HERE.parent / "production-source-candidate-v9"
MAX_PREFETCH_STEPS = 20


class Stop(RuntimeError):
    pass


@contextlib.contextmanager
def batch_lock():
    with BATCH_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("shared production rolling-batch lock is busy") from exc
        yield


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Stop(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise Stop(f"expected JSON object: {path}")
    return value


def run_checked(
    command: list[str], *, cwd: Path | None = None, env: Mapping[str, str] | None = None,
) -> None:
    completed = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, cwd=str(cwd) if cwd is not None else None, env=env,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise Stop(f"command failed: {command[0]}: {detail}")


def auth_values(path: Path, expected_sha256: str) -> tuple[dict[str, Any], Path, str, Path]:
    if sha(path) != expected_sha256:
        raise Stop("root authorization SHA mismatch")
    auth = json_object(path)
    if auth.get("status") != "PASS" or not isinstance(auth.get("plan"), dict) or not isinstance(auth.get("queue"), dict):
        raise Stop("root authorization is not a usable PASS snapshot")
    plan = auth["plan"]
    queue = auth["queue"]
    plan_path = Path(plan.get("path", ""))
    plan_sha = plan.get("sha256")
    if not isinstance(plan_sha, str) or sha(plan_path) != plan_sha or queue.get("plan_sha256") != plan_sha:
        raise Stop("authorization/plan SHA binding mismatch")
    queue_path = Path(queue.get("path", str(Path(queue.get("dir", "")) / "queue.sqlite3")))
    if not queue_path.is_file():
        raise Stop("authorized queue database missing")
    return auth, plan_path, plan_sha, queue_path


def launch_context(auth: Mapping[str, Any]) -> tuple[Path, Path]:
    supervisor = auth.get("supervisor")
    if not isinstance(supervisor, Mapping):
        raise Stop("authorization supervisor binding missing")
    cwd_value = supervisor.get("cwd")
    python_value = supervisor.get("python")
    if not isinstance(cwd_value, str) or not isinstance(python_value, str):
        raise Stop("authorization clean cwd/venv binding missing")
    cwd = Path(cwd_value)
    python = Path(python_value)
    if not cwd.is_dir() or not python.is_file():
        raise Stop("authorization clean cwd/venv path missing")
    clean = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=all"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if clean.returncode != 0 or clean.stdout.strip():
        raise Stop("authorized launch cwd is not a clean repository")
    for name in ("queue_script", "runner"):
        binding = supervisor.get(name)
        if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str) or not isinstance(binding.get("sha256"), str):
            raise Stop(f"authorization {name} binding missing")
        bound = Path(binding["path"])
        reviewed_history_queue = (
            name == "queue_script"
            and bound == PROD / "queue-revision-v1/shared_case_queue_history_v3.py"
            and binding["sha256"] == "d7b28f71bb1c5a1b584f8ff1af3333c8831cc52f390d894ed8e72c533d1c059e"
        )
        source_pinned = bound.resolve().is_relative_to(cwd.resolve()) or reviewed_history_queue
        if not bound.is_file() or not source_pinned or sha(bound) != binding["sha256"]:
            raise Stop(f"authorization {name} binding is not clean/source-pinned or reviewed-history-pinned")
    return cwd, python


def build_launch_env(cwd: Path, python: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Preserve the supervisor environment while selecting its clean venv/source.

    The launcher still requires the reviewed candidate source on PYTHONPATH, but
    replacing the environment with a candidate-only path can select host Python
    or stale runtime policy.  Keep inherited paths and prepend the authorized
    venv/cwd deterministically.
    """
    env = dict(os.environ if base is None else base)
    venv_bin = str(python.parent)
    env["PATH"] = os.pathsep.join([venv_bin, env.get("PATH", "")]).rstrip(os.pathsep)
    env["VIRTUAL_ENV"] = str(python.parent.parent)
    python_paths: list[str] = []
    clean_src = cwd.resolve() / "src"
    if clean_src.is_dir():
        python_paths.append(str(clean_src))
    python_paths.extend([str(cwd.resolve()), str(CANDIDATE_SOURCE)])
    inherited = env.get("PYTHONPATH")
    if inherited:
        python_paths.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(python_paths)
    return env


def queue_state(queue_path: Path) -> dict[str, Any]:
    import sqlite3

    db = sqlite3.connect("file:" + str(queue_path) + "?mode=ro", uri=True)
    try:
        active = db.execute("select count(*) from attempts where status in ('active','orphaned')").fetchone()[0]
        blocked = db.execute("select count(*) from cases where status='blocked'").fetchone()[0]
        counts = {
            str(status): int(count)
            for status, count in db.execute("select status,count(*) from cases group by status")
        }
        row = db.execute("select value_json from meta where key='dispatch_halted'").fetchone()
    finally:
        db.close()
    halted = False
    if row is not None:
        raw = row[0]
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            decoded = raw
        halted = decoded is True or (isinstance(decoded, str) and decoded.lower() == "true")
    return {
        "active_or_orphaned": int(active),
        "blocked": int(blocked),
        "dispatch_halted": halted,
        "case_status_counts": counts,
    }


def queue_idle(queue_path: Path) -> dict[str, Any]:
    state = queue_state(queue_path)
    active = state["active_or_orphaned"]
    blocked = state["blocked"]
    halted = state["dispatch_halted"]
    if active:
        raise Stop(f"queue has {active} active/orphaned attempt(s)")
    if blocked:
        raise Stop(f"queue has {blocked} blocked case(s)")
    if halted:
        raise Stop("queue dispatch is halted")
    counts = state["case_status_counts"]
    if sum(counts.values()) != 1088:
        raise Stop("queue case status partition is not exactly 1088")
    unexpected = sorted(set(counts) - {"pending", "accepted"})
    if unexpected:
        raise Stop("queue contains unexpected case status(es): " + ",".join(unexpected))
    return state


def storage_command(
    *, python: Path, plan: Path, plan_sha256: str, queue: Path, binding: Path, binding_sha256: str,
    pace: Path, pace_sha256: str, ledger: Path, report: Path,
    prefetch: bool, digest_catalog: Path | None, digest_catalog_sha256: str | None,
    resolution: Path | None, resolution_sha256: str | None,
) -> list[str]:
    command = [
        str(python), str(CONTROLLER),
        "--plan-jsonl", str(plan), "--plan-sha256", plan_sha256,
        "--queue", str(queue), "--queue-binding", str(binding),
        "--queue-binding-sha256", binding_sha256,
        "--retention-ledger", str(ledger),
        "--pace-receipt", str(pace), "--pace-receipt-sha256", pace_sha256,
        "--report", str(report),
    ]
    if prefetch:
        command.extend(["--prefetch-one", "--execute-prefetch"])
        if digest_catalog is not None:
            command.extend(["--digest-catalog", str(digest_catalog), "--digest-catalog-sha256", str(digest_catalog_sha256)])
        if resolution is not None:
            command.extend(["--resolution-receipt", str(resolution), "--resolution-receipt-sha256", str(resolution_sha256)])
    return command


def refresh_pace_receipt(path: Path, command: list[str] | None) -> str:
    if command is not None:
        expanded = [item.replace("{pace_receipt}", str(path)) for item in command]
        run_checked(expanded)
    if not path.is_file():
        raise Stop(f"fresh quota receipt missing: {path}")
    value = json_object(path)
    captured = value.get("captured_epoch")
    if value.get("status") != "PASS" or not isinstance(captured, (int, float)) or isinstance(captured, bool):
        raise Stop("fresh quota receipt is not PASS/timestamped")
    age = time.time() - float(captured)
    if age < 0 or age > 300:
        raise Stop(f"fresh quota receipt stale (age {age:.3f}s)")
    return sha(path)


def validate_pace_refresh_command(command: list[str], python: Path) -> None:
    expected = [str(python), str(QUOTA_REFRESH), "{pace_receipt}"]
    if command != expected:
        raise Stop("pace refresh command must use the pinned root quota refresher")
    if sha(QUOTA_REFRESH) != QUOTA_REFRESH_SHA256:
        raise Stop("root quota refresher SHA changed")


def resolution_receipt_path(tag: str, resolution_dir: Path) -> Path:
    token = hashlib.sha256(tag.encode("utf-8")).hexdigest()
    return resolution_dir / f"{token}.json"


def validate_resolution_receipt(path: Path, tag: str) -> None:
    value = json_object(path)
    if (
        value.get("schema") != "assignment.registry-digest-resolution.v1"
        or value.get("status") != "PASS"
        or value.get("tag") != tag
        or value.get("read_only") is not True
    ):
        raise Stop(f"resolution receipt does not bind missing tag: {tag}")


def resolve_missing_tag(
    *, tag: str, supplied: Mapping[str, Path], resolution_dir: Path | None,
    python: Path, cwd: Path,
) -> Path:
    receipt = supplied.get(tag)
    if receipt is None:
        if resolution_dir is None:
            raise Stop(f"missing image has no resolution directory: {tag}")
        if sha(RESOLVER) != RESOLVER_SHA256:
            raise Stop("registry resolver SHA changed")
        resolution_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt = resolution_receipt_path(tag, resolution_dir)
        if not receipt.exists():
            run_checked(
                [str(python), str(RESOLVER), "--tag", tag, "--out", str(receipt)],
                cwd=cwd, env=build_launch_env(cwd, python),
            )
    if not receipt.is_file():
        raise Stop(f"resolution receipt missing for tag: {tag}")
    validate_resolution_receipt(receipt, tag)
    return receipt


def digest_catalog_contains(path: Path, expected_sha256: str | None, tag: str) -> bool:
    if expected_sha256 is None or sha(path) != expected_sha256:
        raise Stop("digest catalog SHA mismatch")
    value = json_object(path)
    records = value.get("records")
    if not isinstance(records, list):
        raise Stop("digest catalog records missing")
    return any(isinstance(item, Mapping) and item.get("tag") == tag for item in records)


def storage_until_cached(
    *, python: Path, cwd: Path, report_dir: Path, plan: Path, plan_sha256: str, queue: Path, binding: Path,
    binding_sha256: str, pace: Path, ledger: Path, pace_refresh_command: list[str] | None,
    digest_catalog: Path | None, digest_catalog_sha256: str | None,
    resolution_receipts: Mapping[str, Path], resolution_dir: Path | None,
    allow_prefetch: bool = True,
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    """Admit, pull at most one missing tag, then re-admit before next tag.

    The controller's prefetch report describes the storage state before its
    pull.  Treating that list as post-pull state repeats the same tag forever;
    each successful pull therefore has a distinct report path and is followed
    by a fresh admission report.
    """
    reports: list[dict[str, Any]] = []
    for step in range(MAX_PREFETCH_STEPS + 1):
        report_path = report_dir / f"storage-admission-v2-{step:02d}.json"
        if report_path.exists():
            raise Stop(f"storage report path already exists: {report_path}")
        pace_sha256 = refresh_pace_receipt(pace, pace_refresh_command)
        command = storage_command(
            python=python, plan=plan, plan_sha256=plan_sha256, queue=queue, binding=binding,
            binding_sha256=binding_sha256, pace=pace, pace_sha256=pace_sha256,
            ledger=ledger, report=report_path, prefetch=False,
            digest_catalog=digest_catalog, digest_catalog_sha256=digest_catalog_sha256,
            resolution=None, resolution_sha256=None,
        )
        run_checked(command, cwd=cwd, env=build_launch_env(cwd, python))
        report = json_object(report_path)
        if report.get("status") != "PASS":
            raise Stop("storage admission is not PASS")
        missing = report.get("missing_unique_images")
        if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
            raise Stop("storage admission missing-image list is malformed")
        reports.append(report)
        if not missing:
            return report_path, report, reports
        if not allow_prefetch:
            raise Stop("final storage admission still has missing immutable images")
        if step == MAX_PREFETCH_STEPS:
            raise Stop("immutable prefetch did not clear all missing images within bounded steps")
        tag = missing[0]
        resolution: Path | None = None
        resolution_sha256: str | None = None
        if digest_catalog is None or not digest_catalog_contains(digest_catalog, digest_catalog_sha256, tag):
            resolution = resolve_missing_tag(
                tag=tag, supplied=resolution_receipts, resolution_dir=resolution_dir,
                python=python, cwd=cwd,
            )
            resolution_sha256 = sha(resolution)
        prefetch_path = report_dir / f"storage-prefetch-v2-{step:02d}.json"
        if prefetch_path.exists():
            raise Stop(f"storage prefetch report path already exists: {prefetch_path}")
        prefetch_command = storage_command(
            python=python, plan=plan, plan_sha256=plan_sha256, queue=queue, binding=binding,
            binding_sha256=binding_sha256, pace=pace, pace_sha256=refresh_pace_receipt(pace, pace_refresh_command),
            ledger=ledger, report=prefetch_path, prefetch=True,
            digest_catalog=digest_catalog, digest_catalog_sha256=digest_catalog_sha256,
            resolution=resolution, resolution_sha256=resolution_sha256,
        )
        run_checked(prefetch_command, cwd=cwd, env=build_launch_env(cwd, python))
        prefetch_report = json_object(prefetch_path)
        if prefetch_report.get("status") != "PASS":
            raise Stop("immutable prefetch admission is not PASS")
        prefetch_result = prefetch_report.get("prefetch")
        if (
            not isinstance(prefetch_result, Mapping)
            or prefetch_result.get("status") != "prefetched_one"
            or prefetch_result.get("tag") != tag
        ):
            raise Stop(f"prefetch did not complete the selected missing tag: {tag}")
        reports.append(prefetch_report)
    raise Stop("unreachable storage admission loop")


def validate_health_receipt(value: Mapping[str, Any], plan_sha256: str) -> None:
    """Accept a complete observation even when one endpoint is unhealthy.

    The reviewed launcher performs the worker-level isolation and selects a
    healthy prefix.  Requiring ``launch_ready`` here would discard that safe
    degraded-subset policy before the launcher can apply it.
    """
    if value.get("status") != "PASS" or value.get("plan_sha256") != plan_sha256:
        raise Stop("fresh endpoint health receipt is not PASS/plan-bound")
    if value.get("endpoint_count") != 20 or value.get("collection_valid") is not True:
        raise Stop("fresh endpoint health collection is incomplete")
    workers = value.get("workers")
    if (
        not isinstance(workers, list)
        or len(workers) != 20
        or any(not isinstance(item, Mapping) or not isinstance(item.get("worker_id"), str) for item in workers)
        or len({item["worker_id"] for item in workers}) != 20
    ):
        raise Stop("fresh endpoint health worker observations are malformed")


def refresh_health(
    *, python: Path, cwd: Path, output_dir: Path, plan: Path, receipt_name: str,
) -> Path:
    if sha(HEALTH) != HEALTH_SHA256:
        raise Stop("health refresh SHA changed")
    env = build_launch_env(cwd, python)
    command = [
        str(python), str(HEALTH), "--worker-pool", str(WORKER_POOL),
        "--plan", str(plan), "--output-dir", str(output_dir), "--receipt-name", receipt_name,
    ]
    run_checked(command, cwd=cwd, env=env)
    receipt = output_dir / receipt_name
    value = json_object(receipt)
    validate_health_receipt(value, sha(plan))
    return receipt


def launcher_command(
    *, python: Path, authorization: Path, authorization_sha256: str, storage_report: Path,
    storage_report_sha256: str, state_dir: Path, health_receipt: Path,
    health_receipt_sha256: str, execute: bool,
) -> list[str]:
    command = [
        str(python), str(LAUNCHER),
        "--authorization", str(authorization),
        "--monitor-sha256", EXECUTOR_MONITOR_SHA256,
        "--storage-report", str(storage_report),
        "--storage-report-sha256", storage_report_sha256,
        "--state-dir", str(state_dir),
        "--health-receipt", str(health_receipt),
        "--health-receipt-sha256", health_receipt_sha256,
    ]
    if execute:
        command.append("--execute")
    return command


def parse_resolution_receipts(values: Sequence[str]) -> dict[str, Path]:
    """Parse repeatable ``TAG=PATH`` bindings for one-image prefetch steps."""
    result: dict[str, Path] = {}
    for value in values:
        tag, separator, raw_path = value.partition("=")
        if not separator or not tag or not raw_path:
            raise Stop("resolution receipts must use TAG=PATH")
        if tag in result:
            raise Stop(f"duplicate resolution receipt binding: {tag}")
        result[tag] = Path(raw_path)
    return result


def load_resolution_index(path: Path) -> dict[str, Path]:
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise Stop("resolution index must be a list")
    result: dict[str, Path] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise Stop("resolution index row is malformed")
        tag, raw_path, expected = item.get("tag"), item.get("path"), item.get("sha256")
        if (
            not isinstance(tag, str) or not tag
            or not isinstance(raw_path, str) or not isinstance(expected, str)
            or tag in result
        ):
            raise Stop("resolution index identity is malformed")
        receipt = Path(raw_path)
        if not receipt.is_file() or sha(receipt) != expected:
            raise Stop(f"resolution index receipt SHA mismatch: {tag}")
        validate_resolution_receipt(receipt, tag)
        result[tag] = receipt
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    if sha(CONTROLLER) != CONTROLLER_SHA256:
        raise Stop("storage controller SHA changed")
    if sha(LAUNCHER) != LAUNCHER_SHA256:
        raise Stop("rolling launcher SHA changed")
    if sha(RETENTION_STAGE) != RETENTION_STAGE_SHA256:
        raise Stop("retention stage SHA changed")
    auth, plan, plan_sha, queue = auth_values(args.authorization, args.authorization_sha256)
    initial_queue = queue_idle(queue)
    initial_counts = initial_queue["case_status_counts"]
    if initial_counts.get("accepted", 0) == 1088 and initial_counts.get("pending", 0) == 0:
        return {
            "schema": "assignment.rolling-idle-next-wave.v2",
            "status": "FINISHED",
            "plan_sha256": plan_sha,
            "queue": initial_queue,
        }
    launch_cwd, launch_python = launch_context(auth)
    binding_sha256 = sha(args.queue_binding)
    pace_refresh_command = getattr(args, "pace_refresh_command", None)
    if args.execute_next_wave and pace_refresh_command is None:
        pace_refresh_command = [str(launch_python), str(QUOTA_REFRESH), "{pace_receipt}"]
    if args.execute_next_wave:
        if pace_refresh_command is None:
            raise Stop("pinned quota refresh command is unavailable")
        validate_pace_refresh_command(pace_refresh_command, launch_python)
    resolution_receipts = dict(getattr(args, "resolution_receipts", {}))
    resolution_index = getattr(args, "resolution_index", None)
    if resolution_index is not None:
        indexed = load_resolution_index(resolution_index)
        for tag, receipt in indexed.items():
            if tag in resolution_receipts and resolution_receipts[tag] != receipt:
                raise Stop(f"duplicate resolution receipt binding: {tag}")
            resolution_receipts[tag] = receipt
    resolution_dir = getattr(args, "resolution_dir", None)
    if resolution_dir is None:
        resolution_dir = args.report_dir / "registry-resolutions"
    with batch_lock():
        queue_idle(queue)
        args.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        maintenance = load(RETENTION_STAGE, "rolling_idle_next_wave_retention")
        retention = maintenance.run(
            authorization=args.authorization,
            authorization_sha256=args.authorization_sha256,
            ledger=args.retention_ledger,
            archive_root=args.archive_root,
            remote_root=args.remote_root,
            execute=args.execute_next_wave,
        )
        if not args.execute_next_wave:
            return {"schema": "assignment.rolling-idle-next-wave.v2", "status": "READY", "retention": retention}
        queue_idle(queue)
        storage_report, report, reports = storage_until_cached(
            python=launch_python, cwd=launch_cwd, report_dir=args.report_dir, plan=plan,
            plan_sha256=plan_sha, queue=queue,
            binding=args.queue_binding, binding_sha256=binding_sha256,
            pace=args.pace_receipt, ledger=args.retention_ledger,
            pace_refresh_command=pace_refresh_command,
            digest_catalog=args.digest_catalog, digest_catalog_sha256=args.digest_catalog_sha256,
            resolution_receipts=resolution_receipts, resolution_dir=resolution_dir,
        )
    health_receipt = refresh_health(
        python=launch_python, cwd=launch_cwd, output_dir=args.health_output_dir,
        plan=plan, receipt_name=args.health_receipt_name,
    )
    # Health probing is intentionally outside the storage lock.  Re-enter it
    # for a fresh quota/storage admission so the launcher never consumes a
    # report that became stale while health was being collected.
    final_report_dir = args.report_dir / "after-health"
    final_report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with batch_lock():
        queue_idle(queue)
        storage_report, report, final_reports = storage_until_cached(
            python=launch_python, cwd=launch_cwd, report_dir=final_report_dir, plan=plan,
            plan_sha256=plan_sha,
            queue=queue, binding=args.queue_binding, binding_sha256=binding_sha256,
            pace=args.pace_receipt, ledger=args.retention_ledger,
            pace_refresh_command=pace_refresh_command,
            digest_catalog=args.digest_catalog, digest_catalog_sha256=args.digest_catalog_sha256,
            resolution_receipts=resolution_receipts, resolution_dir=resolution_dir,
            allow_prefetch=False,
        )
    state_dir = args.launcher_state_dir
    if state_dir.exists():
        raise Stop(f"launcher state directory already exists: {state_dir}")
    launch_env = build_launch_env(launch_cwd, launch_python)
    command = launcher_command(
        python=launch_python, authorization=args.authorization,
        authorization_sha256=args.authorization_sha256,
        storage_report=storage_report,
        storage_report_sha256=sha(storage_report),
        state_dir=state_dir,
        health_receipt=health_receipt,
        health_receipt_sha256=sha(health_receipt),
        execute=True,
    )
    # The reviewed launcher owns production-rolling-batch.lock. The context
    # above has exited; invoking it while holding the same lock would deadlock.
    completed = subprocess.run(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, cwd=str(launch_cwd), env=launch_env,
    )
    if completed.returncode:
        raise Stop(f"reviewed launcher refused wave: {completed.stderr.strip() or completed.stdout.strip()}")
    return {
        "schema": "assignment.rolling-idle-next-wave.v2",
        "status": "LAUNCHED",
        "authorization": {"path": str(args.authorization), "sha256": args.authorization_sha256},
        "plan_sha256": plan_sha,
        "retention": retention,
        "storage_report": {"path": str(storage_report), "sha256": sha(storage_report), "selected_wave_count": report.get("selected_wave_count")},
        "storage_reports": [
            *[str(args.report_dir / f"storage-admission-v2-{i:02d}.json") for i in range(len(reports))],
            *[str(final_report_dir / f"storage-admission-v2-{i:02d}.json") for i in range(len(final_reports))],
        ],
        "health_receipt": {"path": str(health_receipt), "sha256": sha(health_receipt)},
        "launcher_stdout": completed.stdout.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--retention-ledger", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--queue-binding", type=Path, required=True)
    parser.add_argument("--pace-receipt", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--health-output-dir", type=Path, required=True)
    parser.add_argument("--health-receipt-name", default="next-wave-health-v2.json")
    parser.add_argument("--digest-catalog", type=Path)
    parser.add_argument("--digest-catalog-sha256")
    parser.add_argument(
        "--resolution-receipt", action="append", default=[], metavar="TAG=PATH",
        help="repeatable immutable registry resolution receipt binding",
    )
    parser.add_argument("--resolution-index", type=Path)
    parser.add_argument("--resolution-dir", type=Path)
    parser.add_argument(
        "--pace-refresh-command", nargs="+",
        help="command that refreshes --pace-receipt; use {pace_receipt} for its path",
    )
    parser.add_argument("--launcher-state-dir", type=Path, required=True)
    parser.add_argument("--execute-next-wave", action="store_true")
    args = parser.parse_args()
    if bool(args.digest_catalog) != bool(args.digest_catalog_sha256):
        raise SystemExit("digest catalog and SHA must be supplied together")
    args.resolution_receipts = parse_resolution_receipts(args.resolution_receipt)
    result = run(args)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, Stop, subprocess.SubprocessError) as error:
        print("rolling_idle_next_wave: NOT_READY: " + str(error), file=sys.stderr)
        raise SystemExit(2)
