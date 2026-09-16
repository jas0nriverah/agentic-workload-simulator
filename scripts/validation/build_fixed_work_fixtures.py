#!/usr/bin/env python3
"""Materialize the four adopted workload classes without executing conditions.

The builder freezes deterministic source inputs, snapshots and request bodies.
It does not infer measured durations, server readiness or capture completeness.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import statistics
import sys
import tarfile
from typing import Any, Mapping
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT, ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
from scripts.validation import run_instrumentation_replay as replay

MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
PROMPT_TOKENS = (512, 60000)
OUTPUT_TOKENS = 128
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "config.json", "chat_template.jinja")
FILE_COMMANDS = (
    "python3 file_traversal.py",
    "set -o pipefail; find output -type f | LC_ALL=C sort | wc -l",
)
TEST_COMMANDS = (
    "python3 run_fixture.py",
    "python3 edit_fixture.py",
    "python3 run_fixture.py",
    "PYTHONPATH=src python3 -m unittest discover -s tests -p test_linux_work.py -v",
    "set -o pipefail; find seed -type f | LC_ALL=C sort | wc -l",
    "python3 subprocess_fixture.py",
)
FILE_SCRIPT = '''from pathlib import Path
import hashlib
import json

seeds = sorted(Path("seed").glob("*.bin"))
assert len(seeds) == 24
out = Path("output")
out.mkdir()
read_bytes = 0
write_bytes = 0
for index, source in enumerate(seeds):
    data = source.read_bytes()
    assert len(data) == 4096
    read_bytes += len(data)
    created = out / ("file-%02d.bin" % index)
    write_bytes += created.write_bytes(data)
    created.rename(out / ("renamed-%02d.bin" % index))
paths = sorted(out.rglob("*.bin"))
assert len(paths) == 24
digest = hashlib.sha256()
for path in paths:
    data = path.read_bytes()
    assert len(data) == 4096
    read_bytes += len(data)
    digest.update(data)
print(json.dumps({"files": len(paths), "read_bytes": read_bytes,
                  "write_bytes": write_bytes, "sha256": digest.hexdigest()}, sort_keys=True))
'''
TEST_SCRIPT = '''from pathlib import Path
import hashlib
import json

VERSION = "v1"
paths = sorted(Path("seed").glob("*.bin"))
assert len(paths) == 20
digest = hashlib.sha256()
for path in paths:
    data = path.read_bytes()
    assert len(data) == 8192
    digest.update(data)
print(json.dumps({"version": VERSION, "files": len(paths),
                  "sha256": digest.hexdigest()}, sort_keys=True))
'''
EDIT_SCRIPT = '''from pathlib import Path

path = Path("run_fixture.py")
source = path.read_bytes()
old = b'VERSION = "v1"'
new = b'VERSION = "v2"'
assert source.count(old) == 1
path.write_bytes(source.replace(old, new, 1))
assert path.read_bytes().count(new) == 1
'''
SUBPROCESS_SCRIPT = '''import subprocess
import sys

result = subprocess.run([sys.executable, "-c", "print(sum(range(1000)))"],
                        capture_output=True, text=True, check=True)
assert result.stdout == "499500\\n"
assert result.stderr == ""
print(result.stdout, end="")
'''
VERIFY_PAYLOAD_SCRIPT = '''from pathlib import Path
import hashlib
import json

payload = Path("payload.json").read_bytes()
expected = Path("payload.sha256").read_text().strip()
assert hashlib.sha256(payload).hexdigest() == expected
body = json.loads(payload)
assert body["max_tokens"] == 128 and body["n"] == 1
print(expected)
'''


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path: Path, value: Any) -> None:
    write(path, canonical(value) + b"\n")


def snapshot_bytes(files: Mapping[str, bytes]) -> bytes:
    """Stable uncompressed tar: no host mtime, uid, paths, or random seed."""
    if not files:
        raise ValueError("snapshot must contain actual seed files")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in sorted(files.items()):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name:
                raise ValueError(f"unsafe snapshot path: {name}")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def seed_files(count: int, size: int) -> dict[str, bytes]:
    result = {}
    for index in range(count):
        line = f"fixed-seed-{index:02d}\n".encode("ascii")
        result[f"seed/{index:02d}.bin"] = (line * (size // len(line) + 1))[:size]
    return result


def cpu_inputs(source: Path) -> tuple[dict[str, bytes], dict[str, bytes], list[dict[str, Any]]]:
    filesystem = {**seed_files(24, 4096), "file_traversal.py": FILE_SCRIPT.encode()}
    scripts = {
        **seed_files(20, 8192),
        "run_fixture.py": TEST_SCRIPT.encode(),
        "edit_fixture.py": EDIT_SCRIPT.encode(),
        "subprocess_fixture.py": SUBPROCESS_SCRIPT.encode(),
        # Empty package initializers isolate this historical unit-test scope
        # from unrelated telemetry hooks and optional runtime dependencies.
        "src/agentic_sim/__init__.py": b"",
        "src/agentic_sim/telemetry/__init__.py": b"",
    }
    bindings = []
    for original, target in (
        ("tests/telemetry/test_linux_work.py", "tests/test_linux_work.py"),
        ("src/agentic_sim/telemetry/linux_work.py", "src/agentic_sim/telemetry/linux_work.py"),
        ("src/agentic_sim/telemetry/clock.py", "src/agentic_sim/telemetry/clock.py"),
    ):
        path = source / original
        data = path.read_bytes()
        scripts[target] = data
        bindings.append({"source": str(path), "snapshot_member": target, "sha256": sha(data)})
    return filesystem, scripts, bindings


def verify_tokenizer(directory: Path, verification: Path) -> dict[str, Any]:
    report = json.loads(verification.read_bytes())
    if report.get("status") != "pass" or report.get("expected_revision") != REVISION:
        raise ValueError("model verification does not bind the adopted revision")
    rows = {row["name"]: row for row in report["files"]}
    files = []
    for name in TOKENIZER_FILES:
        data = (directory / name).read_bytes()
        row = rows.get(name, {})
        if (row.get("verified") is not True or row.get("hub_revision") != REVISION
                or sha(data) != row.get("sha256") or len(data) != row.get("bytes")):
            raise ValueError(f"pinned tokenizer/config verification failed: {name}")
        files.append({"name": name, "sha256": sha(data), "bytes": len(data)})
    config = json.loads((directory / "config.json").read_bytes())
    if config.get("model_type") != "qwen3_moe" or config.get("max_position_embeddings", 0) < 65536:
        raise ValueError("model architecture/context config differs from the adopted model")
    return {"model": MODEL, "revision": REVISION, "files": files,
            "verification_sha256": sha(verification.read_bytes()),
            "provenance_limit": "Bytes match retained verified Hub metadata; no new weight or live server attestation."}


class PinnedChatTokenizer:
    def __init__(self, directory: Path):
        import jinja2
        import tokenizers
        from jinja2.sandbox import ImmutableSandboxedEnvironment

        self.tokenizer = tokenizers.Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.template = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True).from_string(
            (directory / "chat_template.jinja").read_text()
        )
        self.versions = {"tokenizers": tokenizers.__version__, "jinja2": jinja2.__version__}

    def encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False).ids

    def decode(self, tokens: list[int]) -> str:
        return self.tokenizer.decode(tokens, skip_special_tokens=False)

    def render(self, messages: list[dict[str, str]]) -> str:
        rendered = self.template.render(messages=messages, tools=[], add_generation_prompt=True)
        # This fixture uses precisely two text roles with no tools. Check the
        # observed pinned template rendering, avoiding an inferred chat count.
        expected = "".join(f'<|im_start|>{row["role"]}\n{row["content"]}<|im_end|>\n' for row in messages)
        expected += "<|im_start|>assistant\n"
        if rendered != expected:
            raise ValueError("pinned template changed for the selected two-message request")
        return rendered


def model_payload(tokenizer: Any, target_tokens: int) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    system = "Review the supplied Python code. Describe its behavior and one useful test. Continue until the output budget is exhausted."
    corpus = "# Fixed file and script review input\n" + FILE_SCRIPT + "\n" + TEST_SCRIPT
    # Pick the source pool from token counts only, never from observed runtime.
    repetitions = max(1, (target_tokens + 256) // len(tokenizer.encode(corpus)) + 2)
    text = corpus * repetitions
    while len(tokenizer.encode(text)) < target_tokens + 256:
        text += corpus
        repetitions += 1
    pool = tokenizer.encode(text)
    retained = target_tokens
    for _ in range(32):
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": tokenizer.decode(pool[:retained])}]
        rendered = tokenizer.render(messages)
        observed = len(tokenizer.encode(rendered))
        if observed == target_tokens:
            break
        retained += target_tokens - observed
        if not 0 < retained <= len(pool):
            raise ValueError("fixed request token fitting escaped its source pool")
    else:
        raise ValueError("cannot exactly tokenize the fixed source excerpt")
    body = {"model": MODEL, "messages": messages, "max_tokens": OUTPUT_TOKENS,
            "temperature": 0.0, "top_p": 1.0, "seed": 0, "n": 1,
            "stream": False, "ignore_eos": True}
    evidence = {"prompt_tokens": observed, "output_tokens_requested": OUTPUT_TOKENS,
                "realized_output_tokens": None, "source_pool_repetitions": repetitions,
                "retained_source_tokens": retained, "source_corpus_sha256": sha(corpus.encode()),
                "source_excerpt_may_end_mid_line": True, "count_basis": "pinned_chat_template_and_tokenizer",
                "rendered_prompt_sha256": sha(rendered.encode()), "runtime_versions": tokenizer.versions}
    return body, evidence, rendered.encode()


def proposal(test_count: int) -> dict[str, Any]:
    return {
        "status": "materialization_of_user_adopted_workload_classes",
        "cpu_file": {"seed_files": 24, "seed_file_bytes": 4096, "commands": list(FILE_COMMANDS)},
        "cpu_test": {"seed_files": 20, "seed_file_bytes": 8192, "script_edits": 1,
                     "test_scope_count": test_count, "commands": list(TEST_COMMANDS),
                     "runner_adaptation": "Run the historical unittest-compatible test file using stdlib unittest; do not install pytest in the pinned image."},
        "model": {"name": MODEL, "revision": REVISION, "prompt_tokens": list(PROMPT_TOKENS),
                  "output_tokens": OUTPUT_TOKENS, "temperature": 0, "top_p": 1,
                  "seed": 0, "ignore_eos": True, "requests_per_condition": 1,
                  "prefix_caching": "preserve_production_setting", "client_cache": "disabled",
                  "warmup_requests_per_condition": 0, "cache_reset": "POST /reset_prefix_cache before each condition",
                  "cache_reset_phase": "startup_outside_work",
                  "cache_reset_http_200_not_success_proof": True,
                  "native_cached_input_tokens_required": 0,
                  "native_cached_input_tokens_equal_required": True,
                  "actual_usage_required": True, "deterministic_output_not_assumed": True},
        "paired_repetitions": 3, "orders_by_repeat": {"0": "off_on", "1": "on_off", "2": "off_on"},
        "thresholds": {"median_relative_overhead_max": 0.05, "nearest_rank_p95_relative_overhead_max": 0.10},
        "sizes_selected_before_any_fixture_timing": True,
        "representativeness": "Bounded filesystem metadata/bytes, source-edit/test/pipeline/subprocess, and short/near-capacity model request classes; no population latency representativeness is asserted.",
    }


def build(*, output: Path, submission: Path, source: Path, tokenizer_dir: Path,
          model_verification: Path, api_base: str, serving_metrics_config: Path,
          adapter_python: Path, swe_agent_root: Path, cache_policy_evidence: Path | None = None) -> dict[str, Any]:
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output must be a NEW directory; immutable fixtures are never overwritten")
    parsed = urlsplit(api_base)
    if (parsed.scheme != "http" or parsed.hostname is None or parsed.username or parsed.password
            or parsed.path.rstrip("/") != "/v1" or parsed.query or parsed.fragment):
        raise ValueError("explicit API base must be http://host:port/v1")
    if parsed.port is None:
        raise ValueError("explicit API port is required")
    if not serving_metrics_config.is_absolute() or not adapter_python.is_absolute() or not adapter_python.is_file():
        raise ValueError("serving config and existing adapter Python must have absolute paths")
    plan_path = submission / "live-plan/overhead_replay_plan.v2.json"
    plan = json.loads(plan_path.read_bytes())
    if sha(plan_path.read_bytes()) != Path(str(plan_path) + ".sha256").read_text().split()[0]:
        raise ValueError("source plan hash mismatch")
    if (plan["fixture_ids"] != list(replay.V2_CASE_IDS)
            or plan["orders_by_repeat"] != {"0": "off_on", "1": "on_off", "2": "off_on"}
            or plan["paired_repetitions"] != 3 or plan["condition_pass_count"] != 24):
        raise ValueError("source plan changes the adopted fixture identities or pair order")
    expected_kinds = ("cpu_filesystem_traversal", "cpu_test_script_subprocess", "model_short_request", "model_long_context_request")
    if [(f["fixture_id"], f["fixture_kind"]) for f in plan["fixtures"]] != list(zip(replay.V2_CASE_IDS, expected_kinds)):
        raise ValueError("source fixture declarations differ from the adopted classes")
    thresholds = plan["thresholds"]
    if (thresholds["median_relative_overhead_max"] != 0.05
            or thresholds["nearest_rank_p95_relative_overhead_max"] != 0.10
            or thresholds["startup_reported_separately"] is not True
            or thresholds["absolute_values_reviewed"] is not True):
        raise ValueError("source plan changes the adopted thresholds or timing review")
    expected_pairs = [(fid, n, ("off_on", "on_off", "off_on")[n]) for fid in replay.V2_CASE_IDS for n in range(3)]
    if ([(p["fixture_id"], p["repeat"], p["order"]) for p in plan["passes"]] != expected_pairs
            or len({p["pass_id"] for p in plan["passes"]}) != 12):
        raise ValueError("source plan changes the twelve ordered unique pairs")
    token_evidence = verify_tokenizer(tokenizer_dir, model_verification)
    tokenizer = PinnedChatTokenizer(tokenizer_dir)
    files, scripts, cpu_sources = cpu_inputs(source)
    test_count = sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
                     for node in ast.walk(ast.parse(scripts["tests/test_linux_work.py"])))
    request_material = [model_payload(tokenizer, count) for count in PROMPT_TOKENS]
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "proposal.json", proposal(test_count))
    write(output / "source/overhead_replay_plan.v2.json", plan_path.read_bytes())
    write(output / "source/model_verification.json", model_verification.read_bytes())
    runtime_sources = (
        "scripts/validation/build_fixed_work_fixtures.py", "scripts/validation/fixed_work_adapter.py",
        "scripts/validation/run_instrumentation_replay.py", "scripts/validation/check_persistent_shell_capture.py",
        "src/agentic_sim/telemetry/cpu_policy.py", "src/agentic_sim/telemetry/bpf_work.py",
        "src/agentic_sim/telemetry/sweagent_hooks.py", "src/agentic_sim/telemetry/container_resources.py",
        "src/agentic_sim/telemetry/v2.py", "src/agentic_sim/telemetry/jsonl_writer.py",
    )
    write_json(output / "source/runtime_source_hashes.json", {
        str(source / rel): sha((source / rel).read_bytes()) for rel in runtime_sources})
    for name in TOKENIZER_FILES:
        write(output / "tokenizer" / name, (tokenizer_dir / name).read_bytes())
    write_json(output / "tokenizer/verification.json", token_evidence)
    if cache_policy_evidence is not None:
        write(output / "source/prefix_cache_default_evidence.json", cache_policy_evidence.read_bytes())
    from agentic_sim.telemetry import cpu_policy
    worker_runtime = {"schema_version": "assignment-runtime-manifest.v1",
                      "purpose": "fixed-work CPU subset placement binding; not a production case runtime",
                      "runner": {"cpu_policy": cpu_policy.policy_config("00")}}
    runtime_path = output / "runtime/worker-00.json"
    write_json(runtime_path, worker_runtime)
    runtime_sha = sha(runtime_path.read_bytes())
    write(Path(str(runtime_path) + ".sha256"), (runtime_sha + "  worker-00.json\n").encode())
    historical = []
    for rel in ("verification/linux-work-overhead/file-traversal/overhead_result.json",
                "verification/linux-work-bpf-overhead-bcc/artifacts/test-script/overhead-result.json",
                "verification/bpf-native-continuation-20260908/assignment-bpf-native-pytest-v1/overhead-result.json"):
        path = submission / rel
        value = json.loads(path.read_bytes())
        historical.append({"source": str(path), "sha256": sha(path.read_bytes()),
                           "fixture_command": value["fixture_command"],
                           "use": "Recipe provenance only; historical timing is not the new fixture baseline."})
    write_json(output / "source/cpu_recipe_provenance.json", {"historical": historical, "copied_source": cpu_sources})
    runtime = {"enabled": True, "swe_agent_root": str(swe_agent_root),
               "base_image": "swebench/sweb.eval.x86_64.psf_1776_requests-1724@sha256:e369005d38858ea90f843d853cb8427a2681b7513b846d12a34cb8d52c00763e",
               "image": "assignment-persistent-shell-swe-rex-1-4-0:20260909",
               "no_build_image": True, "action_timeout_seconds": 25, "cpu_policy_required": True}
    collector = {"backend": "bcc", "attach_existing_process": True,
                 "require_persistent_runtime_pid": True,
                 "trace_format": "bcc raw individual syscall and process events plus action aggregates v2"}
    manifest = {"schema_version": replay.MANIFEST_SCHEMA_V2, "status": "fixture_bound",
                "namespace": replay.V2_NAMESPACE, "paired_repetitions": 3,
                "orders_by_repeat": plan["orders_by_repeat"],
                "conditions": {"control": "instrument_off", "treatment": "instrument_on"}, "cases": []}
    inventory = []
    for index, declaration in enumerate(plan["fixtures"]):
        fid = declaration["fixture_id"]
        fixture_dir = output / "fixtures" / fid
        cpu = index < 2
        descriptor = {"fixture_id": fid, "fixture_kind": declaration["fixture_kind"],
                      "cpu_collector": collector, "swe_runtime": runtime,
                      "required_operations": declaration["required_operations"]}
        if cpu:
            commands = FILE_COMMANDS if index == 0 else TEST_COMMANDS
            contents = files if index == 0 else scripts
            request_rows = [{"requests": [], "reason": "CPU-only fixed fixture"}]
            descriptor["serving_and_cache_policy"] = {"model_requests": "none", "endpoint": "none",
                "filesystem_cache": "natural OS cache; restore identical scratch bytes before every condition; no cache flush"}
            model_evidence = None
        else:
            body, model_evidence, rendered = request_material[index - 2]
            payload = canonical(body)
            contents = {"payload.json": payload, "payload.sha256": (sha(payload) + "\n").encode(),
                        "verify_payload.py": VERIFY_PAYLOAD_SCRIPT.encode()}
            commands = ("python3 verify_payload.py",)
            request_rows = [{"logical_request_id": fid + ":request:0", "method": "POST",
                             "path": "/v1/chat/completions", "headers": {"Content-Type": "application/json"},
                             "body": body, "body_sha256": sha(payload)}]
            cache = {"client_cache": "disabled", "server_prefix_cache": "preserve_production_setting",
                     "server_prefix_cache_verified": False,
                     "warmup_requests_before_each_condition": 0,
                     "cache_reset": {"method": "POST", "path": "/reset_prefix_cache",
                                     "phase": "startup_outside_work", "requests_before_each_condition": 1,
                                     "independent_request_labels_required": True,
                                     "no_intervening_inference_required": True,
                                     "http_200_is_not_reset_success_proof": True,
                                     "native_cached_tokens_required": 0},
                     "native_prompt_cached_and_output_token_equality_required": True,
                     "missing_native_cached_token_counts": "pair invalid; never infer hits from client policy"}
            descriptor["serving_and_cache_policy"] = {"api_base": api_base.rstrip("/"), "model": MODEL,
                "model_revision": REVISION, "tokenizer_revision": REVISION, "cache_policy": cache,
                "max_model_len_required": 65536, "max_input_tokens": 61440,
                "output_policy": {k: body[k] for k in ("max_tokens", "temperature", "top_p", "seed", "n", "stream", "ignore_eos")}}
            descriptor["model"] = {"upstream": {"host": parsed.hostname, "port": parsed.port},
                "endpoint": {"method": "POST", "path": "/v1/chat/completions"}, "cache_policy": cache,
                "serving_metrics_config": str(serving_metrics_config), "timeout_seconds": 600,
                "max_body_bytes": 2097152, "headers": {}}
            descriptor["model_execution_prerequisite"] = "Reviewed full model hook capture, actual server/cache readiness, and deferred observer witness integration. Fixture materialization is not a live capture claim."
            write(fixture_dir / "rendered_prompt.txt", rendered)
            write_json(fixture_dir / "prompt_measurement.json", model_evidence)
            write_json(fixture_dir / "cache_reset_recipe.json", {
                "status": "pending_main_recipe_review_and_adapter_integration",
                "api_base": api_base.rstrip("/"), **cache["cache_reset"],
                "body": "", "body_sha256": sha(b""),
                "acceptance": {"prompt_tokens": model_evidence["prompt_tokens"], "cached_tokens": 0,
                               "completion_tokens": OUTPUT_TOKENS, "basis": "native request-linked counts"},
                "do_not_disable_production_prefix_caching": True,
            })
        action_rows = [{"event_id": f"{fid}:action:{n}", "command": command, "expected_status": "success"}
                       for n, command in enumerate(commands)]
        actions = b"".join(canonical(row) + b"\n" for row in action_rows)
        requests = b"".join(canonical(row) + b"\n" for row in request_rows)
        snapshot = snapshot_bytes(contents)
        write(fixture_dir / "actions.jsonl", actions)
        write(fixture_dir / "requests.jsonl", requests)
        write(fixture_dir / "snapshot.tar", snapshot)
        write_json(fixture_dir / "fixed_work_fixture.json", descriptor)
        write_json(fixture_dir / "snapshot_inventory.json", [
            {"path": name, "bytes": len(data), "sha256": sha(data)} for name, data in sorted(contents.items())])
        # The adapter loads the pinned SWE runtime and invokes full hooks
        # explicitly. Suppress premature sitecustomize activation in the
        # runner's interpreter, whose site-packages do not contain SWE-agent.
        template = ["/usr/bin/env", "ASSIGNMENT_TELEMETRY_V2_AUTO=0", str(adapter_python),
                    str(source / "scripts/validation/fixed_work_adapter.py"),
                    "--fixture-manifest", "{fixture_dir}/../../fixture_manifest.json",
                    "--fixture-id", "{case_id}", "--instrumentation-mode", "{instrumentation_mode}",
                    "--scratch-dir", "{scratch_dir}", "--output-dir", "{output_dir}",
                    "--result-path", "{result_path}", "--repeat", "{repeat}"]
        case = {"case_id": fid, "fixture_dir": str(fixture_dir),
                "action_fixture": str(fixture_dir / "actions.jsonl"), "action_fixture_sha256": sha(actions),
                "request_fixture": str(fixture_dir / "requests.jsonl"), "request_fixture_sha256": sha(requests),
                "action_sequence_sha256": sha(actions), "request_sequence_sha256": sha(requests),
                "workload_sha256": replay._workload_digest(sha(actions), sha(requests)),
                "pretrajectory_snapshot": str(fixture_dir / "snapshot.tar"), "pretrajectory_snapshot_sha256": sha(snapshot),
                "argv_template": template, "argv_template_sha256": replay._template_sha(template)}
        manifest["cases"].append(case)
        inventory.append({"fixture_id": fid, "action_count": len(commands), "request_count": 0 if cpu else 1,
                          "snapshot_file_count": len(contents), "snapshot_content_bytes": sum(map(len, contents.values())),
                          "planned_test_count": test_count if index == 1 else 0,
                          "model": model_evidence, "baseline_sample_count": 0, "baseline_work_wall_ms": None,
                          "counts_provenance": "Frozen input bytes/commands, not observed BPF or response counts."})
    manifest_path = output / "fixture_manifest.json"
    write_json(manifest_path, manifest)
    write(Path(str(manifest_path) + ".sha256"), (sha(manifest_path.read_bytes()) + "  fixture_manifest.json\n").encode())
    # Exercise the actual strict runner loader before declaring inputs bound.
    replay.load_manifest(manifest_path)
    write_json(output / "fixture_inventory.json", inventory)
    schedule = []
    for pair in plan["passes"]:
        for mode in (("instrument_off", "instrument_on") if pair["order"] == "off_on" else ("instrument_on", "instrument_off")):
            schedule.append({"pass_id": pair["pass_id"], "fixture_id": pair["fixture_id"],
                             "repeat": pair["repeat"], "order": pair["order"], "mode": mode,
                             "snapshot_reset_required": True})
    write_json(output / "condition_schedule.json", schedule)
    validate = [str(adapter_python), str(source / "scripts/validation/run_instrumentation_replay.py"),
                "--manifest", str(manifest_path), "--output-dir", str(output.parent / (output.name + "-validation"))]
    execute = [*validate[:-1], str(output.parent / (output.name + "-replay")), "--execute"]
    placement_launcher = [str(adapter_python), str(cpu_policy.SOURCE), "--runtime-manifest", str(runtime_path),
                          "--runtime-sha256", runtime_sha, "--"]
    execute = placement_launcher + execute
    subset = [str(adapter_python), str(source / "scripts/validation/build_fixed_work_fixtures.py"), "cpu-subset",
              "--manifest", str(manifest_path), "--output-dir", str(output.parent / (output.name + "-cpu-subset")),
              "--runtime-manifest", str(runtime_path), "--runtime-sha256", runtime_sha,
              "--placement-proof", "{placement_proof}", "--execute"]
    write_json(output / "commands.json", {"validation_only_argv": validate, "after_server_review_argv": execute,
                "cpu_subset_argv_template": subset,
                "execution_prerequisites": ["Main reviewed server readiness and actual production prefix-cache configuration",
                    "Main-reviewed startup cold-reset recipe; independent reset labels and native cached_tokens=0",
                    "Full model hook/proxy capture and deferred observer witness integration",
                    "Runtime-policy owner verified CPU/SMT/container/collector placement"],
                "serving_metrics_config_path": str(serving_metrics_config)})
    write(output / "README.md", ("# Bound fixed-work input bundle\n\n"
        "Four deterministic workload inputs and reset snapshots are materialized. No conditions were executed. "
        "Counts describe frozen inputs; measured baselines and actual usage remain unset. "
        "The two CPU fixtures use the reviewed SWEEnv runtime sidecars. The model conditions still require "
        "reviewed full capture/deferred serving witness integration and actual server/cache readiness. "
        "Preserve production cache flags. The proposed cold recipe resets once before each condition, charges "
        "reset cost to startup, and requires independently labelled reset/request ordering plus native cached_tokens=0. "
        "HTTP 200 alone does not prove reset success. Main must review this recipe before model execution.\n\n"
        "Validation command:\n\n```sh\n" + shlex.join(validate) + "\n```\n\n"
        "After main reviews server and runtime readiness, the exact 24-condition command is:\n\n```sh\n"
        + shlex.join(execute) + "\n```\n\n"
        "See proposal.json for quantities, source/cpu_recipe_provenance.json for recipe adaptations, "
        "tokenizer/verification.json for exact tokenizer byte binding, and commands.json for live prerequisites. "
        "Runtime-policy implementation is separately owned.\n").encode())
    artifact = {"schema_version": "assignment.fixed-work-input-bundle.v1", "status": "inputs_bound_execution_pending",
                "condition_count": 24, "conditions_executed": 0, "files": [
                    {"path": str(p.relative_to(output)), "bytes": p.stat().st_size, "sha256": sha(p.read_bytes())}
                    for p in sorted(output.rglob("*")) if p.is_file()]}
    write_json(output / "artifact_manifest.json", artifact)
    write(output / "artifact_manifest.json.sha256", (sha((output / "artifact_manifest.json").read_bytes()) + "  artifact_manifest.json\n").encode())
    # Persist directory entries as well as data; no partial build is accepted
    # without the terminal artifact manifest and its matching digest.
    for directory in [*sorted((p for p in output.rglob("*") if p.is_dir()), reverse=True), output, output.parent]:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return {"status": artifact["status"], "output_dir": str(output), "fixtures": inventory,
            "manifest_sha256": sha(manifest_path.read_bytes()), "condition_count": len(schedule)}


def cpu_subset(*, manifest_path: Path, output: Path, runtime: Path, runtime_sha: str,
               placement_proof: Path, execute: bool, timeout: float = 600) -> dict[str, Any]:
    """Collect only the two CPU fixtures; never evaluate a four-fixture gate."""
    from agentic_sim.telemetry import cpu_policy

    manifest = replay.load_manifest(manifest_path)
    sources = json.loads((manifest_path.parent / "source/runtime_source_hashes.json").read_bytes())
    if any(sha(Path(path).read_bytes()) != digest for path, digest in sources.items()):
        raise ValueError("runtime source changed after fixture bundle seal; reseal inputs without changing workload bytes")
    policy = cpu_policy.load_runtime(runtime, runtime_sha)
    proof = json.loads(placement_proof.read_bytes())
    proof_sha = sha(placement_proof.read_bytes())
    if proof_sha != Path(str(placement_proof) + ".sha256").read_text().split()[0]:
        raise ValueError("placement proof SHA-256 mismatch")
    if proof.get("passed") is not True:
        raise ValueError("CPU placement reference proof did not pass")
    if output.exists():
        raise ValueError("CPU subset output must be NEW")
    output.mkdir(parents=True)
    write_json(output / "placement_binding.json", {"runtime_sha256": runtime_sha,
               "placement_proof_sha256": proof_sha, "placement_proof": str(placement_proof), "policy": policy,
               "reference_proof_matches_current_source": proof.get("policy_source_sha256") == policy["source_sha256"],
               "current_proof_basis": "live current-policy topology check and each adapter's actual predispatch Docker/process placement"})
    pairs = []
    with cpu_policy.runtime_placement(runtime, runtime_sha, active=execute):
        for case in manifest["cases"][:2]:
            for repeat in range(3):
                pair = replay._pair(case, repeat=repeat, order=manifest["orders_by_repeat"][str(repeat)],
                                    output_root=output, execute=execute, default_timeout=timeout)
                if execute and pair["valid"]:
                    for role in ("control", "treatment"):
                        record = json.loads((Path(pair[role]["output_dir"]) / "cpu_placement.json").read_bytes())
                        if (record.get("status") != "measured" or record.get("policy") != policy
                                or record.get("runtime_manifest_sha256") != runtime_sha
                                or set(record.get("controller_affinity", [])) != cpu_policy.cpu_set(policy["control_cpuset"])
                                or set(record.get("container_host_init_affinity", [])) != cpu_policy.cpu_set(policy["worker_cpuset"])):
                            raise ValueError("CPU condition lacks actual current-policy placement evidence")
                pairs.append(pair)
                print(json.dumps({"fixture": case["case_id"], "repeat": repeat, "pair_valid": pair["valid"]}), flush=True)
                if execute and not pair["valid"]:
                    break
            if execute and not pairs[-1]["valid"]:
                break
    summaries = []
    for case in manifest["cases"][:2]:
        values = [p for p in pairs if p["case_id"] == case["case_id"] and p["valid"]]
        summaries.append({"fixture_id": case["case_id"], "valid_pair_count": len(values),
                          "control_work_wall_ms": [p["control"]["duration_ms"] for p in values],
                          "instrumented_work_wall_ms": [p["treatment"]["duration_ms"] for p in values],
                          "median_overhead_percent": statistics.median([p["relative_overhead_percent"] for p in values]) if len(values) == 3 else None})
    result = {"schema_version": "assignment.fixed-work-cpu-subset.v1",
              "status": "measured" if execute else "validation_only", "cpu_only": True,
              "planned_condition_count": 12, "condition_count": len(pairs) * 2, "pair_count": len(pairs),
              "valid_pair_count": sum(p["valid"] for p in pairs), "cases": summaries,
              "full_24_condition_gate_evaluated": False, "threshold_status": "not_evaluated_cpu_subset",
              "manifest_sha256": sha(manifest_path.read_bytes()), "pairs": pairs,
              "runtime_sources_unchanged": all(sha(Path(path).read_bytes()) == digest for path, digest in sources.items())}
    if execute and not result["runtime_sources_unchanged"]:
        result["status"] = "invalid_runtime_source_changed"
        result["valid_pair_count"] = 0
    replay._atomic_json(output / "cpu_subset_evidence.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["cpu-subset"]:
        subset_parser = argparse.ArgumentParser(description="Twelve CPU conditions only; no four-fixture gate verdict")
        subset_parser.add_argument("--manifest", type=Path, required=True)
        subset_parser.add_argument("--output-dir", type=Path, required=True)
        subset_parser.add_argument("--runtime-manifest", type=Path, required=True)
        subset_parser.add_argument("--runtime-sha256", required=True)
        subset_parser.add_argument("--placement-proof", type=Path, required=True)
        subset_parser.add_argument("--execute", action="store_true")
        args = subset_parser.parse_args(argv[1:])
        result = cpu_subset(manifest_path=args.manifest, output=args.output_dir, runtime=args.runtime_manifest,
                            runtime_sha=args.runtime_sha256, placement_proof=args.placement_proof, execute=args.execute)
        print(json.dumps({k: result[k] for k in ("status", "condition_count", "valid_pair_count", "threshold_status")}))
        return 0 if not args.execute or result["valid_pair_count"] == 6 else 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--submission-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--tokenizer-dir", type=Path, required=True)
    parser.add_argument("--model-verification", type=Path, required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--serving-metrics-config", type=Path, required=True)
    parser.add_argument("--adapter-python", type=Path, required=True)
    parser.add_argument("--swe-agent-root", type=Path, required=True)
    parser.add_argument("--cache-policy-evidence", type=Path)
    args = parser.parse_args(argv)
    result = build(output=args.output_dir, submission=args.submission_root.resolve(), source=args.source_root.resolve(),
                   tokenizer_dir=args.tokenizer_dir.resolve(), model_verification=args.model_verification.resolve(),
                   api_base=args.api_base, serving_metrics_config=args.serving_metrics_config,
                   adapter_python=args.adapter_python, swe_agent_root=args.swe_agent_root.resolve(),
                   cache_policy_evidence=args.cache_policy_evidence)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
