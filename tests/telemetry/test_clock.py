import json
import subprocess
import sys
import unittest

from agentic_sim.telemetry.clock import clock_id, clock_metadata, monotonic_ns


class ClockTests(unittest.TestCase):
    def test_clock_identity_and_sample_are_consistent(self):
        first = monotonic_ns()
        second = monotonic_ns()
        self.assertGreaterEqual(second, first)
        metadata = clock_metadata()
        self.assertIn(metadata["clock_id"], {"CLOCK_MONOTONIC_RAW", "CLOCK_MONOTONIC"})
        self.assertEqual(metadata["clock_id"], clock_id())
        self.assertEqual(metadata["clock_source"], "time.clock_gettime_ns")
        self.assertTrue(metadata["hostname"])
        self.assertIn("boot_id", metadata)

    def test_cli_emits_same_metadata_for_shell_consumers(self):
        result = subprocess.run([sys.executable, "-m", "agentic_sim.telemetry.clock", "--sample"], capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value["schema_version"], "telemetry.clock.v1")
        self.assertEqual(value["clock_id"], clock_id())
        self.assertIsInstance(value["monotonic_ns"], int)


if __name__ == "__main__":
    unittest.main()
