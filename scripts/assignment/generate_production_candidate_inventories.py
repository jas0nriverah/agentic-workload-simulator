#!/usr/bin/env python3
"""Generate the four fresh-ID 1,088-case production candidate inventories.

The preserved 1,088-row matrix is an immutable template.  This generator
copies its task/source identity and shared-baseline lineage, then assigns new
case and resume identities under ``assignment-production-v2``.  It does not
execute a model or evaluator and it never reads trajectory outcomes.

Each candidate contains 800 baseline rows and 288 independent Step-2 rows.
The four-value call grid is ``[20, 30, 50, 100]``.  A candidate's baseline
coordinate is the shared baseline; the other three values become independent
Step-2 executions.  The same rule applies to the other three four-value
grids, so the 96 baseline coordinates remain derived plotting views rather
than additional executions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "assignment-production-v2-plan.v1"
PLAN_ID = "assignment-production-v2-20260908"
NAMESPACE = "assignment-production-v2"
HOLDOUT_INSTANCE_ID = "sympy__sympy-12481"
PRODUCTION_CASE_COUNT = 1088
STEP1_CASE_COUNT = 800
STEP2_CASE_COUNT = 288
SHARED_BASELINE_COORDINATE_COUNT = 96
FINAL_CONFIGURATION_KEYS = (
    "call_limit",
    "max_output_tokens",
    "observation_length",
    "temperature",
    "max_input_tokens",
    "top_p",
    "seed",
)
PRODUCTION_GRIDS: dict[str, tuple[int | float, ...]] = {
    "call_limit": (20, 30, 50, 100),
    "max_output_tokens": (512, 1024, 2048, 4096),
    "observation_length": (10000, 25000, 50000, 100000),
    "temperature": (0.0, 0.2, 0.5, 0.8),
}
BASELINE_FIXED_SETTINGS = {
    "max_output_tokens": 2048,
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 0,
}
CANDIDATES: tuple[dict[str, Any], ...] = (
    {
        "candidate_id": "historical-control-call30-input32768",
        "call_limit": 30,
        "max_input_tokens": 32768,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call50-input61440",
        "call_limit": 50,
        "max_input_tokens": 61440,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call100-input61440",
        "call_limit": 100,
        "max_input_tokens": 61440,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call100-input61440-observation25000",
        "call_limit": 100,
        "max_input_tokens": 61440,
        "observation_length": 25000,
    },
)
PINS = {
    "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
    "model_revision": "b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
    "tokenizer_revision": "b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
    "swe_agent_revision": "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
    "swe_bench_revision": "726c5461e2ef52d83cf1ea2107870a8bb3328d57",
    "vllm_version": "0.10.0",
}
SERVING_CONFIGURATION = {"max_model_len": 65536, "vllm_version": "0.10.0"}
TELEMETRY_BINDING = {
    "manifest_schema": "assignment.telemetry.v2.manifest",
    "schema_version": "assignment.telemetry.v2",
    "instrumentation_version": "telemetry-v2-20260908",
    "feature_schema": "assignment.d9-feature.v2",
}


class ProductionInventoryError(ValueError):
    """Raised when the immutable template cannot support a fresh inventory."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProductionInventoryError(message)


def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = path.read_bytes()
    _require(bool(raw), f"source inventory is empty: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        _require(bool(line.strip()), f"blank source inventory line {line_number}")
        value = json.loads(line)
        _require(isinstance(value, dict), f"source inventory line {line_number} is not an object")
        rows.append(value)
    _require(rows and rows[0].get("record_type") == "plan", "source inventory plan header is missing")
    cases = rows[1:]
    _require(all(row.get("record_type") == "case" for row in cases), "source inventory has a non-case record")
    return rows[0], cases, sha256_bytes(raw)


def _candidate_settings(candidate: Mapping[str, Any]) -> dict[str, Any]:
    required = {"candidate_id", "call_limit", "max_input_tokens", "observation_length"}
    _require(set(candidate) == required, f"candidate declaration has unexpected fields: {candidate}")
    settings = {
        "call_limit": candidate["call_limit"],
        "max_input_tokens": candidate["max_input_tokens"],
        "observation_length": candidate["observation_length"],
        **BASELINE_FIXED_SETTINGS,
    }
    _require(set(settings) == set(FINAL_CONFIGURATION_KEYS), "candidate settings do not cover canonical keys")
    return settings


def _production_step2_metadata(source_header: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Copy canonical task selection while replacing the call grid."""

    source_step2 = source_header.get("step_2")
    sources = source_header.get("sources")
    _require(isinstance(source_step2, Mapping), "historical template lacks step_2 metadata")
    _require(isinstance(sources, Mapping) and set(sources) == {"lite", "verified"}, "historical template lacks both suite sources")
    selected = source_step2.get("selected_task_ids")
    selection = source_step2.get("task_selection")
    _require(isinstance(selected, Mapping) and set(selected) == {"lite", "verified"}, "historical selected task metadata is malformed")
    _require(isinstance(selection, Mapping), "historical task-selection metadata is malformed")
    knobs = [
        {"name": "call_limit", "values": list(PRODUCTION_GRIDS["call_limit"])},
        {"name": "max_output_tokens", "values": list(PRODUCTION_GRIDS["max_output_tokens"])},
        {"name": "observation_length", "values": list(PRODUCTION_GRIDS["observation_length"])},
        {"name": "temperature", "values": list(PRODUCTION_GRIDS["temperature"])},
    ]
    _require(all(baseline[item["name"]] in item["values"] for item in knobs), "candidate baseline is absent from a production grid")
    return {
        "shared_baseline": "reuse_step_1_baseline",
        "task_selection": deepcopy(dict(selection)),
        "selected_task_ids": {suite: sorted(str(item) for item in selected[suite]) for suite in ("lite", "verified")},
        "knobs": knobs,
    }


def _candidate_config(source_header: Mapping[str, Any], candidate: Mapping[str, Any], source_sha256: str) -> dict[str, Any]:
    """Build the config consumed by the matrix config-binding preflight."""

    baseline = _candidate_settings(candidate)
    source_suites = source_header["sources"]
    step2 = _production_step2_metadata(source_header, baseline)
    return {
        "schema_version": "assignment-steps-1-3-plan.v1",
        "plan_id": PLAN_ID,
        "planning_only": True,
        "pins": dict(PINS),
        "execution_limits": {
            "concurrency": 1,
            "per_case_deadline_seconds": 5400,
            "global_deadline_seconds": 1209600,
        },
        "step_1": {
            "suite_order": ["lite", "verified"],
            "suites": {
                suite: {
                    "dataset": source_suites[suite]["dataset"],
                    "revision": source_suites[suite]["revision"],
                }
                for suite in ("lite", "verified")
            },
            "baseline": {key: baseline[key] for key in ("call_limit", "max_output_tokens", "observation_length", "temperature")},
        },
        "step_2": step2,
        "step_3": deepcopy(dict(source_header.get("step_3_selection_policy", {"emit_execution_rows": False, "selection_after": "completed_and_audited_step_1", "selection_count": 1}))),
        "production_configuration": {
            "candidate_id": candidate["candidate_id"],
            "final_configuration": dict(baseline),
            "serving_configuration": dict(SERVING_CONFIGURATION),
            "telemetry": dict(TELEMETRY_BINDING),
            "source_template_sha256": source_sha256,
        },
    }


def candidate_settings(candidate_id: str) -> dict[str, Any]:
    """Return a defensive copy of the declared final settings for a candidate."""

    matches = [candidate for candidate in CANDIDATES if candidate["candidate_id"] == candidate_id]
    _require(len(matches) == 1, f"unknown candidate: {candidate_id}")
    return _candidate_settings(matches[0])


def _parse_cell(row: Mapping[str, Any]) -> tuple[str, int | float]:
    variation = row.get("variation")
    _require(isinstance(variation, Mapping), f"sweep row has no variation: {row.get('resume_key')}")
    knob = variation.get("knob")
    value = variation.get("value")
    _require(knob in PRODUCTION_GRIDS, f"unsupported sweep knob: {knob}")
    _require(value in (10, 20, 30, 50, 512, 1024, 4096, 10000, 25000, 50000, 0.2, 0.5, 0.8),
             f"unexpected historical sweep value: {value!r}")
    return str(knob), value


def _target_sweep_values(knob: str, baseline_value: int | float) -> tuple[int | float, ...]:
    values = PRODUCTION_GRIDS[knob]
    _require(baseline_value in values, f"candidate baseline {knob}={baseline_value!r} is outside its grid")
    result = tuple(value for value in values if value != baseline_value)
    _require(len(result) == 3, f"{knob} must have exactly three nonbaseline values")
    return result


def _fresh_case_id(*, candidate_id: str, old_row: Mapping[str, Any], cell_id: str) -> str:
    old_key = str(old_row.get("resume_key", ""))
    identity = {
        "candidate_id": candidate_id,
        "historical_template_case_id": old_key,
        "historical_template_plan_id": old_row.get("plan_id"),
        "cell_id": cell_id,
        "instance_id": old_row.get("instance_id"),
        "suite": old_row.get("suite"),
    }
    return f"{NAMESPACE}:{sha256_bytes(canonical_json(identity).encode('utf-8'))}"


def _fresh_row(
    *,
    old_row: Mapping[str, Any],
    candidate: Mapping[str, Any],
    baseline: Mapping[str, Any],
    target_value: int | float | None,
    knob: str | None,
) -> dict[str, Any]:
    candidate_id = str(candidate["candidate_id"])
    old_key = str(old_row.get("resume_key", ""))
    _require(old_key.startswith("assignment-case-v1:"), f"historical template row has invalid resume_key: {old_key}")
    if knob is None:
        cell_id = "shared-baseline"
        settings = dict(baseline)
        variation = None
        roles = list(old_row.get("roles", []))
        steps = list(old_row.get("steps", []))
    else:
        _require(target_value is not None, "sweep target value is missing")
        settings = dict(baseline)
        settings[knob] = target_value
        cell_id = f"{knob}={canonical_json(target_value)}"
        variation = {
            "knob": knob,
            "value": target_value,
            "historical_cell_id": old_row.get("cell_id"),
            "historical_value": old_row.get("variation", {}).get("value"),
        }
        roles = ["step_2_sweep"]
        steps = [2]
    case_id = _fresh_case_id(candidate_id=candidate_id, old_row=old_row, cell_id=cell_id)
    result = {
        "record_type": "case",
        "schema_version": SCHEMA_VERSION,
        "plan_id": PLAN_ID,
        "namespace": NAMESPACE,
        "candidate_id": candidate_id,
        "case_id": case_id,
        "resume_key": case_id,
        "historical_template_case_id": old_key,
        "historical_template_plan_id": old_row.get("plan_id"),
        "fresh_case_id": True,
        "production_case": True,
        "confirmation_case": False,
        "holdout_instance_id": HOLDOUT_INSTANCE_ID,
        "selection_outcome_blind": True,
        "suite": old_row.get("suite"),
        "repository": old_row.get("repository"),
        "instance_id": old_row.get("instance_id"),
        "task_sha256": old_row.get("task_sha256"),
        "source_manifest_sha256": old_row.get("source_manifest_sha256"),
        "cell_id": cell_id,
        "steps": steps,
        "roles": roles,
        "settings": settings,
        "final_configuration": dict(settings),
        "serving_configuration": dict(SERVING_CONFIGURATION),
        "variation": variation,
        "concurrency": old_row.get("concurrency"),
        "per_case_deadline_seconds": old_row.get("per_case_deadline_seconds"),
    }
    _require(set(result["settings"]) == set(FINAL_CONFIGURATION_KEYS), f"{case_id}: canonical setting keys mismatch")
    return result


def _validate_template(header: Mapping[str, Any], cases: Sequence[Mapping[str, Any]]) -> None:
    _require(len(cases) == PRODUCTION_CASE_COUNT, f"template case count is {len(cases)}, expected {PRODUCTION_CASE_COUNT}")
    baseline_rows = [row for row in cases if row.get("cell_id") == "shared-baseline"]
    sweep_rows = [row for row in cases if row.get("cell_id") != "shared-baseline"]
    _require(len(baseline_rows) == STEP1_CASE_COUNT, "template must have exactly 800 shared-baseline rows")
    _require(len(sweep_rows) == STEP2_CASE_COUNT, "template must have exactly 288 independent Step-2 rows")
    _require(header.get("execution_case_count") == PRODUCTION_CASE_COUNT, "template header cardinality mismatch")
    _require(len({row.get("resume_key") for row in cases}) == PRODUCTION_CASE_COUNT, "template resume keys are not unique")
    by_knob: dict[str, list[Mapping[str, Any]]] = {knob: [] for knob in PRODUCTION_GRIDS}
    for row in sweep_rows:
        knob, _value = _parse_cell(row)
        by_knob[knob].append(row)
    for knob, rows in by_knob.items():
        _require(len(rows) == 72, f"template has {len(rows)} {knob} rows, expected 72")
        counts: dict[str, int] = {}
        for row in rows:
            _knob, value = _parse_cell(row)
            counts[str(value)] = counts.get(str(value), 0) + 1
        _require(set(counts.values()) == {24}, f"template {knob} rows are not 24 per historical value: {counts}")


def build_candidate_inventory(
    *,
    source_header: Mapping[str, Any],
    source_cases: Sequence[Mapping[str, Any]],
    candidate: Mapping[str, Any],
    source_sha256: str,
    config_sha256: str = "0" * 64,
) -> list[dict[str, Any]]:
    """Build one header plus 1,088 fresh case records."""

    _validate_template(source_header, source_cases)
    baseline = _candidate_settings(candidate)
    candidate_id = str(candidate["candidate_id"])
    target_by_knob: dict[str, tuple[int | float, ...]] = {
        knob: _target_sweep_values(knob, baseline[knob])
        for knob in PRODUCTION_GRIDS
    }
    historical_by_knob: dict[str, list[int | float]] = {knob: [] for knob in PRODUCTION_GRIDS}
    for row in source_cases:
        if row.get("cell_id") == "shared-baseline":
            continue
        knob, value = _parse_cell(row)
        historical_by_knob[knob].append(value)
    historical_values: dict[str, tuple[int | float, ...]] = {}
    for knob, values in historical_by_knob.items():
        unique = tuple(sorted(set(values), key=lambda item: (float(item), str(item))))
        _require(len(unique) == 3, f"historical {knob} sweep must have three values: {unique}")
        historical_values[knob] = unique

    rows: list[dict[str, Any]] = []
    for old_row in source_cases:
        if old_row.get("cell_id") == "shared-baseline":
            rows.append(_fresh_row(old_row=old_row, candidate=candidate, baseline=baseline, target_value=None, knob=None))
            continue
        knob, historical_value = _parse_cell(old_row)
        source_index = historical_values[knob].index(historical_value)
        target_value = target_by_knob[knob][source_index]
        rows.append(_fresh_row(old_row=old_row, candidate=candidate, baseline=baseline, target_value=target_value, knob=knob))
    _require(len(rows) == PRODUCTION_CASE_COUNT, "fresh inventory cardinality mismatch")
    _require(len({row["case_id"] for row in rows}) == PRODUCTION_CASE_COUNT, "fresh case IDs are not unique")
    _require(not any(row["case_id"].startswith("assignment-case-v1:") for row in rows), "old case ID leaked into fresh IDs")
    _require(sum(row["cell_id"] == "shared-baseline" for row in rows) == STEP1_CASE_COUNT, "fresh baseline count mismatch")
    _require(sum(row["cell_id"] != "shared-baseline" for row in rows) == STEP2_CASE_COUNT, "fresh Step-2 count mismatch")
    header = {
        "record_type": "plan",
        "schema_version": SCHEMA_VERSION,
        "plan_id": PLAN_ID,
        "namespace": NAMESPACE,
        "candidate_id": candidate_id,
        "planning_only": True,
        "config_sha256": config_sha256,
        "sources": deepcopy(dict(source_header["sources"])),
        "step_2": _production_step2_metadata(source_header, baseline),
        "status": "candidate_pending_selection",
        "final_configuration_status": "candidate_not_selected",
        "validator_integration": {
            "production_schema": SCHEMA_VERSION,
            "current_run_matrix_schema": "assignment-steps-1-3-plan.v1",
            "current_case_runner_schema": "assignment-steps-1-3-plan.v1",
            "status": "pending_v2_validator_integration",
            "do_not_relabel_as_historical_schema": True,
            "required_before_execution": "reviewed run_matrix/case-runner v2 adapters must validate canonical seven-key settings and fresh production identity",
        },
        "candidate_final_configuration": dict(baseline),
        "serving_configuration": dict(SERVING_CONFIGURATION),
        "pins": dict(PINS),
        "telemetry": dict(TELEMETRY_BINDING),
        "source_template": {
            "path": "live-plan/full_matrix_case_inventory.jsonl",
            "sha256": source_sha256,
            "case_count": PRODUCTION_CASE_COUNT,
            "historical_ids_preserved_as_lineage": True,
        },
        "matrix": {
            "step1_case_count": STEP1_CASE_COUNT,
            "step2_independent_case_count": STEP2_CASE_COUNT,
            "shared_baseline_coordinate_count": SHARED_BASELINE_COORDINATE_COUNT,
            "full_case_count": PRODUCTION_CASE_COUNT,
            "call_limit_grid": list(PRODUCTION_GRIDS["call_limit"]),
            "other_sweep_grids": {key: list(value) for key, value in PRODUCTION_GRIDS.items() if key != "call_limit"},
            "step2_values_are_nonbaseline_only": True,
        },
        "fresh_identity": {
            "case_id_prefix": f"{NAMESPACE}:",
            "resume_key_equals_case_id": True,
            "old_completion_state_reused": False,
            "historical_template_case_id_field": "historical_template_case_id",
        },
        "holdout": {
            "instance_id": HOLDOUT_INSTANCE_ID,
            "outcome_accessed": False,
            "split_assignment": "bound by separate production_split_manifest.v2.json",
        },
        "execution_case_count": PRODUCTION_CASE_COUNT,
        "concurrency": 1,
        "per_case_deadline_seconds": 5400,
        "global_deadline_seconds": 1209600,
    }
    return [header, *rows]


def render_jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return ("\n".join(canonical_json(row) for row in rows) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
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


def write_inventory(rows: Sequence[Mapping[str, Any]], path: Path, *, force: bool = False) -> str:
    if not force:
        _require(not path.exists() and not Path(f"{path}.sha256").exists(), f"refusing to overwrite {path}")
    payload = render_jsonl(rows)
    digest = sha256_bytes(payload)
    _atomic_write(path, payload)
    _atomic_write(Path(f"{path}.sha256"), f"{digest}  {path.name}\n".encode("ascii"))
    return digest


def generate_all(*, source_inventory: Path, output_dir: Path, force: bool = False) -> dict[str, Any]:
    source_header, source_cases, source_sha256 = _read_jsonl(source_inventory)
    output_dir.mkdir(parents=True, exist_ok=True)
    inventories: list[dict[str, Any]] = []
    for candidate in CANDIDATES:
        candidate_id = str(candidate["candidate_id"])
        config_payload = (canonical_json(_candidate_config(source_header, candidate, source_sha256)) + "\n").encode("utf-8")
        config_name = f"{candidate_id}.config.json"
        config_path = output_dir / config_name
        config_sha256 = sha256_bytes(config_payload)
        _atomic_write(config_path, config_payload)
        _atomic_write(Path(f"{config_path}.sha256"), f"{config_sha256}  {config_path.name}\n".encode("ascii"))
        rows = build_candidate_inventory(
            source_header=source_header,
            source_cases=source_cases,
            candidate=candidate,
            source_sha256=source_sha256,
            config_sha256=config_sha256,
        )
        filename = f"{candidate['candidate_id']}.jsonl"
        digest = write_inventory(rows, output_dir / filename, force=force)
        inventories.append({
            "candidate_id": candidate["candidate_id"],
            "path": f"production-candidates/{filename}",
            "sha256": digest,
            "case_count": len(rows) - 1,
            "settings": _candidate_settings(candidate),
            "config_path": f"production-candidates/{config_name}",
            "config_sha256": config_sha256,
        })
    manifest = {
        "schema_version": "assignment-production-v2-candidate-inventory-manifest.v1",
        "status": "candidate_inventories_pending_selection",
        "plan_id": PLAN_ID,
        "namespace": NAMESPACE,
        "source_template": {
            "path": "live-plan/full_matrix_case_inventory.jsonl",
            "sha256": source_sha256,
            "case_count": PRODUCTION_CASE_COUNT,
        },
        "candidate_count": len(inventories),
        "candidate_inventories": inventories,
        "all_case_ids_fresh": True,
        "final_candidate_selection": "pending_Astra_live_confirmation",
        "outcomes_accessed": False,
    }
    manifest_path = output_dir.parent / "production_candidate_inventory_manifest.json"
    manifest_sha = sha256_bytes((canonical_json(manifest) + "\n").encode("utf-8"))
    if not force:
        _require(not manifest_path.exists() and not Path(f"{manifest_path}.sha256").exists(), f"refusing to overwrite {manifest_path}")
    _atomic_write(manifest_path, (canonical_json(manifest) + "\n").encode("utf-8"))
    _atomic_write(Path(f"{manifest_path}.sha256"), f"{manifest_sha}  {manifest_path.name}\n".encode("ascii"))
    return {"manifest_sha256": manifest_sha, "candidate_inventories": inventories}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-inventory", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    result = generate_all(source_inventory=args.source_inventory, output_dir=args.output_dir, force=args.force)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
