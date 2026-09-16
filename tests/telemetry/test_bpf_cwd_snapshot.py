"""Live procfs and protocol tests for the bounded cwd operation; no BCC jobs."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import threading

import pytest

from agentic_sim.telemetry import bpf_work as bpf
from agentic_sim.telemetry.sweagent_hooks import SWEAgentTelemetryHook
from agentic_sim.telemetry.v2 import TelemetryV2


def live_identity():
    return bpf.ProcessIdentity.capture(bpf.ProcessTarget(
        pid=os.getpid(), container_pid=os.getpid(),
        pid_namespace=os.readlink("/proc/self/ns/pid"),
        run_id="cwd-test", attempt_id="attempt", case_id="case",
    ))


def test_real_procfs_observes_cwd_changes_without_cache(tmp_path, monkeypatch):
    identity = live_identity()
    first = tmp_path / "first"
    second = tmp_path / "second with spaces"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(first)
    a = bpf._capture_persistent_shell_cwd(identity)
    monkeypatch.chdir(second)
    b = bpf._capture_persistent_shell_cwd(identity)
    assert a["status"] == b["status"] == "measured"
    assert a["container_cwd"] == str(first)
    assert b["container_cwd"] == str(second)
    assert a["namespace_proof"]["cwd_before"] != b["namespace_proof"]["cwd_before"]
    assert a["ended_mono_ns"] <= b["started_mono_ns"]


def test_deleted_cwd_is_unavailable(tmp_path, monkeypatch):
    identity = live_identity()
    cwd = tmp_path / "deleted"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    cwd.rmdir()
    result = bpf._capture_persistent_shell_cwd(identity)
    assert result["status"] == "unavailable"
    assert result["container_cwd"] is None
    assert "deleted" in result["reason"]


@pytest.mark.parametrize("field,change", [
    ("start_ticks", lambda v: v + 1),
    ("boot_id", lambda v: "different-boot"),
    ("pid_namespace_inode", lambda v: v + 1),
])
def test_real_identity_reuse_boot_and_namespace_rejected(field, change):
    values = live_identity().to_mapping()
    values[field] = change(values[field])
    result = bpf._capture_persistent_shell_cwd(bpf.ProcessIdentity.from_mapping(values))
    assert result["status"] == "unavailable"
    assert result["namespace_proof"] is None


def test_identity_changed_during_snapshot_rejected():
    identity = live_identity()
    with mock.patch.object(bpf.ProcessIdentity, "assert_current", side_effect=[{}, bpf.IdentityBindingError("PID reused")]):
        result = bpf._capture_persistent_shell_cwd(identity)
    assert result["status"] == "unavailable"
    assert result["container_cwd"] is None


@pytest.fixture
def namespace_view(tmp_path, monkeypatch):
    """Real root-relative directory FDs with a simulated proc namespace view."""
    root = tmp_path / "container-root"
    (root / "work").mkdir(parents=True)
    (root / "other").mkdir()
    proc = tmp_path / "proc"
    base = proc / "1234"
    (base / "ns").mkdir(parents=True)
    (base / "root").symlink_to(root, target_is_directory=True)
    (base / "cwd").symlink_to(root / "work", target_is_directory=True)
    (base / "ns/pid").symlink_to("pid:[4321]")
    (base / "ns/mnt").symlink_to("mnt:[9876]")
    identity = bpf.ProcessIdentity(1234, 99, "boot", 4321, "run", "attempt", "case", None, 8, "pid:[4321]", "test")
    original_readlink = os.readlink
    state = {"cwd": "/work"}

    def readlink(path, *a, **k):
        if Path(path) == base / "cwd":
            return state["cwd"]
        return original_readlink(path, *a, **k)

    monkeypatch.setattr(os, "readlink", readlink)
    monkeypatch.setattr(bpf.ProcessIdentity, "assert_current", lambda self: {})
    return identity, proc, root, base, state


def test_container_path_is_proved_relative_to_target_root(namespace_view):
    identity, proc, root, base, state = namespace_view
    result = bpf._capture_persistent_shell_cwd(identity, proc_root=proc)
    assert result["status"] == "measured"
    assert result["container_cwd"] == "/work"
    proof = result["namespace_proof"]
    assert proof["resolved_from_process_root"] == proof["cwd_before"]
    assert proof["root_before"]["inode"] == root.stat().st_ino
    assert proof["mount_namespace_before"] == "mnt:[9876]"


@pytest.mark.parametrize("candidate", ["/other", "/../work", "//work", "(unreachable)/work", "/work (deleted)"])
def test_unproven_or_escaping_namespace_path_rejected(namespace_view, candidate):
    identity, proc, root, base, state = namespace_view
    state["cwd"] = candidate
    result = bpf._capture_persistent_shell_cwd(identity, proc_root=proc)
    assert result["status"] == "unavailable"
    assert result["container_cwd"] is None


def test_symlink_component_is_not_accepted_as_root_namespace_proof(namespace_view):
    identity, proc, root, base, state = namespace_view
    (root / "alias").symlink_to("work", target_is_directory=True)
    state["cwd"] = "/alias"
    assert bpf._capture_persistent_shell_cwd(identity, proc_root=proc)["status"] == "unavailable"


def test_namespace_changes_during_snapshot_rejected(namespace_view, monkeypatch):
    identity, proc, root, base, state = namespace_view
    original = os.readlink
    count = 0

    def readlink(path, *args, **kwargs):
        nonlocal count
        if Path(path) == base / "ns/mnt":
            count += 1
            return "mnt:[9876]" if count == 1 else "mnt:[9877]"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", readlink)
    assert bpf._capture_persistent_shell_cwd(identity, proc_root=proc)["status"] == "unavailable"


def test_proc_read_failure_is_explicit_unavailable():
    identity = live_identity()
    with mock.patch.object(bpf.os, "open", side_effect=PermissionError("proc read denied")):
        result = bpf._capture_persistent_shell_cwd(identity)
    assert result["status"] == "unavailable"
    assert result["error_type"] == "PermissionError"


def service_client(tmp_path):
    identity = live_identity()
    service = bpf.BpfWorkService(SimpleNamespace(identity=identity), tmp_path / "cwd.sock")
    client = bpf.BpfWorkClient(tmp_path / "cwd.sock", identity=identity.to_mapping())

    def call(request):
        response, stop = service._dispatch({**request, "identity": client.identity})
        assert not stop
        return response

    client._call = call
    return service, client


def test_dispatch_refuses_partial_foreign_and_caller_selected_target(tmp_path):
    service, client = service_client(tmp_path)
    for supplied in ({}, {"pid": client.identity["pid"]}, {**client.identity, "container_pid": 999}):
        with pytest.raises(bpf.IdentityBindingError):
            service._dispatch({"op": "cwd_snapshot", "identity": supplied})
    with pytest.raises(bpf.BpfProtocolError):
        service._dispatch({"op": "cwd_snapshot", "identity": client.identity, "pid": os.getpid()})


@pytest.mark.parametrize("bad", ["stale", "identity", "root", "namespace", "malformed", "unavailable_path"])
def test_client_rejects_bad_cwd_proof(tmp_path, bad):
    service, client = service_client(tmp_path)
    original = client._call

    def call(request):
        response = original(request)
        value = response["cwd_snapshot"]
        if bad == "stale": value["started_mono_ns"] = 0
        if bad == "identity": value["identity"] = {**value["identity"], "start_ticks": 1}
        if bad == "root": value["namespace_proof"]["resolved_from_process_root"]["inode"] += 1
        if bad == "namespace": value["namespace_proof"]["mount_namespace_after"] = "mnt:[1]"
        if bad == "malformed": value["namespace_proof"]["cwd_after"] = None
        if bad == "unavailable_path": value.update(status="unavailable", reason="missing")
        return response

    client._call = call
    with pytest.raises((bpf.BpfProtocolError, bpf.IdentityBindingError)):
        client.cwd_snapshot()


def test_actual_uds_roundtrip(tmp_path):
    # A one-request protocol server with actual procfs sampling; no BCC attach.
    identity = live_identity()
    service = bpf.BpfWorkService(SimpleNamespace(identity=identity), tmp_path / "cwd.sock")
    socket = service._bind()
    errors = []

    def serve():
        try:
            socket.settimeout(3)
            connection, _ = socket.accept()
            with connection:
                request = service._read_request(connection)
                response, _ = service._dispatch(request)
                connection.sendall((json.dumps(response) + "\n").encode())
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        client = bpf.BpfWorkClient(tmp_path / "cwd.sock", identity=identity.to_mapping())
        result = client.cwd_snapshot()
        assert result["status"] == "measured"
        assert result["container_cwd"] == os.getcwd()
        request = result["client_roundtrip"]
        assert request["started_mono_ns"] <= result["started_mono_ns"] <= result["ended_mono_ns"] <= request["ended_mono_ns"]
    finally:
        thread.join(timeout=5)
        socket.close()
    assert not thread.is_alive()
    assert not errors


def test_hook_keeps_span_script_reads_and_fresh_witness_without_pwd(tmp_path, monkeypatch):
    service, client = service_client(tmp_path)
    root = tmp_path / "working"
    root.mkdir()
    other = tmp_path / "next"
    other.mkdir()
    (root / "run.py").write_text("print('first')\n")
    (other / "run.py").write_text("print('next')\n")
    reads = []

    def read_file(path, **kwargs):
        reads.append(path)
        return Path(path).read_text()

    env = SimpleNamespace(_assignment_v2_bpf_service=SimpleNamespace(client=client),
                          read_file=read_file, communicate=mock.Mock(side_effect=AssertionError("extra pwd RPC")))
    telemetry = TelemetryV2(tmp_path / "telemetry", run_id="hook-cwd")
    hook = SWEAgentTelemetryHook(telemetry)
    hook.agent = SimpleNamespace(_env=env)
    hook.on_run_start()
    monkeypatch.chdir(root)
    first = hook._native_script_snapshot("python run.py", parent_event_id=None)
    monkeypatch.chdir(other)
    second = hook._native_script_snapshot("python run.py", parent_event_id=None)
    assert reads == [str(root / "run.py"), str(other / "run.py")]
    assert first["paths"][0]["sha256"] != second["paths"][0]["sha256"]
    assert second["generation"] > first["generation"]
    env.communicate.assert_not_called()
    terminals = [r for r in telemetry.rows("lifecycle") if r.get("event_kind") == "script_read" and r.get("terminal")]
    assert len(terminals) == 2
    for row in terminals:
        assert row["phase"] == "state_query" and row["status"] == "success"
        assert row["script_read_count"] == 1
        assert row["script_cwd_witness"]["source"] == "bpf_service_procfs"
        assert row["script_cwd_witness"]["service_snapshot"]["identity"] == client.identity
    telemetry.finish_outer()


def test_compound_cd_preserves_native_logical_cwd(tmp_path):
    client = SimpleNamespace(cwd_snapshot=mock.Mock(side_effect=AssertionError("must use logical cwd")))
    # A prior `cd /logical/link` may point at /physical/deep. `cd ..` uses
    # /logical, so resolving the upcoming script from physical cwd is wrong.
    env = SimpleNamespace(_assignment_v2_bpf_service=SimpleNamespace(client=client),
                          communicate=mock.Mock(return_value="/logical/link\n"),
                          read_file=mock.Mock(return_value="print('logical')\n"))
    telemetry = TelemetryV2(tmp_path / "telemetry", run_id="logical-cwd")
    hook = SWEAgentTelemetryHook(telemetry)
    hook.agent = SimpleNamespace(_env=env)
    hook.on_run_start()
    result = hook._native_script_snapshot("cd .. && python run.py", parent_event_id=None)
    assert result["status"] == "known"
    env.read_file.assert_called_once_with("/logical/run.py", encoding="utf-8", errors="strict")
    env.communicate.assert_called_once_with("pwd", check="ignore")
    client.cwd_snapshot.assert_not_called()
    row = next(r for r in telemetry.rows("lifecycle") if r.get("event_kind") == "script_read" and r.get("terminal"))
    assert row["script_cwd_witness"]["source"] == "swerex_pwd"
    assert "logical cwd" in row["script_cwd_witness"]["fallback_reason"]
    telemetry.finish_outer()


@pytest.mark.parametrize("error", [PermissionError("read"), bpf.IdentityBindingError("PID reused"),
                                   bpf.BpfProtocolError("namespace proof"), bpf.BpfProtocolError("old service")])
def test_hook_native_fallback_on_service_failures(error):
    client = SimpleNamespace(cwd_snapshot=mock.Mock(side_effect=error))
    env = SimpleNamespace(_assignment_v2_bpf_service=SimpleNamespace(client=client),
                          communicate=mock.Mock(return_value="/live/new cwd\n"))
    witness = {}
    assert SWEAgentTelemetryHook._query_container_working_directory(env, witness=witness) == "/live/new cwd"
    env.communicate.assert_called_once_with("pwd", check="ignore")
    assert witness["source"] == "swerex_pwd" and witness["status"] == "measured"
    assert type(error).__name__ in witness["fallback_reason"]


def test_unavailable_snapshot_retained_and_double_failure_unavailable():
    value = {"status": "unavailable", "container_cwd": None, "reason": "cwd deleted"}
    client = SimpleNamespace(cwd_snapshot=lambda: copy.deepcopy(value))
    env = SimpleNamespace(_assignment_v2_bpf_service=SimpleNamespace(client=client),
                          communicate=mock.Mock(side_effect=OSError("pwd failed")))
    witness = {}
    assert SWEAgentTelemetryHook._query_container_working_directory(env, witness=witness) is None
    assert witness["service_snapshot"] == value
    assert witness["status"] == "unavailable"
    assert witness["fallback_reason"] == "cwd deleted"
    assert "pwd failed" in witness["reason"]
