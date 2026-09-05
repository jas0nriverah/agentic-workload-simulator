#!/usr/bin/env python3
"""Normalize one measured SWE-agent run into the Steps 1--3 data contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.assignment.schema import (  # noqa: E402
    AssignmentContractError,
    canonical_sha256,
    validate_model_event,
    validate_tool_event,
    validate_trajectory,
)


HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssignmentContractError(f"cannot read JSON {path}: {exc}") from exc


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise AssignmentContractError(f"{path}:{number} is not a JSON object")
            rows.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssignmentContractError(f"cannot read JSONL {path}: {exc}") from exc
    return rows


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AssignmentContractError(f"cannot hash {label} {path}: {exc}") from exc


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _verify_sidecar(path: Path, label: str) -> str:
    if not path.is_file() or path.is_symlink():
        raise AssignmentContractError(f"{label} must be a regular file: {path}")
    digest = _sha256_file(path, label)
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.is_file() or sidecar.is_symlink():
        raise AssignmentContractError(f"{label} SHA-256 sidecar is missing: {sidecar}")
    try:
        claim = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssignmentContractError(f"cannot read {label} sidecar {sidecar}: {exc}") from exc
    if claim != f"{digest}  {path.name}\n":
        raise AssignmentContractError(f"{label} or its SHA-256 sidecar was tampered with")
    return digest


def _validate_execution_provenance(
    *,
    spec: dict[str, Any],
    runtime_manifest_path: Path,
    case_result_path: Path,
    sources: dict[str, Path],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Bind normalized rows to one completed, manifest-pinned runner attempt."""

    runtime_digest = _verify_sidecar(runtime_manifest_path, "runtime manifest")
    result_digest = _verify_sidecar(case_result_path, "case result")
    manifest = _load_json(runtime_manifest_path)
    result = _load_json(case_result_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "assignment-runtime-manifest.v1":
        raise AssignmentContractError("unsupported runtime manifest schema")
    if not isinstance(result, dict) or result.get("schema_version") != "assignment-case-result.v1":
        raise AssignmentContractError("unsupported case result schema")
    if result.get("status") != "completed":
        raise AssignmentContractError("only a completed case result can authorize measured normalization")
    if result.get("manifest_sha256") != runtime_digest:
        raise AssignmentContractError("case result is not bound to the supplied runtime manifest")
    if result.get("run_id") != spec.get("run_id"):
        raise AssignmentContractError("case result run_id does not match the run specification")

    pins = manifest.get("pins")
    if not isinstance(pins, dict):
        raise AssignmentContractError("runtime manifest pins are missing")
    pin_bindings = {
        "model_revision": "model_revision",
        "swe_agent_revision": "swe_agent_revision",
        "swe_bench_revision": "swe_bench_revision",
    }
    for spec_field, pin_field in pin_bindings.items():
        if spec.get(spec_field) != pins.get(pin_field):
            raise AssignmentContractError(f"run specification {spec_field} is not manifest-pinned")
    git = result.get("git")
    if not isinstance(git, dict) or git.get("commit") != manifest.get("required_commit") or git.get("branch") != manifest.get("required_branch"):
        raise AssignmentContractError("case result Git identity is not manifest-pinned")
    runner = result.get("runner")
    if not isinstance(runner, dict) or runner.get("status") != "completed":
        raise AssignmentContractError("case result runner did not complete")
    if runner.get("command_hash") != spec.get("command_sha256"):
        raise AssignmentContractError("run specification command_sha256 does not match the reviewed runner command")
    evaluator = result.get("evaluator")
    if not isinstance(evaluator, dict) or evaluator.get("status") != "completed" or evaluator.get("submitted") is not True:
        raise AssignmentContractError("case result official evaluator did not complete")

    manifest_integrity = manifest.get("integrity")
    result_integrity = result.get("integrity")
    # The manifest integrity object contains both paths and hashes, while the
    # reviewed case result records the complete hash subset, including
    # adaptive/runtime/proxy bindings for a non-adaptive assignment run.
    # Require exact equality of those hash bindings so normalization cannot
    # silently drop one or accept a different one.
    expected_integrity = (
        {
            key: value
            for key, value in manifest_integrity.items()
            if key.endswith("_sha256")
        }
        if isinstance(manifest_integrity, dict)
        else None
    )
    if expected_integrity is None or result_integrity != expected_integrity or not all(
        isinstance(value, str) and HEX64_RE.fullmatch(value)
        for value in expected_integrity.values()
    ):
        raise AssignmentContractError("case result execution integrity is not manifest-pinned")

    root = case_result_path.resolve().parent
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list):
        raise AssignmentContractError("case result artifact inventory is missing")
    inventory: dict[Path, dict[str, Any]] = {}
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"kind", "path", "sha256", "size"}:
            raise AssignmentContractError("case result has a malformed artifact record")
        relative = item["path"]
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise AssignmentContractError("case result artifact path must be relative")
        source = (root / relative).resolve()
        if not _inside(source, root) or source in inventory:
            raise AssignmentContractError("case result artifact inventory has an unsafe or duplicate path")
        if not source.is_file() or source.is_symlink():
            raise AssignmentContractError(f"case result artifact is unavailable: {relative}")
        digest = _sha256_file(source, f"case artifact {relative}")
        if item.get("sha256") != digest or item.get("size") != source.stat().st_size:
            raise AssignmentContractError(f"case result artifact changed after execution: {relative}")
        inventory[source] = item
    source_hashes: dict[str, str] = {}
    for label, supplied in sources.items():
        source = supplied.resolve()
        if not _inside(source, root):
            raise AssignmentContractError(f"{label} must stay inside the completed case output")
        item = inventory.get(source)
        if item is None:
            raise AssignmentContractError(f"{label} is not present in the case result artifact inventory")
        source_hashes[label] = str(item["sha256"])
    normalization_sources = result.get("normalization_sources")
    if not isinstance(normalization_sources, dict) or set(normalization_sources) != set(sources):
        raise AssignmentContractError("case result normalization source map is missing or malformed")
    for label, supplied in sources.items():
        reference = normalization_sources.get(label)
        if not isinstance(reference, str) or (root / reference).resolve() != supplied.resolve():
            raise AssignmentContractError(f"supplied {label} does not match the case result normalization source")
    evaluator_reference = evaluator.get("result_path")
    if not isinstance(evaluator_reference, str) or (root / evaluator_reference).resolve() != sources["official_evaluator_result"].resolve():
        raise AssignmentContractError("supplied evaluator result is not the case result evaluator artifact")
    source_hashes["runtime_manifest"] = runtime_digest
    source_hashes["case_result"] = result_digest
    return manifest, result, source_hashes


def _official_outcome(path: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Load an outcome produced by the reviewed official-evaluator adapter.

    A caller-entered boolean is not evidence of a SWE-bench result.  The
    adapter output is accepted only while its exact report, dataset, and
    prediction inputs remain present and match their recorded hashes.
    """

    value = _load_json(path)
    if not isinstance(value, dict):
        raise AssignmentContractError("official evaluator result must be a JSON object")
    if value.get("schema_version") != "assignment-official-evaluator.v1":
        raise AssignmentContractError("unsupported official evaluator result schema")
    for field in ("official_resolved", "submitted"):
        if not isinstance(value.get(field), bool):
            raise AssignmentContractError(f"official evaluator result {field} must be boolean")
    if value["submitted"] is not True:
        raise AssignmentContractError("official evaluator result must be submitted")
    for field in ("instance_id", "run_id"):
        if value.get(field) != spec.get(field):
            raise AssignmentContractError(
                f"official evaluator result {field} does not match the run specification"
            )
    counts = value.get("counts")
    if not isinstance(counts, dict):
        raise AssignmentContractError("official evaluator result counts are missing")
    expected_counts = {
        "total_instances": 1,
        "submitted_instances": 1,
        "completed_instances": 1,
        "resolved_instances": 1 if value["official_resolved"] else 0,
        "unresolved_instances": 0 if value["official_resolved"] else 1,
        "error_instances": 0,
    }
    if any(counts.get(field) != expected for field, expected in expected_counts.items()):
        raise AssignmentContractError("official evaluator result counts are inconsistent")
    for prefix in ("report", "dataset", "predictions"):
        raw_path = value.get(f"{prefix}_path")
        recorded_hash = value.get(f"{prefix}_sha256")
        if not isinstance(raw_path, str) or not raw_path:
            raise AssignmentContractError(f"official evaluator result {prefix}_path is missing")
        if not isinstance(recorded_hash, str) or len(recorded_hash) != 64:
            raise AssignmentContractError(f"official evaluator result {prefix}_sha256 is invalid")
        source = Path(raw_path)
        if _sha256_file(source, f"official evaluator {prefix}") != recorded_hash:
            raise AssignmentContractError(
                f"official evaluator {prefix} no longer matches its recorded SHA-256"
            )
    return value


def _operation(action: str) -> tuple[str, str]:
    lowered = action.strip().lower()
    tool = lowered.split(maxsplit=1)[0] if lowered else "unknown"
    if any(token in lowered for token in ("pytest", "unittest", "tox ", "npm test", "cargo test")):
        kind = "test"
    elif any(
        token in lowered
        for token in (
            "apply_patch",
            "str_replace",
            "edit_file",
            "patch_file",
            "sed -i",
            "perl -pi",
        )
    ):
        kind = "patch"
    elif tool in {"find", "ls", "tree", "du", "list_dir", "list_files"}:
        kind = "traversal"
    elif tool in {
        "rg",
        "grep",
        "ag",
        "ack",
        "search_file",
        "search_files",
        "search_dir",
    }:
        kind = "search"
    elif (
        tool
        in {
            "cat",
            "head",
            "tail",
            "less",
            "more",
            "open_file",
            "read_file",
            "view_file",
            "scroll_up",
            "scroll_down",
        }
        or lowered.startswith("sed -n ")
        or " view " in f" {lowered} "
    ):
        kind = "read"
    elif (
        tool in {"create_file", "write_file"}
        or any(token in lowered for token in (">", "tee ", "touch ", "mkdir ", "cp ", "mv "))
    ):
        kind = "write"
    elif lowered:
        kind = "shell"
    else:
        kind = "other"
    return tool, kind


def _context(spec: dict[str, Any]) -> dict[str, Any]:
    required = (
        "run_id", "suite", "repository", "instance_id", "config_id", "repeat_id",
        "hardware_id", "model_revision", "swe_agent_revision", "swe_bench_revision",
        "command_sha256",
    )
    missing = [field for field in required if field not in spec]
    if missing:
        raise AssignmentContractError(f"run spec is missing: {', '.join(missing)}")
    return {field: spec[field] for field in required}


def normalize_tool_events(spec: dict[str, Any], trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    context = _context(spec)
    steps = trajectory.get("trajectory")
    if not isinstance(steps, list):
        raise AssignmentContractError("SWE-agent trajectory lacks a trajectory list")
    rows = []
    for ordinal, step in enumerate(steps):
        if not isinstance(step, dict):
            raise AssignmentContractError(f"trajectory step {ordinal} is not an object")
        action = step.get("action")
        seconds = step.get("execution_time")
        if not isinstance(action, str) or not action.strip():
            # SWE-agent can emit zero-duration records without a tool action:
            # ordinary model-only narration as well as the final autosubmission
            # marker. They remain in the raw trajectory and are not tool
            # invocations, so they must not become fabricated tool events or
            # block normalization of the measured action-bearing steps.
            if action == "" and seconds == 0:
                continue
            raise AssignmentContractError(f"trajectory step {ordinal} has no action")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise AssignmentContractError(f"trajectory step {ordinal} has no measured execution_time")
        tool_name, operation_class = _operation(action)
        row = {
            "schema_version": "assignment.tool-event.v1",
            **{key: context[key] for key in ("run_id", "suite", "repository", "instance_id", "config_id", "repeat_id")},
            "event_id": f"{context['run_id']}-tool-{ordinal:04d}",
            "ordinal": ordinal,
            "tool_name": tool_name,
            "operation_class": operation_class,
            "status": "completed",
            "start_mono_ns": None,
            "end_mono_ns": None,
            "wall_ms": float(seconds) * 1000.0,
            "command_bytes": len(action.encode("utf-8")),
            "cpu_ms": None,
            "bytes_read": None,
            "bytes_written": None,
            "command_sha256": _sha256_text(action),
            "timing_scope": "duration_only_from_sweagent_trajectory",
            "provenance": "measured",
        }
        rows.append(validate_tool_event(row))
    return rows


def normalize_model_events(spec: dict[str, Any], proxy_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    context = _context(spec)
    rows = []
    settings = spec.get("settings")
    declared_max_output = settings.get("max_output_tokens") if isinstance(settings, dict) else None
    accepted = [row for row in proxy_rows if row.get("path") == "/v1/chat/completions"]
    for ordinal, source in enumerate(accepted):
        status_code = source.get("status_code")
        status = "completed" if isinstance(status_code, int) and 200 <= status_code < 300 else "failed"
        start = source.get("start_mono_ns")
        end = source.get("end_mono_ns")
        wall = source.get("duration_ms")
        prompt = source.get("prompt_tokens")
        completion = source.get("completion_tokens")
        context_tokens = prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else None
        row = {
            "schema_version": "assignment.model-event.v1",
            **{key: context[key] for key in ("run_id", "suite", "repository", "instance_id", "config_id", "repeat_id")},
            "request_id": str(source.get("request_id") or f"{context['run_id']}-model-{ordinal:04d}"),
            "ordinal": ordinal,
            "status": status,
            "start_mono_ns": start,
            "end_mono_ns": end,
            "wall_ms": wall,
            "input_tokens": prompt,
            "max_output_tokens": source.get("max_output_tokens", declared_max_output),
            "output_tokens": completion,
            "context_tokens": context_tokens,
            "request_bytes": source.get("request_bytes"),
            "response_bytes": source.get("response_bytes"),
            "cpu_activity_union_ms": source.get("cpu_activity_union_ms"),
            "cuda_activity_union_ms": source.get("cuda_activity_union_ms", source.get("device_activity_union_ms")),
            "kernel_duration_sum_ms": source.get("kernel_duration_sum_ms"),
            "timing_scope": "request_proxy_monotonic_boundary",
            "provenance": "measured",
        }
        rows.append(validate_model_event(row))
    return rows


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-spec", required=True, type=Path)
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--model-events", required=True, type=Path)
    parser.add_argument("--runner-summary", required=True, type=Path)
    parser.add_argument(
        "--runtime-manifest", required=True, type=Path,
        help="manifest-pinned execution contract with an exact .sha256 sidecar",
    )
    parser.add_argument(
        "--case-result", required=True, type=Path,
        help="completed assignment-case-result.v1 with an exact .sha256 sidecar",
    )
    parser.add_argument(
        "--evaluator-result",
        required=True,
        type=Path,
        help="reviewed assignment-official-evaluator.v1 result; manual outcome flags are forbidden",
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    spec = _load_json(args.run_spec)
    trajectory = _load_json(args.trajectory)
    summary = _load_json(args.runner_summary)
    proxy_rows = _load_jsonl(args.model_events)
    if not isinstance(spec, dict) or not isinstance(trajectory, dict) or not isinstance(summary, dict):
        raise AssignmentContractError("run spec, trajectory, and summary must be JSON objects")
    if summary.get("status") != "completed":
        raise AssignmentContractError("only a completed measured run can be normalized")
    e2e = summary.get("duration_ms")
    if isinstance(e2e, bool) or not isinstance(e2e, (int, float)) or e2e <= 0:
        raise AssignmentContractError("runner summary lacks positive trajectory duration_ms")

    _manifest, case_result, source_hashes = _validate_execution_provenance(
        spec=spec,
        runtime_manifest_path=args.runtime_manifest.resolve(),
        case_result_path=args.case_result.resolve(),
        sources={
            "run_spec": args.run_spec,
            "trajectory": args.trajectory,
            "model_events": args.model_events,
            "runner_summary": args.runner_summary,
            "official_evaluator_result": args.evaluator_result,
        },
    )
    official = _official_outcome(args.evaluator_result, spec)
    tool_rows = normalize_tool_events(spec, trajectory)
    model_rows = normalize_model_events(spec, proxy_rows)
    if not tool_rows or not model_rows:
        raise AssignmentContractError("completed run requires both tool and model events")
    tool_wall = sum(float(row["wall_ms"]) for row in tool_rows if row["status"] == "completed")
    model_wall = sum(float(row["wall_ms"]) for row in model_rows if row["status"] == "completed")
    if model_wall <= 0:
        raise AssignmentContractError("completed run has no positive model-request wall time")
    context = _context(spec)
    output = args.output_dir
    tool_path = output / "tool_events.jsonl"
    model_path = output / "model_events.jsonl"
    row = {
        "schema_version": "assignment.trajectory.v1",
        **context,
        "category": str(spec.get("category") or spec["repository"]),
        "sweep_parameter": spec.get("sweep_parameter"),
        "sweep_value": spec.get("sweep_value"),
        "status": "completed",
        "submitted": official["submitted"],
        "official_resolved": official["official_resolved"],
        "e2e_wall_ms": float(e2e),
        "tool_wall_ms": tool_wall,
        "model_wall_ms": model_wall,
        "tool_model_ratio": tool_wall / model_wall,
        "tool_event_count": len(tool_rows),
        "model_event_count": len(model_rows),
        "tool_events_path": tool_path.name,
        "model_events_path": model_path.name,
        "unavailable_reason": None,
        "provenance": "measured",
    }
    row = validate_trajectory(row)
    _atomic_jsonl(tool_path, tool_rows)
    _atomic_jsonl(model_path, model_rows)
    _atomic_json(output / "trajectory.json", row)
    inventory = {
        "schema_version": "assignment.normalized-run-inventory.v1",
        "run_id": row["run_id"],
        "trajectory_sha256": canonical_sha256(row),
        "tool_events_sha256": canonical_sha256(tool_rows),
        "model_events_sha256": canonical_sha256(model_rows),
        "source_sha256": {
            "run_spec": hashlib.sha256(args.run_spec.read_bytes()).hexdigest(),
            **source_hashes,
            "official_evaluator_report": official["report_sha256"],
        },
        "execution_provenance": {
            "schema_version": case_result["schema_version"],
            "run_id": case_result["run_id"],
            "manifest_sha256": case_result["manifest_sha256"],
            "case_result_sha256": source_hashes["case_result"],
            "runner_command_sha256": case_result["runner"]["command_hash"],
            "git": case_result["git"],
            "integrity": case_result["integrity"],
        },
    }
    _atomic_json(output / "inventory.json", inventory)
    print(json.dumps(inventory, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
