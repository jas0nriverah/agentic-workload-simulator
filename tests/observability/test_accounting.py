import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.observability.accounting import AccountingError, summarize_interval_union


class IntervalAccountingTests(unittest.TestCase):
    def _row(self, event_id, start, end, host="host-a", boot="boot-a"):
        return {
            "event_id": event_id,
            "event_type": "model_request",
            "start_mono_ns": start,
            "end_mono_ns": end,
            "clock": {"clock_id": "CLOCK_MONOTONIC_RAW", "hostname": host, "boot_id": boot},
        }

    def test_union_reports_overlap_gap_and_coverage(self):
        result = summarize_interval_union([
            self._row("a", 10, 30),
            self._row("b", 20, 40),
            self._row("c", 50, 60),
        ])
        self.assertEqual(result["status"], "derived")
        self.assertEqual(result["raw_duration_ns"], 50)
        self.assertEqual(result["union_duration_ns"], 40)
        self.assertEqual(result["overlap_duration_ns"], 10)
        self.assertEqual(result["span_duration_ns"], 50)
        self.assertEqual(result["gap_duration_ns"], 10)
        self.assertEqual(result["merged_interval_count"], 2)
        self.assertFalse(result["gpu_time_claims"])

    def test_cross_host_or_boot_merge_fails_closed(self):
        with self.assertRaisesRegex(AccountingError, "clock/host/boot"):
            summarize_interval_union([self._row("a", 1, 2), self._row("b", 2, 3, host="host-b")])

    def test_missing_or_reversed_intervals_fail_closed(self):
        with self.assertRaisesRegex(AccountingError, "no nested clock identity"):
            summarize_interval_union([{"start_mono_ns": 1, "end_mono_ns": 2}])
        with self.assertRaisesRegex(AccountingError, "ends before"):
            summarize_interval_union([self._row("a", 3, 2)])

    def test_empty_stream_is_explicitly_unavailable(self):
        result = summarize_interval_union([])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["provenance"], "unavailable")

    def test_script_shape_is_jsonl_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps(self._row("a", 1, 2)) + "\n", encoding="utf-8")
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
