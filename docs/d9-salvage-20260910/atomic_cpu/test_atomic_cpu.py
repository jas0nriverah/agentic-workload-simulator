#!/usr/bin/env python3
"""Focused contract tests for the bounded atomic CPU comparison."""
from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_atomic_cpu as model  # noqa: E402


class AtomicCpuContractTests(unittest.TestCase):
    def test_entry_feature_keys_ignore_result_fields(self) -> None:
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
            model.operation_key(row),
            model.requested_size_bucket(model.requested_size_value(row)),
            model.path_class(row),
        )
        changed = dict(row)
        changed.update(ret=-1, status_name="failure", duration_ns=999999999)
        self.assertEqual(features, (
            model.operation_key(changed),
            model.requested_size_bucket(model.requested_size_value(changed)),
            model.path_class(changed),
        ))

    def test_size_and_path_buckets_are_entry_only(self) -> None:
        self.assertEqual(model.requested_size_bucket(None), "entry_size_unavailable")
        self.assertEqual(model.requested_size_bucket(4096), "entry_size_1_4KiB")
        self.assertEqual(model.requested_size_bucket(65537), "entry_size_gt_64KiB")
        self.assertEqual(model.path_class({"path_status_name": "unknown", "path": "/x"}), "entry_path_unknown")
        self.assertEqual(model.path_class({"path_status_name": "observed", "path": "/x"}), "entry_path_absolute")
        self.assertEqual(model.path_class({"path_status_name": "observed", "path": "../x"}), "entry_path_dot_relative")
        self.assertEqual(model.path_class({"path_status_name": "truncated", "path": "/x"}), "entry_path_truncated")

    def test_selection_is_ordinal_and_deduplicates_instances(self) -> None:
        cases = [
            {"queue_ordinal": 4, "instance_id": "c"},
            {"queue_ordinal": 2, "instance_id": "a"},
            {"queue_ordinal": 3, "instance_id": "b"},
            {"queue_ordinal": 1, "instance_id": "a"},
            {"queue_ordinal": 5, "instance_id": "d"},
        ]
        with mock.patch.object(model, "_fully_valid_case", return_value=(True, {})):
            selected = model.select_cases({"cases": cases}, count=4)
        self.assertEqual([item["queue_ordinal"] for item in selected], [1, 3, 4, 5])
        self.assertEqual([item["instance_id"] for item in selected], ["a", "b", "c", "d"])

    def test_metric_excludes_nonpositive_targets(self) -> None:
        labels = (model.Labels(), model.Labels(), model.Labels(), model.Labels())
        cases = [{
            "case_id": "case",
            "instance_id": "instance",
            "queue_ordinal": 1,
            "record_size_bytes": 400,
            "kernel_clock": {"clock_id": "CLOCK_MONOTONIC"},
        }]
        store = model.EventStore(cases, *labels)
        store.add(
            case_index=0,
            operation_id=labels[0].get("op"),
            size_id=labels[1].get("size"),
            path_id=labels[2].get("path"),
            kind_id=labels[3].get("read"),
            target_ns=0,
            token=1,
            sequence=1,
            raw_offset=0,
            kernel_start_ns=10,
        )
        self.assertEqual(sum(value > 0 for value in store.target_ns), 0)
        self.assertEqual(store.memory_bytes(), sum(
            item.itemsize * len(item)
            for item in (
                store.instance_index, store.operation_id, store.size_id,
                store.path_id, store.kind_id, store.target_ns, store.token, store.sequence,
                store.raw_offset, store.kernel_start_ns,
            )
        ))

    def test_group_held_out_fit_does_not_read_held_out_durations(self) -> None:
        labels = (model.Labels(), model.Labels(), model.Labels(), model.Labels())
        cases = [
            {"case_id": "a", "instance_id": "a", "queue_ordinal": 1, "record_size_bytes": 400,
             "kernel_clock": {"clock_id": "CLOCK_MONOTONIC"}},
            {"case_id": "b", "instance_id": "b", "queue_ordinal": 2, "record_size_bytes": 400,
             "kernel_clock": {"clock_id": "CLOCK_MONOTONIC"}},
        ]
        store = model.EventStore(cases, *labels)
        op_read = labels[0].get("read")
        op_write = labels[0].get("write")
        size = labels[1].get("size")
        path = labels[2].get("path")
        kind = labels[3].get("read")
        for case_index, operation_id, target_ns in (
            (0, op_read, 100),
            (1, op_read, 900),
            (1, op_write, 300),
        ):
            store.add(
                case_index=case_index,
                operation_id=operation_id,
                size_id=size,
                path_id=path,
                kind_id=kind,
                target_ns=target_ns,
                token=case_index + 1,
                sequence=1,
                raw_offset=case_index * 400,
                kernel_start_ns=10,
            )
        fitted = model.fit_fold(store, held_case=0)
        self.assertEqual(fitted.global_ns, 600.0)
        self.assertEqual(fitted.operation_ns[op_read], 900.0)
        self.assertNotEqual(fitted.global_ns, 300.0)

    def test_action_range_cover_rejects_gap_and_overlap(self) -> None:
        contiguous = [
            {"offset_start": 0, "offset_end": 400, "record_count": 1, "action_token": 11},
            {"offset_start": 400, "offset_end": 800, "record_count": 1, "action_token": 12},
        ]
        gap = [
            {"offset_start": 0, "offset_end": 400, "record_count": 1, "action_token": 11},
            {"offset_start": 800, "offset_end": 1200, "record_count": 1, "action_token": 12},
        ]
        overlap = [
            {"offset_start": 0, "offset_end": 800, "record_count": 2, "action_token": 11},
            {"offset_start": 400, "offset_end": 1200, "record_count": 2, "action_token": 12},
        ]
        self.assertEqual(model._validate_action_ranges(contiguous, 800)[2], {11: 1, 12: 1})
        with self.assertRaises(model.ValidationError):
            model._validate_action_ranges(gap, 1200)
        with self.assertRaises(model.ValidationError):
            model._validate_action_ranges(overlap, 1200)

    def test_event_token_must_match_containing_action_range(self) -> None:
        action_range = {
            "offset_start": 400,
            "offset_end": 800,
            "action_token": 12,
        }
        model._validate_event_membership(action_range, raw_offset=400, action_token=12)
        with self.assertRaises(model.ValidationError):
            model._validate_event_membership(action_range, raw_offset=400, action_token=11)
        with self.assertRaises(model.ValidationError):
            model._validate_event_membership(action_range, raw_offset=800, action_token=12)


if __name__ == "__main__":
    unittest.main()
