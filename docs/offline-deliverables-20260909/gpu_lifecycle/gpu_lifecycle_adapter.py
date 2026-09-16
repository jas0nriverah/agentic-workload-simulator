#!/usr/bin/env python3
"""Offline-only D9 GPU/lifecycle feature and composition adapter.

This module deliberately does not fit a model.  It converts a physical request
journal into prospective request descriptors, rejects known post-event fields,
and composes supplied component *predictions* only when the requested E2E
contract has all of its declared components.  It is intended for an
identity-filtered train-calibration dataset or a contract smoke fixture.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


FORBIDDEN_FEATURES = frozenset(
    {
        "output_tokens",
        "duration_ms",
        "wall_ms",
        "queue_ms",
        "prefill_ms",
        "decode_ms",
        "measured_residual_ms",
        "unknown_residual_ms",
        "current_cache_hit",
        "cache_hit",
    }
)
PROSPECTIVE_FIELDS = ("input_tokens", "context_tokens", "max_output_tokens")
PREDICTION_CLASSES = ("cpu", "gpu", "lifecycle")


class ContractError(ValueError):
    pass


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ContractError(f"{path}:{line_number}: JSONL row must be an object")
            rows.append(row)
    return rows


def _identities(rows: Iterable[Mapping[str, Any]]) -> set[tuple[str, str, str]]:
    return {
        (str(row.get("run_id", "")), str(row.get("instance_id", "")), str(row.get("case_id", "")))
        for row in rows
    }


def prospective_request_features(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return one descriptor per request-start, never copying a terminal label.

    ``context_tokens`` is accepted only when the start row declares it as a
    direct measured/request value.  A derived alias of prompt tokens is not a
    separately available feature.  Missing fields remain null; callers must
    use an explicit support-aware fallback rather than inventing them.
    """
    output: list[dict[str, Any]] = []
    for row in rows:
        if row.get("event_kind") != "model_request_start":
            continue
        features = row.get("features") if isinstance(row.get("features"), Mapping) else row
        feature_mode = features.get("mode", row.get("feature_mode"))
        if feature_mode != "prospective":
            raise ContractError("model_request_start must carry prospective features")
        context_provenance = row.get("context_tokens_provenance")
        context = features.get("context_tokens")
        if context_provenance and context_provenance not in {"measured", "request", "declared"}:
            context = None
        descriptor = {
            "request_id": str(row.get("physical_request_id") or row.get("request_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "instance_id": str(row.get("instance_id") or ""),
            "case_id": str(row.get("case_id") or ""),
            "logical_request_id": row.get("logical_request_id"),
            "retry_index": row.get("retry_index"),
            "input_tokens": features.get("input_tokens"),
            "context_tokens": context,
            "max_output_tokens": features.get("max_output_tokens", row.get("max_output_tokens")),
            "context_tokens_provenance": context_provenance,
            "feature_mode": "prospective",
            "forbidden_fields_absent": True,
        }
        if not descriptor["request_id"] or not descriptor["run_id"] or not descriptor["instance_id"]:
            raise ContractError("request start is missing request/run/instance identity")
        leaked = set(descriptor) & FORBIDDEN_FEATURES
        if leaked:
            raise ContractError("adapter emitted forbidden field(s): " + ", ".join(sorted(leaked)))
        output.append(descriptor)
    return output


def availability_report(rows: Iterable[Mapping[str, Any]], source_kind: str) -> dict[str, Any]:
    rows = list(rows)
    starts = prospective_request_features(rows)
    terminal = [row for row in rows if row.get("event_kind") == "model_request" and row.get("terminal")]
    phase_targets = sum(
        1
        for row in terminal
        if all(isinstance(row.get(field), (int, float)) for field in ("queue_ms", "prefill_ms", "decode_ms"))
    )
    # Native attribution is a terminal-only journal. It is useful to prove the
    # phase target exists, but it cannot invent a pre-dispatch feature row.
    native_phase_targets = sum(
        1
        for row in rows
        if isinstance(row.get("metrics"), Mapping)
        and all(isinstance(row["metrics"].get(name, {}).get("value_ms"), (int, float)) for name in ("queue", "prefill", "decode"))
    )
    instances = {str(row.get("instance_id")) for row in rows if row.get("instance_id")}
    return {
        "schema_version": "assignment.d9.gpu-lifecycle-availability.v1",
        "source_kind": source_kind,
        "identity_count": len(_identities(rows)),
        "independent_instance_count": len(instances),
        "identities": sorted("|".join(parts) for parts in _identities(rows)),
        "request_start_count": len(starts),
        "terminal_request_count": len(terminal),
        "terminal_gpu_phase_target_count": phase_targets + native_phase_targets,
        "native_terminal_only_phase_target_count": native_phase_targets,
        "prospective_feature_completeness": dict(
            Counter(field for row in starts for field in PROSPECTIVE_FIELDS if row.get(field) is not None)
        ),
        "numeric_fit_status": "eligible_only_with_independent_train_calibration_identities"
        if len(instances) > 1 and phase_targets
        else "unidentifiable_from_this_source",
        "forbidden_current_event_features": sorted(FORBIDDEN_FEATURES),
    }


def compose_prediction_manifest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Compose precomputed component predictions under an explicit E2E contract.

    ``conditional_known_action_list`` permits a sum only for the supplied,
    predeclared event set.  ``prospective_full_trajectory`` rejects an E2E sum:
    future actions/requests have not yet been declared.  This distinction
    prevents a replay composition from being presented as trajectory forecast.
    """
    mode = document.get("trajectory_mode")
    if mode not in {"conditional_known_action_list", "prospective_full_trajectory"}:
        raise ContractError("trajectory_mode must be conditional_known_action_list or prospective_full_trajectory")
    predictions = document.get("component_predictions")
    if not isinstance(predictions, list) or not predictions:
        raise ContractError("component_predictions must be a non-empty list")
    ledger = document.get("disjoint_serial_component_ledger")
    ledger_ok = isinstance(ledger, Mapping) and ledger.get("status") == "proven"
    by_run: dict[str, dict[str, float]] = {}
    seen_component_ids: set[str] = set()
    for row in predictions:
        if not isinstance(row, Mapping):
            raise ContractError("component prediction must be an object")
        unknown = set(row) - {"run_id", "component", "component_id", "clock_id", "predicted_ms", "feature_contract"}
        if unknown or set(row) & FORBIDDEN_FEATURES:
            raise ContractError("component prediction contains forbidden/unknown field(s)")
        component = row.get("component")
        if component not in PREDICTION_CLASSES:
            raise ContractError("component must be cpu, gpu, or lifecycle")
        if row.get("feature_contract") != "pre_event_only":
            raise ContractError("component prediction must declare feature_contract=pre_event_only")
        value = row.get("predicted_ms")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ContractError("predicted_ms must be finite, non-boolean, and nonnegative")
        run_id = row.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ContractError("component prediction requires run_id")
        component_id = row.get("component_id")
        if not isinstance(component_id, str) or not component_id or component_id in seen_component_ids:
            raise ContractError("component_id must be unique and non-empty")
        if not isinstance(row.get("clock_id"), str) or not row["clock_id"]:
            raise ContractError("component prediction requires clock_id")
        seen_component_ids.add(component_id)
        aggregate = by_run.setdefault(run_id, {name: 0.0 for name in PREDICTION_CLASSES})
        aggregate[component] += float(value)
    trajectory = []
    for run_id in sorted(by_run):
        aggregate = by_run[run_id]
        complete = all(any(row.get("run_id") == run_id and row.get("component") == name for row in predictions) for name in PREDICTION_CLASSES)
        trajectory.append(
            {
                "run_id": run_id,
                "predicted_cpu_ms": aggregate["cpu"],
                "predicted_gpu_ms": aggregate["gpu"],
                "predicted_lifecycle_ms": aggregate["lifecycle"],
                "predicted_e2e_ms": sum(aggregate.values()) if mode == "conditional_known_action_list" and complete and ledger_ok else None,
                "e2e_status": "conditional_known_action_list" if mode == "conditional_known_action_list" and complete and ledger_ok else "unproven_composition_or_not_a_full_trajectory_forecast",
            }
        )
    return {
        "schema_version": "assignment.d9.gpu-lifecycle-composition.v1",
        "trajectory_mode": mode,
        "composition_ledger_status": "proven" if ledger_ok else "unproven_no_e2e_sum",
        "target_boundaries": {
            "cpu": "CPU tool/event duration only",
            "gpu": "physical request E2E or separately named native phase; never a residual",
            "lifecycle": "non-overlapping setup/startup/retry/teardown intervals only",
            "e2e": "sum of the declared non-overlapping conditional action list only",
        },
        "trajectories": trajectory,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-journal", type=Path, required=True)
    parser.add_argument("--source-kind", choices=("synthetic_smoke", "eligible_train_calibration", "confirmation_excluded"), required=True)
    parser.add_argument("--availability-output", type=Path, required=True)
    parser.add_argument("--composition-input", type=Path)
    parser.add_argument("--composition-output", type=Path)
    parser.add_argument("--native-attribution", type=Path, help="terminal native labels for an exact physical-request binding smoke")
    parser.add_argument("--declared-instance-id", help="confirmation/smoke identity; never a learned feature")
    args = parser.parse_args()
    rows = _read_jsonl(args.model_journal)
    if args.declared_instance_id:
        # Only fills an absent journal identity for an explicitly supplied
        # smoke source; it is not inferred from an outcome or path.
        rows = [dict(row, instance_id=row.get("instance_id") or args.declared_instance_id) for row in rows]
    availability = availability_report(rows, args.source_kind)
    if args.native_attribution:
        native_rows = _read_jsonl(args.native_attribution)
        starts = {str(row.get("physical_request_id") or row.get("request_id")) for row in rows if row.get("event_kind") == "model_request_start"}
        native = {str(row.get("physical_request_id") or row.get("request_id")) for row in native_rows}
        if not starts or starts != native or len(starts) != len(native_rows):
            raise ContractError("native terminal rows do not bind one-to-one to model_request_start physical IDs")
        availability["physical_request_binding"] = {
            "status": "exact_one_to_one",
            "request_start_count": len(starts),
            "native_terminal_count": len(native_rows),
            "note": "terminal native token/cache/phase fields remain targets, never adapter features",
        }
        availability["native_terminal_only_phase_target_count"] = sum(
            1 for row in native_rows
            if isinstance(row.get("metrics"), Mapping)
            and all(isinstance(row["metrics"].get(name, {}).get("value_ms"), (int, float)) for name in ("queue", "prefill", "decode"))
        )
        availability["terminal_gpu_phase_target_count"] = availability["native_terminal_only_phase_target_count"]
    args.availability_output.parent.mkdir(parents=True, exist_ok=True)
    args.availability_output.write_text(json.dumps(availability, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if bool(args.composition_input) != bool(args.composition_output):
        raise ContractError("composition input and output must be supplied together")
    if args.composition_input:
        composition = compose_prediction_manifest(json.loads(args.composition_input.read_text(encoding="utf-8")))
        args.composition_output.write_text(json.dumps(composition, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
