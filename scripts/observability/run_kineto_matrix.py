#!/usr/bin/env python3
"""Run a small serialized request matrix with lossless timing records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


CASES = (
    ("cal_128_32", "calibration", 128, 32),
    ("cal_512_32", "calibration", 512, 32),
    ("cal_2048_64", "calibration", 2048, 64),
    ("cal_4096_64", "calibration", 4096, 64),
    ("hold_1024_48", "holdout", 1024, 48),
    ("hold_3072_64", "holdout", 3072, 64),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def post_json(url: str, payload: dict) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.status, response.read()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)

    records = []
    for index, (case_id, split, input_words, output_tokens) in enumerate(CASES):
        if index:
            time.sleep(1.0)
        prompt = " ".join(f"token{i % 97}" for i in range(input_words))
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": output_tokens,
            "seed": 0,
        }
        start_utc = utc_now()
        start_mono_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        status, body = post_json(f"{args.base_url}/v1/chat/completions", payload)
        end_mono_ns = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        end_utc = utc_now()
        response_path = args.output_dir / f"{case_id}.response.json"
        response_path.write_bytes(body)
        parsed = json.loads(body)
        records.append(
            {
                "case_id": case_id,
                "split": split,
                "input_word_target": input_words,
                "output_token_limit": output_tokens,
                "clock_id": "CLOCK_MONOTONIC_RAW",
                "start_mono_ns": start_mono_ns,
                "end_mono_ns": end_mono_ns,
                "start_utc": start_utc,
                "end_utc": end_utc,
                "wall_ms": (end_mono_ns - start_mono_ns) / 1e6,
                "http_status": status,
                "request_body_sha256": hashlib.sha256(
                    json.dumps(payload, separators=(",", ":")).encode()
                ).hexdigest(),
                "response_sha256": hashlib.sha256(body).hexdigest(),
                "usage": parsed.get("usage"),
                "response_path": str(response_path),
            }
        )

    manifest = {
        "schema_version": "kineto-request-matrix.v1",
        "provenance": "measured",
        "host": os.uname().nodename,
        "model": args.model,
        "base_url": args.base_url,
        "records": records,
    }
    (args.output_dir / "requests.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
