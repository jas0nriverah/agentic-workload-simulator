import hashlib
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from agentic_sim.telemetry.serving_metrics import (
    AccessWitness,
    ServingMetricsCollector,
    ServingMetricsError,
    ServingSnapshot,
    derive_serving_metrics,
    write_snapshot_pair,
)


def _witness(**overrides):
    value = {
        "request_id": "req-1",
        "server_identity": "server-a",
        "lease_id": "lease-1",
        "counter_epoch": "epoch-1",
        "observed_request_ids": ("req-1",),
        "other_request_ids": (),
        "dedicated_server": True,
        "no_other_requests": True,
    }
    value.update(overrides)
    return AccessWitness(**value)


def _histogram_scrape(count=10, sums=(1.0, 2.0, 3.0, 4.0), *, request_id=None):
    families = (
        "vllm:request_queue_time_seconds",
        "vllm:request_prefill_time_seconds",
        "vllm:request_decode_time_seconds",
        "vllm:e2e_request_latency_seconds",
    )
    lines = []
    for family, total in zip(families, sums):
        labels = "{engine=\"0\"" + (f",request_id=\"{request_id}\"" if request_id else "") + "}"
        lines.extend(
            [
                f"# TYPE {family} histogram",
                f"{family}_count{labels} {count}",
                f"{family}_sum{labels} {total}",
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _direct_scrape(*, request_id="req-1"):
    values = {
        "vllm:request_queue_time_seconds": 0.01,
        "vllm:request_prefill_time_seconds": 0.02,
        "vllm:request_decode_time_seconds": 0.03,
        "vllm:e2e_request_latency_seconds": 0.06,
    }
    return (
        "\n".join(
            f"# TYPE {family} gauge\n{family}{{request_id=\"{request_id}\"}} {value}"
            for family, value in values.items()
        )
        + "\n"
    ).encode("utf-8")


def _snapshot(raw, *, identity="server-a", epoch="epoch-1"):
    return ServingSnapshot.from_raw(
        raw,
        url="http://127.0.0.1:8000/metrics",
        server_identity=identity,
        counter_epoch=epoch,
    )


class ServingMetricsTests(unittest.TestCase):
    def test_failed_http_scrape_cannot_supply_valid_native_values(self):
        from agentic_sim.telemetry.serving_metrics import _HTTPFetchError

        for failed_phase in (None, "before", "after"):
            with self.subTest(failed_phase=failed_phase):
                def fetch(url, headers, timeout):
                    phase = headers["X-EIC-Scrape-Phase"]
                    raw = _histogram_scrape(count=10 if phase == "before" else 11)
                    if phase == failed_phase:
                        raise _HTTPFetchError(503, "Unavailable", raw)
                    return raw

                collector = ServingMetricsCollector("http://localhost:8000/metrics",
                    server_identity="server-a", counter_epoch="epoch-1", fetcher=fetch)
                before, after = collector.before_request(), collector.after_request()
                measurement = derive_serving_metrics(before, after, _witness())
                self.assertEqual(measurement.status, "unavailable" if failed_phase else "measured")
                if failed_phase:
                    failed = before if failed_phase == "before" else after
                    self.assertTrue(failed.raw)
                    self.assertIsNotNone(failed.parsed)
                    self.assertIn(failed_phase + " metrics scrape failed", measurement.context_reason)
                    self.assertTrue(all(value.value_ms is None for value in measurement.metrics.values()))

    def test_all_raw_parser_validation_is_inside_scrape_bracket(self):
        import agentic_sim.telemetry.serving_metrics as metrics

        timeline = []
        original = metrics.parse_prometheus_text
        def parse(raw):
            timeline.append("parse")
            return original(raw)
        def clock():
            timeline.append("clock")
            return len(timeline) * 100
        collector = ServingMetricsCollector("http://localhost:8000/metrics",
            server_identity="server-a", counter_epoch="epoch-1",
            fetcher=lambda *args: _histogram_scrape())
        with patch.object(metrics, "parse_prometheus_text", side_effect=parse), patch.object(
            metrics, "monotonic_ns", side_effect=clock
        ):
            snapshot = collector.before_request()
        self.assertEqual(timeline, ["clock", "parse", "clock", "parse", "clock"])
        self.assertEqual(snapshot.scrape_ended_monotonic_ns, 500)

    def test_scrape_ids_join_server_observations_without_duplicate_model_ids(self):
        headers = []
        def fetch(url, request_headers, timeout):
            headers.append(dict(request_headers))
            return _histogram_scrape()
        collector = ServingMetricsCollector("http://localhost:8000/metrics",
            server_identity="server-a", counter_epoch="epoch-1", fetcher=fetch)
        before = collector.before_request(physical_request_id="model-request-1")
        after = collector.after_request(physical_request_id="model-request-1")
        self.assertNotEqual(before.scrape_id, after.scrape_id)
        for snapshot, sent in zip((before, after), headers):
            self.assertEqual(sent['X-EIC-Scrape-ID'], snapshot.scrape_id)
            self.assertEqual(sent['X-EIC-Scrape-Phase'], snapshot.scrape_phase)
            self.assertEqual(snapshot.associated_physical_request_id, 'model-request-1')
            self.assertNotIn('X-EIC-Physical-Request-ID', sent)

    def test_scrape_brackets_survive_success_and_transport_failure(self):
        for fail in (False, True):
            def fetch(*args):
                if fail:
                    raise TimeoutError("fixture timeout")
                return _histogram_scrape()
            collector = ServingMetricsCollector("http://localhost:8000/metrics",
                server_identity="server-a", counter_epoch="epoch-1", fetcher=fetch)
            with patch("agentic_sim.telemetry.serving_metrics.monotonic_ns", side_effect=[100, 120, 150]):
                snapshot = collector.before_request()
            row = snapshot.to_record()
            self.assertEqual((row["scrape_started_monotonic_ns"], row["captured_monotonic_ns"],
                              row["scrape_ended_monotonic_ns"]), (100, 120, 150))
            self.assertEqual(row["scrape_phase"], "before")
            self.assertEqual(snapshot.available, not fail)

    def test_cross_boot_and_overlapping_scrapes_cannot_be_attributed(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(_histogram_scrape(count=11))
        foreign = replace(after, clock={**after.clock, "boot_id": "other-boot"})
        self.assertEqual(derive_serving_metrics(before, foreign, _witness()).status, "unavailable")
        before = replace(before, captured_monotonic_ns=120,
                         scrape_started_monotonic_ns=100, scrape_ended_monotonic_ns=200)
        after = replace(after, captured_monotonic_ns=220,
                        scrape_started_monotonic_ns=190, scrape_ended_monotonic_ns=300)
        self.assertIn("overlap", derive_serving_metrics(before, after, _witness()).context_reason)
        with self.assertRaises(ServingMetricsError):
            replace(before, scrape_ended_monotonic_ns=110)

    def test_isolated_histogram_deltas_are_native_milliseconds(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(_histogram_scrape(count=11, sums=(1.125, 2.25, 3.5, 4.75)))
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.status, "measured")
        self.assertTrue(result.measured)
        self.assertEqual(result.metrics["queue"].value_ms, 125.0)
        self.assertEqual(result.metrics["prefill"].value_ms, 250.0)
        self.assertEqual(result.metrics["decode"].value_ms, 500.0)
        self.assertEqual(result.metrics["e2e"].value_ms, 750.0)
        self.assertEqual(result.metrics["e2e"].count_delta, 1.0)
        self.assertEqual(result.metrics["queue"].scope, "isolated_server_aggregate")
        self.assertFalse(result.to_record()["proxy_elapsed_used"])

    def test_count_delta_other_than_one_is_unavailable(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(_histogram_scrape(count=12, sums=(1.2, 2.2, 3.2, 4.2)))
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.metrics["queue"].count_delta, 2.0)
        self.assertIn("expected 1", result.metrics["queue"].reason)

    def test_identity_and_epoch_changes_fail_closed(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(_histogram_scrape(count=11), identity="server-b", epoch="epoch-2")
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.status, "unavailable")
        self.assertTrue(all(not value.measured for value in result.metrics.values()))
        self.assertIn("server identity", result.context_reason)

    def test_witness_requires_dedicated_server_and_exact_request_union(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(_histogram_scrape(count=11, sums=(1.1, 2.1, 3.1, 4.1)))
        result = derive_serving_metrics(
            before,
            after,
            _witness(observed_request_ids=("req-1", "req-2")),
        )

        self.assertEqual(result.status, "unavailable")
        self.assertIn("exactly the expected request", result.context_reason)

    def test_native_request_labelled_samples_take_precedence(self):
        before = _snapshot(_direct_scrape())
        after = _snapshot(_direct_scrape())
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.status, "measured")
        self.assertEqual(result.metrics["queue"].value_ms, 10.0)
        self.assertEqual(result.metrics["e2e"].value_ms, 60.0)
        self.assertEqual(result.metrics["queue"].scope, "native_per_request")

    def test_nonfinite_or_negative_histogram_values_are_unavailable(self):
        before = _snapshot(_histogram_scrape(count=10))
        malformed = _histogram_scrape(
            count=11, sums=(float("nan"), 2.1, 3.1, 4.1)
        ).replace(b"nan", b"NaN")
        after = _snapshot(malformed)
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.metrics["queue"].status, "unavailable")
        self.assertIn("non-finite", result.metrics["queue"].reason)

    def test_parse_failure_keeps_raw_hash_and_reason(self):
        raw = b"vllm:broken{label=\"unterminated 1\n"
        snapshot = _snapshot(raw)

        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.raw_sha256, hashlib.sha256(raw).hexdigest())
        self.assertTrue(snapshot.parse_error)
        self.assertEqual(snapshot.to_record()["raw_bytes"], len(raw))

    def test_collector_is_read_only_metrics_get_with_injected_fetcher(self):
        raw = _histogram_scrape()
        calls = []

        def fetcher(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            return raw

        collector = ServingMetricsCollector(
            "http://127.0.0.1:8000/metrics",
            server_identity="server-a",
            counter_epoch="epoch-1",
            fetcher=fetcher,
        )
        snapshot = collector.before_request()

        self.assertTrue(snapshot.available)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "http://127.0.0.1:8000/metrics")
        self.assertEqual(calls[0][1]["Accept"], "text/plain; version=0.0.4")

    def test_fetch_failure_is_explicitly_unavailable(self):
        def fetcher(_url, _headers, _timeout):
            raise TimeoutError("fixture timeout")

        collector = ServingMetricsCollector(
            "http://127.0.0.1:8000/metrics",
            server_identity="server-a",
            counter_epoch="epoch-1",
            fetcher=fetcher,
        )
        snapshot = collector.after_request()

        self.assertFalse(snapshot.available)
        self.assertIn("fixture timeout", snapshot.scrape_error)

    def test_raw_pair_writer_preserves_exact_bytes(self):
        before_raw = _histogram_scrape()
        after_raw = _histogram_scrape(count=11, sums=(1.1, 2.1, 3.1, 4.1))
        before = _snapshot(before_raw)
        after = _snapshot(after_raw)
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_snapshot_pair(before, after, tmp)
            self.assertEqual(Path(paths["before"]).read_bytes(), before_raw)
            self.assertEqual(Path(paths["after"]).read_bytes(), after_raw)
            self.assertEqual(
                before.to_record(raw_path=str(paths["before"]))["raw_sha256"],
                before.raw_sha256,
            )

    def test_mapping_witness_accepts_server_lease_aliases(self):
        witness = AccessWitness.from_mapping(
            {
                "request_id": "req-1",
                "server_id": "server-a",
                "server_lease_id": "lease-1",
                "metrics_counter_epoch": "epoch-1",
                "request_ids": ["req-1"],
                "other_ids": [],
                "isolated": True,
                "no_other_request_ids": True,
            }
        )
        witness.validate()
        self.assertEqual(witness.server_lease_id, "lease-1")

    def test_witness_booleans_are_real_booleans(self):
        with self.assertRaises(ServingMetricsError):
            AccessWitness(
                request_id="req-1",
                server_identity="server-a",
                lease_id="lease-1",
                counter_epoch="epoch-1",
                dedicated_server="false",
            )
        with self.assertRaises(ServingMetricsError):
            AccessWitness.from_mapping(
                {
                    "request_id": "req-1",
                    "server_identity": "server-a",
                    "lease_id": "lease-1",
                    "counter_epoch": "epoch-1",
                    "observed_request_ids": ["req-1"],
                    "dedicated_server": "false",
                    "no_other_requests": True,
                }
            )

    def test_mapping_witness_requires_an_explicit_complete_union(self):
        incomplete = {
            "request_id": "req-1",
            "server_identity": "server-a",
            "lease_id": "lease-1",
            "counter_epoch": "epoch-1",
            "observed_request_ids": ["req-1"],
            "dedicated_server": True,
            "no_other_requests": True,
        }
        with self.assertRaises(ServingMetricsError):
            AccessWitness.from_mapping(incomplete)

        with self.assertRaises(ServingMetricsError):
            AccessWitness.from_mapping(
                {
                    **incomplete,
                    "other_request_ids": [],
                    "request_ids": ["req-2"],
                }
            )

    def test_non_histogram_native_series_cannot_be_attributed_as_a_delta(self):
        before = _snapshot(_histogram_scrape(count=10))
        after = _snapshot(
            _histogram_scrape(count=11, sums=(1.1, 2.1, 3.1, 4.1)).replace(
                b"# TYPE vllm:request_queue_time_seconds histogram",
                b"# TYPE vllm:request_queue_time_seconds gauge",
            )
        )
        result = derive_serving_metrics(before, after, _witness())

        self.assertEqual(result.metrics["queue"].status, "unavailable")
        self.assertIn("series missing", result.metrics["queue"].reason)

    def test_snapshot_rejects_parsed_content_swapped_from_other_raw_bytes(self):
        first = _snapshot(_histogram_scrape())
        second = _snapshot(_histogram_scrape(count=11, sums=(1.1, 2.1, 3.1, 4.1)))

        with self.assertRaises(ServingMetricsError):
            replace(first, parsed=second.parsed)


if __name__ == "__main__":
    unittest.main()
