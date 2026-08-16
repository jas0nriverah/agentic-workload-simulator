import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.artifacts import append_jsonl, attempt_layout, validate_artifacts
from agentic_sim.artifacts.contract import initialize_attempt


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
            self.assertEqual(json.loads(layout.path("counters.parquet").read_text())["status"], "unavailable")

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            attempt_layout("/tmp", "e", "lite", "../escape")


if __name__ == "__main__":
    unittest.main()
