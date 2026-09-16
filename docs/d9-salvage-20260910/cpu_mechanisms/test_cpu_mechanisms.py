#!/usr/bin/env python3
"""Focused correctness tests for the bounded CPU mechanism comparison."""
from __future__ import annotations

from array import array
from pathlib import Path
import sys
import unittest


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_cpu_mechanisms as model  # noqa: E402


class CpuMechanismTests(unittest.TestCase):
    def test_entry_features_ignore_result_fields(self) -> None:
        row = {
            "syscall_nr": 0,
            "kind_name": "read",
            "scalar_args": {"syscall_name": "read", "requested_size": 4096},
            "path_status_name": "observed",
            "path": "/tmp/input",
            "ret": 4096,
            "status_name": "success",
            "duration_ns": 1234,
        }
        features = (
            model.atomic.operation_key(row),
            model.atomic.requested_size_bucket(
                model.atomic.requested_size_value(row)
            ),
            model.atomic.path_class(row),
        )
        changed = dict(row)
        changed.update(ret=-1, status_name="failure", duration_ns=999999999)
        self.assertEqual(
            features,
            (
                model.atomic.operation_key(changed),
                model.atomic.requested_size_bucket(
                    model.atomic.requested_size_value(changed)
                ),
                model.atomic.path_class(changed),
            ),
        )

    def test_coverage_representative_uses_joint_interval_not_observed_only(self) -> None:
        # Both targets can be covered by p in [120, 125], even though no
        # observed target is itself in that interval.
        prediction = model._coverage_representative([100, 160])
        self.assertGreaterEqual(prediction, 120.0)
        self.assertLessEqual(prediction, 125.0)
        self.assertLessEqual(abs(prediction - 100.0) / 100.0, 0.25)
        self.assertLessEqual(abs(prediction - 160.0) / 160.0, 0.25)

    def test_coverage_representative_is_training_only(self) -> None:
        # A 1.6x target ratio is jointly feasible; 1.7x is not.  The selected
        # interval therefore cannot claim to cover all three values.
        prediction = model._coverage_representative([100, 160, 170])
        passed = sum(
            abs(prediction - target) / target <= 0.25
            for target in (100, 160, 170)
        )
        self.assertEqual(passed, 2)

    def test_fold_fit_excludes_held_out_duration(self) -> None:
        labels = (
            model.atomic.Labels(),
            model.atomic.Labels(),
            model.atomic.Labels(),
            model.atomic.Labels(),
        )
        cases = [
            {
                "case_id": "a",
                "instance_id": "a",
                "queue_ordinal": 1,
                "record_size_bytes": 400,
                "kernel_clock": {"clock_id": "CLOCK_MONOTONIC"},
            },
            {
                "case_id": "b",
                "instance_id": "b",
                "queue_ordinal": 2,
                "record_size_bytes": 400,
                "kernel_clock": {"clock_id": "CLOCK_MONOTONIC"},
            },
        ]
        store = model.atomic.EventStore(cases, *labels)
        op_read = labels[0].get("read")
        size = labels[1].get("size")
        path = labels[2].get("path")
        kind = labels[3].get("read")
        for case_index, target_ns in ((0, 100), (1, 900), (1, 300)):
            store.add(
                case_index=case_index,
                operation_id=op_read,
                size_id=size,
                path_id=path,
                kind_id=kind,
                target_ns=target_ns,
                token=case_index + 1,
                sequence=1,
                raw_offset=case_index * 400,
                kernel_start_ns=10,
            )
        fitted = model.fit_mechanism(store, held_case=0)
        self.assertEqual(fitted.global_ns, 600.0)
        self.assertEqual(fitted.operation_ns[op_read], 600.0)
        # The held-out 100 ns target cannot affect the robust fit either.
        self.assertGreater(fitted.operation_coverage_ns[op_read], 100.0)

    def test_size_path_prediction_fallback_is_entry_grouped(self) -> None:
        fitted = model.MechanismFit(
            global_ns=1.0,
            operation_ns={1: 10.0},
            operation_size_ns={(1, 2): 20.0},
            operation_path_ns={(1, 3): 30.0},
            operation_size_path_ns={(1, 2, 3): 40.0},
            global_coverage_ns=2.0,
            operation_coverage_ns={1: 11.0},
            training_target_events=1,
            training_operation_groups=1,
            training_size_groups=1,
            training_path_groups=1,
            training_size_path_groups=1,
        )
        self.assertEqual(
            model.predict(fitted, "operation_requested_size_path_median", 1, 2, 3),
            40.0,
        )
        self.assertEqual(
            model.predict(fitted, "operation_requested_size_path_median", 1, 2, 8),
            20.0,
        )
        self.assertEqual(
            model.predict(fitted, "operation_requested_size_path_median", 1, 8, 3),
            30.0,
        )
        self.assertEqual(
            model.predict(fitted, "operation_requested_size_path_median", 1, 8, 8),
            10.0,
        )

    def test_raw_bound_cannot_be_raised_above_400_mb(self) -> None:
        with self.assertRaises(model.ValidationError):
            model._decode_store(model.MAX_RAW_BYTES_DEFAULT + 1)

    def test_metric_summary_tracks_count_and_worst(self) -> None:
        summary = model.ErrorSummary.create(keep_apes=True)
        event = {"event_id": "case:1:1:0"}
        summary.update(prediction_ns=100, target_ns=100, event=event)
        summary.update(prediction_ns=300, target_ns=100, event=event)
        result = summary.finish(include_p95=True)
        self.assertEqual(result["n_events"], 2)
        self.assertEqual(result["within_25_events"], 1)
        self.assertAlmostEqual(result["worst_ape_percent"], 200.0)
        self.assertEqual(result["worst_event"]["event_id"], event["event_id"])


if __name__ == "__main__":
    unittest.main()
