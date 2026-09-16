import hashlib
import json
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.assignment.shared_case_queue as queue_module
from scripts.assignment.shared_case_queue import (
    CASE_ACCEPTED,
    CASE_BLOCKED,
    CASE_PENDING,
    DISCOVERY_FINGERPRINT_SCHEMA,
    EFFECTIVE_FINGERPRINT_SCHEMA,
    EXPIRED_WORKER_ID,
    LeaseConflict,
    QueueHalted,
    QueueNotReady,
    READY_WORKER_IDS,
    SharedCaseQueue,
)


def _write_sidecar(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    Path(str(path) + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def _worker_finish_loop(queue_dir: str, worker_id: str, output) -> None:
    queue = SharedCaseQueue(Path(queue_dir))
    try:
        while True:
            lease = queue.claim_case(worker_id)
            if lease is None:
                return
            outcome = queue.finish_attempt(
                lease,
                result={
                    "schema_version": "assignment-case-result.v1",
                    "resume_key": lease.resume_key,
                    "status": "completed",
                    "evaluator": {"official_resolved": False},
                },
            )
            output.put(("ok", lease.case_id, outcome["status"]))
    except BaseException as exc:  # pragma: no cover - reported to the parent
        output.put(("error", type(exc).__name__, str(exc)))
        raise


def _claim_then_exit(queue_dir: str, worker_id: str, connection) -> None:
    try:
        lease = SharedCaseQueue(Path(queue_dir)).claim_case(worker_id)
        if lease is None:
            connection.send(("empty",))
        else:
            connection.send(("claimed", os.getpid(), lease.attempt_id, lease.case_id))
        connection.close()
        os._exit(0)
    except BaseException as exc:  # pragma: no cover - reported to the parent
        try:
            connection.send(("error", type(exc).__name__, str(exc)))
            connection.close()
        finally:
            os._exit(1)


def _finish_replay(queue_dir, attempt_id, token, result, connection):
    try:
        outcome = SharedCaseQueue(Path(queue_dir)).finish_attempt(attempt_id, lease_token=token, result=result)
        connection.send(("ok", outcome))
    except Exception as exc:
        connection.send(("error", type(exc).__name__, str(exc)))
    finally:
        connection.close()


def _spawn_then_lose_owner(queue_dir, runner, connection):
    real_popen = queue_module.subprocess.Popen

    def spawn_and_crash(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        connection.send(process.pid)
        connection.close()
        os._exit(0)

    args = queue_module._parser().parse_args([
        "supervise", "--queue-dir", queue_dir, "--worker-id", "worker-00",
        "--runner", runner, "--execute", "--acknowledge-paid-gpu-work", "--max-cases", "1",
    ])
    with patch.object(queue_module.subprocess, "Popen", side_effect=spawn_and_crash):
        queue_module.supervise(args)


def _seal_then_crash(queue_dir, failure, connection):
    queue = SharedCaseQueue(Path(queue_dir))
    lease = queue.claim_case("worker-00")
    connection.send(lease.attempt_id)
    connection.close()
    with patch.object(queue, "_commit_prepared", side_effect=lambda *a, **k: os._exit(0)):
        if failure:
            queue.fail_attempt(lease, reason="fixture failure")
        else:
            queue.finish_attempt(lease, result={"resume_key": lease.resume_key, "status": "completed"})


def _claim_with_low_storage(queue_dir, filesystem_id, connection):
    queue = SharedCaseQueue(Path(queue_dir))
    with patch.object(queue_module.os, "statvfs", return_value=SimpleNamespace(f_bavail=324, f_frsize=1, f_fsid=filesystem_id)):
        try:
            queue.claim_case("worker-00")
            connection.send("unexpected claim")
        except QueueHalted:
            connection.send("halted")
    connection.close()


class SharedCaseQueueTest(TestCase):
    def test_missing_planning_policy_is_advisory_for_full_pool(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1, worker_ids=READY_WORKER_IDS)
            with queue._transaction() as connection:
                connection.execute("UPDATE meta SET value_json = 'true' WHERE key = 'require_all_workers'")
            # Isolate storage from the independently tested fingerprint gate.
            with patch.object(queue, "_fingerprint_gate_error_locked", return_value=None):
                self.assertIsNotNone(queue.claim_case("worker-00"))
            self.assertFalse(queue.status()["dispatch_halted"])

    def test_one_registered_worker_can_claim_without_21_peers(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1, worker_ids=("worker-00",))
            with queue._transaction() as connection:
                connection.execute("UPDATE meta SET value_json = 'true' WHERE key = 'require_all_workers'")
                connection.execute("UPDATE meta SET value_json = ? WHERE key = 'allowed_worker_ids'", (json.dumps(READY_WORKER_IDS),))
            with patch.object(queue, "_fingerprint_gate_error_locked", return_value=None), patch.object(queue, "_storage_gate_error_locked", return_value=None):
                self.assertIsNotNone(queue.claim_case("worker-00"))
            status = queue.status()
            self.assertFalse(status["dispatch_halted"])
            self.assertIn("1 of 22", status["advisories"]["worker_pool_partial"]["message"])

    @staticmethod
    def _storage_policy_fixture(root, *, case_count=2, **overrides):
        evidence = root / "measured-pilot.json"
        evidence.write_text('{"fixture_only": true, "measured_case_peak_bytes": 10}\n')
        policy = root / "storage-policy.json"
        value = {"schema_version": queue_module.STORAGE_POLICY_SCHEMA,
                 "case_count": case_count, "worker_count": 22,
                 "pilot_evidence": {"path": str(evidence), "sha256": _write_sidecar(evidence)},
                 "filesystems": [{"path": str(root), "roles": ["queue", "artifacts", "container_storage"],
                                  "pilot_case_peak_bytes": 10, "final_total_estimate_bytes": 100,
                                  "safety_reserve_bytes": 5, "quota_scope": "no_user_quota", **overrides}]}
        policy.write_text(json.dumps(value))
        return policy, _write_sidecar(policy)

    def test_storage_gate_persists_halt_before_claim_from_real_process(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=2)
            policy, digest = self._storage_policy_fixture(root)
            queue.bind_storage_policy(policy, declared_sha256=digest, review_note="CPU fixture numbers only")
            fsid = os.statvfs(root).f_fsid
            context = multiprocessing.get_context("fork")
            reader, writer = context.Pipe(False)
            process = context.Process(target=_claim_with_low_storage, args=(str(queue.queue_dir), fsid, writer))
            process.start()
            self.assertTrue(reader.poll(5))
            self.assertEqual(reader.recv(), "halted")
            process.join(5)
            self.assertEqual(process.exitcode, 0)
            reader.close()
            queue = SharedCaseQueue(queue.queue_dir)
            self.assertEqual(queue.status()["case_status_counts"], {CASE_PENDING: 2})
            self.assertIn("required=325", queue.status()["halt_reason"])
            with self.assertRaises(QueueHalted):
                queue.claim_case("worker-01")  # new process sees the committed global halt
            with patch.object(queue_module.os, "statvfs", return_value=SimpleNamespace(f_bavail=324, f_frsize=1, f_fsid=fsid)):
                with self.assertRaises(QueueHalted):
                    queue.clear_halt(review_note="still below reserve")
            with patch.object(queue_module.os, "statvfs", return_value=SimpleNamespace(f_bavail=325, f_frsize=1, f_fsid=fsid)):
                queue.clear_halt(review_note="fixture capacity restored and reviewed")
                first = queue.claim_case("worker-00")
                self.assertIsNotNone(first)
                with self.assertRaises(queue_module.ReconciliationRequired):
                    queue.bind_storage_policy(policy, declared_sha256=digest, review_note="cannot weaken with active case")

    def test_storage_unknown_measurement_quota_and_hash_changes_fail_closed(self):
        for overrides in ({"pilot_case_peak_bytes": None}, {"final_total_estimate_bytes": 0}, {"quota_scope": "unknown"}, {"quota_scope": "enforced"}):
            with self.subTest(overrides=overrides), __import__("tempfile").TemporaryDirectory() as temporary:
                root = Path(temporary)
                queue = self._queue(root, count=2)
                policy, digest = self._storage_policy_fixture(root, **overrides)
                with self.assertRaises(QueueNotReady):
                    queue.bind_storage_policy(policy, declared_sha256=digest, review_note="unknown is not proof")
        for mutation in ("policy", "pilot", "probe_error"):
            with self.subTest(mutation=mutation), __import__("tempfile").TemporaryDirectory() as temporary:
                root = Path(temporary)
                queue = self._queue(root, count=2)
                policy, digest = self._storage_policy_fixture(root)
                queue.bind_storage_policy(policy, declared_sha256=digest, review_note="fixture only")
                if mutation == "policy":
                    policy.write_text(policy.read_text() + "\n")
                elif mutation == "pilot":
                    (root / "measured-pilot.json").write_text("changed measurement")
                if mutation == "probe_error":
                    with patch.object(queue_module.os, "statvfs", side_effect=OSError("fixture probe unavailable")):
                        with self.assertRaises(QueueHalted):
                            queue.claim_case("worker-00")
                else:
                    with self.assertRaises(QueueHalted):
                        queue.claim_case("worker-00")
                self.assertEqual(queue.status()["case_status_counts"], {CASE_PENDING: 2})

    def test_full_pool_missing_storage_policy_is_disclosed_once_not_halted(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=2, worker_ids=READY_WORKER_IDS)
            with queue._transaction() as connection:
                connection.execute("UPDATE meta SET value_json = 'true' WHERE key = 'require_all_workers'")
            with patch.object(queue, "_fingerprint_gate_error_locked", return_value=None):
                first = queue.claim_case("worker-00")
                second = queue.claim_case("worker-01")
            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            status = queue.status()
            self.assertFalse(status["dispatch_halted"])
            self.assertIn("storage_policy_missing", status["advisories"])
            self.assertNotIn("worker_pool_partial", status["advisories"])
            connection = queue._connect()
            try:
                recorded = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'advisory_recorded'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(recorded, 1)

    def test_bound_storage_policy_shortfall_still_halts(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=2)
            policy, digest = self._storage_policy_fixture(root)
            queue.bind_storage_policy(policy, declared_sha256=digest, review_note="fixture only")
            fsid = os.statvfs(root).f_fsid
            with patch.object(queue_module.os, "statvfs", return_value=SimpleNamespace(f_bavail=324, f_frsize=1, f_fsid=fsid)):
                with self.assertRaises(QueueHalted):
                    queue.claim_case("worker-00")
            self.assertIn("storage reserve insufficient", queue.status()["halt_reason"])
            self.assertEqual(queue.status()["active_or_orphaned_attempts"], [])
    @staticmethod
    def _fake_result_runner(root):
        runner = root / "runner"
        runner.write_text(f"#!{sys.executable}\n" + '''import hashlib, json, sys
from pathlib import Path
output = Path(sys.argv[sys.argv.index('--output-dir') + 1])
case = json.loads((output / 'case_spec.json').read_text())
result = output / 'case_result.json'
result.write_text(json.dumps({'resume_key': case['resume_key'], 'status': 'completed', 'evaluator': {'official_resolved': False}}))
digest = hashlib.sha256(result.read_bytes()).hexdigest()
Path(str(result) + '.sha256').write_text(f'{digest}  {result.name}\\n')
''')
        runner.chmod(0o755)
        return runner

    def test_sealed_before_sqlite_commit_crash_recovers_result_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure), __import__("tempfile").TemporaryDirectory() as temporary:
                queue = self._queue(Path(temporary), count=1)
                context = multiprocessing.get_context("fork")
                reader, writer = context.Pipe(False)
                owner = context.Process(target=_seal_then_crash, args=(str(queue.queue_dir), failure, writer))
                owner.start()
                self.assertTrue(reader.poll(5))
                attempt_id = reader.recv()
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)
                reader.close()
                self.assertEqual(queue.reconcile(attempt_id=attempt_id)[0]["state"], "orphaned")
                manifest = Path(queue._attempt_row(attempt_id)["artifact_dir"]) / "artifact_manifest.json"
                original = manifest.read_bytes()
                for _ in range(2):
                    self.assertTrue(queue.reconcile_orphan(attempt_id)["durable_result"])
                action = "retry" if failure else "accept"
                outcome = queue.reconcile_orphan(attempt_id, action=action, review_note="CPU fixture exit verified")
                self.assertEqual(outcome["status"], "requeued" if failure else "accepted")
                self.assertTrue(queue.reconcile_orphan(attempt_id, action=action)["idempotent"])
                self.assertEqual(manifest.read_bytes(), original)
                if failure:
                    lease = queue.claim_case("worker-00")
                    queue.finish_attempt(lease, result=self._completed(lease))
                self.assertEqual(queue.audit_coverage()["status"], "pass")

    def test_guardian_recovers_completed_child_after_owner_crash(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=1)
            runner = self._fake_result_runner(root)
            context = multiprocessing.get_context("fork")
            reader, writer = context.Pipe(False)
            owner = context.Process(target=_spawn_then_lose_owner, args=(str(queue.queue_dir), str(runner), writer))
            owner.start()
            self.assertTrue(reader.poll(5))
            guardian = reader.recv()
            owner.join(10)
            reader.close()
            try:
                deadline = time.monotonic() + 5
                while queue_module._process_identity(guardian)["status"] == "alive" and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(owner.exitcode, 0)
                attempt = queue.status()["active_or_orphaned_attempts"][0]
                self.assertEqual(queue._attempt_row(attempt["attempt_id"])["launch_state"], "exited")
                self.assertEqual(queue.reconcile(attempt_id=attempt["attempt_id"])[0]["state"], "orphaned")
                self.assertEqual(queue.reconcile_orphan(attempt["attempt_id"], action="accept")["status"], "accepted")
                self.assertEqual(queue.audit_coverage()["status"], "pass")
            finally:
                try:
                    os.killpg(guardian, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_detached_descendant_stays_held_even_after_guardian_is_killed(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=1)
            marker = root / "detached-pid"
            runner = root / "runner"
            runner.write_text(f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\nif os.fork() == 0:\n os.setsid()\n Path({str(marker)!r}).write_text(str(os.getpid()))\n time.sleep(30)\n os._exit(0)\nos._exit(0)\n")
            runner.chmod(0o755)
            context = multiprocessing.get_context("fork")
            reader, writer = context.Pipe(False)
            owner = context.Process(target=_spawn_then_lose_owner, args=(str(queue.queue_dir), str(runner), writer))
            guardian = detached = None
            try:
                owner.start()
                self.assertTrue(reader.poll(5))
                guardian = reader.recv()
                owner.join(5)
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                detached = int(marker.read_text())
                self.assertEqual(queue_module._process_identity(guardian)["status"], "alive")
                attempt_id = queue.status()["active_or_orphaned_attempts"][0]["attempt_id"]
                self.assertEqual(queue._attempt_row(attempt_id)["launch_state"], "bound")
                os.killpg(guardian, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while queue_module._process_identity(guardian)["status"] == "alive" and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertEqual(queue_module._process_identity(detached)["status"], "alive")
                self.assertEqual(queue.reconcile(attempt_id=attempt_id)[0]["state"], "identity_unknown")
                with self.assertRaises(queue_module.ReconciliationRequired):
                    queue.reconcile_orphan(attempt_id, action="retry", review_note="unproven child must stay held")
                self.assertIsNone(queue.claim_case("worker-01"))
            finally:
                for pid in (detached, guardian):
                    if pid is not None:
                        try:
                            os.killpg(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                if owner.is_alive():
                    owner.kill()
                owner.join(5)
                reader.close()

    def test_completion_durability_error_halts_assignment(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=2)
            lease = queue.claim_case("worker-00")
            with patch.object(queue_module, "_fsync_directory", side_effect=OSError("fixture directory fsync failed")):
                with self.assertRaises(OSError):
                    queue.finish_attempt(lease, result=self._completed(lease))
            self.assertTrue(queue.status()["dispatch_halted"])
            with self.assertRaises(QueueHalted):
                queue.claim_case("worker-01")

    def test_supervisor_cpu_fixture_advances_immediately(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=2)
            args = queue_module._parser().parse_args([
                "supervise", "--queue-dir", str(queue.queue_dir), "--worker-id", "worker-00",
                "--runner", str(self._fake_result_runner(root)), "--execute",
                "--acknowledge-paid-gpu-work", "--max-cases", "2",
            ])
            self.assertEqual(queue_module.supervise(args), 3)
            self.assertEqual(queue.status()["case_status_counts"], {CASE_ACCEPTED: 2})
            self.assertEqual(queue.audit_coverage()["status"], "pass")

    def test_unspawned_intent_and_live_child_cannot_finalize_or_retry(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1)
            lease = queue.claim_case("worker-00")
            queue.begin_launch(lease, [sys.executable, "-c", "pass"])
            with self.assertRaises(LeaseConflict):
                queue.begin_launch(lease, [sys.executable, "-c", "pass"])
            self.assertEqual(queue.reconcile(attempt_id=lease.attempt_id)[0]["state"], "identity_unknown")
            for operation in (
                lambda: queue.finish_attempt(lease, result=self._completed(lease)),
                lambda: queue.fail_attempt(lease, reason="unknown child"),
                lambda: queue.retry_attempt(lease, reason="unknown child", review_note="not proof"),
            ):
                with self.assertRaises(queue_module.ReconciliationRequired):
                    operation()
            self.assertFalse((lease.artifact_dir / "case_result.json").exists())
            self.assertIsNone(queue.claim_case("worker-01"))

    def test_completed_nonzero_exit_halts_without_quality_retry(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1)
            lease = queue.claim_case("worker-00")
            result = self._completed(lease)
            self.assertEqual(queue.finish_attempt(lease, result=result, process_returncode=1)["status"], "halted")
            self.assertTrue(queue.finish_attempt(lease, result=result, process_returncode=1)["idempotent"])
            self.assertTrue(queue.status()["dispatch_halted"])
            with self.assertRaises(QueueNotReady):
                queue.retry_attempt(lease, reason="exit recovery", review_note="completed means no rerun")

    def test_conflicting_accepted_replay_preserves_unique_completion_and_files(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1)
            lease = queue.claim_case("worker-00")
            result = self._completed(lease)
            queue.finish_attempt(lease, result=result)
            original = {p.name: p.read_bytes() for p in lease.artifact_dir.iterdir() if p.is_file()}
            with self.assertRaises(queue_module.QueueError):
                queue.finish_attempt(lease.attempt_id, lease_token="0" * 32, result=result)
            self.assertFalse(queue.status()["dispatch_halted"])
            with self.assertRaises(queue_module.ArtifactIntegrityError):
                queue.finish_attempt(lease, result=self._completed(lease, altered=True))
            with self.assertRaises(queue_module.ArtifactIntegrityError):
                queue.fail_attempt(lease, reason="late failure")
            self.assertEqual({p.name: p.read_bytes() for p in lease.artifact_dir.iterdir() if p.is_file()}, original)
            self.assertEqual(queue.status()["case_status_counts"], {CASE_ACCEPTED: 1})
            self.assertTrue(queue.status()["dispatch_halted"])

    @staticmethod
    def _completed(lease, **extra):
        return {"resume_key": lease.resume_key, "status": "completed",
                "evaluator": {"official_resolved": False}, **extra}

    def test_completed_capture_failure_rejected_regression(self):
        flags = [
            {"capture_integrity": False},
            {"failure": {"halt_matrix": True}},
            {"failure": {"classification": "infrastructure_evidence"}},
            {"integrity": {"status": "fail"}},
        ]
        for flag in flags:
            with self.subTest(flag=flag), __import__("tempfile").TemporaryDirectory() as temporary:
                queue = self._queue(Path(temporary), count=2)
                lease = queue.claim_case("worker-00")
                self.assertEqual(queue.finish_attempt(lease, result=self._completed(lease, **flag))["status"], "halted")
                self.assertEqual(queue.status()["case_status_counts"].get(CASE_ACCEPTED, 0), 0)
                with self.assertRaises(QueueHalted):
                    queue.claim_case("worker-01")

    def test_malformed_terminal_capture_metadata_halts(self):
        for extra in (
            {"status": []}, {"failure": []}, {"classification": []}, {"capture_integrity": "false"},
            {"failure": {"halt_matrix": "true"}}, {"halt_matrix": 1}, {"halt_matrix": None},
            {"integrity": "failed"}, {"integrity": []}, {"integrity": {"status": []}},
        ):
            with self.subTest(extra=extra), __import__("tempfile").TemporaryDirectory() as temporary:
                queue = self._queue(Path(temporary), count=1)
                lease = queue.claim_case("worker-00")
                with self.assertRaises(queue_module.ArtifactIntegrityError):
                    queue.finish_attempt(lease, result=self._completed(lease, **extra))
                self.assertTrue(queue.status()["dispatch_halted"])
                self.assertEqual(queue.status()["case_status_counts"], {CASE_BLOCKED: 1})

    def test_completion_replay_real_processes_regression(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=1)
            lease = queue.claim_case("worker-00")
            result = self._completed(lease)
            context = multiprocessing.get_context("fork")
            processes, readers = [], []
            for _ in range(3):
                reader, writer = context.Pipe(False)
                process = context.Process(target=_finish_replay, args=(str(queue.queue_dir), lease.attempt_id, lease.lease_token, result, writer))
                process.start()
                processes.append(process)
                readers.append(reader)
            messages = []
            for process, reader in zip(processes, readers):
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertTrue(reader.poll(2))
                messages.append(reader.recv())
                reader.close()
            self.assertEqual([item[0] for item in messages], ["ok"] * 3, messages)
            self.assertEqual(sum(not item[1]["idempotent"] for item in messages), 1)
            self.assertEqual(queue.audit_coverage()["status"], "pass")

    def test_failure_replay_and_conflicting_replay_regression(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            queue = self._queue(Path(temporary), count=2)
            lease = queue.claim_case("worker-00")
            first = queue.fail_attempt(lease, reason="fixture crashed")
            self.assertEqual(first["status"], "retryable")
            self.assertTrue(queue.fail_attempt(lease, reason="fixture crashed")["idempotent"])
            with self.assertRaises(queue_module.ArtifactIntegrityError):
                queue.fail_attempt(lease, reason="different failure")
            self.assertTrue(queue.status()["dispatch_halted"])

    def test_complete_evidence_audit_regression(self):
        for mutation in ("edit", "delete", "extra", "symlink", "manifest_binding"):
            with self.subTest(mutation=mutation), __import__("tempfile").TemporaryDirectory() as temporary:
                queue = self._queue(Path(temporary), count=1)
                lease = queue.claim_case("worker-00")
                evidence = lease.artifact_dir / "raw-evidence.txt"
                evidence.write_text("original")
                queue.finish_attempt(lease, result=self._completed(lease))
                if mutation == "edit":
                    evidence.write_text("changed")
                elif mutation == "delete":
                    evidence.unlink()
                elif mutation == "extra":
                    (lease.artifact_dir / "unsealed.txt").write_text("extra")
                elif mutation == "symlink":
                    evidence.unlink()
                    evidence.symlink_to("case_spec.json")
                else:
                    manifest = lease.artifact_dir / "artifact_manifest.json"
                    value = json.loads(manifest.read_text())
                    value["worker_id"] = "worker-22"
                    manifest.write_text(json.dumps(value))
                    digest = _write_sidecar(manifest)
                    # Even a consistent hash/sidecar must not bypass identity binding.
                    with queue._transaction() as tx:
                        tx.execute("UPDATE attempts SET artifact_manifest_sha256 = ? WHERE attempt_id = ?", (digest, lease.attempt_id))
                self.assertEqual(queue.audit_coverage()["status"], "fail")
                self.assertTrue(queue.status()["dispatch_halted"])

    def test_unknown_proc_proof_is_not_dead_regression(self):
        original = Path.read_text

        def denied(path, *args, **kwargs):
            if str(path) == f"/proc/{os.getpid()}/stat":
                raise PermissionError("fixture /proc denied")
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", denied):
            self.assertEqual(queue_module._process_identity(os.getpid())["status"], "unknown")

    def test_spawn_gap_holds_live_child_and_unknown_exit_regression(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=1)
            marker = root / "fixture-running"
            runner = root / "runner"
            runner.write_text(f"#!{sys.executable}\nimport time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('running')\ntime.sleep(30)\n")
            runner.chmod(0o755)
            context = multiprocessing.get_context("fork")
            reader, writer = context.Pipe(False)
            owner = context.Process(target=_spawn_then_lose_owner, args=(str(queue.queue_dir), str(runner), writer))
            child_pid = None
            try:
                owner.start()
                self.assertTrue(reader.poll(10))
                child_pid = reader.recv()
                owner.join(10)
                self.assertEqual(owner.exitcode, 0)
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "CPU fixture must actually start")
                attempt = queue.status()["active_or_orphaned_attempts"][0]
                state = queue.reconcile(attempt_id=attempt["attempt_id"])[0]
                self.assertIn(state["state"], {"live", "identity_unknown"})
                with self.assertRaises((LeaseConflict, QueueHalted)):
                    queue.claim_case("worker-00")
                os.killpg(child_pid, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while queue_module._process_identity(child_pid)["status"] == "alive" and time.monotonic() < deadline:
                    time.sleep(0.02)
                state = queue.reconcile(attempt_id=attempt["attempt_id"])[0]
                self.assertEqual(state["state"], "identity_unknown")
                with self.assertRaises(queue_module.ReconciliationRequired):
                    queue.reconcile_orphan(attempt["attempt_id"], action="retry", review_note="PID gone is insufficient")
                self.assertIsNone(queue.claim_case("worker-01"))
            finally:
                if child_pid is not None:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if owner.is_alive():
                    owner.kill()
                owner.join(5)
                reader.close()

    def _queue(self, root: Path, count: int = 8, *, worker_ids=None, require_all_workers: bool = False):
        ids = tuple(worker_ids or ("worker-00", "worker-01", "worker-02", "worker-03"))
        cases = [
            {
                "record_type": "case",
                "schema_version": "assignment-steps-1-3-plan.v1",
                "resume_key": f"case-{index:03d}",
                "per_case_deadline_seconds": 10,
            }
            for index in range(count)
        ]
        queue = SharedCaseQueue.create(
            root / "queue",
            cases=cases,
            worker_ids=ids,
            require_all_workers=require_all_workers,
        )
        for index, worker_id in enumerate(ids):
            (root / f"inventory-{index}").write_text(f"inventory-{worker_id}\n", encoding="utf-8")
            (root / f"runtime-{index}").write_text(f"runtime-{worker_id}\n", encoding="utf-8")
            (root / f"source-{index}").write_text(f"source-{worker_id}\n", encoding="utf-8")
            queue.register_worker(
                worker_id,
                endpoint={
                    "endpoint_id": f"endpoint-{worker_id}",
                    "api_base": f"http://127.0.0.1:{19000 + index}/v1",
                    "server_identity": f"server-{worker_id}",
                },
                inventory=root / f"inventory-{index}",
                runtime=root / f"runtime-{index}",
                source=root / f"source-{index}",
            )
        return queue

    def test_real_multiprocess_claims_are_unique_and_accept_unresolved_outcomes(self):
        with self.subTest("workers claim from one WAL database"):
            with __import__("tempfile").TemporaryDirectory() as temporary:
                root = Path(temporary)
                queue = self._queue(root, count=32)
                context = multiprocessing.get_context("fork")
                output = context.SimpleQueue()
                processes = [
                    context.Process(target=_worker_finish_loop, args=(str(queue.queue_dir), worker_id, output))
                    for worker_id in ("worker-00", "worker-01", "worker-02", "worker-03")
                ]
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(30)
                    self.assertFalse(process.is_alive())
                    self.assertEqual(process.exitcode, 0)

                messages = [output.get() for _ in range(32)]
                errors = [message for message in messages if message[0] == "error"]
                self.assertEqual(errors, [])
                claimed = [message[1] for message in messages if message[0] == "ok"]
                self.assertEqual(len(claimed), 32)
                self.assertEqual(len(set(claimed)), 32)
                self.assertEqual(queue.status()["case_status_counts"], {CASE_ACCEPTED: 32})
                self.assertEqual(queue.audit_coverage()["status"], "pass")

                connection = queue._connect()
                try:
                    rows = connection.execute(
                        "SELECT status, COUNT(*) AS count FROM attempts GROUP BY status"
                    ).fetchall()
                    accepted = connection.execute(
                        "SELECT COUNT(*) FROM attempts WHERE status = 'accepted'"
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual({row["status"]: row["count"] for row in rows}, {"accepted": 32})
                self.assertEqual(accepted, 32)

    def test_crash_reconciliation_recovers_without_expiring_a_live_lease(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=1, worker_ids=("worker-00", "worker-01"))
            context = multiprocessing.get_context("fork")
            parent, child = context.Pipe(False)
            process = context.Process(target=_claim_then_exit, args=(str(queue.queue_dir), "worker-00", child))
            process.start()
            message = parent.recv()
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(message[0], "claimed")
            attempt_id = message[2]

            reconciled = queue.reconcile(attempt_id=attempt_id)
            self.assertEqual(reconciled[0]["state"], "orphaned")
            with self.assertRaises(LeaseConflict):
                queue.claim_case("worker-00")

            recovered = queue.reconcile_orphan(
                attempt_id,
                action="retry",
                review_note="verified owner PID is gone and no result was durable",
            )
            self.assertEqual(recovered["status"], "requeued")
            lease = queue.claim_case("worker-00")
            self.assertIsNotNone(lease)
            assert lease is not None
            queue.finish_attempt(
                lease,
                result={
                    "schema_version": "assignment-case-result.v1",
                    "resume_key": lease.resume_key,
                    "status": "completed",
                    "evaluator": {"official_resolved": False},
                },
            )
            self.assertEqual(queue.audit_coverage()["status"], "pass")
            connection = queue._connect()
            try:
                rows = connection.execute(
                    "SELECT attempt_id, status, retry_of_attempt_id, retry_provenance_json "
                    "FROM attempts ORDER BY attempt_no"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual([row["status"] for row in rows], ["requeued", "accepted"])
            self.assertEqual(rows[1]["retry_of_attempt_id"], rows[0]["attempt_id"])
            self.assertIn("crash_recovery", rows[0]["retry_provenance_json"])

    def test_capture_failure_halts_new_assignment_and_endpoint_observer_rules_are_closed(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=2, worker_ids=("worker-00", "worker-01"))
            first = queue.claim_case("worker-00")
            self.assertIsNotNone(first)
            assert first is not None
            outcome = queue.fail_attempt(
                first,
                reason="serving witness was incomplete",
                classification="infrastructure_evidence",
            )
            self.assertEqual(outcome["status"], "halted")
            self.assertEqual(queue.status()["case_status_counts"][CASE_BLOCKED], 1)
            with self.assertRaises(QueueHalted):
                queue.claim_case("worker-01")

            observer = queue.register_observer(
                "observer-rollout",
                endpoint={"endpoint_id": "observer-endpoint", "api_base": "http://127.0.0.1:19999/v1"},
            )
            self.assertFalse(observer["can_lease_cases"])
            self.assertEqual(len(queue.status()["registered_observers"]), 1)
            with self.assertRaises(LeaseConflict):
                queue.register_worker(
                    "worker-01",
                    endpoint={"endpoint_id": "observer-endpoint", "api_base": "http://127.0.0.1:19999/v1"},
                    inventory=root / "inventory-1",
                    runtime=root / "runtime-1",
                    source=root / "source-1",
                )
            with self.assertRaises(QueueNotReady):
                queue.register_observer(
                    "worker-00",
                    endpoint={"endpoint_id": "other-observer", "api_base": "http://127.0.0.1:19998/v1"},
                )
            with self.assertRaises(QueueNotReady):
                queue.register_observer(
                    "observer-bad",
                    endpoint={"endpoint_id": "other-observer", "api_base": "http://127.0.0.1:19998/v1"},
                    can_lease_cases=True,
                )

    def test_discovery_context_is_a_blocking_gate_until_effective_fingerprint_is_bound(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            discovery = root / "fingerprints.json"
            discovery.write_text(
                json.dumps(
                    {
                        "schema_version": DISCOVERY_FINGERPRINT_SCHEMA,
                        "scope": "read-only discovery",
                        "nodes": [
                            {
                                "node": "node-01",
                                "inventory": {
                                    "serving_processes": [
                                        {"options": {"--port": str(18000 + index), "--max-model-len": "32768"}}
                                        for index in range(22)
                                    ]
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            queue = SharedCaseQueue.create(
                root / "queue",
                cases=[
                    {
                        "record_type": "case",
                        "schema_version": "assignment-steps-1-3-plan.v1",
                        "resume_key": "case-000",
                    }
                ],
                worker_ids=("worker-00",),
                require_all_workers=False,
                fingerprint_path=discovery,
            )
            (root / "inventory").write_text("i\n", encoding="utf-8")
            (root / "runtime").write_text("r\n", encoding="utf-8")
            (root / "source").write_text("s\n", encoding="utf-8")
            queue.register_worker(
                "worker-00",
                endpoint={"endpoint_id": "endpoint-worker-00", "api_base": "http://127.0.0.1:19000/v1"},
                inventory=root / "inventory",
                runtime=root / "runtime",
                source=root / "source",
            )
            with self.assertRaises(QueueHalted):
                queue.claim_case("worker-00")
            self.assertEqual(queue.status()["fingerprint_gate_status"], "discovery_only")

            effective = root / "effective.json"
            effective.write_text(
                json.dumps(
                    {
                        "schema_version": EFFECTIVE_FINGERPRINT_SCHEMA,
                        "server_max_model_len": 65536,
                        "workers": [
                            {
                                "worker_id": worker_id,
                                "endpoint_id": f"endpoint-{worker_id}",
                                "server_max_model_len": 65536,
                                "effective_config_fingerprint": f"{index + 1:064x}",
                            }
                            for index, worker_id in enumerate(READY_WORKER_IDS)
                        ],
                        "observer_rollout": {
                            "leases_cases": False,
                            "active_trajectory_allowed": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            bound = queue.bind_effective_fingerprints(effective, review_note="root rollout artifact reviewed")
            self.assertTrue(queue.status()["dispatch_halted"])
            queue.clear_halt(review_note="effective fixture and all gates reviewed")
            self.assertEqual(bound["summary"]["server_max_model_len"], 65536)
            lease = queue.claim_case("worker-00")
            self.assertIsNotNone(lease)

    def test_confirmation_import_preserves_all_original_case_bytes_and_calls_existing_loader(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            specs = snapshot / "configuration-confirmation-case-specs"
            specs.mkdir(parents=True)
            cases = []
            original = {}
            for index in range(96):
                case_id = f"confirmation-case-{index:03d}"
                payload = (
                    '{"record_type":"case", "schema_version":"assignment.configuration-confirmation-case.v2", '
                    f'"resume_key":"{case_id}"}}\n'
                ).encode("utf-8")
                spec_path = specs / f"{index + 1:03d}.json"
                spec_path.write_bytes(payload)
                original[case_id] = payload
                cases.append(
                    {
                        "candidate_case_id": case_id,
                        "case_spec": {
                            "path": str(spec_path.relative_to(snapshot)),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        },
                    }
                )
            plan_path = snapshot / "configuration_confirmation_execution_plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": "assignment.configuration-confirmation-execution-plan.v2",
                        "case_schema": "assignment.configuration-confirmation-case.v2",
                        "execution_case_count": 96,
                        "cases": cases,
                    }
                ),
                encoding="utf-8",
            )
            plan_sha = _write_sidecar(plan_path)
            runtime_path = root / "runtime-manifest.json"
            runtime_path.write_text('{"fixture": true}\n', encoding="utf-8")
            _write_sidecar(runtime_path)
            fake_runner = root / "sweagent_case_runner.py"
            fake_runner.write_text("# fixture\n", encoding="utf-8")
            calls = []

            def fake_load_case(path, *, confirmation_plan, confirmation_plan_sha256, runtime_manifest):
                calls.append((Path(path), Path(confirmation_plan), confirmation_plan_sha256, runtime_manifest))
                value = json.loads(Path(path).read_text(encoding="utf-8"))
                return value

            with patch.object(
                queue_module,
                "_load_confirmation_case_validator",
                return_value=(fake_load_case, fake_runner, hashlib.sha256(fake_runner.read_bytes()).hexdigest()),
            ):
                queue = SharedCaseQueue.import_confirmation(
                    root / "queue",
                    confirmation_plan=plan_path,
                    confirmation_plan_sha256=plan_sha,
                    runtime_manifest=runtime_path,
                    worker_ids=("worker-00",),
                    require_all_workers=False,
                    case_runner_path=fake_runner,
                )

            self.assertEqual(len(calls), 96)
            self.assertTrue(all(call[2] == plan_sha for call in calls))
            self.assertEqual(queue.status()["case_count"], 96)
            self.assertTrue(queue.meta("confirmation_import")["original_case_bytes_preserved"])
            connection = queue._connect()
            try:
                rows = connection.execute("SELECT case_id, case_bytes FROM cases ORDER BY ordinal").fetchall()
            finally:
                connection.close()
            self.assertEqual(len(rows), 96)
            self.assertEqual(
                {row["case_id"]: bytes(row["case_bytes"]) for row in rows},
                original,
            )

    def test_retry_rejects_quality_or_unresolved_reason_after_an_accepted_completion(self):
        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = self._queue(root, count=1, worker_ids=("worker-00",))
            lease = queue.claim_case("worker-00")
            assert lease is not None
            queue.finish_attempt(
                lease,
                result={
                    "schema_version": "assignment-case-result.v1",
                    "resume_key": lease.resume_key,
                    "status": "completed",
                    "evaluator": {"official_resolved": False},
                },
            )
            self.assertEqual(queue.status()["case_status_counts"], {CASE_ACCEPTED: 1})
            with self.assertRaises(QueueNotReady):
                queue.retry_attempt(
                    lease,
                    reason="retry because the quality is unresolved",
                    classification="quality_retry",
                )
            self.assertEqual(queue.status()["case_status_counts"], {CASE_ACCEPTED: 1})

    def test_exact_ready_pool_excludes_expired_worker(self):
        self.assertEqual(len(READY_WORKER_IDS), 22)
        self.assertNotIn(EXPIRED_WORKER_ID, READY_WORKER_IDS)
        self.assertEqual(READY_WORKER_IDS, tuple(f"worker-{i:02d}" for i in list(range(11)) + list(range(12, 23))))
