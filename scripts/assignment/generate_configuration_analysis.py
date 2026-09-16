#!/usr/bin/env python3
"""Generate the descriptive, source-bound Step 2 configuration analysis.

This reads the preserved trajectory/sweep views only.  It does not execute a
model, evaluator, or workload and it does not read the sealed holdout.  The
96 shared-baseline plotting views are checked against the canonical baseline
rows and are not counted as new runs.  The twelve non-baseline settings are
paired with the same-suite, same-instance baseline; bootstrap resampling is
clustered by ``instance_id`` so the duplicate Django ID remains one cluster.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "20260908T140000Z-offline-v2"
SCHEMA_VERSION = "assignment.configuration-analysis.v1"
HOLDOUT_INSTANCE_ID = "sympy__sympy-12481"
DEFAULT_BOOTSTRAP_REPETITIONS = 10000
DEFAULT_BOOTSTRAP_SEED = 20260908
CONFIDENCE = 0.95
IMMUTABLE_PLAN_SHA256 = "2bead159a24e244ecbf981c63f3d43d24f8bf1fe9a389c398f17325d941069fc"
BASELINE_SETTINGS = {
    "call_limit": 30,
    "max_output_tokens": 2048,
    "observation_length": 100000,
    "temperature": 0.0,
}
SETTING_ORDER = (
    ("call_limit", 10),
    ("call_limit", 20),
    ("call_limit", 50),
    ("max_output_tokens", 512),
    ("max_output_tokens", 1024),
    ("max_output_tokens", 4096),
    ("observation_length", 10000),
    ("observation_length", 25000),
    ("observation_length", 50000),
    ("temperature", 0.2),
    ("temperature", 0.5),
    ("temperature", 0.8),
)
REQUIRED_SETTING_COUNTS = {setting: 24 for setting in SETTING_ORDER}

# The historical 12-setting report above is deliberately kept separate from
# this predeclared confirmation panel.  The panel is a development decision
# aid; it never mutates the final 1088-case production matrix.
CONFIRMATION_PANEL_SCHEMA_VERSION = "assignment.configuration-confirmation-panel.v1"
CONFIRMATION_PANEL_SEED = "assignment-configuration-confirmation-panel-20260908-v1"
CONFIRMATION_EXIT_CLASSES = ("exit_cost", "exit_context")
CONFIRMATION_PANEL_EXISTING_COUNT = 16
CONFIRMATION_PANEL_ADDITIONAL_PER_CLASS = 4
CONFIRMATION_PANEL_COUNT = 24
CONFIRMATION_CASE_COUNT = 96
PRODUCTION_CALL_LIMIT_GRID = (20, 30, 50, 100)
MODEL_NAME = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
MODEL_REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"
TOKENIZER_REVISION = MODEL_REVISION
SWE_AGENT_REVISION = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
SWE_BENCH_REVISION = "726c5461e2ef52d83cf1ea2107870a8bb3328d57"
VLLM_VERSION = "0.10.0"
SERVING_MAX_MODEL_LEN = 65536
CONFIRMATION_FIXED_SETTINGS = {
    "max_output_tokens": 2048,
    "temperature": 0.0,
    "top_p": 1.0,
    "seed": 0,
}

# Keep this as data rather than deriving it from a historical sweep.  The
# client input guard is a candidate setting and is intentionally distinct from
# the pinned serving context length.
CONFIGURATION_CANDIDATES = (
    {
        "candidate_id": "historical-control-call30-input32768",
        "label": "historical control",
        "call_limit": 30,
        "max_input_tokens": 32768,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call50-input61440",
        "label": "expanded call budget",
        "call_limit": 50,
        "max_input_tokens": 61440,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call100-input61440",
        "label": "expanded call budget",
        "call_limit": 100,
        "max_input_tokens": 61440,
        "observation_length": 100000,
    },
    {
        "candidate_id": "expanded-call100-input61440-observation25000",
        "label": "expanded calls with short observation",
        "call_limit": 100,
        "max_input_tokens": 61440,
        "observation_length": 25000,
    },
)


class ConfigurationAnalysisError(ValueError):
    """Raised when source rows cannot support a valid paired analysis."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_hashed(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    digest = sha256_bytes(payload)
    Path(f"{path}.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest


def _write_json(path: Path, value: Any) -> str:
    return _write_hashed(path, (canonical_json(value) + "\n").encode("utf-8"))


def _read_csv(path: Path) -> tuple[list[dict[str, str]], list[str], str]:
    raw = path.read_bytes()
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ConfigurationAnalysisError(f"CSV has no header: {path}")
        rows = [dict(row) for row in reader]
    return rows, list(reader.fieldnames), sha256_bytes(raw)


def _read_metadata(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    raw = path.read_bytes()
    rows = []
    header = None
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            raise ConfigurationAnalysisError(f"blank metadata line {path}:{line_no}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ConfigurationAnalysisError(f"metadata line is not an object: {path}:{line_no}")
        if value.get("record_type") == "metadata":
            if header is not None:
                raise ConfigurationAnalysisError(f"duplicate metadata header: {path}")
            header = value
        else:
            rows.append(value)
    if header is None:
        raise ConfigurationAnalysisError(f"metadata header missing: {path}")
    if header.get("record_count") != len(rows):
        raise ConfigurationAnalysisError(
            f"metadata record_count={header.get('record_count')} but parsed {len(rows)} rows"
        )
    return header, rows, sha256_bytes(raw)


def _bool(value: str, field: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ConfigurationAnalysisError(f"{field} must be true/false, got {value!r}")


def _number(value: str, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationAnalysisError(f"{field} must be numeric, got {value!r}") from exc
    if not math.isfinite(result):
        raise ConfigurationAnalysisError(f"{field} must be finite")
    return result


def _setting_value(knob: str, value: str) -> int | float:
    if knob in {"call_limit", "max_output_tokens", "observation_length"}:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationAnalysisError(f"{knob} value is not an integer: {value!r}") from exc
        return result
    return _number(value, f"{knob}.sweep_value")


def _key(knob: str, value: int | float) -> tuple[str, int | float]:
    # Avoid 0.0/0 and float representation differences in the setting map.
    return knob, value


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(q * len(values)) - 1)]


def _value_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _stable_seed(base: int, label: str) -> int:
    return (base + int(hashlib.sha256(label.encode("utf-8")).hexdigest()[:16], 16)) % (2**63)


def candidate_settings(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete, hashable settings object for one candidate.

    The model and serving pins live beside this object in the panel manifest;
    this object contains every runtime knob whose value is compared by the
    confirmation run.  Keeping the client input guard and serving context in
    separate fields prevents an accidental claim that they are the same
    limit.
    """

    expected = {item["candidate_id"]: item for item in CONFIGURATION_CANDIDATES}
    candidate_id = str(candidate.get("candidate_id", ""))
    if candidate_id not in expected:
        raise ConfigurationAnalysisError(f"unknown configuration candidate: {candidate_id!r}")
    declared = expected[candidate_id]
    if candidate.get("call_limit") != declared["call_limit"] or candidate.get("max_input_tokens") != declared["max_input_tokens"] or candidate.get("observation_length") != declared["observation_length"]:
        raise ConfigurationAnalysisError(f"candidate values do not match declaration: {candidate_id}")
    settings = {
        "call_limit": declared["call_limit"],
        "max_input_tokens": declared["max_input_tokens"],
        "observation_length": declared["observation_length"],
        **CONFIRMATION_FIXED_SETTINGS,
    }
    return settings


def _candidate_record(candidate: Mapping[str, Any]) -> dict[str, Any]:
    settings = candidate_settings(candidate)
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "label": str(candidate["label"]),
        "settings": settings,
        "final_configuration": deepcopy(settings),
        "settings_sha256": sha256_bytes(canonical_json(settings).encode("utf-8")),
        "serving_configuration": {"max_model_len": SERVING_MAX_MODEL_LEN, "vllm_version": VLLM_VERSION},
    }


def _termination_class(row: Mapping[str, Any]) -> str:
    value = row.get("native_exit_class", row.get("termination_class", ""))
    if value is None:
        return ""
    return str(value)


def _selection_hash(*, seed: str, termination_class: str, row: Mapping[str, Any]) -> str:
    # Only identity fields participate.  In particular, no evaluator result,
    # timing, response, or status field can influence the preselection hash.
    identity = {
        "case_id": str(row.get("case_id", row.get("run_id", ""))),
        "category": str(row.get("category", row.get("repository", ""))),
        "instance_id": str(row.get("instance_id", "")),
        "repository": str(row.get("repository", row.get("category", ""))),
        "suite": str(row.get("suite", "")),
        "termination_class": termination_class,
        "seed": seed,
    }
    return sha256_bytes(canonical_json(identity).encode("utf-8"))


def select_confirmation_additions(
    evidence_rows: Sequence[Mapping[str, Any]],
    *,
    excluded_case_ids: Iterable[str],
    excluded_instance_ids: Iterable[str] = (),
    seed: str = CONFIRMATION_PANEL_SEED,
    per_class: int = CONFIRMATION_PANEL_ADDITIONAL_PER_CLASS,
) -> list[dict[str, Any]]:
    """Select distinct historical failure clusters for the confirmation panel.

    A class is selected by deterministic identity hash.  The first pass picks
    one row from each hash-ranked repository category, so each four-row class
    has four distinct categories.  Existing pilot instances and the sealed
    holdout are excluded before ranking.  A selected instance is a cluster;
    duplicate suite copies therefore cannot consume two panel slots.
    """

    if per_class <= 0:
        raise ConfigurationAnalysisError("per_class must be positive")
    excluded_cases = {str(value) for value in excluded_case_ids}
    used_clusters = {str(value) for value in excluded_instance_ids}
    selected: list[dict[str, Any]] = []
    for termination_class in CONFIRMATION_EXIT_CLASSES:
        candidates: list[dict[str, Any]] = []
        for source in evidence_rows:
            row = dict(source)
            case_id = str(row.get("case_id", row.get("run_id", "")))
            instance_id = str(row.get("instance_id", ""))
            category = str(row.get("category", row.get("repository", "")))
            if not case_id or not instance_id or not category:
                continue
            if case_id in excluded_cases or instance_id in used_clusters or instance_id == HOLDOUT_INSTANCE_ID:
                continue
            if _termination_class(row) != termination_class:
                continue
            row["case_id"] = case_id
            row["instance_id"] = instance_id
            row["category"] = category
            row["selection_hash"] = _selection_hash(seed=seed, termination_class=termination_class, row=row)
            candidates.append(row)
        if not candidates:
            raise ConfigurationAnalysisError(f"no candidates available for {termination_class}")

        # Pick the hash-minimum unused row in each category, then rank those
        # representatives.  The second pass is a defensive fallback for
        # sparse synthetic fixtures; real source data has many categories.
        by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in candidates:
            by_category[str(row["category"])].append(row)
        representatives = [min(rows, key=lambda item: (item["selection_hash"], item["case_id"])) for rows in by_category.values()]
        representatives.sort(key=lambda item: (item["selection_hash"], item["case_id"]))
        class_selected: list[dict[str, Any]] = []
        selected_categories: set[str] = set()
        for row in representatives:
            if row["instance_id"] in used_clusters or row["category"] in selected_categories:
                continue
            class_selected.append(row)
            selected_categories.add(str(row["category"]))
            used_clusters.add(str(row["instance_id"]))
            if len(class_selected) == per_class:
                break
        if len(class_selected) < per_class:
            # This path is only reachable when fewer than ``per_class``
            # categories remain after cluster exclusion.  Preserve the
            # distinct-cluster guarantee while making the failure explicit if
            # the source cannot satisfy the category requirement.
            for row in sorted(candidates, key=lambda item: (item["selection_hash"], item["case_id"])):
                if row["instance_id"] in used_clusters:
                    continue
                class_selected.append(row)
                used_clusters.add(str(row["instance_id"]))
                if len(class_selected) == per_class:
                    break
        if len(class_selected) != per_class:
            raise ConfigurationAnalysisError(
                f"{termination_class} selection has {len(class_selected)} rows; expected {per_class}"
            )
        for rank, row in enumerate(class_selected, 1):
            selected.append(
                {
                    **row,
                    "panel_role": termination_class,
                    "selection_rank": rank,
                    "selection_seed": seed,
                }
            )
    return selected


def _source_case_spec_hash(case_spec: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(case_spec).encode("utf-8"))


def _panel_case_id(*, case_id: str, panel_role: str, seed: str) -> str:
    identity = {"case_id": case_id, "panel_role": panel_role, "seed": seed}
    return "configuration-confirmation-panel-v1:" + sha256_bytes(canonical_json(identity).encode("utf-8"))


def _candidate_case_id(*, panel_case_id: str, candidate_id: str) -> str:
    identity = {"candidate_id": candidate_id, "panel_case_id": panel_case_id}
    return "configuration-confirmation-case-v1:" + sha256_bytes(canonical_json(identity).encode("utf-8"))


def _candidate_case_spec_hash(*, source_case_spec: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    return sha256_bytes(
        canonical_json({"candidate_id": candidate["candidate_id"], "settings": candidate["settings"], "serving_configuration": candidate["serving_configuration"], "source_case_spec": source_case_spec}).encode("utf-8")
    )


def build_confirmation_panel(
    *,
    canonical_rows: Sequence[Mapping[str, Any]],
    evidence_rows: Sequence[Mapping[str, Any]],
    existing_pilot_cases: Sequence[Mapping[str, Any]],
    source_case_specs: Mapping[str, Mapping[str, Any]] | None = None,
    source_binding: Mapping[str, Any] | None = None,
    historical_selection_evidence: Mapping[str, Any] | None = None,
    seed: str = CONFIRMATION_PANEL_SEED,
) -> dict[str, Any]:
    """Build the immutable 24-instance/96-case confirmation manifest."""

    if len(existing_pilot_cases) != CONFIRMATION_PANEL_EXISTING_COUNT:
        raise ConfigurationAnalysisError(
            f"expected {CONFIRMATION_PANEL_EXISTING_COUNT} existing pilot cases, got {len(existing_pilot_cases)}"
        )
    canonical_by_case = {
        str(row.get("run_id", row.get("case_id", ""))): dict(row)
        for row in canonical_rows
        if str(row.get("instance_id", "")) != HOLDOUT_INSTANCE_ID
    }
    if len(canonical_by_case) != len([row for row in canonical_rows if str(row.get("instance_id", "")) != HOLDOUT_INSTANCE_ID]):
        raise ConfigurationAnalysisError("canonical rows have duplicate case identities")
    evidence_by_case = {
        str(row.get("case_id", row.get("run_id", ""))): dict(row)
        for row in evidence_rows
        if str(row.get("instance_id", "")) != HOLDOUT_INSTANCE_ID
    }
    if len(evidence_by_case) != len([row for row in evidence_rows if str(row.get("instance_id", "")) != HOLDOUT_INSTANCE_ID]):
        raise ConfigurationAnalysisError("termination evidence has duplicate case identities")
    specs = {str(key): dict(value) for key, value in (source_case_specs or {}).items()}

    panel_sources: list[dict[str, Any]] = []
    existing_case_ids: set[str] = set()
    existing_instances: set[str] = set()
    for pilot in existing_pilot_cases:
        pilot = dict(pilot)
        case_id = str(pilot.get("case_id", pilot.get("resume_key", "")))
        if not case_id or case_id in existing_case_ids:
            raise ConfigurationAnalysisError(f"duplicate/missing existing pilot case: {case_id!r}")
        canonical = canonical_by_case.get(case_id)
        if canonical is None:
            raise ConfigurationAnalysisError(f"pilot case is not a non-holdout canonical baseline: {case_id}")
        instance_id = str(canonical.get("instance_id", ""))
        if instance_id in existing_instances:
            raise ConfigurationAnalysisError(f"existing pilot repeats cluster: {instance_id}")
        evidence = evidence_by_case.get(case_id)
        if evidence is None:
            raise ConfigurationAnalysisError(f"missing termination evidence for pilot case: {case_id}")
        source_spec = deepcopy(specs.get(case_id, pilot))
        if source_spec.get("resume_key", case_id) != case_id:
            raise ConfigurationAnalysisError(f"source case spec resume_key mismatch for {case_id}")
        panel_sources.append(
            {
                "historical_template_case_id": case_id,
                "panel_role": "existing_instrumentation_pilot",
                "selection_hash": None,
                "selection_rank": None,
                "selection_seed": None,
                "canonical": canonical,
                "evidence": evidence,
                "source_case_spec": source_spec,
                "pilot_case_id": pilot.get("pilot_case_id"),
            }
        )
        existing_case_ids.add(case_id)
        existing_instances.add(instance_id)

    additions = select_confirmation_additions(
        evidence_rows,
        excluded_case_ids=existing_case_ids,
        excluded_instance_ids=existing_instances,
        seed=seed,
        per_class=CONFIRMATION_PANEL_ADDITIONAL_PER_CLASS,
    )
    for addition in additions:
        case_id = str(addition["case_id"])
        canonical = canonical_by_case.get(case_id)
        if canonical is None:
            raise ConfigurationAnalysisError(f"selected addition is not a canonical non-holdout row: {case_id}")
        source_spec = deepcopy(specs.get(case_id, {
            "record_type": "case",
            "resume_key": case_id,
            "suite": canonical.get("suite"),
            "repository": canonical.get("repository"),
            "instance_id": canonical.get("instance_id"),
            "cell_id": "shared-baseline",
            "roles": ["step_1_baseline"],
            "settings": deepcopy(BASELINE_SETTINGS),
        }))
        if source_spec.get("resume_key", case_id) != case_id:
            raise ConfigurationAnalysisError(f"source case spec resume_key mismatch for selected case {case_id}")
        panel_sources.append(
            {
                "historical_template_case_id": case_id,
                "panel_role": str(addition["panel_role"]),
                "selection_hash": addition["selection_hash"],
                "selection_rank": addition["selection_rank"],
                "selection_seed": addition["selection_seed"],
                "canonical": canonical,
                "evidence": evidence_by_case[case_id],
                "source_case_spec": source_spec,
                "pilot_case_id": None,
            }
        )

    if len(panel_sources) != CONFIRMATION_PANEL_COUNT:
        raise ConfigurationAnalysisError(f"confirmation panel has {len(panel_sources)} rows, expected {CONFIRMATION_PANEL_COUNT}")
    if len({str(item["canonical"].get("instance_id")) for item in panel_sources}) != CONFIRMATION_PANEL_COUNT:
        raise ConfigurationAnalysisError("confirmation panel contains duplicate instance clusters")

    candidate_records = [_candidate_record(candidate) for candidate in CONFIGURATION_CANDIDATES]
    panel_instances: list[dict[str, Any]] = []
    candidate_cases: list[dict[str, Any]] = []
    for source in panel_sources:
        canonical = source["canonical"]
        evidence = source["evidence"]
        case_id = str(source["historical_template_case_id"])
        panel_id = _panel_case_id(case_id=case_id, panel_role=str(source["panel_role"]), seed=seed)
        source_spec = source["source_case_spec"]
        base = {
            "panel_case_id": panel_id,
            "panel_role": source["panel_role"],
            "historical_template_case_id": case_id,
            "resume_key": source_spec.get("resume_key", case_id),
            "pilot_case_id": source["pilot_case_id"],
            "suite": canonical.get("suite"),
            "repository": canonical.get("repository"),
            "category": canonical.get("category", canonical.get("repository")),
            "instance_id": canonical.get("instance_id"),
            "cluster_id": f"instance:{canonical.get('instance_id')}",
            "source_case_spec": source_spec,
            "source_case_spec_sha256": _source_case_spec_hash(source_spec),
            "termination_evidence": {
                key: evidence.get(key)
                for key in (
                    "case_id", "native_exit_class", "termination_class", "status", "source_root",
                    "model_event_count", "tool_event_count", "prompt_tokens_max", "prompt_ge_client_limit",
                    "output_truncation_responses", "finish_reason", "evidence_sha256",
                )
                if key in evidence
            },
            "selection_hash": source["selection_hash"],
            "selection_rank": source["selection_rank"],
            "selection_seed": source["selection_seed"],
        }
        panel_instances.append(base)
        for candidate in candidate_records:
            case_spec_hash = _candidate_case_spec_hash(source_case_spec=source_spec, candidate=candidate)
            candidate_case = {
                "candidate_case_id": _candidate_case_id(panel_case_id=panel_id, candidate_id=candidate["candidate_id"]),
                "panel_case_id": panel_id,
                "historical_template_case_id": case_id,
                "resume_key": base["resume_key"],
                "panel_role": base["panel_role"],
                "suite": base["suite"],
                "repository": base["repository"],
                "category": base["category"],
                "instance_id": base["instance_id"],
                "cluster_id": base["cluster_id"],
                "candidate_id": candidate["candidate_id"],
                "settings": deepcopy(candidate["settings"]),
                "final_configuration": deepcopy(candidate["settings"]),
                "settings_sha256": candidate["settings_sha256"],
                "serving_configuration": deepcopy(candidate["serving_configuration"]),
                "source_case_spec_sha256": base["source_case_spec_sha256"],
                "case_spec_sha256": case_spec_hash,
                "termination_evidence_sha256": evidence.get("evidence_sha256"),
            }
            candidate_cases.append(candidate_case)

    candidate_cases.sort(key=lambda item: (item["panel_case_id"], item["candidate_id"]))
    panel_instances.sort(key=lambda item: item["panel_case_id"])
    if len(candidate_cases) != CONFIRMATION_CASE_COUNT:
        raise ConfigurationAnalysisError(f"confirmation case count {len(candidate_cases)} != {CONFIRMATION_CASE_COUNT}")
    if len({item["candidate_case_id"] for item in candidate_cases}) != CONFIRMATION_CASE_COUNT:
        raise ConfigurationAnalysisError("confirmation candidate case IDs are not unique")
    role_counts = Counter(item["panel_role"] for item in panel_instances)
    if role_counts != Counter({"existing_instrumentation_pilot": 16, "exit_cost": 4, "exit_context": 4}):
        raise ConfigurationAnalysisError(f"unexpected confirmation panel roles: {role_counts}")

    return {
        "schema_version": CONFIRMATION_PANEL_SCHEMA_VERSION,
        "snapshot_id": SNAPSHOT_ID,
        "status": "complete_offline_preselection",
        "panel": {
            "instance_count": len(panel_instances),
            "candidate_case_count": len(candidate_cases),
            "existing_instrumentation_pilot_count": 16,
            "additional_exit_cost_count": 4,
            "additional_exit_context_count": 4,
            "cluster_unit": "instance_id",
            "holdout_instance_id": HOLDOUT_INSTANCE_ID,
            "holdout_accessed": False,
            "d9_fitting": False,
            "final_run_outcomes_used": False,
            "instances": panel_instances,
        },
        "candidates": candidate_records,
        "candidate_cases": candidate_cases,
        "selection": {
            "algorithm": "sha256_rank_by_termination_class_and_repository_category_v1",
            "seed": seed,
            "identity_fields": ["case_id", "suite", "repository", "category", "instance_id", "termination_class"],
            "historical_failure_classes": list(CONFIRMATION_EXIT_CLASSES),
            "category_diversity": "four distinct repository categories in each added class when source permits",
            "existing_pilot_excluded_before_ranking": True,
            "holdout_excluded_before_ranking": True,
            "selection_is_not_evaluator_or_d9_fit": True,
        },
        "configuration_selection_rule": {
            "historical_outcomes_authorized_for_selection": True,
            "primary": "highest paired resolved count",
            "regression_review": "inspect per-case regressions before any tie break",
            "tie_breakers": ["fewer failures/context exits", "less budget exhaustion", "resource cost"],
            "final_decision_owner": "Astra",
            "automatic_final1088_optimization": False,
            "final1088_outcomes_may_inform_selection": False,
        },
        "natural_pilot_reuse": {
            "existing_case_count": 16,
            "eligible_only_if": [
                "selected candidate final_configuration exactly matches the preselected natural run",
                "instrumentation recorder/source and schema hashes exactly match",
                "serving/model/evaluator pin bindings exactly match",
            ],
            "otherwise": "do not reuse as pilot evidence; rerun or report unavailable",
        },
        "production": {
            "plan_id": "assignment-production-v2-20260908",
            "fresh_case_ids_required": True,
            "full_case_count": 1088,
            "call_limit_grid": list(PRODUCTION_CALL_LIMIT_GRID),
            "other_sweep_grids_unchanged": True,
            "historical_template_case_ids_overwritten": False,
        },
        "pins": {
            "model": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "tokenizer_revision": TOKENIZER_REVISION,
            "swe_agent_revision": SWE_AGENT_REVISION,
            "swe_bench_revision": SWE_BENCH_REVISION,
            "vllm_version": VLLM_VERSION,
            "serving_configuration": {"max_model_len": SERVING_MAX_MODEL_LEN},
            "client_input_field": "max_input_tokens",
            "remote_verification": "unresolved_offline",
        },
        "historical_selection_evidence": dict(historical_selection_evidence or {
            "status": "not_supplied",
            "d9_fitting": False,
            "final_run_outcomes": False,
        }),
        "source_binding": dict(source_binding or {}),
    }


def _load_json(path: Path) -> tuple[Any, str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), sha256_bytes(raw)


def _load_jsonl_records(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            raise ConfigurationAnalysisError(f"blank JSONL line: {path}:{line_no}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ConfigurationAnalysisError(f"JSONL line is not object: {path}:{line_no}")
        rows.append(value)
    return rows, sha256_bytes(raw)


def _load_full_case_specs(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    records, source_sha = _load_jsonl_records(path)
    specs: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("record_type") != "case":
            continue
        case_id = str(record.get("resume_key", ""))
        if not case_id or case_id in specs:
            raise ConfigurationAnalysisError(f"duplicate/missing inventory resume_key: {case_id!r}")
        specs[case_id] = record
    return specs, source_sha


def _load_termination_index(path: Path) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
    value, source_sha = _load_json(path)
    if not isinstance(value, dict):
        raise ConfigurationAnalysisError("termination evidence index must be an object")
    rows = value.get("cases")
    if not isinstance(rows, list):
        raise ConfigurationAnalysisError("termination evidence index cases must be a list")
    if value.get("holdout_accessed") is True:
        raise ConfigurationAnalysisError("termination evidence index claims holdout access")
    return [dict(row) for row in rows], source_sha, value


def _pin_manifest_summary(*, pin_evidence_path: Path, evaluator_config_path: Path, run_manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pin_evidence, pin_sha = _load_json(pin_evidence_path)
    evaluator_config, evaluator_sha = _load_json(evaluator_config_path)
    run_manifest, run_sha = _load_json(run_manifest_path)
    model = pin_evidence.get("model", {})
    swe_agent = pin_evidence.get("swe_agent", {})
    swe_bench = pin_evidence.get("swe_bench", {})
    vllm = pin_evidence.get("vllm", {})
    evaluator = evaluator_config.get("adapter", {})
    summary = {
        "model": MODEL_NAME,
        "model_revision": model.get("revision", MODEL_REVISION),
        "tokenizer_revision": model.get("tokenizer_revision", TOKENIZER_REVISION),
        "swe_agent_revision": swe_agent.get("head", SWE_AGENT_REVISION),
        "swe_bench_revision": swe_bench.get("head", SWE_BENCH_REVISION),
        "vllm_version": vllm.get("version", VLLM_VERSION),
        "serving_configuration": {"max_model_len": SERVING_MAX_MODEL_LEN},
        "evaluator": {
            "module": evaluator_config.get("runner", {}).get("module"),
            "adapter_sha256": evaluator.get("sha256"),
            "swe_bench_revision": swe_bench.get("head", SWE_BENCH_REVISION),
        },
        "remote_verification": "unresolved_offline",
    }
    source_binding = {
        "pin_evidence": {"path": str(pin_evidence_path), "sha256": pin_sha},
        "evaluator_config": {"path": str(evaluator_config_path), "sha256": evaluator_sha},
        "run_manifest": {"path": str(run_manifest_path), "sha256": run_sha},
    }
    baseline = run_manifest.get("baseline_configuration", {})
    if baseline.get("serving_max_model_len") not in (None, SERVING_MAX_MODEL_LEN):
        raise ConfigurationAnalysisError("run manifest serving context does not bind max_model_len=65536")
    if summary["model_revision"] != MODEL_REVISION or summary["swe_agent_revision"] != SWE_AGENT_REVISION or summary["vllm_version"] != VLLM_VERSION:
        raise ConfigurationAnalysisError("local pin evidence does not match frozen candidate pins")
    return summary, source_binding


def render_confirmation_markdown(panel: Mapping[str, Any]) -> str:
    lines = [
        f"# Configuration confirmation panel ({panel['snapshot_id']})",
        "",
        "This offline manifest predeclares four complete configurations for a 24-instance development confirmation panel. Each candidate's `final_configuration` contains exactly `call_limit`, `max_output_tokens`, `observation_length`, `temperature`, `max_input_tokens`, `top_p`, and `seed`; `serving_configuration.max_model_len` is bound separately. It is a selection aid; it does not optimize or alter the fresh 1,088-case production matrix.",
        "",
        "| candidate | calls | client input guard | output cap | observation | temperature | top_p | seed | serving max_model_len | settings hash |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for candidate in panel["candidates"]:
        settings = candidate["settings"]
        serving = candidate["serving_configuration"]
        lines.append(
            f"| `{candidate['candidate_id']}` | {settings['call_limit']} | {settings['max_input_tokens']} | {settings['max_output_tokens']} | {settings['observation_length']} | {settings['temperature']:.1f} | {settings['top_p']:.1f} | {settings['seed']} | {serving['max_model_len']} | `{candidate['settings_sha256']}` |"
        )
    lines += [
        "",
        f"The panel contains {panel['panel']['instance_count']} distinct instance clusters and exactly {panel['panel']['candidate_case_count']} candidate cases (four settings per instance): 16 existing category-balanced pilot identities, four historical `exit_cost` identities, and four historical `exit_context` identities. The sealed `sympy__sympy-12481` cluster is excluded before ranking and was not accessed.",
        "",
        "| panel role | count |",
        "|---|---:|",
    ]
    for role, count in sorted(Counter(item["panel_role"] for item in panel["panel"]["instances"]).items()):
        lines.append(f"| `{role}` | {count} |")
    lines += [
        "",
        "The added rows are ranked by a SHA-256 identity hash within each historical termination class, with one representative from each of four repository categories. Existing pilot rows are excluded before ranking and all rows are bound to their original baseline `resume_key`, full case specification hash, and termination evidence hash.",
        "",
        "Astra selects after the live confirmation evidence by highest paired resolved count, reviewing regressions first; ties use fewer failures/context exits, less budget exhaustion, and then resource cost. The generator does not automatically select a candidate, fit D9, or use final 1,088 outcomes.",
        "",
        "The 16 existing natural pilot trajectories can be reused only for the selected candidate when final configuration, recorder/schema, serving/model/evaluator pins, and source hashes all match exactly; otherwise they are unavailable as pilot evidence.",
        "",
        "The production call-limit grid is `[20, 30, 50, 100]`; the other sweep grids remain unchanged. Production receives fresh case IDs under `assignment-production-v2-20260908`. The historical 25,000 observation point remains an original one-factor sweep reference; the fourth candidate directly tests its interaction with calls=100 and the expanded client input guard, so no result is inferred from the historical point.",
        "",
        "Local Qwen, SWE-agent, SWE-bench, evaluator-adapter, and vLLM evidence is hash-bound in `source_binding`; remote verification remains unresolved until access is restored.",
        "",
    ]
    return "\n".join(lines)


def write_confirmation_panel(
    *,
    trajectories_path: Path,
    pilot_cases_path: Path,
    termination_evidence_index_path: Path,
    full_matrix_inventory_path: Path,
    output_dir: Path,
    pin_evidence_path: Path | None = None,
    evaluator_config_path: Path | None = None,
    run_manifest_path: Path | None = None,
    historical_configuration_report_path: Path | None = None,
    seed: str = CONFIRMATION_PANEL_SEED,
) -> dict[str, Any]:
    """Write the four-candidate, 24-instance, 96-case confirmation bundle."""

    canonical_rows, canonical_fields, canonical_sha = _read_csv(trajectories_path)
    pilot_value, pilot_sha = _load_json(pilot_cases_path)
    if not isinstance(pilot_value, dict) or not isinstance(pilot_value.get("cases"), list):
        raise ConfigurationAnalysisError("pilot cases must contain a cases list")
    evidence_rows, evidence_sha, evidence_manifest = _load_termination_index(termination_evidence_index_path)
    specs, inventory_sha = _load_full_case_specs(full_matrix_inventory_path)
    source_binding: dict[str, Any] = {
        "canonical_trajectories": {"path": str(trajectories_path), "sha256": canonical_sha, "row_count": len(canonical_rows), "fields": canonical_fields},
        "pilot_cases": {"path": str(pilot_cases_path), "sha256": pilot_sha, "case_count": len(pilot_value["cases"])},
        "termination_evidence_index": {"path": str(termination_evidence_index_path), "sha256": evidence_sha, "case_count": len(evidence_rows)},
        "full_matrix_case_inventory": {"path": str(full_matrix_inventory_path), "sha256": inventory_sha, "case_count": len(specs)},
        "generator": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__).resolve())},
    }
    if pin_evidence_path and evaluator_config_path and run_manifest_path:
        pins, pin_sources = _pin_manifest_summary(
            pin_evidence_path=pin_evidence_path,
            evaluator_config_path=evaluator_config_path,
            run_manifest_path=run_manifest_path,
        )
        source_binding.update(pin_sources)
    else:
        pins = {
            "model": MODEL_NAME,
            "model_revision": MODEL_REVISION,
            "tokenizer_revision": TOKENIZER_REVISION,
            "swe_agent_revision": SWE_AGENT_REVISION,
            "swe_bench_revision": SWE_BENCH_REVISION,
            "vllm_version": VLLM_VERSION,
            "serving_configuration": {"max_model_len": SERVING_MAX_MODEL_LEN},
            "remote_verification": "unresolved_offline",
        }

    historical_selection_evidence: dict[str, Any] = {
        "status": "not_supplied",
        "d9_fitting": False,
        "final_run_outcomes": False,
    }
    if historical_configuration_report_path is not None:
        report, report_sha = _load_json(historical_configuration_report_path)
        source_binding["historical_configuration_report"] = {"path": str(historical_configuration_report_path), "sha256": report_sha}
        settings_by_label = {item["setting"]["label"]: item for item in report.get("settings", [])}
        historical_selection_evidence = {
            "status": "descriptive_historical_reference",
            "source_report_sha256": report_sha,
            "d9_fitting": False,
            "final_run_outcomes": False,
            "outcome_access_authorized": True,
            "candidate_reference": {
                "historical-control-call30-input32768": {
                    "matched_historical_setting": "baseline shared rows",
                    "pair_count": report.get("method", {}).get("pair_count_per_setting"),
                    "baseline_resolved_count": report.get("settings", [])[0].get("baseline_resolved_count") if report.get("settings") else None,
                    "status": "historical baseline reference; client input guard expansion is unobserved",
                },
                "expanded-call50-input61440": {
                    "matched_historical_setting": "call_limit=50",
                    "pair_count": settings_by_label.get("call_limit=50", {}).get("pair_count"),
                    "treatment_resolved_count": settings_by_label.get("call_limit=50", {}).get("treatment_resolved_count"),
                    "both_resolved_count": settings_by_label.get("call_limit=50", {}).get("both_resolved_count"),
                    "discordance_count": settings_by_label.get("call_limit=50", {}).get("discordance_count"),
                    "status": "historical call-limit reference only; client input guard expansion is unobserved",
                },
                "expanded-call100-input61440": {
                    "matched_historical_setting": None,
                    "status": "no historical call_limit=100 observation; live confirmation required",
                },
            },
        }
    source_binding["termination_evidence_manifest"] = {
        "schema_version": evidence_manifest.get("schema_version"),
        "holdout_accessed": evidence_manifest.get("holdout_accessed", False),
        "native_case_count": evidence_manifest.get("case_count", len(evidence_rows)),
    }
    panel = build_confirmation_panel(
        canonical_rows=canonical_rows,
        evidence_rows=evidence_rows,
        existing_pilot_cases=pilot_value["cases"],
        source_case_specs=specs,
        source_binding=source_binding,
        historical_selection_evidence=historical_selection_evidence,
        seed=seed,
    )
    panel["pins"] = pins
    output_dir.mkdir(parents=True, exist_ok=True)
    panel_sha = _write_json(output_dir / "CONFIGURATION_CONFIRMATION_PANEL.json", panel)
    markdown_sha = _write_hashed(output_dir / "CONFIGURATION_CONFIRMATION_PANEL.md", render_confirmation_markdown(panel).encode("utf-8"))
    cases_path = output_dir / "configuration_confirmation_cases.jsonl"
    case_payload = "".join(canonical_json(row) + "\n" for row in panel["candidate_cases"])
    cases_sha = _write_hashed(cases_path, case_payload.encode("utf-8"))
    return {
        "panel_sha256": panel_sha,
        "markdown_sha256": markdown_sha,
        "cases_sha256": cases_sha,
        "panel_instances": panel["panel"]["instance_count"],
        "candidate_cases": panel["panel"]["candidate_case_count"],
        "candidate_configurations": len(panel["candidates"]),
        "output_dir": str(output_dir),
    }


def _validate_baseline_copy(
    *,
    sweep_row: Mapping[str, str],
    metadata: Mapping[str, Any],
    baseline: Mapping[str, str],
) -> None:
    """Check a derived plotting copy against its canonical baseline row."""

    for field in ("status", "official_resolved", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms"):
        if sweep_row.get(field) != baseline.get(field):
            raise ConfigurationAnalysisError(
                f"shared-baseline plotting copy {metadata.get('run_id')} differs from canonical {baseline.get('run_id')} in {field}"
            )


def load_paired_rows(
    *,
    trajectories_path: Path,
    sweep_runs_path: Path,
    sweep_metadata_path: Path,
    evaluator_provenance_path: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trajectories, trajectory_fields, trajectory_sha = _read_csv(trajectories_path)
    sweep_rows, sweep_fields, sweep_sha = _read_csv(sweep_runs_path)
    metadata_header, metadata_rows, metadata_sha = _read_metadata(sweep_metadata_path)
    if len(trajectories) != 800:
        raise ConfigurationAnalysisError(f"expected 800 canonical trajectories, got {len(trajectories)}")
    if len(sweep_rows) != 384 or len(metadata_rows) != 384:
        raise ConfigurationAnalysisError(f"expected 384 sweep rows and metadata rows, got {len(sweep_rows)} and {len(metadata_rows)}")
    if metadata_header.get("source_trajectories_sha256") not in (None, trajectory_sha):
        raise ConfigurationAnalysisError("sweep metadata does not bind the supplied trajectories.csv")
    if metadata_header.get("source_sweep_runs_sha256") not in (None, sweep_sha):
        raise ConfigurationAnalysisError("sweep metadata does not bind the supplied sweep_runs.csv")
    if metadata_header.get("source_plan_sha256") not in (None, IMMUTABLE_PLAN_SHA256):
        raise ConfigurationAnalysisError("sweep metadata does not bind the immutable original plan")

    trajectory_by_run: dict[str, dict[str, str]] = {}
    trajectory_by_key: dict[tuple[str, str], dict[str, str]] = {}
    holdout_rows = [row for row in trajectories if row.get("instance_id") == HOLDOUT_INSTANCE_ID]
    if len(holdout_rows) != 2 or {row.get("suite") for row in holdout_rows} != {"lite", "verified"}:
        raise ConfigurationAnalysisError("the two-suite SymPy holdout copies are not present")
    # Drop holdout rows before reading outcome/timing fields.  They remain
    # source-bound by the full-file SHA and row count, but no final label is
    # used by the panel.
    trajectories_for_panel = [row for row in trajectories if row.get("instance_id") != HOLDOUT_INSTANCE_ID]
    for row in trajectories_for_panel:
        run_id = row.get("run_id", "")
        key = (row.get("suite", ""), row.get("instance_id", ""))
        if not run_id or run_id in trajectory_by_run or key in trajectory_by_key:
            raise ConfigurationAnalysisError(f"duplicate trajectory identity: {run_id} / {key}")
        trajectory_by_run[run_id] = row
        trajectory_by_key[key] = row
    if len(trajectory_by_key) != 798:
        raise ConfigurationAnalysisError("non-holdout trajectory suite/instance identities are not unique")

    sweep_by_run = {}
    for row in sweep_rows:
        run_id = row.get("run_id", "")
        if not run_id or run_id in sweep_by_run:
            raise ConfigurationAnalysisError(f"duplicate sweep run_id: {run_id}")
        sweep_by_run[run_id] = row

    # Bind every metadata row to its sweep row and every baseline plotting copy
    # to the canonical trajectory.  The plotting copies remain auditable but
    # are never treated as treatment observations.
    treatments: list[dict[str, Any]] = []
    baseline_copy_count = 0
    metadata_ids = set()
    for metadata in metadata_rows:
        run_id = str(metadata.get("run_id", ""))
        if not run_id or run_id in metadata_ids:
            raise ConfigurationAnalysisError(f"duplicate/missing metadata run_id: {run_id}")
        metadata_ids.add(run_id)
        if run_id not in sweep_by_run:
            raise ConfigurationAnalysisError(f"metadata run_id missing from sweep_runs.csv: {run_id}")
        sweep = sweep_by_run[run_id]
        suite = str(metadata.get("suite", ""))
        instance_id = str(metadata.get("instance_id", ""))
        key = (suite, instance_id)
        baseline = trajectory_by_key.get(key)
        if baseline is None:
            raise ConfigurationAnalysisError(f"no canonical baseline for metadata pair {key}")
        if metadata.get("sweep_parameter") != sweep.get("sweep_parameter") or str(metadata.get("sweep_value")) != str(sweep.get("sweep_value")):
            raise ConfigurationAnalysisError(f"metadata/sweep parameter mismatch for {run_id}")
        if metadata.get("config_id") == "shared-baseline":
            baseline_copy_count += 1
            underlying_id = run_id.split("::", 1)[0]
            if underlying_id != baseline.get("run_id"):
                raise ConfigurationAnalysisError(f"baseline plotting copy {run_id} does not point to canonical {baseline.get('run_id')}")
            _validate_baseline_copy(sweep_row=sweep, metadata=metadata, baseline=baseline)
            continue
        knob = str(metadata.get("sweep_parameter", ""))
        value = _setting_value(knob, str(metadata.get("sweep_value", "")))
        if (knob, value) not in SETTING_ORDER:
            raise ConfigurationAnalysisError(f"unexpected treatment setting {(knob, value)}")
        if metadata.get("config_id") != f"{knob}={metadata.get('sweep_value')}":
            raise ConfigurationAnalysisError(f"unexpected treatment config_id for {run_id}")
        if instance_id == HOLDOUT_INSTANCE_ID:
            raise ConfigurationAnalysisError("sealed holdout entered configuration panel")
        treatments.append({
            "setting": {"knob": knob, "value": value, "label": f"{knob}={metadata.get('sweep_value')}"},
            "suite": suite,
            "repository": metadata.get("repository"),
            "category": metadata.get("category"),
            "instance_id": instance_id,
            "cluster_id": f"instance:{instance_id}",
            "treatment_run_id": run_id,
            "baseline_run_id": baseline["run_id"],
            "treatment_status": sweep.get("status"),
            "baseline_status": baseline.get("status"),
            "treatment_resolved": _bool(sweep.get("official_resolved", ""), "treatment official_resolved"),
            "baseline_resolved": _bool(baseline.get("official_resolved", ""), "baseline official_resolved"),
            "treatment_e2e_wall_ms": _number(sweep.get("e2e_wall_ms", ""), "treatment e2e_wall_ms"),
            "baseline_e2e_wall_ms": _number(baseline.get("e2e_wall_ms", ""), "baseline e2e_wall_ms"),
            "treatment_tool_wall_ms": _number(sweep.get("tool_wall_ms", ""), "treatment tool_wall_ms"),
            "baseline_tool_wall_ms": _number(baseline.get("tool_wall_ms", ""), "baseline tool_wall_ms"),
            "treatment_model_wall_ms": _number(sweep.get("model_wall_ms", ""), "treatment model_wall_ms"),
            "baseline_model_wall_ms": _number(baseline.get("model_wall_ms", ""), "baseline model_wall_ms"),
            "provenance": metadata.get("provenance"),
        })
    if baseline_copy_count != 96:
        raise ConfigurationAnalysisError(f"expected 96 shared-baseline plotting copies, got {baseline_copy_count}")
    if len(treatments) != 288:
        raise ConfigurationAnalysisError(f"expected 288 treatment pairs, got {len(treatments)}")
    counts = Counter((row["setting"]["knob"], row["setting"]["value"]) for row in treatments)
    for setting, expected in REQUIRED_SETTING_COUNTS.items():
        if counts[setting] != expected:
            raise ConfigurationAnalysisError(f"setting {setting} has {counts[setting]} pairs, expected {expected}")
    source_binding = {
        "trajectories": {"path": str(trajectories_path), "sha256": trajectory_sha, "row_count": len(trajectories), "fields": trajectory_fields},
        "sweep_runs": {"path": str(sweep_runs_path), "sha256": sweep_sha, "row_count": len(sweep_rows), "fields": sweep_fields},
        "sweep_metadata": {"path": str(sweep_metadata_path), "sha256": metadata_sha, "row_count": len(metadata_rows), "header": metadata_header},
        "original_plan": {"sha256": metadata_header.get("source_plan_sha256"), "expected_sha256": IMMUTABLE_PLAN_SHA256},
        "evaluator_provenance": None,
        "historical_holdout": {"instance_id": HOLDOUT_INSTANCE_ID, "accessed": False},
    }
    if evaluator_provenance_path is not None:
        with evaluator_provenance_path.open("r", encoding="utf-8") as handle:
            evaluator_row_count = sum(1 for _ in handle) - 1
        source_binding["evaluator_provenance"] = {
            "path": str(evaluator_provenance_path),
            "sha256": sha256_file(evaluator_provenance_path),
            "row_count": evaluator_row_count,
            "accessed_for_source_binding_only": True,
        }
    return treatments, source_binding


def _metric_values(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pair_count = len(rows)
    discordance = sum(row["treatment_resolved"] != row["baseline_resolved"] for row in rows)
    both_resolved = sum(row["treatment_resolved"] and row["baseline_resolved"] for row in rows)
    treatment_resolved = sum(row["treatment_resolved"] for row in rows)
    baseline_resolved = sum(row["baseline_resolved"] for row in rows)
    faster = sum(row["treatment_e2e_wall_ms"] < row["baseline_e2e_wall_ms"] for row in rows)
    ties = sum(row["treatment_e2e_wall_ms"] == row["baseline_e2e_wall_ms"] for row in rows)
    delta = [row["treatment_e2e_wall_ms"] - row["baseline_e2e_wall_ms"] for row in rows]
    return {
        "pair_count": pair_count,
        "cluster_count": len({row["cluster_id"] for row in rows}),
        "discordance_count": discordance,
        "discordance_rate_pair_weighted": discordance / pair_count if pair_count else None,
        "both_resolved_count": both_resolved,
        "treatment_resolved_count": treatment_resolved,
        "baseline_resolved_count": baseline_resolved,
        "resolution_difference_pair_weighted": (treatment_resolved - baseline_resolved) / pair_count if pair_count else None,
        "faster_count": faster,
        "faster_rate_pair_weighted": faster / pair_count if pair_count else None,
        "tie_count": ties,
        "delta_e2e_wall_ms": {
            "mean": statistics.fmean(delta) if delta else None,
            "median": statistics.median(delta) if delta else None,
            "p05": _percentile(delta, 0.05),
            "p95": _percentile(delta, 0.95),
        },
    }


def _bootstrap(rows: list[dict[str, Any]], *, seed: int, repetitions: int) -> dict[str, Any]:
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_cluster[row["cluster_id"]].append(row)
    clusters = sorted(by_cluster)
    if not clusters:
        raise ConfigurationAnalysisError("cannot bootstrap empty setting")
    rng = random.Random(seed)
    discordance_rates: list[float] = []
    resolution_differences: list[float] = []
    faster_rates: list[float] = []
    mean_deltas: list[float] = []
    median_deltas: list[float] = []
    for _ in range(repetitions):
        sample = [row for _ in range(len(clusters)) for row in by_cluster[rng.choice(clusters)]]
        metrics = _metric_values(sample)
        discordance_rates.append(float(metrics["discordance_rate_pair_weighted"]))
        resolution_differences.append(float(metrics["resolution_difference_pair_weighted"]))
        faster_rates.append(float(metrics["faster_rate_pair_weighted"]))
        mean_deltas.append(float(metrics["delta_e2e_wall_ms"]["mean"]))
        median_deltas.append(float(metrics["delta_e2e_wall_ms"]["median"]))
    alpha = 1.0 - CONFIDENCE
    zero_event_bound = None
    if _metric_values(rows)["discordance_count"] == 0:
        zero_event_bound = 1.0 - alpha ** (1.0 / len(clusters))
    return {
        "method": "deterministic cluster bootstrap with replacement",
        "unit": "instance_id_cluster",
        "cluster_count": len(clusters),
        "clusters": clusters,
        "repetitions": repetitions,
        "seed": seed,
        "confidence": CONFIDENCE,
        "percentile_interval": [alpha / 2.0, 1.0 - alpha / 2.0],
        "resolution_difference_ci": [_percentile(resolution_differences, alpha / 2.0), _percentile(resolution_differences, 1.0 - alpha / 2.0)],
        "discordance_rate_ci": [_percentile(discordance_rates, alpha / 2.0), _percentile(discordance_rates, 1.0 - alpha / 2.0)],
        "faster_rate_ci": [_percentile(faster_rates, alpha / 2.0), _percentile(faster_rates, alpha / 2.0 + CONFIDENCE)],
        "mean_delta_e2e_wall_ms_ci": [_percentile(mean_deltas, alpha / 2.0), _percentile(mean_deltas, 1.0 - alpha / 2.0)],
        "median_delta_e2e_wall_ms_ci": [_percentile(median_deltas, alpha / 2.0), _percentile(median_deltas, 1.0 - alpha / 2.0)],
        "zero_discordance_upper_bound": {
            "value": zero_event_bound,
            "confidence": CONFIDENCE,
            "method": "one-sided exact zero-event finite-sample bound",
            "formula": "1 - alpha^(1/n_clusters)",
            "assumption": "independent instance clusters",
            "interpretation": "empirical bootstrap [0,0] for zero observed discordance cannot estimate unseen discordance; this bound is a model-assumption sensitivity bound, not a noninferiority claim",
        },
    }


def build_report(
    rows: list[dict[str, Any]],
    source_binding: Mapping[str, Any],
    *,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
    bootstrap_repetitions: int = DEFAULT_BOOTSTRAP_REPETITIONS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[tuple[str, int | float], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["setting"]["knob"], row["setting"]["value"])].append(row)
    settings = []
    bootstrap_output = []
    for knob, value in SETTING_ORDER:
        pair_rows = grouped[(knob, value)]
        metrics = _metric_values(pair_rows)
        bootstrap = _bootstrap(pair_rows, seed=_stable_seed(bootstrap_seed, f"{knob}={value}"), repetitions=bootstrap_repetitions)
        pair_ids = [
            {
                "suite": row["suite"],
                "instance_id": row["instance_id"],
                "cluster_id": row["cluster_id"],
                "treatment_run_id": row["treatment_run_id"],
                "baseline_run_id": row["baseline_run_id"],
            }
            for row in sorted(pair_rows, key=lambda item: (item["suite"], item["instance_id"]))
        ]
        setting_report = {
            "setting": {"knob": knob, "value": value, "label": f"{knob}={value}"},
            "comparison": "paired treatment vs shared Step 1 baseline",
            "weighting": {
                "reported_counts_and_rates": "pair_weighted",
                "uncertainty_resampling": "cluster_weighted_by_instance_id",
                "duplicate_instance_id": "django__django-13658 appears in Lite and Verified but is one bootstrap cluster",
            },
            **metrics,
            "bootstrap": bootstrap,
            "source_pair_ids": pair_ids,
        }
        settings.append(setting_report)
        bootstrap_output.append({"setting": setting_report["setting"], **bootstrap})
    headline = next(item for item in settings if item["setting"] == {"knob": "observation_length", "value": 25000, "label": "observation_length=25000"})
    expected = {"pair_count": 24, "cluster_count": 23, "discordance_count": 0, "both_resolved_count": 16, "faster_count": 13}
    for key, expected_value in expected.items():
        if headline[key] != expected_value:
            raise ConfigurationAnalysisError(f"25,000 headline {key}={headline[key]}, expected {expected_value}")
    report = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": SNAPSHOT_ID,
        "status": "complete_offline_descriptive",
        "method": {
            "baseline": BASELINE_SETTINGS,
            "settings_count": len(settings),
            "pairing": "each non-baseline sweep row paired to same-suite/same-instance canonical shared-baseline trajectory",
            "plotting_baseline_copies": {"count": 96, "new_runs": 0, "checked_against_canonical": True},
            "holdout": {"instance_id": HOLDOUT_INSTANCE_ID, "accessed": False, "included": False},
            "pair_count_per_setting": 24,
            "cluster_count_per_setting": 23,
            "bootstrap": "deterministic with replacement over instance_id clusters",
        },
        "baseline_settings": BASELINE_SETTINGS,
        "settings": settings,
        "headline_observation_length_25000": {
            "pair_count": headline["pair_count"],
            "cluster_count": headline["cluster_count"],
            "discordance_count": headline["discordance_count"],
            "both_resolved_count": headline["both_resolved_count"],
            "faster_count": headline["faster_count"],
            "statement": "At observation_length=25000, 0/24 pairs discorded, 16/24 had both resolved, and treatment was faster in 13/24 pairs.",
        },
        "interpretation": {
            "zero_discordance": "Zero discordance is the observed sample result. The empirical bootstrap interval can be degenerate at [0,0] and cannot estimate unseen discordance.",
            "finite_sample_sensitivity": "The report includes a conservative one-sided zero-event upper bound under the explicitly labeled independent-cluster assumption.",
            "noninferiority": "No noninferiority, equivalence, or generalization claim is made from this descriptive panel.",
            "selection": "The 25,000 point remains an original one-factor sweep treatment and is not a pilot instrumentation baseline or separate optimization experiment.",
        },
        "source_binding": source_binding,
    }
    return report, bootstrap_output


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Configuration analysis ({report['snapshot_id']})",
        "",
        "This is a descriptive, offline analysis of the preserved 12 non-baseline settings, each paired to its same-instance shared Step 1 baseline.",
        "",
        "The 96 shared-baseline rows in the sweep view were checked against canonical trajectories and counted as plotting copies, not executions. Counts and rates are pair-weighted; bootstrap intervals resample 23 `instance_id` clusters per setting, keeping the duplicate `django__django-13658` suite rows in one cluster.",
        "",
        "| setting | pairs | clusters | discordance | both resolved | faster | median Δ E2E ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["settings"]:
        median_delta = item["delta_e2e_wall_ms"]["median"]
        lines.append(
            f"| `{item['setting']['label']}` | {item['pair_count']} | {item['cluster_count']} | {item['discordance_count']} | {item['both_resolved_count']} | {item['faster_count']} | {median_delta:.3f} |"
        )
    lines += [
        "",
        "The following are deterministic percentile 95% cluster-bootstrap intervals (10,000 repetitions in the snapshot; values are pair-weighted within each resampled cluster sample).",
        "",
        "| setting | mean Δ E2E ms 95% CI | median Δ E2E ms 95% CI | resolution difference 95% CI | discordance rate 95% CI | zero-event upper bound |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    def interval(values):
        return "[%.3f, %.3f]" % (values[0], values[1])

    for item in report["settings"]:
        boot = item["bootstrap"]
        bound = boot["zero_discordance_upper_bound"]["value"]
        lines.append(
            f"| `{item['setting']['label']}` | {interval(boot['mean_delta_e2e_wall_ms_ci'])} | {interval(boot['median_delta_e2e_wall_ms_ci'])} | {interval(boot['resolution_difference_ci'])} | {interval(boot['discordance_rate_ci'])} | {('%.3f' % bound) if bound is not None else '—'} |"
        )
    headline = report["headline_observation_length_25000"]
    lines += [
        "",
        f"For `observation_length=25000`: {headline['discordance_count']}/24 discordant, {headline['both_resolved_count']}/24 both resolved, and {headline['faster_count']}/24 faster.",
        "",
        "Zero discordance is an observed result only. The empirical bootstrap can be `[0, 0]` when no discordance is observed and cannot estimate unseen discordance; the report therefore also gives `1 - alpha^(1/n_clusters)` as a one-sided zero-event bound under the stated independent-cluster assumption. No noninferiority or equivalence claim is made.",
        "",
        "The 25,000 point is retained only as an original one-factor sweep treatment. It is excluded from pilot instrumentation validation and from a separate optimization experiment. The sealed SymPy holdout was not accessed.",
        "",
        "Source hashes and row counts are in `CONFIGURATION_ANALYSIS.json`; paired run identities are included under each setting.",
        "",
    ]
    return "\n".join(lines)


def write_analysis(
    *,
    trajectories_path: Path,
    sweep_runs_path: Path,
    sweep_metadata_path: Path,
    output_dir: Path,
    evaluator_provenance_path: Path | None = None,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
    bootstrap_repetitions: int = DEFAULT_BOOTSTRAP_REPETITIONS,
) -> dict[str, Any]:
    if bootstrap_repetitions <= 0:
        raise ConfigurationAnalysisError("bootstrap repetitions must be positive")
    rows, source_binding = load_paired_rows(
        trajectories_path=trajectories_path,
        sweep_runs_path=sweep_runs_path,
        sweep_metadata_path=sweep_metadata_path,
        evaluator_provenance_path=evaluator_provenance_path,
    )
    source_binding["analysis_generator"] = {
        "path": str(Path(__file__).resolve()),
        "sha256": sha256_file(Path(__file__).resolve()),
        "status": "locally_hash_bound",
    }
    report, bootstrap_output = build_report(
        rows, source_binding, bootstrap_seed=bootstrap_seed, bootstrap_repetitions=bootstrap_repetitions
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report_sha = _write_json(output_dir / "CONFIGURATION_ANALYSIS.json", report)
    markdown_sha = _write_hashed(output_dir / "CONFIGURATION_ANALYSIS.md", render_markdown(report).encode("utf-8"))
    bootstrap_sha = _write_json(output_dir / "bootstrap_ci.json", {"schema_version": SCHEMA_VERSION, "settings": bootstrap_output})

    comparison_path = output_dir / "paired_comparisons.csv"
    fieldnames = [
        "knob", "value", "suite", "instance_id", "cluster_id", "treatment_run_id", "baseline_run_id",
        "treatment_resolved", "baseline_resolved", "discordant", "treatment_e2e_wall_ms", "baseline_e2e_wall_ms",
        "delta_e2e_wall_ms", "treatment_faster", "treatment_status", "baseline_status", "provenance",
    ]
    with comparison_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=lambda item: (item["setting"]["knob"], item["setting"]["value"], item["suite"], item["instance_id"])):
            writer.writerow({
                "knob": row["setting"]["knob"], "value": row["setting"]["value"], "suite": row["suite"],
                "instance_id": row["instance_id"], "cluster_id": row["cluster_id"], "treatment_run_id": row["treatment_run_id"],
                "baseline_run_id": row["baseline_run_id"], "treatment_resolved": str(row["treatment_resolved"]).lower(),
                "baseline_resolved": str(row["baseline_resolved"]).lower(),
                "discordant": str(row["treatment_resolved"] != row["baseline_resolved"]).lower(),
                "treatment_e2e_wall_ms": row["treatment_e2e_wall_ms"], "baseline_e2e_wall_ms": row["baseline_e2e_wall_ms"],
                "delta_e2e_wall_ms": row["treatment_e2e_wall_ms"] - row["baseline_e2e_wall_ms"],
                "treatment_faster": str(row["treatment_e2e_wall_ms"] < row["baseline_e2e_wall_ms"]).lower(),
                "treatment_status": row["treatment_status"], "baseline_status": row["baseline_status"], "provenance": row["provenance"],
            })
    comparison_sha = sha256_file(comparison_path)
    Path(f"{comparison_path}.sha256").write_text(f"{comparison_sha}  {comparison_path.name}\n", encoding="utf-8")
    readme = (
        "# Configuration analysis artifacts\n\n"
        "`CONFIGURATION_ANALYSIS.json` and its Markdown rendering are deterministic, descriptive pairwise summaries. "
        "They are bound to the preserved trajectories, sweep rows, metadata, and evaluator provenance hashes recorded in the JSON. "
        "A zero-discordance bootstrap interval is not a noninferiority claim; see the finite-sample bound and assumptions in the report.\n"
    )
    readme_sha = _write_hashed(output_dir / "README.md", readme.encode("utf-8"))
    return {
        "output_dir": str(output_dir),
        "report_sha256": report_sha,
        "markdown_sha256": markdown_sha,
        "bootstrap_sha256": bootstrap_sha,
        "paired_comparisons_sha256": comparison_sha,
        "readme_sha256": readme_sha,
        "settings": len(report["settings"]),
        "pairs": len(rows),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectories", type=Path, required=True)
    parser.add_argument("--sweep-runs", type=Path, required=True)
    parser.add_argument("--sweep-metadata", type=Path, required=True)
    parser.add_argument("--evaluator-provenance", type=Path)
    parser.add_argument("--output-dir", "--output", dest="output_dir", type=Path, required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    parser.add_argument("--bootstrap-repetitions", type=int, default=DEFAULT_BOOTSTRAP_REPETITIONS)
    parser.add_argument("--confirmation-output-dir", type=Path, help="also write the frozen configuration confirmation bundle")
    parser.add_argument("--pilot-cases", type=Path, help="16-case instrumentation pilot inventory for confirmation generation")
    parser.add_argument("--termination-evidence-index", type=Path, help="metadata-only canonical termination evidence index")
    parser.add_argument("--full-matrix-inventory", type=Path, help="original full-matrix inventory used for exact resume keys/specs")
    parser.add_argument("--pin-evidence", type=Path)
    parser.add_argument("--evaluator-config", type=Path)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--historical-configuration-report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = write_analysis(
            trajectories_path=args.trajectories,
            sweep_runs_path=args.sweep_runs,
            sweep_metadata_path=args.sweep_metadata,
            evaluator_provenance_path=args.evaluator_provenance,
            output_dir=args.output_dir,
            bootstrap_seed=args.bootstrap_seed,
            bootstrap_repetitions=args.bootstrap_repetitions,
        )
        confirmation_paths = (
            args.pilot_cases,
            args.termination_evidence_index,
            args.full_matrix_inventory,
        )
        if args.confirmation_output_dir is not None:
            if any(path is None for path in confirmation_paths):
                raise ConfigurationAnalysisError(
                    "--confirmation-output-dir requires --pilot-cases, --termination-evidence-index, and --full-matrix-inventory"
                )
            confirmation = write_confirmation_panel(
                trajectories_path=args.trajectories,
                pilot_cases_path=args.pilot_cases,
                termination_evidence_index_path=args.termination_evidence_index,
                full_matrix_inventory_path=args.full_matrix_inventory,
                output_dir=args.confirmation_output_dir,
                pin_evidence_path=args.pin_evidence,
                evaluator_config_path=args.evaluator_config,
                run_manifest_path=args.run_manifest,
                historical_configuration_report_path=args.historical_configuration_report,
            )
            result["confirmation"] = confirmation
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"configuration analysis: BLOCKED: {exc}", file=sys.stderr)
        return 2
    print("configuration analysis: PASS")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
