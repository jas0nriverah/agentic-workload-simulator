"""An unprivileged launcher must bind the identity independently checked by BCC."""
from unittest import mock

import pytest

from agentic_sim.telemetry import bpf_work as bpf


def target(**kwargs):
    values = dict(pid=1234, run_id="run", attempt_id="attempt", case_id="case",
                  container_pid=86, pid_namespace="pid:[4026532803]")
    values.update(kwargs)
    return bpf.ProcessTarget(**values)


def inaccessible(cause):
    error = bpf.CollectorAttachError("namespace unavailable")
    error.__cause__ = cause
    return error


def test_permission_fallback_retains_host_ticks_and_runtime_namespace():
    with mock.patch.object(bpf.ProcessIdentity, "capture", side_effect=inaccessible(PermissionError())), \
         mock.patch.object(bpf, "_read_proc_stat", return_value={"pid":1234,"start_ticks":789}), \
         mock.patch.object(bpf, "_read_boot_id", return_value="boot"):
        identity = bpf._capture_service_identity(target())
    assert identity.pid == 1234
    assert identity.start_ticks == 789
    assert identity.pid_namespace_inode == 4026532803
    assert identity.boot_id == "boot"
    assert identity.container_pid == 86


@pytest.mark.parametrize("changes,cause", [
    ({}, FileNotFoundError()), ({"pid_namespace": None}, PermissionError()),
    ({"pid_namespace": "bad"}, PermissionError()), ({"container_pid": None}, PermissionError()),
])
def test_missing_process_or_unbound_namespace_cannot_use_fallback(changes, cause):
    with mock.patch.object(bpf.ProcessIdentity, "capture", side_effect=inaccessible(cause)):
        with pytest.raises(bpf.CollectorAttachError):
            bpf._capture_service_identity(target(**changes))


def test_readable_identity_uses_existing_capture():
    sentinel = object()
    with mock.patch.object(bpf.ProcessIdentity, "capture", return_value=sentinel):
        assert bpf._capture_service_identity(target()) is sentinel
