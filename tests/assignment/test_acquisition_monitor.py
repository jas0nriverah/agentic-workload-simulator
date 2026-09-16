from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import time
from pathlib import Path
from unittest import mock

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts/assignment/acquisition_monitor.py"
SPEC = importlib.util.spec_from_file_location("staged_acquisition_monitor", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
monitor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: dict) -> str:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return digest(path)


def make_queue(root: Path, *, plan_sha: str, worker_id: str = "worker-00", endpoint_id: str = "endpoint-00", pending: bool = True) -> tuple[Path, str]:
    queue = root / "queue"
    queue.mkdir()
    db = queue / "queue.sqlite3"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        PRAGMA user_version = 2;
        CREATE TABLE meta(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE cases(status TEXT NOT NULL);
        CREATE TABLE workers(worker_id TEXT PRIMARY KEY, endpoint_id TEXT, enabled INTEGER);
        CREATE TABLE attempts(
            attempt_id TEXT PRIMARY KEY, case_id TEXT, worker_id TEXT, endpoint_id TEXT,
            status TEXT, launch_state TEXT, runner_pid INTEGER, runner_start_ticks INTEGER,
            runner_boot_id TEXT, runner_exit_json TEXT
        );
        """
    )
    for key, value in {
        "schema_version": "assignment.shared-case-queue.v2",
        "plan_sha256": plan_sha,
        "allowed_worker_ids": [worker_id],
        "dispatch_halted": False,
        "halt_reason": None,
    }.items():
        connection.execute("INSERT INTO meta VALUES (?, ?)", (key, json.dumps(value, sort_keys=True)))
    connection.execute("INSERT INTO workers VALUES (?, ?, 1)", (worker_id, endpoint_id))
    connection.execute("INSERT INTO cases VALUES (?)", ("pending" if pending else "accepted",))
    connection.commit()
    connection.close()
    return queue, plan_sha


def make_auth(root: Path, *, allow_launch: bool = False, allow_resume: bool = False, pending: bool = True) -> tuple[Path, Path, Path]:
    plan = root / "plan.json"
    plan.write_text("sealed plan bytes\n", encoding="utf-8")
    plan_sha = digest(plan)
    queue, _ = make_queue(root, plan_sha=plan_sha, pending=pending)
    config = root / "frozen-config.json"
    config.write_text("{\"frozen\":true}\n", encoding="utf-8")
    receipt = root / "pass-receipt.json"
    receipt_value = {
        "schema_version": "assignment.production-launch-pass.v1",
        "status": "PASS",
        "authorization_id": "auth-test-001",
        "plan_sha256": plan_sha,
        "frozen_config_sha256": digest(config),
        "launch_authorized": False,
    }
    receipt_sha = write_json(receipt, receipt_value)
    queue_script = root / "shared_case_queue.py"
    queue_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runner = root / "runner.py"
    runner.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)
    auth = root / "authorization.json"
    value = {
        "schema_version": "assignment.production-launch-authorization.v1",
        "status": "PASS",
        "launch_enabled": True,
        "written_by": "root",
        "authorization_id": "auth-test-001",
        "acknowledge_paid_gpu_work": True,
        "queue": {"dir": str(queue), "plan_sha256": plan_sha},
        "plan": {"path": str(plan), "sha256": plan_sha},
        "frozen_config": {"path": str(config), "sha256": digest(config)},
        "pass_receipt": {"path": str(receipt), "sha256": receipt_sha},
        "workers": [{"worker_id": "worker-00", "endpoint_id": "endpoint-00"}],
        "allow_supervisor_launch": allow_launch,
        "allow_resume_after_supervisor_exit": allow_resume,
        "max_launches_per_tick": 1,
        "supervisor": {
            "python": sys.executable,
            "queue_script": {"path": str(queue_script), "sha256": digest(queue_script)},
            "runner": {"path": str(runner), "sha256": digest(runner)},
            "max_retries": 1,
        },
    }
    write_json(auth, value)
    return auth, queue, config


def test_gate_absent_is_paused_and_does_not_read_or_launch(tmp_path: Path):
    code, payload = monitor.run_once(tmp_path / "missing-auth.json", tmp_path / "state", execute=True)
    assert code == 2
    assert payload["status"] == "paused"
    assert payload["reason"] == "authorization_invalid"
    assert not (tmp_path / "state" / "launches").exists()


def test_valid_gate_dry_run_is_ready_without_launch(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path)
    code, payload = monitor.run_once(auth, tmp_path / "state", queue_dir=queue)
    assert code == 0
    assert payload["status"] == "ready"
    assert payload["execute_requested"] is False
    assert not (tmp_path / "state" / "launches").exists()


def test_plan_hash_drift_pauses_before_queue_activity(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_bytes(b"changed")
    code, payload = monitor.run_once(auth, tmp_path / "state", queue_dir=queue, execute=True)
    assert code == 2
    assert payload["reason"] == "authorization_invalid"
    assert not (tmp_path / "state" / "launches").exists()


def test_vanished_supervisor_is_not_called_success(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    monitor._atomic_json(
        state / "monitor_state.json",
        {
            "schema_version": monitor.MONITOR_SCHEMA,
            "supervisors": {
                "worker-00": {
                    "pid": 99999999,
                    "start_ticks": 1,
                    "boot_id": "boot",
                    "argv_sha256": "b" * 64,
                }
            },
        },
    )
    code, payload = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code == 2
    assert payload["status"] == "paused"
    assert payload["reason"] == "integrity_failure"
    assert payload["supervisors"]["worker-00"]["status"] == "missing_exit_receipt"
    assert "success" not in json.dumps(payload["supervisors"]["worker-00"]).lower()


def test_execute_launches_only_registered_predeclared_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    auth, queue, _ = make_auth(tmp_path, allow_launch=True)
    captured: list[tuple[str, list[str]]] = []

    def fake_spawn(auth_value, state_dir, worker_id, argv):
        captured.append((worker_id, list(argv)))
        return {
            "worker_id": worker_id,
            "launch_id": "worker-00-test",
            "pid": 1234,
            "start_ticks": 5,
            "boot_id": "boot",
            "argv_sha256": "c" * 64,
            "exit_receipt": str(state_dir / "launches" / "exit.json"),
            "launch_record": str(state_dir / "launches" / "launch.json"),
        }

    monkeypatch.setattr(monitor, "_spawn_supervisor", fake_spawn)
    code, payload = monitor.run_once(auth, tmp_path / "state", queue_dir=queue, execute=True)
    assert code == 0
    assert payload["status"] == "launched"
    assert [row[0] for row in captured] == ["worker-00"]
    assert "--worker-id" in captured[0][1]
    assert "worker-00" in captured[0][1]
    assert "--acknowledge-paid-gpu-work" in captured[0][1]


def test_disabled_registered_worker_is_skipped_not_integrity_failure(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path, allow_launch=True)
    connection = sqlite3.connect(queue / "queue.sqlite3")
    connection.execute("UPDATE workers SET enabled = 0 WHERE worker_id = 'worker-00'")
    connection.commit()
    connection.close()
    code, payload = monitor.run_once(auth, tmp_path / "state", queue_dir=queue, execute=True)
    assert code == 3
    assert payload["status"] == "running"
    assert payload["reason"] == "no_eligible_worker"
    assert not (tmp_path / "state" / "launches").exists()


def _wait_for(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_process_absent(pid: int, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if monitor.process_identity(pid)["status"] == "absent":
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for supervisor PID {pid} to disappear")


def test_real_launch_receipt_resume_and_third_tick_close_history(tmp_path: Path):
    """The child receipt closes old records without enabling duplicate launches."""

    auth, queue, _ = make_auth(tmp_path, allow_launch=True, allow_resume=True)
    state = tmp_path / "state"

    code1, payload1 = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code1 == 0 and payload1["status"] == "launched"
    first_id = payload1["launched"][0]["launch_id"]
    first_record = state / "launches" / f"{first_id}.json"
    first_receipt = state / "launches" / f"{first_id}.exit.json"
    _wait_for(first_receipt)
    _wait_process_absent(int(payload1["launched"][0]["pid"]))
    _wait_for(first_record)
    first_record_value = json.loads(first_record.read_text(encoding="utf-8"))
    first_receipt_value = json.loads(first_receipt.read_text(encoding="utf-8"))
    assert first_record_value["status"] == "receipt_written"
    assert first_record_value["pid"] == first_receipt_value["guard_pid"]
    assert first_record_value["start_ticks"] == first_receipt_value["guard_identity"]["start_ticks"]
    assert first_record_value["boot_id"] == first_receipt_value["guard_identity"]["boot_id"]

    code2, payload2 = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code2 == 0 and payload2["status"] == "launched"
    second_id = payload2["launched"][0]["launch_id"]
    second_receipt = state / "launches" / f"{second_id}.exit.json"
    _wait_for(second_receipt)
    _wait_process_absent(int(payload2["launched"][0]["pid"]))

    code3, payload3 = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code3 == 0 and payload3["status"] == "launched"
    assert payload3.get("reason") != "integrity_failure"
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state / "launches").glob("worker-00-*.json"))
        if not path.name.endswith(".exit.json")
    ]
    assert len(records) >= 2
    assert json.loads(first_record.read_text(encoding="utf-8"))["status"] == "receipt_written"
    second_record = state / "launches" / f"{second_id}.json"
    _wait_for(second_record)
    assert json.loads(second_record.read_text(encoding="utf-8"))["status"] == "receipt_written"
    assert sum(row["status"] in {"spawn_intent", "spawned"} for row in records) <= 1


def test_nonzero_supervisor_receipt_pauses_without_respawn(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path, allow_launch=True, allow_resume=True)
    auth_value = json.loads(auth.read_text(encoding="utf-8"))
    queue_script = Path(auth_value["supervisor"]["queue_script"]["path"])
    queue_script.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
    auth_value["supervisor"]["queue_script"]["sha256"] = digest(queue_script)
    write_json(auth, auth_value)
    state = tmp_path / "state"

    code1, payload1 = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code1 == 0 and payload1["status"] == "launched"
    receipt = state / "launches" / f"{payload1['launched'][0]['launch_id']}.exit.json"
    _wait_for(receipt)
    _wait_process_absent(int(payload1["launched"][0]["pid"]))

    code2, payload2 = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code2 == 2
    assert payload2["reason"] == "integrity_failure"
    assert "returncode=7" in json.dumps(payload2["detail"])
    assert len(list((state / "launches").glob("*.exit.json"))) == 1


def test_queue_worker_set_drift_pauses_without_launch(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path)
    connection = sqlite3.connect(queue / "queue.sqlite3")
    connection.execute("UPDATE meta SET value_json = ? WHERE key = 'allowed_worker_ids'", (json.dumps(["worker-01"]),))
    connection.commit()
    connection.close()
    code, payload = monitor.run_once(auth, tmp_path / "state", queue_dir=queue, execute=True)
    assert code == 2
    assert payload["reason"] == "integrity_failure"
    assert not (tmp_path / "state" / "launches").exists()


def test_unfinished_launch_intent_blocks_duplicate_after_monitor_crash(tmp_path: Path):
    auth, queue, _ = make_auth(tmp_path, allow_launch=True)
    state = tmp_path / "state"
    launches = state / "launches"
    launches.mkdir(parents=True)
    monitor._atomic_json(
        launches / "worker-00-crashed.json",
        {
            "schema_version": monitor.CHILD_SCHEMA,
            "launch_id": "worker-00-crashed",
            "worker_id": "worker-00",
            "status": "spawn_intent",
            "argv_sha256": "d" * 64,
            "exit_receipt": str(launches / "worker-00-crashed.exit.json"),
        },
    )
    code, payload = monitor.run_once(auth, state, queue_dir=queue, execute=True)
    assert code == 2
    assert payload["reason"] == "integrity_failure"
    assert "launch intent" in json.dumps(payload["detail"])

