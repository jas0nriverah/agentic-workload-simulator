"""Audit whether the retained repaired ledgers identify a composable E2E path.

This is deliberately an audit, rather than an estimator.  It reads only the
43 CPU-lifecycle input ledgers that were admitted after the original validity
check, their identity-bound normalized ledgers, and the corresponding retained
model/native journals.  The audit makes interval unions explicit, reports
duplicated/nested boundaries, and checks the raw parent/logical/client-span
join that the compact normalized adapter does not retain.

The result remains ``unsupported`` for a prospective full composition when it
would need an observation-only wrapper, an unknown residual, or an interval
topology that is not a supplied trace contract.  Observed residual time is
never copied into a prediction.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


HERE = Path(__file__).resolve().parent
BASE = HERE.parent
CPU_INPUTS = BASE / "cpu_lifecycle" / "inputs"
LEDGERS = BASE / "evidence" / "ledgers"
OUTPUT = HERE / "audit.json"
CPU_MANIFEST = BASE / "cpu_lifecycle" / "manifest.json"

OUTER_CLASS = "lifecycle:outer_swe_agent"
WRAPPER_CLASS = "lifecycle:runner_process_wrapper"
OUTER_EXCLUSIONS = {OUTER_CLASS, WRAPPER_CLASS}
OBSERVATION_CLASSES = {
    "model_client_call",
    "model_request",
    "lifecycle:unknown_residual",
}
NATIVE_CLASSES = {
    "native:queue",
    "native:prefill",
    "native:decode",
    "native:e2e",
}
NATIVE_COMPONENTS = ("native:queue", "native:prefill", "native:decode")

# These are the overlapping pairs that matter to an additive lifecycle
# composition.  The report also records all exact outer/wrapper duplicates and
# native per-request phase duplicates independently.
IMPORTANT_PAIRS = (
    ("lifecycle:deployment_start", "runtime_command"),
    ("lifecycle:startup", "runtime_command"),
    ("lifecycle:setup", "lifecycle:startup"),
    ("lifecycle:get_state", "runtime_command"),
    ("lifecycle:client_processing", "lifecycle:script_read"),
    ("lifecycle:client_processing", "runtime_command"),
    ("lifecycle:script_read", "runtime_command"),
    ("lifecycle:bash_interrupt_control", "semantic_action"),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _span(row: Mapping[str, Any]) -> tuple[int, int] | None:
    start = row.get("start_mono_ns")
    end = row.get("end_mono_ns")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    start_i = int(start)
    end_i = int(end)
    if end_i <= start_i:
        return None
    return start_i, end_i


def union_length(spans: Iterable[tuple[int, int]]) -> int:
    ordered = sorted(spans)
    if not ordered:
        return 0
    total = 0
    start, end = ordered[0]
    for next_start, next_end in ordered[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start


def overlap_length(left: Iterable[tuple[int, int]], right: Iterable[tuple[int, int]]) -> int:
    total = 0
    for left_start, left_end in left:
        for right_start, right_end in right:
            total += max(0, min(left_end, right_end) - max(left_start, right_start))
    return total


def duration_ms(spans: Iterable[tuple[int, int]]) -> float:
    return sum(end - start for start, end in spans) / 1_000_000.0


def _finite_ms(row: Mapping[str, Any]) -> float | None:
    value = row.get("observed_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _raw_case_root(identity: Mapping[str, Any]) -> Path:
    manifest = json.loads(CPU_MANIFEST.read_text(encoding="utf-8"))
    ordinal = f"{int(identity['queue_ordinal']):05d}"
    matches = [
        row
        for row in manifest.get("cases", [])
        if Path(str(row.get("events_path", ""))).stem == ordinal
        and row.get("instance_id") == identity.get("instance_id")
    ]
    if len(matches) != 1:
        raise AssertionError(f"raw case root is not uniquely bound for {ordinal}")
    root = Path(str(matches[0]["case_root"]))
    if not root.is_dir():
        raise AssertionError(f"retained raw case root is unavailable: {root}")
    return root


def _raw_join_report(
    identity: Mapping[str, Any],
    *,
    outer_span: tuple[int, int],
    cpu_clock: str,
) -> dict[str, Any]:
    """Check the retained raw model/client/native identity chain.

    The normalized adapter intentionally emits model wrapper rows as
    observation-only records and drops parent/client span fields.  That policy
    is not evidence that the raw join is absent, so this bounded check reads
    those fields directly from the already-retained small journals.
    """

    root = _raw_case_root(identity)
    model_path = root / "runner_attempts" / "attempt-001" / "telemetry_v2" / "model_events.jsonl"
    native_path = root / "runner_attempts" / "attempt-001" / "native_serving" / "native_attribution.jsonl"
    if not model_path.is_file() or not native_path.is_file():
        raise AssertionError(f"raw model/native journal missing under {root}")
    model_rows = _load_jsonl(model_path)
    native_rows = _load_jsonl(native_path)

    client_starts = {
        row.get("span_id"): row
        for row in model_rows
        if row.get("event_kind") == "model_client_call_start"
        and row.get("terminal") is not True
        and isinstance(row.get("span_id"), str)
    }
    client_terminals = {
        row.get("span_id"): row
        for row in model_rows
        if row.get("event_kind") == "model_client_call"
        and row.get("terminal") is True
        and isinstance(row.get("span_id"), str)
    }
    request_starts = {
        row.get("physical_request_id"): row
        for row in model_rows
        if row.get("event_kind") == "model_request_start"
        and row.get("terminal") is not True
        and isinstance(row.get("physical_request_id"), str)
    }
    request_terminals = [
        row
        for row in model_rows
        if row.get("event_kind") == "model_request" and row.get("terminal") is True
    ]
    native_by_id = {
        row.get("physical_request_id"): row
        for row in native_rows
        if isinstance(row.get("physical_request_id"), str)
    }

    outer_start, outer_end = outer_span
    parent_logical_client_join = 0
    request_within_outer = 0
    request_within_client = 0
    request_clock_matches_cpu = 0
    native_physical_join = 0
    native_e2e_fits_local_request = 0
    native_e2e_ms = 0.0
    local_request_ms = 0.0
    local_overhead_ms = 0.0
    for request in request_terminals:
        physical_id = request.get("physical_request_id")
        client_span = request.get("client_span_id")
        client_start = client_starts.get(client_span)
        client_terminal = client_terminals.get(client_span)
        request_start = request_starts.get(physical_id)
        exact_identity = (
            client_start is not None
            and client_terminal is not None
            and request_start is not None
            and request_start.get("parent_event_id") == client_start.get("event_id")
            and request.get("logical_request_id")
            == client_start.get("logical_request_id")
            == client_terminal.get("logical_request_id")
            and request.get("client_span_id") == client_terminal.get("span_id")
        )
        parent_logical_client_join += int(exact_identity)

        request_span = _span(request)
        if request_span is not None:
            request_start_ns, request_end_ns = request_span
            request_within_outer += int(
                outer_start <= request_start_ns <= request_end_ns <= outer_end
            )
            raw_clock = request.get("clock") or {}
            request_clock = f"{raw_clock.get('clock_id')}|boot={raw_clock.get('boot_id')}"
            request_clock_matches_cpu += int(request_clock == cpu_clock)
            request_ms = (request_end_ns - request_start_ns) / 1_000_000.0
            local_request_ms += request_ms
            if client_start is not None and client_terminal is not None:
                client_span_value = _span(client_terminal)
                if client_span_value is not None and (
                    client_span_value[0] <= request_start_ns <= request_end_ns <= client_span_value[1]
                ):
                    request_within_client += 1

        native = native_by_id.get(physical_id)
        if native is None:
            continue
        native_physical_join += 1
        metric = (native.get("metrics") or {}).get("e2e")
        native_value = metric.get("value_ms") if isinstance(metric, Mapping) else None
        if request_span is None or not isinstance(native_value, (int, float)):
            continue
        native_value = float(native_value)
        native_e2e_ms += native_value
        if native_value <= (request_span[1] - request_span[0]) / 1_000_000.0:
            native_e2e_fits_local_request += 1
        local_overhead_ms += (request_span[1] - request_span[0]) / 1_000_000.0 - native_value

    return {
        "raw_model_path": str(model_path),
        "raw_native_path": str(native_path),
        "raw_model_sha256": sha256_file(model_path),
        "raw_native_sha256": sha256_file(native_path),
        "model_client_start_rows": len(client_starts),
        "model_client_terminal_rows": len(client_terminals),
        "model_request_start_rows": len(request_starts),
        "model_request_terminal_rows": len(request_terminals),
        "native_request_rows": len(native_by_id),
        "exact_parent_logical_client_span_joins": parent_logical_client_join,
        "native_physical_id_joins": native_physical_join,
        "model_request_rows_within_outer": request_within_outer,
        "model_request_rows_on_cpu_outer_clock": request_clock_matches_cpu,
        "model_request_rows_within_local_client_interval": request_within_client,
        "model_request_rows_outside_local_client_interval": len(request_terminals) - request_within_client,
        "native_e2e_rows_fitting_local_model_request_duration": native_e2e_fits_local_request,
        "local_model_request_sum_ms": local_request_ms,
        "native_e2e_sum_ms": native_e2e_ms,
        "local_model_request_minus_native_e2e_ms": local_overhead_ms,
    }


def _case_inputs() -> list[tuple[Path, dict[str, Any], Path, list[dict[str, Any]]]]:
    cases: list[tuple[Path, dict[str, Any], Path, list[dict[str, Any]]]] = []
    for input_path in sorted(CPU_INPUTS.glob("*.jsonl")):
        input_rows = _load_jsonl(input_path)
        if not input_rows:
            raise AssertionError(f"empty lifecycle input: {input_path}")
        identity = {
            "case_id": input_rows[0]["case_id"],
            "instance_id": input_rows[0]["instance_id"],
            "queue_ordinal": int(input_path.stem),
        }
        if any(
            row.get("case_id") != identity["case_id"]
            or row.get("instance_id") != identity["instance_id"]
            for row in input_rows
        ):
            raise AssertionError(f"input identity drift: {input_path}")
        ledger_dir = LEDGERS / f"{input_path.stem}-{identity['instance_id']}"
        ledger_path = ledger_dir / "normalized_case_events.jsonl"
        if not ledger_path.is_file():
            raise AssertionError(f"missing identity-bound normalized ledger: {ledger_path}")
        ledger_rows = _load_jsonl(ledger_path)
        cases.append((input_path, identity, ledger_path, ledger_rows))
    return cases


def _within_outer(
    row: Mapping[str, Any], *, clock_id: str, outer_start: int, outer_end: int
) -> tuple[int, int] | None:
    if row.get("clock_id") != clock_id:
        return None
    span = _span(row)
    if span is None:
        return None
    start, end = span
    if start < outer_start or end > outer_end:
        return None
    return span


def _class_spans(
    rows: Iterable[Mapping[str, Any]],
    *,
    role: str,
    event_class: str,
    clock_id: str,
    outer_start: int,
    outer_end: int,
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for row in rows:
        if row.get("record_role") != role or row.get("event_class") != event_class:
            continue
        span = _within_outer(
            row,
            clock_id=clock_id,
            outer_start=outer_start,
            outer_end=outer_end,
        )
        if span is not None:
            spans.append(span)
    return spans


def _pair_report(
    by_class: Mapping[str, list[tuple[int, int]]],
    pair: tuple[str, str],
) -> dict[str, Any]:
    left, right = pair
    left_spans = by_class.get(left, [])
    right_spans = by_class.get(right, [])
    overlap_ns = overlap_length(left_spans, right_spans)
    event_pairs = 0
    exact_duplicates = 0
    for a_start, a_end in left_spans:
        for b_start, b_end in right_spans:
            current = max(0, min(a_end, b_end) - max(a_start, b_start))
            if current:
                event_pairs += 1
                exact_duplicates += int(a_start == b_start and a_end == b_end)
    return {
        "left": left,
        "right": right,
        "left_event_count": len(left_spans),
        "right_event_count": len(right_spans),
        "overlapping_event_pairs": event_pairs,
        "exact_duplicate_pairs": exact_duplicates,
        "overlap_ms": overlap_ns / 1_000_000.0,
    }


def _native_join_report(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    native_by_request: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row.get("record_role") != "TARGET" or row.get("event_class") not in NATIVE_CLASSES:
            continue
        physical_id = row.get("physical_request_id")
        if not isinstance(physical_id, str) or not physical_id:
            continue
        native_by_request[physical_id][row["event_class"]] = row

    complete_model_requests: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    all_model_requests: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    model_client_rows: list[Mapping[str, Any]] = []
    for row in rows:
        event_class = row.get("event_class")
        if event_class == "model_request":
            physical_id = row.get("physical_request_id")
            if isinstance(physical_id, str):
                all_model_requests[physical_id].append(row)
                if _span(row) is not None:
                    complete_model_requests[physical_id].append(row)
        elif event_class == "model_client_call":
            model_client_rows.append(row)

    exact_join = 0
    duplicate_or_missing = 0
    for physical_id, native in native_by_request.items():
        complete = complete_model_requests.get(physical_id, [])
        if (
            len(complete) == 1
            and complete[0].get("pre_event_id") == native.get("native:e2e", {}).get("pre_event_id")
        ):
            exact_join += 1
        else:
            duplicate_or_missing += 1

    native_ids = set(native_by_request)
    native_pre_ids = {
        row.get("pre_event_id")
        for native in native_by_request.values()
        for row in native.values()
    }
    model_client_ids = {
        row.get("physical_request_id")
        for row in model_client_rows
        if isinstance(row.get("physical_request_id"), str)
    }
    model_client_pre_ids = {
        row.get("pre_event_id")
        for row in model_client_rows
        if isinstance(row.get("pre_event_id"), str)
    }

    phase_sum_ms = 0.0
    e2e_sum_ms = 0.0
    phase_less_than_e2e = 0
    duplicate_phase_intervals = 0
    complete_native_requests = 0
    for physical_id, native in native_by_request.items():
        if not all(component in native for component in (*NATIVE_COMPONENTS, "native:e2e")):
            continue
        complete_native_requests += 1
        phase_values = [_finite_ms(native[component]) for component in NATIVE_COMPONENTS]
        e2e_value = _finite_ms(native["native:e2e"])
        if any(value is None for value in (*phase_values, e2e_value)):
            continue
        phase_total = sum(value for value in phase_values if value is not None)
        phase_sum_ms += phase_total
        e2e_sum_ms += e2e_value  # type: ignore[operator]
        phase_less_than_e2e += int(phase_total < e2e_value)  # type: ignore[operator]
        intervals = [_span(native[component]) for component in (*NATIVE_COMPONENTS, "native:e2e")]
        if all(interval is not None for interval in intervals) and len(set(intervals)) == 1:
            duplicate_phase_intervals += 1

    all_model_rows = [
        row
        for row in rows
        if row.get("event_class") in {"model_client_call", "model_request"}
    ]
    model_eligibility = Counter(
        (
            row.get("event_class"),
            row.get("record_role"),
            bool(row.get("model_eligible")),
        )
        for row in all_model_rows
    )

    return {
        "native_request_count": len(native_by_request),
        "native_requests_with_all_four_rows": complete_native_requests,
        "model_request_rows": len(all_model_requests),
        "model_request_complete_rows": sum(len(v) for v in complete_model_requests.values()),
        "model_request_null_rows": sum(
            sum(_span(row) is None for row in rows_for_request)
            for rows_for_request in all_model_requests.values()
        ),
        "exact_native_to_model_request_joins": exact_join,
        "native_join_missing_or_duplicate": duplicate_or_missing,
        "model_client_rows": len(model_client_rows),
        "model_client_ids_intersect_native_physical_ids": len(model_client_ids & native_ids),
        "model_client_pre_event_ids_intersect_native_pre_event_ids": len(
            model_client_pre_ids & native_pre_ids
        ),
        "model_observation_eligibility": {
            f"{event_class}|{role}|model_eligible={eligible}": count
            for (event_class, role, eligible), count in sorted(model_eligibility.items())
        },
        "native_phase_interval_duplicate_requests": duplicate_phase_intervals,
        "native_phase_sum_ms": phase_sum_ms,
        "native_e2e_sum_ms": e2e_sum_ms,
        "native_phase_sum_lt_e2e_requests": phase_less_than_e2e,
        "native_phase_minus_e2e_ms": phase_sum_ms - e2e_sum_ms,
    }


def _case_audit(
    input_path: Path,
    identity: Mapping[str, Any],
    ledger_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    outer_rows = [
        row
        for row in rows
        if row.get("record_role") == "TARGET" and row.get("event_class") == OUTER_CLASS
    ]
    wrapper_rows = [
        row
        for row in rows
        if row.get("record_role") == "TARGET" and row.get("event_class") == WRAPPER_CLASS
    ]
    if len(outer_rows) != 1 or len(wrapper_rows) != 1:
        raise AssertionError(f"expected one outer and one wrapper target in {ledger_path}")
    outer, wrapper = outer_rows[0], wrapper_rows[0]
    outer_span = _span(outer)
    wrapper_span = _span(wrapper)
    if outer_span is None or wrapper_span is None:
        raise AssertionError(f"outer or wrapper span is incomplete in {ledger_path}")
    outer_start, outer_end = outer_span
    cpu_clock = outer.get("clock_id")
    if not isinstance(cpu_clock, str):
        raise AssertionError(f"outer CPU clock is missing in {ledger_path}")

    by_class: dict[str, list[tuple[int, int]]] = defaultdict(list)
    target_classes = Counter()
    for row in rows:
        if row.get("record_role") != "TARGET":
            continue
        target_classes[row.get("event_class")] += 1
        if row.get("event_class") in OUTER_EXCLUSIONS or row.get("event_class") in NATIVE_CLASSES:
            continue
        span = _within_outer(
            row,
            clock_id=cpu_clock,
            outer_start=outer_start,
            outer_end=outer_end,
        )
        if span is not None:
            by_class[row["event_class"]].append(span)

    observation_by_class: dict[str, list[tuple[int, int]]] = defaultdict(list)
    observation_rows = Counter()
    observation_complete_rows = Counter()
    for row in rows:
        event_class = row.get("event_class")
        if row.get("record_role") != "OBSERVATION" or event_class not in OBSERVATION_CLASSES:
            continue
        observation_rows[event_class] += 1
        if _span(row) is not None:
            observation_complete_rows[event_class] += 1
        span = _within_outer(
            row,
            clock_id=cpu_clock,
            outer_start=outer_start,
            outer_end=outer_end,
        )
        if span is not None:
            observation_by_class[event_class].append(span)

    target_spans = [span for spans in by_class.values() for span in spans]
    observation_spans = [span for spans in observation_by_class.values() for span in spans]
    combined_spans = target_spans + observation_spans
    outer_length_ns = outer_end - outer_start
    if union_length(combined_spans) != outer_length_ns:
        raise AssertionError(f"non-outer ledger does not cover outer interval: {ledger_path}")
    target_observation_overlap_ns = overlap_length(target_spans, observation_spans)

    native_rows = [
        row
        for row in rows
        if row.get("record_role") == "TARGET" and row.get("event_class") in NATIVE_CLASSES
    ]
    native_clocks = sorted({row.get("clock_id") for row in native_rows})
    native_not_cpu = sum(row.get("clock_id") != cpu_clock for row in native_rows)
    native_report = _native_join_report(rows)
    raw_report = _raw_join_report(identity, outer_span=outer_span, cpu_clock=cpu_clock)

    case = {
        "case_id": identity["case_id"],
        "instance_id": identity["instance_id"],
        "queue_ordinal": identity["queue_ordinal"],
        "input_path": str(input_path.relative_to(BASE)),
        "ledger_path": str(ledger_path.relative_to(BASE)),
        "outer_ms": outer_length_ns / 1_000_000.0,
        "outer_target_count": 1,
        "runner_wrapper_target_count": 1,
        "outer_wrapper_exact_duplicate": outer_span == wrapper_span,
        "cpu_clock_id": cpu_clock,
        "native_clock_ids": native_clocks,
        "native_rows_on_cpu_clock": len(native_rows) - native_not_cpu,
        "target_event_counts": dict(sorted(target_classes.items())),
        "eligible_target_components": {
            event_class: {
                "count": len(spans),
                "sum_ms": duration_ms(spans),
                "union_ms": union_length(spans) / 1_000_000.0,
            }
            for event_class, spans in sorted(by_class.items())
        },
        "observation_components": {
            event_class: {
                "row_count": observation_rows[event_class],
                "complete_row_count": observation_complete_rows[event_class],
                "union_ms": union_length(spans) / 1_000_000.0,
                "sum_ms": duration_ms(spans),
            }
            for event_class, spans in sorted(observation_by_class.items())
        },
        "target_union_ms": union_length(target_spans) / 1_000_000.0,
        "target_sum_ms": duration_ms(target_spans),
        "observation_union_ms": union_length(observation_spans) / 1_000_000.0,
        "observation_sum_ms": duration_ms(observation_spans),
        "target_observation_overlap_ms": target_observation_overlap_ns / 1_000_000.0,
        "outer_minus_target_union_ms": (
            outer_length_ns - union_length(target_spans)
        )
        / 1_000_000.0,
        "combined_union_ms": union_length(combined_spans) / 1_000_000.0,
        "combined_gap_ms": (outer_length_ns - union_length(combined_spans)) / 1_000_000.0,
        "important_overlaps": [
            _pair_report(by_class, pair) for pair in IMPORTANT_PAIRS
        ],
        "native_clock_mismatch_rows": native_not_cpu,
        "native_join": native_report,
        "raw_join": raw_report,
    }
    return case


def audit() -> dict[str, Any]:
    cases = _case_inputs()
    if len(cases) != 43:
        raise AssertionError(f"expected the 43 fully valid CPU cases, found {len(cases)}")
    case_reports = [
        _case_audit(input_path, identity, ledger_path, rows)
        for input_path, identity, ledger_path, rows in cases
    ]
    instance_ids = sorted({case["instance_id"] for case in case_reports})
    if len(instance_ids) != 22:
        raise AssertionError(f"expected 22 independent instances, found {len(instance_ids)}")

    def total(field: str) -> float:
        return sum(float(case[field]) for case in case_reports)

    target_class_counts = Counter()
    target_class_sum_ms = Counter()
    target_class_union_ms = Counter()
    for case in case_reports:
        for event_class, details in case["eligible_target_components"].items():
            target_class_counts[event_class] += details["count"]
            target_class_sum_ms[event_class] += details["sum_ms"]
            target_class_union_ms[event_class] += details["union_ms"]

    overlap_totals: dict[str, dict[str, Any]] = {}
    for pair in IMPORTANT_PAIRS:
        key = f"{pair[0]}|{pair[1]}"
        reports = [
            report
            for case in case_reports
            for report in case["important_overlaps"]
            if report["left"] == pair[0] and report["right"] == pair[1]
        ]
        overlap_totals[key] = {
            "left": pair[0],
            "right": pair[1],
            "overlapping_event_pairs": sum(r["overlapping_event_pairs"] for r in reports),
            "exact_duplicate_pairs": sum(r["exact_duplicate_pairs"] for r in reports),
            "overlap_ms": sum(r["overlap_ms"] for r in reports),
        }

    native_report = {
        "native_request_count": sum(
            case["native_join"]["native_request_count"] for case in case_reports
        ),
        "native_requests_with_all_four_rows": sum(
            case["native_join"]["native_requests_with_all_four_rows"] for case in case_reports
        ),
        "exact_native_to_model_request_joins": sum(
            case["native_join"]["exact_native_to_model_request_joins"] for case in case_reports
        ),
        "native_join_missing_or_duplicate": sum(
            case["native_join"]["native_join_missing_or_duplicate"] for case in case_reports
        ),
        "model_request_complete_rows": sum(
            case["native_join"]["model_request_complete_rows"] for case in case_reports
        ),
        "model_request_null_rows": sum(
            case["native_join"]["model_request_null_rows"] for case in case_reports
        ),
        "model_client_rows": sum(case["native_join"]["model_client_rows"] for case in case_reports),
        "model_client_physical_id_intersection_count": sum(
            case["native_join"]["model_client_ids_intersect_native_physical_ids"]
            for case in case_reports
        ),
        "model_client_pre_event_id_intersection_count": sum(
            case["native_join"]["model_client_pre_event_ids_intersect_native_pre_event_ids"]
            for case in case_reports
        ),
        "native_phase_interval_duplicate_requests": sum(
            case["native_join"]["native_phase_interval_duplicate_requests"]
            for case in case_reports
        ),
        "native_phase_sum_ms": sum(case["native_join"]["native_phase_sum_ms"] for case in case_reports),
        "native_e2e_sum_ms": sum(case["native_join"]["native_e2e_sum_ms"] for case in case_reports),
        "native_phase_sum_lt_e2e_requests": sum(
            case["native_join"]["native_phase_sum_lt_e2e_requests"] for case in case_reports
        ),
        "model_observation_eligibility": dict(
            sorted(
                sum(
                    (
                        Counter(case["native_join"]["model_observation_eligibility"])
                        for case in case_reports
                    ),
                    Counter(),
                ).items()
            )
        ),
    }

    raw_join_report = {
        "model_client_start_rows": sum(
            case["raw_join"]["model_client_start_rows"] for case in case_reports
        ),
        "model_client_terminal_rows": sum(
            case["raw_join"]["model_client_terminal_rows"] for case in case_reports
        ),
        "model_request_start_rows": sum(
            case["raw_join"]["model_request_start_rows"] for case in case_reports
        ),
        "model_request_terminal_rows": sum(
            case["raw_join"]["model_request_terminal_rows"] for case in case_reports
        ),
        "native_request_rows": sum(
            case["raw_join"]["native_request_rows"] for case in case_reports
        ),
        "exact_parent_logical_client_span_joins": sum(
            case["raw_join"]["exact_parent_logical_client_span_joins"]
            for case in case_reports
        ),
        "native_physical_id_joins": sum(
            case["raw_join"]["native_physical_id_joins"] for case in case_reports
        ),
        "model_request_rows_within_outer": sum(
            case["raw_join"]["model_request_rows_within_outer"] for case in case_reports
        ),
        "model_request_rows_on_cpu_outer_clock": sum(
            case["raw_join"]["model_request_rows_on_cpu_outer_clock"]
            for case in case_reports
        ),
        "model_request_rows_within_local_client_interval": sum(
            case["raw_join"]["model_request_rows_within_local_client_interval"]
            for case in case_reports
        ),
        "model_request_rows_outside_local_client_interval": sum(
            case["raw_join"]["model_request_rows_outside_local_client_interval"]
            for case in case_reports
        ),
        "native_e2e_rows_fitting_local_model_request_duration": sum(
            case["raw_join"]["native_e2e_rows_fitting_local_model_request_duration"]
            for case in case_reports
        ),
        "local_model_request_sum_ms": sum(
            case["raw_join"]["local_model_request_sum_ms"] for case in case_reports
        ),
        "native_e2e_sum_ms": sum(
            case["raw_join"]["native_e2e_sum_ms"] for case in case_reports
        ),
        "local_model_request_minus_native_e2e_ms": sum(
            case["raw_join"]["local_model_request_minus_native_e2e_ms"]
            for case in case_reports
        ),
    }

    source_entries = []
    case_by_queue_ordinal = {
        case["queue_ordinal"]: case for case in case_reports
    }
    for input_path, identity, ledger_path, _ in cases:
        raw_join = case_by_queue_ordinal[identity["queue_ordinal"]]["raw_join"]
        source_entries.extend(
            [
                {
                    "path": str(input_path.relative_to(BASE)),
                    "sha256": sha256_file(input_path),
                },
                {
                    "path": str(ledger_path.relative_to(BASE)),
                    "sha256": sha256_file(ledger_path),
                },
                {
                    "path": raw_join["raw_model_path"],
                    "sha256": raw_join["raw_model_sha256"],
                },
                {
                    "path": raw_join["raw_native_path"],
                    "sha256": raw_join["raw_native_sha256"],
                },
            ]
        )
    source_entries.sort(key=lambda entry: entry["path"])

    representative = next(case for case in case_reports if case["queue_ordinal"] == 1)
    representative_naive_sum_ms = (
        representative["target_sum_ms"]
        + representative["observation_sum_ms"]
    )

    result = {
        "schema": "d9.e2e-composition-audit.v1",
        "status": "unsupported_sequential_composition",
        "contract": (
            "The retained raw journals support a trace-conditioned native:e2e duration inside its "
            "local model_request envelope. No prospective full composition is fit or scored: it would "
            "still require an explicit supplied action/request trace and a non-overlapping topology."
        ),
        "scope": {
            "cpu_cases": len(case_reports),
            "independent_instances": len(instance_ids),
            "protected_labels_opened": False,
            "retry_invalid_cpu_cases_included": False,
            "gpu_runs": 0,
            "source": "retained cpu_lifecycle inputs, normalized ledgers, and raw model/native journals",
        },
        "source_hashes": {
            "source_entry_manifest_sha256": canonical_hash(source_entries),
            "entries": source_entries,
        },
        "aggregate": {
            "outer_ms": total("outer_ms"),
            "eligible_target_union_ms": total("target_union_ms"),
            "eligible_target_sum_ms": total("target_sum_ms"),
            "observation_union_ms": total("observation_union_ms"),
            "target_observation_overlap_ms": total("target_observation_overlap_ms"),
            "combined_non_outer_union_ms": total("combined_union_ms"),
            "combined_gap_ms": total("combined_gap_ms"),
            "outer_wrapper_exact_duplicates": sum(
                case["outer_wrapper_exact_duplicate"] for case in case_reports
            ),
            "target_components_within_outer": sum(
                case["outer_minus_target_union_ms"] < 0.000001 for case in case_reports
            ),
        },
        "target_class_counts": dict(sorted(target_class_counts.items())),
        "target_class_sum_ms": dict(sorted(target_class_sum_ms.items())),
        "target_class_union_ms": dict(sorted(target_class_union_ms.items())),
        "important_overlap_totals": overlap_totals,
        "native_join": native_report,
        "raw_join": raw_join_report,
        "representative_reconstruction": {
            "queue_ordinal": representative["queue_ordinal"],
            "instance_id": representative["instance_id"],
            "outer_ms": representative["outer_ms"],
            "eligible_target_union_ms": representative["target_union_ms"],
            "eligible_target_sum_ms": representative["target_sum_ms"],
            "observation_union_ms": representative["observation_union_ms"],
            "target_observation_overlap_ms": representative["target_observation_overlap_ms"],
            "combined_non_outer_union_ms": representative["combined_union_ms"],
            "combined_gap_ms": representative["combined_gap_ms"],
            "naive_additive_sum_of_target_and_observation_spans_ms": representative_naive_sum_ms,
            "naive_to_outer_ratio": representative_naive_sum_ms / representative["outer_ms"],
            "observation_components": representative["observation_components"],
            "important_overlaps": representative["important_overlaps"],
            "raw_join": representative["raw_join"],
        },
        "blockers": [
            {
                "code": "duplicate_outer_wrapper_boundary",
                "evidence": "outer_swe_agent and runner_process_wrapper have identical intervals in all 43 cases",
                "consequence": "Only one can enter an additive composition; adding both double-counts the full run.",
            },
            {
                "code": "nested_cpu_lifecycle_targets",
                "evidence": "get_state/runtime_command, startup/setup, script_read/client_processing/runtime_command and other pairs overlap",
                "consequence": "Per-event predictions cannot be added without a declared structural interval partition that is absent from the retained feature contract.",
            },
            {
                "code": "observation_only_outer_complement",
                "evidence": "Eligible CPU target union covers only about half of outer wall; model_client_call, model_request and unknown_residual observations fill the complement.",
                "consequence": "Model-client/request spans are not eligible lifecycle targets, and unknown residual has no pre-event feature contract; its measured duration cannot be a predictor.",
            },
            {
                "code": "normalized_adapter_omits_raw_request_identity",
                "evidence": "All 1,784 retained raw requests satisfy parent_event_id -> client start, shared logical_request_id, and client_span_id -> client terminal joins; normalized wrapper rows drop those fields and mark them model_eligible=false.",
                "consequence": "The identity join is recoverable offline from retained journals, but the compact normalized adapter cannot be used by itself for the conditional request composition. No new acquisition is implied.",
            },
            {
                "code": "asynchronous_client_request_boundary",
                "evidence": "All 1,784 raw model_request rows are on the CPU outer clock and within the outer interval; native:e2e fits the local model_request duration for all 1,784, while 75 request intervals extend beyond the local model_client_call terminal.",
                "consequence": "Use model_request as the local envelope for the directly joined native duration. Do not assume strict client-call nesting or union native intervals from their separate clock domain.",
            },
        ],
        "decision": {
            "composition_fit": "not_run",
            "composition_score": "unsupported_full_prospective",
            "instance_grouped_separation": {
                "groups": len(instance_ids),
                "full_composition_fit": "not_run_unsupported_topology",
            },
            "trace_conditioned_envelope": "supported_offline_identity_and_duration_bound",
            "direct_historical_e2e_comparison": "omitted_same_cohort_unavailable",
            "additional_runs_for_this_obstruction": False,
            "sample_uncertainty": (
                "No new runs are needed for the raw identity join or the local model_request envelope: both are recoverable from the retained journals. "
                "The remaining limitation is an offline model contract for supplied request/action topology and unknown residual features; measured residual time must remain diagnostic and never become a predictor. "
                "Numerical transfer or protected evaluation uncertainty would require separate runs only after that contract exists."
            ),
        },
        "cases": case_reports,
    }
    return result


def main() -> None:
    result = audit()
    HERE.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "cpu_cases": result["scope"]["cpu_cases"],
                "independent_instances": result["scope"]["independent_instances"],
                "outer_ms": result["aggregate"]["outer_ms"],
                "eligible_target_union_ms": result["aggregate"]["eligible_target_union_ms"],
                "combined_gap_ms": result["aggregate"]["combined_gap_ms"],
                "native_request_count": result["native_join"]["native_request_count"],
                "exact_native_to_model_request_joins": result["native_join"]["exact_native_to_model_request_joins"],
                "model_client_physical_id_intersection_count": result["native_join"]["model_client_physical_id_intersection_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
