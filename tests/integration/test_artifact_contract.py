import json
import tempfile
import unittest

from agentic_sim.artifacts import append_jsonl, attempt_layout, inventory, validate_artifacts
from agentic_sim.artifacts.contract import counter_state, initialize_attempt


class ArtifactContractTests(unittest.TestCase):
    def test_initialize_is_explicit_and_append_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = attempt_layout(tmp, "e1", "lite", "i1")
            initialize_attempt(layout, {"schema_version": "cr6.run-config.v1", "mode": "thin-telemetry"})
            append_jsonl(layout.path("events.jsonl"), {"event_type": "run_start", "authorization": "secret"})
            append_jsonl(layout.path("events.jsonl"), {"event_type": "run_end"})
            self.assertEqual(len(layout.path("events.jsonl").read_text().splitlines()), 2)
            self.assertNotIn("secret", layout.path("events.jsonl").read_text())
            report = validate_artifacts(layout)
            self.assertFalse(report["complete"])
            self.assertFalse(layout.path("counters.parquet").exists())
            self.assertEqual(json.loads(layout.path("counters.unavailable.json").read_text())["status"], "unavailable")
            self.assertEqual(counter_state(layout)["state"], "unavailable")
            self.assertNotIn("counters.parquet", inventory(layout))
            self.assertEqual(inventory(layout)["counters"]["logical_state"], "unavailable")

    def test_legacy_json_in_parquet_is_read_only_legacy_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = attempt_layout(tmp, "e1", "lite", "i1")
            initialize_attempt(layout, {"schema_version": "cr6.run-config.v1"})
            layout.path("counters.unavailable.json").unlink()
            layout.path("counters.parquet").write_text(json.dumps({"status": "unavailable", "provenance": "unavailable"}) + "\n")
            before = layout.path("counters.parquet").read_bytes()
            self.assertEqual(counter_state(layout)["state"], "legacy_unavailable")
            report = validate_artifacts(layout)
            self.assertEqual(report["counter_state"]["state"], "legacy_unavailable")
            self.assertEqual(layout.path("counters.parquet").read_bytes(), before)

    def test_two_counter_states_are_a_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            layout = attempt_layout(tmp, "e1", "lite", "i1")
            initialize_attempt(layout, {"schema_version": "cr6.run-config.v1"})
            layout.path("counters.parquet").write_bytes(b"not-parquet")
            report = validate_artifacts(layout)
            self.assertEqual(report["counter_state"]["state"], "conflict")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            attempt_layout("/tmp", "e", "lite", "../escape")


if __name__ == "__main__":
    unittest.main()
