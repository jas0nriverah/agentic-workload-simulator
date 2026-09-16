from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import patch

from agentic_sim.telemetry.process_resources import FIELDS, capture_process_resources
from agentic_sim.telemetry.features import canonical_json, tool_model_vector
from agentic_sim.telemetry.v2 import TelemetryV2
from scripts.validation.generate_v2_fixtures import _normalise_journals


class ProcessResourceTests(unittest.TestCase):
    def test_native_sample_has_real_identity_and_independent_clock_bracket(self):
        value = capture_process_resources()
        self.assertEqual(value["availability"], "measured")
        self.assertEqual(value["pid"], os.getpid())
        self.assertGreater(value["process_start_ticks"], 0)
        self.assertLessEqual(value["sample_start_mono_ns"], value["sample_end_mono_ns"])
        self.assertEqual(set(value["counters"]), set(FIELDS))
        self.assertIn("not_bytes", value["units"]["ru_inblock"])
        self.assertEqual(canonical_json(value), canonical_json(json.loads(canonical_json(value))))

    def test_failed_or_invalid_sample_is_unavailable_not_zero_work(self):
        with patch("agentic_sim.telemetry.process_resources.resource.getrusage", side_effect=OSError("denied")):
            value = capture_process_resources()
        self.assertEqual(value["availability"], "unavailable")
        self.assertTrue(all(v is None for v in value["counters"].values()))
        for invalid in (float("nan"), -1, True):
            fake = SimpleNamespace(**{k: 0 for k in FIELDS})
            fake.ru_utime = invalid
            with self.subTest(invalid=invalid), patch("agentic_sim.telemetry.process_resources.resource.getrusage", return_value=fake):
                self.assertEqual(capture_process_resources()["availability"], "unavailable")

    def test_resources_are_retained_outside_prospective_features(self):
        with TemporaryDirectory() as directory:
            recorder = TelemetryV2(directory, run_id="resource-fixture")
            span = recorder.begin_tool("git diff", start_mono_ns=100)
            row = recorder.end_tool(span, end_mono_ns=200)
            self.assertEqual(row["feature_vector"], tool_model_vector(row["features"]))
            self.assertNotIn("process_resources_at_record", row["features"])
            self.assertNotIn("process_resources_at_record", row["feature_vector"])
            rows = [json.loads(x) for x in (Path(directory)/"tool_events.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(x["process_resources_at_record"]["availability"] == "measured" for x in rows))
            self.assertGreater(rows[0]["process_resources_at_record"]["sample_start_mono_ns"], 200)
            # Explicitly synthetic fixtures retain no accidentally native
            # counters and remain deterministic after normalization.
            _normalise_journals(Path(directory))
            normalized = [json.loads(x) for x in (Path(directory)/"tool_events.jsonl").read_text().splitlines()]
            self.assertTrue(all(x["process_resources_at_record"]["availability"] == "unavailable" for x in normalized))


if __name__ == "__main__":
    unittest.main()
