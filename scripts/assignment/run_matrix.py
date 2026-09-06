#!/usr/bin/env python3
"""Execute a sealed assignment plan serially through a reviewed case runner.

This script is provider-neutral.  It does not know how to start SWE-agent,
vLLM, Docker, or a GPU workload; the explicitly supplied case runner owns that
work.  This layer verifies the plan, enforces concurrency=1 and hard deadlines,
persists resumable state, and refuses to call a runner that does not produce the
declared result contract.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "assignment-steps-1-3-plan.v1"
STATE_SCHEMA = "assignment-matrix-state.v2"
RESULT_SCHEMA = "assignment-case-result.v1"
SHARDS_SCHEMA = "assignment-plan-shards.v1"
SHARD_FIELDS = {
    "parent_plan_sha256",
    "parent_execution_case_count",
    "parent_original_case_count",
    "shard_index",
    "shard_count",
    "shard_assignment",
}
REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_IN_CONFIG = REPO_ROOT / "configs" / "assignment_steps_1_3.json"
REVIEWED_RUNNER = Path(__file__).resolve().with_name("sweagent_case_runner.py")
# This is deliberately a checked-in value.  A changed adapter must be reviewed
# before it can be used for paid execution.
REVIEWED_RUNNER_SHA256 = "0bdec6be9c6eb45b6be206893010c3a1cc08e6dd81bc94a95c617b7d3de2b7bf"


class ExecutionError(RuntimeError):
    """The sealed plan, state, or case-runner result is unsafe."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ExecutionError(f"{path} is not a JSON object")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ExecutionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _verify_sidecar(path: Path, sidecar: Path, *, label: str = "file") -> str:
    if path.is_symlink() or not path.is_file():
        raise ExecutionError(f"{label} is not a regular file: {path}")
    try:
        fields = sidecar.read_text(encoding="utf-8").strip().split()
    except OSError as exc:
        raise ExecutionError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    if sidecar.is_symlink() or len(fields) != 2 or fields[1] != path.name:
        raise ExecutionError(f"{label} SHA-256 sidecar has an invalid filename or format")
    actual = _sha256(path)
    if fields[0] != actual:
        raise ExecutionError(f"{label} SHA-256 does not match its sidecar")
    return actual


def _validate_runtime_manifest(path: Path, sidecar: Path | None) -> tuple[Path, str]:
    path = path.absolute()
    digest = _verify_sidecar(
        path,
        sidecar or Path(f"{path}.sha256"),
        label="runtime manifest",
    )
    manifest = _read_json(path)
    if manifest.get("schema_version") != "assignment-runtime-manifest.v1":
        raise ExecutionError("runtime manifest has an unsupported schema_version")
    return path, digest


def _load_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ExecutionError(f"cannot read plan {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            raise ExecutionError(f"blank plan line at {number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExecutionError(f"invalid plan JSON at line {number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ExecutionError(f"plan line {number} is not an object")
        rows.append(row)
    if len(rows) < 2 or rows[0].get("record_type") != "plan":
        raise ExecutionError("plan must contain a header followed by cases")
    header, cases = rows[0], rows[1:]
    if header.get("schema_version") != SCHEMA_VERSION or header.get("planning_only") is not True:
        raise ExecutionError("unsupported or unsealed assignment plan")
    if not isinstance(header.get("plan_id"), str) or not header["plan_id"]:
        raise ExecutionError("plan_id is required")
    if header.get("concurrency") != 1:
        raise ExecutionError("assignment execution requires concurrency=1")
    if header.get("execution_case_count") != len(cases):
        raise ExecutionError("plan execution_case_count mismatch")
    keys: set[str] = set()
    for number, case in enumerate(cases, 2):
        if case.get("record_type") != "case" or case.get("schema_version") != SCHEMA_VERSION:
            raise ExecutionError(f"invalid case record at plan line {number}")
        key = case.get("resume_key")
        if not isinstance(key, str) or not key or key in keys:
            raise ExecutionError(f"missing or duplicate resume_key at plan line {number}")
        keys.add(key)
        if case.get("concurrency") != 1:
            raise ExecutionError(f"case {key} does not enforce concurrency=1")
        deadline = case.get("per_case_deadline_seconds")
        if not isinstance(deadline, int) or isinstance(deadline, bool) or deadline <= 0:
            raise ExecutionError(f"case {key} has an invalid deadline")
        if case.get("plan_id") != header.get("plan_id"):
            raise ExecutionError(f"case {key} does not match the plan identity")
    return header, cases


def _validate_config_binding(
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    config_path: Path,
    expected_config_sha256: str | None,
) -> str:
    config_path = config_path.absolute()
    if config_path != CHECKED_IN_CONFIG and expected_config_sha256 is None:
        raise ExecutionError("a non-checked-in assignment config requires --config-sha256")
    try:
        actual_config_sha256 = _sha256(config_path)
    except OSError as exc:
        raise ExecutionError(f"cannot read assignment config {config_path}: {exc}") from exc
    if expected_config_sha256 is not None:
        expected_config_sha256 = _require_sha256(expected_config_sha256, "--config-sha256")
        if actual_config_sha256 != expected_config_sha256:
            raise ExecutionError("assignment config SHA-256 does not match --config-sha256")
    if header.get("config_sha256") != actual_config_sha256:
        raise ExecutionError("plan config_sha256 does not match the verified assignment config")
    config = _read_json(config_path)
    if config.get("schema_version") != SCHEMA_VERSION or config.get("planning_only") is not True:
        raise ExecutionError("assignment config is not a sealed planning configuration")
    if not isinstance(config.get("plan_id"), str) or not config["plan_id"] or config.get("plan_id") != header.get("plan_id"):
        raise ExecutionError("plan identity does not match the assignment config")
    limits = config.get("execution_limits")
    if not isinstance(limits, dict):
        raise ExecutionError("assignment config execution_limits are missing")
    for field in ("concurrency", "per_case_deadline_seconds", "global_deadline_seconds"):
        if header.get(field) != limits.get(field):
            raise ExecutionError(f"plan {field} does not match the assignment config")

    # The planner binds cardinality to the two source manifests and the
    # selected Step 2 cells.  Recompute that binding here; execution_case_count
    # alone is not an adequate proof because a caller can rewrite the sidecar.
    sources = header.get("sources")
    step_two = header.get("step_2")
    if not isinstance(sources, dict) or set(sources) != {"lite", "verified"}:
        raise ExecutionError("plan sources are required to bind assignment cardinality")
    if not isinstance(step_two, dict):
        raise ExecutionError("plan Step 2 identity is required to bind assignment cardinality")
    source_count = 0
    for suite in ("lite", "verified"):
        source = sources[suite]
        if not isinstance(source, dict):
            raise ExecutionError(f"plan source {suite} is malformed")
        count = source.get("task_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ExecutionError(f"plan source {suite} task_count is malformed")
        source_count += count
    selected = step_two.get("selected_task_ids")
    knobs = step_two.get("knobs")
    if not isinstance(selected, dict) or set(selected) != {"lite", "verified"}:
        raise ExecutionError("plan selected_task_ids are malformed")
    selected_count = 0
    for suite in ("lite", "verified"):
        values = selected[suite]
        if not isinstance(values, list) or len(values) != len(set(values)) or not all(isinstance(item, str) and item for item in values):
            raise ExecutionError(f"plan selected_task_ids.{suite} are malformed")
        selected_count += len(values)
    if not isinstance(knobs, list):
        raise ExecutionError("plan Step 2 knobs are malformed")
    nonbaseline_variations = 0
    for knob in knobs:
        if not isinstance(knob, dict) or not isinstance(knob.get("values"), list):
            raise ExecutionError("plan Step 2 knob values are malformed")
        nonbaseline_variations += max(0, len(knob["values"]) - 1)
    original_count = source_count + selected_count * nonbaseline_variations
    shard_fields = {
        "parent_plan_sha256",
        "parent_execution_case_count",
        "parent_original_case_count",
        "shard_index",
        "shard_count",
        "shard_assignment",
    }
    shard_claimed = set(header) & shard_fields
    if shard_claimed:
        if shard_claimed != shard_fields:
            raise ExecutionError("sharded plan metadata is incomplete")
        _require_sha256(header["parent_plan_sha256"], "parent_plan_sha256")
        if not isinstance(header["parent_execution_case_count"], int) or isinstance(header["parent_execution_case_count"], bool) or header["parent_execution_case_count"] < len(cases):
            raise ExecutionError("sharded plan parent execution count is malformed")
        if not isinstance(header["parent_original_case_count"], int) or isinstance(header["parent_original_case_count"], bool) or header["parent_original_case_count"] != original_count:
            raise ExecutionError("sharded plan parent original count does not match the assignment config")
        if not isinstance(header["shard_index"], int) or isinstance(header["shard_index"], bool) or not isinstance(header["shard_count"], int) or isinstance(header["shard_count"], bool) or header["shard_count"] <= 0 or not 0 <= header["shard_index"] < header["shard_count"]:
            raise ExecutionError("sharded plan index/count is malformed")
        if header["shard_assignment"] != "round_robin_plan_order_v1":
            raise ExecutionError("unsupported sharded plan assignment")
        if header.get("execution_case_count") != len(cases):
            raise ExecutionError("sharded plan execution case count is inconsistent")
        if "reconciliation" in header:
            raise ExecutionError("sharded plan must not carry a misleading reconciliation claim")
    else:
        reconciliation = header.get("reconciliation")
        if reconciliation is None:
            if header.get("execution_case_count") != original_count:
                raise ExecutionError("plan cardinality does not match the assignment config binding")
        else:
            if not isinstance(reconciliation, dict):
                raise ExecutionError("plan reconciliation binding is malformed")
            if reconciliation.get("original_case_count") != original_count:
                raise ExecutionError("reconciled plan original cardinality does not match the assignment config")
            if reconciliation.get("remaining_case_count") != len(cases):
                raise ExecutionError("reconciled plan remaining cardinality mismatch")
            if reconciliation.get("matched_case_count", -1) + len(cases) != original_count:
                raise ExecutionError("reconciled plan cardinality accounting is inconsistent")
    config_step_two = config.get("step_2")
    if isinstance(config_step_two, dict):
        if config_step_two.get("task_selection") is not None and step_two.get("task_selection") != config_step_two.get("task_selection"):
            raise ExecutionError("plan Step 2 task selection does not match the assignment config")
        if config_step_two.get("knobs") is not None and knobs != config_step_two.get("knobs"):
            raise ExecutionError("plan Step 2 knobs do not match the assignment config")
    return actual_config_sha256


def _runtime_identity(
    plan_sha256: str,
    header: Mapping[str, Any],
    runner: Path,
    runner_sha256: str,
    runtime_manifest: Path,
    runtime_manifest_sha256: str,
    cpu_docker: bool = False,
) -> dict[str, Any]:
    identity = {
        "plan_sha256": plan_sha256,
        "plan_id": header["plan_id"],
        "config_sha256": header["config_sha256"],
        "runner_path": str(runner),
        "runner_sha256": runner_sha256,
        "runtime_manifest_path": str(runtime_manifest),
        "runtime_manifest_sha256": runtime_manifest_sha256,
    }
    if cpu_docker:
        identity["execution_mode"] = "cpu-docker-runner+h100-inference"
    for field in sorted(SHARD_FIELDS):
        if field in header:
            identity[field] = header[field]
    return identity


def _coverage_sha256(keys: list[str]) -> str:
    return hashlib.sha256((_canonical(sorted(keys)) + "\n").encode("utf-8")).hexdigest()


def _validate_shards_manifest(
    manifest_path: Path | None,
    current_plan_path: Path,
    current_header: Mapping[str, Any],
    current_cases: list[dict[str, Any]],
    current_plan_sha256: str,
) -> dict[str, Any] | None:
    """Prove that one shard is part of a complete, disjoint parent plan."""
    claimed = set(current_header) & SHARD_FIELDS
    if not claimed:
        if manifest_path is not None:
            raise ExecutionError("a shards manifest is only valid for a sharded plan")
        return None
    if claimed != SHARD_FIELDS:
        raise ExecutionError("sharded plan metadata is incomplete")
    if manifest_path is None:
        raise ExecutionError("sharded plan requires --shards-manifest")

    manifest_path = manifest_path.absolute()
    manifest_sha256 = _verify_sidecar(
        manifest_path,
        Path(f"{manifest_path}.sha256"),
        label="shards manifest",
    )
    manifest = _read_json(manifest_path)
    required = {
        "schema_version",
        "assignment",
        "parent_plan_path",
        "parent_plan_sha256",
        "parent_execution_case_count",
        "parent_original_case_count",
        "plan_id",
        "shard_count",
        "shards",
        "coverage_sha256",
    }
    if set(manifest) != required or manifest.get("schema_version") != SHARDS_SCHEMA:
        raise ExecutionError("shards manifest has missing, unknown, or unsupported fields")
    if manifest.get("assignment") != current_header["shard_assignment"]:
        raise ExecutionError("shards manifest assignment does not match the sharded plan")
    parent_sha256 = _require_sha256(manifest.get("parent_plan_sha256"), "shards parent_plan_sha256")
    if parent_sha256 != current_header["parent_plan_sha256"]:
        raise ExecutionError("sharded plan parent SHA-256 does not match the shards manifest")
    if manifest.get("plan_id") != current_header["plan_id"]:
        raise ExecutionError("shards manifest plan identity does not match the sharded plan")
    shard_count = manifest.get("shard_count")
    if not isinstance(shard_count, int) or isinstance(shard_count, bool) or shard_count <= 0:
        raise ExecutionError("shards manifest shard_count is invalid")
    if shard_count != current_header["shard_count"]:
        raise ExecutionError("shards manifest shard_count does not match the sharded plan")
    parent_plan_claim = manifest.get("parent_plan_path")
    if not isinstance(parent_plan_claim, str) or not parent_plan_claim or Path(parent_plan_claim).is_absolute():
        raise ExecutionError("shards manifest parent_plan_path must be relative")
    parent_plan = (manifest_path.parent / parent_plan_claim).resolve()
    if parent_plan.is_symlink() or not parent_plan.is_file():
        raise ExecutionError("shards manifest parent plan is unavailable")
    parent_sha256_actual = _verify_sidecar(
        parent_plan,
        Path(f"{parent_plan}.sha256"),
        label="shards parent plan",
    )
    if parent_sha256_actual != parent_sha256:
        raise ExecutionError("shards parent plan hash does not match the manifest")
    parent_header, parent_cases = _load_plan(parent_plan)
    if set(parent_header) & SHARD_FIELDS:
        raise ExecutionError("shards manifest parent must be an unsharded plan")
    if parent_header.get("plan_id") != manifest["plan_id"]:
        raise ExecutionError("shards parent plan identity does not match the manifest")
    if manifest["parent_execution_case_count"] != len(parent_cases):
        raise ExecutionError("shards parent execution count is incorrect")
    parent_reconciliation = parent_header.get("reconciliation")
    expected_original_count = len(parent_cases)
    if isinstance(parent_reconciliation, dict):
        expected_original_count = parent_reconciliation.get("original_case_count")
    if manifest["parent_original_case_count"] != expected_original_count:
        raise ExecutionError("shards parent original count is incorrect")

    entries = manifest.get("shards")
    if not isinstance(entries, list) or len(entries) != shard_count:
        raise ExecutionError("shards manifest does not enumerate every shard")
    parent_by_key = {case["resume_key"]: case for case in parent_cases}
    all_cases: dict[str, dict[str, Any]] = {}
    seen_indices: set[int] = set()
    current_entry: Mapping[str, Any] | None = None
    current_plan_path = current_plan_path.resolve()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
            "index",
            "path",
            "sha256",
            "case_count",
            "resume_keys_sha256",
        }:
            raise ExecutionError("shards manifest contains a malformed shard entry")
        index = entry["index"]
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < shard_count:
            raise ExecutionError("shards manifest shard index is invalid")
        if index in seen_indices:
            raise ExecutionError("shards manifest contains duplicate shard indices")
        seen_indices.add(index)
        path_claim = entry["path"]
        if not isinstance(path_claim, str) or not path_claim or Path(path_claim).is_absolute():
            raise ExecutionError("shards manifest shard path must be relative")
        shard_path = (manifest_path.parent / path_claim).resolve()
        shard_sha256 = _require_sha256(entry["sha256"], "shard sha256")
        actual_shard_sha256 = _verify_sidecar(
            shard_path,
            Path(f"{shard_path}.sha256"),
            label=f"shard {index}",
        )
        if actual_shard_sha256 != shard_sha256:
            raise ExecutionError(f"shard {index} hash does not match the shards manifest")
        shard_header, shard_cases = _load_plan(shard_path)
        for field in SHARD_FIELDS - {"shard_index"}:
            if shard_header.get(field) != current_header.get(field):
                raise ExecutionError(f"shard {index} metadata does not match the active shard")
        if shard_header.get("shard_index") != index:
            raise ExecutionError(f"shard {index} header index is incorrect")
        if shard_header.get("execution_case_count") != len(shard_cases):
            raise ExecutionError(f"shard {index} case count is incorrect")
        if entry["case_count"] != len(shard_cases):
            raise ExecutionError(f"shard {index} manifest case count is incorrect")
        keys = [case.get("resume_key") for case in shard_cases]
        if not all(isinstance(key, str) and key for key in keys):
            raise ExecutionError(f"shard {index} contains an invalid resume key")
        if entry["resume_keys_sha256"] != _coverage_sha256(keys):
            raise ExecutionError(f"shard {index} resume-key digest is incorrect")
        for case in shard_cases:
            key = case["resume_key"]
            if key in all_cases:
                raise ExecutionError("shards overlap on a resume key")
            parent_case = parent_by_key.get(key)
            if parent_case is None or _canonical(parent_case) != _canonical(case):
                raise ExecutionError("shard case does not exactly match the parent plan")
            all_cases[key] = case
        if shard_path == current_plan_path:
            current_entry = entry

    parent_keys = set(parent_by_key)
    if set(all_cases) != parent_keys:
        raise ExecutionError("shards do not provide exact parent-plan coverage")
    if seen_indices != set(range(shard_count)):
        raise ExecutionError("shards manifest is missing a shard index")
    if manifest["coverage_sha256"] != _coverage_sha256(list(all_cases)):
        raise ExecutionError("shards manifest coverage digest is incorrect")
    if manifest["parent_execution_case_count"] != len(all_cases):
        raise ExecutionError("shards manifest coverage count is incorrect")
    if current_entry is None or current_plan_sha256 != current_entry["sha256"]:
        raise ExecutionError("active plan is not the shard bound by the shards manifest")
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "parent_plan_path": str(parent_plan),
        "parent_plan_sha256": parent_sha256,
        "shard_index": current_header["shard_index"],
        "shard_count": shard_count,
        "parent_execution_case_count": len(parent_cases),
    }


def _deadline_binding(state: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({
        "plan_sha256": state["plan_sha256"],
        "started_epoch": state["started_epoch"],
        "max_wall_seconds": state["max_wall_seconds"],
        "deadline_epoch": state["deadline_epoch"],
    }).encode("utf-8")).hexdigest()


def _new_state(
    plan_sha256: str,
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    max_wall: int,
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    now = int(time.time())
    declared = header.get("global_deadline_seconds")
    if not isinstance(declared, int) or isinstance(declared, bool) or declared <= 0:
        raise ExecutionError("plan has an invalid global deadline")
    if max_wall <= 0 or max_wall > declared:
        raise ExecutionError("--max-wall-seconds must be positive and no larger than the sealed deadline")
    state = {
        "schema_version": STATE_SCHEMA,
        "plan_sha256": plan_sha256,
        **dict(runtime_identity),
        "status": "running",
        "started_epoch": now,
        "max_wall_seconds": max_wall,
        "global_deadline_seconds": declared,
        "deadline_epoch": now + max_wall,
        "case_count": len(cases),
        "completed_resume_keys": [],
        "completed_case_results": {},
        "failed_cases": {},
        "active_resume_key": None,
    }
    state["deadline_binding"] = _deadline_binding(state)
    return state


def _load_state(
    path: Path,
    plan_sha256: str,
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    output: Path,
    runtime_identity: Mapping[str, Any],
    requested_max_wall: int | None,
) -> dict[str, Any]:
    state = _read_json(path)
    if state.get("schema_version") != STATE_SCHEMA or state.get("plan_sha256") != plan_sha256:
        raise ExecutionError("resume state does not match the sealed plan")
    for field, expected in runtime_identity.items():
        if state.get(field) != expected:
            raise ExecutionError(f"resume state {field} does not match the sealed runtime identity")
    if state.get("case_count") != len(cases):
        raise ExecutionError("resume state case count changed")
    valid = {case["resume_key"] for case in cases}
    completed = state.get("completed_resume_keys")
    failed = state.get("failed_cases")
    if not isinstance(completed, list) or not all(isinstance(key, str) for key in completed):
        raise ExecutionError("resume state completed keys are malformed")
    if len(completed) != len(set(completed)) or not set(completed).issubset(valid):
        raise ExecutionError("resume state contains duplicate or unknown completed keys")
    completed_results = state.get("completed_case_results")
    if not isinstance(completed_results, dict) or set(completed_results) != set(completed):
        raise ExecutionError("resume state does not bind every completed key to a case result")
    if not isinstance(failed, dict) or not set(failed).issubset(valid):
        raise ExecutionError("resume state contains unknown failed keys")
    if set(completed).intersection(failed):
        raise ExecutionError("resume state marks a case both completed and failed")
    started = state.get("started_epoch")
    max_wall = state.get("max_wall_seconds")
    global_deadline = state.get("global_deadline_seconds")
    deadline = state.get("deadline_epoch")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (started, max_wall, global_deadline, deadline)):
        raise ExecutionError("resume deadline binding is malformed")
    if max_wall <= 0 or max_wall > global_deadline or global_deadline != header.get("global_deadline_seconds"):
        raise ExecutionError("resume deadline binding is outside the sealed plan deadline")
    if deadline != started + max_wall or state.get("deadline_binding") != _deadline_binding(state):
        raise ExecutionError("resume deadline binding was changed")
    if requested_max_wall is not None and requested_max_wall != max_wall:
        raise ExecutionError("--max-wall-seconds cannot change an existing resume deadline")
    if started > int(time.time()):
        raise ExecutionError("resume start time is in the future")
    index_by_key = {case["resume_key"]: index for index, case in enumerate(cases)}
    for key in completed:
        index = index_by_key[key]
        case_root = output / "cases" / f"{index:05d}"
        spec_path = case_root / "case_spec.json"
        result_path = case_root / "case_result.json"
        expected_spec = (_canonical(cases[index]) + "\n").encode("utf-8")
        if spec_path.is_symlink() or not spec_path.is_file() or spec_path.read_bytes() != expected_spec:
            raise ExecutionError(f"completed case spec is not immutable: {spec_path}")
        expected_identity = {**runtime_identity, "case_sha256": hashlib.sha256(expected_spec).hexdigest()}
        _validate_result(result_path, cases[index], expected_identity)
        binding = completed_results[key]
        if not isinstance(binding, dict) or binding.get("case_index") != index:
            raise ExecutionError(f"completed case binding is malformed: {key}")
        if binding.get("case_sha256") != expected_identity["case_sha256"] or binding.get("result_sha256") != _sha256(result_path):
            raise ExecutionError(f"completed case result is not the immutable bound result: {key}")
        for field, expected in expected_identity.items():
            if binding.get(field) != expected:
                raise ExecutionError(f"completed case runtime identity mismatch: {key}")
    if state.get("status") == "completed":
        raise ExecutionError("matrix is already completed")
    state["active_resume_key"] = None
    state["status"] = "running"
    return state


def _run_case(command: list[str], timeout_seconds: int, stdout_path: Path, stderr_path: Path) -> tuple[int, bool]:
    with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            return process.wait(timeout=timeout_seconds), False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=15)
            return 124, True


def _validate_result(path: Path, case: Mapping[str, Any], identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise ExecutionError(f"case result must not be a symlink: {path}")
    result = _read_json(path)
    if result.get("schema_version") != RESULT_SCHEMA:
        raise ExecutionError(f"case runner wrote unsupported result schema: {path}")
    if result.get("resume_key") != case["resume_key"]:
        raise ExecutionError(f"case result identity mismatch: {path}")
    if result.get("status") not in {"completed", "failed", "timeout", "unavailable"}:
        raise ExecutionError(f"case result has invalid status: {path}")
    if identity is not None:
        for field, expected in identity.items():
            if result.get(field) != expected:
                raise ExecutionError(f"case result {field} does not match the sealed runtime identity: {path}")
    return result


def _seal_result(path: Path, case: Mapping[str, Any], identity: Mapping[str, Any]) -> dict[str, Any]:
    result = _validate_result(path, case)
    for field, expected in identity.items():
        if field in result and result[field] != expected:
            raise ExecutionError(f"case result {field} does not match the sealed runtime identity: {path}")
        result[field] = expected
    _atomic_json(path, result)
    return _validate_result(path, case, identity)


def execute(args: argparse.Namespace) -> int:
    plan_sha256 = _verify_sidecar(
        args.plan,
        args.sha256_sidecar or Path(f"{args.plan}.sha256"),
        label="plan",
    )
    header, cases = _load_plan(args.plan)
    runtime_manifest, runtime_manifest_sha256 = _validate_runtime_manifest(
        args.runtime_manifest,
        args.runtime_manifest_sha256_sidecar,
    )
    runner = args.runner.absolute()
    if runner != REVIEWED_RUNNER:
        raise ExecutionError(f"live execution requires the reviewed case runner at the exact path: {REVIEWED_RUNNER}")
    if not runner.is_file() or runner.is_symlink() or not os.access(runner, os.X_OK):
        raise ExecutionError(f"reviewed case runner is not an executable file: {runner}")
    runner_sha256 = _sha256(runner)
    if runner_sha256 != REVIEWED_RUNNER_SHA256:
        raise ExecutionError("reviewed case runner SHA-256 is not the approved value")
    _validate_config_binding(
        header,
        cases,
        args.config or CHECKED_IN_CONFIG,
        args.config_sha256,
    )
    shard_metadata = _validate_shards_manifest(
        args.shards_manifest,
        args.plan,
        header,
        cases,
        plan_sha256,
    )
    identity = _runtime_identity(
        plan_sha256,
        header,
        runner,
        runner_sha256,
        runtime_manifest,
        runtime_manifest_sha256,
        getattr(args, "cpu_docker", False),
    )
    if shard_metadata is not None:
        identity["shards_manifest_path"] = shard_metadata["manifest_path"]
        identity["shards_manifest_sha256"] = shard_metadata["manifest_sha256"]
    if not args.execute:
        if args.resume:
            raise ExecutionError("--resume requires --execute")
        print(_canonical({
            "schema_version": "assignment-matrix-validation.v1",
            "status": "passed",
            "execution_started": False,
            "plan_sha256": plan_sha256,
            "case_count": len(cases),
            "concurrency": header["concurrency"],
            "case_runner": str(runner),
            "case_runner_sha256": runner_sha256,
            "config_sha256": header["config_sha256"],
            "runtime_manifest": str(runtime_manifest),
            "runtime_manifest_sha256": runtime_manifest_sha256,
            **(
                {
                    "shards_manifest": shard_metadata["manifest_path"],
                    "shards_manifest_sha256": shard_metadata["manifest_sha256"],
                }
                if shard_metadata is not None
                else {}
            ),
        }))
        return 0
    if not args.acknowledge_paid_gpu_work:
        raise ExecutionError(
            "--execute requires --acknowledge-paid-gpu-work; no case runner was started"
        )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    state_path = output / "run_state.json"
    lock_path = output / ".assignment.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ExecutionError("another assignment matrix process holds the output lock") from exc
        if state_path.exists():
            if not args.resume:
                raise ExecutionError("run_state.json exists; inspect it and pass --resume")
            state = _load_state(
                state_path,
                plan_sha256,
                header,
                cases,
                output,
                identity,
                args.max_wall_seconds,
            )
        else:
            if args.resume:
                raise ExecutionError("--resume requires an existing run_state.json")
            max_wall = args.max_wall_seconds if args.max_wall_seconds is not None else header["global_deadline_seconds"]
            state = _new_state(plan_sha256, header, cases, max_wall, identity)
            _atomic_json(state_path, state)

        completed = set(state["completed_resume_keys"])
        attempted = 0
        for index, case in enumerate(cases):
            key = case["resume_key"]
            if key in completed:
                continue
            if args.max_cases is not None and attempted >= args.max_cases:
                state["status"] = "paused"
                _atomic_json(state_path, state)
                return 3
            remaining = state["deadline_epoch"] - int(time.time())
            if remaining <= 0:
                state["status"] = "deadline_reached"
                _atomic_json(state_path, state)
                return 4
            case_root = output / "cases" / f"{index:05d}"
            if case_root.is_symlink():
                raise ExecutionError(f"case output directory must not be a symlink: {case_root}")
            case_root.mkdir(parents=True, exist_ok=True)
            spec_path = case_root / "case_spec.json"
            expected_spec = (_canonical(case) + "\n").encode("utf-8")
            if spec_path.is_symlink() or spec_path.exists() and spec_path.read_bytes() != expected_spec:
                raise ExecutionError(f"immutable case spec changed: {spec_path}")
            if not spec_path.exists():
                temporary = spec_path.with_name(spec_path.name + ".tmp")
                temporary.write_bytes(expected_spec)
                temporary.replace(spec_path)
            result_path = case_root / "case_result.json"
            if result_path.exists():
                case_identity = {
                    **identity,
                    "case_sha256": hashlib.sha256(expected_spec).hexdigest(),
                }
                prior = _validate_result(result_path, case)
                if prior["status"] == "completed":
                    _seal_result(result_path, case, case_identity)
                    state["completed_resume_keys"].append(key)
                    state["completed_case_results"][key] = {
                        "case_index": index,
                        **case_identity,
                        "result_sha256": _sha256(result_path),
                    }
                    completed.add(key)
                    state["failed_cases"].pop(key, None)
                    _atomic_json(state_path, state)
                    continue
                result_path.replace(case_root / "case_result.previous.json")
            state["active_resume_key"] = key
            _atomic_json(state_path, state)
            timeout_seconds = min(case["per_case_deadline_seconds"], remaining)
            command = [
                str(runner),
                "--runtime-manifest", str(runtime_manifest),
                "--case-spec", str(spec_path),
                "--output-dir", str(case_root),
                "--execute",
            ]
            if getattr(args, "cpu_docker", False):
                command.append("--cpu-docker")
            returncode, timed_out = _run_case(
                command, timeout_seconds, case_root / "runner.stdout.log", case_root / "runner.stderr.log"
            )
            attempted += 1
            state["active_resume_key"] = None
            try:
                result = _validate_result(result_path, case)
            except ExecutionError as exc:
                state["failed_cases"][key] = {
                    "returncode": returncode,
                    "reason": "timeout_without_valid_result" if timed_out else f"invalid_result:{exc}",
                }
                _atomic_json(state_path, state)
                continue
            if returncode == 0 and result["status"] == "completed":
                case_identity = {
                    **identity,
                    "case_sha256": hashlib.sha256(expected_spec).hexdigest(),
                }
                _seal_result(result_path, case, case_identity)
                state["completed_resume_keys"].append(key)
                state["completed_case_results"][key] = {
                    "case_index": index,
                    **case_identity,
                    "result_sha256": _sha256(result_path),
                }
                completed.add(key)
                state["failed_cases"].pop(key, None)
            else:
                state["failed_cases"][key] = {
                    "returncode": returncode,
                    "reason": "deadline" if timed_out else result["status"],
                }
            _atomic_json(state_path, state)

        state["status"] = "completed" if len(completed) == len(cases) else "failed"
        _atomic_json(state_path, state)
        return 0 if state["status"] == "completed" else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--plan", required=True, type=Path)
    result.add_argument("--sha256-sidecar", type=Path)
    result.add_argument("--runner", required=True, type=Path)
    result.add_argument(
        "--runtime-manifest",
        required=True,
        type=Path,
        help="external assignment runtime manifest, verified against its SHA-256 sidecar",
    )
    result.add_argument(
        "--runtime-manifest-sha256-sidecar",
        type=Path,
        help="runtime-manifest sidecar (default: RUNTIME_MANIFEST.sha256)",
    )
    result.add_argument(
        "--shards-manifest",
        type=Path,
        help="required for a sharded plan; proves parent-plan coverage and disjoint shard identities",
    )
    result.add_argument(
        "--config",
        type=Path,
        help=f"assignment config (default: {CHECKED_IN_CONFIG})",
    )
    result.add_argument(
        "--config-sha256",
        help="exact SHA-256 for a supplied assignment config",
    )
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument(
        "--execute",
        action="store_true",
        help="execute the sealed live matrix; without this flag validation only is performed",
    )
    result.add_argument(
        "--acknowledge-paid-gpu-work",
        action="store_true",
        help="required with --execute to acknowledge that the case runner may start paid GPU work",
    )
    result.add_argument("--resume", action="store_true")
    result.add_argument("--max-wall-seconds", type=int)
    result.add_argument("--max-cases", type=int, help="testing/maintenance pause after N attempted cases")
    result.add_argument(
        "--cpu-docker",
        action="store_true",
        help="run the reviewed case runner on a CPU VM using Docker while inference stays on the configured H100 endpoint",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return execute(parser().parse_args(argv))
    except ExecutionError as exc:
        print(f"NOT_READY: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
