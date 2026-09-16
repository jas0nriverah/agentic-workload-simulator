"""Fixture materialization checks; no server, inference, Docker or BPF launch."""
import io
import json
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts.validation import build_fixed_work_fixtures as builder
from scripts.validation import fixed_work_adapter as adapter
from scripts.validation import run_instrumentation_replay as replay


class UnitTokenizer:
    """Character tokenizer for builder logic only; never live pin evidence."""
    versions = {"test_tokenizer": "unit-only"}

    def __init__(self, _directory):
        pass

    def encode(self, text):
        return list(map(ord, text))

    def decode(self, ids):
        return "".join(map(chr, ids))

    def render(self, messages):
        return "".join(row["content"] for row in messages) + "assistant:"


def inputs(tmp_path):
    submission = tmp_path / "submission"
    submission.mkdir()
    ids = list(replay.V2_CASE_IDS)
    kinds = ["cpu_filesystem_traversal", "cpu_test_script_subprocess", "model_short_request", "model_long_context_request"]
    plan = {"fixture_ids": ids, "orders_by_repeat": {"0": "off_on", "1": "on_off", "2": "off_on"},
            "paired_repetitions": 3, "condition_pass_count": 24,
            "thresholds": {"median_relative_overhead_max": 0.05, "nearest_rank_p95_relative_overhead_max": 0.10,
                           "startup_reported_separately": True, "absolute_values_reviewed": True},
            "fixtures": [{"fixture_id": fid, "fixture_kind": kind, "required_operations": []} for fid, kind in zip(ids, kinds)],
            "passes": [{"fixture_id": fid, "repeat": repeat, "order": ("off_on", "on_off", "off_on")[repeat],
                        "pass_id": f"test:{fid}:{repeat}"} for fid in ids for repeat in range(3)]}
    plan_path = submission / "live-plan/overhead_replay_plan.v2.json"
    plan_path.parent.mkdir()
    plan_path.write_bytes(builder.canonical(plan))
    Path(str(plan_path) + ".sha256").write_text(builder.sha(plan_path.read_bytes()))
    for rel in ["verification/linux-work-overhead/file-traversal/overhead_result.json",
                "verification/linux-work-bpf-overhead-bcc/artifacts/test-script/overhead-result.json",
                "verification/bpf-native-continuation-20260908/assignment-bpf-native-pytest-v1/overhead-result.json"]:
        path = submission / rel
        path.parent.mkdir(parents=True)
        path.write_text('{"fixture_command":"unit-test-recipe-reference"}')
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    for name in builder.TOKENIZER_FILES:
        (tokenizer / name).write_text(json.dumps({"model_type": "qwen3_moe", "max_position_embeddings": 262144}) if name == "config.json" else "unit-test-bytes")
    verification = tmp_path / "model_verification.json"
    verification.write_text(json.dumps({"status": "pass", "expected_revision": builder.REVISION,
        "files": [{"name": name, "hub_revision": builder.REVISION, "verified": True,
                   "bytes": (tokenizer / name).stat().st_size, "sha256": builder.sha((tokenizer / name).read_bytes())}
                  for name in builder.TOKENIZER_FILES]}))
    return dict(submission=submission, source=builder.ROOT, tokenizer_dir=tokenizer,
                model_verification=verification, api_base="http://127.0.0.1:18100/v1",
                serving_metrics_config=tmp_path / "runtime/serving_metrics.json",
                adapter_python=Path(sys.executable).absolute(), swe_agent_root=tmp_path / "swe")


def test_tar_is_byte_stable_and_never_uses_host_metadata():
    a = builder.snapshot_bytes({"z.txt": b"z", "nested/a.txt": b"a"})
    b = builder.snapshot_bytes({"nested/a.txt": b"a", "z.txt": b"z"})
    assert a == b
    with tarfile.open(fileobj=io.BytesIO(a)) as archive:
        assert archive.getnames() == ["nested/a.txt", "z.txt"]
        assert all(row.isreg() and row.mtime == 0 and row.uid == 0 and row.mode == 0o644 for row in archive)
    for name in ["../outside", "/absolute", "x/../bad", "x\\y"]:
        with pytest.raises(ValueError, match="unsafe"):
            builder.snapshot_bytes({name: b"bad"})


def test_placeholder_tokenizer_bytes_fail_the_real_hash_contract(tmp_path):
    config = inputs(tmp_path)
    (config["tokenizer_dir"] / "tokenizer.json").write_text("{}")
    with pytest.raises(ValueError, match="tokenizer.json"):
        builder.verify_tokenizer(config["tokenizer_dir"], config["model_verification"])


def test_wrong_revision_is_rejected_before_output(tmp_path, monkeypatch):
    config = inputs(tmp_path)
    report = json.loads(config["model_verification"].read_text())
    report["expected_revision"] = "wrong-revision"
    config["model_verification"].write_text(json.dumps(report))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="revision"):
        builder.build(output=output, **config)
    assert not output.exists()


def test_manifest_binds_four_fixtures_and_all_24_conditions(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "PinnedChatTokenizer", UnitTokenizer)
    config = inputs(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    built = builder.build(output=first, **config)
    builder.build(output=second, **config)
    loaded = replay.load_manifest(first / "fixture_manifest.json")
    assert len(loaded["cases"]) == 4 and built["condition_count"] == 24
    assert [row["action_count"] for row in built["fixtures"]] == [2, 6, 1, 1]
    assert [row["request_count"] for row in built["fixtures"]] == [0, 0, 1, 1]
    for case in loaded["cases"]:
        fid = case["case_id"]
        assert case["argv_template"][:2] == ["/usr/bin/env", "ASSIGNMENT_TELEMETRY_V2_AUTO=0"]
        fixture = adapter._load_fixture(first / "fixture_manifest.json", fid)
        assert fixture.actions and adapter._cpu_config(fixture)["backend"] == "bcc"
        assert adapter._swe_runtime_config(fixture)["enabled"] is True
        for name in ["actions.jsonl", "requests.jsonl", "snapshot.tar"]:
            assert (first / "fixtures" / fid / name).read_bytes() == (second / "fixtures" / fid / name).read_bytes()
    schedule = json.loads((first / "condition_schedule.json").read_text())
    assert len(schedule) == 24
    assert [row["mode"] for row in schedule[:6]] == ["instrument_off", "instrument_on", "instrument_on", "instrument_off", "instrument_off", "instrument_on"]
    for row, target in zip(built["fixtures"][2:], builder.PROMPT_TOKENS):
        assert row["model"]["prompt_tokens"] == target
        fixture = first / "fixtures" / row["fixture_id"]
        request = json.loads((fixture / "requests.jsonl").read_text())
        assert request["body_sha256"] == builder.sha(builder.canonical(request["body"]))
        assert request["body"]["max_tokens"] == 128 and request["body"]["ignore_eos"] is True
        assert request["body"]["seed"] == 0 and request["body"]["temperature"] == 0
    with pytest.raises(ValueError, match="NEW directory"):
        builder.build(output=first, **config)
    # The existing runner validates the real strict manifest without launches.
    result = replay.run_replay(loaded, output_dir=tmp_path / "validation", execute=False, timeout_seconds=1)
    assert result["condition_pass_count"] == 24 and result["status"] == "validation_only"


def test_request_mutation_invalidates_manifest_before_condition(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "PinnedChatTokenizer", UnitTokenizer)
    output = tmp_path / "output"
    builder.build(output=output, **inputs(tmp_path))
    request = output / "fixtures/model-short-request-v1/requests.jsonl"
    request.write_text(request.read_text().replace('"seed":0', '"seed":1'))
    with pytest.raises(replay.ReplayError, match="request fixture hash mismatch"):
        replay.load_manifest(output / "fixture_manifest.json")


def test_seeded_cpu_recipes_execute_and_reset_reproduces_original_bytes(tmp_path):
    files, scripts, _ = builder.cpu_inputs(builder.ROOT)
    for index, (members, commands) in enumerate([(files, builder.FILE_COMMANDS), (scripts, builder.TEST_COMMANDS)]):
        tar = tmp_path / f"{index}.tar"
        tar.write_bytes(builder.snapshot_bytes(members))
        scratch = tmp_path / f"scratch-{index}"
        replay._extract_snapshot(tar, scratch)
        outputs = []
        for command in commands:
            result = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=scratch,
                                    capture_output=True, text=True, timeout=25)
            assert result.returncode == 0, result.stderr
            outputs.append(result.stdout)
        if index == 0:
            assert len(list((scratch / "output").glob("*.bin"))) == 24
            assert outputs[-1].strip() == "24"
            assert json.loads(outputs[0])["read_bytes"] == 24 * 4096 * 2
        else:
            assert json.loads(outputs[0])["version"] == "v1"
            assert json.loads(outputs[2])["version"] == "v2"
            assert outputs[-2].strip() == "20" and outputs[-1].strip() == "499500"
        reset = tmp_path / f"reset-{index}"
        replay._extract_snapshot(tar, reset)
        for name, payload in members.items():
            assert (reset / name).read_bytes() == payload


def test_source_order_cannot_silently_change(tmp_path, monkeypatch):
    config = inputs(tmp_path)
    path = config["submission"] / "live-plan/overhead_replay_plan.v2.json"
    plan = json.loads(path.read_text())
    plan["orders_by_repeat"]["1"] = "off_on"
    path.write_bytes(builder.canonical(plan))
    Path(str(path) + ".sha256").write_text(builder.sha(path.read_bytes()))
    with pytest.raises(ValueError, match="pair order"):
        builder.build(output=tmp_path / "output", **config)


def test_threshold_cannot_be_relaxed_during_materialization(tmp_path):
    config = inputs(tmp_path)
    path = config["submission"] / "live-plan/overhead_replay_plan.v2.json"
    plan = json.loads(path.read_text())
    plan["thresholds"]["median_relative_overhead_max"] = 0.50
    path.write_bytes(builder.canonical(plan))
    Path(str(path) + ".sha256").write_text(builder.sha(path.read_bytes()))
    with pytest.raises(ValueError, match="thresholds"):
        builder.build(output=tmp_path / "output", **config)


def test_cpu_subset_never_runs_models_or_claims_full_gate(tmp_path, monkeypatch):
    from agentic_sim.telemetry import cpu_policy
    monkeypatch.setattr(builder, "PinnedChatTokenizer", UnitTokenizer)
    bundle = tmp_path / "bundle"
    builder.build(output=bundle, **inputs(tmp_path))
    runtime = bundle / "runtime/worker-00.json"
    proof = tmp_path / "placement.json"
    proof.write_text(json.dumps({"passed": True, "policy_source_sha256": cpu_policy.policy_config("00")["source_sha256"]}))
    Path(str(proof) + ".sha256").write_text(builder.sha(proof.read_bytes()))
    calls = []

    @contextmanager
    def placement(*_args, **_kwargs):
        yield cpu_policy.policy_config("00")

    def pair(case, *, repeat, **_kwargs):
        calls.append((case["case_id"], repeat))
        condition_dirs = [tmp_path / f"unit-condition-{len(calls)}-{n}" for n in range(2)]
        for path in condition_dirs:
            path.mkdir()
            (path / "cpu_placement.json").write_text(json.dumps({"status": "measured",
                "policy": cpu_policy.policy_config("00"), "runtime_manifest_sha256": builder.sha(runtime.read_bytes()),
                "controller_affinity": sorted(cpu_policy.cpu_set(cpu_policy.CONTROL_CPUSET)),
                "container_host_init_affinity": [0]}))
        return {"case_id": case["case_id"], "valid": True, "relative_overhead_percent": 0,
                "control": {"duration_ms": 10, "output_dir": str(condition_dirs[0])},
                "treatment": {"duration_ms": 10, "output_dir": str(condition_dirs[1])}}

    monkeypatch.setattr(cpu_policy, "runtime_placement", placement)
    monkeypatch.setattr(replay, "_pair", pair)
    result = builder.cpu_subset(manifest_path=bundle / "fixture_manifest.json", output=tmp_path / "cpu",
                                runtime=runtime, runtime_sha=builder.sha(runtime.read_bytes()), placement_proof=proof, execute=True)
    assert len(calls) == 6 and all(fid.startswith("cpu-") for fid, _ in calls)
    assert result["condition_count"] == 12 and result["valid_pair_count"] == 6
    assert result["full_24_condition_gate_evaluated"] is False
    assert result["threshold_status"] == "not_evaluated_cpu_subset"
    request = json.loads((bundle / "fixtures/model-short-request-v1/cache_reset_recipe.json").read_text())
    assert request["acceptance"]["cached_tokens"] == 0
    assert request["http_200_is_not_reset_success_proof"] is True
