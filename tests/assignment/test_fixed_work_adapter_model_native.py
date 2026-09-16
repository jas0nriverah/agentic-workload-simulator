"""CPU-only model integration tests; native callbacks here are explicit fixtures."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.validation import fixed_work_adapter as adapter
from scripts.validation import check_live_native_serving as live
from tests.telemetry.test_serving_observer import (
    _NativeProcessorFixture, _native_fixture, _native_state, _scope, _run, _metrics,
)


PARAMS = {"temperature": 0, "top_p": 1, "seed": 0, "n": 1,
          "stream": False, "max_tokens": 128, "ignore_eos": True}


def _finished(self, req_state, finish_reason, iteration_stats):
    iteration_stats.finished_requests.append(SimpleNamespace(
        finish_reason=finish_reason, e2e_latency=.6, queued_time=.1, prefill_time=.2,
        decode_time=.3, inference_time=.5, num_prompt_tokens=512,
        num_generation_tokens=128, max_tokens_param=128))
    return self.result


def _fixture(root, *, cached=0, failed=False, foreign=False, deferred=False, case_id=None, attempt_id=None):
    server, output = root / "server", root / "condition"
    server.mkdir()
    telemetry = output / "model/telemetry_v2"
    body = {"model": "fixture-model", "messages": [{"role": "user", "content": "fixture"}], **PARAMS}
    request = adapter.Request(0, "POST", "/v1/chat/completions", live.encoded(body), {}, "logical-0")
    response = live.encoded({"id": "chatcmpl-physical-0", "usage": {
        "prompt_tokens": 512, "completion_tokens": 128, "prompt_tokens_details": {"cached_tokens": cached}}})
    response_status = 400 if failed else 200
    processor = _NativeProcessorFixture()

    async def app(scope, receive, send):
        await receive()
        route = scope["path"]
        if route == "/metrics":
            raw, status = _metrics(0, (0., 0., 0., 0.)), 200
        elif route == "/reset_prefix_cache":
            raw, status = b"", 200
        elif route == "/foreign":
            raw, status = b"foreign", 404
        else:
            scope["state"]["request_metadata"] = SimpleNamespace(request_id="chatcmpl-physical-0")
            processor._update_stats_from_finished(_native_state("chatcmpl-physical-0"), "length",
                                                   SimpleNamespace(finished_requests=[]))
            raw, status = response, response_status
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": raw, "more_body": False})

    with patch.object(_NativeProcessorFixture, "_update_stats_from_finished", _finished):
        with _native_fixture(server, app) as observer:
            async def exercise():
                reset_scope = _scope("/reset_prefix_cache", request_id="reset-0")
                await _run(observer, reset_scope, [{"type": "http.request", "body": b"", "more_body": False}])
                if foreign:
                    await _run(observer, _scope("/foreign"), [{"type": "http.request", "body": b"foreign", "more_body": False}])
                if not deferred:
                    await _run(observer, _scope("/metrics", scrape_id="before-0", phase="before"),
                               [{"type": "http.request", "body": b"", "more_body": False}])
                scope = _scope("/v1/chat/completions", request_id="physical-0", case_id=case_id, attempt_id=attempt_id)
                scope["headers"].append((b"x-request-id", b"physical-0"))
                scope["state"] = {}
                await _run(observer, scope, [{"type": "http.request", "body": request.body, "more_body": False}])
                if not deferred:
                    await _run(observer, _scope("/metrics", scrape_id="after-0", phase="after"),
                               [{"type": "http.request", "body": b"", "more_body": False}])
            asyncio.run(exercise())

    header = live.json_rows((server / "observer.jsonl").read_bytes())[0]
    archive = root / "server.tar"
    with archive.open("xb") as stream:
        live.capture_archive(server / "observer.jsonl", server / "native.jsonl", stream)
    config = {"upstream_host": "localhost", "upstream_port": 1, "method": "POST", "path": "/v1/chat/completions",
              "timeout_seconds": 1., "max_body_bytes": 4096, "headers": {}, "cache_policy": {"mode": "fixture"},
              "serving_metrics_config": {"enabled": True}, "server_archive": {"path": str(archive), "sha256": adapter._sha_file(archive)}}
    fixture = SimpleNamespace(case_id="model-short-request-v1", kind="model_short_request", spec={
        "model": config, "serving_and_cache_policy": {"model": "fixture-model", "model_revision": "fixture-revision", "output_policy": PARAMS}})
    adapter._write_json(output / "model/server_runtime.before.json", {
        "pid": header["server_pid"], "start_ticks": header["server_process_start_ticks"],
        "hostname": header["clock"]["hostname"], "boot_id": header["clock"]["boot_id"],
        "counter_epoch": header["counter_epoch"], "native_hook_sha256": header["native_observer"]["binding"]["hook_source_sha256"]})
    adapter._write_json(output / "model/cache_reset.json", {"physical_request_id": "reset-0", "response_sha256": adapter._sha_bytes(b"")})
    audit = {"index": 0, "request_sha256": adapter._sha_bytes(request.body), "request_bytes": len(request.body),
             "response_sha256": adapter._sha_bytes(response), "response_bytes": len(response),
             "status_code": response_status, "response_complete": True, "completion_tokens": 128,
             "response_path": "model/responses/0000.bin", "error": None}
    adapter._write_bytes(output / audit["response_path"], response)
    artifacts = {}
    for name, raw in (("request", request.body), ("response", response)):
        relative = "payloads/" + name + ".bin"
        adapter._write_bytes(telemetry / relative, raw)
        artifacts[name] = {"artifact_path": relative, "sha256": adapter._sha_bytes(raw), "complete": True}
    snapshots = {}
    raw_metric = _metrics(0, (0., 0., 0., 0.))
    for phase in ("before", "after"):
        name = "serving_metrics/" + phase + ".prom"
        if deferred:
            snapshots[phase] = {"status": "unavailable", "raw_path": None, "raw_sha256": None, "scrape_phase": phase}
            continue
        adapter._write_bytes(telemetry / name, raw_metric)
        snapshots[phase] = {"raw_path": name, "raw_sha256": adapter._sha_bytes(raw_metric),
                            "scrape_phase": phase, "scrape_id": phase + "-0", "associated_physical_request_id": "physical-0"}
    original = {"request_id": "physical-0", "status": "unavailable", "unavailable_reason": "native counter publication pending",
                "server_identity": header["server_identity"], "counter_epoch": header["counter_epoch"],
                "snapshots": snapshots}
    if deferred:
        original.update(attribution_mode="native_deferred", unavailable_reason="native_deferred: no per-request scrape",
                        capture={"scrape_count": 0, "physical_request_dispatched": True})
    record_path = telemetry / "serving_metrics/original.json"
    adapter._write_json(record_path, original)
    event = {**audit, "serving_metrics_status": "unavailable", "serving_metrics_physical_request_dispatched": True,
             "serving_metrics_record": {"path": "serving_metrics/original.json", "sha256": adapter._sha_file(record_path)}}
    adapter._append_jsonl(output / "model/request_events.jsonl", event)
    adapter._append_jsonl(telemetry / "model_events.jsonl", {
        "terminal": True, "event_kind": "model_request", "logical_request_id": "logical-0",
        "physical_request_id": "physical-0", "status": "failure" if failed else "success", "output_tokens": 128,
        "request_body_sha256": adapter._sha_bytes(request.body), "request_payload_artifact": artifacts})
    return fixture, output, request, audit, event


def test_native_deferred_accepts_exact_source_and_keeps_old_unavailable(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path)
    original_path = output / "model/telemetry_v2/serving_metrics/original.json"
    original = original_path.read_bytes()
    count, physical = adapter._model_capture_counts(fixture, "instrument_on", output, [request], [audit])
    assert physical == 1 and count["full_production_capture_enabled"] is False
    assert original_path.read_bytes() == original
    row = adapter._read_jsonl(output / "model/native_attribution.jsonl", "native")[0]
    assert row["status"] == "measured", row
    assert row["original_serving_metrics_status"] == "unavailable"
    assert row["work"]["cached_tokens"] == 0 and row["work"]["prompt_tokens"] == 512
    assert row["native"]["native_metric_source"] == "vllm_v1_finished_request_stats"


def test_native_deferred_original_without_scrapes_is_measured_from_native_journal(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path, deferred=True)
    row = adapter._model_native_results(fixture, output, [request], [audit], [event])[0]
    assert row["status"] == "measured", row
    assert row["attribution_mode"] == "native_deferred" and row["scrapes"] is None
    assert row["native"]["native_metric_source"] == "vllm_v1_finished_request_stats"
    assert row["native"]["metrics"]["queue"]["value_ms"] == pytest.approx(100.0)


def test_native_deferred_original_claiming_a_scrape_is_rejected(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path, deferred=True)
    path = output / "model/telemetry_v2/serving_metrics/original.json"
    original = json.loads(path.read_bytes())
    original["capture"]["scrape_count"] = 2
    adapter._write_json(path, original)
    event["serving_metrics_record"]["sha256"] = adapter._sha_file(path)
    row = adapter._model_native_results(fixture, output, [request], [audit], [event])[0]
    assert row["status"] == "unavailable" and "zero proxy scrapes" in row["unavailable_reason"]


def test_deferred_mode_still_fails_closed_on_foreign_ingress(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path, deferred=True, foreign=True)
    row = adapter._model_native_results(fixture, output, [request], [audit], [event])[0]
    assert row["status"] == "unavailable"


@pytest.mark.parametrize("cached", [1, None, True])
def test_reset_http_200_does_not_prove_cold_cache(tmp_path, cached):
    fixture, output, request, audit, event = _fixture(tmp_path, cached=cached)
    rows = adapter._model_native_results(fixture, output, [request], [audit], [event])
    assert rows[0]["status"] == "unavailable"
    assert "actual model usage" in rows[0]["unavailable_reason"]


def test_failed_request_preserves_native_partial_and_original_bytes(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path, failed=True)
    rows = adapter._model_native_results(fixture, output, [request], [audit], [event])
    assert rows[0]["status"] == "unavailable" and rows[0]["native_status"] == "unavailable"
    assert rows[0]["native"]["status"] == "unavailable"
    native = next(Path(rows[0]["archive"]["resolved_root"]).rglob("native.jsonl"))
    assert any(r["record_type"] == "native_finished" for r in live.json_rows(native.read_bytes()))


def test_intervening_foreign_request_invalidates_cold_reset(tmp_path):
    fixture, output, request, audit, event = _fixture(tmp_path, foreign=True)
    row = adapter._model_native_results(fixture, output, [request], [audit], [event])[0]
    assert row["status"] == "unavailable" and "intervening" in row["unavailable_reason"]


@pytest.mark.parametrize("fault", ["physical", "scrape", "response", "runtime", "multiplicity"])
def test_native_joins_fail_closed(tmp_path, fault):
    fixture, output, request, audit, event = _fixture(tmp_path)
    telemetry = output / "model/telemetry_v2"
    if fault == "multiplicity":
        rows = adapter._read_jsonl(telemetry / "model_events.jsonl", "model")
        adapter._append_jsonl(telemetry / "model_events.jsonl", dict(rows[0], physical_request_id="retry-1"))
    elif fault in ("physical", "scrape"):
        path = telemetry / "serving_metrics/original.json"
        original = json.loads(path.read_bytes())
        if fault == "physical":
            original["request_id"] = "wrong"
        else:
            original["snapshots"]["after"]["scrape_id"] = "wrong"
        adapter._write_json(path, original)
        event["serving_metrics_record"]["sha256"] = adapter._sha_file(path)
    elif fault == "runtime":
        path = output / "model/server_runtime.before.json"
        runtime = json.loads(path.read_bytes())
        adapter._write_json(path, dict(runtime, counter_epoch="other-epoch"))
    else:
        audit["response_sha256"] = "f" * 64
    row = adapter._model_native_results(fixture, output, [request], [audit], [event])[0]
    assert row["status"] == "unavailable", row


def test_proxy_only_off_is_rejected_from_actual_process_flags(tmp_path):
    path = tmp_path / "runtime.json"
    report = {"source": "procfs", "hostname": "fixture", "boot_id": "fixture-boot", "gpu_uuid": "GPU-fixture",
              "observed_monotonic_ns": 1, "process": {"pid": 123, "start_ticks": 100, "argv": [
                  "python", "-m", "vllm.entrypoints.openai.api_server", "--enable-prompt-tokens-details",
                  "--middleware", "serving_observer.ServingObserver"], "environment": {"EIC_NATIVE_VLLM_OBSERVER": "true"}}}
    adapter._write_json(path, report)
    config = {"server_runtime": {"before": {"path": str(path), "sha256": adapter._sha_file(path)}}}
    with pytest.raises(adapter.AdapterError, match="proxy-only off"):
        adapter._model_runtime(config, "instrument_off", "before")
    report["process"]["argv"] = report["process"]["argv"][:-2]
    report["process"]["environment"] = {}
    adapter._write_json(path, report)
    config["server_runtime"]["before"]["sha256"] = adapter._sha_file(path)
    assert adapter._model_runtime(config, "instrument_off", "before")["instrumentation_mode"] == "instrument_off"
