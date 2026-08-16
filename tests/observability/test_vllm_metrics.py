import unittest

from agentic_sim.observability.vllm_metrics import (
    CUMULATIVE,
    HISTOGRAM,
    INSTANTANEOUS,
    MissingMetricFamiliesError,
    PerRequestInterpretationError,
    PrometheusParseError,
    counter_delta,
    histogram_delta,
    parse_prometheus_text,
    reject_per_request_interpretation,
    validate_required_families,
)


FIXTURE = """\
# HELP vllm:prompt_tokens_total Number of prefill tokens processed.
# TYPE vllm:prompt_tokens counter
vllm:prompt_tokens_total{engine="0",model_name="Qwen/Qwen3"} 10 1710000000000
# TYPE vllm:generation_tokens counter
vllm:generation_tokens_total{engine="0",model_name="Qwen/Qwen3"} 7
# TYPE vllm:request_success counter
vllm:request_success_total{engine="0",finished_reason="eos",model_name="Qwen/Qwen3"} 2
vllm:request_success_total{engine="0",finished_reason="stop",model_name="Qwen/Qwen3"} 1
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="Qwen/Qwen3"} 1
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="Qwen/Qwen3"} 0.25
# TYPE vllm:e2e_request_latency_seconds histogram
vllm:e2e_request_latency_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 1
vllm:e2e_request_latency_seconds_bucket{engine="0",le="+Inf",model_name="Qwen/Qwen3"} 2
vllm:e2e_request_latency_seconds_count{engine="0",model_name="Qwen/Qwen3"} 2
vllm:e2e_request_latency_seconds_sum{engine="0",model_name="Qwen/Qwen3"} 0.7
# TYPE vllm:time_to_first_token_seconds histogram
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:time_per_output_token_seconds histogram
vllm:time_per_output_token_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_queue_time_seconds histogram
vllm:request_queue_time_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_inference_time_seconds histogram
vllm:request_inference_time_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_decode_time_seconds histogram
vllm:request_decode_time_seconds_bucket{engine="0",le="0.3",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_prompt_tokens histogram
vllm:request_prompt_tokens_bucket{engine="0",le="32",model_name="Qwen/Qwen3"} 2
# TYPE vllm:request_generation_tokens histogram
vllm:request_generation_tokens_bucket{engine="0",le="32",model_name="Qwen/Qwen3"} 2
"""


class VllmMetricsTests(unittest.TestCase):
    def test_parser_preserves_labels_types_values_and_timestamp(self):
        snapshot = parse_prometheus_text(FIXTURE)
        prompt = snapshot.for_family("vllm:prompt_tokens_total")[0]
        self.assertEqual(prompt.labels["model_name"], "Qwen/Qwen3")
        self.assertEqual(prompt.labels["engine"], "0")
        self.assertEqual(prompt.metric_type, "counter")
        self.assertEqual(prompt.timestamp_ms, 1710000000000)
        self.assertEqual(prompt.value, 10.0)
        self.assertEqual(len(snapshot.for_family("vllm:request_success_total")), 2)

    def test_parser_handles_escaped_labels_and_bytes(self):
        snapshot = parse_prometheus_text(
            b'# TYPE vllm:num_requests_running gauge\n'
            b'vllm:num_requests_running{note="a\\n b \\"q\\""} NaN\n'
        )
        sample = snapshot[0]
        self.assertEqual(sample.labels["note"], 'a\n b "q"')
        self.assertEqual(sample.metric_type, "gauge")

    def test_malformed_or_duplicate_series_fails_closed(self):
        with self.assertRaises(PrometheusParseError):
            parse_prometheus_text('vllm:broken{label="unterminated 1')
        with self.assertRaises(PrometheusParseError):
            parse_prometheus_text(
                'vllm:num_requests_running{a="1"} 1\n'
                'vllm:num_requests_running{a="1"} 2\n'
            )

    def test_required_families_and_classifications(self):
        validate_required_families(parse_prometheus_text(FIXTURE))
        self.assertEqual(parse_prometheus_text(FIXTURE).for_family("vllm:kv_cache_usage_perc")[0].family.aggregation, INSTANTANEOUS)
        self.assertEqual(parse_prometheus_text(FIXTURE).for_family("vllm:prompt_tokens_total")[0].family.aggregation, CUMULATIVE)
        self.assertEqual(parse_prometheus_text(FIXTURE).for_family("vllm:e2e_request_latency_seconds")[0].family.aggregation, HISTOGRAM)
        with self.assertRaises(MissingMetricFamiliesError) as context:
            validate_required_families(parse_prometheus_text("# TYPE vllm:prompt_tokens counter\nvllm:prompt_tokens_total 1\n"))
        self.assertIn("vllm:e2e_request_latency_seconds", context.exception.missing)

    def test_counter_delta_marks_missing_and_reset_unavailable(self):
        self.assertEqual(counter_delta(10, 13).value, 3.0)
        self.assertEqual(counter_delta(13, 10).status, "reset")
        self.assertEqual(counter_delta(None, 10).status, "unavailable")

    def test_histogram_delta_preserves_series_and_rejects_reset(self):
        before = parse_prometheus_text(
            '# TYPE vllm:e2e_request_latency_seconds histogram\n'
            'vllm:e2e_request_latency_seconds_bucket{le="1"} 2\n'
            'vllm:e2e_request_latency_seconds_count 2\n'
            'vllm:e2e_request_latency_seconds_sum 1.0\n'
        )
        after = parse_prometheus_text(
            '# TYPE vllm:e2e_request_latency_seconds histogram\n'
            'vllm:e2e_request_latency_seconds_bucket{le="1"} 5\n'
            'vllm:e2e_request_latency_seconds_count 5\n'
            'vllm:e2e_request_latency_seconds_sum 3.0\n'
        )
        result = histogram_delta(before, after)
        self.assertTrue(result.measured)
        self.assertEqual(sorted(result.deltas.values()), [2.0, 3.0, 3.0])
        reset = histogram_delta(after, before)
        self.assertEqual(reset.status, "reset")
        missing = histogram_delta(before, parse_prometheus_text(""))
        self.assertEqual(missing.status, "unavailable")

    def test_native_metrics_explicitly_reject_per_request_interpretation(self):
        with self.assertRaises(PerRequestInterpretationError):
            reject_per_request_interpretation("vllm:e2e_request_latency_seconds", "request-1")


if __name__ == "__main__":
    unittest.main()
