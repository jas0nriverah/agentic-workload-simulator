#!/usr/bin/env python3
r"""Bounded serving fixtures; prepare/collect/validate never send inference.

Operator sequence (run ONLY during an assigned GPU window)::

  python3 scripts/validation/check_live_native_serving.py prepare \
    --direct-url http://HOST:PORT --model MODEL --output PLAN.json
  python3 scripts/validation/check_live_native_serving.py run \
    --plan PLAN.json --gpu-window-id ASSIGNED_WINDOW --output CLIENT_DIR
  python3 scripts/validation/check_live_native_serving.py fetch \
    --ssh-host LOGIN --ssh-control EXISTING_MASTER --journal /absolute/asgi.jsonl \
    --native-journal /absolute/native.jsonl --output SERVER.tar
  python3 scripts/validation/check_live_native_serving.py validate \
    --client-dir CLIENT_DIR --archive SERVER.tar --output REVIEW_DIR

Use archive instead of fetch when journals are locally accessible. A batch
stores each journal prefix/raw artifact once, unchanged, under rootfs/; its
manifest resolves original absolute paths without writing to those paths.
Fetch reads only the two selected journals and their referenced metrics files.
No historical logs, retries, metrics polling, deployment, or restart commands.

Disconnect/foreign fixtures use the direct observed endpoint: the current
proxy buffers upstream responses. Optional --proxy-url routes validation
failure/stream_complete through the proxy; validate then requires --proxy-records
(v2 model JSONL) to resolve its generated physical ID. Foreign probes are invalid
POST bodies with the observer header, and need no additional inference.

All networking/archive code is stdlib. Offline validation imports the existing
repository derive module lazily. Native phases include preemptions; they are
not CUDA kernel timing. Missing/partial native capture stays unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import socket
import stat
import subprocess
import sys
import tarfile
import time
from urllib.parse import urlsplit
import uuid


SCENARIOS = ("validation_failure", "stream_disconnect", "foreign_overlap", "stream_complete")
LIMIT = 256 * 1024 * 1024
RESPONSE_LIMIT = 8 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")) + "\n").encode()


def write_new(path, raw):
    with Path(path).open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def json_rows(raw):
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("empty journal or incomplete final record; retain archive and fetch a later batch")
    return [json.loads(line) for line in raw.splitlines()]


def endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("endpoint must be an http(s) URL without embedded credentials")
    if parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/v1", "/v1/chat/completions"):
        raise ValueError("endpoint path must be empty, /v1, or /v1/chat/completions")
    return f"{parsed.scheme}://{parsed.netloc}/v1/chat/completions"


def prepare(direct_url, model, scenarios=SCENARIOS[:3], proxy_url=None, max_tokens=128):
    if not 1 <= max_tokens <= 256 or not model:
        raise ValueError("supply model and max_tokens in 1..256")
    if not scenarios or len(set(scenarios)) != len(scenarios) or any(s not in SCENARIOS for s in scenarios):
        raise ValueError("select distinct supported scenarios")
    cases = []
    for scenario in scenarios:
        physical = "eic-live-" + uuid.uuid4().hex
        proxy = bool(proxy_url and scenario in ("validation_failure", "stream_complete"))
        body = {"model": model, "messages": [{"role": "user", "content":
                "Count from 1 through 100, spelling each number on a separate line."}],
                "stream": True, "stream_options": {"include_usage": True}, "max_tokens": max_tokens}
        if scenario == "validation_failure":
            body["messages"] = "invalid-messages-" + physical
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "X-EIC-Physical-Request-ID": physical, "X-Request-Id": physical,
                   "X-EIC-Logical-Request-ID": physical + "-logical",
                   "X-EIC-Client-Span-ID": physical + "-client",
                   "X-EIC-Case-ID": scenario, "X-EIC-Attempt-ID": physical}
        case = {"scenario": scenario, "transport": "proxy" if proxy else "direct",
                "url": endpoint(proxy_url if proxy else direct_url), "physical_request_id": physical,
                "headers": headers, "body": body}
        if scenario == "foreign_overlap":
            case["foreign"] = {"url": endpoint(direct_url), "headers": {
                "Content-Type": "application/json", "Accept-Encoding": "identity",
                "X-EIC-Observer-Request": "1"}, "body": {
                "model": model, "messages": "foreign-invalid-messages-" + physical}}
        cases.append(case)
    return {"schema": "eic.live-native-fixtures.v1", "status": "prepared_no_requests_sent",
            "cases": cases, "attempts_per_case": 1, "native_required_for_success": True}


def request_once(spec, directory, stem, *, disconnect=False, on_stream=None, timeout=30):
    """One attempt; preserve exact sent body and every received body byte."""
    raw = encoded(spec["body"])
    write_new(directory / (stem + ".request.bin"), raw)
    parsed = urlsplit(endpoint(spec["url"]))
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=timeout)
    result = {"request_file": stem + ".request.bin", "request_sha256": digest(raw),
              "response_file": stem + ".response.bin", "started_client_monotonic_ns": time.monotonic_ns(),
              "response_complete": False, "client_closed_early": False, "error": None, "status": None}
    chunks = bytearray()
    deadline = time.monotonic() + timeout
    try:
        connection.request("POST", parsed.path, body=raw, headers=spec["headers"])
        response = connection.getresponse()
        result["status"] = response.status
        result["response_headers"] = response.getheaders()
        is_sse = response.status == 200 and "text/event-stream" in response.getheader("Content-Type", "")
        fired = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(chunks) >= RESPONSE_LIMIT:
                raise TimeoutError("bounded response time/byte limit reached")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(4096, RESPONSE_LIMIT - len(chunks)))
            if not chunk:
                result["response_complete"] = True
                break
            chunks.extend(chunk)
            if is_sse and b"data:" in chunks and not fired:
                fired = True
                if on_stream is not None:
                    result["foreign"] = on_stream()
                if disconnect:
                    # HTTPResponse may own the socket after a Connection: close header.
                    sock = connection.sock
                    if sock is None and response.fp is not None:
                        sock = getattr(getattr(response.fp, "raw", None), "_sock", None)
                    if sock is not None:
                        sock.shutdown(socket.SHUT_RDWR)
                    response.close()
                    result["client_closed_early"] = True
                    break
    except (OSError, ValueError, http.client.HTTPException) as exc:
        if isinstance(exc, http.client.IncompleteRead):
            chunks.extend(exc.partial[:RESPONSE_LIMIT - len(chunks)])
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        connection.close()
        write_new(directory / result["response_file"], bytes(chunks))
    result.update(response_sha256=digest(chunks), response_bytes=len(chunks),
                  ended_client_monotonic_ns=time.monotonic_ns())
    return result


def run(plan_path, output, window, timeout=30):
    if not window.strip() or not 1 <= timeout <= 60:
        raise ValueError("an assigned GPU window ID and timeout in 1..60 are required")
    plan_raw = Path(plan_path).read_bytes()
    plan = json.loads(plan_raw)
    require(plan.get("schema") == "eic.live-native-fixtures.v1" and
            1 <= len(plan["cases"]) <= len(SCENARIOS), "expected a bounded prepared fixture plan")
    kinds = [case["scenario"] for case in plan["cases"]]
    require(len(set(kinds)) == len(kinds) and all(kind in SCENARIOS for kind in kinds),
            "plan requires distinct supported scenarios")
    for case in plan["cases"]:
        require(case["transport"] in ("direct", "proxy"), "unknown transport")
        require(case["scenario"] not in ("stream_disconnect", "foreign_overlap") or case["transport"] == "direct",
                "disconnect/foreign proof requires the direct observed endpoint")
        require(1 <= case["body"].get("max_tokens", 0) <= 256, "fixture max_tokens must be in 1..256")
        endpoint(case["url"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "plan.json", plan_raw)
    report = {"schema": "eic.live-native-run.v1", "gpu_window_id": window,
              "plan_sha256": digest(plan_raw), "cases": []}
    # Each result is durable before the next case. There are no automatic retries.
    with (output / "client.jsonl").open("xb") as journal:
        for index, case in enumerate(plan["cases"]):
            foreign = None
            if "foreign" in case:
                foreign = lambda: request_once(case["foreign"], output, f"{index}-foreign", timeout=timeout)
            row = request_once(case, output, str(index), disconnect=case["scenario"] == "stream_disconnect",
                               on_stream=foreign, timeout=timeout)
            row.update(scenario=case["scenario"], submitted_physical_request_id=case["physical_request_id"])
            report["cases"].append(row)
            journal.write(encoded(row))
            journal.flush()
            os.fsync(journal.fileno())
    write_new(output / "run.json", encoded(report))
    return report


def metric_references(value):
    if isinstance(value, dict):
        if isinstance(value.get("raw_path"), str):
            yield value
        for child in value.values():
            yield from metric_references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from metric_references(child)


def source_member(original):
    path = PurePosixPath(original)
    if not path.is_absolute() or ".." in path.parts or str(path) != original or original.startswith("//"):
        raise ValueError("archive references require canonical absolute POSIX paths")
    return "rootfs/" + original.lstrip("/")


def capture_archive(journal, native_journal, stream, limit=LIMIT):
    """Read native prefix first, then ASGI; never rewrite/truncate journal bytes."""
    files, used = {}, 0

    def read(original, role):
        nonlocal used
        original = str(Path(original).absolute())
        member = source_member(original)
        path = Path(original)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("source symlinks are not supported")
        with path.open("rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit - used:
                raise ValueError("source is not regular or batch exceeds byte limit")
            raw = handle.read(info.st_size)
        if len(raw) != info.st_size:
            raise ValueError("source shrank while taking byte prefix")
        used += len(raw)
        files[original] = (raw, {"member": member, "role": role, "sha256": digest(raw), "bytes": len(raw)})
        return original, raw

    native_path, native_raw = read(native_journal, "native_journal")
    asgi_path, asgi_raw = read(journal, "asgi_journal")
    if native_path == asgi_path:
        raise ValueError("ASGI and native paths must differ")
    complete = True
    try:
        rows = json_rows(asgi_raw)
        json_rows(native_raw)
    except (ValueError, UnicodeError):
        # Preserve the incomplete tail; validation rejects it. Still collect complete rows' artifacts.
        complete = False
        rows = []
        for line in asgi_raw.splitlines(keepends=True):
            try:
                if line.endswith(b"\n"):
                    rows.append(json.loads(line))
            except (ValueError, UnicodeError):
                pass
    for ref in metric_references(rows):
        original = ref["raw_path"]
        source_member(original)
        if original not in files:
            read(original, "metrics_raw")
        raw = files[original][0]
        if ref.get("raw_sha256") != digest(raw):
            raise ValueError("metrics raw bytes differ from journal hash")
    manifest = {"schema": "eic.serving-batch-archive.v1", "journal": asgi_path,
                "native_journal": native_path, "complete_jsonl_prefixes": complete,
                "snapshot_order": ["native_journal", "asgi_journal", "metrics_raw"],
                "files": {key: value[1] for key, value in files.items()}}
    with tarfile.open(fileobj=stream, mode="w|") as tar:
        for name, raw in [("manifest.json", encoded(manifest))] + [(v[1]["member"], v[0]) for v in files.values()]:
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(raw), 0o600
            tar.addfile(item, io.BytesIO(raw))
    return manifest


def fetch(args):
    # Send this stdlib-only source over the existing SSH master, receive one tar.
    command = shlex.join([args.remote_python, "-", "archive", "--journal", args.journal,
                          "--native-journal", args.native_journal, "--output", "-"])
    with Path(args.output).open("xb") as output:
        result = subprocess.run(["ssh", "-S", args.ssh_control, "-o", "BatchMode=yes", "--",
                                 args.ssh_host, command], input=Path(__file__).read_bytes(),
                                stdout=output, stderr=subprocess.PIPE, timeout=60, check=False)
        output.flush()
        os.fsync(output.fileno())
    if result.returncode:
        raise ValueError("batch fetch failed; partial output retained: " + result.stderr.decode(errors="replace"))


def unpack_archive(path, destination, limit=LIMIT):
    """No extractall and no absolute writes. Original raw_path remains resolvable."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(path, "r:") as tar:
        items = tar.getmembers()
        if len(items) > 10000 or sum(item.size for item in items) > limit + 4 * 1024 * 1024:
            raise ValueError("archive exceeds size/member bound")
        names = [item.name for item in items]
        if len(set(names)) != len(names) or any(not item.isfile() for item in items):
            raise ValueError("duplicate or non-regular archive member")
        if "manifest.json" not in names:
            raise ValueError("archive manifest missing")
        manifest_raw = tar.extractfile("manifest.json").read()
        manifest = json.loads(manifest_raw)
        expected = {source_member(key) for key in manifest["files"]} | {"manifest.json"}
        if set(names) != expected:
            raise ValueError("archive members differ from absolute path manifest")
        for original, info in manifest["files"].items():
            if info["member"] != source_member(original):
                raise ValueError("absolute path mapping differs from canonical member")
            raw = tar.extractfile(info["member"]).read()
            if len(raw) != info["bytes"] or digest(raw) != info["sha256"]:
                raise ValueError("archive member hash/size mismatch")
            target = destination / info["member"]
            target.parent.mkdir(parents=True, exist_ok=True)
            write_new(target, raw)
        write_new(destination / "manifest.json", manifest_raw)
    for field in ("journal", "native_journal"):
        if manifest["files"][manifest[field]]["role"] != ("asgi_journal" if field == "journal" else field):
            raise ValueError("archive journal role mismatch")
    return manifest


def resolve_artifact(manifest, root, original):
    info = manifest["files"][original]
    if info["member"] != source_member(original):
        raise ValueError("invalid absolute artifact mapping")
    return Path(root) / info["member"]


def load_derive():
    path = Path(__file__).resolve().parents[1] / "observability/derive_server_attribution.py"
    spec = importlib.util.spec_from_file_location("live_native_deferred_derive", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve_physical(case, rows):
    if case["transport"] == "direct":
        return case["physical_request_id"]
    logical = case["headers"]["X-EIC-Logical-Request-ID"]
    matches = [row for row in rows if row.get("logical_request_id") == logical]
    ids = {r.get("physical_request_id") for r in matches if r.get("physical_request_id")}
    if len(ids) != 1:
        raise ValueError("proxy logical ID must resolve to exactly one retained physical attempt")
    physical = ids.pop()
    if not any(r.get("client_span_id") == case["headers"]["X-EIC-Client-Span-ID"] for r in matches):
        raise ValueError("proxy record lacks the exact client span join")
    if not any(r.get("request_body_sha256") == digest(encoded(case["body"])) for r in matches):
        raise ValueError("proxy request body hash differs from prepared fixture")
    return physical


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def check_case(case, client, data, native_rows, sidecar, physical):
    """A negative fixture passes only with positive, complete rejection evidence."""
    starts = [r for r in data.starts.values() if r.get("physical_request_id") == physical]
    require(len(starts) == 1, "exactly one server physical ingress required")
    start = starts[0]
    terminal = data.terminals.get(start["observation_id"])
    require(terminal is not None, "server terminal missing; client close alone is insufficient")
    require(start.get("http_request_id") == physical, "server X-Request-Id differs from physical ID")
    require(terminal.get("request_body_complete") is True and
            terminal.get("request_body_sha256") == client["request_sha256"], "server request body is incomplete/different")
    require(terminal.get("response_status") == client["status"], "client/server HTTP statuses differ")
    require(not data.fatal_records, "ASGI observer fatal evidence present")
    marks = [r for r in data.watermarks if r["sequence"] > terminal["sequence"] and
             r["covered_through_monotonic_ns"] >= terminal["terminal_monotonic_ns"]]
    require(bool(marks), "ASGI terminal has no completeness coverage")
    partial = [r for r in native_rows if r.get("record_type") == "native_finished" and
               (r["raw"].get("engine_request_id") == "chatcmpl-" + physical or
                r["raw"].get("parent_request_id") == "chatcmpl-" + physical)]
    linked = [r for r in native_rows if r.get("record_type") == "native_http_terminal" and
              r.get("observation_id") == start["observation_id"]]
    require(len(linked) == 1 and linked[0].get("native_capture_error") is None,
            "native HTTP reconciliation missing/failed")
    last_seq = max([linked[0]["sequence"]] + [r["sequence"] for r in partial])
    ended = max([terminal["terminal_monotonic_ns"]] + [r["observed_monotonic_ns"] for r in partial])
    require(any(r.get("record_type") == "native_watermark" and r["sequence"] > last_seq and
                r["covered_through_monotonic_ns"] >= ended and r["asgi_sequence"] >= marks[0]["sequence"]
                for r in native_rows), "native partial/HTTP records lack watermark coverage")
    kind = case["scenario"]
    reason = sidecar.get("unavailable_reason")
    if kind == "stream_complete":
        require(sidecar["status"] == "measured", "native success unavailable: " + str(reason))
        require(client["response_complete"] and client.get("sse_done") and client["error"] is None,
                "client did not receive complete SSE with [DONE]")
    else:
        require(sidecar["status"] == "unavailable", "negative fixture unexpectedly measured native timing")
        if kind == "foreign_overlap":
            require(client["status"] == 200 and client["response_complete"] and
                    client.get("sse_done") and client["error"] is None,
                    "foreign fixture target did not deliver a complete successful stream")
            require(reason == "foreign or competing model ingress overlaps native evidence window",
                    "native rejection was not proved to result from competing ingress: " + str(reason))
            foreign = client.get("foreign")
            require(foreign is not None and foreign["status"] in (400, 422) and foreign["response_complete"],
                    "foreign invalid POST was not completed")
            candidates = [r for r in data.terminals.values() if r.get("request_body_sha256") == foreign["request_sha256"]]
            require(len(candidates) == 1, "foreign probe requires one exact body hash match")
            other = candidates[0]
            require(other["request_class"] == "foreign" and other.get("physical_request_id") is None and
                    other["method"] == "POST" and other["route"] == "/v1/chat/completions",
                    "observer header hid foreign POST or foreign route/join differs")
            require(other["response_status"] == foreign["status"] and other["response_body_complete"] is True,
                    "foreign server terminal does not confirm rejection")
            require(other["response_body_sha256"] == foreign["response_sha256"] and
                    other["request_body_complete"] is True, "foreign HTTP body evidence differs/incomplete")
            require(any(r["sequence"] > other["sequence"] and
                        r["covered_through_monotonic_ns"] >= other["terminal_monotonic_ns"] for r in data.watermarks),
                    "foreign terminal lacks completeness coverage")
            require(other["started_monotonic_ns"] < terminal["terminal_monotonic_ns"] and
                    other["terminal_monotonic_ns"] > start["started_monotonic_ns"],
                    "foreign POST did not overlap target in server clock")
        else:
            require(reason in ("native target lacks matching actual serving request metadata ID",
                               "native target HTTP request is incomplete, failed or disconnected"),
                    "native unavailable for an unexpected correctness failure: " + str(reason))
            if kind == "validation_failure":
                require(client["status"] in (400, 422) and client["response_complete"] and client["error"] is None,
                        "expected a complete HTTP 400/422 validation failure")
                require(terminal["response_body_complete"] is True, "server validation response incomplete")
            elif kind == "stream_disconnect":
                require(client["status"] == 200 and client["client_closed_early"] and not client.get("sse_done"),
                        "client did not interrupt an unfinished successful stream")
                require(terminal.get("client_disconnected") is True or terminal["terminal_status"] == "disconnected",
                        "server did not observe disconnect; may have already finished")
            else:
                raise ValueError("unsupported fixture")
    if client["response_complete"]:
        require(terminal["response_body_complete"] is True and terminal["response_body_sha256"] == client["response_sha256"],
                "complete client/server response hashes differ")
    return {"status": "passed", "physical_request_id": physical, "observation_id": start["observation_id"],
            "native_status": sidecar["status"], "native_partial_records": [
                {"sequence": r["sequence"], "raw_sha256": r["raw_sha256"]} for r in partial]}


def validate(client_dir, archive, output, proxy_records=None):
    client_dir, output = Path(client_dir), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": "eic.live-native-review.v1", "status": "failed", "cases": [],
              "archive_sha256": digest(Path(archive).read_bytes()), "cuda_kernel_timing": False}
    try:
        root = output / "server"
        manifest = unpack_archive(archive, root)
        require(manifest["complete_jsonl_prefixes"] is True, "archive contains an incomplete journal tail")
        journal = resolve_artifact(manifest, root, manifest["journal"])
        native = resolve_artifact(manifest, root, manifest["native_journal"])
        derive = load_derive()
        data = derive.read_observer_journal(journal)
        native_rows = json_rows(native.read_bytes())
        for ref in metric_references(data.records):
            require(digest(resolve_artifact(manifest, root, ref["raw_path"]).read_bytes()) == ref["raw_sha256"],
                    "archived absolute metrics reference/hash mismatch")
        plan_raw, run_raw = (client_dir / "plan.json").read_bytes(), (client_dir / "run.json").read_bytes()
        plan, run_report = json.loads(plan_raw), json.loads(run_raw)
        require(run_report["plan_sha256"] == digest(plan_raw), "client plan hash mismatch")
        require(len(plan["cases"]) == len(run_report["cases"]) > 0, "client fixture results missing")
        proxy_raw = Path(proxy_records).read_bytes() if proxy_records else None
        proxy_rows = json_rows(proxy_raw) if proxy_raw is not None else []
        report.update(plan_sha256=digest(plan_raw), client_run_sha256=digest(run_raw),
                      proxy_records_sha256=digest(proxy_raw) if proxy_raw is not None else None,
                      gpu_window_id=run_report["gpu_window_id"], archive_manifest=manifest,
                      observer_identity=data.header)
        # One batch tree, one sidecar file. Existing derive is intentionally used unchanged.
        with (output / "attribution.jsonl").open("xb") as sidecars:
            for case, client in zip(plan["cases"], run_report["cases"]):
                result = {"scenario": case["scenario"], "status": "failed"}
                try:
                    require(case["scenario"] == client["scenario"] and
                            case["physical_request_id"] == client["submitted_physical_request_id"], "client case ordering/ID mismatch")
                    for request in [client] + ([client["foreign"]] if client.get("foreign") else []):
                        for kind in ("request", "response"):
                            name = request[kind + "_file"]
                            require(Path(name).name == name, "client artifact path is not a filename")
                            raw = (client_dir / name).read_bytes()
                            require(digest(raw) == request[kind + "_sha256"], "client artifact hash mismatch")
                            if kind == "response":
                                request["sse_done"] = any(line.strip() == b"data: [DONE]" for line in raw.splitlines())
                    require(client["request_sha256"] == digest(encoded(case["body"])), "request differs from plan")
                    if client.get("foreign"):
                        require(client["foreign"]["request_sha256"] == digest(encoded(case["foreign"]["body"])),
                                "foreign request differs from plan")
                    physical = resolve_physical(case, proxy_rows)
                    sidecar = derive.derive_server_attribution(journal=journal, native_journal=native, request_id=physical)
                    sidecars.write(encoded(sidecar))
                    sidecars.flush()
                    os.fsync(sidecars.fileno())
                    result.update(check_case(case, client, data, native_rows, sidecar, physical))
                except (ValueError, KeyError, OSError, TypeError) as exc:
                    result["reason"] = str(exc)
                report["cases"].append(result)
        report["status"] = "passed" if all(r["status"] == "passed" for r in report["cases"]) else "failed"
    except (ValueError, KeyError, OSError, TypeError, tarfile.TarError) as exc:
        report["error"] = str(exc)
    write_new(output / "review.json", encoded(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("prepare", help="write requests and IDs; no network")
    p.add_argument("--direct-url", required=True)
    p.add_argument("--proxy-url")
    p.add_argument("--model", required=True)
    p.add_argument("--scenarios", nargs="+", choices=SCENARIOS, default=list(SCENARIOS[:3]))
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--output", required=True)
    p = commands.add_parser("run", help="SEND requests once, only in an assigned GPU window")
    p.add_argument("--plan", required=True)
    p.add_argument("--gpu-window-id", required=True)
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--output", required=True)
    for name in ("archive", "fetch"):
        p = commands.add_parser(name, help="one unchanged batch of selected journals and metrics raw files")
        p.add_argument("--journal", required=True)
        p.add_argument("--native-journal", required=True)
        p.add_argument("--output", required=True)
        if name == "fetch":
            p.add_argument("--ssh-host", required=True)
            p.add_argument("--ssh-control", required=True)
            p.add_argument("--remote-python", default="python3")
    p = commands.add_parser("validate", help="offline ASGI/native/HTTP proof with existing deferred derive")
    p.add_argument("--client-dir", required=True)
    p.add_argument("--archive", required=True)
    p.add_argument("--proxy-records")
    p.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            write_new(args.output, encoded(prepare(args.direct_url, args.model, args.scenarios, args.proxy_url, args.max_tokens)))
        elif args.command == "run":
            run(args.plan, args.output, args.gpu_window_id, args.timeout)
        elif args.command == "fetch":
            fetch(args)
        elif args.command == "archive":
            if args.output == "-":
                capture_archive(args.journal, args.native_journal, sys.stdout.buffer)
            else:
                with Path(args.output).open("xb") as stream:
                    capture_archive(args.journal, args.native_journal, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
        else:
            report = validate(args.client_dir, args.archive, args.output, args.proxy_records)
            print(json.dumps({"status": report["status"], "report": str(Path(args.output) / "review.json")}))
            return 0 if report["status"] == "passed" else 3
        return 0
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
