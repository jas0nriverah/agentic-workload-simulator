from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("shared_case_queue_history_v4.py")
SPEC = importlib.util.spec_from_file_location("shared_case_queue_history_v4_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
queue_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = queue_module
SPEC.loader.exec_module(queue_module)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _descriptor(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha(path)}


def _state_digest(queue: queue_module.SharedCaseQueue) -> str:
    connection = queue._connect()
    try:
        return queue_module._preserved_queue_state_digest(connection)
    finally:
        connection.close()


class SequentialBindingRevisionTest(unittest.TestCase):
    def _fixture(self, temporary: Path, worker_count: int = 1):
        workers = [f"worker-{index:02d}" for index in range(worker_count)]
        files: dict[str, dict[str, Path]] = {}
        for index, worker_id in enumerate(workers):
            values = {}
            for label, payload in (
                ("inventory", {"hardware": worker_id}),
                ("runtime-old", {"runtime_epoch": f"old-{worker_id}"}),
                ("runtime-first", {"runtime_epoch": f"first-{worker_id}"}),
                ("runtime-second", {"runtime_epoch": f"second-{worker_id}"}),
                ("source", {"source": "clean"}),
            ):
                path = temporary / f"{label}-{index}.json"
                path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
                values[label] = path
            files[worker_id] = values

        def manifest(path: Path, runtime_label: str, *, changed_endpoint: str | None = None) -> Path:
            rows = []
            for index, worker_id in enumerate(workers):
                endpoint_id = changed_endpoint if changed_endpoint and worker_id == workers[-1] else f"endpoint-{index:02d}"
                rows.append({
                    "worker_id": worker_id,
                    "enabled": True,
                    "endpoint": {
                        "endpoint_id": endpoint_id,
                        "api_base": f"http://worker.test:{18016 + index}/v1",
                        "server_identity": f"server-{index:02d}",
                    },
                    "inventory": _descriptor(files[worker_id]["inventory"]),
                    "runtime": _descriptor(files[worker_id][runtime_label]),
                    "source": _descriptor(files[worker_id]["source"]),
                })
            path.write_text(json.dumps({
                "schema_version": "assignment.worker-pool-manifest.v1",
                "workers": rows,
            }, indent=2) + "\n", encoding="utf-8")
            return path

        old_manifest = manifest(temporary / "workers-old.json", "runtime-old")
        first_manifest = manifest(temporary / "workers-first.json", "runtime-first")
        second_manifest = manifest(temporary / "workers-second.json", "runtime-second")
        bad_manifest = manifest(temporary / "workers-bad.json", "runtime-second", changed_endpoint="endpoint-other")
        queue = queue_module.SharedCaseQueue.create(
            queue_dir=temporary / "queue",
            cases=[
                {"schema_version": "assignment-production-v2-plan.v1", "resume_key": "fixture-case-00", "task": "fixture"},
                {"schema_version": "assignment-production-v2-plan.v1", "resume_key": "fixture-case-01", "task": "fixture"},
            ],
            worker_ids=workers,
            artifact_root=temporary / "artifacts",
            require_all_workers=False,
        )
        queue.register_workers_manifest(old_manifest)
        return {
            "queue": queue,
            "workers": workers,
            "old_manifest": old_manifest,
            "first_manifest": first_manifest,
            "second_manifest": second_manifest,
            "bad_manifest": bad_manifest,
        }

    @staticmethod
    def _finish(queue, lease) -> None:
        queue.finish_attempt(lease, result={
            "schema_version": "assignment-case-result.v1",
            "resume_key": lease.resume_key,
            "status": "completed",
            "evaluator": {"official_resolved": False},
        })

    def test_two_sequential_revisions_preserve_historical_audits(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw))
            queue = fixture["queue"]
            first_lease = queue.claim_case("worker-00")
            self.assertIsNotNone(first_lease)
            self._finish(queue, first_lease)
            before_first = _state_digest(queue)
            first = queue.revise_idle_worker_bindings(
                fixture["first_manifest"], migration_id="revision-1",
                source_queue_state_sha256=before_first, review_note="first runtime revision",
            )
            self.assertEqual(first["revision_number"], 1)
            self.assertEqual(_state_digest(queue), before_first)
            first_audit = queue.audit_coverage()
            self.assertEqual(first_audit["status"], "fail")
            self.assertEqual(first_audit["errors"], ["case fixture-case-01 is pending"])
            # A live v3 queue has the first revision object but no history list.
            # The second revision must upgrade that representation in place.
            connection = sqlite3.connect(queue.db_path)
            try:
                connection.execute("DELETE FROM meta WHERE key = 'binding_revision_history'")
                connection.commit()
            finally:
                connection.close()

            second_lease = queue.claim_case("worker-00")
            self.assertIsNotNone(second_lease)
            self._finish(queue, second_lease)
            before_second = _state_digest(queue)
            second = queue.revise_idle_worker_bindings(
                fixture["second_manifest"], migration_id="revision-2",
                source_queue_state_sha256=before_second, review_note="second runtime epoch revision",
            )
            self.assertEqual(second["revision_number"], 2)
            self.assertEqual(_state_digest(queue), before_second)
            self.assertEqual(queue.audit_coverage()["status"], "pass")

            status = queue.status()
            self.assertEqual(
                [item["migration_id"] for item in status["binding_revision_history"]],
                ["revision-1", "revision-2"],
            )
            connection = sqlite3.connect(queue.db_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM worker_binding_history WHERE worker_id = 'worker-00'").fetchone()[0],
                    2,
                )
            finally:
                connection.close()

            idempotent = queue.revise_idle_worker_bindings(
                fixture["second_manifest"], migration_id="revision-2",
                source_queue_state_sha256=_state_digest(queue), review_note="repeat second revision",
            )
            self.assertEqual(idempotent["status"], "already_applied")
            with self.assertRaises(queue_module.QueueNotReady):
                queue.revise_idle_worker_bindings(
                    fixture["second_manifest"], migration_id="revision-1",
                    source_queue_state_sha256=_state_digest(queue), review_note="reused ID must reject divergent manifest",
                )

    def test_active_attempt_blocks_revision_without_metadata(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw))
            queue = fixture["queue"]
            lease = queue.claim_case("worker-00")
            self.assertIsNotNone(lease)
            before = _state_digest(queue)
            with self.assertRaises(queue_module.ReconciliationRequired):
                queue.revise_idle_worker_bindings(
                    fixture["first_manifest"], migration_id="active-rejected",
                    source_queue_state_sha256=before, review_note="active attempt must remain held",
                )
            self.assertEqual(_state_digest(queue), before)
            self.assertIsNone(queue.status()["binding_revision"])

    def test_midloop_failure_rolls_back_prior_history_insert(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw), worker_count=2)
            queue = fixture["queue"]
            before = _state_digest(queue)
            with self.assertRaises(queue_module.QueueNotReady):
                queue.revise_idle_worker_bindings(
                    fixture["bad_manifest"], migration_id="midloop-rejected",
                    source_queue_state_sha256=before, review_note="second endpoint identity must reject",
                )
            self.assertEqual(_state_digest(queue), before)
            connection = sqlite3.connect(queue.db_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM worker_binding_history").fetchone()[0], 0)
                self.assertIsNone(connection.execute("SELECT 1 FROM meta WHERE key = 'binding_revision'").fetchone())
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
