import unittest

from agentic_sim.events import EventEnvelope


class EventEnvelopeTests(unittest.TestCase):
    def test_duration_is_derived(self):
        event = EventEnvelope(
            schema_version="0.dev",
            event_id="e1",
            run_id="r1",
            event_type="test",
            start_time_ns=10,
            end_time_ns=1_000_010,
            provenance="dev",
        )
        self.assertEqual(event.duration_ms, 1.0)
        self.assertIn('"duration_ms": 1.0', event.to_json())

    def test_negative_duration_is_rejected(self):
        with self.assertRaises(ValueError):
            EventEnvelope("0.dev", "e1", "r1", "test", 2, 1, "dev")

    def test_unknown_provenance_is_rejected(self):
        with self.assertRaises(ValueError):
            EventEnvelope("0.dev", "e1", "r1", "test", 1, 2, "unknown")
