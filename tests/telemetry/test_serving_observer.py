"""Focused ASGI observer and deferred-attribution acceptance tests."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from agentic_sim.telemetry.serving_observer import (
    ATTEMPT_ID_HEADER,
    CASE_ID_HEADER,
    ObserverFatalError,
    ServingObserver,
    ServingObserverError,
)
from scripts.observability.derive_server_attribution import (
    AttributionError,
    append_sidecar,
    derive_server_attribution,
    read_observer_journal,
)


def _scope(
    path: str = "/v1/completions",
    *,
    request_id: str | None = None,
    scrape_id: str | None = None,
    phase: str | None = None,
    method: str | None = None,
    observer_request: bool = False,
    case_id: str | None = None,
    attempt_id: str | None = None,
):
    headers = []
    if request_id is not None:
        headers.append((b"x-eic-physical-request-id", request_id.encode("utf-8")))
    if scrape_id is not None:
        headers.append((b"x-eic-scrape-id", scrape_id.encode("utf-8")))
    if phase is not None:
        headers.append((b"x-eic-scrape-phase", phase.encode("utf-8")))
    if observer_request:
        headers.append((b"x-eic-observer-request", b"1"))
    if case_id is not None:
        headers.append((CASE_ID_HEADER.encode("ascii"), case_id.encode("utf-8")))
    if attempt_id is not None:
        headers.append((ATTEMPT_ID_HEADER.encode("ascii"), attempt_id.encode("utf-8")))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": method or ("GET" if path == "/metrics" else "POST"),
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }


def _metrics(count: int, sums: tuple[float, float, float, float]) -> bytes:
    families = (
        "request_queue_time_seconds",
        "request_prefill_time_seconds",
        "request_decode_time_seconds",
        "e2e_request_latency_seconds",
    )
    lines: list[str] = []
    for family, total in zip(families, sums):
        name = "vllm:" + family
        lines.extend(
            [
                f"# TYPE {name} histogram",
                f'{name}_bucket{{engine="0",le="+Inf",model_name="fixture"}} {count}',
                f'{name}_count{{engine="0",model_name="fixture"}} {count}',
                f'{name}_sum{{engine="0",model_name="fixture"}} {total}',
            ]
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


async def _run(observer: ServingObserver, scope, messages):
    received = list(messages)
    sent = []

    async def receive():
        if received:
            return received.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await observer(scope, receive, send)
    return sent


def _observer(root: Path, app, **kwargs) -> ServingObserver:
    return ServingObserver(
        app,
        root / "observer.jsonl",
        artifact_dir=root / "artifacts",
        server_identity="fixture-server",
        lease_id="fixture-lease",
        counter_epoch="fixture-epoch",
        dedicated_server=True,
        source_hash="a" * 64,
        **kwargs,
    )


def _observed_pair(root: Path) -> Path:
    """Produce real observer evidence with two streamed metrics responses."""
    async def app(scope, receive, send):
        await receive()
        phase = dict(scope["headers"]).get(b"x-eic-scrape-phase")
        if phase == b"before":
            body = _metrics(0, (0., 0., 0., 0.))
        elif phase == b"after":
            body = _metrics(1, (.1, .2, .3, .6))
        else:
            body = b"ok"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body[:8], "more_body": True})
        await send({"type": "http.response.body", "body": body[8:], "more_body": False})

    async def exercise(observer):
        scopes = (
            _scope("/metrics", scrape_id="bound-before", phase="before", observer_request=True),
            _scope(request_id="bound-target"),
            _scope("/metrics", scrape_id="bound-after", phase="after", observer_request=True),
        )
        for scope in scopes:
            await _run(observer, scope, [{"type": "http.request", "body": b"", "more_body": False}])

    observer = _observer(root, app)
    try:
        asyncio.run(exercise(observer))
    finally:
        observer.close()
    return root / "observer.jsonl"


class _NativeProcessorFixture:
    """Exercise callback preservation; this is not a substitute vLLM install."""
    def __init__(self):
        self.calls = 0
        self.append_count = 1
        self.original_error = None
        self.finished_cached_tokens = None
        self.result = object()

    def process_outputs(
        self,
        req_state,
        finish_reason,
        iteration_stats,
        *,
        req_id=None,
        engine_core_output=None,
        num_cached_tokens=None,
    ):
        """Mimic the pinned vLLM finish caller's cache-bearing locals.

        ``capture_caller_cache`` intentionally accepts evidence only from this
        exact caller frame.  Keep the local names and object relationships
        aligned with the reviewed vLLM path so these tests exercise that
        boundary instead of calling the wrapped callback from a helper.
        """
        req_id = req_state.request_id if req_id is None else req_id
        if engine_core_output is None:
            engine_core_output = SimpleNamespace(
                request_id=req_id,
                num_cached_tokens=num_cached_tokens,
            )
        if num_cached_tokens is None:
            num_cached_tokens = getattr(engine_core_output, "num_cached_tokens", None)
        return self._update_stats_from_finished(req_state, finish_reason, iteration_stats)

    def _update_stats_from_finished(self, req_state, finish_reason, iteration_stats):
        self.calls += 1
        if self.original_error is not None:
            raise self.original_error
        if iteration_stats is None:
            return self.result
        for _ in range(self.append_count):
            iteration_stats.finished_requests.append(SimpleNamespace(
                finish_reason=finish_reason, e2e_latency=.6, queued_time=.1,
                prefill_time=.2, decode_time=.3, inference_time=.5,
                num_prompt_tokens=10, num_generation_tokens=2, max_tokens_param=8,
                cached_tokens=self.finished_cached_tokens,
            ))
        return self.result


@contextmanager
def _native_fixture(root, app):
    vllm = ModuleType("vllm")
    vllm.__version__ = "0.10.0"
    output = ModuleType("vllm.v1.engine.output_processor")
    output.OutputProcessor = _NativeProcessorFixture
    output.__file__ = __file__
    stats = ModuleType("vllm.v1.metrics.stats")
    stats.__file__ = __file__
    with patch.dict(sys.modules, {"vllm": vllm, output.__name__: output, stats.__name__: stats}), patch.dict(
        "os.environ", {"EIC_NATIVE_VLLM_OBSERVER": "true", "EIC_NATIVE_VLLM_JOURNAL": str(root / "native.jsonl")}
    ):
        observer = _observer(root, app, metrics_sampler=lambda: b"fixture unused")
        try:
            yield observer
        finally:
            observer.close()


def _native_scope(request_id):
    scope = _scope("/v1/chat/completions", request_id=request_id)
    scope["headers"].append((b"x-request-id", request_id.encode()))
    scope["state"] = {}
    return scope


def _native_state(request_id, parent=None):
    return SimpleNamespace(
        request_id=request_id, parent_req=parent,
        stats=SimpleNamespace(arrival_time=1_700_000_000., queued_ts=10., scheduled_ts=10.1,
                              first_token_ts=10.3, last_token_ts=10.6),
    )


def _native_app(processor, *, engine_id=None, parent=None, iterations=None):
    async def app(scope, receive, send):
        await receive()
        request_id = "chatcmpl-" + dict(scope["headers"])[b"x-request-id"].decode()
        scope["state"]["request_metadata"] = SimpleNamespace(request_id=request_id)
        native_state = _native_state(engine_id or request_id, parent)
        iteration = iterations if iterations is not None else SimpleNamespace(finished_requests=[])
        result = processor.process_outputs(
            native_state,
            "stop",
            iteration,
            req_id=native_state.request_id,
            engine_core_output=SimpleNamespace(
                request_id=native_state.request_id,
                num_cached_tokens=None,
            ),
            num_cached_tokens=None,
        )
        assert result is processor.result
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})
    return app


_USE_OUTPUT_CACHE = object()


def _native_finish_rows(
    root,
    processor,
    *,
    caller="process_outputs",
    req_state_id="chatcmpl-target",
    req_id=None,
    output_id=None,
    output_cache=None,
    output_has_cache=True,
    local_cache=_USE_OUTPUT_CACHE,
    finished_cache=None,
):
    """Run one request through the fixture's real callback caller and return raw rows."""
    processor.finished_cached_tokens = finished_cache

    async def app(scope, receive, send):
        await receive()
        iteration = SimpleNamespace(finished_requests=[])
        req_state = _native_state(req_state_id)
        if caller == "direct":
            processor._update_stats_from_finished(req_state, "stop", iteration)
        else:
            output = SimpleNamespace(request_id=output_id if output_id is not None else req_state_id)
            if output_has_cache:
                output.num_cached_tokens = output_cache
            pinned_cache = output_cache if local_cache is _USE_OUTPUT_CACHE else local_cache
            processor.process_outputs(
                req_state,
                "stop",
                iteration,
                req_id=req_id if req_id is not None else req_state_id,
                engine_core_output=output,
                num_cached_tokens=pinned_cache,
            )
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    with _native_fixture(root, app) as observer:
        sent = asyncio.run(_run(observer, _native_scope("target"), [
            {"type": "http.request", "body": b"", "more_body": False}
        ]))
        assert sent[-1]["body"] == b"ok"
    return [json.loads(line) for line in (root / "native.jsonl").read_bytes().splitlines()]


class NativeFinishedObserverTests(unittest.TestCase):
    def test_native_is_opt_in_and_source_pin_mismatch_does_not_install(self):
        async def app(scope, receive, send):
            pass
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.dict("os.environ", {"EIC_NATIVE_VLLM_OBSERVER": "false"}), patch(
                "agentic_sim.telemetry.native_vllm_observer.install", side_effect=AssertionError("must remain disabled")
            ):
                observer = _observer(root, app)
                self.assertIsNone(observer._native_observer)
                observer.close()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = _NativeProcessorFixture._update_stats_from_finished
            with patch.dict("os.environ", {"EIC_NATIVE_VLLM_EXPECTED_HOOK_SHA256": "0" * 64}):
                with self.assertRaisesRegex(ValueError, "source differs"):
                    with _native_fixture(root, app):
                        self.fail("source mismatch must reject installation")
            self.assertIs(_NativeProcessorFixture._update_stats_from_finished, original)

    def test_two_serial_requests_use_native_fields_without_aggregate_scrapes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processor = _NativeProcessorFixture()
            iterations = SimpleNamespace(finished_requests=[])
            with _native_fixture(root, _native_app(processor, iterations=iterations)) as observer:
                for request_id in ("native-one", "native-two"):
                    sent = asyncio.run(_run(observer, _native_scope(request_id),
                                           [{"type": "http.request", "body": b"prompt", "more_body": False}]))
                    self.assertEqual(sent[-1]["body"], b"ok")
                self.assertEqual(processor.calls, 2)
                self.assertEqual(len(iterations.finished_requests), 2)
                for request_id in ("native-one", "native-two"):
                    row = derive_server_attribution(journal=root / "observer.jsonl", request_id=request_id,
                                                    native_journal=root / "native.jsonl")
                    self.assertEqual(row["producer_status"], "measured", row.get("unavailable_reason"))
                    self.assertEqual(row["metrics"]["prefill"]["value_ms"], 200.)
                    self.assertIsNone(row["metrics"]["prefill"]["count_delta"])
                    self.assertFalse(row["native_count_delta_checks_reused"])
                    self.assertFalse(row["cuda_kernel_timing"])
                    self.assertEqual(row["native_measurement"]["engine_request_id"], "chatcmpl-" + request_id)
                    self.assertIsNone(row["native_measurement"]["finished"]["cached_tokens"])
                    self.assertEqual(
                        row["native_measurement"]["finished"]["cached_tokens_provenance"],
                        "unavailable_finished_request_stats_field_absent",
                    )

    def test_process_outputs_captures_zero_and_positive_engine_cache_counts(self):
        for cached in (0, 7):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                rows = _native_finish_rows(root, _NativeProcessorFixture(), output_cache=cached)
                finished = next(row for row in rows if row["record_type"] == "native_finished")
                self.assertEqual(finished["raw"]["finished"]["cached_tokens"], cached)
                self.assertEqual(
                    finished["raw"]["finished"]["cached_tokens_provenance"],
                    "engine_core_output.num_cached_tokens@pinned_process_outputs_finish_caller",
                )

    def test_cache_capture_rejects_wrong_caller_and_request_but_keeps_explicit_unknown(self):
        cases = (
            {"name": "wrong-caller", "caller": "direct"},
            {"name": "wrong-request", "req_id": "not-chatcmpl-target"},
            {"name": "missing", "output_cache": None, "output_has_cache": False},
        )
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as temporary:
                rows = _native_finish_rows(Path(temporary), _NativeProcessorFixture(), **{
                    key: value for key, value in case.items() if key != "name"
                })
                finished = next(row for row in rows if row["record_type"] == "native_finished")
                self.assertIsNone(finished["raw"]["finished"]["cached_tokens"])
                self.assertEqual(
                    finished["raw"]["finished"]["cached_tokens_provenance"],
                    "unavailable_finished_request_stats_field_absent",
                )

    def test_cache_capture_rejects_invalid_overprompt_and_disagreeing_values(self):
        cases = (
            {"name": "invalid-bool", "output_cache": True},
            {"name": "overprompt", "output_cache": 11},
            {"name": "disagree", "output_cache": 2, "finished_cache": 1},
        )
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as temporary:
                rows = _native_finish_rows(Path(temporary), _NativeProcessorFixture(), **{
                    key: value for key, value in case.items() if key != "name"
                })
                self.assertFalse(any(row["record_type"] == "native_finished" for row in rows))
                errors = [row for row in rows if row["record_type"] == "native_error"]
                self.assertEqual(len(errors), 1)
                if case["name"] == "invalid-bool":
                    self.assertIn("nonnegative integer", errors[0]["error"])
                elif case["name"] == "overprompt":
                    self.assertIn("exceeds prompt work", errors[0]["error"])
                else:
                    self.assertIn("disagree", errors[0]["error"])

    def test_native_missing_foreign_parented_and_duplicate_ids_fail_closed(self):
        for mode in ("foreign", "parented", "duplicate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                processor = _NativeProcessorFixture()
                kwargs = ({"engine_id": "unknown-engine"} if mode == "foreign" else
                          {"engine_id": "child-1", "parent": SimpleNamespace(request_id="chatcmpl-target")} if mode == "parented" else {})
                app = _native_app(processor, **kwargs)
                with _native_fixture(root, app) as observer:
                    asyncio.run(_run(observer, _native_scope("target"),
                                     [{"type": "http.request", "body": b"", "more_body": False}]))
                    if mode == "duplicate":
                        processor._update_stats_from_finished(_native_state("chatcmpl-target"), "stop",
                                                              SimpleNamespace(finished_requests=[]))
                    row = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                    native_journal=root / "native.jsonl")
                    self.assertEqual(row["producer_status"], "unavailable")
                    self.assertIn("missing, duplicated", row["unavailable_reason"])

    def test_native_append_count_error_preserves_original_result_and_response(self):
        for count in (0, 2):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                processor = _NativeProcessorFixture()
                processor.append_count = count
                with _native_fixture(root, _native_app(processor)) as observer:
                    sent = asyncio.run(_run(observer, _native_scope("target"),
                                           [{"type": "http.request", "body": b"", "more_body": False}]))
                    self.assertEqual(sent[-1]["body"], b"ok")
                    self.assertEqual(processor.calls, 1)
                    self.assertTrue(observer.healthy)
                    row = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                    native_journal=root / "native.jsonl")
                    self.assertEqual(row["producer_status"], "unavailable")
                    self.assertIn("append delta", row["unavailable_reason"])

    def test_native_original_exception_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            processor = _NativeProcessorFixture()
            processor.original_error = RuntimeError("original engine error")
            with _native_fixture(root, _native_app(processor)) as observer:
                with self.assertRaises(RuntimeError) as raised:
                    asyncio.run(_run(observer, _native_scope("target"),
                                     [{"type": "http.request", "body": b"", "more_body": False}]))
                self.assertIs(raised.exception, processor.original_error)
                self.assertEqual(processor.calls, 1)

    def test_native_durability_failure_preserves_http_and_blocks_native_measurement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with _native_fixture(root, _native_app(_NativeProcessorFixture())) as observer:
                with patch.object(observer._native_observer.journal, "append", side_effect=OSError("native fsync failed")):
                    sent = asyncio.run(_run(observer, _native_scope("target"),
                                           [{"type": "http.request", "body": b"", "more_body": False}]))
                self.assertEqual(sent[-1]["body"], b"ok")
                self.assertTrue(observer.healthy)
                self.assertIn("fsync", observer._native_observer.fatal_error)
                row = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                native_journal=root / "native.jsonl")
                self.assertEqual(row["producer_status"], "unavailable")

    def test_native_recovery_uses_new_sidecar_and_preserves_old_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with _native_fixture(root, _native_app(_NativeProcessorFixture())) as observer:
                asyncio.run(_run(observer, _native_scope("target"),
                                 [{"type": "http.request", "body": b"", "more_body": False}]))
                old = derive_server_attribution(journal=root / "observer.jsonl", request_id="target")
                self.assertEqual(old["producer_status"], "unavailable")
                old_path = root / "aggregate-sidecar.jsonl"
                append_sidecar(old_path, old)
                old_bytes = old_path.read_bytes()
                native = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                   native_journal=root / "native.jsonl")
                self.assertEqual(native["producer_status"], "measured")
                with self.assertRaisesRegex(AttributionError, "already contains"):
                    append_sidecar(old_path, native)
                append_sidecar(root / "native-sidecar.jsonl", native)
                self.assertEqual(old_bytes, old_path.read_bytes())

    def test_native_raw_hash_epoch_and_missing_watermark_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with _native_fixture(root, _native_app(_NativeProcessorFixture())) as observer:
                asyncio.run(_run(observer, _native_scope("target"),
                                 [{"type": "http.request", "body": b"", "more_body": False}]))
                source = (root / "native.jsonl").read_bytes()
                for mode in ("hash", "epoch", "watermark"):
                    with self.subTest(mode=mode):
                        rows = [json.loads(line) for line in source.splitlines()]
                        sample = next(r for r in rows if r["record_type"] == "native_finished")
                        if mode == "hash":
                            sample["raw"]["finished"]["prefill_time"] = 99.
                        elif mode == "epoch":
                            sample["counter_epoch"] = "wrong-epoch"
                        else:
                            rows = rows[:-1]
                        changed = root / (mode + ".jsonl")
                        changed.write_text("".join(json.dumps(row) + "\n" for row in rows))
                        row = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                        native_journal=changed)
                        self.assertEqual(row["producer_status"], "unavailable")
                        self.assertIn({"hash": "hash", "epoch": "identity", "watermark": "watermark"}[mode],
                                      row["unavailable_reason"])

    def test_native_missing_callback_stats_none_validation_and_disconnect_are_explicit(self):
        for mode in ("missing_callback", "stats_none", "validation_failure", "disconnect"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                processor = _NativeProcessorFixture()

                async def app(scope, receive, send):
                    await receive()
                    scope["state"]["request_metadata"] = SimpleNamespace(request_id="chatcmpl-target")
                    if mode == "stats_none":
                        result = processor._update_stats_from_finished(_native_state("chatcmpl-target"), "stop", None)
                        self.assertIs(result, processor.result)
                    if mode == "disconnect":
                        return
                    status = 400 if mode == "validation_failure" else 200
                    await send({"type": "http.response.start", "status": status, "headers": []})
                    await send({"type": "http.response.body", "body": b"unchanged", "more_body": False})

                with _native_fixture(root, app) as observer:
                    message = ({"type": "http.disconnect"} if mode == "disconnect" else
                               {"type": "http.request", "body": b"", "more_body": False})
                    sent = asyncio.run(_run(observer, _native_scope("target"), [message]))
                    if mode != "disconnect":
                        self.assertEqual(sent[-1]["body"], b"unchanged")
                    native_rows = [json.loads(line) for line in (root / "native.jsonl").read_bytes().splitlines()]
                    self.assertFalse(any(r["record_type"] == "native_finished" for r in native_rows))
                    reconciliation = [r for r in native_rows if r["record_type"] == "native_http_terminal"]
                    self.assertEqual(len(reconciliation), 1)
                    self.assertEqual(reconciliation[0]["native_status"], "unverified_requires_exact_finished_record")
                    row = derive_server_attribution(journal=root / "observer.jsonl", request_id="target",
                                                    native_journal=root / "native.jsonl")
                    self.assertEqual(row["producer_status"], "unavailable")


class ServingObserverTests(unittest.TestCase):
    def _assert_scrape_tamper_rejected(self, mutate, reason):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = _observed_pair(root)
            rows = [json.loads(line) for line in journal.read_bytes().splitlines()]
            terminal = next(row for row in rows
                            if row["record_type"] == "request_terminal" and row.get("scrape_id") == "bound-after")
            watermark = next(row for row in rows
                             if row["record_type"] == "completeness_watermark"
                             and row["sequence"] > terminal["sequence"])
            mutate(terminal, watermark)
            changed = root / "tampered.jsonl"
            changed.write_text("".join(json.dumps(row) + "\n" for row in rows))
            row = derive_server_attribution(
                journal=changed, request_id="bound-target",
                before_scrape_id="bound-before", after_scrape_id="bound-after",
            )
            self.assertEqual(row["producer_status"], "unavailable")
            self.assertIn(reason, row["unavailable_reason"])

    def test_asgi_scrape_start_must_equal_request_ingress(self):
        def mutate(terminal, watermark):
            terminal["metrics_capture"]["scrape_started_monotonic_ns"] -= 1
        self._assert_scrape_tamper_rejected(mutate, "start differs from request ingress")

    def test_asgi_scrape_future_end_rejected_even_with_covering_watermark(self):
        def mutate(terminal, watermark):
            future = terminal["terminal_monotonic_ns"] + 1_000_000_000
            terminal["metrics_capture"]["captured_monotonic_ns"] = future
            terminal["metrics_capture"]["scrape_ended_monotonic_ns"] = future
            # Keep the watermark internally valid, independently testing the
            # request-bound check rather than the watermark-time check.
            watermark["covered_through_monotonic_ns"] = future
            watermark["watermark_monotonic_ns"] = future + 1
        self._assert_scrape_tamper_rejected(mutate, "end differs from request terminal")

    def test_asgi_capture_must_match_successful_final_send(self):
        def mutate(terminal, watermark):
            capture = terminal["metrics_capture"]
            capture["captured_monotonic_ns"] = capture["scrape_started_monotonic_ns"] + 1
            self.assertNotEqual(capture["captured_monotonic_ns"], terminal["response_final_monotonic_ns"])
        self._assert_scrape_tamper_rejected(mutate, "capture differs from successful final response send")

    def test_watermark_cannot_cover_time_after_its_timestamp(self):
        def mutate(terminal, watermark):
            watermark["covered_through_monotonic_ns"] = watermark["watermark_monotonic_ns"] + 1
        self._assert_scrape_tamper_rejected(mutate, "watermark covers time after its own timestamp")

    def test_valid_asgi_scrape_bounds_and_equal_watermark_time_are_measured(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = _observed_pair(root)
            data = read_observer_journal(journal)
            for terminal in data.terminals.values():
                capture = terminal.get("metrics_capture")
                if capture:
                    start = data.starts[terminal["observation_id"]]
                    self.assertEqual(capture["scrape_started_monotonic_ns"], start["started_monotonic_ns"])
                    self.assertEqual(capture["scrape_ended_monotonic_ns"], terminal["terminal_monotonic_ns"])
                    self.assertEqual(capture["captured_monotonic_ns"], terminal["response_final_monotonic_ns"])
            rows = [dict(row) for row in data.records]
            for row in rows:
                if row["record_type"] == "completeness_watermark":
                    row["watermark_monotonic_ns"] = row["covered_through_monotonic_ns"]
            boundary = root / "equal-watermark.jsonl"
            boundary.write_text("".join(json.dumps(row) + "\n" for row in rows))
            for source in (journal, boundary):
                row = derive_server_attribution(
                    journal=source, request_id="bound-target",
                    before_scrape_id="bound-before", after_scrape_id="bound-after",
                )
                self.assertEqual(row["producer_status"], "measured", row.get("unavailable_reason"))

    def test_streaming_body_is_forwarded_byte_for_byte_and_hashed(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"left", "more_body": True})
            await send({"type": "http.response.body", "body": b"right", "more_body": False})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            sent = asyncio.run(
                _run(
                    observer,
                    _scope(request_id="req-stream", case_id="case-1", attempt_id="attempt-1"),
                    [{"type": "http.request", "body": b"input", "more_body": False}],
                )
            )
            observer.close()
            self.assertEqual(
                sent,
                [
                    {"type": "http.response.start", "status": 200, "headers": []},
                    {"type": "http.response.body", "body": b"left", "more_body": True},
                    {"type": "http.response.body", "body": b"right", "more_body": False},
                ],
            )
            data = read_observer_journal(root / "observer.jsonl")
            terminal = next(item for item in data.terminals.values() if item["physical_request_id"] == "req-stream")
            self.assertEqual(terminal["request_body_sha256"], hashlib.sha256(b"input").hexdigest())
            self.assertEqual(terminal["response_body_sha256"], hashlib.sha256(b"leftright").hexdigest())
            self.assertEqual(terminal["request_body_completeness"], "complete")
            self.assertEqual(terminal["response_body_completeness"], "complete")
            self.assertEqual(terminal["terminal_status"], "complete")
            self.assertEqual(terminal["case_id"], "case-1")
            self.assertEqual(terminal["attempt_id"], "attempt-1")

    def test_application_failure_is_terminal_and_does_not_become_a_measurement(self):
        async def app(scope, receive, send):
            await receive()
            raise RuntimeError("fixture application failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            with self.assertRaisesRegex(RuntimeError, "fixture application failure"):
                asyncio.run(_run(observer, _scope(request_id="req-fail"), [{"type": "http.request", "body": b"", "more_body": False}]))
            data = read_observer_journal(root / "observer.jsonl")
            terminal = next(item for item in data.terminals.values() if item["physical_request_id"] == "req-fail")
            self.assertEqual(terminal["terminal_status"], "failed")
            self.assertIn("fixture application failure", terminal["error"])
            self.assertTrue(data.watermarks)
            observer.close()

    def test_listener_cancellation_after_final_send_preserves_complete_response(self):
        for before_final, propagate in ((False, False), (False, True), (True, False)):
            with self.subTest(before_final=before_final, propagate=propagate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                async def app(scope, receive, send):
                    await receive()
                    listener = asyncio.create_task(receive())
                    await asyncio.sleep(0)
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    if not before_final:
                        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})
                    listener.cancel()
                    try:
                        await listener
                    except asyncio.CancelledError:
                        if propagate:
                            raise
                    if before_final:
                        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})

                observer = _observer(root, app)

                async def exercise():
                    first = True

                    async def receive():
                        nonlocal first
                        if first:
                            first = False
                            return {"type": "http.request", "body": b"input", "more_body": False}
                        await asyncio.Future()

                    async def send(message):
                        pass

                    await observer(_scope(request_id="cleanup"), receive, send)

                if propagate:
                    with self.assertRaises(asyncio.CancelledError):
                        asyncio.run(exercise())
                else:
                    asyncio.run(exercise())
                observer.close()
                terminal = next(iter(read_observer_journal(root / "observer.jsonl").terminals.values()))
                self.assertTrue(terminal["response_body_complete"])
                self.assertEqual(terminal["response_body_sha256"], hashlib.sha256(b"data: [DONE]\n\n").hexdigest())
                if before_final:
                    self.assertEqual(terminal["terminal_status"], "failed")
                    self.assertEqual(terminal["error_phase"], "request_receive")
                    self.assertIsNone(terminal["postcompletion_receive_cancelled_ns"])
                else:
                    self.assertEqual(terminal["terminal_status"], "complete")
                    self.assertFalse(terminal["client_disconnected"])
                    self.assertIsNone(terminal["error"])
                    self.assertGreaterEqual(terminal["postcompletion_receive_cancelled_ns"], terminal["response_final_monotonic_ns"])
                    self.assertEqual(terminal["postcompletion_application_cancelled_ns"] is not None, propagate)

    def test_disconnect_before_and_after_final_send_remain_distinct(self):
        for before_final in (True, False):
            with self.subTest(before_final=before_final), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)

                async def app(scope, receive, send):
                    await receive()
                    await send({"type": "http.response.start", "status": 200, "headers": []})
                    if before_final:
                        await receive()
                    await send({"type": "http.response.body", "body": b"done", "more_body": False})
                    if not before_final:
                        await receive()

                observer = _observer(root, app)
                asyncio.run(_run(observer, _scope(request_id="disconnect-order"), [{"type": "http.request", "body": b"", "more_body": False}]))
                observer.close()
                terminal = next(iter(read_observer_journal(root / "observer.jsonl").terminals.values()))
                self.assertEqual(terminal["terminal_status"], "disconnected" if before_final else "complete")
                self.assertEqual(terminal["client_disconnected"], before_final)
                self.assertEqual(terminal["postcompletion_disconnect_ns"] is not None, not before_final)

    def test_disconnect_is_recorded_without_inventing_a_response(self):
        async def app(scope, receive, send):
            message = await receive()
            self.assertEqual(message["type"], "http.disconnect")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            asyncio.run(_run(observer, _scope(request_id="req-disconnect"), [{"type": "http.disconnect"}]))
            data = read_observer_journal(root / "observer.jsonl")
            terminal = next(item for item in data.terminals.values() if item["physical_request_id"] == "req-disconnect")
            self.assertEqual(terminal["terminal_status"], "disconnected")
            self.assertTrue(terminal["client_disconnected"])
            self.assertEqual(terminal["response_body_completeness"], "not_started")
            observer.close()

    def test_concurrent_foreign_route_gets_its_own_observation_and_blocks_attribution(self):
        async def app(scope, receive, send):
            await receive()
            await asyncio.sleep(0.01)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def run_both(observer):
            return await asyncio.gather(
                _run(observer, _scope(request_id="req-target"), [{"type": "http.request", "body": b"a", "more_body": False}]),
                _run(observer, _scope("/health"), [{"type": "http.request", "body": b"", "more_body": False}]),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            asyncio.run(run_both(observer))
            data = read_observer_journal(root / "observer.jsonl")
            starts = list(data.starts.values())
            self.assertEqual({item["request_class"] for item in starts}, {"model", "foreign"})
            self.assertEqual(len({item["observation_id"] for item in starts}), 2)
            foreign = next(item for item in starts if item["request_class"] == "foreign")
            self.assertIsNone(foreign["physical_request_id"])
            observer.close()

    def test_known_health_get_is_observer_traffic_without_header(self):
        async def app(scope, receive, send):
            await receive()
            await asyncio.sleep(0)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def exercise(observer):
            await asyncio.gather(
                _run(observer, _scope("/health", method="GET"),
                     [{"type": "http.request", "body": b"", "more_body": False}]),
                _run(observer, _scope(request_id="health-target"),
                     [{"type": "http.request", "body": b"prompt", "more_body": False}]),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            observer.capture_registry_snapshot(
                scrape_id="health-before", phase="before", sampler=lambda: _metrics(0, (0., 0., 0., 0.))
            )
            asyncio.run(exercise(observer))
            observer.capture_registry_snapshot(
                scrape_id="health-after", phase="after", sampler=lambda: _metrics(1, (.1, .2, .3, .6))
            )
            data = read_observer_journal(root / "observer.jsonl")
            self.assertEqual(next(iter(data.starts.values()))["request_class"], "observer")
            row = derive_server_attribution(
                journal=root / "observer.jsonl", request_id="health-target",
                before_scrape_id="health-before", after_scrape_id="health-after",
            )
            self.assertEqual(row["producer_status"], "measured")
            observer.close()

    def test_late_prior_publication_and_death_before_watermark_are_unavailable(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        for prior in (False, True):
            with self.subTest(prior=prior), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                observer = _observer(root, app)
                if prior:
                    asyncio.run(_run(observer, _scope(request_id="prior"),
                                     [{"type": "http.request", "body": b"old", "more_body": False}]))
                observer.capture_registry_snapshot(
                    scrape_id="baseline", phase="before", sampler=lambda: _metrics(0, (0., 0., 0., 0.))
                )
                asyncio.run(_run(observer, _scope(request_id="target"),
                                 [{"type": "http.request", "body": b"new", "more_body": False}]))
                observer.capture_registry_snapshot(
                    scrape_id="final", phase="after", sampler=lambda: _metrics(1, (.1, .2, .3, .6))
                )
                journal = root / "observer.jsonl"
                if not prior:
                    # Simulate a durable prefix ending at the after sample,
                    # with process death before its completeness watermark.
                    journal = root / "death-prefix.jsonl"
                    lines = (root / "observer.jsonl").read_bytes().splitlines(keepends=True)
                    journal.write_bytes(b"".join(lines[:-1]))
                row = derive_server_attribution(
                    journal=journal, request_id="target", before_scrape_id="baseline", after_scrape_id="final"
                )
                self.assertEqual(row["producer_status"], "unavailable")
                self.assertIn("late prior" if prior else "watermark", row["unavailable_reason"])
                observer.close()

    def test_observer_header_cannot_downgrade_foreign_inference_post(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        raw_before = _metrics(0, (0.0, 0.0, 0.0, 0.0))
        raw_after = _metrics(1, (0.1, 0.2, 0.3, 0.6))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            observer.capture_registry_snapshot(
                scrape_id="before-post-foreign",
                phase="before",
                sampler=lambda: raw_before,
            )
            foreign_scope = _scope("/v1/completions", observer_request=True)
            asyncio.run(_run(observer, foreign_scope, [{"type": "http.request", "body": b"foreign", "more_body": False}]))
            asyncio.run(_run(observer, _scope(request_id="req-post-target"), [{"type": "http.request", "body": b"target", "more_body": False}]))
            observer.capture_registry_snapshot(
                scrape_id="after-post-foreign",
                phase="after",
                sampler=lambda: raw_after,
            )
            data = read_observer_journal(root / "observer.jsonl")
            foreign = next(item for item in data.starts.values() if item["request_class"] == "foreign")
            self.assertIsNone(foreign["physical_request_id"])
            row = derive_server_attribution(
                journal=root / "observer.jsonl",
                request_id="req-post-target",
                before_scrape_id="before-post-foreign",
                after_scrape_id="after-post-foreign",
            )
            self.assertEqual(row["producer_status"], "unavailable")
            self.assertIn("foreign request", row["unavailable_reason"])
            observer.close()

    def test_unknown_get_with_observer_header_remains_foreign(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            asyncio.run(
                _run(
                    observer,
                    _scope("/unknown", method="GET", observer_request=True),
                    [{"type": "http.request", "body": b"", "more_body": False}],
                )
            )
            data = read_observer_journal(root / "observer.jsonl")
            self.assertEqual(next(iter(data.starts.values()))["request_class"], "foreign")
            observer.close()

    def test_default_registry_binding_is_explicitly_unavailable_without_actual_helper(self):
        async def app(scope, receive, send):
            await receive()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch(
                "agentic_sim.telemetry.serving_observer.make_prometheus_registry_sampler",
                side_effect=ServingObserverError("fixture has no actual vLLM registry"),
            ):
                observer = _observer(root, app)
                record = observer.capture_registry_snapshot(
                    scrape_id="unavailable-registry",
                    phase="before",
                )
            self.assertEqual(record["sample_status"], "unavailable")
            self.assertIn("no actual vLLM registry", record["sample_error"])
            observer.close()

    def test_registry_samples_over_byte_limit_are_unavailable_without_artifact(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def sample_after_request(observer):
            await _run(observer, _scope(request_id="size-target"),
                       [{"type": "http.request", "body": b"", "more_body": False}])
            await asyncio.gather(*tuple(observer._tasks))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app, max_metrics_bytes=16, metrics_sampler=lambda: b"x" * 17,
                                 postcompletion_sample_delays=(0.,))
            row = observer.capture_registry_snapshot(scrape_id="too-large", phase="before",
                                                     sampler=lambda: b"x" * 17)
            self.assertEqual(row["sample_status"], "unavailable")
            self.assertIn("max_metrics_bytes", row["sample_error"])
            asyncio.run(sample_after_request(observer))
            data = read_observer_journal(root / "observer.jsonl")
            post = next(row for row in data.metric_records if row["record_type"] == "postcompletion_metrics")
            self.assertEqual(post["sample_status"], "unavailable")
            self.assertIn("max_metrics_bytes", post["sample_error"])
            self.assertEqual(list((root / "artifacts").rglob("*.prom")), [])
            observer.close()

    def test_delayed_postcompletion_sample_is_bounded_and_durable(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        async def sampler():
            await asyncio.sleep(0.005)
            return _metrics(1, (0.1, 0.2, 0.3, 0.6))

        async def exercise(observer):
            await _run(observer, _scope(request_id="req-delayed"), [{"type": "http.request", "body": b"", "more_body": False}])
            await asyncio.sleep(0.03)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(
                root,
                app,
                metrics_sampler=sampler,
                postcompletion_sample_delays=(0.001,),
            )
            asyncio.run(exercise(observer))
            data = read_observer_journal(root / "observer.jsonl")
            samples = [item for item in data.metric_records if item["record_type"] == "postcompletion_metrics"]
            self.assertEqual(len(samples), 1)
            self.assertEqual(samples[0]["sample_status"], "complete")
            artifact = Path(samples[0]["raw_path"])
            self.assertEqual(artifact.read_bytes(), _metrics(1, (0.1, 0.2, 0.3, 0.6)))
            observer.close()

    def test_observed_metrics_route_and_deferred_sidecar_reuse_native_checks(self):
        before_raw = _metrics(0, (0.0, 0.0, 0.0, 0.0))
        after_raw = _metrics(1, (0.1, 0.2, 0.3, 0.6))

        async def app(scope, receive, send):
            await receive()
            body = before_raw if scope["path"] == "/metrics" and scope.get("_phase") == "before" else after_raw
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body[:20], "more_body": True})
            await send({"type": "http.response.body", "body": body[20:], "more_body": False})

        # A normal ASGI scope has no private phase field.  The fixture app
        # chooses the response by the explicit scrape ID instead.
        async def route_app(scope, receive, send):
            await receive()
            body = before_raw if any(value == b"before" for name, value in scope["headers"] if name == b"x-eic-scrape-phase") else after_raw
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": body, "more_body": False})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, route_app)
            asyncio.run(_run(observer, _scope("/metrics", scrape_id="scrape-before", phase="before"), [{"type": "http.request", "body": b"", "more_body": False}]))
            asyncio.run(_run(observer, _scope(request_id="req-native"), [{"type": "http.request", "body": b"prompt", "more_body": False}]))
            asyncio.run(_run(observer, _scope("/metrics", scrape_id="scrape-after", phase="after"), [{"type": "http.request", "body": b"", "more_body": False}]))
            row = derive_server_attribution(
                journal=root / "observer.jsonl",
                request_id="req-native",
                before_scrape_id="scrape-before",
                after_scrape_id="scrape-after",
            )
            self.assertEqual(row["producer_status"], "measured")
            self.assertEqual(row["native_count_delta_checks_reused"], True)
            self.assertEqual(row["scrapes"]["before"]["raw_sha256"], hashlib.sha256(before_raw).hexdigest())
            self.assertEqual(row["scrapes"]["after"]["raw_sha256"], hashlib.sha256(after_raw).hexdigest())
            observer.close()

    def test_clock_epoch_and_missing_sequence_fail_closed(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            asyncio.run(_run(observer, _scope(request_id="req-clock"), [{"type": "http.request", "body": b"", "more_body": False}]))
            observer.close()
            source = root / "observer.jsonl"
            rows = [json.loads(line) for line in source.read_text().splitlines()]

            missing = root / "missing.jsonl"
            missing_rows = [dict(row) for row in rows]
            missing_rows[1]["sequence"] = 99
            missing.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in missing_rows))
            with self.assertRaisesRegex(AttributionError, "sequence"):
                read_observer_journal(missing)

            for field, expected in (("clock_id", "CLOCK_MONOTONIC"), ("counter_epoch", "other-epoch")):
                changed = root / (field + ".jsonl")
                changed_rows = [dict(row) for row in rows]
                if field == "clock_id":
                    changed_rows[1]["clock"] = dict(changed_rows[1]["clock"])
                    changed_rows[1]["clock"][field] = expected
                else:
                    changed_rows[1][field] = expected
                changed.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in changed_rows))
                row = derive_server_attribution(journal=changed, request_id="req-clock")
                self.assertEqual(row["producer_status"], "unavailable")
                self.assertRegex(row["unavailable_reason"], "identity|epoch|clock")

    def test_journal_durability_failure_marks_observer_fatal(self):
        async def app(scope, receive, send):
            await receive()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok", "more_body": False})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            observer = _observer(root, app)
            original = observer.journal.append
            calls = {"count": 0}

            def fail_after_header(record):
                calls["count"] += 1
                if calls["count"] == 2:
                    raise OSError("fixture fsync failure")
                return original(record)

            observer.journal.append = fail_after_header
            with self.assertRaises(ObserverFatalError):
                asyncio.run(_run(observer, _scope(request_id="req-fatal"), [{"type": "http.request", "body": b"", "more_body": False}]))
            self.assertIsNotNone(observer.fatal_error)
            observer.close()


if __name__ == "__main__":
    unittest.main()
