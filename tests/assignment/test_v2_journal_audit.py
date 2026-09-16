import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from agentic_sim.telemetry.v2 import TelemetryV2
from scripts.assignment import sweagent_case_runner as runner

from scripts.validation.audit_v2_journals import audit
from agentic_sim.telemetry.features import build_model_features, build_tool_features


def span(name, phase, start_ms, end_ms, **values):
    base = {"schema_version": "assignment.telemetry.v2.lifecycle", "span_id": name,
            "run_id": "synthetic-run", "attempt_id": "attempt-001", "case_id": "synthetic-case",
            "clock": {"hostname": "fixture", "boot_id": "fixture-boot", "clock_id": "CLOCK_MONOTONIC_RAW"},
            "phase": phase, "availability": "measured", "start_mono_ns": start_ms * 1000000, **values}
    return [{**base, "event_id": name + "-start", "terminal": False, "event_kind": phase + "_start",
             "end_mono_ns": None, "duration_ms": None, "status": "pending"},
            {**base, "event_id": name + "-end", "terminal": True, "event_kind": "model_request" if phase == "model_request" else phase,
             "end_mono_ns": end_ms * 1000000, "duration_ms": end_ms-start_ms, "status": "success"}]


def fixture():
    tool = build_tool_features("git diff | head")
    model = build_model_features({"input_tokens": 20, "context_tokens": 20, "max_output_tokens": 2048})
    return (span("outer", "outer_swe_agent", 0, 1000) +
            span("tool", "tool_execution", 100, 200, action="git diff | head", action_id="action-1",
                 action_sha256=tool["action_sha256"], features=tool) +
            span("request", "model_request", 400, 600, request_id="request-1", logical_request_id="logical-1",
                 retry_index=0, retry_of=None, features=model))


class JournalAuditTests(unittest.TestCase):
    def test_case_runner_binds_disk_journal_identity_to_manifest(self):
        expected = {"run_id": "synthetic-run", "attempt_id": "attempt-001", "case_id": "synthetic-case"}
        for changed_field in (None, "run_id", "attempt_id", "case_id"):
            with self.subTest(changed_field=changed_field), TemporaryDirectory() as directory:
                root = Path(directory)
                rows = span("outer", "outer_swe_agent", 0, 1000)
                if changed_field is not None:
                    for row in rows:
                        row[changed_field] = "other-identity"
                # The journal remains internally valid even when all rows bind
                # to another attempt. The runner must compare its audited identity.
                self.assertEqual(audit(rows)["status"], "pass")
                (root / "lifecycle_events.jsonl").write_text(
                    "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
                for name in ("tool_events.jsonl", "model_events.jsonl", "hardware_snapshots.jsonl"):
                    (root / name).write_text("", encoding="utf-8")
                (root / "telemetry_manifest.json").write_text(json.dumps({
                    "schema_version": "assignment.telemetry.v2.manifest", **expected,
                    "request_payload_persisted": True, "raw_hardware_inventory": {},
                    "hardware_profile_sha256": "a" * 64,
                }), encoding="utf-8")
                (root / "linux_work").mkdir()
                (root / "linux_work/work_summary.json").write_text(json.dumps({
                    "schema_version": runner.BPF_WORK_SUMMARY_SCHEMA,
                }), encoding="utf-8")
                # Isolate unrelated hardware/BPF checks; disk parsing and the
                # journal auditor, manifest checks, and error classification are real.
                with patch.object(runner, "_audit_v2_snapshots", return_value={}), \
                     patch.object(runner, "_audit_bpf_work_evidence", return_value={"individual_operation_count": 1}), \
                     patch.object(runner, "_audit_bpf_service_artifacts", return_value={}):
                    kwargs = dict(telemetry_dir=root, expected_identity=expected,
                                  telemetry_config={"mode": "v2", "remote_hardware_profile": {"sha256": "a" * 64}})
                    if changed_field is None:
                        result = runner._audit_v2_evidence(**kwargs)
                        self.assertEqual(result["summary"]["status"], "pass")
                    else:
                        with self.assertRaisesRegex(runner.EvidenceIntegrityError, f"journal {changed_field}.*not bound"):
                            runner._audit_v2_evidence(**kwargs)

    def test_disjoint_unknown_intervals_are_actual_complement(self):
        result = audit(fixture())
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["attributed_union_ms"], 300)
        self.assertEqual(result["unknown_intervals_mono_ns"], [[0, 100000000], [200000000, 400000000], [600000000, 1000000000]])
        self.assertIsNone(result["request_mutations"])

    def test_nested_phases_do_not_double_count(self):
        rows = fixture() + span("setup", "setup", 50, 250)
        result = audit(rows)
        self.assertEqual(result["attributed_union_ms"], 400)
        self.assertEqual(result["unknown_wall_ms"], 600)

    def test_missing_terminal_or_duplicate_identity_fails(self):
        rows = fixture()
        self.assertEqual(audit(rows[:-1])["status"], "fail")
        self.assertEqual(audit(rows + [copy.deepcopy(rows[-1])])["status"], "fail")

    def test_cross_boot_timestamps_cannot_be_unioned(self):
        rows = fixture()
        rows[-1]["clock"] = {**rows[-1]["clock"], "boot_id": "other-boot"}
        with self.assertRaises(ValueError):
            audit(rows)

    def test_missing_identity_and_nonfinite_duration_fail_closed(self):
        rows = fixture()
        for row in rows:
            row["run_id"] = None
        with self.assertRaises(ValueError):
            audit(rows)
        for invalid in (float("nan"), float("inf"), True):
            rows = fixture()
            rows[-1]["duration_ms"] = invalid
            self.assertEqual(audit(rows)["status"], "fail")

    def test_actual_journal_rows_include_intents_and_failure_markers(self):
        with TemporaryDirectory() as directory:
            recorder = TelemetryV2(directory, run_id="synthetic-run", case_id="synthetic-case")
            recorder.start_outer(start_mono_ns=100)
            recorder.record_tool_intent("git diff", start_mono_ns=110)
            tool = recorder.begin_tool("git diff", start_mono_ns=120)
            recorder.end_tool(tool, status="timeout", end_mono_ns=140)
            request = recorder.begin_request({"max_output_tokens": 2048, "temperature": 0, "model": "fixture"}, start_mono_ns=150)
            recorder.end_request(request, status="failure", end_mono_ns=180)
            recorder.finish_outer(end_mono_ns=200)
            recorder.reconcile_e2e()
            result = audit(recorder.rows())
            self.assertEqual(result["status"], "pass", result)
            self.assertEqual(result["tool_events"], 1)
            self.assertEqual(result["physical_requests"], 1)

    def test_terminal_identity_change_and_feature_mutation_fail(self):
        rows = fixture()
        rows[-1]["request_id"] = "different-request"
        self.assertEqual(audit(rows)["status"], "fail")
        rows = fixture()
        rows[2]["features"]["operation_class"] = "invented"
        self.assertGreater(audit(rows)["feature_parity_mismatches"], 0)

    def test_retry_cannot_link_to_an_unrelated_request(self):
        rows = fixture()
        retry = span("retry", "model_request", 650, 700, request_id="request-2",
                     logical_request_id="different-logical-call", retry_index=1,
                     retry_of="request-1", features=copy.deepcopy(rows[-1]["features"]))
        self.assertEqual(audit(rows + retry)["status"], "fail")
        for row in retry:
            row["logical_request_id"] = "logical-1"
        self.assertEqual(audit(rows + retry)["status"], "pass")

    def test_physical_framework_commands_use_their_exact_contract_not_tool_features(self):
        with TemporaryDirectory() as directory:
            recorder = TelemetryV2(directory, run_id="synthetic-run", case_id="synthetic-case")
            recorder.start_outer(start_mono_ns=100)
            for index, command in enumerate(("export LANG=C.UTF-8", "", "git status")):
                command_span = recorder.begin_runtime_command(command, phase="tool_execution",
                                                               start_mono_ns=110 + index * 20)
                command_span.finish(end_mono_ns=120 + index * 20)
            recorder.finish_outer(end_mono_ns=200)
            result = audit(recorder.rows())
            self.assertEqual(result["status"], "pass", result)
            self.assertEqual(result["runtime_commands"], 3)
            self.assertEqual(result["tool_events"], 0)
            for mutation in ("runtime_command", "runtime_command_sha256", "cpu_action_required"):
                rows = copy.deepcopy(recorder.rows())
                terminal = next(row for row in rows if row.get("event_kind") == "runtime_command")
                terminal[mutation] = "changed"
                self.assertEqual(audit(rows)["status"], "fail")

    def test_missing_semantic_tool_features_still_fail(self):
        rows = fixture()
        rows[2].pop("features")
        rows[3].pop("features")
        self.assertGreater(audit(rows)["feature_parity_mismatches"], 0)


if __name__ == "__main__":
    unittest.main()
