import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).parents[1] / "validate_ledger.py"
SPEC = importlib.util.spec_from_file_location("ledger", MODULE)
ledger = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ledger)


class LedgerNegativeFixtures(unittest.TestCase):
    """Small complete journals, each with one deterministic corruption."""

    def build(self, mutation=None):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name) / "case"
        attempt = root / "runner_attempts/attempt-001"
        (attempt / "telemetry_v2/linux_work").mkdir(parents=True)
        (attempt / "native_serving").mkdir()
        identity = {"instance_id": "i", "case_id": "c", "attempt_id": "attempt-001"}
        (root / "case_spec.json").write_text(json.dumps({**identity, "namespace": "confirmation"}))
        clock = {"hostname": "h", "clock_id": "mono", "boot_id": "b"}
        def row(eid, kind, terminal, start, end, **more): return {**identity, "event_id": eid, "event_kind": kind, "terminal": terminal, "span_id": "s-" + kind.removesuffix("_start"), "start_mono_ns": start, "end_mono_ns": end, "clock": clock, **more}
        model = [row("mstart", "model_request_start", False, 1, None, physical_request_id="p", features={"mode":"prospective", "input_tokens":3}), row("mend", "model_request", True, 1, 3, physical_request_id="p")]
        tool = [row("tstart", "tool_event_start", False, 3, None), row("tend", "tool_event", True, 3, 5)]
        life = [row("ostart", "outer_swe_agent_start", False, 0, None), row("outer", "outer_swe_agent", True, 0, 10), row("rstart", "runtime_command_start", False, 5, None), row("rend", "runtime_command", True, 5, 8)]
        native = [{"physical_request_id":"p", "target_request":{"case_id":"c", "attempt_id":"attempt-001", "started_monotonic_ns":1, "terminal_monotonic_ns":3}, "clock":clock, "metrics":{k:{"value_ms":1} for k in ("queue","prefill","decode","e2e")} }]
        if mutation: mutation(model, tool, life, native)
        for name, rows in (("model_events.jsonl",model),("tool_events.jsonl",tool),("lifecycle_events.jsonl",life)):
            (attempt / "telemetry_v2" / name).write_text("".join(json.dumps(x)+"\n" for x in rows))
        (attempt / "native_serving/native_attribution.jsonl").write_text(json.dumps(native[0])+"\n")
        work = attempt / "telemetry_v2/linux_work"
        (work / "raw_events.bin").write_bytes(b"0" * 8)
        (work / "raw_aggregates.jsonl").write_text("{}\n")
        (work / "bpf_collector_manifest.json").write_text(json.dumps({"record_size_bytes": 1}))
        (work / "work_summary.json").write_text(json.dumps({"actions": [{"raw": {"event_records_complete": True, "binary_event_stream": {"offset_start": 0, "offset_end": 1, "record_count": 1}}}]}))
        return tmp, root

    def assert_code(self, mutation, code):
        tmp, root = self.build(mutation)
        with tmp:
            report = ledger.validate_case(root, root / "out")
        self.assertIn(code, {error["code"] for error in report["validation"]["errors"]})

    def test_clean_baseline(self):
        tmp, root = self.build()
        with tmp:
            self.assertEqual(ledger.validate_case(root, root / "out")["validation"]["status"], "valid")
    def test_duplicate_physical(self): self.assert_code(lambda m,t,l,n: m.append(dict(m[0], event_id="mstart2")), "duplicate_physical_request_id")
    def test_missing_terminal(self): self.assert_code(lambda m,t,l,n: m.pop(), "physical_native_bijection_failure")
    def test_cross_clock_interval(self): self.assert_code(lambda m,t,l,n: l.__setitem__(3, dict(l[3], clock={"hostname":"other", "clock_id":"mono", "boot_id":"b"})), "paired_span_cross_clock_domain")
    def test_leaf_overlap(self): self.assert_code(lambda m,t,l,n: l.extend([dict(l[3], event_id="l1", lifecycle_leaf=True, start_mono_ns=5, end_mono_ns=8), dict(l[3], event_id="l2", lifecycle_leaf=True, start_mono_ns=7, end_mono_ns=9)]), "lifecycle_leaf_overlap")
    def test_identity_mutation(self): self.assert_code(lambda m,t,l,n: t.__setitem__(0, dict(t[0], case_id="wrong")), "identity_mismatch")
    def test_missing_all_tool_starts(self): self.assert_code(lambda m,t,l,n: t.pop(0), "orphan_terminal")
    def test_start_timestamp_mismatch(self): self.assert_code(lambda m,t,l,n: t[1].update(start_mono_ns=4), "paired_span_start_timestamp_mismatch")
    def test_missing_native_phase(self): self.assert_code(lambda m,t,l,n: n[0]["metrics"].pop("decode"), "invalid_or_missing_native_metric")
    def test_invalid_native_phase(self):
        for value in (-1, float('nan'), float('inf'), True):
            self.assert_code(lambda m,t,l,n: n[0]["metrics"]["decode"].update(value_ms=value), "invalid_or_missing_native_metric")
    def test_incidental_attempt_mention_is_not_binding(self):
        tmp, root = self.build()
        with tmp:
            (root / 'runner_attempts/attempt-002/telemetry_v2').mkdir(parents=True)
            (root / 'case_result.json').write_text(json.dumps({'error': 'debug attempt-001'}))
            with self.assertRaises(ledger.LedgerError):
                ledger.validate_case(root, root / 'out')


if __name__ == "__main__": unittest.main()
