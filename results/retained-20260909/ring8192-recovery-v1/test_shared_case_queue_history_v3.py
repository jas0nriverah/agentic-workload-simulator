from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("shared_case_queue_history_v3.py")
SPEC = importlib.util.spec_from_file_location("shared_case_queue_history_v1_tested", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
queue_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = queue_module
SPEC.loader.exec_module(queue_module)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _descriptor(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha(path)}


class BindingRevisionTest(unittest.TestCase):
    def _fixture(self, temporary: Path, *, with_adapter: bool = True, finish: bool = True):
        inventory = temporary / "inventory.json"
        runtime_old = temporary / "runtime-old.json"
        runtime_new = temporary / "runtime-new.json"
        source_old = temporary / "source-old.json"
        source_new = temporary / "source-new.json"
        for path, payload in (
            (inventory, {"hardware": "fixture"}),
            (runtime_old, {"runtime": "v9"}),
            (runtime_new, {"runtime": "v10-ring8192"}),
            (source_old, {"source": "v9"}),
            (source_new, {"source": "v10-ring8192"}),
        ):
            path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        endpoint = {
            "endpoint_id": "endpoint-00",
            "api_base": "http://worker.test:18016/v1",
            "server_identity": "server-00",
        }

        def worker_manifest(path: Path, runtime: Path, source: Path) -> Path:
            value = {
                "schema_version": "assignment.worker-pool-manifest.v1",
                "workers": [{
                    "worker_id": "worker-00",
                    "enabled": True,
                    "endpoint": endpoint,
                    "inventory": _descriptor(inventory),
                    "runtime": _descriptor(runtime),
                    "source": _descriptor(source),
                }],
            }
            path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            return path

        old_manifest = worker_manifest(temporary / "workers-old.json", runtime_old, source_old)
        new_manifest = worker_manifest(temporary / "workers-new.json", runtime_new, source_new)
        old_adapter = temporary / "adapter-old.json"
        new_adapter = temporary / "adapter-new.json"
        if with_adapter:
            old_value = {
                "schema_version": queue_module.ADAPTER_MANIFEST_SCHEMA,
                "name": "old-adapter",
                "purpose": "fixture",
                "argv": ["--adapter-version", "old"],
            }
            new_value = {**old_value, "name": "new-adapter", "argv": ["--adapter-version", "new"]}
            old_adapter.write_text(json.dumps(old_value, indent=2) + "\n", encoding="utf-8")
            new_adapter.write_text(json.dumps(new_value, indent=2) + "\n", encoding="utf-8")
        queue_dir = temporary / "queue-old"
        artifact_root = temporary / "artifacts"
        kwargs = {
            "queue_dir": queue_dir,
            "cases": [{
                "schema_version": "assignment-production-v2-plan.v1",
                "resume_key": "fixture-case-00",
                "task": "fixture",
            }],
            "worker_ids": ["worker-00"],
            "artifact_root": artifact_root,
            "require_all_workers": False,
        }
        if with_adapter:
            kwargs["adapter_manifest_path"] = old_adapter
        queue = queue_module.SharedCaseQueue.create(**kwargs)
        queue.register_workers_manifest(old_manifest)
        lease = queue.claim_case("worker-00")
        assert lease is not None
        if finish:
            result = {
                "schema_version": "assignment-case-result.v1",
                "resume_key": lease.resume_key,
                "status": "completed",
                "evaluator": {"official_resolved": False},
            }
            queue.finish_attempt(lease, result=result)
            self.assertEqual(queue.audit_coverage()["status"], "pass")
        return {
            "queue": queue,
            "old_manifest": old_manifest,
            "new_manifest": new_manifest,
            "new_adapter": new_adapter if with_adapter else None,
            "old_adapter": old_adapter if with_adapter else None,
            "queue_dir": queue_dir,
            "artifact_root": artifact_root,
        }

    def test_migration_preserves_accepted_audit_and_rebinds_idle_worker(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw))
            destination = Path(raw) / "queue-new"
            receipt = queue_module.migrate_idle_queue(
                fixture["queue_dir"],
                destination,
                fixture["new_manifest"],
                migration_id="ring8192-test-1",
                review_note="fixture reviewed source/runtime binding revision",
                adapter_manifest_path=fixture["new_adapter"],
                adapter_manifest_sha256=_sha(fixture["new_adapter"]),
            )
            self.assertEqual(receipt["source_counts"], receipt["destination_counts"])
            self.assertTrue(receipt["accepted_rows_preserved"])
            migrated = queue_module.SharedCaseQueue(destination)
            self.assertEqual(migrated.audit_coverage()["status"], "pass")
            status = migrated.status()
            self.assertEqual(status["binding_revision"]["migration_id"], "ring8192-test-1")
            self.assertEqual(status["adapter_manifest"]["sha256"], _sha(fixture["new_adapter"]))
            self.assertEqual(
                status["binding_revision"]["previous_adapter_manifest"]["sha256"],
                _sha(fixture["old_adapter"]),
            )
            connection = sqlite3.connect(destination / "queue.sqlite3")
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM worker_binding_history").fetchone()[0], 1)
                old_hash = connection.execute(
                    "SELECT binding_sha256 FROM worker_binding_history WHERE worker_id = 'worker-00'"
                ).fetchone()[0]
                current_hash = connection.execute(
                    "SELECT binding_sha256 FROM workers WHERE worker_id = 'worker-00'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertNotEqual(old_hash, current_hash)

    def test_migration_refuses_held_attempt_without_creating_revision(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw), finish=False)
            with self.assertRaises(queue_module.ReconciliationRequired):
                queue_module.migrate_idle_queue(
                    fixture["queue_dir"],
                    Path(raw) / "queue-held-rejected",
                    fixture["new_manifest"],
                    migration_id="ring8192-held",
                    review_note="must reject while held",
                )

    def test_historical_binding_canonical_hash_is_required_for_audit(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = self._fixture(Path(raw))
            destination = Path(raw) / "queue-new"
            queue_module.migrate_idle_queue(
                fixture["queue_dir"],
                destination,
                fixture["new_manifest"],
                migration_id="ring8192-test-tamper",
                review_note="fixture reviewed source/runtime binding revision",
            )
            connection = sqlite3.connect(destination / "queue.sqlite3")
            try:
                connection.execute(
                    "UPDATE worker_binding_history SET binding_json = '{\"schema_version\":\"tampered\"}'"
                )
                connection.commit()
            finally:
                connection.close()
            result = queue_module.SharedCaseQueue(destination).audit_coverage()
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("binding" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
