"""Bounded contract tests for the Astropy-to-Django transfer helper."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("d9_cpu_transfer_test_module", HERE / "run_cpu_transfer.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class TransferContractTests(unittest.TestCase):
    def test_fixed_ordinal_prefix_stops_at_first_overflow(self) -> None:
        cases = [
            {"queue_ordinal": 18, "instance_id": "django__django-10914", "partition": "train_calibration", "raw_bytes": 311_068_000},
            {"queue_ordinal": 19, "instance_id": "django__django-10924", "partition": "train_calibration", "raw_bytes": 176_002_400},
            {"queue_ordinal": 20, "instance_id": "django__django-11001", "partition": "train_calibration", "raw_bytes": 239_921_600},
        ]
        with mock.patch.object(MODULE, "_valid_case", return_value=(True, "fully_valid_train_calibration")), mock.patch.object(
            MODULE.atomic, "prepare_case", side_effect=lambda case: dict(case)
        ):
            selected, audit = MODULE.select_django_cases(
                {"cases": cases}, max_raw_bytes=400_000_000, max_cases=4
            )

        self.assertEqual([case["queue_ordinal"] for case in selected], [18])
        self.assertEqual(audit["selected_raw_bytes"], 311_068_000)
        self.assertEqual(audit["stop_reason"], "next_valid_case_exceeds_raw_byte_bound")
        self.assertEqual(audit["considered"][-1]["queue_ordinal"], 19)


    def test_existing_candidate_families_keep_operation_fallback(self) -> None:
        model = {
            "global_median_ns": 10.0,
            "operation_medians_ns": {},
            "operation_requested_size_bucket_medians_ns": {},
            "operation_path_class_medians_ns": {},
        }
        event = {
            "operation": "unseen-operation",
            "requested_size_bucket": "entry_size_1_4KiB",
            "path_class": "entry_path_unknown",
        }
        for candidate in MODULE.atomic.MODEL_ORDER:
            self.assertEqual(MODULE._predict(model, candidate, event), 10.0)


    def test_bounded_result_integrity_and_fixed_model_order(self) -> None:
        artifact_path = HERE / "model.json"
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["models"], [
            "global_median",
            "operation_median",
            "operation_requested_size_bucket_median",
            "operation_path_class_median",
        ])
        self.assertEqual(artifact["selection"]["selected_count"], 1)
        self.assertLessEqual(artifact["selection"]["selected_raw_bytes"], 400_000_000)
        self.assertTrue(artifact["population"]["raw_hash_all_verified"])
        self.assertEqual(artifact["population"]["token_join_mismatch_count"], 0)
        self.assertEqual(artifact["population"]["range_mismatch_count"], 0)
        self.assertEqual(artifact["population"]["aggregate_drops"], {
            "censored_pending_count": 0,
            "event_callback_error_count": 0,
            "lost_event_records": 0,
            "lost_path_records": 0,
            "lost_pending_records": 0,
            "perf_lost_events": 0,
        })
        self.assertEqual(artifact["provenance"]["protected_label_files_opened"], [])
