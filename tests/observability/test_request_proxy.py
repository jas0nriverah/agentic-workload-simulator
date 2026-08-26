import http.client
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.observability.request_proxy import JsonlWriter, ProxyServer


class AdaptiveRuntimeFake:
    """Small durable fake for proxy ordering and failure tests."""

    def __init__(self, root: Path, *, fail_prediction: bool = False):
        self.root = root
        self.marker = root / "prediction.durable"
        self.records: list[dict[str, object]] = []
        self.fail_prediction = fail_prediction
        self.predicted = False
        self.revealed = False

    def predict_model_request(self, request_id: str, body: bytes) -> dict[str, object]:
        if self.fail_prediction:
            raise RuntimeError("synthetic prediction failure")
        # The fake intentionally derives no record from body content.  The
        # marker is fsync'd before returning, matching the production contract.
        self.marker.write_text(request_id, encoding="utf-8")
        with self.marker.open("rb") as handle:
            os.fsync(handle.fileno())
        self.predicted = True
        self.records.append({"kind": "prediction", "request_id": request_id})
        return {"record_sha256": "a" * 64}

    def reveal_model_request(self, request_id: str, **kwargs: object) -> dict[str, object]:
        self.revealed = True
        record = {
            "kind": "label",
            "request_id": request_id,
            "status": "unavailable" if kwargs.get("unavailable_reason") else "completed",
            **kwargs,
        }
        self.records.append(record)
        return {"record_sha256": "b" * 64, **record}


class AdaptiveUpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.server.events.append(("upstream_seen", self.server.adaptive_marker.exists()))
        time.sleep(0.03)
        response = json.dumps(
            {"ok": True, "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format, *_args):
        return


class CountingUpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.dispatch_count += 1
        self.send_response(500)
        self.end_headers()

    def log_message(self, _format, *_args):
        return


def _start_proxy(upstream_port: int, events: Path, runtime=None):
    proxy = ProxyServer(
        ("127.0.0.1", 0),
        upstream_host="127.0.0.1",
        upstream_port=upstream_port,
        writer=JsonlWriter(events),
        timeout_seconds=1,
        max_body_bytes=4096,
        adaptive_runtime=runtime,
    )
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    return proxy, thread


def _post(proxy: ProxyServer, body: bytes) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
    connection.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    response_body = response.read()
    connection.close()
    return response.status, response_body


class UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        response = json.dumps(
            {"ok": True, "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)
        self.server.received_body = body
        self.server.received_request_id = self.headers.get("X-EIC-Request-ID")

    def log_message(self, _format, *_args):
        return


class RequestProxyTests(unittest.TestCase):
    def test_forwards_without_recording_payloads(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        with TemporaryDirectory() as temporary:
            events = Path(temporary) / "request_events.jsonl"
            proxy = ProxyServer(
                ("127.0.0.1", 0),
                upstream_host="127.0.0.1",
                upstream_port=upstream.server_address[1],
                writer=JsonlWriter(events),
                timeout_seconds=2,
                max_body_bytes=1024,
            )
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
            upstream_thread.start()
            proxy_thread.start()
            try:
                body = b'{"messages":[{"role":"user","content":"private prompt"}],"max_tokens":128,"temperature":0.2}'
                connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=2)
                connection.request("POST", "/v1/chat/completions", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                response_body = response.read()
                connection.close()
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response_body)["usage"]["total_tokens"], 10)
            self.assertEqual(upstream.received_body, body)
            self.assertTrue(upstream.received_request_id.startswith("request-"))
            record = json.loads(events.read_text(encoding="utf-8"))
            self.assertEqual(record["event_type"], "model_request_boundary")
            self.assertEqual(record["status_code"], 200)
            self.assertEqual(record["prompt_tokens"], 7)
            self.assertEqual(record["completion_tokens"], 3)
            self.assertEqual(record["max_output_tokens"], 128)
            self.assertEqual(record["temperature"], 0.2)
            self.assertEqual(record["request_mutation"], False)
            self.assertNotIn("private prompt", record)
            self.assertNotIn("messages", record)

    def test_adaptive_prediction_is_durable_before_dispatch_and_reveals_after_response(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), AdaptiveUpstreamHandler)
        upstream.events = []
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root)
            upstream.adaptive_marker = runtime.marker
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(upstream.server_address[1], events, runtime)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"secret prompt text"}],"max_tokens":32}',
                )
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 200)
            self.assertEqual(json.loads(response_body)["usage"]["total_tokens"], 7)
            self.assertEqual(upstream.events, [("upstream_seen", True)])
            self.assertEqual([record["kind"] for record in runtime.records], ["prediction", "label"])
            self.assertTrue(runtime.revealed)
            self.assertEqual(runtime.records[1]["status"], "completed")
            self.assertGreaterEqual(runtime.records[1]["observed_ms"], 20)
            proxy_record = json.loads(events.read_text(encoding="utf-8"))
            self.assertTrue(proxy_record["prediction_durable_before_upstream"])
            self.assertIsNotNone(proxy_record["adaptive_prediction_record_sha256"])
            self.assertIsNotNone(proxy_record["adaptive_label_record_sha256"])
            serialized = events.read_text(encoding="utf-8") + json.dumps(runtime.records, sort_keys=True)
            self.assertNotIn("secret prompt text", serialized)
            self.assertNotIn("messages", serialized)

    def test_adaptive_prediction_failure_prevents_upstream_dispatch(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstreamHandler)
        upstream.dispatch_count = 0
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root, fail_prediction=True)
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(upstream.server_address[1], events, runtime)
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                status, _response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"must not dispatch"}],"max_tokens":16}',
                )
            finally:
                proxy.shutdown()
                upstream.shutdown()
                proxy.server_close()
                upstream.server_close()
                proxy_thread.join(timeout=2)
                upstream_thread.join(timeout=2)

            self.assertEqual(status, 500)
            self.assertEqual(upstream.dispatch_count, 0)
            self.assertFalse(runtime.predicted)
            record = json.loads(events.read_text(encoding="utf-8"))
            self.assertEqual(record["error"], "RuntimeError")
            self.assertIsNone(record["adaptive_prediction_record_sha256"])

    def test_upstream_connection_failure_reveals_prediction_as_unavailable(self):
        probe = ThreadingHTTPServer(("127.0.0.1", 0), CountingUpstreamHandler)
        unused_port = probe.server_address[1]
        probe.server_close()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = AdaptiveRuntimeFake(root)
            events = root / "request_events.jsonl"
            proxy, proxy_thread = _start_proxy(unused_port, events, runtime)
            try:
                status, _response_body = _post(
                    proxy,
                    b'{"messages":[{"role":"user","content":"connection failure prompt"}],"max_tokens":16}',
                )
            finally:
                proxy.shutdown()
                proxy.server_close()
                proxy_thread.join(timeout=2)

            self.assertEqual(status, 502)
            self.assertTrue(runtime.predicted)
            self.assertTrue(runtime.revealed)
            self.assertEqual(runtime.records[1]["status"], "unavailable")
            self.assertTrue(runtime.records[1]["unavailable_reason"].startswith("upstream_"))
            serialized = events.read_text(encoding="utf-8") + json.dumps(runtime.records, sort_keys=True)
            self.assertNotIn("connection failure prompt", serialized)


if __name__ == "__main__":
    unittest.main()
