import copy
import hashlib

import pytest

from agentic_sim.telemetry.v2 import TelemetryContractError, TelemetryV2
from scripts.assignment import sweagent_case_runner as runner


def recorded_commands(tmp_path):
    telemetry = TelemetryV2(tmp_path, run_id="synthetic-run", attempt_id="attempt-001",
                            case_id="synthetic-case", instance_id="synthetic-instance")
    for index, command in enumerate(("export LANG=C.UTF-8", "cd /repo", "git status")):
        span = telemetry.begin_runtime_command(command, phase="setup", parent_event_id="setup-parent",
                                               start_mono_ns=100 + index * 20)
        span.finish(end_mono_ns=110 + index * 20)
    return telemetry._rows


def test_each_setup_command_has_distinct_required_physical_identity(tmp_path):
    rows = recorded_commands(tmp_path)
    expected = runner._expected_cpu_action_rows(rows)
    assert len(expected) == 3
    assert len({row["event_id"] for row in expected}) == 3
    assert len({row["span_id"] for row in expected}) == 3
    assert [row["actual_action"] for row in expected] == ["export LANG=C.UTF-8", "cd /repo", "git status"]
    for row in expected:
        assert row["cpu_action_required"] is True
        assert row["actual_action_sha256"] == hashlib.sha256(row["actual_action"].encode()).hexdigest()
        assert row["parent_event_id"] == "setup-parent"


@pytest.mark.parametrize("mutation", ["missing_terminal", "duplicate_terminal", "changed_command",
                                    "wrong_attempt", "changed_clock", "disable_capture"])
def test_runtime_command_corruption_fails_closed(tmp_path, mutation):
    rows = copy.deepcopy(recorded_commands(tmp_path))
    terminal = next(row for row in rows if row.get("event_kind") == "runtime_command")
    if mutation == "missing_terminal":
        rows.remove(terminal)
    elif mutation == "duplicate_terminal":
        rows.append(copy.deepcopy(terminal))
    elif mutation == "changed_command":
        terminal["runtime_command"] = "different command"
    elif mutation == "wrong_attempt":
        terminal["attempt_id"] = "wrong-attempt"
    elif mutation == "changed_clock":
        terminal["clock"]["clock_id"] = "CLOCK_MONOTONIC"
    else:
        terminal["cpu_action_required"] = False
    with pytest.raises(runner.CaseRunnerError, match="runtime command"):
        runner._expected_cpu_action_rows(rows)


def test_tool_and_runtime_commands_both_require_raw_action_coverage(tmp_path):
    rows = recorded_commands(tmp_path)
    tool = {"schema_version": "assignment.telemetry.v2.tool", "event_kind": "tool_event_start",
            "terminal": False, "event_id": "tool-action", "actual_action": "pytest -q",
            "actual_action_sha256": hashlib.sha256(b"pytest -q").hexdigest()}
    expected = runner._expected_cpu_action_rows([tool, *rows])
    assert len(expected) == 4 and expected[0] == tool
    assert tool not in runner._expected_cpu_action_rows([{**tool, "intent_only": True}, *rows])


def test_invalid_runtime_command_never_emits_start(tmp_path):
    telemetry = TelemetryV2(tmp_path, run_id="synthetic-run")
    for command in (None, 123, "\x00", "echo\x00unsafe"):
        with pytest.raises(TelemetryContractError, match="runtime command"):
            telemetry.begin_runtime_command(command, phase="setup")
    with pytest.raises(TelemetryContractError, match="unsupported lifecycle"):
        telemetry.begin_runtime_command("true", phase="invented-phase")


@pytest.mark.parametrize("command", ["", " ", "\n"])
def test_framework_noop_is_captured_with_exact_empty_or_whitespace_bytes(tmp_path, command):
    telemetry = TelemetryV2(tmp_path, run_id="synthetic-run")
    span = telemetry.begin_runtime_command(command, phase="setup")
    span.finish()
    expected = runner._expected_cpu_action_rows(telemetry._rows)
    assert len(expected) == 1
    assert expected[0]["actual_action"] == command
    assert expected[0]["actual_action_sha256"] == hashlib.sha256(command.encode()).hexdigest()
    assert expected[0]["cpu_action_required"] is True
