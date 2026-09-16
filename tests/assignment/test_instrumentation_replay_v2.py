"""Approved four-fixture replay, with no synthetic live-acceptance claim."""
import copy
import io
import json
import tarfile

import pytest

from scripts.validation import run_instrumentation_replay as replay


def manifest_file(tmp_path, *, schema=replay.MANIFEST_SCHEMA_V2):
    actions = tmp_path / "actions.jsonl"
    requests = tmp_path / "requests.jsonl"
    actions.write_text('{"command":"true"}\n')
    requests.write_text('{"requests":[]}\n')
    snapshot = tmp_path / "snapshot.tar"
    with tarfile.open(snapshot, "w") as archive:
        info = tarfile.TarInfo("file.txt")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    action_sha = replay.sha256_file(actions)
    request_sha = replay.sha256_file(requests)
    template = ["unused-adapter"] + ["{" + name + "}" for name in sorted(replay.REQUIRED_PLACEHOLDERS)]
    case = dict(
        fixture_dir=str(tmp_path), action_fixture=str(actions), request_fixture=str(requests),
        action_fixture_sha256=action_sha, request_fixture_sha256=request_sha,
        action_sequence_sha256=action_sha, request_sequence_sha256=request_sha,
        workload_sha256=replay._workload_digest(action_sha, request_sha),
        pretrajectory_snapshot=str(snapshot), pretrajectory_snapshot_sha256=replay.sha256_file(snapshot),
        argv_template=template, argv_template_sha256=replay._template_sha(template),
    )
    ids = replay.V2_CASE_IDS if schema == replay.MANIFEST_SCHEMA_V2 else [f"legacy-{i}" for i in range(16)]
    manifest = dict(schema_version=schema, status="offline_fixture_bound", namespace=replay.V2_NAMESPACE,
                    paired_repetitions=3, orders_by_repeat={"0":"off_on", "1":"on_off", "2":"off_on"},
                    conditions={"control":"instrument_off", "treatment":"instrument_on"},
                    cases=[dict(case, case_id=case_id) for case_id in ids])
    path = tmp_path / "manifest.json"
    replay._atomic_json(path, manifest)
    return path, manifest


@pytest.mark.parametrize("schema,count", [(replay.MANIFEST_SCHEMA_V2,4),(replay.MANIFEST_SCHEMA,16)])
def test_validation_runs_correct_protocol_without_claiming_measurement(tmp_path, schema, count):
    path, _ = manifest_file(tmp_path, schema=schema)
    loaded = replay.load_manifest(path)
    result = replay.run_replay(loaded, output_dir=tmp_path / "out", execute=False, timeout_seconds=2)
    assert result["case_count"] == count
    assert len(result["pairs"]) == count * 3
    assert result["valid_pair_count"] == 0
    assert result["threshold_status"] != "pass"
    assert result["status"] == "validation_only"
    if count == 4:
        assert result["condition_pass_count"] == 24


def test_four_arbitrary_cases_cannot_replace_predeclared_fixtures(tmp_path):
    path, value = manifest_file(tmp_path)
    value["cases"][0]["case_id"] = "unapproved-easy-fixture"
    replay._atomic_json(path, value)
    with pytest.raises(replay.ReplayError, match="predeclared"):
        replay.load_manifest(path)


def measured_result(case):
    expected = replay._fixture_fingerprint(case)
    return dict(schema_version=replay.RESULT_SCHEMA_V2, case_id=case["case_id"], repeat=0,
                instrumentation_mode="instrument_on", status="completed", **expected,
                output_token_count=0, evidence_provenance="measured_fixed_workload",
                output_token_provenance="no_model_requests", work_wall_ms=10, startup_wall_ms=2,
                serving_and_cache_policy_sha256="a"*64,
                capture=dict(full_production_capture_enabled=True, individual_cpu_operation_records=12,
                             physical_requests=0, raw_model_request_records=0, dropped_cpu_records=0,
                             cpu_capture_map_failures=0, missing_raw_request_bodies=0))


def test_capture_loss_and_aggregate_only_cpu_evidence_fail(tmp_path):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][0]
    valid = measured_result(case)
    result_path = tmp_path / "result.json"
    for field, value in [(None,None),("dropped_cpu_records",1),("individual_cpu_operation_records",0),
                         ("full_production_capture_enabled",False),("cpu_capture_map_failures",True)]:
        result = copy.deepcopy(valid)
        if field:
            result["capture"][field] = value
        result_path.write_text(json.dumps(result))
        _, errors = replay._validate_result(result_path, case, mode="instrument_on", repeat=0,
                                            expected=replay._fixture_fingerprint(case))
        assert bool(errors) == bool(field), errors


def test_model_request_loss_is_not_a_valid_replay(tmp_path):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][2]
    value = measured_result(case)
    value["output_token_count"] = 32
    value["output_token_provenance"] = "measured_response_usage"
    value["capture"]["physical_requests"] = 1
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(value))
    _, errors = replay._validate_result(result_path, case, mode="instrument_on", repeat=0,
                                        expected=replay._fixture_fingerprint(case))
    assert any("physical request records" in error for error in errors)


@pytest.mark.parametrize("mismatch", ["output_token_count", "serving_and_cache_policy_sha256", "physical_requests"])
def test_unequal_realized_work_invalidates_pair(tmp_path, monkeypatch, mismatch):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][2]
    def condition(case, *, mode, **kwargs):
        result = {"output_token_count": 8, "serving_and_cache_policy_sha256": "a"*64,
                  "capture": {"physical_requests": 1}}
        if mode == "instrument_on":
            if mismatch == "physical_requests":
                result["capture"][mismatch] = 2
            else:
                result[mismatch] = "b"*64 if mismatch.endswith("sha256") else 9
        return {"result": result, "valid": True, "errors": [], "duration_ms": 100,
                "declared_fixture": replay._fixture_fingerprint(case)}
    monkeypatch.setattr(replay, "_run_condition", condition)
    pair = replay._pair(case, repeat=0, order="off_on", output_root=tmp_path / "out",
                        execute=True, default_timeout=1)
    assert pair["valid"] is False
    assert pair["relative_overhead"] is None
    assert pair["invalid_pair_reason"]


def test_fixture_mutation_after_load_blocks_launch(tmp_path, monkeypatch):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][0]
    case["action_fixture"].write_text('{"command":"different"}\n')
    def forbidden(*args, **kwargs):
        pytest.fail("mutated fixture must never launch")
    monkeypatch.setattr(replay, "run_owned_process", forbidden)
    with pytest.raises(replay.ReplayError, match="changed since manifest"):
        replay._pair(case, repeat=0, order="off_on", output_root=tmp_path / "out",
                     execute=True, default_timeout=1)


def test_malformed_capture_is_recorded_as_invalid_pair(tmp_path, monkeypatch):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][0]
    def condition(case, **kwargs):
        return dict(result={"capture": [1]}, valid=False, errors=["malformed capture"],
                    duration_ms=1, declared_fixture=replay._fixture_fingerprint(case))
    monkeypatch.setattr(replay, "_run_condition", condition)
    pair = replay._pair(case, repeat=0, order="off_on", output_root=tmp_path / "out",
                        execute=True, default_timeout=1)
    assert not pair["valid"]
    assert pair["relative_overhead"] is None


@pytest.mark.parametrize("bad", ["off_capture", "cpu_raw", "repeat_bool", "duration_zero", "duration_nan", "startup_negative", "legacy_schema"])
def test_invalid_measurements_fail_closed(tmp_path, bad):
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][0]
    value = measured_result(case)
    mode = "instrument_on"
    if bad == "off_capture":
        mode = value["instrumentation_mode"] = "instrument_off"
        value["capture"]["full_production_capture_enabled"] = False
    elif bad == "cpu_raw":
        value["capture"]["raw_model_request_records"] = 1
    elif bad == "repeat_bool":
        value["repeat"] = False
    elif bad == "duration_zero":
        value["work_wall_ms"] = 0
    elif bad == "duration_nan":
        value["work_wall_ms"] = float("nan")
    elif bad == "startup_negative":
        value["startup_wall_ms"] = -1
    else:
        value["schema_version"] = replay.RESULT_SCHEMA
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(value))
    _, errors = replay._validate_result(result_path, case, mode=mode, repeat=0,
                                        expected=replay._fixture_fingerprint(case))
    assert errors


@pytest.mark.parametrize("failure", [None, "duration", "cleanup"])
def test_condition_uses_work_interval_and_checks_process_envelope(tmp_path, monkeypatch, failure):
    from types import SimpleNamespace
    path, _ = manifest_file(tmp_path)
    case = replay.load_manifest(path)["cases"][0]
    ticks = iter([0, 20_000_000])
    monkeypatch.setattr(replay.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(replay, "deadline_with_timeout", lambda timeout: 123)
    def adapter(*args, **kwargs):
        value = measured_result(case)
        if failure == "duration":
            value["startup_wall_ms"] = 100
        result_path = replay.Path(kwargs["env"]["ASSIGNMENT_REPLAY_OUTPUT_DIR"]) / "replay_result.json"
        result_path.write_text(json.dumps(value))
        return SimpleNamespace(returncode=0, timed_out=False,
                               cleanup={"cleanup_complete": failure != "cleanup"})
    monkeypatch.setattr(replay, "run_owned_process", adapter)
    row = replay._run_condition(case, mode="instrument_on", repeat=0, pair_dir=tmp_path / "pair",
                                execute=True, default_timeout=1)
    assert row["valid"] is (failure is None)
    assert row["adapter_total_wall_ms"] == 20
    if failure is None:
        assert row["duration_ms"] == 10
        assert row["result"]["startup_wall_ms"] == 2
    else:
        assert any(("durations" if failure == "duration" else "cleanup") in e for e in row["errors"])
