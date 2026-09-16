"""Command-probe binding regressions for physical reset actions."""

from __future__ import annotations

import hashlib

import pytest

from agentic_sim.telemetry.work import (
    CommandProbeBinding,
    measure_runtime_work,
    parse_strace_summary,
)


def test_empty_command_binding_preserves_hash_and_measured_work() -> None:
    binding = CommandProbeBinding("reset-empty", "", 100, 200)
    empty_hash = hashlib.sha256(b"").hexdigest()
    assert binding.command_sha256 == empty_hash
    metrics = {
        "event_id": "reset-empty",
        "command": "",
        "start_mono_ns": 110,
        "end_mono_ns": 190,
        "pid": 1234,
        "provenance": "measured",
        "bytes_read": 7,
        "bytes_written": 3,
        "files_touched": 1,
        "subprocess_count": 0,
    }
    measured = measure_runtime_work({"work_volume": metrics}, binding=binding)
    assert measured.bytes_read == 7
    assert measured.bytes_written == 3
    assert measured.files_touched == 1
    assert measured.subprocess_count == 0
    assert measured.binding is not None
    assert measured.binding["command_sha256"] == empty_hash


def test_empty_command_strace_summary_binds_exact_hash() -> None:
    binding = CommandProbeBinding("reset-empty", "", 100, 200)
    summary = {
        "mode": "strace -ff -ttt -T",
        "event_id": "reset-empty",
        "command_sha256": hashlib.sha256(b"").hexdigest(),
        "start_mono_ns": 100,
        "end_mono_ns": 200,
        "cgroup": "/sys/fs/cgroup/fixture",
        "provenance": "measured",
        "bytes_read": 0,
    }
    measured = parse_strace_summary(summary, binding=binding)
    assert measured.bytes_read == 0
    assert measured.binding is not None


@pytest.mark.parametrize("command", [None, 123, "bad\x00command"])
def test_probe_binding_rejects_missing_nontext_and_nul_commands(command) -> None:
    with pytest.raises(ValueError, match="probe command"):
        CommandProbeBinding("invalid", command, 1, 2)
