import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE = Path(__file__).with_name("calibrate_repaired_d9.py")
SPEC = importlib.util.spec_from_file_location("repaired_calibration", MODULE)
calibration = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(calibration)
LEDGER_MODULE = Path(__file__).parents[1] / "ledger" / "validate_ledger.py"
LEDGER_SPEC = importlib.util.spec_from_file_location("repaired_ledger", LEDGER_MODULE)
ledger = importlib.util.module_from_spec(LEDGER_SPEC)
assert LEDGER_SPEC.loader is not None
LEDGER_SPEC.loader.exec_module(ledger)


class RepairedD9CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.proof = self.root / "proof"
        self.proof.mkdir()
        self.split = self.root / "production_split_manifest.v2.json"
        self.original_proof = calibration.PROOF_ROOT
        self.original_split = calibration.PINNED_SPLIT_MANIFEST
        self.original_case_roots = calibration.CASE_ROOTS_ROOT
        self.original_split_sha = calibration.PINNED_SPLIT_SHA256
        calibration.PROOF_ROOT = self.proof
        calibration.CASE_ROOTS_ROOT = self.proof
        calibration.PINNED_SPLIT_MANIFEST = self.split
        self.train = [f"train-{number}" for number in range(8)]
        clusters = [
            {"instance_id": instance, "partition": "train_calibration"}
            for instance in self.train
        ]
        clusters.extend(
            {"instance_id": f"extra-train-{number}", "partition": "train_calibration"}
            for number in range(538)
        )
        clusters.append({"instance_id": "confirmation-only", "partition": "confirmation_development_excluded"})
        clusters.extend(
            {"instance_id": f"held-{number}", "partition": "final_evaluation"}
            for number in range(160)
        )
        self.assertEqual(len(clusters), 707)
        self.split.write_text(json.dumps({"schema_version": "assignment-production-instance-cluster-split.v2", "clusters": clusters}))
        calibration.PINNED_SPLIT_SHA256 = calibration._sha256(self.split)

    def tearDown(self):
        calibration.PROOF_ROOT = self.original_proof
        calibration.PINNED_SPLIT_MANIFEST = self.original_split
        calibration.CASE_ROOTS_ROOT = self.original_case_roots
        calibration.PINNED_SPLIT_SHA256 = self.original_split_sha
        self.temp.cleanup()

    def _case(self, instance, case_id, *, malformed_feature=False, include_events=True):
        directory = self.proof / case_id
        directory.mkdir()
        (directory / "case_spec.json").write_text(json.dumps({"instance_id": instance, "case_id": case_id}))
        pipeline = self.root / "pipeline-output"
        pipeline.mkdir(exist_ok=True)
        validation_path = pipeline / f"{case_id}-validation.json"
        validation_path.write_text(json.dumps({"validation": {"status": "valid"}, "source_hashes": ["abc"]}))
        events_path = pipeline / f"{case_id}-events.jsonl"
        if include_events:
            events = []
            events.append({"record_role": "OBSERVATION", "features": None, "observed_ms": None})
            for ordinal in range(10):
                request = f"request-{instance}-{ordinal}"
                for component, value in (("queue", 0), ("prefill", 2), ("decode", 4)):
                    features = {"request_kind": "completion", "prompt_tokens_bucket": "small"}
                    if malformed_feature:
                        features["future_state"] = "completed"
                    events.append(
                        {
                            "record_role": "TARGET",
                            "instance_id": instance, "case_id": case_id, "attempt_id": "attempt-1",
                            "event_id": f"{instance}-{ordinal}-{component}", "event_class": f"native:{component}",
                            "target_boundary": f"native_per_request_{component}", "observed_ms": value,
                            "features": features, "feature_provenance": {"hardware_fingerprint": "fp-a"},
                            "host_id": "host-one", "clock_id": "mono", "physical_request_id": request,
                            "partition": "train_calibration", "model_eligible": True,
                        }
                    )
            events_path.write_text("".join(json.dumps(row) + "\n" for row in events))
        return {
            "case_root": str(directory), "instance_id": instance, "case_id": case_id,
            # This caller assertion is intentionally wrong; the adapter must
            # use the pinned split instead.
            "partition": "train_calibration", "events_path": str(events_path),
            "validation_report_path": str(validation_path),
            "events_sha256": calibration._sha256(events_path) if include_events else "0" * 64,
            "validation_report_sha256": calibration._sha256(validation_path),
        }

    def _ledger_case(self, instance, case_id):
        """Create one minimal valid raw case and run the real ledger adapter."""
        root = self.proof / case_id
        attempt = root / "runner_attempts" / "attempt-001"
        (attempt / "telemetry_v2" / "linux_work").mkdir(parents=True)
        (attempt / "native_serving").mkdir()
        identity = {"instance_id": instance, "case_id": case_id, "attempt_id": "attempt-001"}
        (root / "case_spec.json").write_text(json.dumps(identity))
        clock = {"hostname": "ledger-host", "clock_id": "mono", "boot_id": "boot"}

        def row(event_id, kind, terminal, start, end, **extra):
            return {**identity, "event_id": event_id, "event_kind": kind, "terminal": terminal,
                    "span_id": "span-" + kind.removesuffix("_start"), "start_mono_ns": start,
                    "end_mono_ns": end, "clock": clock, **extra}

        model = [
            row("model-start", "model_request_start", False, 1, None, physical_request_id="request-1",
                features={"mode": "prospective", "input_tokens": 3}),
            row("model-terminal", "model_request", True, 1, 3, physical_request_id="request-1"),
        ]
        tool = [row("tool-start", "tool_event_start", False, 3, None, physical_request_id="tool-1"), row("tool-terminal", "tool_event", True, 3, 5)]
        lifecycle = [
            row("outer-start", "outer_swe_agent_start", False, 0, None, physical_request_id="outer-1"),
            row("outer-terminal", "outer_swe_agent", True, 0, 10),
            row("runtime-start", "runtime_command_start", False, 5, None, physical_request_id="runtime-1"),
            row("runtime-terminal", "runtime_command", True, 5, 8),
        ]
        native = [{"physical_request_id": "request-1", "target_request": {"case_id": case_id, "attempt_id": "attempt-001", "started_monotonic_ns": 1, "terminal_monotonic_ns": 3}, "clock": clock,
                   "metrics": {name: {"value_ms": 1} for name in ("queue", "prefill", "decode", "e2e")}}]
        for filename, rows in (("model_events.jsonl", model), ("tool_events.jsonl", tool), ("lifecycle_events.jsonl", lifecycle)):
            (attempt / "telemetry_v2" / filename).write_text("".join(json.dumps(value) + "\n" for value in rows))
        (attempt / "native_serving" / "native_attribution.jsonl").write_text(json.dumps(native[0]) + "\n")
        work = attempt / "telemetry_v2" / "linux_work"
        (work / "raw_events.bin").write_bytes(b"0" * 8)
        (work / "raw_aggregates.jsonl").write_text("{}\n")
        (work / "bpf_collector_manifest.json").write_text(json.dumps({"record_size_bytes": 1}))
        (work / "work_summary.json").write_text(json.dumps({"actions": [{"raw": {"event_records_complete": True, "binary_event_stream": {"offset_start": 0, "offset_end": 1, "record_count": 1}}}]}))
        output = self.root / "ledger-output" / case_id
        report = ledger.validate_case(root, output)
        self.assertEqual(report["validation"]["status"], "valid")
        return {
            "case_root": str(root), "instance_id": instance, "case_id": case_id,
            "events_path": report["case_event_records"]["path"], "validation_report_path": report["validation_report_path"],
            "events_sha256": calibration._sha256(Path(report["case_event_records"]["path"])),
            "validation_report_sha256": calibration._sha256(Path(report["validation_report_path"])),
        }

    def test_fits_grouped_independent_paths_and_excludes_confirmation_before_open(self):
        cases = [self._case(instance, f"case-{instance}") for instance in self.train]
        # No events file exists for this confirmation case.  A successful run
        # proves the identity/partition gate happens before event-file access.
        cases.append(self._case("confirmation-only", "confirmation-case", include_events=False))
        report = calibration.calibrate({"cases": cases}, self.root / "output")
        self.assertEqual(report["disposition"], "fitted_with_supported_paths")
        self.assertEqual(len(report["excluded_before_events_open"]), 1)
        self.assertEqual(report["excluded_before_events_open"][0]["instance_id"], "confirmation-only")
        self.assertTrue(all(row["grouped_folds"]["test_event_count"] > 0 for row in report["model_paths"] if row["status"] == "fitted"))
        self.assertEqual(report["native_component_sum_oof"]["status"], "supported_native_component_sum_queue_prefill_decode")
        self.assertEqual(report["outer_e2e_oof"]["status"], "unsupported_no_separate_outer_e2e_target_model_path")
        # Queue targets are zero.  Predictions remain zero and therefore pass
        # without epsilon smoothing or a fabricated finite APE.
        queue = next(row for row in report["model_paths"] if row["event_class"] == "native:queue")
        self.assertEqual(queue["oof_metrics"]["zero_target_count"], 80)
        self.assertEqual(queue["oof_metrics"]["infinite_ape_count"], 0)
        self.assertEqual(queue["oof_metrics"]["max_ape_pct"], 0.0)
        self.assertEqual(queue["oof_metrics"]["max_ape_status"], "finite")

    def test_rejects_future_feature_without_partial_fit(self):
        cases = [self._case(instance, f"case-{instance}", malformed_feature=True) for instance in self.train]
        report = calibration.calibrate({"cases": cases}, self.root / "output")
        self.assertEqual(report["disposition"], "pending_no_fit")
        self.assertEqual(report["pending_reason"], "eligible_input_rejected; no partial fit after contract error")
        self.assertIn("future_state", report["input_errors"][0]["error"])

    def test_confirmation_only_is_honestly_pending(self):
        case = self._case("confirmation-only", "confirmation-case", include_events=False)
        report = calibration.calibrate({"cases": [case]}, self.root / "output")
        self.assertEqual(report["disposition"], "pending_no_fit")
        self.assertIn("confirmation/excluded", report["pending_reason"])
        self.assertEqual(report["excluded_before_events_open"][0]["derived_partition"], "confirmation_development_excluded")

    def test_real_ledger_schema_is_accepted_but_native_unknown_hardware_remains_unfitted(self):
        cases = [self._ledger_case(instance, f"ledger-{instance}") for instance in self.train[:3]]
        rows, roles = calibration._read_eligible_events(
            Path(cases[0]["events_path"]), cases[0]["instance_id"], cases[0]["case_id"]
        )
        self.assertEqual(roles["TARGET"], 7)
        native = next(row for row in rows if row["event_class"] == "native:queue")
        self.assertEqual(native["features"], {"request_kind": "model_request"})
        self.assertEqual(native["hardware_domain"], "profile_domain_unknown")
        report = calibration.calibrate({"cases": cases}, self.root / "calibration-output")
        self.assertEqual(report["disposition"], "pending_no_fit")
        self.assertFalse(report["input_errors"])
        self.assertEqual(report["event_count"], 21)
        self.assertTrue(all(path["status"] == "unsupported_insufficient_independent_support" for path in report["model_paths"]))

    def test_split_sha_is_pinned_before_case_inventory(self):
        calibration.PINNED_SPLIT_SHA256 = "f" * 64
        with self.assertRaisesRegex(calibration.ContractError, "SHA-256 differs"):
            calibration.inventory([])


if __name__ == "__main__":
    unittest.main()
