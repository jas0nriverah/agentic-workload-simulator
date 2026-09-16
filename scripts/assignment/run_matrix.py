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
import math
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
import stat
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.validation.check_instrumentation_pilot import ACQUISITION_REVIEW_FIELDS  # noqa: E402

from agentic_sim.runners.case_lifecycle import (  # noqa: E402
    CASE_DEADLINE_ENV,
    CASE_OWNER_ENV,
    deadline_environment,
    deadline_from_env,
    deadline_with_timeout,
    remaining_seconds,
    run_owned_process,
)
from agentic_sim.runners.owned_docker import cleanup_owned_containers  # noqa: E402


SCHEMA_VERSION = "assignment-steps-1-3-plan.v1"
PRODUCTION_SCHEMA_VERSION = "assignment-production-v2-plan.v1"
PRODUCTION_NAMESPACE = "assignment-production-v2"
PRODUCTION_PLAN_ID = "assignment-production-v2-20260908"
PRODUCTION_CASE_COUNT = 1088
PRODUCTION_STEP1_CASE_COUNT = 800
PRODUCTION_STEP2_CASE_COUNT = 288
PRODUCTION_SHARED_BASELINE_COORDINATE_COUNT = 96
PRODUCTION_SETTINGS = frozenset({
    "call_limit",
    "max_output_tokens",
    "observation_length",
    "temperature",
    "max_input_tokens",
    "top_p",
    "seed",
})
PRODUCTION_PLAN_FIELDS = frozenset({
    "record_type",
    "schema_version",
    "plan_id",
    "namespace",
    "candidate_id",
    "planning_only",
    "config_sha256",
    "sources",
    "step_2",
    "status",
    "final_configuration_status",
    "candidate_final_configuration",
    "serving_configuration",
    "pins",
    "telemetry",
    "source_template",
    "matrix",
    "fresh_identity",
    "holdout",
    "execution_case_count",
    "concurrency",
    "per_case_deadline_seconds",
    "global_deadline_seconds",
    "validator_integration",
})
PRODUCTION_EXECUTION_BINDING_FIELD = "execution_binding"
PRODUCTION_EXECUTION_BINDING_SCHEMA = "assignment-production-v2-execution-binding.v1"
PRODUCTION_EXECUTION_BINDING_FIELDS = frozenset({
    "schema_version",
    "status",
    "test_only",
    "candidate_id",
    "selected_settings",
    "selection_record",
    "candidate_inventory",
    "candidate_config",
    "runtime_manifest",
    "remote_hardware_profile",
    "acquisition_contract",
    "acquisition_proof",
    "pilot_evidence",
    "source_bundle",
})
PRODUCTION_OPTIONAL_EXECUTION_BINDING_FIELDS = frozenset({"historical_regression_proof"})
PRODUCTION_BOUND_ARTIFACT_FIELDS = frozenset({"path", "sha256"})
PRODUCTION_CASE_FIELDS = frozenset({
    "record_type",
    "schema_version",
    "plan_id",
    "namespace",
    "candidate_id",
    "case_id",
    "resume_key",
    "historical_template_case_id",
    "historical_template_plan_id",
    "fresh_case_id",
    "production_case",
    "confirmation_case",
    "holdout_instance_id",
    "selection_outcome_blind",
    "suite",
    "repository",
    "instance_id",
    "task_sha256",
    "source_manifest_sha256",
    "cell_id",
    "steps",
    "roles",
    "settings",
    "final_configuration",
    "serving_configuration",
    "variation",
    "concurrency",
    "per_case_deadline_seconds",
})
STATE_SCHEMA = "assignment-matrix-state.v2"
RESULT_SCHEMA = "assignment-case-result.v1"
FAILURE_RESULT_SCHEMA = "assignment-case-failure.v2"
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
REVIEWED_RUNNER_SHA256 = "5093f99d3231c6fde3fb7c4594894e5506cc2bf529d0312e9a373def34a0e5e1"


def _without_v2_activation(env: Mapping[str, str]) -> dict[str, str]:
    """Keep the matrix/case-runner supervisors outside sitecustomize opt-in."""

    return {
        str(key): str(value)
        for key, value in env.items()
        if not str(key).startswith("ASSIGNMENT_TELEMETRY_V2_")
    }


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


def _production_setting_values(settings: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(settings, dict) or frozenset(settings) != PRODUCTION_SETTINGS:
        raise ExecutionError(f"{label} must contain the complete seven-key production configuration")
    for key in ("call_limit", "max_output_tokens", "observation_length", "max_input_tokens"):
        value = settings[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ExecutionError(f"{label}.{key} must be a positive integer")
    temperature = settings["temperature"]
    if (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not math.isfinite(float(temperature))
        or not 0 <= float(temperature) <= 2
    ):
        raise ExecutionError(f"{label}.temperature is invalid")
    top_p = settings["top_p"]
    if (
        not isinstance(top_p, (int, float))
        or isinstance(top_p, bool)
        or not math.isfinite(float(top_p))
        or not 0 <= float(top_p) <= 1
    ):
        raise ExecutionError(f"{label}.top_p is invalid")
    seed = settings["seed"]
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        raise ExecutionError(f"{label}.seed must be a non-negative integer")
    return dict(settings)


def _validate_production_case(
    case: Mapping[str, Any],
    *,
    header: Mapping[str, Any],
    line_number: int,
) -> None:
    candidate = case.get("case_id") or f"line {line_number}"
    if set(case) != PRODUCTION_CASE_FIELDS:
        raise ExecutionError(f"production case {candidate} has missing or unknown fields")
    if case.get("record_type") != "case" or case.get("schema_version") != PRODUCTION_SCHEMA_VERSION:
        raise ExecutionError(f"invalid production case record at plan line {line_number}")
    for field in ("plan_id", "namespace", "candidate_id", "suite", "repository", "instance_id", "cell_id", "resume_key"):
        if not isinstance(case.get(field), str) or not case[field].strip():
            raise ExecutionError(f"production case {candidate} has an invalid {field}")
    if case["plan_id"] != header["plan_id"] or case["namespace"] != header["namespace"] or case["candidate_id"] != header["candidate_id"]:
        raise ExecutionError(f"production case {candidate} does not match the plan identity")
    if case["suite"] not in {"lite", "verified"}:
        raise ExecutionError(f"production case {candidate} has an invalid suite")
    case_id = case["case_id"]
    if (
        not isinstance(case_id, str)
        or not case_id.startswith(f"{PRODUCTION_NAMESPACE}:")
        or len(case_id) != len(f"{PRODUCTION_NAMESPACE}:") + 64
        or any(character not in "0123456789abcdef" for character in case_id.split(":", 1)[1])
        or case["resume_key"] != case_id
    ):
        raise ExecutionError(f"production case {candidate} does not have a fresh stable case identity")
    historical_id = case["historical_template_case_id"]
    if (
        not isinstance(historical_id, str)
        or not historical_id.startswith("assignment-case-v1:")
        or len(historical_id) != len("assignment-case-v1:") + 64
        or any(character not in "0123456789abcdef" for character in historical_id.split(":", 1)[1])
    ):
        raise ExecutionError(f"production case {candidate} has an invalid historical lineage identity")
    if case["historical_template_plan_id"] != "assignment-steps-1-3":
        raise ExecutionError(f"production case {candidate} has an invalid historical plan lineage")
    if (
        case["fresh_case_id"] is not True
        or case["production_case"] is not True
        or case["confirmation_case"] is not False
        or case["selection_outcome_blind"] is not True
        or case["holdout_instance_id"] != "sympy__sympy-12481"
    ):
        raise ExecutionError(f"production case {candidate} has invalid selection or holdout metadata")
    for field in ("task_sha256", "source_manifest_sha256"):
        value = case[field]
        if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ExecutionError(f"production case {candidate} has an invalid {field}")
    if case["concurrency"] != 1 or case["per_case_deadline_seconds"] != header["per_case_deadline_seconds"]:
        raise ExecutionError(f"production case {candidate} does not enforce the plan execution limits")
    settings = _production_setting_values(case["settings"], label=f"production case {candidate}.settings")
    final_configuration = _production_setting_values(
        case["final_configuration"],
        label=f"production case {candidate}.final_configuration",
    )
    if final_configuration != settings:
        raise ExecutionError(f"production case {candidate} final_configuration differs from settings")
    if case["serving_configuration"] != {"max_model_len": 65536, "vllm_version": "0.10.0"}:
        raise ExecutionError(f"production case {candidate} has an invalid serving configuration")

    variation = case["variation"]
    cell_id = case["cell_id"]
    if cell_id == "shared-baseline":
        if variation is not None:
            raise ExecutionError(f"production baseline case {candidate} has a variation")
        if case["steps"] == [1] and case["roles"] == ["step_1_baseline"]:
            pass
        elif case["steps"] == [1, 2] and case["roles"] == ["step_1_baseline", "step_2_shared_baseline"]:
            pass
        else:
            raise ExecutionError(f"production baseline case {candidate} has invalid lifecycle roles")
        if settings != dict(header["candidate_final_configuration"]):
            raise ExecutionError(f"production baseline case {candidate} does not use candidate settings")
    else:
        if not isinstance(variation, dict) or set(variation) != {"knob", "value", "historical_cell_id", "historical_value"}:
            raise ExecutionError(f"production sweep case {candidate} has invalid variation metadata")
        knob = variation["knob"]
        grids = {
            "call_limit": (20, 30, 50, 100),
            "max_output_tokens": (512, 1024, 2048, 4096),
            "observation_length": (10000, 25000, 50000, 100000),
            "temperature": (0.0, 0.2, 0.5, 0.8),
        }
        if knob not in grids or variation["value"] not in grids[knob] or variation["value"] == header["candidate_final_configuration"][knob]:
            raise ExecutionError(f"production sweep case {candidate} has an invalid target value")
        if cell_id != f"{knob}={json.dumps(variation['value'], sort_keys=True, separators=(',', ':'))}":
            raise ExecutionError(f"production sweep case {candidate} cell_id is not canonical")
        if case["steps"] != [2] or case["roles"] != ["step_2_sweep"]:
            raise ExecutionError(f"production sweep case {candidate} has invalid lifecycle roles")
        historical_grid = {
            "call_limit": {10, 20, 50},
            "max_output_tokens": {512, 1024, 4096},
            "observation_length": {10000, 25000, 50000},
            "temperature": {0.2, 0.5, 0.8},
        }
        if variation["historical_value"] not in historical_grid[knob] or not isinstance(variation["historical_cell_id"], str) or not variation["historical_cell_id"].strip():
            raise ExecutionError(f"production sweep case {candidate} has invalid historical variation metadata")
        expected_settings = dict(header["candidate_final_configuration"])
        expected_settings[knob] = variation["value"]
        if settings != expected_settings:
            raise ExecutionError(f"production sweep case {candidate} settings do not match its variation")


def _read_bound_artifact(value: Any, *, label: str, parse_json: bool = True) -> tuple[Path, str, Any]:
    if not isinstance(value, dict) or set(value) != PRODUCTION_BOUND_ARTIFACT_FIELDS:
        raise ExecutionError(f"{label} binding is malformed")
    path_value = value.get("path")
    digest = value.get("sha256")
    if not isinstance(path_value, str) or not path_value or not Path(path_value).is_absolute():
        raise ExecutionError(f"{label} path must be absolute")
    if not isinstance(digest, str) or len(digest) != 64 or set(digest) == {"0"} or any(character not in "0123456789abcdef" for character in digest):
        raise ExecutionError(f"{label} SHA-256 is malformed")
    path = Path(path_value)
    if path.is_symlink() or not path.is_file():
        raise ExecutionError(f"{label} is unavailable: {path}")
    actual = _sha256(path)
    if actual != digest:
        raise ExecutionError(f"{label} SHA-256 does not match the bound artifact")
    if not parse_json:
        return path, digest, None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutionError(f"{label} is not a valid UTF-8 JSON artifact: {exc}") from exc
    return path, digest, parsed


def _require_success_proof(value: Any, *, label: str, list_key: str) -> dict[str, Any]:
    # The acquisition/regression gate owns launch review.  Its concrete v2
    # contract reports `status: pass` and deliberately leaves launch
    # authorization false/absent; the scheduler must consume that proof rather
    # than inventing an independent authorization requirement.  Older sealed
    # proof records used the more verbose statuses, so retain those for
    # compatibility with already-reviewed artifacts.
    success_statuses = {"pass", "passed", "verified", "complete"}
    if not isinstance(value, dict) or value.get("status") not in success_statuses or value.get("launch_authorized") is True:
        raise ExecutionError(f"{label} is not a successful reviewed proof")
    rows = value.get(list_key)
    if not isinstance(rows, list) or not rows:
        raise ExecutionError(f"{label} has no {list_key}")
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in success_statuses or not isinstance(row.get("artifact_roles"), list) or not row["artifact_roles"]:
            raise ExecutionError(f"{label} contains an incomplete proof row")
    return value


def _validate_advisory_regression(value: Any, *, test_only: bool) -> None:
    """Preserve a supplied historical review without requiring a passing campaign."""
    if not isinstance(value, dict) or value.get("schema_version") != "assignment.historical-regression-proof.v2":
        raise ExecutionError("historical regression advisory has an unsupported schema")
    if not test_only and (value.get("test_only") is True or value.get("evidence_kind") in {"offline_test_fixture", "synthetic_test"}):
        raise ExecutionError("test-only historical regression advisory cannot bind a live plan")


def _validate_source_members(archive_path: Path, files: Any) -> None:
    """Verify archived bytes against the existing member inventory, without extracting."""
    if not isinstance(files, list) or not files:
        raise ExecutionError("source bundle manifest has no file inventory")
    expected = {}
    for row in files:
        if not isinstance(row, dict):
            raise ExecutionError("source bundle member inventory is malformed")
        name, size, digest = row.get("path"), row.get("size_bytes"), row.get("sha256")
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or ".." in Path(name).parts or name in expected
                or not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(digest, str) or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)):
            raise ExecutionError("source bundle member inventory is malformed")
        expected[name] = (size, digest)
    seen = set()
    try:
        with tarfile.open(archive_path, "r:*") as archive:
            for member in archive:
                if not member.isfile() or member.name not in expected or member.name in seen:
                    raise ExecutionError("source archive members differ from the manifest")
                seen.add(member.name)
                size, digest = expected[member.name]
                if member.size != size:
                    raise ExecutionError(f"source archive member size mismatch: {member.name}")
                handle = archive.extractfile(member)
                if handle is None:
                    raise ExecutionError(f"source archive member is unreadable: {member.name}")
                actual = hashlib.sha256()
                with handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        actual.update(block)
                if actual.hexdigest() != digest:
                    raise ExecutionError(f"source archive member hash mismatch: {member.name}")
    except (OSError, tarfile.TarError) as exc:
        raise ExecutionError(f"cannot verify source archive members: {exc}") from exc
    if seen != set(expected):
        raise ExecutionError("source archive members differ from the manifest")


def _validate_production_execution_binding(header: Mapping[str, Any]) -> None:
    binding = header.get(PRODUCTION_EXECUTION_BINDING_FIELD)
    if not isinstance(binding, dict) or not PRODUCTION_EXECUTION_BINDING_FIELDS <= set(binding) <= PRODUCTION_EXECUTION_BINDING_FIELDS | PRODUCTION_OPTIONAL_EXECUTION_BINDING_FIELDS:
        raise ExecutionError("frozen production plan execution binding is malformed")
    if binding.get("schema_version") != PRODUCTION_EXECUTION_BINDING_SCHEMA or binding.get("status") != "frozen_for_execution" or not isinstance(binding.get("test_only"), bool) or binding.get("candidate_id") != header.get("candidate_id"):
        raise ExecutionError("frozen production plan execution binding identity is malformed")
    selected = _production_setting_values(binding.get("selected_settings"), label="execution_binding.selected_settings")
    if selected != header.get("candidate_final_configuration"):
        raise ExecutionError("frozen production plan selected settings do not match the candidate")

    refs: dict[str, tuple[Path, str, Any]] = {}
    for field in set(binding) - {"schema_version", "status", "test_only", "candidate_id", "selected_settings"}:
        refs[field] = _read_bound_artifact(
            binding[field],
            label=f"execution_binding.{field}",
            parse_json=field != "candidate_inventory",
        )
    selection = refs["selection_record"][2]
    if not isinstance(selection, dict) or selection.get("schema_version") != "assignment-production-candidate-selection.v1" or selection.get("status") != "selected" or selection.get("plan_id") != header.get("plan_id") or selection.get("candidate_id") != header.get("candidate_id") or selection.get("selected_settings") != header.get("candidate_final_configuration"):
        raise ExecutionError("selection record is not bound to the frozen candidate")
    candidate_config = refs["candidate_config"][2]
    if not isinstance(candidate_config, dict) or candidate_config.get("schema_version") != SCHEMA_VERSION or candidate_config.get("planning_only") is not True or candidate_config.get("plan_id") != header.get("plan_id") or candidate_config.get("production_configuration", {}).get("candidate_id") != header.get("candidate_id") or candidate_config.get("production_configuration", {}).get("final_configuration") != header.get("candidate_final_configuration"):
        raise ExecutionError("candidate config is not bound to the frozen production plan")
    if refs["candidate_config"][1] != header.get("config_sha256"):
        raise ExecutionError("candidate config hash does not match the frozen plan")
    runtime_manifest = refs["runtime_manifest"][2]
    if not isinstance(runtime_manifest, dict) or runtime_manifest.get("schema_version") != "assignment-runtime-manifest.v1":
        raise ExecutionError("frozen execution binding does not contain a reviewed runtime manifest")
    hardware = refs["remote_hardware_profile"][2]
    if not isinstance(hardware, dict) or not hardware:
        raise ExecutionError("frozen execution binding does not contain a remote hardware profile")
    contract = refs["acquisition_contract"][2]
    if not isinstance(contract, dict) or contract.get("schema_version") != "assignment.acquisition-contract.v2" or contract.get("production_case_count") != PRODUCTION_CASE_COUNT:
        raise ExecutionError("frozen execution binding does not contain the acquisition contract")
    _require_success_proof(refs["acquisition_proof"][2], label="acquisition proof", list_key="requirements")
    if "historical_regression_proof" in refs:
        _validate_advisory_regression(refs["historical_regression_proof"][2], test_only=binding["test_only"])
    pilot = refs["pilot_evidence"][2]
    if not isinstance(pilot, dict) or pilot.get("schema_version") != "assignment.instrumentation-pilot-evidence.v2" or pilot.get("status") not in {"pass", "passed"} or pilot.get("launch_authorized") is True or pilot.get("frozen_pilot_configuration") != header.get("candidate_final_configuration"):
        raise ExecutionError("pilot evidence is not a successful exact-configuration proof")
    review = pilot.get("review")
    required_review = ACQUISITION_REVIEW_FIELDS
    if not isinstance(review, dict) or not required_review <= set(review) or not all(review.get(name) is True for name in required_review):
        raise ExecutionError("pilot evidence review is incomplete")
    selected = pilot.get("selected_case_ids")
    if not isinstance(selected, list) or not selected or not all(isinstance(case, str) and case.strip() for case in selected) or len(set(selected)) != len(selected):
        raise ExecutionError("pilot evidence needs nonempty distinct capture case IDs")
    source_bundle_path, _source_bundle_digest, source_bundle = refs["source_bundle"]
    if not isinstance(source_bundle, dict) or source_bundle.get("schema_version") != "assignment.offline-source-bundle.v1":
        raise ExecutionError("frozen execution binding does not contain an offline source manifest")
    if not binding["test_only"]:
        git_head = source_bundle.get("git_head")
        if not isinstance(git_head, str) or len(git_head) != 40 or any(character not in "0123456789abcdef" for character in git_head):
            raise ExecutionError("live frozen execution requires source ancestry metadata")
        if source_bundle.get("test_only") is True or source_bundle.get("evidence_kind") in {"offline_test_fixture", "synthetic_test"}:
            raise ExecutionError("test-only source bundle cannot bind a live plan")
    bundle_name = source_bundle.get("bundle")
    bundle_digest = source_bundle.get("bundle_sha256")
    if not isinstance(bundle_name, str) or not bundle_name or Path(bundle_name).name != bundle_name or not isinstance(bundle_digest, str) or len(bundle_digest) != 64 or set(bundle_digest) == {"0"} or any(character not in "0123456789abcdef" for character in bundle_digest):
        raise ExecutionError("offline source manifest bundle binding is malformed")
    bundle_path = source_bundle_path.parent / bundle_name
    if bundle_path.is_symlink() or not bundle_path.is_file() or _sha256(bundle_path) != bundle_digest:
        raise ExecutionError("offline source bundle bytes do not match the sealed source manifest")
    _validate_source_members(bundle_path, source_bundle.get("files"))


def _validate_production_plan(header: Mapping[str, Any], cases: list[dict[str, Any]]) -> None:
    candidate_fields = PRODUCTION_PLAN_FIELDS
    frozen_fields = PRODUCTION_PLAN_FIELDS | frozenset({PRODUCTION_EXECUTION_BINDING_FIELD})
    if frozenset(header) not in {candidate_fields, frozen_fields}:
        raise ExecutionError("production candidate plan has missing or unknown fields")
    if header.get("record_type") != "plan" or header.get("schema_version") != PRODUCTION_SCHEMA_VERSION:
        raise ExecutionError("unsupported production candidate plan schema")
    if header.get("namespace") != PRODUCTION_NAMESPACE or header.get("plan_id") != PRODUCTION_PLAN_ID:
        raise ExecutionError("production candidate plan identity is unsupported")
    status = header.get("status")
    final_configuration_status = header.get("final_configuration_status")
    if header.get("planning_only") is not True or status not in {"candidate_pending_selection", "frozen_for_execution"}:
        raise ExecutionError("production candidate plan has an invalid selection status")
    if (status == "candidate_pending_selection") != (final_configuration_status == "candidate_not_selected"):
        raise ExecutionError("production candidate plan selection status is inconsistent")
    if (status == "frozen_for_execution") != (final_configuration_status == "selected"):
        raise ExecutionError("frozen production plan must record a selected configuration")
    if status == "candidate_pending_selection" and PRODUCTION_EXECUTION_BINDING_FIELD in header:
        raise ExecutionError("unselected production candidate must not carry an execution binding")
    if status == "frozen_for_execution" and PRODUCTION_EXECUTION_BINDING_FIELD not in header:
        raise ExecutionError("frozen production plan requires an execution binding")
    candidate_id = header.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ExecutionError("production candidate plan requires a candidate_id")
    candidate_settings = _production_setting_values(header.get("candidate_final_configuration"), label="production candidate plan.candidate_final_configuration")
    if candidate_settings["call_limit"] not in {20, 30, 50, 100} or candidate_settings["max_output_tokens"] != 2048 or candidate_settings["observation_length"] not in {25000, 100000} or candidate_settings["temperature"] != 0.0 or candidate_settings["max_input_tokens"] not in {32768, 61440} or candidate_settings["top_p"] != 1.0 or candidate_settings["seed"] != 0:
        raise ExecutionError("production candidate plan has unsupported final settings")
    if header.get("serving_configuration") != {"max_model_len": 65536, "vllm_version": "0.10.0"}:
        raise ExecutionError("production candidate plan serving configuration is unsupported")
    config_sha256 = header.get("config_sha256")
    if not isinstance(config_sha256, str) or len(config_sha256) != 64 or set(config_sha256) == {"0"} or any(character not in "0123456789abcdef" for character in config_sha256):
        raise ExecutionError("production candidate plan config_sha256 is malformed")
    sources = header.get("sources")
    if not isinstance(sources, dict) or set(sources) != {"lite", "verified"}:
        raise ExecutionError("production candidate plan sources are malformed")
    for suite, expected_dataset in (("lite", "SWE-bench/SWE-bench_Lite"), ("verified", "SWE-bench/SWE-bench_Verified")):
        source = sources[suite]
        if not isinstance(source, dict) or set(source) != {"dataset", "revision", "manifest_sha256", "task_count"}:
            raise ExecutionError(f"production candidate plan source {suite} is malformed")
        if source["dataset"] != expected_dataset or not isinstance(source["revision"], str) or len(source["revision"]) != 40 or any(character not in "0123456789abcdef" for character in source["revision"]):
            raise ExecutionError(f"production candidate plan source {suite} revision is malformed")
        if not isinstance(source["manifest_sha256"], str) or len(source["manifest_sha256"]) != 64 or any(character not in "0123456789abcdef" for character in source["manifest_sha256"]):
            raise ExecutionError(f"production candidate plan source {suite} manifest hash is malformed")
        if source["task_count"] not in {300, 500}:
            raise ExecutionError(f"production candidate plan source {suite} count is malformed")
    step_two = header.get("step_2")
    if not isinstance(step_two, dict) or set(step_two) != {"shared_baseline", "task_selection", "selected_task_ids", "knobs"} or step_two["shared_baseline"] != "reuse_step_1_baseline":
        raise ExecutionError("production candidate plan step_2 metadata is malformed")
    selection = step_two["task_selection"]
    if not isinstance(selection, dict) or selection.get("algorithm") != "sha256_rank_v1" or not isinstance(selection.get("seed"), str) or not selection["seed"] or selection.get("tasks_per_suite") != 12:
        raise ExecutionError("production candidate plan task selection is malformed")
    selected = step_two["selected_task_ids"]
    if not isinstance(selected, dict) or set(selected) != {"lite", "verified"} or any(not isinstance(selected[suite], list) or len(selected[suite]) != 12 or len(set(selected[suite])) != 12 or not all(isinstance(item, str) and item for item in selected[suite]) for suite in ("lite", "verified")):
        raise ExecutionError("production candidate plan selected task IDs are malformed")
    knobs = step_two["knobs"]
    expected_knobs = {
        "call_limit": [20, 30, 50, 100],
        "max_output_tokens": [512, 1024, 2048, 4096],
        "observation_length": [10000, 25000, 50000, 100000],
        "temperature": [0.0, 0.2, 0.5, 0.8],
    }
    if not isinstance(knobs, list) or [item.get("name") if isinstance(item, dict) else None for item in knobs] != list(expected_knobs) or any(not isinstance(item, dict) or set(item) != {"name", "values"} or item["values"] != expected_knobs[item["name"]] for item in knobs):
        raise ExecutionError("production candidate plan sweep knobs are malformed")
    pins = header.get("pins")
    if not isinstance(pins, dict) or set(pins) != {"model", "model_revision", "tokenizer_revision", "swe_agent_revision", "swe_bench_revision", "vllm_version"}:
        raise ExecutionError("production candidate plan pins are malformed")
    for field in ("model_revision", "tokenizer_revision", "swe_agent_revision", "swe_bench_revision"):
        value = pins[field]
        if not isinstance(value, str) or len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
            raise ExecutionError(f"production candidate plan pin {field} is malformed")
    if pins["vllm_version"] != "0.10.0" or not isinstance(pins["model"], str) or not pins["model"].strip():
        raise ExecutionError("production candidate plan model pins are malformed")
    if header.get("telemetry") != {
        "manifest_schema": "assignment.telemetry.v2.manifest",
        "schema_version": "assignment.telemetry.v2",
        "instrumentation_version": "telemetry-v2-20260908",
        "feature_schema": "assignment.d9-feature.v2",
    }:
        raise ExecutionError("production candidate plan telemetry binding is malformed")
    validator_integration = header.get("validator_integration")
    if not isinstance(validator_integration, dict) or set(validator_integration) != {
        "production_schema",
        "current_run_matrix_schema",
        "current_case_runner_schema",
        "status",
        "do_not_relabel_as_historical_schema",
        "required_before_execution",
    }:
        raise ExecutionError("production candidate plan validator integration binding is malformed")
    if (
        validator_integration["production_schema"] != PRODUCTION_SCHEMA_VERSION
        or validator_integration["current_run_matrix_schema"] not in {SCHEMA_VERSION, PRODUCTION_SCHEMA_VERSION}
        or validator_integration["current_case_runner_schema"] not in {SCHEMA_VERSION, PRODUCTION_SCHEMA_VERSION}
        or not isinstance(validator_integration["status"], str)
        or not validator_integration["status"]
        or validator_integration["do_not_relabel_as_historical_schema"] is not True
        or not isinstance(validator_integration["required_before_execution"], str)
        or not validator_integration["required_before_execution"]
    ):
        raise ExecutionError("production candidate plan validator integration binding is malformed")
    source_template = header.get("source_template")
    if not isinstance(source_template, dict) or set(source_template) != {"path", "sha256", "case_count", "historical_ids_preserved_as_lineage"} or source_template.get("path") != "live-plan/full_matrix_case_inventory.jsonl" or source_template.get("case_count") != PRODUCTION_CASE_COUNT or source_template.get("historical_ids_preserved_as_lineage") is not True:
        raise ExecutionError("production candidate plan source template is malformed")
    source_sha = source_template.get("sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64 or any(character not in "0123456789abcdef" for character in source_sha):
        raise ExecutionError("production candidate plan source template SHA-256 is malformed")
    matrix = header.get("matrix")
    expected_matrix = {
        "step1_case_count": PRODUCTION_STEP1_CASE_COUNT,
        "step2_independent_case_count": PRODUCTION_STEP2_CASE_COUNT,
        "shared_baseline_coordinate_count": PRODUCTION_SHARED_BASELINE_COORDINATE_COUNT,
        "full_case_count": PRODUCTION_CASE_COUNT,
        "call_limit_grid": [20, 30, 50, 100],
        "other_sweep_grids": {
            "max_output_tokens": [512, 1024, 2048, 4096],
            "observation_length": [10000, 25000, 50000, 100000],
            "temperature": [0.0, 0.2, 0.5, 0.8],
        },
        "step2_values_are_nonbaseline_only": True,
    }
    if matrix != expected_matrix:
        raise ExecutionError("production candidate plan matrix binding is malformed")
    if header.get("fresh_identity") != {
        "case_id_prefix": f"{PRODUCTION_NAMESPACE}:",
        "resume_key_equals_case_id": True,
        "old_completion_state_reused": False,
        "historical_template_case_id_field": "historical_template_case_id",
    }:
        raise ExecutionError("production candidate plan fresh identity binding is malformed")
    if header.get("holdout") != {
        "instance_id": "sympy__sympy-12481",
        "outcome_accessed": False,
        "split_assignment": "bound by separate production_split_manifest.v2.json",
    }:
        raise ExecutionError("production candidate plan holdout binding is malformed")
    if header.get("execution_case_count") != PRODUCTION_CASE_COUNT or len(cases) != PRODUCTION_CASE_COUNT or header.get("concurrency") != 1 or header.get("per_case_deadline_seconds") != 5400 or header.get("global_deadline_seconds") != 1209600:
        raise ExecutionError("production candidate plan execution limits or cardinality are malformed")
    seen: set[str] = set()
    baseline_count = 0
    shared_step2_count = 0
    sweep_counts: dict[str, dict[str, int]] = {}
    for line_number, case in enumerate(cases, 2):
        _validate_production_case(case, header=header, line_number=line_number)
        key = case["resume_key"]
        if key in seen:
            raise ExecutionError(f"duplicate production resume_key at plan line {line_number}")
        seen.add(key)
        if case["cell_id"] == "shared-baseline":
            baseline_count += 1
            if case["steps"] == [1, 2]:
                shared_step2_count += 1
        else:
            variation = case["variation"]
            knob = variation["knob"]
            value = json.dumps(variation["value"], sort_keys=True, separators=(",", ":"))
            sweep_counts.setdefault(knob, {})[value] = sweep_counts.setdefault(knob, {}).get(value, 0) + 1
    if baseline_count != PRODUCTION_STEP1_CASE_COUNT or shared_step2_count != 24:
        raise ExecutionError("production candidate plan baseline cardinality is malformed")
    production_grids = {
        "call_limit": (20, 30, 50, 100),
        "max_output_tokens": (512, 1024, 2048, 4096),
        "observation_length": (10000, 25000, 50000, 100000),
        "temperature": (0.0, 0.2, 0.5, 0.8),
    }
    expected_sweep_values = {
        knob: {
            json.dumps(value, sort_keys=True, separators=(",", ":"))
            for value in values
            if value != candidate_settings[knob]
        }
        for knob, values in production_grids.items()
    }
    if set(sweep_counts) != set(expected_sweep_values) or any(set(sweep_counts[knob]) != values or set(sweep_counts[knob].values()) != {24} for knob, values in expected_sweep_values.items()):
        raise ExecutionError("production candidate plan sweep cardinality is malformed")
    if status == "frozen_for_execution":
        _validate_production_execution_binding(header)


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
    if header.get("schema_version") == PRODUCTION_SCHEMA_VERSION:
        _validate_production_plan(header, cases)
        return header, cases
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


def load_plan(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load either the historical execution plan or a strict v2 candidate inventory.

    The v2 candidate inventory is deliberately loadable for preflight and
    case-spec integration checks, but remains marked ``candidate_pending_selection``;
    :func:`execute` refuses to treat it as an executable matrix until a final
    selection step emits the separately bound execution plan.
    """

    return _load_plan(path)


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
        "started_mono_ns": state["started_mono_ns"],
        "max_wall_seconds": state["max_wall_seconds"],
        "deadline_epoch": state["deadline_epoch"],
        "deadline_mono_ns": state["deadline_mono_ns"],
    }).encode("utf-8")).hexdigest()


def _new_state(
    plan_sha256: str,
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    max_wall: int,
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    now = int(time.time())
    started_mono_ns = time.monotonic_ns()
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
        "started_mono_ns": started_mono_ns,
        "max_wall_seconds": max_wall,
        "global_deadline_seconds": declared,
        "deadline_epoch": now + max_wall,
        "deadline_mono_ns": started_mono_ns + max_wall * 1_000_000_000,
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
    started_mono_ns = state.get("started_mono_ns")
    max_wall = state.get("max_wall_seconds")
    global_deadline = state.get("global_deadline_seconds")
    deadline = state.get("deadline_epoch")
    deadline_mono_ns = state.get("deadline_mono_ns")
    if any(not isinstance(value, int) or isinstance(value, bool) for value in (started, started_mono_ns, max_wall, global_deadline, deadline, deadline_mono_ns)):
        raise ExecutionError("resume deadline binding is malformed")
    if max_wall <= 0 or max_wall > global_deadline or global_deadline != header.get("global_deadline_seconds"):
        raise ExecutionError("resume deadline binding is outside the sealed plan deadline")
    if (
        deadline != started + max_wall
        or deadline_mono_ns != started_mono_ns + max_wall * 1_000_000_000
        or state.get("deadline_binding") != _deadline_binding(state)
    ):
        raise ExecutionError("resume deadline binding was changed")
    if requested_max_wall is not None and requested_max_wall != max_wall:
        raise ExecutionError("--max-wall-seconds cannot change an existing resume deadline")
    if started > int(time.time()) or started_mono_ns > time.monotonic_ns():
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
        bound_result = _validate_result(result_path, cases[index], expected_identity)
        if bound_result.get("schema_version") != RESULT_SCHEMA or bound_result.get("status") != "completed":
            raise ExecutionError(f"completed case binding points at a non-completed result: {result_path}")
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


def _run_case(command: list[str], timeout_seconds: float | int, stdout_path: Path, stderr_path: Path) -> tuple[int, bool]:
    """Launch one reviewed case under the inherited absolute deadline."""

    env = _without_v2_activation(os.environ)
    inherited_deadline = deadline_from_env(env)
    deadline = inherited_deadline
    timeout_cap: float | int | None = None
    if deadline is None:
        deadline = deadline_with_timeout(timeout_seconds)
        timeout_cap = timeout_seconds
    lifecycle_path = stdout_path.with_name("runner.lifecycle.json")
    if lifecycle_path.exists() or lifecycle_path.is_symlink():
        _archive_result(lifecycle_path, remove=False)
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            outcome = run_owned_process(
                command,
                env=env,
                stdout=stdout,
                stderr=stderr,
                deadline_mono_ns=deadline,
                timeout_seconds=timeout_cap,
            )
    except Exception as exc:
        # Preserve a machine-readable supervisor failure even when the child
        # could not be launched.  The matrix turns this into a failure-only
        # result after the child/supervisor has fully exited.
        _atomic_json(
            lifecycle_path,
            {
                "schema_version": "assignment-matrix-runner-lifecycle.v1",
                "status": "error",
                "deadline_mono_ns": deadline,
                "cleanup_complete": False,
                "cleanup": {"cleanup_complete": False, "reason": str(exc)},
                "error_type": type(exc).__name__,
                "reason": str(exc),
            },
        )
        raise
    _atomic_json(
        lifecycle_path,
        {
            "schema_version": "assignment-matrix-runner-lifecycle.v1",
            "status": "timeout" if outcome.timed_out else "completed",
            "returncode": outcome.returncode,
            "timed_out": outcome.timed_out,
            "deadline_mono_ns": outcome.deadline_mono_ns,
            "started_mono_ns": outcome.started_mono_ns,
            "ended_mono_ns": outcome.ended_mono_ns,
            "cleanup_complete": outcome.cleanup.get("cleanup_complete") is True,
            "cleanup": dict(outcome.cleanup),
        },
    )
    return outcome.returncode, outcome.timed_out


def _failure_inventory(output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Inventory retained case evidence without following symlinks.

    This runs only after the owned case supervisor has returned and all matrix
    log streams are closed.  The result marker and its sidecar are excluded so
    their own hashes cannot create a recursive or unstable record.
    """

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    def onerror(exc: OSError) -> None:
        errors.append({"path": str(getattr(exc, "filename", "")), "error": str(exc)})

    if not output_dir.exists():
        return records, errors
    for directory, directories, files in os.walk(output_dir, followlinks=False, onerror=onerror):
        parent = Path(directory)
        linked_directories = [name for name in directories if (parent / name).is_symlink()]
        directories[:] = [name for name in directories if name not in linked_directories]
        for name in sorted(files + linked_directories):
            path = parent / name
            relative = str(path.relative_to(output_dir))
            if relative in {"case_result.json", "case_result.json.sha256"}:
                continue
            try:
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode):
                    records.append({
                        "path": relative,
                        "kind": "symlink",
                        "target": os.readlink(path),
                        "mtime_ns": before.st_mtime_ns,
                    })
                    continue
                if not stat.S_ISREG(before.st_mode):
                    records.append({"path": relative, "kind": "special", "mtime_ns": before.st_mtime_ns})
                    continue
                digest = _sha256(path)
                after = path.stat()
                records.append({
                    "path": relative,
                    "kind": "file",
                    "sha256": digest,
                    "size": after.st_size,
                    "mtime_ns": after.st_mtime_ns,
                })
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    errors.append({"path": relative, "error": "artifact changed during inventory"})
            except (OSError, ValueError) as exc:
                errors.append({"path": relative, "error": str(exc)})
    return sorted(records, key=lambda item: item["path"]), errors


def _atomic_bytes(path: Path, payload: bytes) -> None:
    """Write bytes durably without replacing a target until complete."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _archive_result(path: Path, *, remove: bool) -> Path:
    """Snapshot one result and sidecar under a unique, immutable directory."""

    if path.is_symlink() or not path.is_file():
        raise ExecutionError(f"cannot archive non-regular case result: {path}")
    source_stat = path.stat()
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ExecutionError(f"cannot archive case result {path}: {exc}") from exc
    sidecar = Path(str(path) + ".sha256")
    sidecar_payload: bytes | None = None
    if sidecar.exists() or sidecar.is_symlink():
        if sidecar.is_symlink() or not sidecar.is_file():
            raise ExecutionError(f"cannot archive non-regular case result sidecar: {sidecar}")
        try:
            sidecar_payload = sidecar.read_bytes()
        except OSError as exc:
            raise ExecutionError(f"cannot archive case result sidecar {sidecar}: {exc}") from exc

    history = path.parent / "case_result_history"
    history.mkdir(parents=True, exist_ok=True)
    archive_dir: Path
    while True:
        archive_dir = history / f"{time.time_ns()}-{uuid.uuid4().hex}"
        try:
            archive_dir.mkdir()
            break
        except FileExistsError:
            continue
    archive_path = archive_dir / path.name
    _atomic_bytes(archive_path, payload)
    # Retain an exact copy of a supplied sidecar; for older results that had
    # none, create a canonical sidecar proving the archived bytes instead.
    if sidecar_payload is None:
        sidecar_payload = f"{hashlib.sha256(payload).hexdigest()}  {path.name}\n".encode("ascii")
    _atomic_bytes(archive_dir / sidecar.name, sidecar_payload)
    _atomic_json(
        archive_dir / "archive.json",
        {
            "schema_version": "assignment-case-result-archive.v1",
            "source": path.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "source_mtime_ns": source_stat.st_mtime_ns,
            "source_ctime_ns": source_stat.st_ctime_ns,
            "source_size": source_stat.st_size,
            "archived_epoch_ns": time.time_ns(),
        },
    )
    if remove:
        path.unlink()
        if sidecar.exists():
            sidecar.unlink()
    return archive_dir


def _archive_unpaired_sidecar(path: Path) -> Path | None:
    """Preserve an orphan sidecar before a new failure marker is written."""

    if not (path.exists() or path.is_symlink()):
        return None
    if path.is_symlink() or not path.is_file():
        raise ExecutionError(f"cannot archive non-regular case result sidecar: {path}")
    history = path.parent / "case_result_history"
    history.mkdir(parents=True, exist_ok=True)
    while True:
        archive_dir = history / f"{time.time_ns()}-{uuid.uuid4().hex}"
        try:
            archive_dir.mkdir()
            break
        except FileExistsError:
            continue
    _atomic_bytes(archive_dir / path.name, path.read_bytes())
    _atomic_json(
        archive_dir / "archive.json",
        {"schema_version": "assignment-case-result-archive.v1", "source": path.name, "archived_epoch_ns": time.time_ns()},
    )
    path.unlink()
    return archive_dir


def _read_lifecycle(case_root: Path) -> dict[str, Any] | None:
    path = case_root / "runner.lifecycle.json"
    if not path.exists() or path.is_symlink():
        return None
    try:
        value = _read_json(path)
    except ExecutionError:
        return {"status": "error", "cleanup_complete": False, "reason": "runner lifecycle metadata is invalid"}
    return value


def _write_result_sidecar(path: Path) -> str:
    digest = _sha256(path)
    _atomic_bytes(Path(str(path) + ".sha256"), f"{digest}  {path.name}\n".encode("ascii"))
    return digest


def _finalize_failure_result(
    case_root: Path,
    case: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    returncode: int | None,
    timed_out: bool,
    lifecycle: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Write a failure-only marker after all owned cleanup has completed."""

    if status not in {"failed", "timeout", "unavailable"}:
        raise ExecutionError(f"invalid failure marker status: {status}")
    result_path = case_root / "case_result.json"
    prior: dict[str, Any] = {}
    if result_path.exists() or result_path.is_symlink():
        if result_path.is_symlink():
            raise ExecutionError(f"case result must not be a symlink: {result_path}")
        try:
            parsed = _read_json(result_path)
        except ExecutionError:
            parsed = {}
        if isinstance(parsed, dict):
            prior = parsed
        _archive_result(result_path, remove=True)
    else:
        _archive_unpaired_sidecar(Path(str(result_path) + ".sha256"))

    previous_failure = prior.get("failure")
    failure = dict(previous_failure) if isinstance(previous_failure, Mapping) else {}
    failure.setdefault("type", "MatrixLifecycleFailure")
    failure.setdefault("message", reason)
    failure.setdefault("recorded_epoch_ns", time.time_ns())
    marker: dict[str, Any] = {
        **prior,
        "schema_version": FAILURE_RESULT_SCHEMA,
        "resume_key": case["resume_key"],
        "status": status,
        "reason": reason,
        "accepted": False,
        "failure": failure,
        "returncode": returncode,
        "timed_out": timed_out,
        "lifecycle": dict(lifecycle or {}),
    }
    marker["artifacts"], marker["inventory_errors"] = _failure_inventory(case_root)
    _atomic_json(result_path, marker)
    _write_result_sidecar(result_path)
    return marker


def _validate_result(path: Path, case: Mapping[str, Any], identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise ExecutionError(f"case result must not be a symlink: {path}")
    result = _read_json(path)
    schema = result.get("schema_version")
    if schema not in {RESULT_SCHEMA, FAILURE_RESULT_SCHEMA}:
        raise ExecutionError(f"case runner wrote unsupported result schema: {path}")
    if result.get("resume_key") != case["resume_key"]:
        raise ExecutionError(f"case result identity mismatch: {path}")
    status = result.get("status")
    if status not in {"completed", "failed", "timeout", "unavailable"}:
        raise ExecutionError(f"case result has invalid status: {path}")
    if schema == FAILURE_RESULT_SCHEMA:
        if status == "completed":
            raise ExecutionError(f"failure-only case result cannot be completed: {path}")
        if result.get("accepted") is not False:
            raise ExecutionError(f"failure-only case result must set accepted=false: {path}")
        if not isinstance(result.get("reason"), str) or not result["reason"].strip():
            raise ExecutionError(f"failure-only case result requires a failure reason: {path}")
        if not isinstance(result.get("artifacts"), list):
            raise ExecutionError(f"failure-only case result requires an artifacts list: {path}")
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
    _archive_result(path, remove=False)
    _atomic_json(path, result)
    _write_result_sidecar(path)
    return _validate_result(path, case, identity)


def execute(args: argparse.Namespace) -> int:
    plan_sha256 = _verify_sidecar(
        args.plan,
        args.sha256_sidecar or Path(f"{args.plan}.sha256"),
        label="plan",
    )
    header, cases = _load_plan(args.plan)
    if header.get("schema_version") == PRODUCTION_SCHEMA_VERSION and header.get("status") != "frozen_for_execution":
        raise ExecutionError(
            "assignment-production-v2 candidate inventory is loadable for preflight only; "
            "finalize candidate selection and emit a config-bound execution plan before running"
        )
    if header.get("schema_version") == PRODUCTION_SCHEMA_VERSION and header.get("execution_binding", {}).get("test_only") is True:
        raise ExecutionError("test-only frozen production plan cannot execute")
    if header.get("schema_version") == PRODUCTION_SCHEMA_VERSION:
        # A frozen plan binds the exact runtime/config artifacts used to
        # review the candidate.  Revalidate those same paths at execution;
        # accepting a different valid manifest would silently detach runtime
        # identity from the reviewed freeze.
        binding = header[PRODUCTION_EXECUTION_BINDING_FIELD]
        bound_runtime = Path(binding["runtime_manifest"]["path"]).absolute()
        if args.runtime_manifest.absolute() != bound_runtime:
            raise ExecutionError("runtime manifest path does not match the frozen execution binding")
        bound_config = Path(binding["candidate_config"]["path"]).absolute()
        supplied_config = (args.config or CHECKED_IN_CONFIG).absolute()
        if supplied_config != bound_config:
            raise ExecutionError("assignment config path does not match the frozen execution binding")
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
            remaining = remaining_seconds(state["deadline_mono_ns"])
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
            if result_path.exists() or result_path.is_symlink():
                case_identity = {
                    **identity,
                    "case_sha256": hashlib.sha256(expected_spec).hexdigest(),
                }
                prior = _validate_result(result_path, case)
                if prior["status"] == "completed":
                    # Preserve the exact pre-seal bytes before adding the
                    # runtime identity.  This archive is unique and never
                    # replaces an earlier retry snapshot.
                    _seal_result(result_path, case, case_identity)
                    _write_result_sidecar(result_path)
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
                _archive_result(result_path, remove=True)
            else:
                _archive_unpaired_sidecar(Path(str(result_path) + ".sha256"))
            state["active_resume_key"] = key
            _atomic_json(state_path, state)
            # Bind one absolute monotonic deadline for this case.  The case
            # runner, nested SWE-agent runner, and official evaluator all
            # inherit this exact value; none may start a fresh evaluator
            # timeout relative to its own launch.
            case_deadline_mono_ns = min(
                state["deadline_mono_ns"],
                time.monotonic_ns() + case["per_case_deadline_seconds"] * 1_000_000_000,
            )
            timeout_seconds = remaining_seconds(case_deadline_mono_ns)
            command = [
                str(runner),
                "--runtime-manifest", str(runtime_manifest),
                "--case-spec", str(spec_path),
                "--output-dir", str(case_root),
                "--execute",
            ]
            if getattr(args, "cpu_docker", False):
                command.append("--cpu-docker")
            # Preserve an outer owner identity if one was supplied.  A matrix
            # started directly establishes a fresh owner once and keeps it in
            # the inherited environment for this case only, so Docker labels
            # cannot accidentally match another matrix invocation.
            owner_had_value = CASE_OWNER_ENV in os.environ
            owner_previous = os.environ.get(CASE_OWNER_ENV)
            os.environ[CASE_OWNER_ENV] = uuid.uuid4().hex
            returncode = 127
            timed_out = False
            runner_error: Exception | None = None
            docker_cleanup: dict[str, Any] = {
                "status": "not_requested",
                "cleanup_complete": True,
                "retained": True,
            }
            try:
                with deadline_environment(case_deadline_mono_ns):
                    try:
                        returncode, timed_out = _run_case(
                            command,
                            timeout_seconds,
                            case_root / "runner.stdout.log",
                            case_root / "runner.stderr.log",
                        )
                    except Exception as exc:
                        # A supervisor/launch failure still gets a durable
                        # failure marker.  Docker cleanup must happen before
                        # the owner environment is restored.
                        runner_error = exc
                    if getattr(args, "cpu_docker", False):
                        try:
                            docker_cleanup = dict(
                                cleanup_owned_containers(
                                    os.environ[CASE_OWNER_ENV],
                                    case_root / "matrix_docker_cleanup",
                                )
                            )
                        except Exception as exc:
                            docker_cleanup = {
                                "schema_version": "assignment-owned-docker-cleanup.v1",
                                "owner": os.environ.get(CASE_OWNER_ENV),
                                "cleanup_complete": False,
                                "retained": True,
                                "errors": [{"type": type(exc).__name__, "reason": str(exc)}],
                            }
                    lifecycle_path = case_root / "runner.lifecycle.json"
                    lifecycle = _read_lifecycle(case_root)
                    if lifecycle is None:
                        # Real `_run_case` always writes this file. A missing
                        # record after a clean return means nothing owned is
                        # left to reap (helpers/tests). Errors and timeouts
                        # stay fail-closed.
                        lifecycle = {
                            "schema_version": "assignment-matrix-runner-lifecycle.v1",
                            "status": "inferred",
                            "cleanup_complete": runner_error is None and not timed_out,
                        }
                    lifecycle["docker_cleanup"] = docker_cleanup
                    lifecycle["cleanup_complete"] = (
                        lifecycle.get("cleanup_complete") is True
                        and docker_cleanup.get("cleanup_complete") is True
                    )
                    if runner_error is not None:
                        lifecycle["status"] = "error"
                        lifecycle["cleanup_complete"] = False
                        lifecycle["error_type"] = type(runner_error).__name__
                        lifecycle["reason"] = str(runner_error)
                    _atomic_json(lifecycle_path, lifecycle)
            finally:
                if owner_had_value:
                    assert owner_previous is not None
                    os.environ[CASE_OWNER_ENV] = owner_previous
                else:
                    os.environ.pop(CASE_OWNER_ENV, None)
            attempted += 1
            state["active_resume_key"] = None
            lifecycle = _read_lifecycle(case_root)
            lifecycle_complete = lifecycle is None or lifecycle.get("cleanup_complete") is True
            if getattr(args, "cpu_docker", False):
                lifecycle_complete = lifecycle_complete and docker_cleanup.get("cleanup_complete") is True
            try:
                result = _validate_result(result_path, case)
            except ExecutionError as exc:
                result = None
                result_error = exc
            else:
                result_error = None
            completed_result = (
                runner_error is None
                and result_error is None
                and result is not None
                and result.get("schema_version") == RESULT_SCHEMA
                and result.get("status") == "completed"
                and returncode == 0
                and not timed_out
                and lifecycle_complete
            )
            if not completed_result:
                if runner_error is not None:
                    reason = f"case_runner_error:{type(runner_error).__name__}:{runner_error}"
                elif timed_out:
                    reason = "timeout_without_valid_result" if result_error is not None else "case deadline expired"
                elif result_error is not None:
                    reason = f"invalid_result:{result_error}"
                elif not lifecycle_complete:
                    reason = "owned lifecycle cleanup incomplete"
                elif result is not None and isinstance(result.get("reason"), str) and result["reason"].strip():
                    reason = result["reason"]
                elif result is not None:
                    reason = str(result.get("status", "case runner failed"))
                else:
                    reason = "case runner exited without a result"
                marker_status = "timeout" if timed_out else (
                    result.get("status") if result is not None and result.get("status") in {"failed", "timeout", "unavailable"} else "failed"
                )
                marker = _finalize_failure_result(
                    case_root,
                    case,
                    status=marker_status,
                    reason=reason,
                    returncode=returncode,
                    timed_out=timed_out,
                    lifecycle=lifecycle,
                )
                state["failed_cases"][key] = {
                    "returncode": returncode,
                    "reason": reason,
                    "status": marker_status,
                    "result_sha256": _sha256(result_path),
                    "cleanup_complete": lifecycle_complete,
                }
                _atomic_json(state_path, state)
                # A required v2 capture/audit failure is an infrastructure
                # stop condition.  Continuing would silently assign the
                # remaining matrix cases without the evidence contract.
                failure = marker.get("failure") if isinstance(marker, Mapping) else None
                if isinstance(failure, Mapping) and failure.get("halt_matrix") is True:
                    state["status"] = "halted_evidence_failure"
                    state["halt_reason"] = str(failure.get("message") or reason)
                    state["halt_resume_key"] = key
                    _atomic_json(state_path, state)
                    return 5
                continue
            case_identity = {
                **identity,
                "case_sha256": hashlib.sha256(expected_spec).hexdigest(),
            }
            _seal_result(result_path, case, case_identity)
            _write_result_sidecar(result_path)
            state["completed_resume_keys"].append(key)
            state["completed_case_results"][key] = {
                "case_index": index,
                **case_identity,
                "result_sha256": _sha256(result_path),
            }
            completed.add(key)
            state["failed_cases"].pop(key, None)
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
