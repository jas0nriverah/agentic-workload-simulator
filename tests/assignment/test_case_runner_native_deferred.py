"""Production case-runner native-deferred archive/derive reconstruction."""

import json
from pathlib import Path
import pytest
from unittest.mock import patch

from scripts.assignment import sweagent_case_runner as runner
from scripts.validation import fixed_work_adapter as adapter
from tests.assignment.test_fixed_work_adapter_model_native import _fixture


@pytest.mark.parametrize("fault", [None, "server_identity", "counter_epoch", "attempt_id", "asgi_attempt_id", "asgi_case_id", "wrong_request", "duplicate"])
def test_case_runner_reconstructs_native_deferred_saved_artifact(tmp_path, fault):
    fixture, output, _request, audit, event = _fixture(
        tmp_path, deferred=True,
        case_id="wrong" if fault == "asgi_case_id" else "case",
        attempt_id="wrong" if fault == "asgi_attempt_id" else "attempt",
    )
    telemetry = output / "model/telemetry_v2"
    terminal_path = telemetry / "model_events.jsonl"
    terminal = json.loads(terminal_path.read_text())
    terminal.update(
        schema_version="assignment.telemetry.v2.model",
        phase="model_request",
        serving_metrics_record=event["serving_metrics_record"],
        transport_status_code=200,
        transport_response_complete=True,
        response_body_sha256=audit["response_sha256"],
        run_id="run", case_id="case",
        attempt_id="wrong" if fault == "attempt_id" else "attempt",
    )
    for descriptor in terminal["request_payload_artifact"].values():
        descriptor["bytes"] = (telemetry / descriptor["artifact_path"]).stat().st_size
    terminal_path.write_text((json.dumps(terminal) + "\n") * (2 if fault == "duplicate" else 1))
    for name in ("lifecycle_events.jsonl", "tool_events.jsonl", "hardware_snapshots.jsonl"):
        (telemetry / name).write_text('{"schema_version":"assignment.telemetry.v2.placeholder"}\n')

    archive = Path(fixture.spec["model"]["server_archive"]["path"])
    original = json.loads((telemetry / event["serving_metrics_record"]["path"]).read_text())
    config = {
        "mode": "v2",
        "serving_metrics": {"mode": "native_deferred", **{
            key: "wrong" if fault == key else original[key]
            for key in ("server_identity", "counter_epoch")
        }},
        "native_server_archive": {"path": archive, "sha256": adapter._sha_file(archive)},
    }
    kwargs = dict(
        output_dir=output,
        telemetry_dir=telemetry,
        telemetry_config=config,
        expected_identity={"run_id": "run", "attempt_id": "attempt", "case_id": "case"},
    )
    if fault:
        if fault == "wrong_request":
            import scripts.observability.derive_server_attribution as derive_module
            import scripts.validation.check_live_native_serving as live_module

            original_derive = derive_module.derive_server_attribution

            def wrong_request(*args, **call_kwargs):
                value = dict(original_derive(*args, **call_kwargs))
                value["target_request"] = dict(value["target_request"], engine_request_id="wrong-engine-request")
                return value

            with patch.object(derive_module, "derive_server_attribution", wrong_request), patch.object(
                live_module, "load_derive", return_value=derive_module
            ):
                with pytest.raises(runner.CaseRunnerError, match="engine ID"):
                    runner._acquire_native_server_evidence(**kwargs)
        else:
            with pytest.raises(runner.CaseRunnerError, match=fault.removeprefix("asgi_")):
                runner._acquire_native_server_evidence(**kwargs)
        return
    summary = runner._acquire_native_server_evidence(**kwargs)

    assert summary["status"] == "measured"
    assert summary["physical_request_count"] == 1
    assert summary["measured_count"] == 1
    sidecar = json.loads((output / summary["sidecar_path"]).read_text())
    assert sidecar["physical_request_id"] == "physical-0"
    assert sidecar["native_metric_source"] == "vllm_v1_finished_request_stats"
    assert sidecar["metrics"]["queue"]["value_ms"] == 100.0
    assert sidecar["verified_api_token_work"]["cached_tokens"] == 0


_CACHE_PROVENANCE = "engine_core_output.num_cached_tokens@pinned_process_outputs_finish_caller"


def _token_sidecar(cached, *, provenance=_CACHE_PROVENANCE):
    return {
        "native_measurement": {
            "finished": {
                "num_prompt_tokens": 14,
                "num_generation_tokens": 2,
                "cached_tokens": cached,
                "cached_tokens_provenance": provenance,
            }
        }
    }


@pytest.mark.parametrize("cached", [0, 7], ids=["cold-zero", "hot-positive"])
def test_native_token_work_uses_measured_cache_when_api_omits_details(cached):
    usage = {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": None}
    result = runner._validate_native_token_work(_token_sidecar(cached), usage, label="fixture")
    assert result["cached_tokens"] == cached
    assert result["api_cached_tokens"] is None
    assert result["cached_tokens_provenance"] == _CACHE_PROVENANCE
    assert usage["cached_tokens"] is None, "validator must not mutate retained API usage"


def test_native_token_work_rejects_cache_missing_from_api_and_native_wrong_caller():
    sidecar = _token_sidecar(None, provenance="unavailable_finished_request_stats_field_absent")
    with pytest.raises(runner.CaseRunnerError, match="unavailable"):
        runner._validate_native_token_work(
            sidecar, {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": None}, label="wrong-caller"
        )


def test_native_token_work_rejects_invalid_boolean_cache_count():
    with pytest.raises(runner.CaseRunnerError, match="cache-token"):
        runner._validate_native_token_work(
            _token_sidecar(True),
            {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": None},
            label="invalid-bool",
        )


def test_native_token_work_rejects_cache_count_over_prompt_work():
    with pytest.raises(runner.CaseRunnerError, match="exceeds prompt_tokens"):
        runner._validate_native_token_work(
            _token_sidecar(15),
            {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": None},
            label="overprompt",
        )


def test_native_token_work_rejects_disagreement_with_retained_api_cache_count():
    with pytest.raises(runner.CaseRunnerError, match="differs"):
        runner._validate_native_token_work(
            _token_sidecar(2),
            {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": 1},
            label="disagree-api",
        )


@pytest.mark.parametrize("fault", ["missing_usage", "num_prompt_tokens", "num_generation_tokens"])
def test_native_token_work_requires_measured_consistent_usage(fault):
    usage = {"prompt_tokens": 14, "completion_tokens": 2, "cached_tokens": 0}
    finished = _token_sidecar(None)["native_measurement"]["finished"]
    if fault == "missing_usage":
        usage = None
    else:
        finished[fault] += 1
    if fault:
        with pytest.raises(runner.CaseRunnerError):
            runner._validate_native_token_work({"native_measurement": {"finished": finished}}, usage, label="fixture")


def test_case_retains_exact_execution_snapshot_and_runtime(tmp_path):
    from tests.assignment.test_materialize_execution_snapshot import make_dirty_repo, clone_head
    from scripts.validation import materialize_execution_snapshot as snapshot
    source, _ = make_dirty_repo(tmp_path)
    execution = tmp_path / "execution"
    clone_head(source, execution)
    receipt_dir = tmp_path / "receipt"
    snapshot.materialize(source, execution, receipt_dir)
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{}\n')
    Path(str(runtime) + ".sha256").write_text(snapshot.digest(runtime) + "  runtime.json\n")
    kwargs = dict(repo=execution, output_dir=tmp_path / "case", runtime_path=runtime)
    proof = runner._bind_execution_snapshot(receipt_dir / "source_manifest.json", **kwargs)
    assert proof["status"] == "pass"
    assert len(proof["artifacts"]) == 4
    assert runner._bind_execution_snapshot(receipt_dir / "source_manifest.json", **kwargs) == proof
    (execution / "untracked_source.py").write_text("wrong bytes\n")
    with pytest.raises(ValueError, match="changed"):
        runner._bind_execution_snapshot(receipt_dir / "source_manifest.json", **kwargs)
