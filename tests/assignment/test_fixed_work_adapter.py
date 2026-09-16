"""Focused contract tests for the executable fixed-work adapter.

These tests use a real persistent shell for the CPU control condition.  The
capture and model tests feed observed journals through the same validators used
by the adapter; they do not fabricate a completed live model or BPF run.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import time
from types import SimpleNamespace

import pytest

from scripts.validation import fixed_work_adapter as adapter


def _write_snapshot(path: Path) -> None:
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("seed.txt")
        payload = b"seed\n"
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))


def _write_fixture(
    root: Path,
    *,
    fixture_id: str = "cpu-file-traversal-v1",
    kind: str = "cpu_filesystem_traversal",
    action_rows: list[dict[str, object]] | None = None,
    request_rows: list[dict[str, object]] | None = None,
    extra_spec: dict[str, object] | None = None,
) -> Path:
    fixture_dir = root / "fixture"
    fixture_dir.mkdir()
    actions = fixture_dir / "action_fixture.jsonl"
    requests = fixture_dir / "request_fixture.jsonl"
    action_rows = action_rows or [{"event_id": "action-0", "command": "true"}]
    request_rows = request_rows or [{"requests": []}]
    actions.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in action_rows))
    requests.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in request_rows))
    snapshot = fixture_dir / "pretrajectory.tar"
    _write_snapshot(snapshot)

    action_sha = adapter._sha_file(actions)
    request_sha = adapter._sha_file(requests)
    policy = {
        "endpoint": "fixture://fixed-work",
        "cache_policy": {"mode": "disabled", "key": "fixture-body"},
        "payload_identity": "immutable-jsonl",
    }
    spec = {
        "fixture_id": fixture_id,
        "fixture_kind": kind,
        "action_fixture": str(actions),
        "request_fixture": str(requests),
        "action_fixture_sha256": action_sha,
        "request_fixture_sha256": request_sha,
        "action_sequence_sha256": action_sha,
        "request_sequence_sha256": request_sha,
        "workload_sha256": adapter._sha_bytes(f"{action_sha}\0{request_sha}".encode("ascii")),
        "pretrajectory_snapshot": str(snapshot),
        "pretrajectory_snapshot_sha256": adapter._sha_file(snapshot),
        "serving_and_cache_policy": policy,
    }
    spec.update(extra_spec or {})
    manifest = root / "fixture_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": adapter.FIXTURE_MANIFEST_SCHEMA,
                "fixtures": [spec],
            },
            sort_keys=True,
        )
        + "\n"
    )
    return manifest


def _proxy_event(request: adapter.Request, response: bytes, *, status: int | None = 200) -> dict[str, object]:
    parsed_usage = adapter._usage(response) if status is not None else None
    return {
        "request_sha256": adapter._sha_bytes(request.body),
        "request_bytes": len(request.body),
        "status_code": status,
        "response_sha256": adapter._sha_bytes(response),
        "response_bytes": len(response),
        "completion_tokens": parsed_usage,
    }


def test_cpu_control_executes_immutable_work_in_supplied_scratch(tmp_path: Path) -> None:
    manifest = _write_fixture(
        tmp_path,
        action_rows=[
            {
                "event_id": "literal-shell-values",
                "command": "printf '%s' 'literal $HOME and `quotes`' > observed.txt; printf '%s' 'captured output'",
            }
        ],
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = tmp_path / "output"
    result_path = output / "replay_result.json"

    result = adapter.run_adapter(
        manifest,
        "cpu-file-traversal-v1",
        "instrument_off",
        scratch,
        output,
        result_path,
        0,
        timeout_seconds=10,
    )

    assert result["status"] == "completed"
    assert result["work_wall_ms"] > 0
    assert result["startup_wall_ms"] >= 0
    assert result["output_token_count"] == 0
    assert result["output_token_provenance"] == "no_model_requests"
    assert result["capture"] == {
        "full_production_capture_enabled": False,
        "individual_cpu_operation_records": 0,
        "physical_requests": 0,
        "raw_model_request_records": 0,
        "dropped_cpu_records": 0,
        "cpu_capture_map_failures": 0,
        "missing_raw_request_bodies": 0,
    }
    assert (scratch / "observed.txt").read_text() == "literal $HOME and `quotes`"
    assert (output / "actions" / "0000.stdout").read_text() == "captured output"
    audit = json.loads((output / "action_capture_audit.json").read_text())
    assert audit["container_resources"]["status"] == "unavailable"


def test_fixture_hash_mutation_blocks_before_work(tmp_path: Path) -> None:
    manifest = _write_fixture(tmp_path)
    loaded = adapter._load_fixture(manifest, "cpu-file-traversal-v1")
    loaded.action_path.write_text('{"command":"changed"}\n')

    with pytest.raises(adapter.AdapterError, match="action fixture hash mismatch"):
        adapter._load_fixture(manifest, "cpu-file-traversal-v1")


def test_bpf_loss_is_rejected_instead_of_reported_as_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trace_dir = tmp_path / "bpf"
    trace_dir.mkdir()
    (trace_dir / "raw_events.bin").write_bytes(b"test double stream")
    action_audit = [{"dispatched_command_sha256": "a" * 64}]

    monkeypatch.setattr(adapter, "iter_bpf_events", lambda _path, token=None: iter(({}, {})))

    def summary(*, lost: int) -> dict[str, object]:
        return {
            "actions": [
                {
                    "raw": {
                        "action_token": 11,
                        "command_sha256": "a" * 64,
                        "event_count": 2,
                        "required_event_count": 2,
                        "event_records_complete": True,
                        "perf_lost_events": 0,
                        "censored_pending": [],
                        "raw_aggregate": {
                            "lost_event_records": lost,
                            "lost_pending_records": 0,
                            "lost_path_records": 0,
                            "lineage_map_failures": 0,
                        },
                    }
                }
            ],
            "action_finalizations": [],
        }

    counts = adapter._bpf_capture_counts(summary(lost=0), trace_dir, action_audit)
    assert counts["individual_cpu_operation_records"] == 2
    with pytest.raises(adapter.AdapterError, match="BPF full capture has loss"):
        adapter._bpf_capture_counts(summary(lost=1), trace_dir, action_audit)


def test_work_interval_charges_collector_and_shell_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = SimpleNamespace(
        case_id="cpu-file-traversal-v1",
        kind="cpu_filesystem_traversal",
        actions=(adapter.Action(0, "action-0", "true", "a" * 64),),
        request_rows=(),
        spec={
            "cpu_collector": {
                "backend": "bcc",
                "attach_existing_process": True,
                "require_persistent_runtime_pid": True,
                "trace_format": "raw individual",
            }
        },
        workload_sha256="b" * 64,
        snapshot_sha256="c" * 64,
        action_sha256="d" * 64,
        request_sha256="e" * 64,
        policy_sha256="f" * 64,
    )
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    durations: list[float] = []

    class FakeService:
        client = object()

        def stop(self) -> None:
            started = time.perf_counter_ns()
            time.sleep(0.02)
            durations.append((time.perf_counter_ns() - started) / 1_000_000)

    def stop_shell(_process: object) -> None:
        started = time.perf_counter_ns()
        time.sleep(0.01)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)

    monkeypatch.setattr(adapter, "_spawn_bash_fixture", lambda: SimpleNamespace(pid=123))
    monkeypatch.setattr(adapter, "_prepare_persistent_shell", lambda *_args: None)
    monkeypatch.setattr(adapter, "_run_actions", lambda *_args: [{"dispatched_command_sha256": "a" * 64}])
    monkeypatch.setattr(adapter, "_launch_cpu_service", lambda *_args: FakeService())
    monkeypatch.setattr(adapter, "_read_summary", lambda *_args: {})
    monkeypatch.setattr(
        adapter,
        "_bpf_capture_counts",
        lambda *_args: {
            "individual_cpu_operation_records": 1,
            "dropped_cpu_records": 0,
            "cpu_capture_map_failures": 0,
        },
    )
    monkeypatch.setattr(adapter, "_stop_bash_fixture", stop_shell)

    result = adapter._cpu_condition(fixture, "instrument_on", scratch, output, 0, 10)

    assert len(durations) == 2
    assert result["work_wall_ms"] >= sum(durations)
    assert result["startup_wall_ms"] >= 0
    assert result["capture"]["individual_cpu_operation_records"] == 1
    assert result["capture"]["full_production_capture_enabled"] is False


def test_model_work_interval_charges_proxy_bpf_and_shell_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = SimpleNamespace(
        case_id="model-short-request-v1",
        kind="model_short_request",
        actions=(adapter.Action(0, "action-0", "true", "a" * 64),),
        request_rows=(),
        spec={},
        workload_sha256="b" * 64,
        snapshot_sha256="c" * 64,
        action_sha256="d" * 64,
        request_sha256="e" * 64,
        policy_sha256="f" * 64,
    )
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cleanup_durations: list[float] = []
    config = {
        "upstream_host": "127.0.0.1",
        "upstream_port": 18100,
        "timeout_seconds": 1.0,
        "max_body_bytes": 4096,
        "headers": {},
        "serving_metrics_config": {},
    }
    request = adapter.Request(0, "POST", "/v1/chat/completions", b"{}", {}, "request-0")

    class FakeService:
        client = object()

        def stop(self) -> None:
            started = time.perf_counter_ns()
            time.sleep(0.01)
            cleanup_durations.append((time.perf_counter_ns() - started) / 1_000_000)

    class FakeProxy:
        server_address = ("127.0.0.1", 18120)

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def serve_forever(self) -> None:
            return

        def request_graceful_shutdown(self) -> None:
            return

        def close_gracefully(self, *, timeout: float) -> None:
            del timeout
            started = time.perf_counter_ns()
            time.sleep(0.01)
            cleanup_durations.append((time.perf_counter_ns() - started) / 1_000_000)

    def stop_shell(_process: object) -> None:
        started = time.perf_counter_ns()
        time.sleep(0.01)
        cleanup_durations.append((time.perf_counter_ns() - started) / 1_000_000)

    from scripts.observability import request_proxy

    monkeypatch.setattr(adapter, "_model_config", lambda _fixture: config)
    monkeypatch.setattr(adapter, "_model_runtime", lambda _config, _mode, phase: {
        "observed_monotonic_ns": 1 if phase == "before" else 2})
    monkeypatch.setattr(adapter, "_model_cache_reset", lambda *_args: {})
    monkeypatch.setattr(adapter, "_parse_requests", lambda _fixture, model: (request,))
    monkeypatch.setattr(adapter, "_cpu_config", lambda _fixture: {"backend": "bcc"})
    monkeypatch.setattr(adapter, "_spawn_bash_fixture", lambda: SimpleNamespace(pid=123))
    monkeypatch.setattr(adapter, "_prepare_persistent_shell", lambda *_args: None)
    monkeypatch.setattr(adapter, "_run_actions", lambda *_args: [{"dispatched_command_sha256": "a" * 64}])
    monkeypatch.setattr(adapter, "_launch_cpu_service", lambda *_args: FakeService())
    monkeypatch.setattr(adapter, "_read_summary", lambda *_args: {})
    monkeypatch.setattr(adapter, "_stop_bash_fixture", stop_shell)
    monkeypatch.setattr(
        adapter,
        "_model_request_loop",
        lambda *_args: [
            {
                "status_code": 200,
                "response_complete": True,
                "completion_tokens": 1,
            }
        ],
    )
    monkeypatch.setattr(
        adapter,
        "_model_capture_counts",
        lambda *_args: (
            {
                "full_production_capture_enabled": False,
                "individual_cpu_operation_records": 0,
                "physical_requests": 1,
                "raw_model_request_records": 1,
                "dropped_cpu_records": 0,
                "cpu_capture_map_failures": 0,
                "missing_raw_request_bodies": 0,
            },
            1,
        ),
    )
    monkeypatch.setattr(
        adapter,
        "_bpf_capture_counts",
        lambda *_args: {
            "individual_cpu_operation_records": 1,
            "dropped_cpu_records": 0,
            "cpu_capture_map_failures": 0,
        },
    )
    monkeypatch.setattr(request_proxy, "ProxyServer", FakeProxy)

    result = adapter._model_condition(fixture, "instrument_on", scratch, output, 0, 10)

    assert len(cleanup_durations) == 3
    assert result["work_wall_ms"] >= sum(cleanup_durations)
    assert result["output_token_count"] == 1
    assert result["capture"]["full_production_capture_enabled"] is False


def test_model_instrument_on_rejects_uncaptured_fixture_actions() -> None:
    fixture = SimpleNamespace(case_id="model-short-request-v1", actions=(object(),))

    with pytest.raises(adapter.AdapterError, match="uncaptured fixture actions"):
        adapter._require_action_capture(fixture, "instrument_on", None)
    adapter._require_action_capture(fixture, "instrument_off", None)


def test_failed_request_is_preserved_and_cannot_supply_off_dispatch_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = adapter.Request(
        index=0,
        method="POST",
        path="/v1/chat/completions",
        body=b'{"prompt":"fixed"}',
        headers={},
        logical_request_id="request-0",
    )

    class FailingConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object, **_kwargs: object) -> None:
            raise ConnectionError("upstream refused")

        def close(self) -> None:
            return

    monkeypatch.setattr(adapter.http.client, "HTTPConnection", FailingConnection)
    output = tmp_path / "output"
    rows = adapter._model_request_loop(
        {"timeout_seconds": 1.0, "headers": {}},
        (request,),
        SimpleNamespace(server_address=("127.0.0.1", 18120)),
        output,
    )

    row = rows[0]
    assert row["status_code"] is None
    assert row["response_complete"] is False
    assert row["completion_tokens"] is None
    assert "upstream refused" in str(row["error"])
    assert (output / "model" / "responses" / "0000.bin").read_bytes() == b""
    assert json.loads((output / "model" / "request_audit.jsonl").read_text())["error"]

    fixture = SimpleNamespace(case_id="model-short-request-v1", spec={})
    event = _proxy_event(request, b"", status=None)
    adapter._append_jsonl(output / "model" / "request_events.jsonl", event)
    with pytest.raises(adapter.AdapterError, match="off-mode physical dispatch witness"):
        adapter._model_capture_counts(fixture, "instrument_off", output, (request,), rows)


def test_mode_switch_requires_serving_and_raw_capture_only_on(tmp_path: Path) -> None:
    request = adapter.Request(
        index=0,
        method="POST",
        path="/v1/chat/completions",
        body=b'{"prompt":"fixed"}',
        headers={},
        logical_request_id="request-0",
    )
    response = b'{"usage":{"completion_tokens":3}}'
    audit = {
        "request_sha256": adapter._sha_bytes(request.body),
        "request_bytes": len(request.body),
        "status_code": 200,
        "response_sha256": adapter._sha_bytes(response),
        "response_bytes": len(response),
        "completion_tokens": 3,
    }
    output = tmp_path / "output"
    event = dict(audit)
    adapter._append_jsonl(output / "model" / "request_events.jsonl", event)
    fixture = SimpleNamespace(case_id="model-short-request-v1", spec={})

    stale_capture = output / "model" / "telemetry_v2"
    stale_capture.mkdir(parents=True)
    with pytest.raises(adapter.AdapterError, match="v2 raw capture directory"):
        adapter._model_capture_counts(fixture, "instrument_off", output, (request,), (audit,))
    stale_capture.rmdir()

    off_capture, physical = adapter._model_capture_counts(
        fixture, "instrument_off", output, (request,), (audit,)
    )
    assert physical == 1
    assert off_capture["full_production_capture_enabled"] is False
    assert off_capture["physical_requests"] == 1
    assert off_capture["raw_model_request_records"] == 0

    with pytest.raises(adapter.AdapterError, match="instrument_on request proxy"):
        adapter._model_capture_counts(fixture, "instrument_on", output, (request,), (audit,))

    event["serving_metrics_physical_request_dispatched"] = True
    event["serving_metrics_status"] = "measured"
    (output / "model" / "request_events.jsonl").write_text(json.dumps(event) + "\n")
    telemetry = output / "model" / "telemetry_v2"
    request_artifact = telemetry / "request_payloads" / "request.bin"
    response_artifact = telemetry / "request_payloads" / "response.bin"
    adapter._write_bytes(request_artifact, request.body)
    adapter._write_bytes(response_artifact, response)
    adapter._append_jsonl(
        telemetry / "model_events.jsonl",
        {
            "terminal": True,
            "event_kind": "model_request",
            "logical_request_id": "request-0",
            "physical_request_id": "physical-0",
            "status": "success",
            "output_tokens": 3,
            "request_body_sha256": adapter._sha_bytes(request.body),
            "request_payload_artifact": {
                "request": {
                    "artifact_path": "request_payloads/request.bin",
                    "sha256": adapter._sha_bytes(request.body),
                    "complete": True,
                },
                "response": {
                    "artifact_path": "request_payloads/response.bin",
                    "sha256": adapter._sha_bytes(response),
                    "complete": True,
                },
            },
        },
    )
    # Aggregate/proxy success is insufficient without direct native evidence.
    with pytest.raises(adapter.AdapterError, match="direct native model proof unavailable"):
        adapter._model_capture_counts(fixture, "instrument_on", output, (request,), (audit,))


def _container_resource(identity: dict[str, object], *, usage_usec: int = 100) -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    raw_values = {
        "cpu.stat": f"usage_usec {usage_usec}\nuser_usec {usage_usec}\nsystem_usec 0\n",
        "cpu.max": "max 100000\n",
        "io.stat": "8:0 rbytes=0 wbytes=4096 rios=0 wios=1\n",
        "cpu.pressure": "some avg10=0.00 total=0\n",
        "io.pressure": "some avg10=0.00 total=0\n",
        "memory.pressure": "some avg10=0.00 total=0\n",
    }
    for name, raw in raw_values.items():
        files[name] = {"raw": raw, "sha256": adapter._sha_bytes(raw.encode("ascii"))}
    return {
        "schema_version": "assignment.container-resources.v1",
        "status": "measured",
        "scope": "whole_target_container_interval_context_not_per_syscall_or_predictive_feature",
        "target_pid": identity["pid"],
        "target_start_ticks": identity["start_ticks"],
        "boot_id": identity["boot_id"],
        "cgroup_membership": "0::/system.slice/docker-test.scope\n",
        "cgroup_path": "/sys/fs/cgroup/system.slice/docker-test.scope",
        "cgroup_device": 28,
        "cgroup_inode": 1001,
        "started_monotonic_ns": 10,
        "ended_monotonic_ns": 20,
        "files": files,
        "host_context": {
            "pressure/cpu": {"raw": ""},
            "pressure/io": {"raw": ""},
            "pressure/memory": {"raw": ""},
        },
    }


def test_docker_runtime_opt_in_routes_cpu_to_full_hook_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _write_fixture(
        tmp_path,
        extra_spec={"swe_runtime": {"enabled": True}},
    )
    fixture = adapter._load_fixture(manifest, "cpu-file-traversal-v1")
    expected = {"capture": {"full_production_capture_enabled": True}}
    observed: dict[str, object] = {}

    def production_path(*args: object, **kwargs: object) -> dict[str, object]:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return expected

    monkeypatch.setattr(adapter, "_cpu_condition_sweenv", production_path)
    result = adapter._cpu_condition(
        fixture,
        "instrument_on",
        tmp_path / "scratch",
        tmp_path / "output",
        2,
        10,
    )

    assert result is expected
    assert observed["args"][0] is fixture  # type: ignore[index]
    assert observed["args"][-1] == {"enabled": True}  # type: ignore[index]


def test_container_context_requires_raw_cpu_io_quota_and_pressure(tmp_path: Path) -> None:
    identity = {"pid": 321, "start_ticks": 654, "boot_id": "boot-test"}
    snapshot = {"container_resources": _container_resource(identity)}

    observed = adapter._validate_container_resource(snapshot, identity, "start/action-0")

    assert observed["status"] == "measured"
    assert observed["usage_usec"] == 100
    assert set(adapter.REQUIRED_CGROUP_CONTEXT_FILES).issubset(observed["files"])

    broken = json.loads(json.dumps(snapshot))
    del broken["container_resources"]["files"]["io.stat"]["raw"]
    with pytest.raises(adapter.AdapterError, match="lacks raw cgroup io.stat"):
        adapter._validate_container_resource(broken, identity, "start/action-0")


def test_container_context_validates_paired_stable_cgroup_brackets(tmp_path: Path) -> None:
    identity = {"pid": 321, "start_ticks": 654, "boot_id": "boot-test"}
    start = _container_resource(identity, usage_usec=100)
    end = _container_resource(identity, usage_usec=145)
    boundary = {
        "event_id": "collector-event-0",
        "start_mono_ns": 1000,
        "end_mono_ns": 2000,
        "start_snapshot": {"container_resources": start},
        "end_snapshot": {"container_resources": end},
    }
    linux_dir = tmp_path / "linux"
    linux_dir.mkdir()
    (linux_dir / "bpf_collector_manifest.json").write_text(json.dumps({"identity": identity}))
    (linux_dir / "work_summary.json").write_text(
        json.dumps(
            {
                "actions": [{"raw": {"boundary": boundary}}],
                "action_finalizations": [],
            }
        )
    )

    evidence = adapter._validate_container_context(linux_dir, ["collector-event-0"])

    assert evidence["sample_count"] == 1
    assert evidence["samples"]["collector-event-0"]["cpu_delta_usec"] == 45
    assert evidence["all_samples_measured"] is True

    end["cgroup_inode"] = 1002
    (linux_dir / "work_summary.json").write_text(
        json.dumps(
            {
                "actions": [{"raw": {"boundary": boundary}}],
                "action_finalizations": [],
            }
        )
    )
    with pytest.raises(adapter.AdapterError, match="changed cgroup binding"):
        adapter._validate_container_context(linux_dir, ["collector-event-0"])


def test_script_hook_missing_capture_source_is_blocked(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    row = {
        "event_id": "script-read-0",
        "event_kind": "script_read",
        "terminal": True,
        "status": "success",
        "availability": "measured",
        "script_read_count": 1,
        "script_container_cwd": "/tmp/fixed-work",
        "script_state": {
            "status": "known",
            "generation": 1,
            "source_event_id": None,
            "paths": [],
        },
    }

    with pytest.raises(adapter.AdapterError, match="no source BPF event identity"):
        adapter._validate_script_hook_rows(telemetry, [row])


def _script_hook_row(telemetry: Path, witness: dict[str, object]) -> dict[str, object]:
    content = b"print('fixture')\n"
    digest = hashlib.sha256(content).hexdigest()
    artifact_dir = telemetry / "script_contents"
    artifact_dir.mkdir(exist_ok=True)
    (artifact_dir / "run.py").write_bytes(content)
    return {
        "event_id": "script-read-0",
        "event_kind": "script_read",
        "terminal": True,
        "status": "success",
        "availability": "measured",
        "script_read_count": 1,
        "script_container_cwd": "/tmp/fixed-work",
        "script_cwd_witness": witness,
        "script_state": {
            "status": "known",
            "generation": 1,
            "source_event_id": "state-query-0",
            "paths": [
                {
                    "path": "/tmp/fixed-work/run.py",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "content_artifact": {
                        "artifact_path": "script_contents/run.py",
                        "sha256": digest,
                        "size_bytes": len(content),
                        "truncated": False,
                        "hash_basis": "decoded_text_utf8_reencoding",
                        "byte_exact": False,
                    },
                }
            ],
        },
    }


def test_script_hook_cwd_witness_source_controls_bpf_join_expectation(tmp_path: Path) -> None:
    telemetry = tmp_path / "telemetry"
    telemetry.mkdir()
    procfs = {
        "source": "bpf_service_procfs",
        "status": "measured",
        "service_snapshot": {
            "status": "measured",
            "container_cwd": "/tmp/fixed-work",
            "identity": {"pid": 42, "start_ticks": 7},
            "namespace_proof": {"pid_namespace_before": "pid:[1]", "pid_namespace_after": "pid:[1]"},
        },
    }
    evidence = adapter._validate_script_hook_rows(telemetry, [_script_hook_row(telemetry, procfs)])
    assert evidence["reads"][0]["cwd_witness_source"] == "bpf_service_procfs"
    assert evidence["reads"][0]["cwd_witness_shell_action"] is False

    fallback = {"source": "swerex_pwd", "status": "measured", "fallback_reason": "action cd requires native shell logical cwd"}
    evidence = adapter._validate_script_hook_rows(telemetry, [_script_hook_row(telemetry, fallback)])
    assert evidence["reads"][0]["cwd_witness_shell_action"] is True

    broken = dict(procfs, service_snapshot={**procfs["service_snapshot"], "container_cwd": "/elsewhere"})
    with pytest.raises(adapter.AdapterError, match="bound namespace proof"):
        adapter._validate_script_hook_rows(telemetry, [_script_hook_row(telemetry, broken)])
    with pytest.raises(adapter.AdapterError, match="not measured"):
        adapter._validate_script_hook_rows(telemetry, [_script_hook_row(telemetry, {"source": "swerex_pwd", "status": "unavailable"})])


@pytest.mark.parametrize("placed", [False, True])
def test_docker_off_uses_uploaded_fixture_and_charges_environment_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, placed: bool
) -> None:
    from agentic_sim.telemetry import cpu_policy
    policy = cpu_policy.policy_config("00") if placed else None
    monkeypatch.setattr(cpu_policy, "from_environment", lambda: policy)
    if placed:
        monkeypatch.setenv(cpu_policy.RUNTIME_SHA_ENV, "a" * 64)
        monkeypatch.setattr(adapter.os, "sched_getaffinity", lambda pid: {0} if pid == 42 else cpu_policy.cpu_set(cpu_policy.CONTROL_CPUSET))
    manifest = _write_fixture(
        tmp_path,
        action_rows=[{"event_id": "literal-action", "command": "printf 'fixture\\n'"}],
        extra_spec={"swe_runtime": {"enabled": True, "action_timeout_seconds": 2}},
    )
    fixture = adapter._load_fixture(manifest, "cpu-file-traversal-v1")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    output = tmp_path / "output"
    uploaded: list[tuple[str, str]] = []
    close_durations: list[float] = []

    class FakeRuntime:
        async def upload(self, request: object) -> None:
            uploaded.append((request.source_path, request.target_path))  # type: ignore[attr-defined]

    class FakeDeployment:
        container_name = "fixed-work-test-container"

        def __init__(self) -> None:
            self.runtime = FakeRuntime()

    class FakeConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeDocker:
        @classmethod
        def from_config(cls, config: FakeConfig) -> FakeDeployment:
            assert config.kwargs.get("docker_args", []) == (["--cpuset-cpus", "0"] if placed else [])
            del config
            return FakeDeployment()

    class FakeEnv:
        def __init__(self, *, deployment: FakeDeployment, repo: object, post_startup_commands: list[str]) -> None:
            del repo, post_startup_commands
            self.deployment = deployment

        def start(self) -> None:
            return

        def communicate(self, command: str, **kwargs: object) -> str:
            assert command.startswith("cd /tmp/fixed-work-")
            assert kwargs["check"] == "raise"
            return ""

        def close(self) -> None:
            started = time.perf_counter_ns()
            time.sleep(0.01)
            close_durations.append((time.perf_counter_ns() - started) / 1_000_000)

    class FakeLive:
        @staticmethod
        def container_inspect(_name: str) -> dict[str, object]:
            if placed:
                return {"inspect_returncode": 0, "inspect": [{"Id": "test-container", "State": {"Pid": 42}, "HostConfig": {
                    "CpusetCpus": "0", "CpuQuota": 0, "CpuPeriod": 0, "NanoCpus": 0,
                    "CpuShares": 0, "CpuRealtimePeriod": 0, "CpuRealtimeRuntime": 0}}]}
            return {"status": "observed"}

        @staticmethod
        def run_action(**kwargs: object) -> dict[str, object]:
            return {
                "label": kwargs["label"],
                "command": kwargs["command"],
                "command_sha256": adapter._sha_bytes(str(kwargs["command"]).encode()),
                "expected_status": kwargs["expected_status"],
                "stdout": "fixture\n",
                "exception": None,
                "callback_error": None,
                "pre_event_id": None,
            }

    class FakeAgent:
        def __init__(self, env: FakeEnv) -> None:
            self._env = env

    fake_runtime = {
        "live": FakeLive,
        "SWEEnv": FakeEnv,
        "DockerDeploymentConfig": FakeConfig,
        "DockerDeployment": FakeDocker,
        "ProbeAgent": FakeAgent,
        "UploadRequest": SimpleNamespace,
        "image": "fixed-image",
    }
    monkeypatch.setattr(adapter, "_load_pinned_swe_runtime", lambda *_args: fake_runtime)

    result = adapter._cpu_condition(fixture, "instrument_off", scratch, output, 0, 10)

    assert result["capture"]["full_production_capture_enabled"] is False
    assert result["work_wall_ms"] >= close_durations[0]
    assert uploaded == [(str(scratch), uploaded[0][1])]
    assert not (output / "telemetry").exists()
    assert not (output / "linux_work").exists()
    assert (output / "actions" / "0000.stdout").read_text() == "fixture\n"
    assert (output / "cpu_placement.json").exists() == placed


def test_production_fixture_requiring_placement_cannot_launch_unbound(tmp_path, monkeypatch):
    from agentic_sim.telemetry import cpu_policy
    monkeypatch.setattr(cpu_policy, "from_environment", lambda: None)
    manifest = _write_fixture(tmp_path, extra_spec={"swe_runtime": {"enabled": True, "cpu_policy_required": True}})
    fixture = adapter._load_fixture(manifest, "cpu-file-traversal-v1")
    with pytest.raises(adapter.AdapterError, match="hash-bound CPU placement"):
        adapter._cpu_condition(fixture, "instrument_off", tmp_path, tmp_path, 0, 10)
