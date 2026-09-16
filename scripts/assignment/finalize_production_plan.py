#!/usr/bin/env python3
"""Freeze one reviewed production candidate into an executable v2 plan.

Candidate inventories are deliberately only planning inputs.  This command
requires an explicit, hash-bound candidate selection and successful reviewed
proofs before it changes the plan header to ``frozen_for_execution``.  It
never chooses a candidate from outcome data and never runs a case, evaluator,
model, container, or remote command.

The proof files are JSON objects and every binding is checked against the
bytes on disk.  ``--allow-test-fixtures`` exists only for unit tests; it
accepts objects carrying an explicit ``test_only`` marker (or the
``offline_test_fixture`` evidence kind) and still writes into the caller's
chosen output path.  A real freeze must omit that option.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assignment import run_matrix  # noqa: E402
from scripts.validation import check_instrumentation_pilot  # noqa: E402


SCHEMA_VERSION = "assignment-production-v2-freeze-receipt.v1"
SELECTION_SCHEMA = "assignment-production-candidate-selection.v1"
PRODUCTION_SCHEMA = run_matrix.PRODUCTION_SCHEMA_VERSION
PLAN_ID = run_matrix.PRODUCTION_PLAN_ID
PRODUCTION_CASE_COUNT = run_matrix.PRODUCTION_CASE_COUNT
FINAL_SETTINGS = run_matrix.PRODUCTION_SETTINGS
BOUND_FIELDS = run_matrix.PRODUCTION_EXECUTION_BINDING_FIELDS
BOUND_SCHEMA = run_matrix.PRODUCTION_EXECUTION_BINDING_SCHEMA
PRODUCTION_EXECUTION_BINDING_FIELD = run_matrix.PRODUCTION_EXECUTION_BINDING_FIELD
HEX64 = set("0123456789abcdef")
# Gate artifacts use the concrete v2 `pass` status.  Keep the historical
# spellings so older reviewed proof records remain readable.
SUCCESS_STATUSES = {"pass", "passed", "verified", "complete"}
REVIEW_FIELDS = check_instrumentation_pilot.ACQUISITION_REVIEW_FIELDS


class FinalizationError(ValueError):
    """Raised when a candidate or required proof cannot be frozen safely."""


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise FinalizationError(message)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FinalizationError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def _regular(path_value: Path, *, label: str) -> Path:
    path = path_value.expanduser().resolve()
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    return path


def _digest(path: Path, *, label: str) -> str:
    path = _regular(path, label=label)
    return _sha256_file(path)


def _read_json(path: Path, *, label: str) -> tuple[Path, str, dict[str, Any]]:
    path = _regular(path, label=label)
    digest = _sha256_file(path)
    try:
        value = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"{label} is not one valid UTF-8 JSON object: {exc}") from exc
    _fail(isinstance(value, dict), f"{label} must contain a JSON object")
    return path, digest, value


def _bound(path: Path, *, label: str) -> dict[str, str]:
    path = _regular(path, label=label)
    return {"path": str(path), "sha256": _sha256_file(path)}


def _require_digest(value: Any, *, label: str) -> str:
    _fail(isinstance(value, str) and len(value) == 64 and set(value) <= HEX64 and set(value) != {"0"}, f"{label} must be a non-zero lowercase SHA-256")
    return value


def _test_only(value: Mapping[str, Any]) -> bool:
    return value.get("test_only") is True or value.get("evidence_kind") in {"offline_test_fixture", "synthetic_test"}


def _reject_outcome_access(value: Mapping[str, Any], *, label: str, require_explicit: bool = False) -> None:
    """Reject any explicit claim that final/production outcomes were read."""

    seen = False
    for key in (
        "outcome_accessed",
        "outcomes_accessed",
        "production_outcomes_accessed",
        "final_run_outcomes_accessed",
        "production_outcomes_used",
        "final_outcomes_used",
    ):
        if key in value:
            seen = True
            _fail(value[key] is False, f"{label} records outcome access through {key}")
    if require_explicit:
        _fail(seen, f"{label} must explicitly state that production outcomes were not accessed")


def _validate_selection(
    path: Path,
    *,
    candidate_header: Mapping[str, Any],
    candidate_digest: str,
    config_digest: str,
    allow_test_fixtures: bool,
) -> tuple[Path, str, dict[str, Any]]:
    selection_path, digest, selection = _read_json(path, label="candidate selection record")
    if _test_only(selection):
        _fail(allow_test_fixtures, "test-only candidate selection requires --allow-test-fixtures")
    _fail(set(selection) >= {"schema_version", "status", "plan_id", "candidate_id", "selected_settings", "candidate_inventory_sha256", "candidate_config_sha256"}, "candidate selection record is missing required bindings")
    _fail(selection.get("schema_version") == SELECTION_SCHEMA, "candidate selection record has an unsupported schema")
    _fail(selection.get("status") == "selected", "candidate selection record is not selected")
    _fail(selection.get("plan_id") == candidate_header.get("plan_id") == PLAN_ID, "candidate selection plan_id does not match the production candidate")
    _fail(selection.get("candidate_id") == candidate_header.get("candidate_id"), "candidate selection names a different candidate")
    _fail(selection.get("selected_settings") == candidate_header.get("candidate_final_configuration"), "candidate selection settings do not match the inventory")
    _fail(selection.get("candidate_inventory_sha256") == candidate_digest, "candidate selection inventory hash does not match the input")
    _fail(selection.get("candidate_config_sha256") == config_digest, "candidate selection config hash does not match the input")
    _reject_outcome_access(selection, label="candidate selection record", require_explicit=True)
    return selection_path, digest, selection


def _validate_candidate_config(
    *,
    header: Mapping[str, Any],
    cases: list[dict[str, Any]],
    config_path: Path,
    config_digest: str,
) -> dict[str, Any]:
    config_path, actual, config = _read_json(config_path, label="candidate config")
    _fail(actual == config_digest, "candidate config digest changed while finalizing")
    _fail(config.get("schema_version") == run_matrix.SCHEMA_VERSION and config.get("planning_only") is True, "candidate config is not a planning configuration")
    _fail(config.get("plan_id") == header.get("plan_id") == PLAN_ID, "candidate config plan_id does not match the inventory")
    production = config.get("production_configuration")
    _fail(isinstance(production, dict), "candidate config lacks production_configuration")
    _fail(production.get("candidate_id") == header.get("candidate_id"), "candidate config candidate_id does not match the inventory")
    _fail(production.get("final_configuration") == header.get("candidate_final_configuration"), "candidate config final settings do not match the inventory")
    # Reuse the scheduler's cardinality/config binding.  This does not run a
    # case and ensures the finalizer and scheduler apply the same contract.
    run_matrix._validate_config_binding(header, cases, config_path, config_digest)
    return config


def _validate_runtime_and_hardware(
    *,
    runtime_path: Path,
    hardware_path: Path,
    candidate_header: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    runtime_path, runtime_digest, runtime = _read_json(runtime_path, label="runtime manifest")
    _fail(runtime.get("schema_version") == "assignment-runtime-manifest.v1", "runtime manifest has an unsupported schema")
    hardware_path, hardware_digest, hardware = _read_json(hardware_path, label="remote hardware profile")
    _fail(isinstance(hardware.get("schema_version"), str) and hardware["schema_version"].startswith("assignment."), "remote hardware profile has no assignment schema")
    pins = candidate_header.get("pins")
    _fail(isinstance(pins, dict), "candidate pins are missing")
    runtime_pins = runtime.get("pins")
    _fail(isinstance(runtime_pins, dict), "runtime manifest has no pinned revision block")
    for field in ("model_revision", "tokenizer_revision", "swe_agent_revision", "swe_bench_revision", "vllm_version"):
        _fail(runtime_pins.get(field) == pins.get(field), f"runtime {field} does not match the candidate pin")
    model = runtime.get("model")
    _fail(isinstance(model, dict) and model.get("name") == pins.get("model") and model.get("revision") == pins.get("model_revision"), "runtime model identity does not match the candidate pin")
    return {"path": str(runtime_path), "sha256": runtime_digest}, {"path": str(hardware_path), "sha256": hardware_digest}


def _validate_contract(path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    contract_path, digest, contract = _read_json(path, label="acquisition contract")
    _fail(contract.get("schema_version") == "assignment.acquisition-contract.v2", "acquisition contract schema is unsupported")
    _fail(contract.get("production_case_count") == PRODUCTION_CASE_COUNT, "acquisition contract production cardinality is not 1088")
    _fail(isinstance(contract.get("requirements"), list) and len(contract["requirements"]) == 15, "acquisition contract requirement list is incomplete")
    _fail({row.get("id") for row in contract["requirements"] if isinstance(row, dict)} == {f"A{index:02d}" for index in range(1, 16)}, "acquisition contract requirement IDs are not A01-A15")
    return {"path": str(contract_path), "sha256": digest}, contract


def _validate_proof(
    path: Path,
    *,
    label: str,
    schema: str,
    list_key: str | None,
    expected_ids: set[str] | None,
    contract_digest: str,
    source_bundle_digest: str | None,
    allow_test_fixtures: bool,
) -> tuple[Path, str, dict[str, Any]]:
    proof_path, digest, proof = _read_json(path, label=label)
    if _test_only(proof):
        _fail(allow_test_fixtures, f"test-only {label} requires --allow-test-fixtures")
    _fail(proof.get("schema_version") == schema, f"{label} schema is unsupported")
    if _test_only(proof):
        _fail(proof.get("status") in SUCCESS_STATUSES | {"pass"}, f"{label} is not a successful test proof")
    else:
        _fail(proof.get("evidence_kind") == "live_prelaunch", f"{label} is not a live_prelaunch proof")
        _fail(proof.get("status") == "pass", f"{label} is not a passing proof")
    if "launch_authorized" in proof:
        _fail(proof.get("launch_authorized") is False, f"{label} cannot self-authorize launch")
    if list_key is not None:
        rows = proof.get(list_key)
        _fail(isinstance(rows, list) and rows, f"{label} has no {list_key}")
        ids: set[str] = set()
        for row in rows:
            _fail(isinstance(row, dict) and row.get("status") in SUCCESS_STATUSES and isinstance(row.get("artifact_roles"), list) and bool(row["artifact_roles"]), f"{label} contains an incomplete proof row")
            if "id" in row:
                ids.add(str(row["id"]))
        if expected_ids is not None:
            _fail(ids == expected_ids, f"{label} does not cover the complete required ID set")
    if "contract_sha256" in proof:
        _fail(proof["contract_sha256"] == contract_digest, f"{label} contract hash is not bound to the input contract")
    if source_bundle_digest is not None and proof.get("source_bundle_sha256") not in (None, source_bundle_digest):
        _fail(False, f"{label} source bundle hash does not match the sealed source bundle")
    return proof_path, digest, proof


def _validate_pilot(
    path: Path,
    *,
    settings: Mapping[str, Any],
    source_bundle_digest: str | None,
    allow_test_fixtures: bool,
) -> tuple[Path, str, dict[str, Any]]:
    pilot_path, digest, pilot = _read_json(path, label="pilot evidence")
    if _test_only(pilot):
        _fail(allow_test_fixtures, "test-only pilot evidence requires --allow-test-fixtures")
    _fail(pilot.get("schema_version") == "assignment.instrumentation-pilot-evidence.v2", "pilot evidence schema is unsupported")
    if _test_only(pilot):
        _fail(allow_test_fixtures, "test-only pilot evidence requires --allow-test-fixtures")
        _fail(pilot.get("status") in SUCCESS_STATUSES | {"pass"}, "pilot evidence is not a successful test proof")
    else:
        _fail(pilot.get("evidence_kind") == "live", "pilot evidence is not a live checker input")
        if "status" in pilot:
            _fail(pilot.get("status") in {"pass", "passed"}, "pilot evidence is not passing")
        if "launch_authorized" in pilot:
            _fail(pilot.get("launch_authorized") is False, "pilot evidence cannot self-authorize launch")
        try:
            check_instrumentation_pilot.verify_bindings(pilot, pilot_path.parent)
            checker_result = check_instrumentation_pilot.assess(pilot)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise FinalizationError(f"pilot checker rejected the live evidence: {exc}") from exc
        _fail(checker_result.get("status") == "pass" and checker_result.get("launch_authorized") is False, "pilot checker did not pass or attempted launch authorization")
    _fail(pilot.get("frozen_pilot_configuration") == dict(settings), "pilot evidence configuration does not match the selected candidate")
    review = pilot.get("review")
    _fail(isinstance(review, dict) and set(review) >= REVIEW_FIELDS and all(review.get(field) is True for field in REVIEW_FIELDS), "pilot evidence review is incomplete")
    selected_case_ids = pilot.get("selected_case_ids")
    _fail(isinstance(selected_case_ids, list) and bool(selected_case_ids) and all(isinstance(case, str) and case.strip() for case in selected_case_ids) and len(set(selected_case_ids)) == len(selected_case_ids), "pilot evidence needs nonempty distinct capture case IDs")
    if source_bundle_digest is not None and pilot.get("source_bundle_sha256") not in (None, source_bundle_digest):
        raise FinalizationError("pilot evidence source bundle hash does not match the sealed source bundle")
    _reject_outcome_access(pilot, label="pilot evidence")
    return pilot_path, digest, pilot


def _validate_source_bundle(path: Path, *, allow_test_fixtures: bool) -> tuple[Path, str, dict[str, Any]]:
    bundle_path, digest, bundle = _read_json(path, label="source bundle proof")
    test_only = _test_only(bundle)
    if test_only:
        _fail(allow_test_fixtures, "test-only source bundle proof requires --allow-test-fixtures")
    _fail(bundle.get("schema_version") == "assignment.offline-source-bundle.v1", "source bundle manifest schema is unsupported")
    if not test_only:
        git_head = bundle.get("git_head")
        _fail(isinstance(git_head, str) and len(git_head) == 40 and set(git_head) <= HEX64, "live source bundle must bind a 40-character commit")
    bundle_name = bundle.get("bundle")
    bundle_digest = _require_digest(bundle.get("bundle_sha256"), label="source bundle manifest bundle_sha256")
    _fail(isinstance(bundle_name, str) and bool(bundle_name) and Path(bundle_name).name == bundle_name, "source bundle manifest bundle name is malformed")
    archive = _regular(bundle_path.parent / bundle_name, label="sealed source archive")
    _fail(_sha256_file(archive) == bundle_digest, "sealed source archive does not match source manifest")
    _fail(isinstance(bundle.get("files"), list) and bool(bundle["files"]), "source bundle manifest has no file inventory")
    try:
        run_matrix._validate_source_members(archive, bundle["files"])
    except run_matrix.ExecutionError as exc:
        raise FinalizationError(str(exc)) from exc
    return bundle_path, digest, bundle


def _atomic_write(path: Path, payload: bytes, *, refuse_existing: bool = True) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if refuse_existing:
        _fail(not path.exists() and not path.is_symlink(), f"refusing to overwrite freeze output: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
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


def _write_sidecar(path: Path, digest: str) -> None:
    sidecar = Path(f"{path}.sha256")
    _atomic_write(sidecar, f"{digest}  {path.name}\n".encode("ascii"))


def _read_inventory(path: Path) -> tuple[Path, str, dict[str, Any], list[dict[str, Any]]]:
    path = _regular(path, label="candidate inventory")
    raw = path.read_bytes()
    digest = _sha256_bytes(raw)
    rows: list[dict[str, Any]] = []
    try:
        for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
            _fail(bool(line.strip()), f"candidate inventory has a blank line at {number}")
            value = json.loads(line)
            _fail(isinstance(value, dict), f"candidate inventory line {number} is not an object")
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FinalizationError(f"candidate inventory is not valid UTF-8 JSONL: {exc}") from exc
    _fail(len(rows) >= 2 and rows[0].get("record_type") == "plan", "candidate inventory lacks a plan header")
    header, cases = rows[0], rows[1:]
    _fail(header.get("status") == "candidate_pending_selection" and header.get("final_configuration_status") == "candidate_not_selected", "candidate inventory is already frozen or has an invalid selection status")
    _fail(PRODUCTION_EXECUTION_BINDING_FIELD not in header, "candidate inventory unexpectedly carries an execution binding")
    run_matrix.load_plan(path)
    return path, digest, header, cases


def finalize_production_plan(
    *,
    candidate_inventory: Path,
    candidate_config: Path,
    selection_record: Path,
    runtime_manifest: Path,
    remote_hardware_profile: Path,
    acquisition_contract: Path,
    acquisition_proof: Path,
    historical_regression_proof: Path | None = None,
    pilot_evidence: Path,
    source_bundle: Path,
    output_plan: Path,
    receipt: Path | None = None,
    allow_test_fixtures: bool = False,
) -> dict[str, Any]:
    """Validate all inputs and atomically write one frozen JSONL plan."""

    candidate_inventory_path, candidate_digest, header, cases = _read_inventory(candidate_inventory)
    candidate_config_path = _regular(candidate_config, label="candidate config")
    candidate_config_digest = _sha256_file(candidate_config_path)
    _fail(header.get("config_sha256") == candidate_config_digest, "candidate inventory config_sha256 does not match candidate config")
    _validate_candidate_config(header=header, cases=cases, config_path=candidate_config_path, config_digest=candidate_config_digest)
    _validate_selection(selection_record, candidate_header=header, candidate_digest=candidate_digest, config_digest=candidate_config_digest, allow_test_fixtures=allow_test_fixtures)
    runtime_ref, hardware_ref = _validate_runtime_and_hardware(runtime_path=runtime_manifest, hardware_path=remote_hardware_profile, candidate_header=header)
    contract_ref, contract = _validate_contract(acquisition_contract)
    acquisition_ref, _acquisition_digest, acquisition = _validate_proof(
        acquisition_proof,
        label="acquisition proof",
        schema="assignment.acquisition-proof.v2",
        list_key="requirements",
        expected_ids={f"A{index:02d}" for index in range(1, 16)},
        contract_digest=contract_ref["sha256"],
        source_bundle_digest=None,
        allow_test_fixtures=allow_test_fixtures,
    )
    source_bundle_path, source_bundle_proof_digest, source_bundle_value = _validate_source_bundle(source_bundle, allow_test_fixtures=allow_test_fixtures)
    source_bundle_digest = str(source_bundle_value["bundle_sha256"])
    regression_binding = None
    if historical_regression_proof is not None:
        regression_path, regression_digest, regression = _read_json(historical_regression_proof, label="historical regression advisory")
        try:
            run_matrix._validate_advisory_regression(regression, test_only=allow_test_fixtures)
        except run_matrix.ExecutionError as exc:
            raise FinalizationError(str(exc)) from exc
        regression_binding = {"path": str(regression_path), "sha256": regression_digest}
    pilot_ref, _pilot_digest, pilot = _validate_pilot(
        pilot_evidence,
        settings=header["candidate_final_configuration"],
        source_bundle_digest=source_bundle_digest,
        allow_test_fixtures=allow_test_fixtures,
    )
    _fail(acquisition.get("source_bundle_sha256") in (None, source_bundle_digest), "acquisition proof source bundle binding is inconsistent")

    # The final plan is byte-for-byte the candidate case matrix with a
    # reviewed, complete binding added to its header.  Case IDs and lineage
    # therefore cannot change during selection/freeze.
    frozen_header = deepcopy(header)
    frozen_header["status"] = "frozen_for_execution"
    frozen_header["final_configuration_status"] = "selected"
    frozen_header["validator_integration"] = {
        "production_schema": PRODUCTION_SCHEMA,
        "current_run_matrix_schema": PRODUCTION_SCHEMA,
        "current_case_runner_schema": PRODUCTION_SCHEMA,
        "status": "frozen_execution_binding_validated",
        "do_not_relabel_as_historical_schema": True,
        "required_before_execution": "scheduler preflight must revalidate every bound artifact and canonical seven-key setting",
    }
    frozen_header["execution_binding"] = {
        "schema_version": BOUND_SCHEMA,
        "status": "frozen_for_execution",
        "test_only": bool(allow_test_fixtures),
        "candidate_id": header["candidate_id"],
        "selected_settings": deepcopy(header["candidate_final_configuration"]),
        "selection_record": _bound(_regular(selection_record, label="candidate selection record"), label="candidate selection record"),
        "candidate_inventory": _bound(candidate_inventory_path, label="candidate inventory"),
        "candidate_config": _bound(candidate_config_path, label="candidate config"),
        "runtime_manifest": runtime_ref,
        "remote_hardware_profile": hardware_ref,
        "acquisition_contract": contract_ref,
        "acquisition_proof": _bound(_regular(acquisition_proof, label="acquisition proof"), label="acquisition proof"),
        "pilot_evidence": _bound(pilot_ref, label="pilot evidence"),
        "source_bundle": _bound(source_bundle_path, label="source bundle proof"),
    }
    _fail(set(frozen_header["execution_binding"]) == BOUND_FIELDS, "internal execution binding field drift")
    if regression_binding is not None:
        frozen_header["execution_binding"]["historical_regression_proof"] = regression_binding
    _fail(output_plan.resolve() != candidate_inventory_path.resolve(), "frozen output must be separate from the candidate inventory")
    payload = ("\n".join(_canonical(row) for row in [frozen_header, *cases]) + "\n").encode("utf-8")
    output_plan = output_plan.expanduser().resolve()
    output_digest = _sha256_bytes(payload)
    _atomic_write(output_plan, payload)
    _write_sidecar(output_plan, output_digest)

    # Load the exact bytes after writing.  This invokes the scheduler's strict
    # v2 validator, including all binding hashes and 1,088-case invariants.
    loaded_header, loaded_cases = run_matrix.load_plan(output_plan)
    _fail(loaded_header.get("status") == "frozen_for_execution" and len(loaded_cases) == PRODUCTION_CASE_COUNT, "scheduler rejected the frozen production plan")
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "frozen_for_execution_test_only" if allow_test_fixtures else "frozen_for_execution",
        # Freezing proves that the selected, source-bound plan is internally
        # executable.  Astra's separate launch review owns authorization; the
        # finalizer must never turn a successful proof into launch approval.
        "launch_authorized": False,
        "launch_review_required": True,
        "test_only": bool(allow_test_fixtures),
        "plan_id": PLAN_ID,
        "candidate_id": header["candidate_id"],
        "selected_settings": deepcopy(header["candidate_final_configuration"]),
        "output_plan": str(output_plan),
        "output_plan_sha256": output_digest,
        "case_count": len(cases),
        "bound_inputs": {
            "candidate_inventory_sha256": candidate_digest,
            "candidate_config_sha256": candidate_config_digest,
            "runtime_manifest_sha256": runtime_ref["sha256"],
            "remote_hardware_profile_sha256": hardware_ref["sha256"],
            "acquisition_contract_sha256": contract_ref["sha256"],
            "source_bundle_proof_file_sha256": source_bundle_proof_digest,
        },
        "outcomes_accessed": False,
        "model_or_evaluator_started": False,
    }
    if receipt is not None:
        receipt = receipt.expanduser().resolve()
        _atomic_write(receipt, (_canonical(result) + "\n").encode("utf-8"))
        receipt_digest = _sha256_file(receipt)
        _write_sidecar(receipt, receipt_digest)
        result["receipt"] = str(receipt)
        result["receipt_sha256"] = receipt_digest
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-inventory", required=True, type=Path)
    parser.add_argument("--candidate-config", required=True, type=Path)
    parser.add_argument("--selection-record", required=True, type=Path)
    parser.add_argument("--runtime-manifest", required=True, type=Path)
    parser.add_argument("--remote-hardware-profile", required=True, type=Path)
    parser.add_argument("--acquisition-contract", required=True, type=Path)
    parser.add_argument("--acquisition-proof", required=True, type=Path)
    parser.add_argument("--historical-regression-proof", type=Path, help="optional historical advisory; bytes are bound without requiring passing regressions")
    parser.add_argument("--pilot-evidence", required=True, type=Path)
    parser.add_argument("--source-bundle", required=True, type=Path)
    parser.add_argument("--output-plan", required=True, type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--allow-test-fixtures", action="store_true", help="accept explicitly marked synthetic proof objects; for tests only")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = finalize_production_plan(
            candidate_inventory=args.candidate_inventory,
            candidate_config=args.candidate_config,
            selection_record=args.selection_record,
            runtime_manifest=args.runtime_manifest,
            remote_hardware_profile=args.remote_hardware_profile,
            acquisition_contract=args.acquisition_contract,
            acquisition_proof=args.acquisition_proof,
            historical_regression_proof=args.historical_regression_proof,
            pilot_evidence=args.pilot_evidence,
            source_bundle=args.source_bundle,
            output_plan=args.output_plan,
            receipt=args.receipt,
            allow_test_fixtures=args.allow_test_fixtures,
        )
    except FinalizationError as exc:
        print(f"finalize_production_plan: {exc}", file=sys.stderr)
        return 2
    print(_canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
