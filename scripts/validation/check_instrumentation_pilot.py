#!/usr/bin/env python3
"""Review capture integrity; report historical engineering targets separately.

Input metrics must be computed from hash-bound journals by the v2 summarizer.
This checker cannot attest to the truth of a caller-supplied summary. Astra
must review journals, source parity and the preflight evidence before launch.
The PDF normative register governs readiness. Pilot sizes, 5%/10% overhead,
95% attribution and fixed closure tolerances are diagnostics, not PDF gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import median

SCHEMA = "assignment.instrumentation-pilot-evidence.v2"
CONFIG_KEYS = {"call_limit", "max_output_tokens", "observation_length", "temperature", "max_input_tokens", "top_p", "seed"}
# These concern actual missing/invalid acquisition, rather than a prescribed
# campaign or proof format. Main still reviews whether the evidence supports
# the intended workload; passing this checker does not assert representativeness.
ACQUISITION_REVIEW_FIELDS = frozenset({
    "useful_live_workload_descriptors_verified", "external_event_coverage_verified",
    "evaluator_correctness_verified", "literal_pdf_acquisition_verified",
    "individual_cpu_records_verified", "raw_model_records_verified",
})


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def assess(evidence):
    failures = []
    advisories = []

    def check(condition, message):
        if not condition:
            failures.append(message)

    def advise(condition, message):
        if not condition:
            advisories.append(message)

    check(evidence.get("schema_version") == SCHEMA, "Unsupported pilot evidence schema")
    check(evidence.get("evidence_kind") == "live", "Synthetic evidence cannot pass a live gate")
    selected = evidence.get("selected_case_ids", [])
    check(bool(selected) and len(set(selected)) == len(selected), "Capture evidence needs distinct case IDs")
    advise(len(selected) == 16, "Historical pilot design used 16 cases; that count is advisory")
    summaries = evidence.get("case_summaries", [])
    check(len(summaries) == len(selected) and {r.get("case_id") for r in summaries} == set(selected), "Pilot summary identities differ from preselection")
    settings = evidence.get("baseline", {})
    check(set(settings) == CONFIG_KEYS
          and all(finite(v) for v in settings.values()), "Candidate configuration is missing or malformed")
    check(settings == evidence.get("frozen_pilot_configuration"), "Pilot configuration differs from its frozen candidate")
    for row in summaries:
        case = row.get("case_id", "unknown")
        check(row.get("execution_status") == "completed", f"{case}: incomplete trajectory execution")
        check(row.get("status") == "pass" and row.get("internal_consistency_errors") == [], f"{case}: journal audit did not pass")
        for name in ("missing_pre_actions", "missing_terminal_tools", "missing_terminal_requests", "duplicate_ids", "negative_intervals", "unlinked_retries", "request_mutations", "feature_parity_mismatches", "future_feature_violations", "dropped_cpu_records", "cpu_capture_map_failures", "missing_raw_request_bodies"):
            check(isinstance(row.get(name), int) and not isinstance(row.get(name), bool) and row[name] == 0, f"{case}: {name} is nonzero or unreported")
        for name in ("tool_events", "physical_requests", "individual_cpu_operation_records"):
            check(isinstance(row.get(name), int) and not isinstance(row.get(name), bool) and row[name] >= 0, f"{case}: invalid or unreported {name}")
            advise(row.get(name, 0) != 0, f"{case}: {name} not exercised; this case alone does not prove that capture path")
        raw_count = row.get("raw_model_request_records")
        check(isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count == row.get("physical_requests"), f"{case}: raw model attempt coverage differs from physical requests")
        outer, attributed, unknown, error = (row.get(k) for k in ("outer_wall_ms", "attributed_union_ms", "unknown_wall_ms", "closure_error_ms"))
        valid = all(finite(v) for v in (outer, attributed, unknown, error)) and outer > 0 and 0 <= attributed <= outer and 0 <= unknown <= outer
        check(valid, f"{case}: invalid timing metrics")
        if valid:
            tolerance = max(1.0, outer * .001)
            advise(attributed / outer >= .95, f"{case}: below historical 95% attribution target; review missing mechanisms")
            advise(unknown / outer <= .05, f"{case}: above historical 5% UNKNOWN target; review whether raw data can explain it")
            advise(abs(error) <= tolerance and abs(outer - attributed - unknown) <= tolerance,
                   f"{case}: outside historical closure tolerance; inspect clocks and union arithmetic")
        check(row.get("attribution_excludes_unknown_and_outer_wrappers") is True, f"{case}: attribution policy not verified")

    # These are evidence-review requirements, not substitutions for the
    # quantitative journal checks above. Missing is never treated as passed.
    for name in ("offline_tests_passed", "instrumentation_tests_passed", "remote_artifacts_reconciled", "remote_pins_verified", "legacy_process_noninterference_verified", "useful_live_workload_descriptors_verified", "train_serve_projection_reviewed", "holdout_isolation_verified", "external_event_coverage_verified", "e2e_attribution_improvement_verified", "evaluator_correctness_verified", "interruption_resume_verified", "literal_pdf_acquisition_verified", "historical_failure_regressions_verified", "individual_cpu_records_verified", "raw_model_records_verified"):
        (check if name in ACQUISITION_REVIEW_FIELDS else advise)(
            evidence.get("review", {}).get(name) is True, f"Evidence review missing: {name}")

    replay_ids = evidence.get("replay_case_ids", [])
    check(len(set(replay_ids)) == len(replay_ids), "Duplicate overhead fixture identity")
    advise(len(replay_ids) == 4, "Historical overhead design used four fixtures; that count is advisory")
    pairs = evidence.get("overhead_pairs", [])
    per_case = []
    for case in replay_ids:
        group = [r for r in pairs if r.get("case_id") == case]
        advise(len(group) == 3 and {r.get("repeat") for r in group} == {0, 1, 2}, f"{case}: differs from historical three-pair design")
        check(len({r.get("workload_sha256") for r in group}) == 1, f"{case}: replay workloads differ across repeats")
        ratios = []
        for pair in group:
            check(pair.get("control_workload_sha256") == pair.get("instrumented_workload_sha256") == pair.get("workload_sha256") and isinstance(pair.get("workload_sha256"), str) and len(pair["workload_sha256"]) == 64, f"{case}: replay payload mismatch")
            advise(pair.get("order") == ("AB" if pair.get("repeat", -1) % 2 == 0 else "BA"), f"{case}: replay order differs from historical alternating order")
            check(pair.get("same_serving_and_cache_policy") is True, f"{case}: replay serving/cache policy mismatch")
            check(pair.get("full_production_capture_enabled") is True, f"{case}: overhead was not measured with full production capture")
            a, b = pair.get("control_wall_ms"), pair.get("instrumented_wall_ms")
            check(finite(a) and finite(b) and a > 0 and b > 0, f"{case}: invalid replay durations")
            if finite(a) and finite(b) and a > 0 and b > 0:
                ratios.append(100 * (b / a - 1))
        if ratios:
            per_case.append(median(ratios))
    check(all(r.get("case_id") in replay_ids for r in pairs), "Overhead pair has an undeclared fixture identity")
    advise(len(pairs) == 3 * len(replay_ids), "Pair count differs from historical three-per-fixture design")
    med = median(per_case) if per_case else None
    p95 = sorted(per_case)[math.ceil(.95 * len(per_case)) - 1] if per_case else None
    advise(med is not None and med <= 5, "Historical median overhead target 5% exceeded or unavailable; not a PDF acceptance threshold")
    advise(p95 is not None and p95 <= 10, "Historical nearest-rank p95 target 10% exceeded or unavailable; not a PDF acceptance threshold")
    return {"status": "pass" if not failures else "fail", "failures": failures,
            "advisories": advisories, "assessment_scope": "capture_integrity_and_evidence_consistency",
            "measurement_representativeness": "requires_main_review_of_actual_effects_not_numeric_target",
            "median_overhead_percent": med, "p95_overhead_percent": p95,
            "launch_authorized": False,
            "next_step": "Astra must review hash-bound evidence before authorizing the full matrix."}


def verify_acquisition_proofs(payloads, hashes, roles):
    """Bind the complete PDF/regression review to actual artifact inventories.

    This checks integrity and completeness of the review record. Raw counters
    still must be regenerated by the collector/journal auditors and reviewed
    by Astra; a self-authored 'pass' document is not an independent witness.
    """
    contract = payloads["acquisition_contract"]
    if contract.get("schema_version") != "assignment.acquisition-contract.v2" or contract.get("production_case_count") != 1088:
        raise ValueError("Invalid literal acquisition contract or production count")
    for role, schema, field, declared in (
        ("acquisition_proof", "assignment.acquisition-proof.v2", "requirements", contract["requirements"]),
        ("historical_regression_proof", "assignment.historical-regression-proof.v2", "regressions", contract["historical_regressions"]),
    ):
        proof = payloads[role]
        if proof.get("schema_version") != schema or proof.get("evidence_kind") != "live_prelaunch":
            raise ValueError(f"{role}: missing live prelaunch proof")
        if proof.get("contract_sha256") != hashes["acquisition_contract"] or proof.get("source_bundle_sha256") != hashes["source_bundle"]:
            raise ValueError(f"{role}: contract/source binding mismatch")
        items = proof.get(field, [])
        if len(items) != len(declared) or {r.get("id") for r in items} != {r["id"] for r in declared}:
            raise ValueError(f"{role}: incomplete or duplicate requirement coverage")
        for row in items:
            references = row.get("artifact_roles", [])
            if row.get("status") != "pass" or not references or not set(references) <= roles:
                raise ValueError(f"{role}: {row.get('id')} lacks passing hash-bound evidence")
            if not isinstance(row.get("verification"), str) or not row["verification"].strip():
                raise ValueError(f"{role}: {row.get('id')} lacks a verification method")
            if role == "historical_regression_proof":
                disposition = row.get("disposition")
                if disposition not in {"fixed", "measured_explicitly", "validity_limitation"}:
                    raise ValueError(f"{role}: {row.get('id')} lacks a corrective disposition")
                if disposition == "validity_limitation" and row["id"] not in {"R15", "R16"}:
                    raise ValueError("Required acquisition/overhead failures cannot be waived as limitations")
                if row["id"] in {"R15", "R16"} and not row.get("remaining_limitation"):
                    raise ValueError("D9 accuracy/transfer needs an explicit prelaunch outcome limitation")


def verify_bindings(evidence, base):
    bindings = evidence.get("artifact_bindings", [])
    if not bindings:
        raise ValueError("Pilot evidence requires artifact hashes")
    roles = set()
    payloads, hashes = {}, {}
    pilot_ids = None
    manifest_settings = None
    for row in bindings:
        path = (base / row["path"]).resolve()
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError(f"Pilot source hash mismatch: {path}")
        role = row.get("role")
        if role in roles:
            raise ValueError(f"Duplicate pilot artifact role: {role}")
        roles.add(role)
        hashes[role] = row["sha256"]
        if role in {"acquisition_contract", "acquisition_proof", "historical_regression_proof"}:
            payloads[role] = json.loads(path.read_text())
        if row.get("role") == "run_manifest":
            manifest = json.loads(path.read_text())
            raw = manifest.get("final_configuration", manifest.get("baseline_configuration", {}))
            manifest_settings = {key: raw.get(key) for key in CONFIG_KEYS}
        if row.get("role") == "pilot_inventory":
            inventory = json.loads(path.read_text())
            pilot_ids = [case["case_id"] for case in inventory["cases"]]
    needed = {"pilot_inventory", "event_journals", "overhead_replay", "source_bundle", "remote_reconciliation", "feature_parity", "run_manifest", "acquisition_contract", "acquisition_proof", "historical_regression_proof", "raw_cpu_record_inventory", "raw_model_record_inventory", "offline_test_report", "full_matrix_inventory"}
    if not needed <= roles:
        raise ValueError(f"Missing pilot evidence roles: {sorted(needed - roles)}")
    verify_acquisition_proofs(payloads, hashes, roles)
    if manifest_settings != evidence.get("frozen_pilot_configuration"):
        raise ValueError("Pilot candidate configuration is not bound to the run manifest")
    if pilot_ids != evidence.get("selected_case_ids"):
        raise ValueError("Pilot evidence does not match the exact sealed preselection order")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    verify_bindings(evidence, args.evidence.parent)
    result = assess(evidence)
    print(json.dumps(result, sort_keys=True, indent=2))
    raise SystemExit(0 if result["status"] == "pass" else 1)
