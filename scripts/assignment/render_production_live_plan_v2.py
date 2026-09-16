#!/usr/bin/env python3
"""Render the concrete offline production/pilot manifests for assignment v2.

This is a source-bound renderer for the current run design, rather than an
execution framework.  It emits fresh candidate inventories' companion
manifests, executable configuration-command records, the 16-case reuse map,
the four-fixture/24-pass overhead protocol, a cluster split, and explicitly
pending acquisition/regression proof templates.  It never launches a model,
evaluator, container, or remote command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SNAPSHOT_ID = "20260908T140000Z-offline-v2"
PLAN_ID = "assignment-production-v2-20260908"
PRODUCTION_NAMESPACE = "assignment-production-v2"
CONFIRMATION_PLAN_ID = "assignment-configuration-confirmation-v2-20260908"
CONFIRMATION_NAMESPACE = "assignment-configuration-confirmation-v2"
REPLAY_NAMESPACE = "instrumentation-pilot-overhead-v2"
HOLDOUT_INSTANCE_ID = "sympy__sympy-12481"
CONFIRMATION_COUNT = 96
PILOT_COUNT = 16
FINAL_CONFIGURATION_KEYS = (
    "call_limit",
    "max_output_tokens",
    "observation_length",
    "temperature",
    "max_input_tokens",
    "top_p",
    "seed",
)
PILOT_REVIEW_FIELDS = (
    "offline_tests_passed",
    "instrumentation_tests_passed",
    "remote_artifacts_reconciled",
    "remote_pins_verified",
    "legacy_process_noninterference_verified",
    "useful_live_workload_descriptors_verified",
    "train_serve_projection_reviewed",
    "holdout_isolation_verified",
    "external_event_coverage_verified",
    "e2e_attribution_improvement_verified",
    "evaluator_correctness_verified",
    "interruption_resume_verified",
    "literal_pdf_acquisition_verified",
    "historical_failure_regressions_verified",
    "individual_cpu_records_verified",
    "raw_model_records_verified",
)
REQUIRED_ARTIFACT_ROLES = (
    "pilot_inventory",
    "event_journals",
    "overhead_replay",
    "source_bundle",
    "remote_reconciliation",
    "feature_parity",
    "run_manifest",
    "acquisition_contract",
    "acquisition_proof",
    "historical_regression_proof",
    "raw_cpu_record_inventory",
    "raw_model_record_inventory",
    "offline_test_report",
    "full_matrix_inventory",
)
MODEL_NAME = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
SWE_AGENT_REVISION = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
SWE_BENCH_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
VLLM_VERSION = "0.10.0"
SERVING_MAX_MODEL_LEN = 65536
TELEMETRY_BINDING = {
    "manifest_schema": "assignment.telemetry.v2.manifest",
    "schema_version": "assignment.telemetry.v2",
    "instrumentation_version": "telemetry-v2-20260908",
    "feature_schema": "assignment.d9-feature.v2",
}


class LivePlanError(ValueError):
    """Raised when an offline source cannot support the current live plan."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LivePlanError(message)


def _write_bytes(path: Path, payload: bytes, *, force: bool = True) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        _require(not path.exists() and not Path(f"{path}.sha256").exists(), f"refusing to overwrite {path}")
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
    digest = sha256_bytes(payload)
    sidecar = Path(f"{path}.sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return digest


def _write_json(path: Path, value: Any, *, force: bool = True) -> str:
    return _write_bytes(path, (canonical_json(value) + "\n").encode("utf-8"), force=force)


def _read_json(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LivePlanError(f"cannot read JSON {path}: {exc}") from exc
    return value


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _load_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"blank JSONL line {path}:{number}")
        value = json.loads(line)
        _require(isinstance(value, dict), f"JSONL line is not an object {path}:{number}")
        rows.append(value)
    _require(rows and rows[0].get("record_type") == "plan", f"missing plan header: {path}")
    return rows[0], rows[1:], sha256_bytes(raw)


def _source_binding(path: Path, root: Path, *, count: int | None = None) -> dict[str, Any]:
    result = {"path": _rel(path, root), "sha256": sha256_file(path)}
    if count is not None:
        result["count"] = count
    return result


def _load_candidate_manifest(live_plan: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = live_plan / "production_candidate_inventory_manifest.json"
    value = _read_json(path)
    _require(value.get("candidate_count") == 4, "candidate inventory manifest must declare four candidates")
    _require(value.get("final_candidate_selection") == "pending_Astra_live_confirmation", "candidate selection is not pending")
    manifest = {"path": _rel(path, live_plan), "sha256": sha256_file(path), "value": value}
    return value, manifest


def _candidate_inventory_cases(live_plan: Path, candidate_manifest: Mapping[str, Any], candidate_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    entries = [item for item in candidate_manifest.get("candidate_inventories", []) if item.get("candidate_id") == candidate_id]
    _require(len(entries) == 1, f"missing candidate inventory: {candidate_id}")
    path = live_plan / str(entries[0]["path"])
    header, cases, digest = _load_jsonl(path)
    _require(digest == entries[0].get("sha256"), f"candidate inventory hash mismatch: {candidate_id}")
    _require(len(cases) == 1088, f"candidate inventory cardinality mismatch: {candidate_id}")
    return header, cases, digest


def _render_request_configs(*, live_plan: Path, candidates: Sequence[Mapping[str, Any]], repo_root: Path) -> dict[str, dict[str, Any]]:
    request_dir = live_plan / "confirmation-request-configs"
    records: dict[str, dict[str, Any]] = {}
    environment_path = repo_root / "configs" / "noninteractive_tool_environment.v1.json"
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    _require(environment.get("schema_version") == "assignment.noninteractive-tool-environment.v1", "invalid tool environment schema")
    _require(environment.get("env_variables") == {"GIT_PAGER": "cat", "MANPAGER": "cat", "PAGER": "cat"}, "unreviewed tool environment")
    for candidate in candidates:
        candidate_id = str(candidate["candidate_id"])
        settings = dict(candidate["settings"])
        payload = {
            "agent": {
                "tools": {"env_variables": dict(environment["env_variables"])},
                "model": {
                    "completion_kwargs": {
                        "max_tokens": settings["max_output_tokens"],
                        "seed": settings["seed"],
                        "top_p": settings["top_p"],
                    }
                }
            }
        }
        filename = f"{_safe_slug(candidate_id)}.json"
        path = request_dir / filename
        digest = _write_json(path, payload)
        records[candidate_id] = {
            "path": _rel(path, live_plan),
            "sha256": digest,
            "tool_environment_sha256": hashlib.sha256(environment_path.read_bytes()).hexdigest(),
            "settings_fields": {
                "max_output_tokens": "agent.model.completion_kwargs.max_tokens",
                "top_p": "agent.model.completion_kwargs.top_p",
                "seed": "agent.model.completion_kwargs.seed",
            },
        }
    return records


def _effective_command(*, settings: Mapping[str, Any], instance_id: str) -> list[str]:
    """Return the literal v1.1 SWE-agent argv with every setting applied."""

    return [
        "uv", "run", "--project", "{swe_agent_project}", "sweagent", "run-batch",
        "--config", "{agent_config}", "--config", "{request_config}",
        "--instances.type", "file", "--instances.path", "{instances_path}",
        "--instances.filter", f"^{instance_id}$",
        "--agent.model.name", MODEL_NAME,
        "--agent.model.api_base", "{proxy_api_base}", "--agent.model.api_key", "{vllm_api_key}",
        "--agent.model.total_cost_limit", "0", "--agent.model.per_instance_cost_limit", "0",
        "--agent.model.per_instance_call_limit", str(settings["call_limit"]),
        "--agent.model.temperature", str(settings["temperature"]),
        "--agent.model.max_input_tokens", str(settings["max_input_tokens"]),
        "--agent.model.max_output_tokens", str(settings["max_output_tokens"]),
        "--agent.templates.max_observation_length", str(settings["observation_length"]),
        "--output_dir", "{runner_output_dir}", "--num_workers", "1",
    ]


def _build_confirmation_plan(*, live_plan: Path, panel: Mapping[str, Any], candidate_manifest: Mapping[str, Any], repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = list(panel.get("candidates", []))
    _require(len(candidates) == 4, "confirmation panel candidate count must be four")
    request_configs = _render_request_configs(live_plan=live_plan, candidates=candidates, repo_root=repo_root)
    panel_instances = {str(row["panel_case_id"]): row for row in panel["panel"]["instances"]}
    candidate_cases = sorted(panel.get("candidate_cases", []), key=lambda row: (row["panel_case_id"], row["candidate_id"]))
    _require(len(candidate_cases) == CONFIRMATION_COUNT, "confirmation panel must contain 96 candidate cases")
    case_spec_dir = live_plan / "configuration-confirmation-case-specs"
    case_rows: list[dict[str, Any]] = []
    case_specs: list[dict[str, Any]] = []
    command_hashes: set[str] = set()
    for index, candidate_case in enumerate(candidate_cases, 1):
        candidate_id = str(candidate_case["candidate_id"])
        settings = dict(candidate_case["final_configuration"])
        _require(set(settings) == set(FINAL_CONFIGURATION_KEYS), f"{candidate_id}: canonical configuration keys mismatch")
        panel_instance = panel_instances.get(str(candidate_case["panel_case_id"]))
        _require(panel_instance is not None, f"missing panel instance for {candidate_case['candidate_case_id']}")
        source_spec = dict(panel_instance["source_case_spec"])
        case_id = str(candidate_case["candidate_case_id"])
        case_spec = {
            "record_type": "case",
            "schema_version": "assignment.configuration-confirmation-case.v2",
            "plan_id": CONFIRMATION_PLAN_ID,
            "namespace": CONFIRMATION_NAMESPACE,
            "case_id": case_id,
            "resume_key": case_id,
            "candidate_id": candidate_id,
            "panel_case_id": candidate_case["panel_case_id"],
            "historical_template_case_id": candidate_case["historical_template_case_id"],
            "suite": candidate_case["suite"],
            "repository": candidate_case["repository"],
            "instance_id": candidate_case["instance_id"],
            "task_sha256": source_spec.get("task_sha256"),
            "source_manifest_sha256": source_spec.get("source_manifest_sha256"),
            "cell_id": source_spec.get("cell_id", "shared-baseline"),
            "settings": settings,
            "final_configuration": dict(settings),
            "serving_configuration": dict(candidate_case["serving_configuration"]),
            "instrumentation": dict(TELEMETRY_BINDING),
            "outcome_blind": True,
            "outcomes_accessed": False,
            "source_case_spec_sha256": candidate_case["source_case_spec_sha256"],
            "case_spec_sha256": candidate_case["case_spec_sha256"],
        }
        spec_path = case_spec_dir / f"{index:03d}-{_safe_slug(candidate_id)}.json"
        spec_digest = _write_json(spec_path, case_spec)
        request = request_configs[candidate_id]
        command = _effective_command(settings=settings, instance_id=str(candidate_case["instance_id"]))
        command_hash = sha256_bytes((canonical_json(command) + "\n").encode("utf-8"))
        _require(command_hash not in command_hashes, "duplicate confirmation command hash")
        command_hashes.add(command_hash)
        reviewed_case_runner = [
            "python3", "scripts/assignment/sweagent_case_runner.py",
            "--case-spec", "{case_spec_path}", "--output-dir", "{attempt_output_dir}",
            "--runtime-manifest", "{runtime_manifest}", "--execute",
        ]
        case_rows.append({
            "execution_index": index,
            "candidate_case_id": case_id,
            "candidate_id": candidate_id,
            "panel_case_id": candidate_case["panel_case_id"],
            "historical_template_case_id": candidate_case["historical_template_case_id"],
            "suite": candidate_case["suite"],
            "repository": candidate_case["repository"],
            "instance_id": candidate_case["instance_id"],
            "case_spec": {"path": _rel(spec_path, live_plan), "sha256": spec_digest},
            "request_config": request,
            "settings": settings,
            "serving_configuration": candidate_case["serving_configuration"],
            "reviewed_case_runner_command_template": reviewed_case_runner,
            "effective_sweagent_command": command,
            "effective_sweagent_command_sha256": command_hash,
            "command_placeholders": [
                "swe_agent_project", "agent_config", "request_config", "instances_path",
                "proxy_api_base", "vllm_api_key", "runner_output_dir",
            ],
            "command_application": {
                "call_limit": {"argv_flag": "--agent.model.per_instance_call_limit", "value": settings["call_limit"]},
                "max_input_tokens": {"argv_flag": "--agent.model.max_input_tokens", "value": settings["max_input_tokens"]},
                "max_output_tokens": {"argv_flag": "--agent.model.max_output_tokens", "value": settings["max_output_tokens"], "request_field": "agent.model.completion_kwargs.max_tokens"},
                "observation_length": {"argv_flag": "--agent.templates.max_observation_length", "value": settings["observation_length"]},
                "temperature": {"argv_flag": "--agent.model.temperature", "value": settings["temperature"]},
                "top_p": {"request_field": "agent.model.completion_kwargs.top_p", "value": settings["top_p"]},
                "seed": {"request_field": "agent.model.completion_kwargs.seed", "value": settings["seed"]},
            },
            "status": "pending_live_confirmation",
            "outcome_accessed": False,
        })
        case_specs.append({"path": _rel(spec_path, live_plan), "sha256": spec_digest})
    plan = {
        "schema_version": "assignment.configuration-confirmation-execution-plan.v2",
        "snapshot_id": SNAPSHOT_ID,
        "plan_id": CONFIRMATION_PLAN_ID,
        "namespace": CONFIRMATION_NAMESPACE,
        "status": "pending_live_confirmation",
        "launch_authorized": False,
        "candidate_selection": "pending_Astra_live_confirmation",
        "execution_case_count": CONFIRMATION_COUNT,
        "candidate_count": 4,
        "panel_instance_count": 24,
        "case_schema": "assignment.configuration-confirmation-case.v2",
        "canonical_configuration_keys": list(FINAL_CONFIGURATION_KEYS),
        "serving_configuration_separate": {"max_model_len": SERVING_MAX_MODEL_LEN, "vllm_version": VLLM_VERSION},
        "runner": {
            "reviewed_case_runner": "scripts/assignment/sweagent_case_runner.py",
            "effective_command_origin": "src/agentic_sim/runners/sweagent_runner.py::build_command",
            "effective_command_template_supports_all_settings": True,
            "request_config_contract": "confirmation-request-configs/*.json",
            "current_local_case_runner_schema": "assignment-steps-1-3-plan.v1",
            "confirmation_case_schema": "assignment.configuration-confirmation-case.v2",
            "schema_adapter_status": "pending_v2_case_runner_integration; do not relabel confirmation cases as the historical schema",
            "concurrency": 1,
            "evaluator": "the reviewed case runner invokes the exact official evaluator after a trajectory; evaluator attempts are separately bounded",
        },
        "validator_integration": {
            "current_run_matrix_schema": "assignment-steps-1-3-plan.v1",
            "current_case_runner_schema": "assignment-steps-1-3-plan.v1",
            "confirmation_case_schema": "assignment.configuration-confirmation-case.v2",
            "status": "pending_v2_case_runner_integration",
            "do_not_relabel_as_historical_schema": True,
            "required_before_live": "reviewed adapter must validate canonical seven-key settings, fresh identity, pins, source hashes, and telemetry handshake",
        },
        "instrumentation": dict(TELEMETRY_BINDING),
        "pins": {
            "model": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "tokenizer_revision": MODEL_REVISION,
            "swe_agent_revision": SWE_AGENT_REVISION,
            "swe_bench_revision": SWE_BENCH_REVISION,
            "vllm_version": VLLM_VERSION,
            "remote_verification": "unresolved_offline",
        },
        "case_specs": case_specs,
        "cases": case_rows,
        "no_outcomes": True,
        "source_bindings": {
            "configuration_panel": {"path": "../configuration-analysis/CONFIGURATION_CONFIRMATION_PANEL.json", "sha256": sha256_file(live_plan.parent / "configuration-analysis" / "CONFIGURATION_CONFIRMATION_PANEL.json")},
            "candidate_inventory_manifest": {"path": "production_candidate_inventory_manifest.json", "sha256": sha256_file(live_plan / "production_candidate_inventory_manifest.json")},
            "telemetry_v2": _source_binding(repo_root / "src/agentic_sim/telemetry/v2.py", repo_root),
            "telemetry_features": _source_binding(repo_root / "src/agentic_sim/telemetry/features.py", repo_root),
        },
    }
    digest = _write_json(live_plan / "configuration_confirmation_execution_plan.json", plan)
    return plan, {"path": "configuration_confirmation_execution_plan.json", "sha256": digest}


def _build_pilot_reuse(*, live_plan: Path, snapshot_root: Path, panel: Mapping[str, Any], repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pilot = _read_json(live_plan / "pilot_cases.json")
    pilot_cases = list(pilot.get("cases", []))
    _require(len(pilot_cases) == PILOT_COUNT, "pilot inventory must contain 16 cases")
    panel_instances = {str(item["historical_template_case_id"]): item for item in panel["panel"]["instances"]}
    candidate_cases = list(panel["candidate_cases"])
    by_panel_candidate = {(str(item["panel_case_id"]), str(item["candidate_id"])): item for item in candidate_cases}
    entries: list[dict[str, Any]] = []
    for index, pilot_case in enumerate(pilot_cases, 1):
        historical_id = str(pilot_case.get("resume_key", pilot_case.get("case_id", "")))
        instance = panel_instances.get(historical_id)
        _require(instance is not None, f"pilot case is absent from confirmation panel: {historical_id}")
        panel_case_id = str(instance["panel_case_id"])
        mappings = {}
        for candidate in panel["candidates"]:
            candidate_id = str(candidate["candidate_id"])
            candidate_case = by_panel_candidate[(panel_case_id, candidate_id)]
            mappings[candidate_id] = {
                "candidate_case_id": candidate_case["candidate_case_id"],
                "settings_sha256": candidate_case["settings_sha256"],
                "case_spec_sha256": candidate_case["case_spec_sha256"],
                "exact_candidate_settings": candidate_case["final_configuration"],
            }
        entries.append({
            "pilot_order": index,
            "pilot_case_id": pilot_case.get("pilot_case_id"),
            "historical_template_case_id": historical_id,
            "historical_resume_key": historical_id,
            "panel_case_id": panel_case_id,
            "suite": pilot_case.get("suite"),
            "repository": pilot_case.get("repository"),
            "instance_id": pilot_case.get("instance_id"),
            "original_natural_settings": pilot_case.get("settings"),
            "candidate_mappings": mappings,
            "eligible_candidate_ids": ["historical-control-call30-input32768"],
            "reuse_rule": {
                "status": "pending_live_exact_match",
                "requires_selected_candidate_exact_settings": True,
                "requires_exact_telemetry_schema_and_recorder_source": True,
                "requires_exact_model_agent_serving_evaluator_pins": True,
                "requires_exact_case_and_attempt_identity": True,
                "otherwise": "do_not_reuse_and_report_unavailable_or_rerun",
            },
            "instrumentation_binding": {
                **TELEMETRY_BINDING,
                "recorder_source": {
                    "path": "src/agentic_sim/telemetry/v2.py",
                    "sha256": sha256_file(repo_root / "src/agentic_sim/telemetry/v2.py"),
                },
                "feature_source": {
                    "path": "src/agentic_sim/telemetry/features.py",
                    "sha256": sha256_file(repo_root / "src/agentic_sim/telemetry/features.py"),
                },
            },
            "outcome_accessed": False,
        })
    result = {
        "schema_version": "assignment.instrumentation-pilot-reuse-map.v2",
        "snapshot_id": SNAPSHOT_ID,
        "status": "pending_live_exact_match",
        "pilot_case_count": PILOT_COUNT,
        "final_candidate_selection": "pending_Astra_live_confirmation",
        "natural_pilot_namespace": "instrumentation-pilot-v1",
        "confirmation_namespace": CONFIRMATION_NAMESPACE,
        "reuse_only_if": [
            "selected candidate final_configuration exactly matches the natural run",
            "telemetry schema/version and recorder/feature source hashes exactly match",
            "model/tokenizer/SWE-agent/SWE-bench/vLLM/evaluator bindings exactly match",
        ],
        "cases": entries,
        "no_outcomes": True,
        "source_bindings": {
            "pilot_cases": _source_binding(live_plan / "pilot_cases.json", snapshot_root, count=16),
            "configuration_panel": _source_binding(snapshot_root / "configuration-analysis" / "CONFIGURATION_CONFIRMATION_PANEL.json", snapshot_root, count=24),
            "telemetry_v2": _source_binding(repo_root / "src/agentic_sim/telemetry/v2.py", repo_root),
            "telemetry_features": _source_binding(repo_root / "src/agentic_sim/telemetry/features.py", repo_root),
        },
    }
    digest = _write_json(live_plan / "pilot_reuse_mapping.v2.json", result)
    return result, {"path": "pilot_reuse_mapping.v2.json", "sha256": digest}


def _build_overhead_plan(*, live_plan: Path, pilot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    fixture_specs = [
        {
            "fixture_id": "cpu-file-traversal-v1",
            "fixture_kind": "cpu_filesystem_traversal",
            "description": "fixed read/write/traversal workload including find/walk style path enumeration",
            "required_operations": ["read", "write", "traversal"],
        },
        {
            "fixture_id": "cpu-test-script-subprocess-v1",
            "fixture_kind": "cpu_test_script_subprocess",
            "description": "fixed script edit/read, test-runner scope, pipeline and subprocess workload",
            "required_operations": ["script", "test", "pipeline", "subprocess"],
        },
        {
            "fixture_id": "model-short-request-v1",
            "fixture_kind": "model_short_request",
            "description": "fixed representative short model request with recorded action/request payload",
            "required_operations": ["model_request", "raw_request_body"],
        },
        {
            "fixture_id": "model-long-context-request-v1",
            "fixture_kind": "model_long_context_request",
            "description": "fixed representative long-context request exercising client guard and serving metrics",
            "required_operations": ["model_request", "raw_request_body", "context_tokens"],
        },
    ]
    fixture_ids = [row["fixture_id"] for row in fixture_specs]
    passes: list[dict[str, Any]] = []
    for fixture in fixture_specs:
        for repeat in range(3):
            order = "off_on" if repeat % 2 == 0 else "on_off"
            identity = {"fixture_id": fixture["fixture_id"], "repeat": repeat, "order": order, "namespace": REPLAY_NAMESPACE}
            passes.append({
                "pass_id": f"{REPLAY_NAMESPACE}:{sha256_bytes(canonical_json(identity).encode('utf-8'))}",
                "fixture_id": fixture["fixture_id"],
                "repeat": repeat,
                "order": order,
                "control_mode": "instrument_off",
                "treatment_mode": "instrument_on",
                "condition_command_template": [
                    "python3", "{fixed_work_adapter}", "--fixture-manifest", "{fixture_manifest}",
                    "--fixture-id", fixture["fixture_id"], "--instrumentation-mode", "{instrumentation_mode}",
                    "--scratch-dir", "{scratch_dir}", "--output-dir", "{output_dir}",
                    "--result-path", "{result_path}", "--repeat", str(repeat),
                ],
                "precondition_reset": {
                    "command_template": ["python3", "{reset_adapter}", "--snapshot", "{pretrajectory_snapshot}", "--destination", "{scratch_dir}"],
                    "required_before_each_condition": True,
                    "snapshot_sha256": None,
                },
                "same_workload_fields": [
                    "workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256",
                    "request_sequence_sha256", "output_token_count",
                ],
                "same_serving_and_cache_policy": True,
                "full_production_capture_enabled": None,
                "measured_control_wall_ms": None,
                "measured_instrumented_wall_ms": None,
                "workload_sha256": None,
                "status": "pending_live_fixture_capture",
                "invalid_pair_policy": "record any realized workload/output-token mismatch and exclude the pair; never compare unequal work",
            })
    result = {
        "schema_version": "assignment.instrumentation-overhead-replay-plan.v2",
        "snapshot_id": SNAPSHOT_ID,
        "namespace": REPLAY_NAMESPACE,
        "status": "pending_live_fixture_capture",
        "launch_authorized": False,
        "fixture_count": 4,
        "fixture_ids": fixture_ids,
        "fixtures": fixture_specs,
        "natural_pilot_case_ids": [row.get("pilot_case_id") for row in pilot.get("cases", [])],
        "natural_trajectory_count": PILOT_COUNT,
        "natural_trajectories_reported_separately": True,
        "paired_repetitions": 3,
        "pair_count": len(passes),
        "condition_pass_count": len(passes) * 2,
        "pass_count": len(passes) * 2,
        "orders_by_repeat": {"0": "off_on", "1": "on_off", "2": "off_on"},
        "passes": passes,
        "thresholds": {
            "median_relative_overhead_max": 0.05,
            "nearest_rank_p95_relative_overhead_max": 0.10,
            "absolute_values_reviewed": True,
            "startup_reported_separately": True,
        },
        "required_review_fields": {
            "individual_cpu_operation_records": "positive per fixture where CPU operation is exercised",
            "raw_model_request_records": "must equal physical_requests for model fixtures",
            "dropped_cpu_records": 0,
            "cpu_capture_map_failures": 0,
            "missing_raw_request_bodies": 0,
            "full_production_capture_enabled": True,
        },
        "fixed_work_contract": {
            "action_and_request_payloads": "captured once into immutable fixture manifests before replay",
            "filesystem_reset": "restore the same hash-bound pretrajectory snapshot before every condition",
            "serving_and_cache": "same endpoint, model, cache policy, and request payload; mismatch invalidates pair",
            "output_tokens": "record realized output_token_count on both sides; mismatch invalidates pair",
        },
        "executor_availability": {
            "current_script": "scripts/validation/run_instrumentation_replay.py",
            "current_cli_case_count": 4,
            "current_cli_supports_four_short_fixture_ids": True,
            "legacy_v1_case_count": 16,
            "status": "four_fixture_orchestrator_ready_live_adapter_and_fixtures_pending",
            "validation_command_template": [
                "python3", "scripts/validation/run_instrumentation_replay.py",
                "--manifest", "{fixture_manifest}", "--output-dir", "{output_dir}", "--execute",
            ],
            "fail_closed_if": ["fixture manifest is missing", "adapter cannot toggle mode", "reset fails", "workload equality evidence missing", "full capture flags not verified"],
        },
        "no_outcomes": True,
    }
    digest = _write_json(live_plan / "overhead_replay_plan.v2.json", result)
    return result, {"path": "overhead_replay_plan.v2.json", "sha256": digest}


def _build_split(*, live_plan: Path, candidate_manifest: Mapping[str, Any], panel: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_id = str(candidate_manifest["candidate_inventories"][0]["candidate_id"])
    _header, cases, inventory_sha = _candidate_inventory_cases(live_plan, candidate_manifest, candidate_id)
    panel_instances = {str(row["instance_id"]): row for row in panel["panel"]["instances"]}
    _require(len(panel_instances) == 24, "panel must contain 24 distinct instance clusters")
    seed = "assignment-production-v2-final-evaluation-20260908"
    all_instances = sorted({str(row["instance_id"]) for row in cases})
    _require(HOLDOUT_INSTANCE_ID in all_instances, "production inventory must retain the sealed holdout cluster")
    clusters: list[dict[str, Any]] = []
    for instance_id in all_instances:
        cluster_id = f"instance:{instance_id}"
        case_rows = [row for row in cases if str(row["instance_id"]) == instance_id]
        if instance_id == HOLDOUT_INSTANCE_ID:
            partition = "sealed_holdout"
            rank_hash = None
        elif instance_id in panel_instances:
            partition = "confirmation_development_excluded"
            rank_hash = None
        else:
            rank_hash = sha256_bytes(f"{seed}\0{cluster_id}".encode("utf-8"))
            partition = "pending_rank"
        clusters.append({
            "cluster_id": cluster_id,
            "instance_id": instance_id,
            "case_count": len(case_rows),
            "baseline_case_count": sum(row["cell_id"] == "shared-baseline" for row in case_rows),
            "step2_case_count": sum(row["cell_id"] != "shared-baseline" for row in case_rows),
            "panel_role": panel_instances.get(instance_id, {}).get("panel_role"),
            "rank_hash": rank_hash,
            "partition": partition,
            "outcome_accessed": False,
        })
    ranked = sorted((row for row in clusters if row["partition"] == "pending_rank"), key=lambda row: (row["rank_hash"], row["cluster_id"]))
    final_count = int(len(ranked) * 0.20)
    for index, row in enumerate(ranked):
        row["rank"] = index
        row["partition"] = "final_evaluation" if index < final_count else "train_calibration"
    for row in clusters:
        if row["partition"] != "pending_rank":
            row["rank"] = None
    by_cluster = {row["cluster_id"]: row for row in clusters}
    case_assignments = [
        {
            "candidate_id": candidate_id,
            "case_id": row["case_id"],
            "historical_template_case_id": row["historical_template_case_id"],
            "instance_id": row["instance_id"],
            "cluster_id": f"instance:{row['instance_id']}",
            "cell_id": row["cell_id"],
            "partition": by_cluster[f"instance:{row['instance_id']}"]["partition"],
            "outcome_accessed": False,
        }
        for row in cases
    ]
    result = {
        "schema_version": "assignment-production-instance-cluster-split.v2",
        "snapshot_id": SNAPSHOT_ID,
        "status": "frozen_offline_preseal_pending_live_outcome_access",
        "plan_id": PLAN_ID,
        "unit": "instance_cluster",
        "seed": seed,
        "hash_algorithm": "sha256(seed\\0cluster_id), ascending; final_evaluation=floor(20% of remaining clusters)",
        "production_case_count": 1088,
        "candidate_inventory_source": {"candidate_id": candidate_id, "sha256": inventory_sha},
        "all_candidate_inventories_share_cluster_assignment": True,
        "cluster_count": len(clusters),
        "clusters_excluded_from_final_d9": 24,
        "development_excluded_clusters": sorted(
            [{"cluster_id": row["cluster_id"], "instance_id": row["instance_id"], "panel_role": row["panel_role"]} for row in clusters if row["partition"] == "confirmation_development_excluded"],
            key=lambda row: row["cluster_id"],
        ),
        "sealed_holdout": {
            "instance_id": HOLDOUT_INSTANCE_ID,
            "cluster_id": f"instance:{HOLDOUT_INSTANCE_ID}",
            "suite_copy_count": 2,
            "partition": "sealed_holdout",
            "outcome_accessed": False,
        },
        "partition_counts": {
            partition: {
                "cluster_count": sum(row["partition"] == partition for row in clusters),
                "case_count": sum(row["partition"] == partition for row in case_assignments),
            }
            for partition in ("confirmation_development_excluded", "sealed_holdout", "train_calibration", "final_evaluation")
        },
        "fit_rule": "fit/freeze D9 on train_calibration only; do not inspect final_evaluation or sealed_holdout outcomes before freeze",
        "case_assignments": case_assignments,
        "clusters": sorted(clusters, key=lambda row: row["cluster_id"]),
        "no_evaluator_labels": True,
        "outcomes_accessed": False,
    }
    digest = _write_json(live_plan / "production_split_manifest.v2.json", result)
    return result, {"path": "production_split_manifest.v2.json", "sha256": digest}


def _build_operator_guidance(
    *,
    live_plan: Path,
    snapshot_root: Path,
    repo_root: Path,
    candidate_manifest: Mapping[str, Any],
    candidate_ref: Mapping[str, Any],
    confirmation_ref: Mapping[str, Any],
    overhead_ref: Mapping[str, Any],
    split_ref: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Write the executable current v2 operator documents.

    The old renderer wrote ``recovery_resume.md`` with a baseline-only
    procedure.  That file is deliberately preserved as historical material;
    every newly rendered v2 package gets an explicit replacement and a
    README pointer so an operator cannot mistake the old replay recipe for
    the current design.
    """

    repo_path = str(repo_root.resolve())
    snapshot_path = str(snapshot_root.resolve())
    candidate_rows = []
    for item in candidate_manifest["candidate_inventories"]:
        settings = item["settings"]
        candidate_rows.append(
            "| `{candidate_id}` | {call_limit} | {max_input_tokens} | {observation_length} |".format(
                candidate_id=item["candidate_id"],
                call_limit=settings["call_limit"],
                max_input_tokens=settings["max_input_tokens"],
                observation_length=settings["observation_length"],
            )
        )
    candidate_table = "\n".join(candidate_rows)

    readme = f"""# Offline v2 production live plan ({SNAPSHOT_ID})

This package was rendered without SSH, H100 access, model/evaluator execution, or holdout outcome access. `launch_authorized` is false and all live proof records remain pending.

The current operator procedure is [`recovery_resume.v2.md`](recovery_resume.v2.md). The sibling [`recovery_resume.md`](recovery_resume.md) is a preserved legacy document generated by the baseline renderer. It is explicitly superseded and must not be used for this package.

- Production is a fresh `{PLAN_ID}` namespace: four candidate inventories, each with 1,088 cases (800 Step 1 plus 288 independent Step 2 cases).
- The call-limit grid is `[20, 30, 50, 100]`; other sweep grids remain unchanged. Candidate selection is pending Astra's comparison on the predeclared panel.
- The fixed confirmation panel is 24 instances × four candidates = 96 trajectories. Those trajectories are selection evidence, not production cases.
- Instrumentation overhead is four fixed fixtures × three AB/BA/AB pairs = 12 pairs and 24 condition passes. It is separate from the 96-trajectory confirmation panel.
- The fourth candidate directly tests `observation_length=25000`; the old document's exclusion of that coordinate does not apply to this v2 candidate panel.

The generated artifact references are hash-bound in `production_live_plan_artifact_manifest.v2.json`. Remote reconciliation, live acquisition, historical-regression proof, source-bundle sealing, candidate selection, and launch authorization remain unresolved.
"""

    recovery = f"""# Current v2 recovery and execution procedure

This is the authoritative procedure for `{PLAN_ID}` in snapshot `{SNAPSHOT_ID}`. It is an offline plan and has `launch_authorized: false`; this document does not authorize a workload, evaluator, model server, container, SSH session, or paid GPU execution.

## Supersession and preserved history

The sibling `recovery_resume.md` is a preserved legacy artifact from the baseline renderer. It describes a baseline-only 16-case recipe, calls the natural-pilot replay 96 condition passes, and excludes the 25,000 observation coordinate. That procedure is explicitly superseded by this v2 document. Do not execute it or relabel its artifacts as v2 evidence. Historical snapshots remain immutable.

## Paths and offline checks

Set the paths for the checkout and this rendered package before using the commands below:

```bash
set -euo pipefail
REPO_ROOT={repo_path}
SNAPSHOT_ROOT={snapshot_path}
LIVE_PLAN="$SNAPSHOT_ROOT/live-plan"
PYTHON="$REPO_ROOT/.venv/bin/python"
WORK_ROOT=/absolute/path/to/assignment-production-v2-work
PROOF_ROOT="$WORK_ROOT/proof"
MATRIX_ROOT="$WORK_ROOT/matrix-runs"
FROZEN_PLAN="$WORK_ROOT/frozen-production-plan.jsonl"
RUNTIME_MANIFEST="$WORK_ROOT/runtime-manifest.json"
REMOTE_HARDWARE_PROFILE="$WORK_ROOT/remote-hardware-profile.json"
test -x "$PYTHON"
test -f "$LIVE_PLAN/production_execution_workflow.v2.json"
test -f "$LIVE_PLAN/production_run_manifest.v2.json"
```

These are bounded offline checks and do not launch a workload:

```bash
(cd "$REPO_ROOT" && PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src "$PYTHON" -m unittest discover -s tests/assignment -v)
(cd "$REPO_ROOT" && PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m compileall -q src/agentic_sim/assignment scripts/assignment tests/assignment)
```

The first command is a contract check. It does not prove live BPF capture, remote pins, H100 availability, evaluator correctness, or D1–D9 acquisition. Record any failure with the exact checkout and source hashes; concurrent edits to collector, runtime, proxy, or runner files invalidate a shared result.

## Required gate order

1. Reconcile the remote process inventory, H100 identity and serving flags, disk thresholds, source checkout, pins, endpoint listeners, and all historical legacy processes. Record fresh hash-bound evidence in a new continuation snapshot. The offline `remote_reconciliation` template is not proof.
2. Capture the natural 16-case pilot under its declared baseline. Reuse is allowed only when the selected candidate's seven settings, model/agent/evaluator/serving pins, telemetry schema and source hashes, and case identities match exactly. Otherwise the evidence is unavailable or must be rerun.
3. Execute the configuration confirmation panel: 24 instances × the four candidates below = 96 trajectories. Compare resolved counts, regressions, failures/context exits, budget exhaustion, and cost using the predeclared panel. Astra selects the strongest defensible candidate; production outcomes cannot tune that selection.
4. Execute the separate overhead protocol: four fixed short fixtures, three alternating AB/BA/AB pairs per fixture, 12 pairs and 24 condition passes. Reset the same workload snapshot before each condition and require workload, request/action sequence, and realized output-token equality. This is not a 96-trajectory replay.
5. Replace every pending acquisition and historical-regression proof row with independently reviewed live evidence. Required raw records, CPU operation records, loss/map-failure counts, request bodies, attribution, timing, disk, source, and legacy noninterference gates must be present. Documentation alone cannot pass a gate.
6. After Astra records the selected candidate and all gates pass, run the finalizer below. It creates the only plan that the current scheduler may execute. The finalizer never authorizes launch.
7. Run the scheduler in validation-only mode, inspect its output and all sidecars, then obtain the separate launch review. A fresh run uses the command with `--execute` and the paid-work acknowledgement; an interruption uses `--resume` against the same output directory after inspecting durable state.

### Candidate panel

| candidate | call limit | max input tokens | observation length |
| --- | ---: | ---: | ---: |
{candidate_table}

All candidates also bind `max_output_tokens=2048`, `temperature=0`, `top_p=1`, and `seed=0`. The candidate inventories and their current hashes are recorded in `{candidate_ref["path"]}`. The confirmation plan is `{confirmation_ref["path"]}` ({CONFIRMATION_COUNT} trajectories), the overhead plan is `{overhead_ref["path"]}` (24 condition passes), and the split manifest is `{split_ref["path"]}`.

The fourth candidate directly binds `call_limit=100`, `max_input_tokens=61440`, and `observation_length=25000` for the confirmation comparison. It is a current v2 candidate coordinate, not a globally forbidden value.

## Finalization and scheduler preflight

The selection record, runtime manifest, remote hardware profile, acquisition proof, regression proof, pilot evidence, and source bundle are required inputs. Their paths below are operator variables because their live evidence does not exist in this offline package:

```bash
CANDIDATE_ID=selected-candidate-id
CANDIDATE_INVENTORY="$LIVE_PLAN/production-candidates/$CANDIDATE_ID.jsonl"
CANDIDATE_CONFIG="$LIVE_PLAN/production-candidates/$CANDIDATE_ID.config.json"
SELECTION_RECORD="$WORK_ROOT/selection/$CANDIDATE_ID.json"
ACQUISITION_PROOF="$PROOF_ROOT/acquisition_proof.json"
REGRESSION_PROOF="$PROOF_ROOT/historical_regression_proof.json"
PILOT_EVIDENCE="$PROOF_ROOT/pilot_evidence.json"
SOURCE_BUNDLE="$PROOF_ROOT/source_bundle.json"

"$PYTHON" "$REPO_ROOT/scripts/assignment/finalize_production_plan.py" \\
  --candidate-inventory "$CANDIDATE_INVENTORY" \\
  --candidate-config "$CANDIDATE_CONFIG" \\
  --selection-record "$SELECTION_RECORD" \\
  --runtime-manifest "$RUNTIME_MANIFEST" \\
  --remote-hardware-profile "$REMOTE_HARDWARE_PROFILE" \\
  --acquisition-contract "$LIVE_PLAN/assignment_acquisition_contract.v2.json" \\
  --acquisition-proof "$ACQUISITION_PROOF" \\
  --historical-regression-proof "$REGRESSION_PROOF" \\
  --pilot-evidence "$PILOT_EVIDENCE" \\
  --source-bundle "$SOURCE_BUNDLE" \\
  --output-plan "$FROZEN_PLAN" \\
  --receipt "$WORK_ROOT/finalization-receipt.json"

"$PYTHON" "$REPO_ROOT/scripts/assignment/run_matrix.py" \\
  --plan "$FROZEN_PLAN" \\
  --sha256-sidecar "$FROZEN_PLAN.sha256" \\
  --runner "$REPO_ROOT/scripts/assignment/sweagent_case_runner.py" \\
  --runtime-manifest "$RUNTIME_MANIFEST" \\
  --runtime-manifest-sha256-sidecar "$RUNTIME_MANIFEST.sha256" \\
  --config "$CANDIDATE_CONFIG" \\
  --config-sha256 "$(sha256sum "$CANDIDATE_CONFIG" | awk '{{print $1}}')" \\
  --output-dir "$MATRIX_ROOT"
```

The scheduler command above is validation-only. A reviewed, fully gated launch would append `--execute --acknowledge-paid-gpu-work --max-wall-seconds 14400` to the same command. Do not add `--resume` to a fresh output directory; use it only for a previously started, inspected matrix. Neither command should be run until the status fields in the v2 manifest and all proof bindings are replaced with live evidence.

## Historical remote access procedure (read-only reference)

The historical transcript used a user-owned multiplexed PACE connection. This is a procedure to apply only during a separately authorized remote gate; it was not run while rendering this package:

```bash
PACE_SOCKET="$HOME/.ssh/cm/pace-control"
PACE_TARGET="jriverah3@128.61.254.151"
test -S "$PACE_SOCKET"
ssh -S "$PACE_SOCKET" -O check "$PACE_TARGET"
ssh -S "$PACE_SOCKET" -o ControlMaster=no -o BatchMode=yes "$PACE_TARGET"
```

The transcript identifies login-1 as `128.61.254.151` and login-2 as `128.61.254.154`. From an already listed Slurm allocation, its detached reverse relay used this shape, with `JOB` and `WW` replaced by the allocation and worker port; first verify that the listener is absent so an existing relay is never duplicated:

```bash
JOB=validated-slurm-job-id
WW=worker-port-suffix
nohup srun --jobid "$JOB" --overlap --ntasks=1 --exact --quiet bash -lc \\
  'exec ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=60 -o ServerAliveCountMax=3 -o StrictHostKeyChecking=yes -R 127.0.0.1:180WW:127.0.0.1:80WW jriverah3@128.61.254.151' \\
  >"$WORK_ROOT/relay-$WW.log" 2>&1 &
```

The CPU-side verification is `ss -ltn` followed by `/health`, `/v1/models`, and `/metrics` for every expected local listener `127.0.0.1:18000` through `127.0.0.1:18015`, plus a remote `squeue`/job and endpoint identity record. The historical transcript ended with workers 00, 05, and 06 unresolved; that is historical evidence, not current clearance. A fresh remote inventory must also prove that the old monitor/TCP bridges are gone or isolated before collection.

## Missing proof versus missing code

The renderer, four candidate inventories, 96-case panel, 16-case reuse map, 24-pass overhead specification, split, finalizer, and scheduler are present as offline planning code/artifacts. The following are still missing proof: remote reconciliation and H100 identity, clean source bundle and final hashes, selected candidate record, live 16-case evidence, all 96 comparison outcomes, all 24 overhead captures, A01–A15 and R01–R21 proof rows, raw request/CPU records and loss accounting, timing/attribution, legacy noninterference, evaluator review, and final launch review.

The overhead orchestrator accepts the four predeclared fixtures under its v2 schema and preserves the legacy 16-case v1 protocol. Live fixed-work adapters and captured fixtures still require integration before overhead can be treated as measured evidence. Each v2 result must report full capture, individual CPU/request counts, losses, measured work/startup durations, and serving/cache policy identity. These remain execution gates.

Current workflow references: `{confirmation_ref["path"]}`, `{overhead_ref["path"]}`, `{split_ref["path"]}`, and `production_execution_workflow.v2.json`. The current telemetry and native sink source hashes must be added to the root source bundle after integration; no source hash in this offline document authorizes launch.
"""

    readme_path = live_plan / "README.md"
    recovery_path = live_plan / "recovery_resume.v2.md"
    readme_ref = {"path": readme_path.name, "sha256": _write_bytes(readme_path, readme.encode("utf-8"))}
    recovery_ref = {"path": recovery_path.name, "sha256": _write_bytes(recovery_path, recovery.encode("utf-8"))}
    return {"readme": readme_ref, "recovery": recovery_ref}


def _build_workflow(*, live_plan: Path, candidate_manifest: Mapping[str, Any], candidate_ref: Mapping[str, Any], confirmation_ref: Mapping[str, Any], overhead_ref: Mapping[str, Any], split_ref: Mapping[str, Any], contract_ref: Mapping[str, Any], guidance_refs: Mapping[str, Mapping[str, Any]], source_documents: Mapping[str, Any], repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    inventories = [
        {"candidate_id": item["candidate_id"], "path": item["path"], "sha256": item["sha256"], "case_count": item["case_count"]}
        for item in candidate_manifest["candidate_inventories"]
    ]
    source_hashes = {
        "production_inventory_manifest": {"path": "production_candidate_inventory_manifest.json", "sha256": sha256_file(live_plan / "production_candidate_inventory_manifest.json")},
        "confirmation_execution_plan": dict(confirmation_ref),
        "overhead_replay_plan": dict(overhead_ref),
        "split_manifest": dict(split_ref),
        "acquisition_contract": dict(contract_ref),
        "production_inventory_generator": {"path": "scripts/assignment/generate_production_candidate_inventories.py", "sha256": sha256_file(repo_root / "scripts/assignment/generate_production_candidate_inventories.py")},
        "live_plan_renderer": {"path": "scripts/assignment/render_production_live_plan_v2.py", "sha256": sha256_file(repo_root / "scripts/assignment/render_production_live_plan_v2.py")},
        "production_plan_finalizer": {"path": "scripts/assignment/finalize_production_plan.py", "sha256": sha256_file(repo_root / "scripts/assignment/finalize_production_plan.py")},
        "final_run_design": dict(source_documents["final_run_design"]),
        "methodology_and_gates": dict(source_documents["methodology_and_gates"]),
        "authority_pdf": dict(source_documents["authority_pdf"]),
    }
    result = {
        "schema_version": "assignment-production-execution-workflow.v2",
        "snapshot_id": SNAPSHOT_ID,
        "plan_id": PLAN_ID,
        "namespace": PRODUCTION_NAMESPACE,
        "status": "offline_ready_selection_and_live_proof_pending",
        "launch_authorized": False,
        "operator_guidance": {
            "current_readme": dict(guidance_refs["readme"]),
            "current_recovery": dict(guidance_refs["recovery"]),
            "legacy_recovery": {
                "path": "recovery_resume.md",
                "status": "legacy_superseded",
                "generated_by": "scripts/assignment/generate_instrumentation_plan.py",
            },
        },
        "production": {
            "full_case_count": 1088,
            "step1_case_count": 800,
            "step2_independent_case_count": 288,
            "candidate_selection": "pending_Astra confirmation panel comparison",
            "fresh_case_id_prefix": "assignment-production-v2:",
            "candidate_inventories": inventories,
            "no_final_outcome_tuning": True,
        },
        "configuration_confirmation": {
            "case_count": 96,
            "plan": dict(confirmation_ref),
            "final_candidate_selection": "pending",
            "not_production_cases": True,
        },
        "overhead": {"plan": dict(overhead_ref), "fixture_count": 4, "pair_count": 12, "condition_pass_count": 24, "not_additional_production_cases": True},
        "recovery": {
            "case_identity": "case_id and resume_key are fresh and equal; historical_template_case_id is lineage only",
            "attempt_identity": "attempt-001 then attempt-002 only; each retry has a new attempt_id and request IDs",
            "infrastructure_attempts": {"maximum": 2, "initial_plus_retry": True, "backoff_seconds": 60, "retry_scope": "infrastructure failures only"},
            "evaluator_attempts": {"maximum": 2, "backoff_seconds": 30, "regenerate_patch": False, "retry_scope": "evaluator infrastructure failure only"},
            "normal_unresolved_or_agent_limit": "never retry for score or quality; preserve explicit terminal record",
            "resume_cursor": "advance only after durable case result, raw evidence export, evaluator binding and SHA-256 sidecars are fsynced",
            "corrupt_or_duplicate_completion": "quarantine and halt next assignment; never overwrite or accept by count",
            "interruption": "preserve active attempt, export durable incomplete record, resume exact case with new attempt identity",
        },
        "disk": {
            "continuous_durable_export": True,
            "pre_pilot_free_bytes_threshold": "max(100 GiB, 20% of filesystem capacity)",
            "full_start_free_bytes_threshold": "max(100 GiB, 2*1088*max measured pilot durable bytes per case + max observed live container/image/workspace footprint)",
            "stop_assigning_threshold": "max(50 GiB, 10% of filesystem capacity)",
            "threshold_action": "stop assigning the next case and preserve active case; export metadata and raw evidence",
            "estimates_vs_measured": "record both; cannot claim disk ready from estimate alone",
            "cleanup_policy": "no deletion/prune without evidence classification and separate authorization; preserve historical snapshots, frozen v3, original 800, worktrees, histories and required images",
        },
        "retry_and_proxy": {
            "client_http_retry": "use the pinned SWE-agent/LiteLLM settings after remote verification; report exact attempt limit/backoff; no extra proxy retry",
            "proxy": "one physical request per logical client request; preserve raw pre-dispatch body, response/error, request ID and retry_of",
            "request_duplication_gate": "raw_model_request_records must equal physical_requests; duplicate physical dispatch is an evidence failure",
        },
        "source_hash_workflow": {
            "hash_algorithm": "SHA-256 exact bytes",
            "sidecar_format": "<digest>  <filename>\\n",
            "before_live": "hash source, pins, configs, candidate inventory, confirmation/overhead/split manifests and runtime manifest",
            "during_live": "hash raw JSONL/journals/request bodies/attempt outputs before advancing cursor; retain lossless compression hashes",
            "after_integration": "root computes final source bundle hash after all agent integrations; replace pending source hash bindings before launch",
            "sensitive_raw_data": "do not bundle full container environments or command-line credentials; retain redacted evidence and flag any raw file needing sanitization",
        },
        "hardware_and_serving_preflight": {
            "capture_before_run": [
                "CPU model/capability/cache/topology/frequency assumptions",
                "kernel/container/filesystem/mount backing and RAM",
                "GPU exact SKU/UUID/VRAM/clocks/power limits/driver/CUDA",
                "model config/tokenizer/weight shard metadata/file byte sizes/precision",
                "vLLM revision/complete serving flags/KV-cache configuration",
            ],
            "capture_start_and_end": True,
            "per_event_expensive_shell_queries": False,
            "unavailable_fields": "explicit unavailable with source/reason; never infer bandwidth from GPU name",
            "serving_metrics": "native per-request values preferred; otherwise metric deltas require exclusive lease/access witness, same counter epoch/server identity and finite nonnegative sums; otherwise unavailable",
        },
        "capture_gate": {
            "individual_cpu_operation_records": "positive where operations are exercised",
            "raw_model_request_records_equals_physical_requests": True,
            "dropped_cpu_records": 0,
            "cpu_capture_map_failures": 0,
            "missing_raw_request_bodies": 0,
            "full_production_capture_enabled_on_overhead_pairs": True,
            "successful_case_outer_e2e_union_attribution_fraction": ">=0.95 per case; UNKNOWN excluded from numerator",
            "unknown_residual_fraction": "<=0.05 per successful case",
        },
        "source_hashes": source_hashes,
        "outcome_accessed": False,
    }
    digest = _write_json(live_plan / "production_execution_workflow.v2.json", result)
    return result, {"path": "production_execution_workflow.v2.json", "sha256": digest}


def _build_proof_templates(*, live_plan: Path, contract: Mapping[str, Any], contract_ref: Mapping[str, Any], source_bundle_placeholder: str) -> dict[str, dict[str, Any]]:
    proof_dir = live_plan / "proof-templates"
    pending_roles: dict[str, dict[str, Any]] = {}
    requirements = []
    for row in contract["requirements"]:
        requirements.append({
            "id": row["id"],
            "status": "pending",
            "artifact_roles": [],
            "verification": "Pending replacement with hash-bound offline/live proof; this template is not evidence.",
            "required_contract_text": row["requirement"],
        })
    acquisition = {
        "schema_version": "assignment.acquisition-proof.v2",
        "evidence_kind": "live_prelaunch",
        "status": "pending_live_evidence",
        "launch_authorized": False,
        "contract_sha256": contract_ref["sha256"],
        "source_bundle_sha256": source_bundle_placeholder,
        "requirements": requirements,
        "note": "Pending template only. Replace every row with independently reviewed hash-bound proof before launch; no requirement is claimed passed.",
    }
    acq_path = proof_dir / "acquisition_proof.pending.json"
    acq_sha = _write_json(acq_path, acquisition)
    pending_roles["acquisition_proof"] = {"path": _rel(acq_path, live_plan), "sha256": acq_sha}

    regressions = []
    for row in contract["historical_regressions"]:
        regressions.append({
            "id": row["id"],
            "status": "pending",
            "artifact_roles": [],
            "verification": "Pending replacement with hash-bound regression evidence; this template is not evidence.",
            "disposition": "pending_review",
            "remaining_limitation": None,
            "previous_failure": row["previous_failure"],
        })
    regression = {
        "schema_version": "assignment.historical-regression-proof.v2",
        "evidence_kind": "live_prelaunch",
        "status": "pending_live_evidence",
        "launch_authorized": False,
        "contract_sha256": contract_ref["sha256"],
        "source_bundle_sha256": source_bundle_placeholder,
        "regressions": regressions,
        "note": "Pending template only. Required acquisition/overhead failures cannot be waived; only R15/R16 may later carry explicit validity limitations.",
    }
    reg_path = proof_dir / "historical_regression_proof.pending.json"
    reg_sha = _write_json(reg_path, regression)
    pending_roles["historical_regression_proof"] = {"path": _rel(reg_path, live_plan), "sha256": reg_sha}

    pilot_ids = [row.get("pilot_case_id") for row in _read_json(live_plan / "pilot_cases.json")["cases"]]
    pilot_evidence = {
        "schema_version": "assignment.instrumentation-pilot-evidence.v2",
        "evidence_kind": "pending_live",
        "status": "pending_live_evidence",
        "launch_authorized": False,
        "selected_case_ids": pilot_ids,
        "baseline": None,
        "frozen_pilot_configuration": None,
        "case_summaries": [],
        "replay_case_ids": [
            "cpu-file-traversal-v1", "cpu-test-script-subprocess-v1",
            "model-short-request-v1", "model-long-context-request-v1",
        ],
        "overhead_pairs": [],
        "review": {name: False for name in PILOT_REVIEW_FIELDS},
        "artifact_bindings": [],
        "note": "Pending template only; synthetic/offline records cannot satisfy the live gate.",
    }
    pilot_path = proof_dir / "pilot_evidence.pending.json"
    pilot_sha = _write_json(pilot_path, pilot_evidence)
    pending_roles["pilot_evidence_template"] = {"path": _rel(pilot_path, live_plan), "sha256": pilot_sha}

    pending_payloads = {
        "event_journals": {"schema_version": "assignment.telemetry.v2.pending", "status": "pending_live", "individual_cpu_operation_records": None, "raw_model_request_records": None, "physical_requests": None, "dropped_cpu_records": None, "cpu_capture_map_failures": None, "missing_raw_request_bodies": None},
        "source_bundle": {"schema_version": "assignment.source-bundle.v2.pending", "status": "pending_root_final_hash", "source_bundle_sha256": source_bundle_placeholder},
        "remote_reconciliation": {"schema_version": "assignment.remote-reconciliation.v2.pending", "status": "pending_remote_access", "remote_verification": "unresolved_offline"},
        "feature_parity": {"schema_version": "assignment.feature-parity.v2.pending", "status": "pending_live_review", "train_serve_parity": None},
        "raw_cpu_record_inventory": {"schema_version": "assignment.raw-cpu-record-inventory.v2.pending", "status": "pending_live", "individual_cpu_operation_records": None, "dropped_cpu_records": None, "cpu_capture_map_failures": None},
        "raw_model_record_inventory": {"schema_version": "assignment.raw-model-record-inventory.v2.pending", "status": "pending_live", "raw_model_request_records": None, "physical_requests": None, "missing_raw_request_bodies": None},
        "offline_test_report": {"schema_version": "assignment.offline-test-report.v2.pending", "status": "pending_root_test_seal", "tests": [], "all_required_gate_tests_passed": None},
    }
    for role, payload in pending_payloads.items():
        path = proof_dir / f"{role}.pending.json"
        digest = _write_json(path, payload)
        pending_roles[role] = {"path": _rel(path, live_plan), "sha256": digest}
    return pending_roles


def _build_role_manifest(*, live_plan: Path, candidate_manifest: Mapping[str, Any], contract_ref: Mapping[str, Any], confirmation_ref: Mapping[str, Any], overhead_ref: Mapping[str, Any], workflow_ref: Mapping[str, Any], proof_refs: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {
        "pilot_inventory": {"path": "pilot_cases.json", "sha256": sha256_file(live_plan / "pilot_cases.json")},
        "event_journals": dict(proof_refs["event_journals"]),
        "overhead_replay": dict(overhead_ref),
        "source_bundle": dict(proof_refs["source_bundle"]),
        "remote_reconciliation": dict(proof_refs["remote_reconciliation"]),
        "feature_parity": dict(proof_refs["feature_parity"]),
        "run_manifest": {"path": "production_run_manifest.v2.json", "sha256": "PENDING_RUN_MANIFEST_HASH"},
        "acquisition_contract": dict(contract_ref),
        "acquisition_proof": dict(proof_refs["acquisition_proof"]),
        "historical_regression_proof": dict(proof_refs["historical_regression_proof"]),
        "raw_cpu_record_inventory": dict(proof_refs["raw_cpu_record_inventory"]),
        "raw_model_record_inventory": dict(proof_refs["raw_model_record_inventory"]),
        "offline_test_report": dict(proof_refs["offline_test_report"]),
        "full_matrix_inventory": {"path": "production_candidate_inventory_manifest.json", "sha256": sha256_file(live_plan / "production_candidate_inventory_manifest.json")},
    }
    result = {
        "schema_version": "assignment.production-proof-role-manifest.v2",
        "status": "pending_live_proof_replacement",
        "required_roles": list(REQUIRED_ARTIFACT_ROLES),
        "roles": roles,
        "launch_authorized": False,
        "note": "Pending role bindings include plan/template hashes; root must replace pending proof payloads and source hash before launch.",
    }
    digest = _write_json(live_plan / "production_proof_role_manifest.v2.json", result)
    return result, {"path": "production_proof_role_manifest.v2.json", "sha256": digest}


def _build_run_manifest(*, live_plan: Path, candidate_manifest: Mapping[str, Any], confirmation_ref: Mapping[str, Any], overhead_ref: Mapping[str, Any], split_ref: Mapping[str, Any], workflow_ref: Mapping[str, Any], guidance_refs: Mapping[str, Mapping[str, Any]], contract_ref: Mapping[str, Any], role_ref: Mapping[str, Any], source_documents: Mapping[str, Any], repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    result = {
        "schema_version": "assignment-production-run-manifest.v2",
        "snapshot_id": SNAPSHOT_ID,
        "plan_id": PLAN_ID,
        "namespace": PRODUCTION_NAMESPACE,
        "status": "offline_ready_selection_and_live_proof_pending",
        "launch_authorized": False,
        "operator_guidance": {
            "current_readme": dict(guidance_refs["readme"]),
            "current_recovery": dict(guidance_refs["recovery"]),
            "legacy_recovery": {"path": "recovery_resume.md", "status": "legacy_superseded"},
        },
        "final_configuration": None,
        "candidate_selection": {"status": "pending", "candidate_ids": [item["candidate_id"] for item in candidate_manifest["candidate_inventories"]], "owner": "Astra"},
        "matrix": {"full_case_count": 1088, "step1_case_count": 800, "step2_independent_case_count": 288, "call_limit_grid": [20, 30, 50, 100], "other_sweep_grids_unchanged": True, "fresh_case_ids": True, "historical_template_case_ids_overwritten": False},
        "candidate_inventories": candidate_manifest["candidate_inventories"],
        "validator_integration": {
            "production_plan_schema": "assignment-production-v2-plan.v1",
            "current_run_matrix_schema": "assignment-steps-1-3-plan.v1",
            "current_case_runner_schema": "assignment-steps-1-3-plan.v1",
            "status": "pending_v2_validator_integration",
            "do_not_relabel_as_historical_schema": True,
            "required_before_execution": "reviewed v2 matrix/case-runner adapters must validate seven-key configuration, fresh case IDs, source/pin hashes, and telemetry handshake",
        },
        "configuration_confirmation": {"case_count": 96, "path": confirmation_ref["path"], "sha256": confirmation_ref["sha256"], "status": "pending_live", "not_production": True},
        "pilot": {"natural_case_count": 16, "reuse_map": "pilot_reuse_mapping.v2.json", "status": "pending_live_exact_match"},
        "overhead": {"path": overhead_ref["path"], "sha256": overhead_ref["sha256"], "fixture_count": 4, "pair_count": 12, "condition_pass_count": 24, "status": "pending_live_fixture_capture"},
        "split": {"path": split_ref["path"], "sha256": split_ref["sha256"], "unit": "instance_cluster", "development_excluded_clusters": 24, "holdout_instance_id": HOLDOUT_INSTANCE_ID},
        "pins": {"model": MODEL_NAME, "model_revision": MODEL_REVISION, "tokenizer_revision": MODEL_REVISION, "swe_agent_revision": SWE_AGENT_REVISION, "swe_bench_revision": SWE_BENCH_REVISION, "vllm_version": VLLM_VERSION, "serving_configuration": {"max_model_len": SERVING_MAX_MODEL_LEN}, "remote_verification": "unresolved_offline"},
        "telemetry": dict(TELEMETRY_BINDING),
        "capture_gate": {"individual_cpu_operation_records_positive": True, "raw_model_request_records_equals_physical_requests": True, "dropped_cpu_records": 0, "cpu_capture_map_failures": 0, "missing_raw_request_bodies": 0, "full_production_capture_enabled_on_overhead_pairs": True, "successful_case_outer_e2e_attribution_fraction_min": 0.95, "unknown_residual_fraction_max": 0.05},
        "recovery_disk_retry_workflow": {"path": workflow_ref["path"], "sha256": workflow_ref["sha256"]},
        "acquisition": {"contract": contract_ref, "proof_role_manifest": role_ref, "status": "pending_live_proof"},
        "source_hash_finalization": "pending_root_after_all_integrations",
        "outcome_accessed": False,
        "historical_boundaries": {"original_800_immutable": True, "prior_snapshots_immutable": True, "frozen_d9_v3_immutable": True, "holdout_outcomes_accessed": False, "production_outcomes_used_for_selection": False},
        "source_bindings": {
            "run_manifest_generator": {"path": "scripts/assignment/render_production_live_plan_v2.py", "sha256": sha256_file(repo_root / "scripts/assignment/render_production_live_plan_v2.py")},
            "production_plan_finalizer": {"path": "scripts/assignment/finalize_production_plan.py", "sha256": sha256_file(repo_root / "scripts/assignment/finalize_production_plan.py")},
            "final_run_design": dict(source_documents["final_run_design"]),
            "methodology_and_gates": dict(source_documents["methodology_and_gates"]),
            "authority_pdf": dict(source_documents["authority_pdf"]),
        },
    }
    digest = _write_json(live_plan / "production_run_manifest.v2.json", result)
    return result, {"path": "production_run_manifest.v2.json", "sha256": digest}


def _build_artifact_manifest(*, live_plan: Path, generated_refs: Mapping[str, Mapping[str, Any]], source_documents: Mapping[str, Any], repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(live_plan.rglob("*")):
        if not path.is_file() or path.name.endswith(".sha256") or path.name == "production_live_plan_artifact_manifest.v2.json":
            continue
        entries.append({"path": _rel(path, live_plan), "sha256": sha256_file(path), "size": path.stat().st_size})
    result = {
        "schema_version": "assignment.production-live-plan-artifact-manifest.v2",
        "snapshot_id": SNAPSHOT_ID,
        "status": "offline_artifacts_pending_root_source_bundle_seal",
        "launch_authorized": False,
        "artifact_count": len(entries),
        "artifacts": entries,
        "generated_named_refs": dict(generated_refs),
        "source_scripts": {
            "production_inventory_generator": {"path": "scripts/assignment/generate_production_candidate_inventories.py", "sha256": sha256_file(repo_root / "scripts/assignment/generate_production_candidate_inventories.py")},
            "live_plan_renderer": {"path": "scripts/assignment/render_production_live_plan_v2.py", "sha256": sha256_file(repo_root / "scripts/assignment/render_production_live_plan_v2.py")},
            "production_plan_finalizer": {"path": "scripts/assignment/finalize_production_plan.py", "sha256": sha256_file(repo_root / "scripts/assignment/finalize_production_plan.py")},
        },
        "source_documents": {key: dict(value) for key, value in source_documents.items()},
        "source_bundle_finalization": "root computes final source bundle hash after agent integrations; this manifest is regenerated then",
        "outcomes_accessed": False,
    }
    digest = _write_json(live_plan / "production_live_plan_artifact_manifest.v2.json", result)
    return result, {"path": "production_live_plan_artifact_manifest.v2.json", "sha256": digest}


def render_live_plan(*, snapshot_root: Path, repo_root: Path) -> dict[str, Any]:
    live_plan = snapshot_root / "live-plan"
    panel = _read_json(snapshot_root / "configuration-analysis" / "CONFIGURATION_CONFIRMATION_PANEL.json")
    candidate_manifest, candidate_ref = _load_candidate_manifest(live_plan)
    contract_source = repo_root / "configs" / "assignment_acquisition_contract.v2.json"
    contract_path = live_plan / "assignment_acquisition_contract.v2.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(contract_path, contract_source.read_bytes())
    contract_ref = {"path": "assignment_acquisition_contract.v2.json", "sha256": sha256_file(contract_path)}
    _require(contract_ref["sha256"] == sha256_file(contract_source), "copied acquisition contract changed")
    contract = _read_json(contract_source)
    _require(contract.get("schema_version") == "assignment.acquisition-contract.v2" and contract.get("production_case_count") == 1088, "invalid current acquisition contract")
    final_design = snapshot_root / "FINAL_RUN_DESIGN.md"
    methodology = snapshot_root / "METHODOLOGY_AND_GATES.md"
    _require(final_design.is_file() and not final_design.is_symlink(), f"missing final run design: {final_design}")
    _require(methodology.is_file() and not methodology.is_symlink(), f"missing methodology and gates: {methodology}")
    source_documents = {
        "final_run_design": _source_binding(final_design, snapshot_root),
        "methodology_and_gates": _source_binding(methodology, snapshot_root),
        "authority_pdf": {
            "sha256": contract["authority_pdf_sha256"],
            "availability": "declared_by_acquisition_contract_unresolved_in_offline_workspace",
            "source": "configs/assignment_acquisition_contract.v2.json::authority_pdf_sha256",
        },
    }
    confirmation, confirmation_ref = _build_confirmation_plan(live_plan=live_plan, panel=panel, candidate_manifest=candidate_manifest, repo_root=repo_root)
    reuse, reuse_ref = _build_pilot_reuse(live_plan=live_plan, snapshot_root=snapshot_root, panel=panel, repo_root=repo_root)
    overhead, overhead_ref = _build_overhead_plan(live_plan=live_plan, pilot=_read_json(live_plan / "pilot_cases.json"))
    split, split_ref = _build_split(live_plan=live_plan, candidate_manifest=candidate_manifest, panel=panel)
    guidance_refs = _build_operator_guidance(
        live_plan=live_plan,
        snapshot_root=snapshot_root,
        repo_root=repo_root,
        candidate_manifest=candidate_manifest,
        candidate_ref=candidate_ref,
        confirmation_ref=confirmation_ref,
        overhead_ref=overhead_ref,
        split_ref=split_ref,
    )
    proof_refs = _build_proof_templates(live_plan=live_plan, contract=contract, contract_ref=contract_ref, source_bundle_placeholder="PENDING_ROOT_SOURCE_BUNDLE_FINALIZATION")
    role_manifest, role_ref = _build_role_manifest(live_plan=live_plan, candidate_manifest=candidate_manifest, contract_ref=contract_ref, confirmation_ref=confirmation_ref, overhead_ref=overhead_ref, workflow_ref={"path": "production_execution_workflow.v2.json", "sha256": "PENDING_WORKFLOW_HASH"}, proof_refs=proof_refs)
    workflow, workflow_ref = _build_workflow(live_plan=live_plan, candidate_manifest=candidate_manifest, candidate_ref=candidate_ref, confirmation_ref=confirmation_ref, overhead_ref=overhead_ref, split_ref=split_ref, contract_ref=contract_ref, guidance_refs=guidance_refs, source_documents=source_documents, repo_root=repo_root)
    # The role manifest intentionally records the workflow as a plan role only;
    # update its concrete workflow reference after the workflow is rendered.
    role_manifest["workflow"] = workflow_ref
    _write_json(live_plan / "production_proof_role_manifest.v2.json", role_manifest)
    role_ref = {"path": "production_proof_role_manifest.v2.json", "sha256": sha256_file(live_plan / "production_proof_role_manifest.v2.json")}
    run_manifest, run_ref = _build_run_manifest(live_plan=live_plan, candidate_manifest=candidate_manifest, confirmation_ref=confirmation_ref, overhead_ref=overhead_ref, split_ref=split_ref, workflow_ref=workflow_ref, guidance_refs=guidance_refs, contract_ref=contract_ref, role_ref=role_ref, source_documents=source_documents, repo_root=repo_root)
    # A role manifest and run manifest cannot contain one another's final
    # digest without a circular hash.  Keep the role manifest's run-manifest
    # digest explicitly pending; the run manifest binds the concrete role
    # manifest digest and root seals the remaining role after integration.
    _write_json(live_plan / "production_run_manifest.v2.json", run_manifest)
    run_ref = {"path": "production_run_manifest.v2.json", "sha256": sha256_file(live_plan / "production_run_manifest.v2.json")}
    generated_refs = {"candidate_inventory_manifest": candidate_ref, "confirmation_execution_plan": confirmation_ref, "pilot_reuse_map": reuse_ref, "overhead_plan": overhead_ref, "split_manifest": split_ref, "workflow": workflow_ref, "proof_role_manifest": role_ref, "run_manifest": run_ref, "acquisition_contract": contract_ref, "operator_guidance_readme": guidance_refs["readme"], "operator_guidance_recovery": guidance_refs["recovery"]}
    artifact_manifest, artifact_ref = _build_artifact_manifest(live_plan=live_plan, generated_refs=generated_refs, source_documents=source_documents, repo_root=repo_root)
    return {"artifacts": generated_refs, "artifact_manifest": artifact_ref, "artifact_count": artifact_manifest["artifact_count"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-root", required=True, type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    result = render_live_plan(snapshot_root=args.snapshot_root.resolve(), repo_root=args.repo_root.resolve())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
