#!/usr/bin/env python3
"""Root-authorized idle retention maintenance for the final production queue.

This staged command is intentionally limited to accepted-attempt archival and
retention-ledger maintenance.  It verifies the frozen authorization, plan,
queue-idle boundary, accepted attempt/manifest bindings, and every existing
retention receipt before it writes a merged ledger.  It does not launch work,
prefetch images, reclaim containers, or mutate the queue.

The archive operation reuses the checked retention helper's exact binding
check and its reviewed ``reclaim_pressure_level4.py`` path.  A later idle-wave
driver can consume the resulting ledger and invoke the reviewed rolling-wave
launcher under its own fresh storage and endpoint gates.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
RETENTION_HELPER = HERE / "run_accepted_attempt_retention_v1.py"
RETENTION_HELPER_SHA256 = "85aa6ff7100fa75fb36157f5dec2561fee5a959fe356a36514cd6666a068674f"
RECLAIMER = HERE / "reclaim_pressure_level4.py"
RECLAIMER_SHA256 = "37e3d6bd659d8aa4d214a1ca50a69a29fa32fc0de6660d814c1f09423ad3e6ae"
CONTROLLER = HERE / "production_storage_controller_v2.py"
CONTROLLER_SHA256 = "08bd4c2cfe75b0c49bb987cd3993c5438c710f7c9f6cf12fa3c6ddc2b1bdf208"
BATCH_LOCK = HERE / "production-rolling-batch.lock"
CASE_COUNT = 1088
AUTHORIZATION_SCHEMA = "assignment.production-launch-authorization.v1"
PASS_RECEIPT_SCHEMA = "assignment.production-launch-pass.v1"
REMOTE_NAMESPACE = Path("/storage/ice1/9/6/jriverah3/eic-work")
REMOTE_RETAINED_ROOT = REMOTE_NAMESPACE / "runtime/astra-evidence-archive-20260909/squashfs-retained-20260909"


class Stop(RuntimeError):
    pass


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    """Replace a ledger only after the complete merged value is durable."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise Stop(f"temporary ledger already exists: {temporary}")
    with temporary.open("x") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise Stop(f"cannot load reviewed helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_authorization(path: Path, expected_sha256: str) -> dict[str, Any]:
    if sha(path) != expected_sha256:
        raise Stop("root authorization SHA mismatch")
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != AUTHORIZATION_SCHEMA
        or value.get("status") != "PASS"
        or not isinstance(value.get("authorization_id"), str)
        or not value["authorization_id"]
    ):
        raise Stop("root authorization is not PASS")
    plan = value.get("plan")
    queue = value.get("queue")
    if (
        not isinstance(plan, Mapping)
        or not isinstance(plan.get("path"), str)
        or not isinstance(plan.get("sha256"), str)
        or not isinstance(queue, Mapping)
        or not isinstance(queue.get("dir"), str)
    ):
        raise Stop("root authorization plan/queue binding is malformed")
    if queue.get("plan_sha256") != plan["sha256"]:
        raise Stop("root authorization plan binding differs between plan and queue")
    pass_binding = value.get("pass_receipt")
    if (
        not isinstance(pass_binding, Mapping)
        or not isinstance(pass_binding.get("path"), str)
        or not isinstance(pass_binding.get("sha256"), str)
    ):
        raise Stop("root authorization pass receipt binding is malformed")
    pass_path = Path(pass_binding["path"])
    if not pass_path.is_file() or sha(pass_path) != pass_binding["sha256"]:
        raise Stop("root authorization pass receipt SHA mismatch")
    try:
        pass_value = json.loads(pass_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop("root authorization pass receipt is unreadable") from exc
    if (
        not isinstance(pass_value, dict)
        or pass_value.get("schema_version") != PASS_RECEIPT_SCHEMA
        or pass_value.get("status") != "PASS"
        or pass_value.get("decision") != "FINAL PRODUCTION: GO"
        or pass_value.get("authorization_id") != value["authorization_id"]
        or pass_value.get("plan_sha256") != plan["sha256"]
    ):
        raise Stop("root authorization pass receipt is not bound to this PASS authorization")
    return value


def plan_case_ids(auth: Mapping[str, Any]) -> set[str]:
    plan = auth["plan"]
    path = Path(plan["path"])
    if sha(path) != plan["sha256"]:
        raise Stop("authorized frozen plan SHA mismatch")
    cases: set[str] = set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("record_type") != "case":
            continue
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in cases:
            raise Stop("authorized frozen plan case identity malformed")
        cases.add(case_id)
    if len(cases) != CASE_COUNT:
        raise Stop("authorized frozen plan does not contain exactly 1088 cases")
    return cases


def queue_path(auth: Mapping[str, Any]) -> Path:
    queue = auth["queue"]
    candidate = queue.get("path")
    if isinstance(candidate, str):
        path = Path(candidate)
    else:
        path = Path(queue["dir"]) / "queue.sqlite3"
    if not path.is_file():
        raise Stop(f"authorized queue database missing: {path}")
    return path


def accepted_rows(db_path: Path, plan_ids: set[str], plan_sha256: str) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect("file:" + str(db_path) + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        meta = connection.execute("select value_json from meta where key='plan_sha256'").fetchone()
        if not meta:
            raise Stop("queue plan SHA metadata missing")
        try:
            value = json.loads(meta[0])
        except (TypeError, json.JSONDecodeError):
            value = meta[0]
        if value != plan_sha256:
            raise Stop("queue plan SHA metadata mismatch")
        active = connection.execute(
            "select count(*) from attempts where status in ('active','orphaned')"
        ).fetchone()[0]
        if active:
            raise Stop(f"queue is not idle: {active} active/orphaned attempt(s)")
        blocked = connection.execute(
            "select count(*) from cases where status='blocked'"
        ).fetchone()[0]
        if blocked:
            raise Stop(f"queue has {blocked} blocked case(s)")
        rows = connection.execute(
            "select c.case_id,c.status,c.accepted_attempt_id,a.attempt_id,a.status attempt_status,"
            "a.artifact_dir,a.artifact_manifest_path,a.artifact_manifest_sha256,"
            "a.result_path,a.result_sha256 "
            "from cases c left join attempts a on a.attempt_id=c.accepted_attempt_id "
            "where c.status='accepted' order by c.ordinal"
        ).fetchall()
    finally:
        connection.close()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = {str(key): row[key] for key in row.keys()}
        case_id = item["case_id"]
        if (
            not isinstance(case_id, str)
            or case_id not in plan_ids
            or case_id in result
            or item["accepted_attempt_id"] != item["attempt_id"]
            or item["attempt_status"] != "accepted"
            or not all(isinstance(item[key], str) and item[key] for key in (
                "accepted_attempt_id", "artifact_dir", "artifact_manifest_path",
                "artifact_manifest_sha256", "result_path", "result_sha256"
            ))
        ):
            raise Stop("accepted queue binding is malformed")
        result[case_id] = item
    return result


def read_ledger(
    path: Path, controller: Any, plan_ids: set[str], plan_sha256: str,
    accepted: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], set[str]]:
    if not path.exists():
        return (
            {
                "schema": "assignment.accepted-attempt-retention-ledger.v1",
                "plan_sha256": None,
                "retained_cases": [],
            },
            set(),
        )
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise Stop("retention ledger is not an object")
    if value.get("schema") not in (None, "", "assignment.accepted-attempt-retention-ledger.v1"):
        raise Stop("retention ledger schema mismatch")
    if value.get("plan_sha256") not in (None, "", plan_sha256):
        raise Stop("retention ledger plan SHA mismatch")
    records = value.get("retained_cases")
    if not isinstance(records, list):
        raise Stop("retention ledger records missing")
    bound = {
        case_id: (row["accepted_attempt_id"], row["artifact_manifest_sha256"])
        for case_id, row in accepted.items()
    }
    retained = controller.ledger_rows(path, plan_ids, bound)
    return value, retained


def archive_token(case_id: str, attempt_id: str) -> str:
    return hashlib.sha256((case_id + "\0" + attempt_id).encode()).hexdigest()


def validated_remote_root(value: str) -> Path:
    """Return the only approved retained namespace for remote archives."""
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise Stop("remote root must be an absolute path without traversal")
    if path == REMOTE_NAMESPACE:
        path = REMOTE_RETAINED_ROOT
    if path != REMOTE_RETAINED_ROOT or not path.is_relative_to(REMOTE_NAMESPACE):
        raise Stop(f"remote root must be the established retained namespace: {REMOTE_RETAINED_ROOT}")
    return path


def retention_binding(row: Mapping[str, Any], db_path: Path) -> dict[str, Any]:
    artifact_manifest = Path(row["artifact_manifest_path"])
    result_path = Path(row["result_path"])
    binding = {
        "queue_path": str(db_path),
        "case_id": row["case_id"],
        "accepted_attempt_id": row["accepted_attempt_id"],
        "artifact_manifest_sha256": row["artifact_manifest_sha256"],
        "result_sha256": row["result_sha256"],
        "artifact_manifest_path": str(artifact_manifest),
        "result_path": str(result_path),
        "artifact_manifest_file_sha256": sha(artifact_manifest),
        "result_file_sha256": sha(result_path),
    }
    if (
        binding["artifact_manifest_file_sha256"] != row["artifact_manifest_sha256"]
        or binding["result_file_sha256"] != row["result_sha256"]
    ):
        raise Stop(f"accepted attempt payload hash mismatch: {row['accepted_attempt_id']}")
    return binding


def recover_existing_retention(
    *, controller: Any, plan_ids: set[str], accepted: Mapping[str, Mapping[str, Any]],
    row: Mapping[str, Any], out_dir: Path, remote_path: str,
) -> dict[str, Any]:
    """Recover a completed retention whose ledger append was interrupted."""
    receipt_path = out_dir / "accepted-attempt-retention-receipt.json"
    underlying = out_dir / "receipt.json"
    manifest = out_dir / "retention-manifest.json"
    if out_dir.is_symlink() or not out_dir.is_dir() or not receipt_path.is_file():
        raise Stop(f"retention output exists without a completed receipt: {out_dir}")
    try:
        value = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"existing retention receipt is unreadable: {receipt_path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != "assignment.accepted-attempt-retention-receipt.v1"
        or value.get("status") != "PASS"
        or value.get("case_id") != row["case_id"]
        or value.get("attempt_id") != row["accepted_attempt_id"]
        or value.get("artifact_manifest_sha256") != row["artifact_manifest_sha256"]
        or value.get("result_sha256") != row["result_sha256"]
        or value.get("mounted_artifact_path") != row["artifact_dir"]
        or value.get("remote_path") != remote_path
        or value.get("underlying_receipt") != str(underlying)
        or value.get("retention_manifest") != str(manifest)
        or not underlying.is_file()
        or not manifest.is_file()
        or value.get("underlying_receipt_sha256") != sha(underlying)
        or value.get("retention_manifest_sha256") != sha(manifest)
    ):
        raise Stop(f"existing retention receipt binding mismatch: {receipt_path}")
    record = {
        "case_id": row["case_id"],
        "attempt_id": row["accepted_attempt_id"],
        "artifact_manifest_sha256": row["artifact_manifest_sha256"],
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha(receipt_path),
    }
    staged = {
        "schema": "assignment.accepted-attempt-retention-ledger.v1",
        "plan_sha256": None,
        "retained_cases": [record],
    }
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=str(out_dir.parent),
            prefix=".controller-recovery-", suffix=".json", delete=False,
        ) as stream:
            json.dump(staged, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            staged_path = Path(stream.name)
        bound = {
            row["case_id"]: (row["accepted_attempt_id"], row["artifact_manifest_sha256"])
        }
        if controller.ledger_rows(staged_path, plan_ids, bound) != {row["case_id"]}:
            raise Stop(f"controller rejected existing retention proof: {receipt_path}")
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
    return record


def retention_value(
    *, helper: Any, controller: Any, plan_ids: set[str], accepted: Mapping[str, Mapping[str, Any]],
    row: Mapping[str, Any], db_path: Path, out_dir: Path, remote_path: str,
) -> dict[str, Any]:
    binding = retention_binding(row, db_path)
    helper.check(binding)
    if out_dir.exists():
        return recover_existing_retention(
            controller=controller, plan_ids=plan_ids, accepted=accepted,
            row=row, out_dir=out_dir, remote_path=remote_path,
        )
    if sha(RECLAIMER) != RECLAIMER_SHA256:
        raise Stop("reviewed retention reclaimer SHA mismatch")
    reclaimer = load_module(RECLAIMER, "rolling_idle_retention_reclaimer")
    reclaimer.TARGET = Path(row["artifact_dir"])
    reclaimer.OUT = out_dir
    reclaimer.REMOTE = Path(remote_path)
    if reclaimer.main() != 0:
        raise Stop(f"retention reclaimer failed: {row['accepted_attempt_id']}")
    helper.check(binding)
    underlying = out_dir / "receipt.json"
    manifest = out_dir / "retention-manifest.json"
    if not underlying.is_file() or not manifest.is_file():
        raise Stop(f"retention proof is incomplete: {row['accepted_attempt_id']}")
    receipt = {
        "schema": "assignment.accepted-attempt-retention-receipt.v1",
        "status": "PASS",
        "case_id": row["case_id"],
        "attempt_id": row["accepted_attempt_id"],
        "artifact_manifest_sha256": row["artifact_manifest_sha256"],
        "result_sha256": row["result_sha256"],
        "underlying_receipt": str(underlying),
        "underlying_receipt_sha256": sha(underlying),
        "retention_manifest": str(manifest),
        "retention_manifest_sha256": sha(manifest),
        "mounted_artifact_path": row["artifact_dir"],
        "remote_path": remote_path,
        "finished_epoch": time.time(),
    }
    helper.atomic(out_dir / "accepted-attempt-retention-receipt.json", receipt)
    receipt_path = out_dir / "accepted-attempt-retention-receipt.json"
    return {
        "case_id": row["case_id"],
        "attempt_id": row["accepted_attempt_id"],
        "artifact_manifest_sha256": row["artifact_manifest_sha256"],
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha(receipt_path),
    }


def run(
    *, authorization: Path, authorization_sha256: str, ledger: Path, archive_root: Path,
    remote_root: str, execute: bool,
) -> dict[str, Any]:
    auth = load_authorization(authorization, authorization_sha256)
    remote_root_path = validated_remote_root(remote_root)
    plan_ids = plan_case_ids(auth)
    db_path = queue_path(auth)
    plan_sha256 = auth["plan"]["sha256"]
    accepted = accepted_rows(db_path, plan_ids, plan_sha256)
    controller_sha = sha(CONTROLLER)
    if controller_sha != CONTROLLER_SHA256:
        raise Stop(f"reviewed storage controller SHA mismatch: {controller_sha}")
    if sha(RETENTION_HELPER) != RETENTION_HELPER_SHA256:
        raise Stop("reviewed accepted-attempt retention helper SHA mismatch")
    controller = load_module(CONTROLLER, "rolling_idle_storage_controller")
    old_ledger, retained = read_ledger(ledger, controller, plan_ids, plan_sha256, accepted)
    unretained = [row for case_id, row in accepted.items() if case_id not in retained]
    planned: list[dict[str, Any]] = []
    for row in unretained:
        token = archive_token(row["case_id"], row["accepted_attempt_id"])
        planned.append({
            "case_id": row["case_id"],
            "attempt_id": row["accepted_attempt_id"],
            "artifact_manifest_sha256": row["artifact_manifest_sha256"],
            "output_dir": str(archive_root / token),
            "remote_path": str(remote_root_path / token),
        })
    result: dict[str, Any] = {
        "schema": "assignment.rolling-idle-retention-maintenance.v1",
        "status": "READY" if not execute else "PASS",
        "authorization": {"path": str(authorization), "sha256": authorization_sha256},
        "plan_sha256": plan_sha256,
        "queue_path": str(db_path),
        "queue_idle": True,
        "accepted_count": len(accepted),
        "already_retained_count": len(retained),
        "unretained_count": len(unretained),
        "planned": planned,
        "executed": [],
        "next_wave": "separate reviewed launcher invocation after retention ledger merge",
    }
    if not execute:
        return result
    archive_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    records = list(old_ledger.get("retained_cases", [])) if isinstance(old_ledger.get("retained_cases"), list) else []

    def write_ledger() -> None:
        atomic_json(ledger, {
            "schema": "assignment.accepted-attempt-retention-ledger.v1",
            "plan_path": auth["plan"]["path"],
            "plan_sha256": plan_sha256,
            "retained_cases": records,
            "updated_epoch": time.time(),
        })

    for item, row in zip(planned, unretained):
        current = accepted_rows(db_path, plan_ids, plan_sha256)
        if current.get(row["case_id"], {}).get("accepted_attempt_id") != row["accepted_attempt_id"]:
            raise Stop(f"accepted binding changed before retention: {row['case_id']}")
        receipt = retention_value(
            helper=load_module(RETENTION_HELPER, "rolling_idle_retention_helper"),
            controller=controller, plan_ids=plan_ids, accepted=accepted,
            row=row,
            db_path=db_path,
            out_dir=Path(item["output_dir"]),
            remote_path=item["remote_path"],
        )
        result["executed"].append(receipt)
        records.append(receipt)
        write_ledger()
    if not unretained:
        write_ledger()
    result["ledger_path"] = str(ledger)
    result["ledger_sha256"] = sha(ledger)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--authorization-sha256", required=True)
    parser.add_argument("--retention-ledger", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--remote-root", required=True)
    parser.add_argument("--execute-retention", action="store_true")
    args = parser.parse_args()
    with BATCH_LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("shared production rolling-batch lock is busy") from exc
        result = run(
            authorization=args.authorization,
            authorization_sha256=args.authorization_sha256,
            ledger=args.retention_ledger,
            archive_root=args.archive_root,
            remote_root=args.remote_root,
            execute=args.execute_retention,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, Stop, sqlite3.Error) as error:
        print("rolling_idle_maintenance: NOT_READY: " + str(error), file=sys.stderr)
        raise SystemExit(2)
