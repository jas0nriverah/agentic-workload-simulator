#!/usr/bin/env python3
"""Bounded historical/configuration score analysis.

This script is deliberately report-only.  It derives the frozen historical
scope before reading classifications or any per-case result, filters by
identity first, and then reads only aggregate saved summaries plus one
accepted current queue result.  It never runs an evaluator, inference, GPU,
network operation, fit, or raw case loader.

Example::

    python3 score_opportunities.py --out-dir .

The default roots are the paths used for the 2026-09-09 review; all inputs
can be overridden for a bounded reproduction in a copied workspace.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import sqlite3
import sys
from typing import Any, Iterable


DEFAULT_REPO = Path("/home/riverahernandezjason/agentic-submission-repairs-20260908")
DEFAULT_ASSIGNMENT = Path("/home/riverahernandezjason/h100-assignment-work-20260905/assignment")
PREFLIGHT_REL = Path("submission/20260909T000000Z-resume/verification/astra-combined-preflight-20260909-9cP7NP")
HISTORICAL_REL = Path("submission/20260908T140000Z-offline-v2")
AUDIT_REL = Path("submission/20260908T010000Z/audit-step1-resolved-rates-readonly")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def truth(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def integer(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def add_rows(
    output: list[dict[str, Any]],
    dimension: str,
    category: str,
    rows: Iterable[dict[str, str]],
    *,
    resolved_key: str = "actual_official_resolved",
    note: str = "",
) -> None:
    materialized = list(rows)
    output.append(
        {
            "dimension": dimension,
            "category": category,
            "n": len(materialized),
            "resolved": sum(truth(row.get(resolved_key)) for row in materialized),
            "resolved_rate": (
                sum(truth(row.get(resolved_key)) for row in materialized) / len(materialized)
                if materialized
                else None
            ),
            "note": note,
        }
    )


def prompt_bin(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value < 8192:
        return "0-8191"
    if value < 16384:
        return "8192-16383"
    if value < 32768:
        return "16384-32767"
    return "32768+"


def patch_bin(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "0"
    if value <= 100:
        return "1-100"
    if value <= 1000:
        return "101-1000"
    return "1001+"


def classify_queue(
    assignment: Path,
    preflight: Path,
    scope: Any,
    eligible_baselines: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Read queue metadata and the one accepted result after identity gating."""

    db_path = preflight / "configuration-v1/queue-v2/queue.sqlite3"
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    case_rows = list(conn.execute("SELECT case_id, status, entry_json FROM cases ORDER BY ordinal"))
    attempt_rows = list(
        conn.execute(
            "SELECT attempt_id, case_id, attempt_no, worker_id, endpoint_id, status, "
            "launch_state, outcome_classification, result_path FROM attempts ORDER BY case_id"
        )
    )
    meta = {
        row["key"]: row["value_json"]
        for row in conn.execute("SELECT key, value_json FROM meta ORDER BY key")
    }
    conn.close()

    parsed_cases: list[dict[str, Any]] = []
    for row in case_rows:
        entry = json.loads(row["entry_json"])
        identity = {
            "instance_id": entry.get("instance_id"),
            "case_id": row["case_id"],
            "historical_template_case_id": entry.get("historical_template_case_id"),
        }
        # Scope is checked before using candidate, case, or result fields.
        if not scope.is_eligible(identity):
            continue
        parsed_cases.append(
            {
                "case_id": row["case_id"],
                "status": row["status"],
                "candidate_id": entry.get("candidate_id"),
                "instance_id": entry.get("instance_id"),
                "historical_template_case_id": entry.get("historical_template_case_id"),
            }
        )

    status_by_candidate = Counter((r["status"], r["candidate_id"]) for r in parsed_cases)
    attempt_status = Counter(r["status"] for r in attempt_rows)
    attempt_outcomes = Counter(r["outcome_classification"] for r in attempt_rows)
    blocked_workers = Counter(
        (r["worker_id"], r["endpoint_id"], r["outcome_classification"])
        for r in attempt_rows
        if r["status"] == "blocked"
    )

    accepted: dict[str, Any] | None = None
    accepted_rows = [r for r in attempt_rows if r["status"] == "accepted"]
    if len(accepted_rows) == 1:
        row = accepted_rows[0]
        case = next((c for c in parsed_cases if c["case_id"] == row["case_id"]), None)
        if case is not None and row["result_path"]:
            result_path = Path(row["result_path"]).resolve()
            preflight_root = preflight.resolve()
            if preflight_root not in result_path.parents:
                raise RuntimeError(f"accepted result escaped preflight root: {result_path}")
            result = read_json(result_path)
            spec_path = result_path.parent / "case_spec.json"
            spec = read_json(spec_path)
            # The current result is an own artifact.  Recheck identity before
            # consuming the official outcome and telemetry fields.
            scope.assert_eligible(
                {
                    "instance_id": spec.get("instance_id"),
                    "case_id": spec.get("case_id"),
                    "historical_template_case_id": spec.get("historical_template_case_id"),
                }
            )
            baseline = eligible_baselines.get(spec.get("historical_template_case_id"))
            summary = result.get("telemetry", {}).get("v2_evidence", {}).get("summary", {})
            native = result.get("telemetry", {}).get("native_serving", {})
            accepted = {
                "candidate_id": spec.get("candidate_id"),
                "instance_id": spec.get("instance_id"),
                "case_id": spec.get("case_id"),
                "historical_template_case_id": spec.get("historical_template_case_id"),
                "suite": spec.get("suite"),
                "repository": spec.get("repository"),
                "configuration": spec.get("final_configuration", {}),
                "serving_configuration": spec.get("serving_configuration", {}),
                "official_resolved": result.get("evaluator", {}).get("official_resolved"),
                "result_status": result.get("status"),
                "evaluator_status": result.get("evaluator", {}).get("status"),
                "result_sha256": sha256(result_path),
                "result_path": str(result_path),
                "native_evidence": {
                    "status": native.get("status"),
                    "physical_request_count": native.get("physical_request_count"),
                    "measured_count": native.get("measured_count"),
                    "unavailable_count": native.get("unavailable_count"),
                    "server_identity": native.get("server_identity"),
                },
                "telemetry_summary": {
                    key: summary.get(key)
                    for key in (
                        "execution_status",
                        "outer_wall_ms",
                        "attributed_union_ms",
                        "newly_attributed_union_ms",
                        "unknown_wall_ms",
                        "physical_requests",
                        "runtime_commands",
                        "tool_events",
                        "closure_error_ms",
                        "feature_parity_mismatches",
                        "missing_pre_actions",
                        "missing_terminal_requests",
                        "missing_terminal_tools",
                    )
                },
                "baseline": (
                    {
                        "resume_key": baseline.get("resume_key"),
                        "instance_id": baseline.get("instance_id"),
                        "primary_class": baseline.get("corrected_primary_class"),
                        "official_resolved": truth(baseline.get("actual_official_resolved")),
                        "patch_bytes": integer(baseline.get("patch_bytes")),
                        "edit_anthropic_install_fail": truth(baseline.get("edit_anthropic_install_fail")),
                        "tree_sitter_fail": truth(baseline.get("tree_sitter_fail")),
                        "python35_or_old_pip": truth(baseline.get("python35_or_old_pip")),
                        "summary_duration_ms": number(baseline.get("summary_duration_ms")),
                        "source_root": baseline.get("source_root"),
                    }
                    if baseline
                    else None
                ),
            }

    halt_reason = None
    if "halt_reason" in meta:
        try:
            halt_reason = json.loads(meta["halt_reason"])
        except json.JSONDecodeError:
            halt_reason = meta["halt_reason"]

    queue = {
        "database": str(db_path),
        "database_sha256": sha256(db_path),
        "case_count": len(parsed_cases),
        "status_counts": dict(Counter(r["status"] for r in parsed_cases)),
        "status_by_candidate": {f"{status}|{candidate}": count for (status, candidate), count in sorted(status_by_candidate.items())},
        "attempt_status_counts": dict(attempt_status),
        "attempt_outcome_counts": dict(attempt_outcomes),
        "blocked_workers": [
            {"worker_id": w, "endpoint_id": e, "outcome": o, "count": n}
            for (w, e, o), n in sorted(blocked_workers.items())
        ],
        "halt_reason": halt_reason,
        "accepted_attempt_count": len(accepted_rows),
    }
    return queue, accepted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--assignment-root", type=Path, default=DEFAULT_ASSIGNMENT)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    assignment = args.assignment_root.resolve()
    sys.path.insert(0, str(repo))
    from scripts.assignment.historical_analysis_scope import frozen_scope, validate_panel

    # This must be the first historical operation.  It reads only the five
    # pinned split manifests and the panel identities.
    scope = frozen_scope(assignment)
    scope_artifact = scope.artifact()

    historical = assignment / HISTORICAL_REL
    audit = assignment / AUDIT_REL
    preflight = assignment / PREFLIGHT_REL
    classifications_path = audit / "classifications.csv"
    paired_path = historical / "configuration-analysis/paired_comparisons.csv"
    term_path = historical / "configuration-analysis/TERMINATION_EVIDENCE_INDEX.json"
    panel_path = historical / "configuration-analysis/CONFIGURATION_CONFIRMATION_PANEL.json"
    panel_validation = validate_panel(scope, panel_path)

    # Saved classification rows are opened only after scope derivation.  We
    # inspect identity fields first and retain eligible rows only.
    eligible: list[dict[str, str]] = []
    total_rows = 0
    for row in csv.DictReader(classifications_path.open(newline="", encoding="utf-8")):
        total_rows += 1
        identity = {"instance_id": row.get("instance_id"), "resume_key": row.get("resume_key")}
        if scope.is_eligible(identity):
            eligible.append(row)
    if len(eligible) != 647:
        raise RuntimeError(f"eligible classification count changed: {len(eligible)} (expected 647)")

    eligible_baselines = {row["resume_key"]: row for row in eligible}
    eligible_by_template = {row["resume_key"]: row for row in eligible}

    # Native termination evidence is an identity-only historical aggregate.
    # No source path or per-case raw artifact is opened.
    term = read_json(term_path)
    native_by_case: dict[str, str] = {}
    native_meta_by_case: dict[str, dict[str, Any]] = {}
    for row in term["cases"]:
        if scope.is_eligible({"instance_id": row.get("instance_id"), "case_id": row.get("case_id")}):
            native_by_case[row["case_id"]] = row.get("native_exit_class", "unknown")
            native_meta_by_case[row["case_id"]] = row
    if not set(eligible_baselines).issubset(native_by_case):
        missing = sorted(set(eligible_baselines) - set(native_by_case))
        raise RuntimeError(f"termination join missing eligible cases: {missing[:3]}")

    taxonomy: list[dict[str, Any]] = []
    by_native: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in eligible:
        by_native[native_by_case[row["resume_key"]]].append(row)
    for category, rows in sorted(by_native.items()):
        add_rows(taxonomy, "termination", category, rows)

    by_primary: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in eligible:
        by_primary[row.get("corrected_primary_class") or row.get("primary_class") or "unknown"].append(row)
    for category, rows in sorted(by_primary.items()):
        add_rows(taxonomy, "patch_primary_class", category, rows)

    for flag in ("empty_patch", "missing_model_patch", "noop_like", "has_debug_files"):
        add_rows(
            taxonomy,
            "patch_signal",
            flag,
            (row for row in eligible if truth(row.get(flag))),
            note="nonexclusive heuristic or artifact signal; not a causal label",
        )
    for row in eligible:
        row["_patch_bin"] = patch_bin(integer(row.get("patch_bytes")))
    for category in sorted({row["_patch_bin"] for row in eligible}):
        add_rows(taxonomy, "patch_bytes_bin", category, (r for r in eligible if r["_patch_bin"] == category))

    environment_flags = (
        "edit_anthropic_install_fail",
        "tree_sitter_fail",
        "python35_or_old_pip",
        "function_calling_warning",
        "parser_error",
    )
    for flag in environment_flags:
        add_rows(
            taxonomy,
            "environment_signal",
            flag,
            (row for row in eligible if truth(row.get(flag))),
            note="association only; flags can co-occur and do not prove cause",
        )

    runtime_flags = (
        "eval_missing",
        "eval_corrupt",
        "eval_error_instances",
        "proxy_remote_disconnected",
        "strict_eval_completed",
        "traj_exists",
    )
    for flag in runtime_flags:
        if flag in {"eval_error_instances", "proxy_remote_disconnected"}:
            selected = [
                row
                for row in eligible
                if (integer(row.get(flag), 0) or 0) > 0
            ]
        else:
            selected = [row for row in eligible if truth(row.get(flag))]
        add_rows(
            taxonomy,
            "server_runtime_signal",
            flag,
            selected,
            note="saved evaluator/runtime metadata; no raw server archive mining",
        )

    prompt_rows = defaultdict(list)
    for row in eligible:
        # prompt_tokens_max is in the identity-only termination aggregate, not
        # in the saved classification CSV.  Keep the join identity exact.
        category = prompt_bin(integer(native_meta_by_case[row["resume_key"]].get("prompt_tokens_max")))
        prompt_rows[category].append(row)
        row["_prompt_bin"] = category
    for category in ("0-8191", "8192-16383", "16384-32767", "32768+", "unknown"):
        if category in prompt_rows:
            add_rows(taxonomy, "observation_prompt_max_bin", category, prompt_rows[category])
    add_rows(
        taxonomy,
        "observation_signal",
        "context_truncation",
        (row for row in eligible if truth(row.get("context_truncation"))),
        note="termination-associated flag; context exits can still resolve",
    )
    add_rows(
        taxonomy,
        "observation_signal",
        "baseline_observation_length_100000",
        (row for row in eligible if integer(row.get("settings_observation_length")) == 100000),
        note="all eligible original baselines; no historical guard intervention",
    )

    for category in sorted({row.get("repeat_id") or "unknown" for row in eligible}):
        add_rows(taxonomy, "retry_lineage", f"repeat_id={category}", (r for r in eligible if (r.get("repeat_id") or "unknown") == category), note="all original rows are r0; retry metadata is fixed")

    paired: list[dict[str, Any]] = []
    with paired_path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            # Identity gate precedes outcome/delta interpretation.
            if not scope.is_eligible(
                {
                    "instance_id": row.get("instance_id"),
                    "treatment_run_id": row.get("treatment_run_id"),
                    "baseline_run_id": row.get("baseline_run_id"),
                }
            ):
                continue
            paired.append(row)

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in paired:
        grouped[(row["knob"], row["value"])].append(row)
    historical_summary: list[dict[str, Any]] = []
    comparable_knobs = {"call_limit", "max_output_tokens", "observation_length", "temperature"}
    for (knob, value), rows in sorted(grouped.items()):
        wins = [r for r in rows if truth(r["treatment_resolved"]) and not truth(r["baseline_resolved"])]
        losses = [r for r in rows if not truth(r["treatment_resolved"]) and truth(r["baseline_resolved"])]
        deltas = [number(r.get("delta_e2e_wall_ms")) for r in rows]
        deltas = [x for x in deltas if x is not None]
        historical_summary.append(
            {
                "knob": knob,
                "value": value,
                "pairs": len(rows),
                "clusters": len({r["instance_id"] for r in rows}),
                "treatment_resolved": sum(truth(r["treatment_resolved"]) for r in rows),
                "baseline_resolved": sum(truth(r["baseline_resolved"]) for r in rows),
                "wins": len(wins),
                "losses": len(losses),
                "discordant": len(wins) + len(losses),
                "both_resolved": sum(truth(r["treatment_resolved"]) and truth(r["baseline_resolved"]) for r in rows),
                "median_delta_e2e_wall_ms": statistics.median(deltas) if deltas else None,
                "historically_comparable_dimension": knob in comparable_knobs,
                "provenance": "saved eligible paired_comparisons.csv; descriptive historical screen",
            }
        )

    queue, accepted = classify_queue(assignment, preflight, scope, eligible_baselines)

    # One bounded current accepted record is useful as a lineage check, but it
    # is not treated as a candidate winner.  Record the explicit confounders.
    current_comparison = None
    if accepted:
        current_comparison = {
            "candidate_id": accepted["candidate_id"],
            "instance_id": accepted["instance_id"],
            "historical_template_case_id": accepted["historical_template_case_id"],
            "candidate_official_resolved": accepted["official_resolved"],
            "historical_baseline_official_resolved": accepted.get("baseline", {}).get("official_resolved") if accepted.get("baseline") else None,
            "outcome_change": (
                "resolved_from_historical_unresolved"
                if accepted["official_resolved"] and accepted.get("baseline") and not accepted["baseline"]["official_resolved"]
                else "no_claim"
            ),
            "candidate_configuration": accepted["configuration"],
            "baseline_configuration": {
                "call_limit": 30,
                "max_input_tokens": 32768,
                "max_output_tokens": 2048,
                "observation_length": 100000,
                "temperature": 0.0,
            },
            "candidate_telemetry": accepted["telemetry_summary"],
            "candidate_native_evidence": accepted["native_evidence"],
            "baseline_metadata": accepted.get("baseline"),
            "interpretation": "accepted same-template newer comparison; quality change is confounded by call budget, input guard, runtime/evaluator/source and baseline editor/tool-install failure",
            "selection_use": "diagnostic only; cannot select a current winner from one accepted case",
        }

    evidence = {
        "schema_version": "assignment.retained-score-opportunities.v1",
        "created_by": str(Path(__file__).resolve()),
        "scope": {
            "excluded_instance_count": len(scope.excluded_instance_ids),
            "excluded_run_count": len(scope.excluded_run_ids),
            "manifest_list_sha256": scope_artifact["manifest_list_sha256"],
            "panel_disjoint": panel_validation["disjoint"],
            "panel_instance_count": panel_validation["instance_count"],
            "panel_case_count": 96,
            "prior_access_disclosure": scope_artifact["prior_access_disclosure"],
        },
        "inputs": {
            "historical_review": {
                "path": str((repo / "docs/HISTORICAL_IMPROVEMENT_REVIEW_20260909.md").resolve()),
                "sha256": sha256(repo / "docs/HISTORICAL_IMPROVEMENT_REVIEW_20260909.md"),
            },
            "classifications": {"path": str(classifications_path), "sha256": sha256(classifications_path), "rows_read": total_rows, "rows_eligible": len(eligible)},
            "termination_index": {"path": str(term_path), "sha256": sha256(term_path), "eligible_rows_joined": len(native_by_case)},
            "paired_comparisons": {"path": str(paired_path), "sha256": sha256(paired_path), "eligible_rows": len(paired)},
            "confirmation_panel": {"path": str(panel_path), "sha256": sha256(panel_path)},
            "queue": {"path": str(preflight / "configuration-v1/queue-v2/queue.sqlite3"), "sha256": queue["database_sha256"]},
        },
        "eligible_taxonomy": taxonomy,
        "taxonomy_limits": {
            "classification_rows_read_after_scope_derivation": total_rows,
            "classification_rows_retained": len(eligible),
            "parser_error_and_function_calling_flags": "universal 647/647 saved flags; cannot diagnose malformed patches",
            "context_truncation_flag": "114 rows; nonexclusive and distinct from 63 native exit_context rows",
            "recovered_proxy_error": "36-row corrected primary class; distinct from 15-row proxy_remote_disconnected flag",
            "timeout": "no native deadline/timeout class in eligible termination aggregate",
            "malformed_patch": "unsupported by these metadata; no relabeling",
            "long_setup": "historical phase unavailable; one current accepted phase decomposition only",
            "environment": "flags co-occur and do not establish cause; editor/install flagged rows include 63 resolved",
        },
        "historical_configuration_summary": historical_summary,
        "historical_configuration_limits": {
            "all_retained_treatments_zero_wins": all(row["wins"] == 0 for row in historical_summary),
            "historical_comparable": sorted(comparable_knobs),
            "untested_or_noncomparable": [
                "max_input_tokens/client guard",
                "pager environment",
                "runtime/server/capture changes",
                "loop/test guidance",
                "retry policy",
                "long setup/lifecycle attribution",
            ],
            "interpretation": "historical pairs screen harms and motivate the predeclared 96-case confirmation; they do not select a current configuration after runtime changes",
        },
        "loop_diagnostic_reused": {
            "eligible_trajectory_runs": 860,
            "runs_with_exact_action_at_least_three_times": 197,
            "repeated_occurrences_after_first": 458,
            "adjacent_identical_action_pairs": 21,
            "official_resolution_stratification": "not established; diagnostic correlation only",
            "source": "HISTORICAL_IMPROVEMENT_REVIEW_20260909.md",
        },
        "retry_diagnostic_reused": {
            "baseline_rows": 647,
            "repeat_id_distribution": dict(Counter(row.get("repeat_id") or "unknown" for row in eligible)),
            "saved_audit_metadata": "798/798 canonical rows report retries=20, min_wait=10.0, max_wait=120.0",
            "interpretation": "no historical retry variation supports a retry-quality effect estimate",
        },
        "long_setup": {
            "historical_baseline_setup_phase": "unavailable in saved E classifications; do not infer setup from total duration",
            "accepted_current_case": (
                {
                    "instance_id": accepted["instance_id"],
                    "official_resolved": accepted["official_resolved"],
                    "phase_union_ms": read_json(Path(accepted["result_path"]))["telemetry"]["v2_evidence"]["summary"].get("phase_union_ms", {}),
                    "interpretation": "one accepted phase decomposition; descriptive, no threshold or causal claim",
                }
                if accepted
                else None
            ),
        },
        "queue_status": queue,
        "accepted_current_comparison": current_comparison,
        "declared_96_comparison": {
            "case_count": 96,
            "instance_count": 24,
            "candidates": [
                "historical-control-call30-input32768",
                "expanded-call50-input61440",
                "expanded-call100-input61440",
                "expanded-call100-input61440-observation25000",
            ],
            "contrasts": [
                {
                    "id": "control_vs_call50_guard",
                    "left": "historical-control-call30-input32768",
                    "right": "expanded-call50-input61440",
                    "identifiable_change": ["call_limit", "max_input_tokens/client_guard"],
                    "cannot_attribute": "call budget alone",
                },
                {
                    "id": "call50_vs_call100_fixed_guard",
                    "left": "expanded-call50-input61440",
                    "right": "expanded-call100-input61440",
                    "identifiable_change": ["call_limit"],
                    "fixed": ["max_input_tokens", "observation_length", "output", "temperature", "seed"],
                },
                {
                    "id": "observation100000_vs25000_fixed_calls",
                    "left": "expanded-call100-input61440",
                    "right": "expanded-call100-input61440-observation25000",
                    "identifiable_change": ["observation_length"],
                    "fixed": ["call_limit", "max_input_tokens", "output", "temperature", "seed"],
                },
            ],
            "current_observed_status": {candidate: queue["status_by_candidate"] for candidate in []},
            "selection_rule": "paired official resolution, regression review, then declared failure/context/budget/cost tie-breaks; no final 1088 outcomes",
        },
    }
    # Avoid a second queue read and expose status counts in the declared block.
    evidence["declared_96_comparison"]["current_observed_status"] = {
        candidate: {
            status: count
            for key, count in queue["status_by_candidate"].items()
            if (status := key.split("|", 1)[0]) and key.split("|", 1)[1] == candidate
        }
        for candidate in evidence["declared_96_comparison"]["candidates"]
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = args.out_dir / "score_evidence.json"
    csv_path = args.out_dir / "score_evidence.csv"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    csv_fields = ["dimension", "category", "n", "resolved", "resolved_rate", "note"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in taxonomy:
            writer.writerow({key: row.get(key) for key in csv_fields})
        for row in historical_summary:
            writer.writerow(
                {
                    "dimension": "historical_pair",
                    "category": f"{row['knob']}={row['value']}",
                    "n": row["pairs"],
                    "resolved": row["treatment_resolved"],
                    "resolved_rate": row["treatment_resolved"] / row["pairs"] if row["pairs"] else None,
                    "note": f"wins={row['wins']};losses={row['losses']};baseline_resolved={row['baseline_resolved']};median_delta_ms={row['median_delta_e2e_wall_ms']}",
                }
            )

    print(json.dumps({"evidence": str(evidence_path), "csv": str(csv_path), "eligible": len(eligible), "paired_rows": len(paired), "accepted": accepted is not None}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
