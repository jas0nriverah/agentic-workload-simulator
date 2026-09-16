#!/usr/bin/env python3
"""Run a hash-bound fixed-work instrumentation replay.

The natural pilot trajectory is never replayed by this command.  A caller must
first provide a fixture manifest containing immutable action/request JSONL and a
pre-trajectory snapshot for every case.  The command then restores that
snapshot before each condition, invokes an explicit argv template with an
explicit ``instrument_off``/``instrument_on`` token, and requires the invoked
adapter to return measured workload and output-token evidence.  Missing or
unequal evidence makes a pair invalid; the utility never fills a missing value
or compares unequal work.

Validation is the default.  ``--execute`` is required to launch the supplied
adapter.  This utility has no SWE-agent, model, evaluator, or SSH defaults and
does not infer a runner command from the ordinary stochastic case runner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.case_lifecycle import (  # noqa: E402
    deadline_with_timeout,
    run_owned_process,
)


MANIFEST_SCHEMA = "assignment.instrumentation-replay-manifest.v1"
RESULT_SCHEMA = "assignment.instrumentation-replay-result.v1"
EVIDENCE_SCHEMA = "assignment.instrumentation-replay-evidence.v1"
# The v1 constants above are deliberately retained for the legacy 16-case
# protocol.  The approved production overhead contract has its own schema so
# a four-fixture manifest cannot silently change the meaning of an old run.
MANIFEST_SCHEMA_V2 = "assignment.instrumentation-replay-manifest.v2"
RESULT_SCHEMA_V2 = "assignment.instrumentation-replay-result.v2"
EVIDENCE_SCHEMA_V2 = "assignment.instrumentation-replay-evidence.v2"
CONDITION_SCHEMA_V2 = "assignment.instrumentation-replay-condition.v2"
PAIR_SCHEMA_V2 = "assignment.instrumentation-replay-pair.v2"
V2_NAMESPACE = "instrumentation-pilot-overhead-v2"
V2_CASE_IDS = (
    "cpu-file-traversal-v1",
    "cpu-test-script-subprocess-v1",
    "model-short-request-v1",
    "model-long-context-request-v1",
)
V2_REPETITIONS = 3
V2_PAIR_COUNT = len(V2_CASE_IDS) * V2_REPETITIONS
CONDITION_MODES = ("instrument_off", "instrument_on")
V2_CONDITION_PASS_COUNT = V2_PAIR_COUNT * len(CONDITION_MODES)
ORDERS = ("off_on", "on_off", "off_on")
REQUIRED_PLACEHOLDERS = frozenset(
    {
        "case_id",
        "fixture_dir",
        "scratch_dir",
        "output_dir",
        "result_path",
        "instrumentation_mode",
        "repeat",
    }
)
HEX64 = set("0123456789abcdef")


class ReplayError(ValueError):
    """A replay manifest, fixture, or measured result is unsafe."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise ReplayError(message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ReplayError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _absolute_regular(path_value: Any, label: str) -> Path:
    _fail(isinstance(path_value, str) and bool(path_value.strip()), f"{label} must be a non-empty path")
    raw = Path(path_value).expanduser()
    _fail(raw.is_absolute(), f"{label} must be absolute")
    _fail(not raw.is_symlink(), f"{label} must not be a symlink")
    path = raw.resolve()
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {raw}")
    return path


def _absolute_directory(path_value: Any, label: str) -> Path:
    _fail(isinstance(path_value, str) and bool(path_value.strip()), f"{label} must be a non-empty path")
    raw = Path(path_value).expanduser()
    _fail(raw.is_absolute(), f"{label} must be absolute")
    _fail(not raw.is_symlink(), f"{label} must not be a symlink")
    path = raw.resolve()
    _fail(path.is_dir() and not path.is_symlink(), f"{label} must be an existing directory: {raw}")
    return path


def _sha_sidecar(path: Path) -> str:
    sidecar = Path(str(path) + ".sha256")
    _fail(sidecar.is_file() and not sidecar.is_symlink(), f"missing regular SHA-256 sidecar: {sidecar}")
    expected = f"{sha256_file(path)}  {path.name}\n"
    _fail(sidecar.read_text(encoding="utf-8") == expected, f"SHA-256 sidecar mismatch: {sidecar}")
    return expected.split("  ", 1)[0]


def _digest_field(value: Any, label: str) -> str:
    _fail(isinstance(value, str) and len(value) == 64 and not (set(value.lower()) - HEX64), f"{label} must be a SHA-256 digest")
    return value.lower()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"cannot read {label}: {path}: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _validate_jsonl(path: Path, label: str) -> str:
    payload = path.read_bytes()
    _fail(bool(payload), f"{label} is empty: {path}")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReplayError(f"{label} is not UTF-8: {path}") from exc
    _fail(bool(lines), f"{label} has no records: {path}")
    for number, line in enumerate(lines, 1):
        _fail(bool(line.strip()), f"{label} has a blank line at {number}: {path}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"{label} line {number} is not JSON: {path}") from exc
        _fail(isinstance(row, dict), f"{label} line {number} must be a JSON object: {path}")
    return _sha_bytes(payload)


def _workload_digest(action_sha: str, request_sha: str) -> str:
    return _sha_bytes(f"{action_sha}\0{request_sha}".encode("ascii"))


def _placeholder_names(template: Sequence[str]) -> set[str]:
    names: set[str] = set()
    formatter = __import__("string").Formatter()
    for token in template:
        _fail(isinstance(token, str) and token and "\x00" not in token, "argv_template contains an empty or NUL token")
        try:
            fields = formatter.parse(token)
            for _literal, field_name, _format_spec, _conversion in fields:
                if field_name:
                    _fail(field_name.isidentifier(), f"argv_template has unsupported placeholder: {field_name}")
                    names.add(field_name)
        except ValueError as exc:
            raise ReplayError(f"argv_template has invalid format syntax: {token}") from exc
    return names


def _template_sha(template: Sequence[str]) -> str:
    return _sha_bytes((_canonical(list(template)) + "\n").encode("utf-8"))


def _safe_member_name(name: str) -> PurePosixPath:
    # Import locally so the public imports stay small and the validation is
    # explicit about POSIX archive names regardless of host platform.
    from pathlib import PurePosixPath

    _fail(name not in {"", "."}, "snapshot contains an empty archive member")
    pure = PurePosixPath(name)
    _fail(not pure.is_absolute(), "snapshot contains an absolute archive member")
    _fail(".." not in pure.parts, "snapshot contains a parent-traversal archive member")
    _fail("\\" not in name, "snapshot contains a backslash archive member")
    return pure


def _validate_snapshot(path: Path) -> None:
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
    except (OSError, tarfile.TarError) as exc:
        raise ReplayError(f"pretrajectory snapshot is not a readable tar archive: {path}") from exc
    _fail(bool(members), f"pretrajectory snapshot is empty: {path}")
    names: set[str] = set()
    for member in members:
        _safe_member_name(member.name)
        _fail(member.name not in names, f"snapshot contains duplicate member: {member.name}")
        names.add(member.name)
        _fail(member.isdir() or member.isreg(), f"snapshot member is not a regular file/directory: {member.name}")
        _fail(not member.issym() and not member.islnk(), f"snapshot links are not allowed: {member.name}")
        _fail(not (member.mode & stat.S_ISUID or member.mode & stat.S_ISGID), f"snapshot contains set-id member: {member.name}")


def _extract_snapshot(path: Path, destination: Path) -> dict[str, Any]:
    _fail(not destination.exists() and not destination.is_symlink(), f"scratch directory already exists: {destination}")
    destination.mkdir(parents=True, exist_ok=False)
    count = 0
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                pure = _safe_member_name(member.name)
                target = destination.joinpath(*pure.parts)
                _fail(target.resolve().is_relative_to(destination.resolve()), f"snapshot member escapes scratch directory: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(member.mode & 0o777)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    _fail(source is not None, f"snapshot file cannot be read: {member.name}")
                    with source, target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                    target.chmod(member.mode & 0o777)
                count += 1
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {"path": str(destination), "member_count": count, "snapshot_sha256": sha256_file(path)}


def _validate_fixture(case: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "case_id",
        "fixture_dir",
        "action_fixture",
        "action_fixture_sha256",
        "request_fixture",
        "request_fixture_sha256",
        "action_sequence_sha256",
        "request_sequence_sha256",
        "workload_sha256",
        "pretrajectory_snapshot",
        "pretrajectory_snapshot_sha256",
        "argv_template",
        "argv_template_sha256",
    }
    _fail(set(case) == required, f"replay case has missing or unknown fields: {case.get('case_id')}")
    case_id = case["case_id"]
    _fail(isinstance(case_id, str) and bool(case_id.strip()) and "\x00" not in case_id, "case_id must be non-empty text")
    fixture_dir = _absolute_directory(case["fixture_dir"], f"{case_id}.fixture_dir")
    action_path = _absolute_regular(case["action_fixture"], f"{case_id}.action_fixture")
    request_path = _absolute_regular(case["request_fixture"], f"{case_id}.request_fixture")
    _fail(action_path.parent == fixture_dir or fixture_dir in action_path.parents, f"{case_id}: action fixture must be under fixture_dir")
    _fail(request_path.parent == fixture_dir or fixture_dir in request_path.parents, f"{case_id}: request fixture must be under fixture_dir")
    action_sha = _validate_jsonl(action_path, f"{case_id} action fixture")
    request_sha = _validate_jsonl(request_path, f"{case_id} request fixture")
    _fail(action_sha == _digest_field(case["action_fixture_sha256"], f"{case_id}.action_fixture_sha256"), f"{case_id}: action fixture hash mismatch")
    _fail(request_sha == _digest_field(case["request_fixture_sha256"], f"{case_id}.request_fixture_sha256"), f"{case_id}: request fixture hash mismatch")
    expected_workload = _workload_digest(action_sha, request_sha)
    _fail(expected_workload == _digest_field(case["workload_sha256"], f"{case_id}.workload_sha256"), f"{case_id}: workload hash mismatch")
    _fail(action_sha == _digest_field(case["action_sequence_sha256"], f"{case_id}.action_sequence_sha256"), f"{case_id}: action sequence hash mismatch")
    _fail(request_sha == _digest_field(case["request_sequence_sha256"], f"{case_id}.request_sequence_sha256"), f"{case_id}: request sequence hash mismatch")
    snapshot = _absolute_regular(case["pretrajectory_snapshot"], f"{case_id}.pretrajectory_snapshot")
    snapshot_sha = sha256_file(snapshot)
    _fail(snapshot_sha == _digest_field(case["pretrajectory_snapshot_sha256"], f"{case_id}.pretrajectory_snapshot_sha256"), f"{case_id}: snapshot hash mismatch")
    _validate_snapshot(snapshot)
    template = case["argv_template"]
    _fail(isinstance(template, list) and bool(template), f"{case_id}.argv_template must be a non-empty argv list")
    names = _placeholder_names(template)
    missing = sorted(REQUIRED_PLACEHOLDERS - names)
    _fail(not missing, f"{case_id}: argv_template is missing placeholders {missing}")
    template_sha = _template_sha(template)
    _fail(template_sha == _digest_field(case["argv_template_sha256"], f"{case_id}.argv_template_sha256"), f"{case_id}: argv_template hash mismatch")
    return {
        "case_id": case_id,
        "fixture_dir": fixture_dir,
        "action_fixture": action_path,
        "request_fixture": request_path,
        "action_sequence_sha256": action_sha,
        "request_sequence_sha256": request_sha,
        "workload_sha256": expected_workload,
        "pretrajectory_snapshot": snapshot,
        "pretrajectory_snapshot_sha256": snapshot_sha,
        "argv_template": [str(item) for item in template],
        "argv_template_sha256": template_sha,
        "working_directory": _absolute_directory(case["working_directory"], f"{case_id}.working_directory") if "working_directory" in case else None,
        "timeout_seconds": case.get("timeout_seconds"),
    }


def load_manifest(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    _fail(path.is_file() and not path.is_symlink(), f"replay manifest is unavailable: {path}")
    _sha_sidecar(path)
    value = _read_json(path, "replay manifest")
    required = {
        "schema_version",
        "status",
        "namespace",
        "paired_repetitions",
        "orders_by_repeat",
        "conditions",
        "cases",
    }
    _fail(set(value) == required, "replay manifest has missing or unknown fields")
    schema = value["schema_version"]
    _fail(schema in {MANIFEST_SCHEMA, MANIFEST_SCHEMA_V2}, "unsupported replay manifest schema")
    v2 = schema == MANIFEST_SCHEMA_V2
    _fail(isinstance(value["status"], str) and value["status"] in {"fixture_bound", "offline_fixture_bound"}, "replay manifest status must be fixture_bound")
    _fail(isinstance(value["namespace"], str) and bool(value["namespace"]), "replay manifest namespace is invalid")
    _fail(value["paired_repetitions"] == 3, "fixed-work replay requires exactly three paired repetitions")
    orders = value["orders_by_repeat"]
    _fail(orders == {"0": "off_on", "1": "on_off", "2": "off_on"}, "replay orders must be off_on/on_off/off_on")
    conditions = value["conditions"]
    _fail(conditions == {"control": "instrument_off", "treatment": "instrument_on"}, "replay conditions must bind control/treatment modes")
    cases = value["cases"]
    count = len(V2_CASE_IDS) if v2 else 16
    _fail(isinstance(cases, list) and len(cases) == count, f"replay manifest must contain exactly {count} cases")
    if v2:
        _fail(value["namespace"] == V2_NAMESPACE, "v2 replay namespace mismatch")
    normalized = []
    seen: set[str] = set()
    for case in cases:
        _fail(isinstance(case, dict), "replay case must be an object")
        normalized_case = _validate_fixture(case)
        normalized_case["replay_schema"] = schema
        _fail(normalized_case["case_id"] not in seen, f"duplicate replay case_id: {normalized_case['case_id']}")
        seen.add(normalized_case["case_id"])
        timeout = normalized_case["timeout_seconds"]
        if timeout is not None:
            _fail(isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and math.isfinite(float(timeout)) and float(timeout) > 0, f"{normalized_case['case_id']}: timeout_seconds must be positive")
        normalized.append(normalized_case)
    if v2:
        _fail(seen == set(V2_CASE_IDS), "v2 replay requires the four predeclared fixture identities")
    return {"path": path, "schema_version": schema, "namespace": value["namespace"], "cases": normalized, "orders_by_repeat": orders, "conditions": conditions}


def _atomic_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    digest = _sha_bytes(payload)
    sidecar = Path(str(path) + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _format_argv(case: Mapping[str, Any], *, mode: str, repeat: int, output_dir: Path, scratch_dir: Path) -> list[str]:
    values = {
        "case_id": str(case["case_id"]),
        "fixture_dir": str(case["fixture_dir"]),
        "scratch_dir": str(scratch_dir),
        "output_dir": str(output_dir),
        "result_path": str(output_dir / "replay_result.json"),
        "instrumentation_mode": mode,
        "repeat": str(repeat),
    }
    try:
        argv = [token.format(**values) for token in case["argv_template"]]
    except (KeyError, ValueError) as exc:
        raise ReplayError(f"{case['case_id']}: cannot expand argv_template: {exc}") from exc
    _fail(argv and all(isinstance(token, str) and token and "\x00" not in token for token in argv), f"{case['case_id']}: expanded argv is invalid")
    return argv


def _fixture_fingerprint(case: Mapping[str, Any]) -> dict[str, str]:
    action_sha = sha256_file(case["action_fixture"])
    request_sha = sha256_file(case["request_fixture"])
    snapshot_sha = sha256_file(case["pretrajectory_snapshot"])
    return {
        "workload_sha256": _workload_digest(action_sha, request_sha),
        "action_sequence_sha256": action_sha,
        "request_sequence_sha256": request_sha,
        "pretrajectory_snapshot_sha256": snapshot_sha,
    }


def _condition_env(case: Mapping[str, Any], *, mode: str, repeat: int, scratch_dir: Path, output_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    if mode == "instrument_on":
        environment["ASSIGNMENT_TELEMETRY_V2_AUTO"] = "1"
    else:
        environment.pop("ASSIGNMENT_TELEMETRY_V2_AUTO", None)
    environment.update(
        {
            "ASSIGNMENT_REPLAY_MODE": mode,
            "ASSIGNMENT_REPLAY_CASE_ID": str(case["case_id"]),
            "ASSIGNMENT_REPLAY_REPEAT": str(repeat),
            "ASSIGNMENT_REPLAY_FIXTURE_DIR": str(case["fixture_dir"]),
            "ASSIGNMENT_REPLAY_SCRATCH_DIR": str(scratch_dir),
            "ASSIGNMENT_REPLAY_OUTPUT_DIR": str(output_dir),
            "ASSIGNMENT_REPLAY_ACTION_FIXTURE": str(case["action_fixture"]),
            "ASSIGNMENT_REPLAY_REQUEST_FIXTURE": str(case["request_fixture"]),
        }
    )
    return environment


def _validate_result(path: Path, case: Mapping[str, Any], *, mode: str, repeat: int, expected: Mapping[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not path.is_file() or path.is_symlink():
        return None, ["adapter did not produce a regular replay_result.json"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"adapter result is not valid JSON: {type(exc).__name__}"]
    if not isinstance(value, dict):
        return None, ["adapter result must be a JSON object"]
    required = {
        "schema_version",
        "case_id",
        "repeat",
        "instrumentation_mode",
        "status",
        "workload_sha256",
        "pretrajectory_snapshot_sha256",
        "action_sequence_sha256",
        "request_sequence_sha256",
        "output_token_count",
        "evidence_provenance",
        "output_token_provenance",
    }
    v2 = case.get("replay_schema") == MANIFEST_SCHEMA_V2
    if v2:
        required |= {"capture", "serving_and_cache_policy_sha256", "work_wall_ms", "startup_wall_ms"}
    if set(value) != required:
        errors.append("adapter result has missing or unknown fields")
    if value.get("schema_version") != (RESULT_SCHEMA_V2 if v2 else RESULT_SCHEMA):
        errors.append("adapter result schema mismatch")
    if value.get("case_id") != case["case_id"]:
        errors.append("adapter result case_id mismatch")
    if type(value.get("repeat")) is not int or value.get("repeat") != repeat:
        errors.append("adapter result repeat mismatch")
    if value.get("instrumentation_mode") != mode:
        errors.append("adapter result instrumentation_mode mismatch")
    if value.get("status") != "completed":
        errors.append("adapter result status is not completed")
    for name in ("workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256", "request_sequence_sha256"):
        if value.get(name) != expected[name]:
            errors.append(f"adapter result {name} mismatch")
    token_count = value.get("output_token_count")
    if not isinstance(token_count, int) or isinstance(token_count, bool) or token_count < 0:
        errors.append("adapter result output_token_count is not a measured non-negative integer")
    if value.get("evidence_provenance") != "measured_fixed_workload":
        errors.append("adapter result evidence_provenance is not measured_fixed_workload")
    cpu_fixture = v2 and str(case["case_id"]).startswith("cpu-")
    expected_token_provenance = "no_model_requests" if cpu_fixture else "measured_response_usage"
    if value.get("output_token_provenance") != expected_token_provenance:
        errors.append("adapter result output_token_provenance is not measured_response_usage")
    if cpu_fixture and token_count != 0:
        errors.append("CPU-only fixture must report zero output tokens")
    if v2:
        for field in ("work_wall_ms", "startup_wall_ms"):
            number = value.get(field)
            if type(number) not in (float, int) or not math.isfinite(number) or number < 0 or (field == "work_wall_ms" and number == 0):
                errors.append(f"adapter result {field} is not a measured finite duration")
        try:
            _digest_field(value.get("serving_and_cache_policy_sha256"), "serving/cache policy")
        except ReplayError as exc:
            errors.append(str(exc))
        capture = value.get("capture")
        fields = {"full_production_capture_enabled", "individual_cpu_operation_records", "physical_requests", "raw_model_request_records", "dropped_cpu_records", "cpu_capture_map_failures", "missing_raw_request_bodies"}
        if not isinstance(capture, dict) or set(capture) != fields:
            errors.append("v2 capture evidence is missing or malformed")
        else:
            if capture["full_production_capture_enabled"] is not (mode == "instrument_on"):
                errors.append("capture mode does not match replay condition")
            for field in fields - {"full_production_capture_enabled"}:
                if type(capture[field]) is not int or capture[field] < 0:
                    errors.append(f"capture {field} must be a nonnegative integer")
            if not errors and mode == "instrument_on":
                if any(capture[field] for field in ("dropped_cpu_records", "cpu_capture_map_failures", "missing_raw_request_bodies")):
                    errors.append("required capture evidence was lost")
                if cpu_fixture and capture["individual_cpu_operation_records"] <= 0:
                    errors.append("CPU fixture has no individual CPU operation records")
                if not cpu_fixture and (capture["physical_requests"] <= 0 or capture["raw_model_request_records"] != capture["physical_requests"]):
                    errors.append("model fixture is missing physical request records")
            if mode == "instrument_off" and any(capture[field] != 0 for field in fields - {"full_production_capture_enabled", "physical_requests"}):
                errors.append("off-mode unexpectedly reports capture records or loss")
            if cpu_fixture and capture["raw_model_request_records"] != 0:
                errors.append("CPU-only fixture reports raw model request records")
            if cpu_fixture and capture["physical_requests"] != 0:
                errors.append("CPU-only fixture dispatched a model request")
    return value, errors


def _run_condition(case: Mapping[str, Any], *, mode: str, repeat: int, pair_dir: Path, execute: bool, default_timeout: float) -> dict[str, Any]:
    condition_dir = pair_dir / mode
    scratch_dir = pair_dir / "scratch" / mode
    condition_dir.mkdir(parents=True, exist_ok=False)
    expected = {name: case[name] for name in (
        "workload_sha256", "action_sequence_sha256", "request_sequence_sha256",
        "pretrajectory_snapshot_sha256",
    )}
    _fail(_fixture_fingerprint(case) == expected, "fixture changed since manifest validation")
    declared = {
        "case_id": case["case_id"],
        "repeat": repeat,
        "instrumentation_mode": mode,
        **expected,
    }
    before_fixture = _fixture_fingerprint(case)
    reset = _extract_snapshot(case["pretrajectory_snapshot"], scratch_dir) if execute else {"path": str(scratch_dir), "member_count": None, "snapshot_sha256": expected["pretrajectory_snapshot_sha256"]}
    argv = _format_argv(case, mode=mode, repeat=repeat, output_dir=condition_dir, scratch_dir=scratch_dir)
    command_sha = _sha_bytes((_canonical(argv) + "\n").encode("utf-8"))
    stdout_path = condition_dir / "adapter.stdout.log"
    stderr_path = condition_dir / "adapter.stderr.log"
    started = time.monotonic_ns()
    returncode: int | None = None
    timed_out = False
    cleanup: Mapping[str, Any] = {"cleanup_complete": True, "launched": False}
    errors: list[str] = []
    if execute:
        timeout = float(case["timeout_seconds"] if case["timeout_seconds"] is not None else default_timeout)
        deadline = deadline_with_timeout(timeout)
        environment = _condition_env(case, mode=mode, repeat=repeat, scratch_dir=scratch_dir, output_dir=condition_dir)
        working_directory = case["working_directory"] or ROOT
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            try:
                outcome = run_owned_process(argv, cwd=str(working_directory), env=environment, stdout=stdout, stderr=stderr, deadline_mono_ns=deadline)
            except BaseException as exc:
                errors.append(f"adapter launch failed: {type(exc).__name__}: {exc}")
            else:
                returncode = outcome.returncode
                timed_out = bool(outcome.timed_out)
                cleanup = dict(outcome.cleanup)
                if returncode != 0:
                    errors.append(f"adapter returned {returncode}")
                if timed_out:
                    errors.append("adapter timed out")
                if cleanup.get("cleanup_complete") is not True:
                    errors.append("adapter process cleanup incomplete")
    else:
        errors.append("validation_only")
    ended = time.monotonic_ns()
    after_fixture = _fixture_fingerprint(case)
    if after_fixture != before_fixture:
        errors.append("fixture changed during replay")
    if execute:
        result, result_errors = _validate_result(condition_dir / "replay_result.json", case, mode=mode, repeat=repeat, expected=expected)
        errors.extend(result_errors)
    else:
        result = None
    adapter_wall_ms = (ended - started) / 1_000_000
    work_wall_ms = adapter_wall_ms
    if case.get("replay_schema") == MANIFEST_SCHEMA_V2 and result is not None and not errors:
        work_wall_ms = result["work_wall_ms"]
        if work_wall_ms + result["startup_wall_ms"] > adapter_wall_ms + 1.0:
            errors.append("adapter phase durations exceed measured process wall time")
    row = {
        "schema_version": CONDITION_SCHEMA_V2 if case.get("replay_schema") == MANIFEST_SCHEMA_V2 else "assignment.instrumentation-replay-condition.v1",
        "status": "measured" if execute and not errors else ("invalid" if execute else "not_run"),
        "case_id": case["case_id"],
        "repeat": repeat,
        "instrumentation_mode": mode,
        "declared_fixture": declared,
        "argv_sha256": command_sha,
        "argv_template_sha256": case["argv_template_sha256"],
        "output_dir": str(condition_dir),
        "scratch_reset": reset,
        "started_mono_ns": started,
        "ended_mono_ns": ended,
        "duration_ms": work_wall_ms,
        "adapter_total_wall_ms": adapter_wall_ms,
        "returncode": returncode,
        "timed_out": timed_out,
        "cleanup": dict(cleanup),
        "result": result,
        "result_sha256": sha256_file(condition_dir / "replay_result.json") if (condition_dir / "replay_result.json").is_file() and not (condition_dir / "replay_result.json").is_symlink() else None,
        "errors": errors,
        "valid": bool(execute and not errors and result is not None),
    }
    _atomic_json(condition_dir / "condition_evidence.json", row)
    return row


def _pair(case: Mapping[str, Any], *, repeat: int, order: str, output_root: Path, execute: bool, default_timeout: float) -> dict[str, Any]:
    pair_dir = output_root / _sha_bytes(str(case["case_id"]).encode("utf-8"))[:16] / f"repeat-{repeat:03d}"
    pair_dir.mkdir(parents=True, exist_ok=False)
    first, second = ("instrument_off", "instrument_on") if order == "off_on" else ("instrument_on", "instrument_off")
    rows = {
        first: _run_condition(case, mode=first, repeat=repeat, pair_dir=pair_dir, execute=execute, default_timeout=default_timeout),
        second: _run_condition(case, mode=second, repeat=repeat, pair_dir=pair_dir, execute=execute, default_timeout=default_timeout),
    }
    errors: list[str] = []
    for mode in CONDITION_MODES:
        errors.extend(f"{mode}: {error}" for error in rows[mode]["errors"])
    control = rows["instrument_off"]
    treatment = rows["instrument_on"]
    for key in ("workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256", "request_sequence_sha256"):
        if control["declared_fixture"].get(key) != treatment["declared_fixture"].get(key):
            errors.append(f"{key} differs between conditions")
    control_tokens = (control.get("result") or {}).get("output_token_count")
    treatment_tokens = (treatment.get("result") or {}).get("output_token_count")
    if control_tokens != treatment_tokens:
        errors.append("output_token_count differs between conditions")
    if case.get("replay_schema") == MANIFEST_SCHEMA_V2:
        if (control.get("result") or {}).get("serving_and_cache_policy_sha256") != (treatment.get("result") or {}).get("serving_and_cache_policy_sha256"):
            errors.append("serving/cache policy differs between conditions")
        control_capture = (control.get("result") or {}).get("capture")
        treatment_capture = (treatment.get("result") or {}).get("capture")
        if not isinstance(control_capture, dict) or not isinstance(treatment_capture, dict):
            errors.append("missing or malformed capture evidence between conditions")
        elif control_capture.get("physical_requests") != treatment_capture.get("physical_requests"):
            errors.append("physical request count differs between conditions")
    valid = control["valid"] and treatment["valid"] and not errors
    relative = (treatment["duration_ms"] / control["duration_ms"] - 1.0) if valid and control["duration_ms"] > 0 else None
    row = {
        "schema_version": PAIR_SCHEMA_V2 if case.get("replay_schema") == MANIFEST_SCHEMA_V2 else "assignment.instrumentation-replay-pair.v1",
        "case_id": case["case_id"],
        "repeat": repeat,
        "order": order,
        "control_mode": "instrument_off",
        "treatment_mode": "instrument_on",
        "control": rows["instrument_off"],
        "treatment": rows["instrument_on"],
        "output_token_count": control_tokens if valid else None,
        "relative_overhead": relative,
        "relative_overhead_percent": relative * 100.0 if relative is not None else None,
        "valid": valid,
        "invalid_pair_reason": errors if not valid else [],
    }
    _atomic_json(pair_dir / "pair_evidence.json", row)
    return row


def _nearest_rank(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(1, math.ceil(fraction * len(ordered))) - 1
    return ordered[index]


def run_replay(manifest: Mapping[str, Any], *, output_dir: Path, execute: bool, timeout_seconds: float) -> dict[str, Any]:
    _fail(not output_dir.exists() or not any(output_dir.iterdir()), f"refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    pairs: list[dict[str, Any]] = []
    case_summaries: list[dict[str, Any]] = []
    for case in manifest["cases"]:
        case_pairs = []
        for repeat in range(3):
            order = manifest["orders_by_repeat"][str(repeat)]
            case_pairs.append(_pair(case, repeat=repeat, order=order, output_root=output_dir, execute=execute, default_timeout=timeout_seconds))
        pairs.extend(case_pairs)
        medians = [float(pair["relative_overhead_percent"]) for pair in case_pairs if pair["valid"] and pair["relative_overhead_percent"] is not None]
        case_summaries.append(
            {
                "case_id": case["case_id"],
                "pair_count": len(case_pairs),
                "valid_pair_count": len(medians),
                "median_overhead_percent": sorted(medians)[len(medians) // 2] if len(medians) == 3 else None,
                "valid": len(medians) == 3,
            }
        )
    medians = [float(row["median_overhead_percent"]) for row in case_summaries if row["median_overhead_percent"] is not None]
    v2 = manifest.get("schema_version") == MANIFEST_SCHEMA_V2
    median_overhead = (statistics.median(medians) if v2 else sorted(medians)[len(medians) // 2]) if len(medians) == len(case_summaries) and medians else None
    p95_overhead = _nearest_rank(medians, 0.95) if len(medians) == len(case_summaries) else None
    result = {
        "schema_version": EVIDENCE_SCHEMA_V2 if v2 else EVIDENCE_SCHEMA,
        "status": "measured" if execute else "validation_only",
        "manifest_sha256": sha256_file(manifest["path"]),
        "manifest_path": str(manifest["path"]),
        "namespace": manifest["namespace"],
        "case_count": len(manifest["cases"]),
        "paired_repetitions": 3,
        "orders_by_repeat": manifest["orders_by_repeat"],
        "conditions": manifest["conditions"],
        "output_dir": str(output_dir),
        "pairs": pairs,
        "case_summaries": case_summaries,
        "valid_pair_count": sum(1 for pair in pairs if pair["valid"]),
        "median_overhead_percent": median_overhead,
        "p95_overhead_percent": p95_overhead,
        "thresholds": {"median_relative_overhead_max_percent": 5.0, "p95_relative_overhead_max_percent": 10.0, "p95_definition": f"nearest-rank across {len(case_summaries)} per-case medians"},
        "threshold_status": "pass" if median_overhead is not None and p95_overhead is not None and median_overhead <= 5.0 and p95_overhead <= 10.0 else "unavailable_or_fail",
        "threshold_authority": "C_engineering_diagnostic_only_not_PDF_readiness",
        "measurement_validity": "main_review_required_no_representativeness_claim_from_percentage_alone",
        "invalid_pair_policy": "record mismatch and exclude pair; never compare unequal work",
        "no_values_imputed": True,
    }
    if v2:
        result.update(fixture_count=len(manifest["cases"]), pair_count=len(pairs), condition_pass_count=len(pairs) * 2)
    _atomic_json(output_dir / "replay_evidence.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="hash-bound fixed-work fixture manifest")
    parser.add_argument("--output-dir", type=Path, required=True, help="new empty replay output directory")
    parser.add_argument("--timeout-seconds", type=float, default=5400.0)
    parser.add_argument("--execute", action="store_true", help="launch the explicit fixed-work adapter argv templates")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _fail(math.isfinite(args.timeout_seconds) and args.timeout_seconds > 0, "timeout-seconds must be positive")
        manifest = load_manifest(args.manifest)
        result = run_replay(manifest, output_dir=args.output_dir.expanduser().resolve(), execute=args.execute, timeout_seconds=args.timeout_seconds)
    except (OSError, ReplayError, json.JSONDecodeError) as exc:
        print(f"instrumentation replay: BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("status", "case_count", "paired_repetitions", "valid_pair_count", "median_overhead_percent", "p95_overhead_percent", "threshold_status")}, sort_keys=True))
    # Invalid comparisons remain errors. Exceeding a historical engineering
    # percentage does not independently block acquisition or assert validity.
    if args.execute and any(not pair["valid"] for pair in result["pairs"]):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
