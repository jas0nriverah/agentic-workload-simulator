import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agentic_sim.observability.memory import estimate_memory, estimate_weight_bytes
from agentic_sim.observability.nvtx import capability, range as nvtx_range
from agentic_sim.observability.overhead import PairingError, summarize_overhead_records
from agentic_sim.observability.perfetto import export_perfetto_trace


class OverheadTests(unittest.TestCase):
    def _row(self, run_id, level, profilers, duration):
        return {
            "run_id": run_id,
            "config_hash": "same-config",
            "observability_level": level,
            "profilers_enabled": profilers,
            "instrumentation_version": "obs-1",
            "metrics": {"e2e_ms": duration},
        }

    def test_repeated_pairs_include_median_p90_and_percentage(self):
        result = summarize_overhead_records(
            [self._row("control-1", "control", [], 100), self._row("control-2", "control", [], 110)],
            [self._row("thin-1", "thin", ["prometheus"], 120), self._row("thin-2", "thin", ["prometheus"], 130)],
            profile_kind="thin",
        )
        metric = result["metrics"]["e2e_ms"]
        self.assertEqual(result["schema_version"], "observability.profiling-overhead.v1")
        self.assertEqual(metric["control"]["count"], 2)
        self.assertEqual(metric["profile"]["median"], 125.0)
        self.assertAlmostEqual(metric["overhead_pct"]["median"], (125.0 - 105.0) / 105.0 * 100.0)
        self.assertEqual(result["observability"]["profile"]["run_count"], 2)

    def test_gpu_claims_and_mixed_metadata_are_rejected(self):
        control = self._row("control", "control", [], 1)
        profile = self._row("profile", "thin", ["prometheus"], 2)
        control["metrics"] = {"gpu_time_ms": 1}
        profile["metrics"] = {"gpu_time_ms": 2}
        with self.assertRaises(PairingError):
            summarize_overhead_records([control], [profile])
        profile["metrics"] = {"e2e_ms": 2}
        profile_two = self._row("profile-2", "thin", ["prometheus"], 3)
        profile_two["instrumentation_version"] = "obs-2"
        with self.assertRaises(PairingError):
            summarize_overhead_records([self._row("control", "control", [], 1)], [profile, profile_two])


class MemoryTests(unittest.TestCase):
    def test_explicit_moe_memory_estimate_is_estimated_not_measured(self):
        result = estimate_memory(
            model_revision="model-rev",
            precision="bf16",
            parameter_count=30_000_000_000,
            context_length=2048,
            num_layers=48,
            num_kv_heads=8,
            head_dim=128,
            batch_size=1,
        )
        self.assertEqual(result["provenance"], "estimated")
        self.assertGreater(result["estimated_peak_bytes"], result["components"]["weights_bytes"])
        self.assertTrue(result["actual_fit_authoritative"])

    def test_weight_only_missing_precision_is_unavailable(self):
        result = estimate_weight_bytes(parameter_count=100)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["provenance"], "unavailable")


class TraceAndNvtxTests(unittest.TestCase):
    def test_perfetto_trace_is_deterministic_and_does_not_copy_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "events.jsonl"
            output = Path(tmp) / "trace.json"
            events.write_text(
                json.dumps({
                    "event_type": "model_request", "run_id": "r", "attempt_id": "a",
                    "start_mono_ns": 1_000_000, "end_mono_ns": 3_000_000,
                    "request_id": "req-1", "payload": {"prompt": "secret"},
                    "provenance": "measured",
                }) + "\n",
                encoding="utf-8",
            )
            trace = export_perfetto_trace(events, output_path=output, run_id="r", attempt_id="a")
            self.assertEqual(trace["schema_version"], "observability.trace.perfetto.v1")
            self.assertEqual(trace["traceEvents"][0]["ts"], 0.0)
            self.assertNotIn("secret", output.read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                export_perfetto_trace(events, output_path=output)

    def test_nvtx_is_noop_unless_explicitly_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(capability()["status"], "disabled")
            with nvtx_range("agent_step", category="agent"):
                pass


if __name__ == "__main__":
    unittest.main()
