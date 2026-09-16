"""CPU-only fixture tests; simulated callbacks are not live vLLM evidence."""
import asyncio
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.validation import check_live_native_serving as check
from tests.telemetry.test_serving_observer import (
    _NativeProcessorFixture, _native_fixture, _native_state, _run, _scope,
)


PREFIX = b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
DONE = b'data: [DONE]\n\n'
ERROR = b'{"error":"invalid messages"}'


def build_batch(root, kind, *, actual_disconnect=True, overlap=True, missing_native=False):
    source, client = root / "source", root / "client"
    source.mkdir()
    client.mkdir()
    plan = check.prepare("http://unused.invalid:18222", "test-only", [kind])
    case = plan["cases"][0]
    physical = case["physical_request_id"]
    processor = _NativeProcessorFixture()
    response = ERROR if kind == "validation_failure" else PREFIX + DONE
    if kind == "stream_disconnect" and actual_disconnect:
        response = PREFIX
    status = 400 if kind == "validation_failure" else 200
    foreign_response = None

    def scope_for(spec, target=False):
        scope = _scope("/v1/chat/completions")
        scope["headers"] = [(k.lower().encode(), v.encode()) for k, v in spec["headers"].items()]
        scope["state"] = {}
        return scope

    async def invoke_foreign():
        nonlocal foreign_response
        foreign_response = await _run(observer, scope_for(case["foreign"]), [
            {"type": "http.request", "body": check.encoded(case["foreign"]["body"]), "more_body": False}])

    async def app(scope, receive, send):
        await receive()
        headers = dict(scope["headers"])
        if scope["path"] == "/metrics":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"# test metrics\n", "more_body": False})
            return
        if b"x-eic-physical-request-id" not in headers:
            await send({"type": "http.response.start", "status": 422, "headers": []})
            await send({"type": "http.response.body", "body": ERROR, "more_body": False})
            return
        if kind == "validation_failure":
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": ERROR, "more_body": False})
            return
        scope["state"]["request_metadata"] = SimpleNamespace(request_id="chatcmpl-" + physical)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": PREFIX, "more_body": True})
        if kind == "foreign_overlap" and overlap:
            await invoke_foreign()
        if not missing_native:
            processor._update_stats_from_finished(_native_state("chatcmpl-" + physical), "stop",
                                                   SimpleNamespace(finished_requests=[]))
        if kind == "stream_disconnect" and actual_disconnect:
            await receive()  # _run returns http.disconnect after the body.
        else:
            await send({"type": "http.response.body", "body": DONE, "more_body": False})

    with _native_fixture(source, app) as observer:
        asyncio.run(_run(observer, scope_for(case, True), [
            {"type": "http.request", "body": check.encoded(case["body"]), "more_body": False}]))
        if kind == "foreign_overlap" and not overlap:
            asyncio.run(invoke_foreign())
        # A repeated selected raw reference must not duplicate a whole prefix or payload.
        asyncio.run(_run(observer, _scope("/metrics", scrape_id="fixture-scrape"), [
            {"type": "http.request", "body": b"", "more_body": False}]))

    def client_row(spec, stem, body, code):
        request = check.encoded(spec["body"])
        check.write_new(client / (stem + ".request.bin"), request)
        check.write_new(client / (stem + ".response.bin"), body)
        return {"request_file": stem + ".request.bin", "request_sha256": check.digest(request),
                "response_file": stem + ".response.bin", "response_sha256": check.digest(body),
                "response_bytes": len(body), "response_complete": True, "status": code,
                "client_closed_early": False, "error": None}

    row = client_row(case, "0", PREFIX if kind == "stream_disconnect" else response, status)
    row.update(scenario=kind, submitted_physical_request_id=physical)
    if kind == "stream_disconnect":
        row.update(client_closed_early=True, response_complete=False)
    if foreign_response is not None:
        row["foreign"] = client_row(case["foreign"], "0-foreign", ERROR, 422)
    plan_raw = check.encoded(plan)
    check.write_new(client / "plan.json", plan_raw)
    check.write_new(client / "run.json", check.encoded({"plan_sha256": check.digest(plan_raw),
                    "gpu_window_id": "CPU-only-unittest", "cases": [row]}))
    archive = root / "batch.tar"
    with archive.open("xb") as stream:
        check.capture_archive(source / "observer.jsonl", source / "native.jsonl", stream)
    return client, archive, source


class LiveServingFixtureTests(unittest.TestCase):
    def test_local_http_runner_retains_stream_and_failure_and_closes_once(self):
        received = []
        foreign_seen = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                body = json.loads(raw)
                received.append((dict(self.headers), raw))
                invalid = isinstance(body["messages"], str)
                if invalid:
                    self.send_response(422)
                    self.send_header("Content-Length", str(len(ERROR)))
                    self.end_headers()
                    self.wfile.write(ERROR)
                    if self.headers.get("X-EIC-Observer-Request"):
                        foreign_seen.set()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(PREFIX + DONE)))
                self.end_headers()
                self.wfile.write(PREFIX)
                self.wfile.flush()
                if self.headers.get("X-EIC-Case-ID") == "foreign_overlap":
                    foreign_seen.wait(2)
                try:
                    self.wfile.write(DONE)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                plan = check.prepare(f"http://127.0.0.1:{server.server_port}", "CPU-only", check.SCENARIOS)
                check.write_new(root / "plan.json", check.encoded(plan))
                report = check.run(root / "plan.json", root / "client", "CPU-only-unittest", timeout=3)
                rows = {r["scenario"]: r for r in report["cases"]}
                self.assertEqual(rows["validation_failure"]["status"], 422)
                self.assertTrue(rows["stream_disconnect"]["client_closed_early"])
                self.assertTrue(rows["stream_complete"]["response_complete"])
                self.assertEqual(rows["foreign_overlap"]["foreign"]["status"], 422)
                self.assertEqual(len(received), 5)  # Four targets, one invalid foreign POST; no retries.
                for row in rows.values():
                    raw = (root / "client" / row["response_file"]).read_bytes()
                    self.assertEqual(check.digest(raw), row["response_sha256"])
                for headers, raw in received:
                    if "X-EIC-Observer-Request" not in headers:
                        self.assertEqual(headers["X-Request-Id"], headers["X-EIC-Physical-Request-ID"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_prepare_has_no_network_and_keeps_foreign_header_untrusted(self):
        with patch("socket.create_connection", side_effect=AssertionError("network not allowed")):
            plan = check.prepare("http://localhost:18222", "fixture", proxy_url="http://localhost:18000")
        cases = {c["scenario"]: c for c in plan["cases"]}
        self.assertEqual(cases["validation_failure"]["transport"], "proxy")
        self.assertEqual(cases["stream_disconnect"]["transport"], "direct")
        foreign = cases["foreign_overlap"]["foreign"]
        self.assertEqual(foreign["headers"]["X-EIC-Observer-Request"], "1")
        self.assertNotIn("X-EIC-Physical-Request-ID", foreign["headers"])
        self.assertNotIn("X-Request-Id", foreign["headers"])
        self.assertIsInstance(foreign["body"]["messages"], str)

    def test_positive_stream_and_three_negative_fixtures_have_actual_server_evidence(self):
        for kind in check.SCENARIOS:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client, archive, source = build_batch(root, kind)
                report = check.validate(client, archive, root / "review")
                self.assertEqual(report["status"], "passed", report)
                self.assertEqual(report["cases"][0]["native_status"],
                                 "measured" if kind == "stream_complete" else "unavailable")
                if kind == "stream_disconnect":
                    self.assertEqual(len(report["cases"][0]["native_partial_records"]), 1)
                manifest = report["archive_manifest"]
                for original in manifest["files"]:
                    restored = check.resolve_artifact(manifest, root / "review/server", original)
                    self.assertEqual(restored.read_bytes(), Path(original).read_bytes())
                self.assertEqual(len(list((root / "review").rglob("observer.jsonl"))), 1)

    def test_client_disconnect_after_server_finish_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, archive, _ = build_batch(root, "stream_disconnect", actual_disconnect=False)
            report = check.validate(client, archive, root / "review")
            self.assertEqual(report["status"], "failed")

    def test_nonoverlapping_foreign_request_does_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, archive, _ = build_batch(root, "foreign_overlap", overlap=False)
            report = check.validate(client, archive, root / "review")
            self.assertEqual(report["status"], "failed")

    def test_unavailable_missing_native_is_not_foreign_isolation_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, archive, _ = build_batch(root, "foreign_overlap", missing_native=True)
            report = check.validate(client, archive, root / "review")
            self.assertEqual(report["status"], "failed")
            self.assertIn("not proved", report["cases"][0]["reason"])

    def test_native_clock_epoch_gap_and_fatal_do_not_pass_expected_failure(self):
        for corruption in ("counter_epoch", "sequence", "native_error", "watermark"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                client, _, source = build_batch(root, "validation_failure")
                path = source / "native.jsonl"
                rows = check.json_rows(path.read_bytes())
                if corruption == "native_error":
                    rows[-1]["record_type"] = "native_error"
                    rows[-1]["error"] = "fixture durability error"
                elif corruption == "watermark":
                    rows = [r for r in rows if r["record_type"] != "native_watermark"]
                    for seq, row in enumerate(rows, 1):
                        row["sequence"] = seq
                else:
                    rows[1][corruption] = "different" if corruption == "counter_epoch" else 9000
                path.write_bytes(b"".join(check.encoded(r) for r in rows))
                with (root / "changed.tar").open("xb") as stream:
                    check.capture_archive(source / "observer.jsonl", path, stream)
                report = check.validate(client, root / "changed.tar", root / "review")
                self.assertEqual(report["status"], "failed", report)

    def test_truncated_tail_is_preserved_and_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client, _, source = build_batch(root, "validation_failure")
            path = source / "native.jsonl"
            with path.open("ab") as stream:
                stream.write(b'{"incomplete":')
            with (root / "partial.tar").open("xb") as stream:
                manifest = check.capture_archive(source / "observer.jsonl", path, stream)
            self.assertFalse(manifest["complete_jsonl_prefixes"])
            report = check.validate(client, root / "partial.tar", root / "review")
            self.assertEqual(report["status"], "failed")
            restored = check.resolve_artifact(manifest, root / "review/server", str(path))
            self.assertEqual(restored.read_bytes(), path.read_bytes())

    def test_archive_byte_bound_and_raw_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, source = build_batch(root, "validation_failure")
            with self.assertRaisesRegex(ValueError, "byte limit"):
                check.capture_archive(source / "observer.jsonl", source / "native.jsonl", io.BytesIO(), limit=1)
            raw = next((source / "artifacts/metrics").glob("*.prom"))
            raw.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "journal hash"):
                check.capture_archive(source / "observer.jsonl", source / "native.jsonl", io.BytesIO())
        for path in ("relative", "/a/../outside", "//host/path"):
            with self.assertRaises(ValueError):
                check.source_member(path)

    def test_archive_runs_from_stdin_without_site_or_repository_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, _, source = build_batch(root, "validation_failure")
            result = subprocess.run([sys.executable, "-S", "-", "archive", "--journal",
                                     str(source / "observer.jsonl"), "--native-journal",
                                     str(source / "native.jsonl"), "--output", "-"],
                                    input=Path(check.__file__).read_bytes(), stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=tmp, timeout=5, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            check.write_new(root / "stdin.tar", result.stdout)
            manifest = check.unpack_archive(root / "stdin.tar", root / "restored")
            self.assertTrue(manifest["complete_jsonl_prefixes"])

    def test_archive_rejects_unlisted_traversal_and_tampered_bytes(self):
        for corrupt in ("traversal", "bytes"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _, archive, _ = build_batch(root, "validation_failure")
                bad = root / "bad.tar"
                with tarfile.open(archive, "r:") as original, tarfile.open(bad, "w") as target:
                    for member in original.getmembers():
                        raw = original.extractfile(member).read()
                        if corrupt == "bytes" and member.name.endswith("native.jsonl"):
                            raw = b"x" + raw[1:]
                        target.addfile(member, io.BytesIO(raw))
                    if corrupt == "traversal":
                        member = tarfile.TarInfo("../escape")
                        member.size = 1
                        target.addfile(member, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    check.unpack_archive(bad, root / "restored")
                self.assertFalse((root / "escape").exists())

    def test_proxy_resolves_actual_attempt_and_rejects_retry_multiplicity(self):
        case = check.prepare("http://localhost:1", "fixture", ["validation_failure"],
                             proxy_url="http://localhost:2")["cases"][0]
        row = {"logical_request_id": case["headers"]["X-EIC-Logical-Request-ID"],
               "client_span_id": case["headers"]["X-EIC-Client-Span-ID"],
               "physical_request_id": "actual-proxy-attempt", "request_body_sha256": check.digest(check.encoded(case["body"]))}
        self.assertEqual(check.resolve_physical(case, [row]), "actual-proxy-attempt")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            check.resolve_physical(case, [row, dict(row, physical_request_id="retry")])
        with self.assertRaisesRegex(ValueError, "client span"):
            check.resolve_physical(case, [dict(row, client_span_id="wrong")])


if __name__ == "__main__":
    unittest.main()
