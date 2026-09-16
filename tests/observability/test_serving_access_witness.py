import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.telemetry.serving_metrics import AccessWitness, ServingMetricsError
from scripts.observability.serving_access_witness import (
    ACCESS_LOG_SCHEMA,
    ACCESS_SCOPE,
    VLLM_VERSION,
    WITNESS_EVIDENCE_KIND,
    WITNESS_SCHEMA,
    WitnessProducerError,
    main,
    produce_witness,
    read_access_log,
)


def _header(
    *,
    first_sequence=1,
    last_sequence=3,
    dedicated_server=True,
    stream_complete=True,
    coverage_start=100,
    coverage_end=1000,
):
    return {
        "schema_version": ACCESS_LOG_SCHEMA,
        "record_type": "stream_header",
        "server_identity": "server-a",
        "lease_id": "lease-1",
        "counter_epoch": "epoch-1",
        "vllm_version": VLLM_VERSION,
        "access_scope": ACCESS_SCOPE,
        "stream_complete": stream_complete,
        "dedicated_server": dedicated_server,
        "coverage_start_monotonic_ns": coverage_start,
        "coverage_end_monotonic_ns": coverage_end,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
    }


def _entry(sequence, request_class, request_id, started, ended):
    return {
        "schema_version": ACCESS_LOG_SCHEMA,
        "record_type": "request",
        "source_sequence": sequence,
        "request_class": request_class,
        "request_id": request_id,
        "server_identity": "server-a",
        "lease_id": "lease-1",
        "counter_epoch": "epoch-1",
        "started_monotonic_ns": started,
        "ended_monotonic_ns": ended,
    }


def _write_log(root, entries, **header_overrides):
    header_values = {"last_sequence": len(entries)}
    header_values.update(header_overrides)
    header = _header(**header_values)
    path = root / "server-access.jsonl"
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in [header, *entries]),
        encoding="utf-8",
    )
    return path


class ServingAccessWitnessTests(unittest.TestCase):
    def test_complete_server_log_produces_positive_witness_and_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_log = _write_log(
                root,
                [
                    _entry(1, "observer", None, 120, 130),
                    _entry(2, "model", "req-1", 200, 300),
                    _entry(3, "observer", None, 210, 220),
                ],
            )
            output = root / "witness.jsonl"

            row = produce_witness(
                access_log=access_log,
                output=output,
                request_id="req-1",
                window_start_monotonic_ns=190,
                window_end_monotonic_ns=310,
                expected_server_identity="server-a",
                expected_lease_id="lease-1",
                expected_counter_epoch="epoch-1",
            )

            self.assertEqual(row["schema_version"], WITNESS_SCHEMA)
            self.assertEqual(row["evidence_kind"], WITNESS_EVIDENCE_KIND)
            self.assertEqual(row["producer_status"], "measured")
            self.assertEqual(row["observed_request_ids"], ["req-1"])
            self.assertEqual(row["other_request_ids"], [])
            self.assertEqual(
                row["access_log"]["sha256"],
                hashlib.sha256(access_log.read_bytes()).hexdigest(),
            )
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 1)

    def test_server_log_with_competing_model_request_emits_negative_witness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_log = _write_log(
                root,
                [
                    _entry(1, "model", "req-1", 200, 300),
                    _entry(2, "model", "req-2", 250, 260),
                    _entry(3, "observer", None, 310, 320),
                ],
            )
            output = root / "witness.jsonl"

            row = produce_witness(
                access_log=access_log,
                output=output,
                request_id="req-1",
                window_start_monotonic_ns=190,
                window_end_monotonic_ns=310,
            )

            self.assertEqual(row["producer_status"], "unavailable")
            self.assertEqual(row["other_request_ids"], ["req-2"])
            self.assertFalse(row["no_other_requests"])
            with self.assertRaises(ServingMetricsError):
                AccessWitness.from_mapping(row).validate()

    def test_non_dedicated_server_is_retained_as_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_log = _write_log(
                root,
                [_entry(1, "model", "req-1", 200, 300)],
                last_sequence=1,
                dedicated_server=False,
            )

            row = produce_witness(
                access_log=access_log,
                output=root / "witness.jsonl",
                request_id="req-1",
                window_start_monotonic_ns=190,
                window_end_monotonic_ns=310,
            )

            self.assertEqual(row["producer_status"], "unavailable")
            self.assertIn("not marked dedicated", row["unavailable_reason"])

    def test_incomplete_or_gapped_log_cannot_produce_witness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incomplete = _write_log(
                root,
                [_entry(1, "model", "req-1", 200, 300)],
                last_sequence=1,
                stream_complete=False,
            )
            with self.assertRaisesRegex(WitnessProducerError, "not marked complete"):
                read_access_log(incomplete)

            gapped = root / "gapped.jsonl"
            rows = [
                _header(first_sequence=1, last_sequence=3),
                _entry(1, "model", "req-1", 200, 300),
                _entry(3, "observer", None, 400, 410),
            ]
            gapped.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(WitnessProducerError, "does not match its records"):
                read_access_log(gapped)

    def test_access_log_rejects_duplicate_model_id_and_observer_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            duplicate = _write_log(
                root,
                [
                    _entry(1, "model", "req-1", 200, 300),
                    _entry(2, "model", "req-1", 400, 500),
                    _entry(3, "observer", None, 600, 610),
                ],
            )
            with self.assertRaisesRegex(WitnessProducerError, "request_id is duplicated"):
                read_access_log(duplicate)

            observer_with_id = _write_log(
                root,
                [
                    _entry(1, "observer", "req-1", 200, 300),
                    _entry(2, "model", "req-2", 400, 500),
                    _entry(3, "observer", None, 600, 610),
                ],
            )
            with self.assertRaisesRegex(WitnessProducerError, "observer request must not"):
                read_access_log(observer_with_id)

    def test_window_must_be_inside_complete_log_and_include_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_log = _write_log(
                root,
                [_entry(1, "model", "req-1", 200, 300)],
                last_sequence=1,
            )
            with self.assertRaisesRegex(WitnessProducerError, "outside complete"):
                produce_witness(
                    access_log=access_log,
                    output=root / "witness-a.jsonl",
                    request_id="req-1",
                    window_start_monotonic_ns=90,
                    window_end_monotonic_ns=310,
                )
            with self.assertRaisesRegex(WitnessProducerError, "no server access-log"):
                produce_witness(
                    access_log=access_log,
                    output=root / "witness-b.jsonl",
                    request_id="req-missing",
                    window_start_monotonic_ns=190,
                    window_end_monotonic_ns=310,
                )

    def test_cli_dry_run_validates_without_writing_witness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            access_log = _write_log(
                root,
                [_entry(1, "model", "req-1", 200, 300)],
                last_sequence=1,
            )
            output = root / "witness.jsonl"

            result = main(
                [
                    "--access-log",
                    str(access_log),
                    "--output",
                    str(output),
                    "--request-id",
                    "req-1",
                    "--window-start-ns",
                    "190",
                    "--window-end-ns",
                    "310",
                    "--dry-run",
                ]
            )

            self.assertEqual(result, 0)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
