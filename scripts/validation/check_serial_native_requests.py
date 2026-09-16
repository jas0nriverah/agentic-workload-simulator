#!/usr/bin/env python3
"""Two serial live model requests through the existing durable request proxy.

This is a serving integration probe, not a SWE-bench result or overhead gate.
Raw request/response and scrape bytes survive independently of later native
server-journal retrieval and attribution.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from scripts.observability.request_proxy import JsonlWriter, ProxyServer


def save(path, raw):
    with path.open("xb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-identity", required=True)
    parser.add_argument("--counter-epoch", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        raise SystemExit("--execute is required for the two live requests")
    out = args.output.absolute()
    out.mkdir(parents=True, exist_ok=False)
    config = {
        "schema_version": "assignment.serving-metrics-config.v1", "enabled": True,
        "metrics_url": f"http://127.0.0.1:{args.port}/metrics",
        "server_identity": args.server_identity, "counter_epoch": args.counter_epoch,
        "timeout_seconds": 10., "access_witness_path": str(out / "unavailable-external-witness.jsonl"),
        "access_witness_evidence_kind": "external_access_lease", "vllm_version": "0.10.0",
    }
    save(out / "serving-config.json", (json.dumps(config, sort_keys=True, indent=2) + "\n").encode())
    proxy = ProxyServer(("127.0.0.1", 0), upstream_host="127.0.0.1", upstream_port=args.port,
                        writer=JsonlWriter(out / "request-events.jsonl"), timeout_seconds=180,
                        max_body_bytes=1024 * 1024, v2_output_dir=out / "telemetry_v2",
                        v2_run_id=out.name, v2_case_id="controlled-native-serial-probe",
                        v2_attempt_id="attempt-001", v2_require_request_payloads=True,
                        serving_metrics_config=config)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    results = []
    try:
        for index in range(2):
            body = json.dumps({"model": args.model, "messages": [{"role": "user", "content": "Reply with the word ready."}],
                               "max_tokens": 16, "temperature": 0, "top_p": 1, "seed": 0,
                               "n": 1, "stream": False}, sort_keys=True, separators=(",", ":")).encode()
            save(out / f"request-{index}.json", body)
            connection = http.client.HTTPConnection("127.0.0.1", proxy.server_address[1], timeout=190)
            started = time.monotonic_ns()
            try:
                connection.request("POST", "/v1/chat/completions", body=body,
                                   headers={"Content-Type": "application/json", "X-EIC-Logical-Request-ID": f"native-probe-{index}"})
                response = connection.getresponse()
                raw = response.read()
                save(out / f"response-{index}.json", raw)
                parsed = json.loads(raw)
                row = {"index": index, "status": response.status, "response_id": parsed.get("id"),
                       "usage": parsed.get("usage"), "wall_ms": (time.monotonic_ns() - started) / 1e6,
                       "request_sha256": hashlib.sha256(body).hexdigest(), "response_sha256": hashlib.sha256(raw).hexdigest()}
                results.append(row)
                print(json.dumps(row), flush=True)
                if response.status != 200:
                    raise RuntimeError(f"request {index} returned HTTP {response.status}")
                if not str(parsed.get("id", "")).startswith("chatcmpl-"):
                    raise RuntimeError("response lacks native chat request identity")
            finally:
                connection.close()
    finally:
        proxy.shutdown()
        proxy.join_request_threads(timeout=10)
        proxy.close_gracefully(timeout=10)
        thread.join(timeout=10)
        save(out / "requests-summary.json", (json.dumps({"schema_version": "assignment.native-serial-probe.v1",
            "requests": results, "native_attribution": "pending_server_journal_validation",
            "full_production_gate": False}, indent=2, sort_keys=True) + "\n").encode())


if __name__ == "__main__":
    main()
