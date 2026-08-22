import http.client
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.observability.request_proxy import JsonlWriter, ProxyServer


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
                body = b'{"messages":[{"role":"user","content":"private prompt"}]}'
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
            self.assertEqual(record["request_mutation"], False)
            self.assertNotIn("private prompt", record)
            self.assertNotIn("messages", record)


if __name__ == "__main__":
    unittest.main()
