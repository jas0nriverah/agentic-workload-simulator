"""Durable telemetry must survive short OS writes without silent truncation."""
import json
import os
from unittest import mock

import pytest

from agentic_sim.telemetry.v2 import AppendOnlyWriter


def test_short_writes_keep_complete_request_records(tmp_path):
    writer = AppendOnlyWriter(tmp_path / "requests.jsonl")
    real_write = os.write
    records = [
        {"request_id": "r1", "phase": "start", "input_tokens": 123, "text": "café"},
        {"request_id": "r1", "phase": "end", "status": "failed"},
    ]
    with mock.patch(
        "agentic_sim.telemetry.v2.os.write",
        side_effect=lambda fd, data: real_write(fd, data[:3]),
    ):
        for record in records:
            writer.append(record)
    assert [json.loads(line) for line in writer.path.read_text().splitlines()] == records


def test_zero_write_fails_and_releases_writer(tmp_path):
    writer = AppendOnlyWriter(tmp_path / "requests.jsonl")
    with mock.patch("agentic_sim.telemetry.v2.os.write", return_value=0), mock.patch(
        "agentic_sim.telemetry.v2.os.fsync"
    ) as sync:
        with pytest.raises(OSError, match="no progress"):
            writer.append({"request_id": "failed"})
        sync.assert_not_called()
    writer.append({"request_id": "next"})
    assert json.loads(writer.path.read_text()) == {"request_id": "next"}
