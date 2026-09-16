#!/usr/bin/env python3
"""Fit bounded repaired-D9 calibration paths from ledger-normalized events.

This is deliberately a narrow offline adapter.  It reads a caller manifest
only for case locations, then derives the partition from the pinned production
cluster manifest and verifies each case_spec before it opens an events file.
Confirmation, final-evaluation, sealed, unknown, and duplicate-instance cases
are never calibration input.  Models are independent per event class, target
boundary, and declared hardware-profile domain; no cross-hardware scaling is
invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "assignment.repaired-d9-calibration.v1"
FOLD_PREFIX = "assignment.d9.repaired-fold-v1:"
FOLDS = 5
MIN_EVENTS = 25
MIN_INSTANCES = 3

ASSIGNMENT_ROOT = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
PROOF_ROOT = ASSIGNMENT_ROOT / "submission/20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP"
PINNED_SPLIT_MANIFEST = ASSIGNMENT_ROOT / "submission/20260908T140000Z-offline-v2/live-plan/production_split_manifest.v2.json"
PINNED_SPLIT_SHA256 = "0b0c37147b45ec824e2af45d82ba20b3ed57ac56b64f58ea07004e0c13bcc99f"
# Future repaired training cases may be staged under a different submission
# directory.  Case identity still has to come from a case_spec below this
# assignment-owned boundary before any event file becomes readable.
CASE_ROOTS_ROOT = ASSIGNMENT_ROOT / "submission"

# These are categorical declarations known at event start.  The adapter fails
# closed on every other feature name, including fields that might be harmless
# today but could later turn out to carry a measured or post-event value.
ALLOWED_FEATURES = frozenset(
    {
        "semantic_class",
        "operation_class",
        "operation",
        "launch_family",
        "runner",
        "execution_mode",
        "declared_work_bucket",
        "declared_input_bucket",
        "request_kind",
        "batch_bucket",
        "prompt_tokens_bucket",
        "max_output_tokens_bucket",
    }
)
LEDGER_PRE_EVENT_FEATURES = frozenset({"mode", "input_tokens"})
FORBIDDEN_FEATURE_HINTS = (
    "observed", "target", "duration", "timing", "time", "latency", "wall",
    "residual", "output", "cache", "current", "future", "state", "status",
    "outcome", "result", "return", "response", "finish", "case", "instance",
    "attempt", "event_id", "host", "clock", "physical_request",
)
NATIVE_COMPONENTS = ("native_queue", "native_prefill", "native_decode")


class ContractError(ValueError):
    """A manifest or normalized event violates the calibration contract."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ContractError(f"JSON object required: {path}")
    return value


def _fold(instance_id: str) -> int:
    digest = hashlib.sha256((FOLD_PREFIX + instance_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % FOLDS


def _safe_resolve(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ContractError(f"path escapes proof root: {path}") from exc
    return resolved


def _load_pinned_partitions() -> tuple[dict[str, str], dict[str, Any]]:
    actual_hash = _sha256(PINNED_SPLIT_MANIFEST)
    if actual_hash != PINNED_SPLIT_SHA256:
        raise ContractError(
            "pinned split manifest SHA-256 differs from required value: "
            f"expected {PINNED_SPLIT_SHA256}, got {actual_hash}"
        )
    document = _read_json(PINNED_SPLIT_MANIFEST)
    if document.get("schema_version") != "assignment-production-instance-cluster-split.v2":
        raise ContractError("pinned split manifest schema is not accepted")
    clusters = document.get("clusters")
    if not isinstance(clusters, list):
        raise ContractError("pinned split manifest lacks clusters")
    partitions: dict[str, str] = {}
    for row in clusters:
        if not isinstance(row, Mapping):
            raise ContractError("pinned split manifest has malformed cluster")
        instance = row.get("instance_id")
        partition = row.get("partition")
        if not isinstance(instance, str) or not instance or not isinstance(partition, str) or not partition:
            raise ContractError("pinned split manifest cluster lacks identity or partition")
        if instance in partitions:
            raise ContractError(f"duplicate instance in pinned split manifest: {instance}")
        partitions[instance] = partition
    if len(partitions) != 707 or sum(x == "train_calibration" for x in partitions.values()) != 546:
        raise ContractError("pinned split manifest no longer has the frozen 707/546 cluster assignment")
    return partitions, document


def _case_spec_identity(case_root: Path) -> tuple[str, str, str]:
    spec_path = _safe_resolve(case_root / "case_spec.json", CASE_ROOTS_ROOT)
    spec = _read_json(spec_path)
    instance = spec.get("instance_id")
    case_id = spec.get("case_id")
    if not isinstance(instance, str) or not instance or not isinstance(case_id, str) or not case_id:
        raise ContractError(f"case_spec lacks instance_id or case_id: {spec_path}")
    return instance, case_id, _sha256(spec_path)


def inventory(case_roots: Iterable[Path | str]) -> dict[str, Any]:
    """Return only case-spec identity and the pinned derived partition.

    This intentionally never opens an event journal, validation report, or
    result file.  The root pipeline can call it when arrivals occur and pass
    only resulting train-calibration cases to the ledger/calibration stage.
    """

    partitions, split_document = _load_pinned_partitions()
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, value in enumerate(case_roots):
        try:
            root = _safe_resolve(Path(value), CASE_ROOTS_ROOT)
            instance, case_id, spec_hash = _case_spec_identity(root)
            rows.append(
                {
                    "case_root": str(root), "instance_id": instance, "case_id": case_id,
                    "derived_partition": partitions.get(instance, "unknown_not_in_pinned_split"),
                    "case_spec_sha256": spec_hash,
                }
            )
        except (TypeError, ContractError) as exc:
            errors.append({"case_index": index, "error": str(exc)})
    return {
        "schema_version": SCHEMA_VERSION,
        "metadata_only": True,
        "pinned_split_manifest": str(PINNED_SPLIT_MANIFEST),
        "pinned_split_manifest_sha256": _sha256(PINNED_SPLIT_MANIFEST),
        "pinned_split_schema_version": split_document.get("schema_version"),
        "cases": rows,
        "errors": errors,
    }


def _feature_map(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise ContractError("features(start-only) must be an object")
    unknown = sorted(str(key) for key in raw if str(key) not in ALLOWED_FEATURES | LEDGER_PRE_EVENT_FEATURES)
    if unknown:
        raise ContractError("feature keys outside preavailable whitelist: " + ", ".join(unknown))
    values: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key)
        # The ledger's prospective marker is a contract assertion, not a
        # predictive feature.  ``input_tokens`` is known before request start
        # and is reduced immediately to a fixed bucket.
        if name == "mode":
            if value != "prospective":
                raise ContractError("ledger feature mode must be prospective")
            continue
        if name == "input_tokens":
            if isinstance(value, bool):
                raise ContractError("input_tokens must be a nonnegative integer")
            try:
                count = int(value)
            except (TypeError, ValueError) as exc:
                raise ContractError("input_tokens must be a nonnegative integer") from exc
            if count < 0 or str(value).strip() not in {str(count), str(float(count))}:
                raise ContractError("input_tokens must be a nonnegative integer")
            values["prompt_tokens_bucket"] = _token_bucket(count)
            continue
        lowered = name.lower()
        if any(hint in lowered for hint in FORBIDDEN_FEATURE_HINTS):
            # max_output_tokens_bucket is the single expressly configured
            # output-related exception and remains pre-event.
            if name != "max_output_tokens_bucket":
                raise ContractError(f"forbidden feature field: {name}")
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (str, int)) or (isinstance(value, float) and math.isfinite(value)):
            text = str(value).strip().lower()
        else:
            raise ContractError(f"feature {name} must be a finite scalar bucket")
        if not text or len(text) > 128 or "\n" in text or "\r" in text:
            raise ContractError(f"feature {name} is not a bounded categorical value")
        values[name] = text
    return dict(sorted(values.items()))


def _token_bucket(value: int) -> str:
    for boundary in (0, 128, 512, 2048, 8192, 32768):
        if value <= boundary:
            return str(boundary)
    return "32768+"


def _domain(row: Mapping[str, Any]) -> str:
    provenance = row.get("feature_provenance")
    fingerprint = None
    if isinstance(provenance, Mapping):
        fingerprint = provenance.get("hardware_fingerprint") or provenance.get("hardware_profile_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        return "profile_domain_unknown"
    return "hardware_fingerprint:" + fingerprint.strip()


def _event_path(event: Mapping[str, Any]) -> tuple[str, str]:
    event_class = event.get("event_class")
    boundary = event.get("target_boundary")
    if not isinstance(event_class, str) or not event_class.strip() or not isinstance(boundary, str) or not boundary.strip():
        raise ContractError("event_class and target_boundary are required model strata")
    return event_class.strip(), boundary.strip()


def _finite_nonnegative(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise ContractError(f"{name} must be finite and nonnegative")
    return number


def _read_eligible_events(path: Path, expected_instance: str, expected_case: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    record_roles: Counter[str] = Counter()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ContractError(f"cannot open normalized events: {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise ContractError(f"blank normalized event line {line_number}")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid normalized event JSON line {line_number}: {exc}") from exc
        if not isinstance(raw, Mapping):
            raise ContractError(f"normalized event {line_number} is not an object")
        role = raw.get("record_role")
        if not isinstance(role, str) or not role:
            raise ContractError(f"normalized event lacks explicit record_role at line {line_number}")
        record_roles[role] += 1
        if role != "TARGET":
            # Context/observation rows remain in the normalized ledger for
            # auditability.  They are not converted into targets by a class,
            # feature, or missing-value heuristic.
            continue
        instance = raw.get("instance_id")
        case_id = raw.get("case_id")
        event_id = raw.get("event_id")
        attempt_id = raw.get("attempt_id")
        partition = raw.get("partition")
        host_id = raw.get("host_id")
        clock_id = raw.get("clock_id")
        provenance = raw.get("feature_provenance")
        if instance != expected_instance or case_id != expected_case:
            raise ContractError(f"normalized event identity mismatch at line {line_number}")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ContractError(f"normalized event lacks attempt_id at line {line_number}")
        # Ledger preserves the case-spec partition as untrusted provenance.
        # A missing value is allowed because the calibrator derived the pinned
        # partition already; a contradictory asserted value is rejected.
        if partition is not None and partition != "train_calibration":
            raise ContractError(f"normalized event partition disagrees with pinned partition at line {line_number}")
        if not isinstance(host_id, str) or not host_id or not isinstance(clock_id, str) or not clock_id:
            raise ContractError(f"normalized event lacks host_id or clock_id at line {line_number}")
        if not isinstance(provenance, (Mapping, str)) or isinstance(provenance, str) and not provenance.strip():
            raise ContractError(f"normalized event lacks feature_provenance at line {line_number}")
        if raw.get("model_eligible") is not True:
            raise ContractError(f"TARGET row is not ledger model_eligible at line {line_number}")
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise ContractError(f"normalized event duplicate/missing event_id at line {line_number}")
        seen.add(event_id)
        event_class, boundary = _event_path(raw)
        target = _finite_nonnegative(raw.get("observed_ms"), "observed_ms")
        rows.append(
            {
                "instance_id": instance,
                "case_id": case_id,
                "event_id": event_id,
                "event_class": event_class,
                "target_boundary": boundary,
                "observed_ms": target,
                "features": _feature_map(raw.get("features")),
                "hardware_domain": _domain(raw),
                "physical_request_id": raw.get("physical_request_id") if isinstance(raw.get("physical_request_id"), str) else None,
            }
        )
    return rows, dict(sorted(record_roles.items()))


def _median(rows: Iterable[Mapping[str, Any]]) -> float:
    return float(statistics.median(float(row["observed_ms"]) for row in rows))


def _feature_key(row: Mapping[str, Any]) -> str:
    return json.dumps(row["features"], sort_keys=True, separators=(",", ":"))


def _table(rows: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    global_median = _median(rows)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_feature_key(row)].append(row)
    table = {
        key: _median(values)
        for key, values in grouped.items()
        if len(values) >= MIN_EVENTS and len({str(x["instance_id"]) for x in values}) >= MIN_INSTANCES
    }
    return global_median, table


def _prediction(row: Mapping[str, Any], global_median: float, table: Mapping[str, float]) -> float:
    return float(table.get(_feature_key(row), global_median))


def _metric(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    if not predictions:
        return {"n": 0, "status": "unsupported_no_test_rows"}
    pass_count = 0
    finite_apes: list[float] = []
    infinite = 0
    zero_target = 0
    absolute = 0.0
    for row in predictions:
        observed = float(row["observed_ms"])
        predicted = float(row["prediction_ms"])
        absolute += abs(predicted - observed)
        if observed == 0:
            zero_target += 1
            if predicted == 0:
                pass_count += 1
            else:
                infinite += 1
            continue
        ape = abs(predicted - observed) / observed * 100.0
        finite_apes.append(ape)
        pass_count += ape <= 25.0
    return {
        "n": len(predictions),
        "within25_count": pass_count,
        "within25_rate": pass_count / len(predictions),
        "absolute_error_ms": absolute,
        "zero_target_count": zero_target,
        "infinite_ape_count": infinite,
        "max_ape_pct": max(finite_apes) if finite_apes and not infinite else (0.0 if zero_target and not infinite else None),
        "max_ape_status": "finite" if not infinite else "infinite_due_to_zero_target_nonzero_prediction",
        "mean_ape_pct": statistics.mean(finite_apes) if finite_apes and not infinite else (0.0 if zero_target and not infinite else None),
        "mean_ape_status": "finite" if not infinite and (finite_apes or zero_target) else "unsupported_infinite_zero_target_error",
    }


def _component_name(row: Mapping[str, Any]) -> str | None:
    values = {
        str(row["event_class"]).strip().lower().replace(":", "_").replace("-", "_"),
        str(row["target_boundary"]).strip().lower().replace(":", "_").replace("-", "_"),
    }
    for name in NATIVE_COMPONENTS:
        phase = name.removeprefix("native_")
        if any(value == name or value.endswith("_" + phase) and "native" in value for value in values):
            return name
    return None


def _native_component_sum_metric(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in predictions:
        component = _component_name(row)
        request = row.get("physical_request_id")
        if component is None or not request:
            continue
        key = (str(row["hardware_domain"]), str(request), int(row["fold"]))
        if component in grouped[key]:
            return {"status": "unsupported_duplicate_component_per_physical_request"}
        grouped[key][component] = row
    complete = [items for items in grouped.values() if set(items) == set(NATIVE_COMPONENTS)]
    if not complete:
        return {"status": "unsupported_incomplete_native_component_composition"}
    composed = [
        {
            "observed_ms": sum(float(row["observed_ms"]) for row in items.values()),
            "prediction_ms": sum(float(row["prediction_ms"]) for row in items.values()),
        }
        for items in complete
    ]
    result = _metric(composed)
    result["status"] = "supported_native_component_sum_queue_prefill_decode"
    result["scope"] = "sum of independently predicted native queue/prefill/decode targets; not native E2E or outer E2E"
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def _pending(report: dict[str, Any], output_dir: Path, reason: str) -> dict[str, Any]:
    report["disposition"] = "pending_no_fit"
    report["pending_reason"] = reason
    report["needed_inputs"] = [
        "case_spec-bound repaired cases whose instance_id is assigned train_calibration by the pinned split manifest",
        "at least 3 independent eligible instances and 25 valid normalized events for each fitted event-class/target-boundary/hardware-domain path",
        "ledger validation report with accepted disposition and source hashes",
        "pre-event whitelisted feature buckets and hardware-profile fingerprint provenance",
    ]
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "disposition": "pending_no_fit",
        "reason": reason,
        "source_provenance": report["source_provenance"],
        "model_paths": [],
    }
    _write_json(output_dir / "calibration_report.json", report)
    _write_json(output_dir / "model_artifact.json", artifact)
    return report


def calibrate(normalized_manifest: Path | str | Mapping[str, Any], output_dir: Path | str) -> dict[str, Any]:
    """Fit eligible repaired paths and write report/model artifacts.

    ``normalized_manifest`` is the root pipeline manifest with a ``cases``
    list.  Its claimed partition and identity proof are deliberately ignored;
    trusted partitioning and case identity are rebuilt before event files open.
    """

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if isinstance(normalized_manifest, Mapping):
        manifest = normalized_manifest
        manifest_hash = _canonical_hash(manifest)
    else:
        manifest_path = Path(normalized_manifest)
        manifest = _read_json(manifest_path)
        manifest_hash = _sha256(manifest_path)
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ContractError("pipeline manifest must have a cases list")
    partitions, split_document = _load_pinned_partitions()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "disposition": "pending_no_fit",
        "source_provenance": {
            "pipeline_manifest_sha256": manifest_hash,
            "pinned_split_manifest": str(PINNED_SPLIT_MANIFEST),
            "pinned_split_manifest_sha256": _sha256(PINNED_SPLIT_MANIFEST),
            "pinned_split_schema_version": split_document.get("schema_version"),
        },
        "identity_inventory": [],
        "input_errors": [],
        "excluded_before_events_open": [],
        "eligible_case_inventory": [],
        "model_paths": [],
    }

    eligible_cases: list[dict[str, Any]] = []
    seen_case_roots: set[Path] = set()
    for index, entry in enumerate(cases):
        if not isinstance(entry, Mapping):
            report["input_errors"].append({"case_index": index, "error": "case entry must be an object"})
            continue
        try:
            root = _safe_resolve(Path(str(entry.get("case_root", ""))), CASE_ROOTS_ROOT)
            if root in seen_case_roots:
                raise ContractError("duplicate case_root")
            seen_case_roots.add(root)
            instance, case_id, spec_hash = _case_spec_identity(root)
            claimed_instance = entry.get("instance_id")
            claimed_case = entry.get("case_id")
            if claimed_instance != instance or claimed_case != case_id:
                raise ContractError("caller manifest identity disagrees with case_spec")
            partition = partitions.get(instance, "unknown_not_in_pinned_split")
            inventory = {
                "case_index": index,
                "case_root": str(root),
                "instance_id": instance,
                "case_id": case_id,
                "derived_partition": partition,
                "case_spec_sha256": spec_hash,
            }
            report["identity_inventory"].append(inventory)
            if partition != "train_calibration":
                report["excluded_before_events_open"].append({**inventory, "reason": "not_train_calibration"})
                continue
            events_value = Path(str(entry.get("events_path", "")))
            validation_value = Path(str(entry.get("validation_report_path", "")))
            # The ledger pipeline writes normalized files outside a proof case
            # directory.  These locations become readable only after the
            # case_spec and pinned train partition gate above; their declared
            # SHA-256 values are then checked before JSONL parsing.
            events_path = (events_value if events_value.is_absolute() else root / events_value).resolve()
            validation_path = (validation_value if validation_value.is_absolute() else root / validation_value).resolve()
            events_sha = entry.get("events_sha256")
            validation_sha = entry.get("validation_report_sha256")
            if not isinstance(events_sha, str) or len(events_sha) != 64:
                raise ContractError("eligible caller manifest lacks events_sha256")
            if not isinstance(validation_sha, str) or len(validation_sha) != 64:
                raise ContractError("eligible caller manifest lacks validation_report_sha256")
            eligible_cases.append(
                {**inventory, "events_path": events_path, "validation_path": validation_path,
                 "events_sha256": events_sha, "validation_report_sha256": validation_sha}
            )
        except ContractError as exc:
            report["input_errors"].append({"case_index": index, "error": str(exc)})

    report["eligible_case_inventory"] = [
        {key: value for key, value in row.items() if key not in {"events_path", "validation_path"}}
        for row in eligible_cases
    ]
    eligible_instances = {row["instance_id"] for row in eligible_cases}
    if not eligible_cases:
        return _pending(report, output, "no_case_spec_bound_train_calibration_cases; confirmation/excluded cases were not opened")
    if len(eligible_instances) < MIN_INSTANCES:
        return _pending(report, output, f"only_{len(eligible_instances)}_independent_train_instances_available_before_event_read")

    events: list[dict[str, Any]] = []
    for case in eligible_cases:
        try:
            if _sha256(case["validation_path"]) != case["validation_report_sha256"]:
                raise ContractError("validation report SHA-256 differs from caller manifest")
            validation = _read_json(case["validation_path"])
            disposition = str(validation.get("disposition", "")).lower()
            validation_detail = validation.get("validation")
            status = str(validation_detail.get("status", "")).lower() if isinstance(validation_detail, Mapping) else ""
            hashes = validation.get("source_hashes")
            is_accepted = disposition in {"accepted", "pass", "valid"} or status in {"accepted", "pass", "valid"}
            if not is_accepted or not isinstance(hashes, (Mapping, list)):
                raise ContractError("ledger validation report is not accepted with source_hashes")
            if _sha256(case["events_path"]) != case["events_sha256"]:
                raise ContractError("normalized events SHA-256 differs from caller manifest")
            rows, role_counts = _read_eligible_events(case["events_path"], case["instance_id"], case["case_id"])
            for row in rows:
                row["fold"] = _fold(str(row["instance_id"]))
            events.extend(rows)
            report["source_provenance"].setdefault("eligible_cases", []).append(
                {
                    "case_id": case["case_id"], "case_spec_sha256": case["case_spec_sha256"],
                    "normalized_events_sha256": case["events_sha256"],
                    "validation_report_sha256": case["validation_report_sha256"],
                    "validation_source_hashes": hashes,
                    "normalized_record_role_counts": role_counts,
                }
            )
        except ContractError as exc:
            report["input_errors"].append({"case_id": case["case_id"], "error": str(exc)})
    if report["input_errors"]:
        return _pending(report, output, "eligible_input_rejected; no partial fit after contract error")
    if not events:
        return _pending(report, output, "eligible_cases_have_no_normalized_events")

    path_rows: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        path_rows[(row["event_class"], row["target_boundary"], row["hardware_domain"])].append(row)
    oof_predictions: list[dict[str, Any]] = []
    artifact_paths: list[dict[str, Any]] = []
    for key in sorted(path_rows):
        rows = path_rows[key]
        event_class, target_boundary, domain = key
        instances = sorted({str(row["instance_id"]) for row in rows})
        description: dict[str, Any] = {
            "event_class": event_class, "target_boundary": target_boundary, "hardware_domain": domain,
            "event_count": len(rows), "instance_count": len(instances),
        }
        if len(rows) < MIN_EVENTS or len(instances) < MIN_INSTANCES:
            description["status"] = "unsupported_insufficient_independent_support"
            artifact_paths.append(description)
            continue
        if domain == "profile_domain_unknown":
            description["status"] = "unsupported_unknown_hardware_profile_domain"
            description["reason"] = "host_id is not a hardware domain and no hardware fingerprint was supplied"
            artifact_paths.append(description)
            continue
        fold_support = []
        for fold in sorted({int(row["fold"]) for row in rows}):
            train_rows = [row for row in rows if row["fold"] != fold]
            fold_support.append(
                {"fold": fold, "train_event_count": len(train_rows),
                 "train_instance_count": len({str(row["instance_id"]) for row in train_rows})}
            )
        if any(item["train_event_count"] < MIN_EVENTS or item["train_instance_count"] < MIN_INSTANCES for item in fold_support):
            description["status"] = "unsupported_insufficient_grouped_train_fold_support"
            description["fold_support"] = fold_support
            artifact_paths.append(description)
            continue
        path_predictions: list[dict[str, Any]] = []
        for fold in sorted({int(row["fold"]) for row in rows}):
            test_rows = [row for row in rows if row["fold"] == fold]
            train_rows = [row for row in rows if row["fold"] != fold]
            if not train_rows:
                continue
            global_median, table = _table(train_rows)
            for row in test_rows:
                path_predictions.append({**row, "prediction_ms": _prediction(row, global_median, table)})
        full_global, full_table = _table(rows)
        description.update(
            {
                "status": "fitted",
                "grouped_folds": {
                    "rule": "sha256('assignment.d9.repaired-fold-v1:' + instance_id) first 8 bytes modulo 5",
                    "observed_folds": sorted({int(row["fold"]) for row in rows}),
                    "test_event_count": len(path_predictions),
                    "fold_support": fold_support,
                },
                "oof_metrics": _metric(path_predictions),
                "model": {"kind": "per_event_class_target_boundary_hardware_domain_median", "global_median_ms": full_global,
                          "feature_strata": full_table, "minimum_group_events": MIN_EVENTS,
                          "minimum_group_instances": MIN_INSTANCES},
            }
        )
        artifact_paths.append(description)
        oof_predictions.extend(path_predictions)
    report["model_paths"] = artifact_paths
    report["event_count"] = len(events)
    report["independent_instance_count"] = len({row["instance_id"] for row in events})
    report["per_event_oof"] = {
        "all_fitted_paths": _metric(oof_predictions),
        "by_path": [
            {"event_class": row["event_class"], "target_boundary": row["target_boundary"], "hardware_domain": row["hardware_domain"],
             "metrics": row["oof_metrics"]}
            for row in artifact_paths if row.get("status") == "fitted"
        ],
    }
    report["native_component_sum_oof"] = _native_component_sum_metric(oof_predictions)
    report["outer_e2e_oof"] = {
        "status": "unsupported_no_separate_outer_e2e_target_model_path",
        "reason": "native queue/prefill/decode components are not asserted to sum to native or outer E2E",
    }
    fitted = [row for row in artifact_paths if row.get("status") == "fitted"]
    if not fitted:
        return _pending(report, output, "no_event_class_target_boundary_hardware_domain_path_has_minimum_support")
    report["disposition"] = "fitted_with_supported_paths"
    artifact = {"schema_version": SCHEMA_VERSION, "disposition": report["disposition"],
                "source_provenance": report["source_provenance"], "model_paths": fitted}
    _write_json(output / "calibration_report.json", report)
    _write_json(output / "model_artifact.json", artifact)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = calibrate(args.manifest, args.output_dir)
    except ContractError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"disposition": report["disposition"], "report": str(args.output_dir / "calibration_report.json")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
