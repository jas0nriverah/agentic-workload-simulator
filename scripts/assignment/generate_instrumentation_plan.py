#!/usr/bin/env python3
"""Build the offline, hash-bound Phase 3 instrumentation package.

The command plans work only.  It reads the pinned task/configuration files and
emits a new snapshot; it never starts a workload, evaluator, container, SSH
session, or model server.  The full inventory is the exact 1,088-row matrix
produced by :mod:`plan_matrix`.  The pilot is a separate 16-case namespace
whose case rows point back to the exact shared-baseline rows and resume keys.
Its eight repository categories are predeclared from historical
instrumentation failure/operation regimes; task choice inside each category
is hash-deterministic and outcome blind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "assignment"))
sys.path.insert(0, str(ROOT / "src"))
import plan_matrix  # noqa: E402

try:  # Bind the plan to the recorder actually integrated in this worktree.
    from agentic_sim.telemetry.features import FEATURE_BUILDER_ID, FEATURE_SCHEMA  # noqa: E402
    from agentic_sim.telemetry.v2 import (  # noqa: E402
        HARDWARE_SCHEMA,
        LIFECYCLE_SCHEMA,
        MANIFEST_SCHEMA,
        MODEL_SCHEMA,
        TELEMETRY_SCHEMA,
        TELEMETRY_VERSION,
        TOOL_SCHEMA,
    )
except ImportError:  # pragma: no cover - permits source-only planning checkouts.
    TELEMETRY_SCHEMA = "assignment.telemetry.v2"
    LIFECYCLE_SCHEMA = "assignment.telemetry.v2.lifecycle"
    TOOL_SCHEMA = "assignment.telemetry.v2.tool"
    MODEL_SCHEMA = "assignment.telemetry.v2.model"
    HARDWARE_SCHEMA = "assignment.telemetry.v2.hardware"
    MANIFEST_SCHEMA = "assignment.telemetry.v2.manifest"
    TELEMETRY_VERSION = "telemetry-v2-20260908"
    FEATURE_SCHEMA = "assignment.d9-feature.v2"
    FEATURE_BUILDER_ID = "d9-feature-builder.v2.action-boundary-20260908"


SNAPSHOT_ID = "20260908T140000Z-offline-v2"
INSTRUMENTATION_SCHEMA_VERSION = TELEMETRY_SCHEMA
PILOT_SCHEMA_VERSION = "assignment-instrumentation-pilot.v2"
REPLAY_SCHEMA_VERSION = "assignment-instrumentation-replay.v1"
EVALUATOR_SCHEMA_VERSION = "assignment-official-evaluator-config.v1"
SELECTION_ALGORITHM = "sha256_rank_mandatory_repository_categories_v2"
SELECTION_SEED = "assignment-instrumentation-pilot-20260908-v2"
MANDATORY_PILOT_REPOSITORIES = (
    "astropy/astropy",
    "django/django",
    "matplotlib/matplotlib",
    "pydata/xarray",
    "pytest-dev/pytest",
    "scikit-learn/scikit-learn",
    "sphinx-doc/sphinx",
    "sympy/sympy",
)
DEFAULT_PILOT_REPOSITORY_COUNT = 8
HOLDOUT_INSTANCE_ID = "sympy__sympy-12481"
BASELINE = {
    "call_limit": 30,
    "max_output_tokens": 2048,
    "observation_length": 100000,
    "temperature": 0.0,
}
REQUIRED_TASK_COUNTS = {"lite": 300, "verified": 500}
IMMUTABLE_PLAN_SHA256 = "2bead159a24e244ecbf981c63f3d43d24f8bf1fe9a389c398f17325d941069fc"


class PlanPackageError(ValueError):
    """Raised when the offline launch package cannot be generated safely."""


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


def _write_bytes(path: Path, payload: bytes) -> str:
    _atomic_write(path, payload)
    return sha256_bytes(payload)


def _write_json(path: Path, value: Any) -> str:
    return _write_bytes(path, (canonical_json(value) + "\n").encode("utf-8"))


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> str:
    payload = ("\n".join(canonical_json(row) for row in rows) + "\n").encode("utf-8")
    return _write_bytes(path, payload)


def _write_sidecar(path: Path, digest: str) -> None:
    _write_bytes(Path(f"{path}.sha256"), f"{digest}  {path.name}\n".encode("utf-8"))


def _write_json_hashed(path: Path, value: Any) -> str:
    digest = _write_json(path, value)
    _write_sidecar(path, digest)
    return digest


def _write_bytes_hashed(path: Path, payload: bytes) -> str:
    digest = _write_bytes(path, payload)
    _write_sidecar(path, digest)
    return digest


def _copy_hashed(source: Path, destination: Path) -> str:
    return _write_bytes_hashed(destination, source.read_bytes())


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    if not raw:
        raise PlanPackageError(f"task manifest is empty: {path}")
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            raise PlanPackageError(f"blank task-manifest line {path}:{line_number}")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise PlanPackageError(f"task-manifest line is not an object: {path}:{line_number}")
        rows.append(value)
    ids = [row.get("instance_id") for row in rows]
    if any(not isinstance(value, str) or not value for value in ids):
        raise PlanPackageError(f"every task row needs an instance_id: {path}")
    if len(set(ids)) != len(ids):
        raise PlanPackageError(f"duplicate instance_id in {path}")
    return rows, raw


def _git_metadata() -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            return subprocess.check_output(
                ["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None

    status = run("status", "--porcelain") or ""
    return {
        "repository": str(ROOT),
        "head_revision": run("rev-parse", "HEAD"),
        "worktree_dirty": bool(status),
        "status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def _rank(value: str) -> str:
    return sha256_bytes(f"{SELECTION_SEED}\0{value}".encode("utf-8"))


def _repository(row: Mapping[str, Any]) -> str:
    return str(row.get("repo") or row.get("repository") or row["instance_id"].split("__", 1)[0])


def select_pilot(
    tasks_by_suite: Mapping[str, list[dict[str, Any]]],
    *,
    repository_count: int = DEFAULT_PILOT_REPOSITORY_COUNT,
) -> tuple[list[str], list[dict[str, str]]]:
    """Select one task per suite/category from common repositories.

    Only identifiers and repository/category fields are read by this selector;
    rows containing status, outcomes, timing, or response content are never
    consulted.  The holdout instance is removed before ranking.
    """

    repositories = {}
    for suite, rows in tasks_by_suite.items():
        repositories[suite] = {
            _repository(row)
            for row in rows
            if row.get("instance_id") != HOLDOUT_INSTANCE_ID
        }
    common = sorted(repositories["lite"] & repositories["verified"])
    mandatory = list(MANDATORY_PILOT_REPOSITORIES)
    if repository_count != len(mandatory):
        raise PlanPackageError(
            "the pilot preselection is fixed at the eight mandatory repository categories"
        )
    missing = sorted(set(mandatory) - set(common))
    if missing:
        raise PlanPackageError(f"mandatory pilot categories are not shared by both suites: {missing}")
    selected_repositories = mandatory
    selected: list[dict[str, str]] = []
    for suite in ("lite", "verified"):
        for repository in selected_repositories:
            candidates = [
                row
                for row in tasks_by_suite[suite]
                if row.get("instance_id") != HOLDOUT_INSTANCE_ID
                and _repository(row) == repository
            ]
            if not candidates:
                raise PlanPackageError(f"pilot category {repository} has no {suite} task")
            task = min(
                candidates,
                key=lambda row: (
                    _rank(f"task\0{suite}\0{repository}\0{row['instance_id']}"),
                    row["instance_id"],
                ),
            )
            selected.append(
                {"instance_id": str(task["instance_id"]), "suite": suite, "category": repository}
            )
    selected.sort(
        key=lambda row: (
            0 if row["suite"] == "lite" else 1,
            row["category"],
            row["instance_id"],
        )
    )
    return selected_repositories, selected


def _pilot_case_id(suite: str, category: str, instance_id: str) -> str:
    identity = canonical_json([suite, category, instance_id]).encode("utf-8")
    return f"instrumentation-pilot-v1:{sha256_bytes(identity)}"


def _instrumentation_schema() -> dict[str, Any]:
    common = {
        "event_id": "stable per-event identity derived from case/attempt/sequence",
        "run_id": "stable matrix-case identity",
        "case_id": "sealed matrix case identity",
        "attempt_id": "append-only retry-attempt identity",
        "sequence": "strict non-negative stream sequence",
        "start_mono_ns": "CLOCK_MONOTONIC_RAW start boundary",
        "end_mono_ns": "CLOCK_MONOTONIC_RAW end boundary",
        "duration_ms": "non-negative end-start duration",
        "status": "success|failure|timeout|unavailable",
        "error_type": "nullable stable classification",
        "error_message": "nullable sanitized diagnostic",
        "availability": "measured|derived|declared|unavailable",
    }
    return {
        "schema_version": INSTRUMENTATION_SCHEMA_VERSION,
        "instrumentation_version": TELEMETRY_VERSION,
        "manifest_schema": MANIFEST_SCHEMA,
        "stream_schemas": {
            "lifecycle": LIFECYCLE_SCHEMA,
            "tool": TOOL_SCHEMA,
            "model": MODEL_SCHEMA,
            "hardware": HARDWARE_SCHEMA,
        },
        "feature_schema": {"schema_version": FEATURE_SCHEMA, "builder_id": FEATURE_BUILDER_ID},
        "serialization": {
            "encoding": "UTF-8",
            "json": "sort_keys=true, separators=(',', ':'), ensure_ascii=false",
            "event_order": "sequence ascending then event_id ascending",
            "missing_optional_fields": "JSON null plus availability=unavailable",
            "hash": "SHA-256 exact bytes; sidecar '<digest>  <filename>\\n'",
        },
        "identity": {
            "required": ["event_id", "case_id", "attempt_id", "sequence"],
            "retry_lineage": "new attempt_id and event identities; retry_of links the new record to its predecessor",
            "append_only": True,
            "reject_duplicate_event_id": True,
            "reject_negative_or_nonmonotonic_interval": True,
        },
        "stream_types": {
            "trajectory_lifecycle": {
                "required": {
                    **common,
                    "event_kind": "outer_e2e|setup|startup|client_processing|get_state|state_query|tool_execution|model_request|retry|failure|teardown|generic_wrapper|e2e_reconciliation|unknown_residual",
                    "phase": "closed-vocabulary lifecycle phase",
                    "parent_event_id": "nullable parent",
                    "reason": "nullable explicit gap/retry reason",
                },
                "rules": [
                    "Capture outer start/end and every measured phase interval.",
                    "Preserve failures, timeouts, and partial streams with explicit status.",
                    "Unknown residual is its own interval/category and is never reassigned to a measured phase.",
                ],
            },
            "cpu_tool": {
                "required": {
                    **common,
                    "event_kind": "tool_event",
                    "action": "pre-execution action token",
                    "executable": "executable name",
                    "subcommand": "nullable module/subcommand",
                    "script_path": "nullable canonical path",
                    "script_revision": "nullable pre-execution content revision",
                    "test_runner": "nullable normalized runner",
                    "test_scope": "nullable measured scope",
                    "traversal_mode": "none|find|find_exec|glob|walk|git_pathspec|other|unknown",
                    "pipeline": "ordered subprocess and pipe structure",
                    "path_scope": "nullable measured work scope",
                    "bytes_read": "nullable directly measured bytes",
                    "bytes_written": "nullable directly measured bytes",
                    "files_touched": "nullable directly measured count",
                    "subprocess_count": "nullable directly measured count",
                },
                "rules": [
                    "Capture action/executable/subcommand before execution.",
                    "find -exec and piped/unpiped git operations remain distinct.",
                    "Never turn an unmeasured count or volume into zero.",
                ],
            },
            "gpu_model_request": {
                "required": {
                    **common,
                    "event_kind": "model_request",
                    "request_id": "stable physical request identity",
                    "retry_of": "nullable prior request_id",
                    "retry_index": "non-negative retry number",
                    "model": "served model identifier",
                    "model_revision": "immutable 40-character revision",
                    "input_tokens": "measured or unavailable",
                    "output_tokens": "measured or unavailable",
                    "context_tokens": "measured or unavailable",
                    "queue_ms": "nullable serving queue timing",
                    "prefill_ms": "nullable serving prefill timing",
                    "decode_ms": "nullable serving decode timing",
                    "gpu_hardware": "hardware descriptor snapshot",
                },
                "rules": [
                    "Retain failed and timed-out requests.",
                    "Only serving-stack values with reliable request boundaries become timings.",
                    "Retries use new request_id and link retry_of.",
                ],
            },
            "hardware_snapshot": {
                "required": {
                    "event_id": "stable snapshot identity",
                    "timestamp_mono_ns": "monotonic timestamp",
                    "cpu_model": "actual CPU model",
                    "cpu_frequency_hz": "measured or unavailable",
                    "cpu_capabilities": "actual capability flags",
                    "storage": "only measured I/O fields used by the model",
                    "gpu_name": "actual GPU name",
                    "gpu_uuid": "actual UUID where exposed",
                    "gpu_compute_capability": "actual compute capability",
                    "gpu_memory_bytes": "actual VRAM",
                    "gpu_memory_bandwidth_bytes_per_s": "actual/spec value with source",
                    "gpu_compute_tflops": "actual/spec value with source",
                    "precision": "actual model precision",
                    "availability": "per-field measured|declared|unavailable",
                },
                "rules": [
                    "Only model-consumed hardware features are emitted.",
                    "Declared assumptions carry availability=declared and source text.",
                    "No synthetic thread-count scaling or fabricated device values.",
                ],
            },
        },
        "reconciliation": {
            "e2e_equation": "outer_end - outer_start = union(measured phase intervals) + unknown_residual_ms",
            "metric": "union(measured phase intervals) / outer E2E interval wall time",
            "successful_case_outer_wall_coverage_gate": {
                "minimum_fraction": 0.95,
                "unknown_max_fraction": 0.05,
                "unit": "actual outer E2E wall interval per successful case",
                "unknown_excluded_from_numerator": True,
                "category_label_count_is_insufficient": True,
            },
            "closure_tolerance": "max(1 ms, 0.1% of outer E2E wall interval)",
            "unknown_residual_policy": "report separately; never assign residual to CPU/GPU/model",
            "interval_union_policy": "merge overlapping measured intervals before attribution",
            "identity_coverage_gate": 1.0,
        },
        "overhead_replay": {
            "type": "fixed_workload_replay",
            "same_workload_required": True,
            "same_payload_cache_and_serving_policy": True,
            "pretrajectory_reset": "restore the same hash-bound per-case filesystem/repository snapshot before every repetition",
            "fixture_source": "each natural pilot captures an immutable action/request fixture before replay",
            "realized_equality_fields": ["workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256", "request_sequence_sha256", "output_token_count"],
            "invalid_pair_policy": "record the mismatch and exclude that pair from overhead estimation; never compare unequal work",
            "runner_command_template": [
                "python3", "scripts/assignment/sweagent_case_runner.py", "--case-spec", "{case_spec}",
                "--output-dir", "{attempt_output_dir}", "--runtime-manifest", "{runtime_manifest}", "--execute",
            ],
            "condition_toggle": "the reviewed runner integration supplies instrument_off or instrument_on; the exact mode and resulting telemetry directory are recorded in the attempt manifest",
            "control_and_treatment_must_share": ["case_spec", "runtime_manifest", "pretrajectory_snapshot", "fixture", "serving_endpoint", "cache_policy"],
            "conditions": ["instrument_off", "instrument_on"],
            "paired_repetitions_per_case": 3,
            "alternating_orders": ["off_on", "on_off", "off_on"],
            "median_relative_overhead_max": 0.05,
            "p95_relative_overhead_max": 0.10,
            "p95_definition": "nearest-rank across the 16 per-case medians",
            "budgeted_passes": 96,
            "natural_trajectories": "16 instrument-on baseline trajectories reported separately from fixed-work replay",
        },
        "train_serve_parity": {
            "feature_schema": "identical ordered names/types/units in train and serve",
            "serialization": "same canonical JSON and null/availability defaults",
            "pre_event_boundary": "features persisted before target event label is observable",
            "forbidden_features": [
                "outcome",
                "success status",
                "runtime label",
                "response content",
                "post-event byte/file/subprocess counts",
                "post-event request counts",
            ],
            "negative_tests_required": True,
        },
    }


def _evaluator_config(
    *, source_paths: Mapping[str, str], source_hashes: Mapping[str, str], adapter_path: str, adapter_sha256: str
) -> dict[str, Any]:
    return {
        "schema_version": EVALUATOR_SCHEMA_VERSION,
        "status": "offline_sealed_configuration",
        "purpose": "Official SWE-bench outcome evaluation for exact matrix rows; evaluator wall time is excluded from trajectory E2E.",
        "runner": {
            "module": "swebench.harness.run_evaluation",
            "command_template": [
                "python3", "-m", "swebench.harness.run_evaluation", "--dataset_name", "{dataset_name}",
                "--split", "test", "--instance_ids", "{instance_id}", "--predictions_path", "{prediction_path}",
                "--max_workers", "1", "--run_id", "{evaluator_run_id}", "--report_dir", "{evaluator_output_dir}",
            ],
            "one_instance_per_invocation": True,
            "max_workers": 1,
            "split": "test",
            "prediction_contract": "prediction is bound to suite, instance_id, attempt_id, model revision, and exact prediction bytes",
        },
        "datasets": {
            "lite": {
                "dataset_name": "SWE-bench/SWE-bench_Lite",
                "revision": "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
                "manifest_path": source_paths["lite"],
                "manifest_sha256": source_hashes["lite"],
                "task_count": 300,
            },
            "verified": {
                "dataset_name": "SWE-bench/SWE-bench_Verified",
                "revision": "91aa3ed51b709be6457e12d00300a6a596d4c6a3",
                "manifest_path": source_paths["verified"],
                "manifest_sha256": source_hashes["verified"],
                "task_count": 500,
            },
        },
        "adapter": {"module": "scripts/assignment/evaluate_swebench_case.py", "path": adapter_path, "sha256": adapter_sha256},
        "acceptance": {
            "official_evaluator_returncode": 0,
            "outcome_source": "official evaluator report only",
            "unresolved_outcome_policy": "retain explicit unavailable/unresolved record; never infer from runner return code",
            "evaluator_timing_excluded_from_e2e": True,
            "holdout_access": "forbidden",
        },
        "artifact_binding": {
            "evaluator_output_root": "matrix-runs/{case_index}/evaluator/",
            "prediction_root": "matrix-runs/{case_index}/prediction/",
            "required_hashes": ["prediction.json", "evaluator_result.json", "report.json", "command.json"],
        },
    }


def _build_holdout_split(
    *,
    baseline_cases: list[dict[str, Any]],
    task_rows: Mapping[str, list[dict[str, Any]]],
    historical_path: Path | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    full: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    baseline_by_key = {(row["suite"], row["instance_id"]): row for row in baseline_cases}
    for suite in ("lite", "verified"):
        for task in sorted(task_rows[suite], key=lambda row: row["instance_id"]):
            instance_id = str(task["instance_id"])
            case = baseline_by_key[(suite, instance_id)]
            is_holdout = instance_id == HOLDOUT_INSTANCE_ID
            record = {
                "record_type": "split_record",
                "schema_version": "assignment.event-split-manifest.v2",
                "suite": suite,
                "instance_id": instance_id,
                "cluster_id": f"instance:{instance_id}",
                "case_id": case["resume_key"],
                "assignment": "holdout" if is_holdout else "development",
                "pilot_eligible": False if is_holdout else True,
                "configuration_analysis_eligible": False if is_holdout else True,
                "final_outcome_access": False,
            }
            full.append(record)
            if is_holdout:
                holdout.append(record)
    if len(holdout) != 2:
        raise PlanPackageError(f"expected two suite copies of {HOLDOUT_INSTANCE_ID}, found {len(holdout)}")
    historical: dict[str, Any] = {
        "path": None,
        "sha256": None,
        "preserved_holdout_run_ids": [],
        "preserved_existing_split_if_viable": False,
    }
    if historical_path is not None and historical_path.exists():
        raw = historical_path.read_bytes()
        historical["path"] = str(historical_path)
        historical["sha256"] = sha256_bytes(raw)
        try:
            old = json.loads(raw.decode("utf-8"))
            old_ids = old.get("holdout_run_ids", []) if isinstance(old, dict) else []
            historical["preserved_holdout_run_ids"] = list(old_ids) if isinstance(old_ids, list) else []
            historical["preserved_existing_split_if_viable"] = bool(old_ids)
        except (json.JSONDecodeError, UnicodeDecodeError):
            historical["parse_status"] = "unavailable"
    split = {
        "schema_version": "assignment.event-split-manifest.v2",
        "split_unit": "instance_cluster",
        "status": "offline_presealed_no_final_outcome_access",
        "development_record_count": len(full) - len(holdout),
        "development_cluster_count": len({r["cluster_id"] for r in full if r["assignment"] == "development"}),
        "holdout_record_count": len(holdout),
        "holdout_cluster_count": len({r["cluster_id"] for r in holdout}),
        "holdout_instance_id": HOLDOUT_INSTANCE_ID,
        "holdout_excluded_from": ["pilot", "development", "configuration_analysis", "model_fit", "cv"],
        "no_final_outcome_access": True,
        "historical_split": historical,
        "preserve_both_suite_copies": True,
        "records": full,
    }
    return split, full, holdout


def _markdown_docs(output_dir: Path, *, plan_sha: str, pilot_sha: str, pilot_plan_sha: str) -> None:
    docs = {
        "recovery_resume.md": f"""# Recovery and resume procedure

This package is an offline plan. It has not started a workload, evaluator, model server, container, or SSH session.

1. Verify every adjacent SHA-256 sidecar and copy the package to durable storage as a new source.
2. Complete the remote process, H100, disk, source-pin, and artifact reconciliation gates. Record their results in a new snapshot; the local legacy retirement evidence does not attest to remote state.
3. Verify `full_matrix_case_inventory.jsonl` (`{plan_sha}`), `pilot_cases.json` (`{pilot_sha}`), and `pilot_plan.jsonl` (`{pilot_plan_sha}`).
4. Execute only the 16 `case_id`/`resume_key` values in `pilot_plan.jsonl`, at baseline settings, serially. Retry by the same resume key and append a new attempt identity.
5. For each natural pilot, capture a hash-bound action/request fixture and pretrajectory filesystem snapshot. Restore that snapshot before each of the 3 off/on paired repetitions (96 condition passes total), alternating AB/BA/AB. Record workload, action/request sequence, and output-token equality; mark a pair invalid rather than comparing unequal work.
6. Persist each event stream, prediction, evaluator output, lifecycle cursor, and sidecar atomically before advancing the cursor. Preserve partial failures and timeouts.
7. Run `scripts/validation/check_instrumentation_pilot.py` against hash-bound live evidence. Its quantitative coverage gate is actual union attribution over each successful case's outer E2E wall interval; category-label counts cannot satisfy it.
8. Astra reviews the pilot evidence. Only then may Luna launch the separate full 1,088-case matrix namespace.

The 25,000-character observation point is a treatment coordinate in the original sweep and is excluded from pilot instrumentation validation and any separate optimization experiment.
""",
        "disk_budget_plan.md": """# Disk-budget plan

Preflight a durable filesystem and continuous export before any remote launch. Measure the completed pilot's bytes per case, including raw event streams, append-only retries, stdout/stderr, predictions, evaluator reports, and sidecars. Project that measured bound to 1,088 cases with a declared 2x reserve, and stop at a declared free-space threshold before exhaustion. Export and hash each closed case before advancing the cursor. Historical snapshots and frozen D9 v3 artifacts are read-only inputs and are never reclaimed by this plan.
""",
        "failure_retry_policy.md": """# Failure and retry policy

Attempts and events are append-only. A retry receives a new `attempt_id` and links `retry_of`; it never overwrites an earlier attempt or copies a measured value. Preserve setup, tool, model-request, teardown, timeout, failure, and evaluator diagnostics. Retry an evaluator independently when a prediction exists. Mark outcomes unavailable when the official evaluator does not produce a result. Fail closed on identity, interval, hash, train/serve parity, or process-isolation violations. A partial stream remains partial and cannot be relabeled successful.
""",
        "artifact_hash_layout.md": """# Artifact and hash layout

Every generated file has a `<file>.sha256` sidecar containing `<digest>  <filename>`. JSON is UTF-8 canonical JSON with sorted keys, compact separators, and a terminal newline; JSONL preserves exact source bytes where marked.

- `source_manifests/`: exact Lite and Verified task JSONL bytes.
- `full_matrix_case_inventory.jsonl`: exact 1,088-case plan, 800 baseline plus 288 one-factor sweeps.
- `pilot_cases.json` and `pilot_plan.jsonl`: the separate 16-case pilot namespace and exact baseline case specifications.
- `pilot_replay_plan.json`: fixed-work replay contract for the same 16 cases.
- `split_manifest.json`/`holdout_inventory.json`: cluster split preserving both suite copies of the SymPy holdout.
- `instrumentation_schema_v2.json`: lifecycle, tool, model, hardware, reconciliation, replay, and train/serve contracts.
- `source_bundle_manifest.json`: path/hash/role inventory for review and later live evidence binding.
- `verification/legacy-retirement/retirement.json`: local retirement evidence; remote process noninterference remains unresolved.
- `run_manifest.json`: pins, counts, boundaries, gate thresholds, and unresolved remote checks.
""",
    }
    for name, text in docs.items():
        _write_bytes_hashed(output_dir / name, text.encode("utf-8"))


def _source_bundle_manifest(output_dir: Path) -> tuple[dict[str, Any], str]:
    roles = {
        "pilot_cases.json": "pilot_inventory",
        "pilot_plan.jsonl": "pilot_inventory",
        "pilot_replay_plan.json": "overhead_replay",
        "source_manifests/lite.jsonl": "source_bundle",
        "source_manifests/verified.jsonl": "source_bundle",
        "assignment_steps_1_3.json": "source_bundle",
        "full_matrix_case_inventory.jsonl": "source_bundle",
        "split_manifest.json": "source_bundle",
        "holdout_inventory.json": "source_bundle",
        "evaluator_config.json": "source_bundle",
        "evaluator_adapter.py": "source_bundle",
        "instrumentation_schema_v2.json": "feature_parity",
        "pin_evidence.json": "source_bundle",
        "run_manifest.json": "source_bundle",
        "validation/check_instrumentation_pilot.py": "feature_parity",
        "validation/audit_v2_journals.py": "feature_parity",
        "telemetry/v2.py": "feature_parity",
        "telemetry/features.py": "feature_parity",
        "generators/generate_instrumentation_plan.py": "source_bundle",
        "verification/legacy-retirement/retirement.json": "remote_reconciliation",
        "remote_reconciliation/README.md": "remote_reconciliation",
    }
    files = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.name.endswith(".sha256") or path.name == "source_bundle_manifest.json":
            continue
        relative = path.relative_to(output_dir).as_posix()
        files.append({"path": relative, "sha256": sha256_file(path), "role": roles.get(relative, "documentation")})
    manifest = {
        "schema_version": "assignment-source-bundle-manifest.v1",
        "status": "offline_hash_bound_remote_reconciliation_pending",
        "hash_algorithm": "sha256",
        "files": files,
        "required_live_evidence_roles": [
            "pilot_inventory", "event_journals", "overhead_replay", "source_bundle", "remote_reconciliation", "feature_parity"
        ],
        "remote_artifacts": "not present in this offline snapshot",
    }
    digest = _write_json_hashed(output_dir / "source_bundle_manifest.json", manifest)
    return manifest, digest


def _local_pin_evidence(
    *, config: Mapping[str, Any], output_dir: Path, runtime_evidence: Path | None, evaluator_adapter: Path | None
) -> tuple[dict[str, Any], str, str, str | None]:
    external_root = ROOT.parent / "h100-assignment-work-20260905"
    repos = {
        "swe_agent": external_root / "repos" / "SWE-agent",
        "swe_bench": external_root / "repos" / "SWE-bench",
    }
    repo_evidence = {}
    for name, path in repos.items():
        head = None
        try:
            head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
        pyproject = path / "pyproject.toml"
        repo_evidence[name] = {
            "path": str(path),
            "head": head,
            "expected_head": config["pins"]["swe_agent_revision" if name == "swe_agent" else "swe_bench_revision"],
            "head_matches_expected": head == config["pins"]["swe_agent_revision" if name == "swe_agent" else "swe_bench_revision"],
            "pyproject_sha256": sha256_file(pyproject) if pyproject.exists() else None,
            "local_status": "locally_inspected",
            "remote_status": "unresolved",
        }
    runtime_copy_path = output_dir / "pin_evidence" / "runtime_manifest_worker_00.json"
    runtime_sha = None
    if runtime_evidence is not None and runtime_evidence.exists():
        runtime_sha = _copy_hashed(runtime_evidence, runtime_copy_path)
    adapter_sha = None
    adapter_rel = None
    if evaluator_adapter is not None and evaluator_adapter.exists():
        adapter_rel = "evaluator_adapter.py"
        adapter_sha = _copy_hashed(evaluator_adapter, output_dir / adapter_rel)
    context_sources = []
    for candidate in (
        external_root / "assignment" / "runtime" / "manifests-reviewed-c8d-source-exact-20260905-14-context65536-evaluator-venv" / "MANIFEST_SET_PROVENANCE.json",
        external_root / "assignment" / "wiring-validation-c8d-source-exact-20260905-19-context65536-evaluator-venv" / "worker-04" / "runner_attempts" / "attempt-001" / "run_batch.config.yaml",
    ):
        if candidate.exists():
            context_sources.append({"path": str(candidate), "sha256": sha256_file(candidate)})
    evidence = {
        "schema_version": "assignment-pin-evidence.v1",
        "status": "local_evidence_remote_verification_pending",
        "model": {
            "name": config["pins"]["model"],
            "revision": config["pins"]["model_revision"],
            "tokenizer_revision": config["pins"]["tokenizer_revision"],
            "local_status": "declared_in_checked_in_runtime_manifest" if runtime_sha else "declared_in_config_only",
            "remote_status": "unresolved",
        },
        "swe_agent": repo_evidence["swe_agent"],
        "swe_bench": repo_evidence["swe_bench"],
        "vllm": {
            "version": config["pins"]["vllm_version"],
            "revision": None,
            "image_digest": None,
            "local_status": "version_declared; package/image not locally verified",
            "remote_status": "unresolved",
        },
        "context_contract": {
            "client_max_input_tokens": 32768,
            "serving_max_model_len": 65536,
            "separate_semantics": True,
            "source": context_sources,
            "local_status": "context65536 local artifacts identified; exact launch field still requires review",
            "remote_status": "unresolved",
            "do_not_silently_use_server_32768": True,
        },
        "evaluator": {
            "module": "swebench.harness.run_evaluation",
            "adapter_path": adapter_rel,
            "adapter_sha256": adapter_sha,
            "local_status": "adapter_and_swe_bench_revision_inspected" if adapter_sha else "revision_only",
            "remote_status": "unresolved",
        },
        "runtime_manifest": {
            "path": "pin_evidence/runtime_manifest_worker_00.json" if runtime_sha else None,
            "sha256": runtime_sha,
            "remote_status": "unresolved",
        },
        "source_hash_finalization": "pending_root_after_integrations",
    }
    digest = _write_json_hashed(output_dir / "pin_evidence.json", evidence)
    return evidence, digest, adapter_rel or "", adapter_sha


def generate_package(
    *,
    config_path: Path,
    lite_tasks: Path,
    verified_tasks: Path,
    output_dir: Path,
    snapshot_id: str = SNAPSHOT_ID,
    pilot_repository_count: int = DEFAULT_PILOT_REPOSITORY_COUNT,
    historical_split: Path | None = None,
    runtime_evidence: Path | None = None,
    evaluator_adapter: Path | None = None,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise PlanPackageError(f"refusing to overwrite non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    config = plan_matrix.load_config(config_path)
    lite_rows, lite_raw = _read_jsonl(lite_tasks)
    verified_rows, verified_raw = _read_jsonl(verified_tasks)
    task_rows = {"lite": lite_rows, "verified": verified_rows}
    for suite, rows in task_rows.items():
        if len(rows) != REQUIRED_TASK_COUNTS[suite]:
            raise PlanPackageError(f"{suite} requires exactly {REQUIRED_TASK_COUNTS[suite]} rows, got {len(rows)}")
        if sum(row.get("instance_id") == HOLDOUT_INSTANCE_ID for row in rows) != 1:
            raise PlanPackageError(f"{suite} must contain exactly one {HOLDOUT_INSTANCE_ID} holdout row")

    manifests = {
        "lite": plan_matrix.load_task_manifest(lite_tasks, "lite"),
        "verified": plan_matrix.load_task_manifest(verified_tasks, "verified"),
    }
    config_sha = sha256_file(config_path)
    plan_rows = plan_matrix.build_plan(config, manifests, config_sha256=config_sha)
    cases = plan_rows[1:]
    if len(cases) != 1088:
        raise PlanPackageError(f"expected 1,088 cases, got {len(cases)}")
    baseline_cases = [row for row in cases if row["cell_id"] == "shared-baseline"]
    sweep_cases = [row for row in cases if row["cell_id"] != "shared-baseline"]
    if len(baseline_cases) != 800 or len(sweep_cases) != 288:
        raise PlanPackageError(f"expected 800 baseline + 288 sweeps, got {len(baseline_cases)} + {len(sweep_cases)}")
    if any(row["settings"] != BASELINE for row in baseline_cases):
        raise PlanPackageError("baseline settings must be exactly 30/2048/100000/0")
    if sum(row.get("variation", {}).get("knob") == "observation_length" and row.get("variation", {}).get("value") == 25000 for row in sweep_cases) != 24:
        raise PlanPackageError("the 25,000 observation sweep must contain exactly 24 treatment cases")

    source_dir = output_dir / "source_manifests"
    lite_copy = source_dir / "lite.jsonl"
    verified_copy = source_dir / "verified.jsonl"
    lite_sha = _write_bytes_hashed(lite_copy, lite_raw)
    verified_sha = _write_bytes_hashed(verified_copy, verified_raw)
    config_copy = output_dir / "assignment_steps_1_3.json"
    config_copy_sha = _copy_hashed(config_path, config_copy)
    plan_path = output_dir / "full_matrix_case_inventory.jsonl"
    plan_sha = _write_bytes_hashed(plan_path, plan_matrix.render_jsonl(plan_rows))
    historical_plan = ROOT.parent / "h100-assignment-work-20260905" / "assignment" / "plan" / "sealed_plan.jsonl"
    if historical_plan.exists() and sha256_file(historical_plan) != plan_sha:
        raise PlanPackageError("generated full matrix differs from immutable sealed original plan")
    if historical_plan.exists() and plan_sha != IMMUTABLE_PLAN_SHA256:
        raise PlanPackageError("immutable original plan SHA-256 changed")

    selected_repositories, selected = select_pilot(task_rows, repository_count=pilot_repository_count)
    baseline_by_key = {(row["suite"], row["instance_id"]): row for row in baseline_cases}
    pilot_entries: list[dict[str, Any]] = []
    for item in selected:
        case = baseline_by_key[(item["suite"], item["instance_id"])]
        pilot_id = _pilot_case_id(item["suite"], item["category"], item["instance_id"])
        entry = {
            "case_id": case["resume_key"],
            "resume_key": case["resume_key"],
            "pilot_case_id": pilot_id,
            "plan_case_sha256": sha256_bytes(canonical_json(case).encode("utf-8")),
            "suite": case["suite"],
            "instance_id": case["instance_id"],
            "repository": case["repository"],
            "category": item["category"],
            "cell_id": case["cell_id"],
            "steps": case["steps"],
            "roles": case["roles"],
            "settings": case["settings"],
            "task_sha256": case["task_sha256"],
            "source_manifest_sha256": case["source_manifest_sha256"],
            "selection_outcome_blind": True,
        }
        pilot_entries.append(entry)
    if len(pilot_entries) != 16 or len({entry["case_id"] for entry in pilot_entries}) != 16:
        raise PlanPackageError("pilot must contain 16 unique baseline case IDs")
    if any(entry["instance_id"] == HOLDOUT_INSTANCE_ID for entry in pilot_entries):
        raise PlanPackageError("holdout entered pilot")

    pilot = {
        "schema_version": PILOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "status": "offline_preselected_not_launched",
        "namespace": "instrumentation-pilot-v1",
        "selection": {
            "algorithm": SELECTION_ALGORITHM,
            "seed": SELECTION_SEED,
            "category_definition": "repository",
            "repository_categories": selected_repositories,
            "category_selection_justification": "predeclared historical instrumentation failure/operation regimes; no outcomes, timings, status, or response content used",
            "cases_per_suite": pilot_repository_count,
            "total_cases": len(pilot_entries),
            "outcome_blind": True,
            "holdout_excluded_before_selection": HOLDOUT_INSTANCE_ID,
            "selection_inputs": {"lite_manifest_sha256": lite_sha, "verified_manifest_sha256": verified_sha},
        },
        "execution": {
            "settings": BASELINE,
            "role": "step_1_baseline",
            "repeats": 1,
            "concurrency": 1,
            "resume_identity": "case_id equals exact full-matrix baseline resume_key; retries append attempt_id",
            "instrumentation_replay": "fixed-work replay is described in pilot_replay_plan.json and uses the same 16 IDs",
            "natural_trajectory_count": 16,
            "observation_limit_experiment_separation": "25,000 is an original sweep treatment only; it is excluded from pilot instrumentation and separate optimization",
        },
        "cases": pilot_entries,
    }
    pilot_path = output_dir / "pilot_cases.json"
    pilot_sha = _write_json_hashed(pilot_path, pilot)
    pilot_header = dict(plan_rows[0])
    pilot_header.update({"namespace": "instrumentation-pilot-v1", "execution_case_count": 16, "pilot_only": True})
    pilot_plan_rows = [pilot_header]
    for entry in pilot_entries:
        case = dict(baseline_by_key[(entry["suite"], entry["instance_id"])])
        case.update({"namespace": "instrumentation-pilot-v1", "pilot_case_id": entry["pilot_case_id"], "pilot_only": True})
        pilot_plan_rows.append(case)
    pilot_plan_path = output_dir / "pilot_plan.jsonl"
    pilot_plan_sha = _write_bytes_hashed(pilot_plan_path, plan_matrix.render_jsonl(pilot_plan_rows))
    replay = {
        "schema_version": REPLAY_SCHEMA_VERSION,
        "status": "offline_protocol_not_run",
        "namespace": "instrumentation-pilot-replay-v1",
        "case_ids": [entry["case_id"] for entry in pilot_entries],
        "pilot_case_ids": [entry["pilot_case_id"] for entry in pilot_entries],
        "workload": {
            "same_recorded_workload_required": True,
            "same_payload_cache_and_serving_policy_required": True,
            "pretrajectory_reset": "restore the same hash-bound per-case filesystem/repository snapshot before every repetition",
            "fixture_source": "each natural pilot captures an immutable action/request fixture before replay",
            "realized_equality_fields": ["workload_sha256", "pretrajectory_snapshot_sha256", "action_sequence_sha256", "request_sequence_sha256", "output_token_count"],
            "workload_sha256": "record_at_live_capture",
        },
        "conditions": {"control": "instrument_off", "treatment": "instrument_on"},
        "paired_repetitions": 3,
        "budgeted_passes": 96,
        "orders_by_repeat": {"0": "AB", "1": "BA", "2": "AB"},
        "thresholds": {"median_relative_overhead_max": 0.05, "p95_relative_overhead_max": 0.10, "p95": "nearest-rank across 16 per-case medians"},
        "invalid_pair_policy": "record any realized workload/output-token mismatch and exclude that pair; never compare unequal work",
        "runner_command_template": [
            "python3", "scripts/assignment/sweagent_case_runner.py", "--case-spec", "{case_spec}",
            "--output-dir", "{attempt_output_dir}", "--runtime-manifest", "{runtime_manifest}", "--execute",
        ],
        "condition_toggle": "reviewed runner integration records instrument_off or instrument_on in each attempt manifest",
        "control_and_treatment_must_share": ["case_spec", "runtime_manifest", "pretrajectory_snapshot", "fixture", "serving_endpoint", "cache_policy"],
        "natural_trajectories": {"count": 16, "condition": "instrument_on", "reported_separately": True},
        "no_outcomes": True,
    }
    replay_path = output_dir / "pilot_replay_plan.json"
    replay_sha = _write_json_hashed(replay_path, replay)

    split, split_records, holdout_records = _build_holdout_split(
        baseline_cases=baseline_cases, task_rows=task_rows, historical_path=historical_split
    )
    split_path = output_dir / "split_manifest.json"
    split_sha = _write_json_hashed(split_path, split)
    holdout_inventory = {
        "schema_version": "assignment-holdout-inventory.v1",
        "status": "presealed_outcome_blind",
        "split_unit": "instance_cluster",
        "instance_id": HOLDOUT_INSTANCE_ID,
        "excluded_from": ["pilot", "development", "configuration_analysis", "model_fit", "cv"],
        "no_final_outcome_access": True,
        "suite_copies": [{"suite": row["suite"], "case_id": row["case_id"], "cluster_id": row["cluster_id"]} for row in holdout_records],
        "historical_split": split["historical_split"],
    }
    holdout_path = output_dir / "holdout_inventory.json"
    holdout_sha = _write_json_hashed(holdout_path, holdout_inventory)
    development_path = output_dir / "development_inventory.jsonl"
    development_sha = _write_jsonl(development_path, [row for row in split_records if row["assignment"] == "development"])
    _write_sidecar(development_path, development_sha)

    _, pin_sha, adapter_rel, adapter_sha = _local_pin_evidence(
        config=config, output_dir=output_dir, runtime_evidence=runtime_evidence, evaluator_adapter=evaluator_adapter
    )
    source_paths = {"lite": "source_manifests/lite.jsonl", "verified": "source_manifests/verified.jsonl"}
    evaluator = _evaluator_config(
        source_paths=source_paths,
        source_hashes={"lite": lite_sha, "verified": verified_sha},
        adapter_path=adapter_rel or "evaluator_adapter.py",
        adapter_sha256=adapter_sha or "pending_local_evidence",
    )
    evaluator_path = output_dir / "evaluator_config.json"
    evaluator_sha = _write_json_hashed(evaluator_path, evaluator)
    schema_path = output_dir / "instrumentation_schema_v2.json"
    schema = _instrumentation_schema()
    schema_sha = _write_json_hashed(schema_path, schema)
    _copy_hashed(ROOT / "scripts" / "validation" / "check_instrumentation_pilot.py", output_dir / "validation" / "check_instrumentation_pilot.py")
    _copy_hashed(ROOT / "scripts" / "validation" / "audit_v2_journals.py", output_dir / "validation" / "audit_v2_journals.py")
    _copy_hashed(ROOT / "src" / "agentic_sim" / "telemetry" / "v2.py", output_dir / "telemetry" / "v2.py")
    _copy_hashed(ROOT / "src" / "agentic_sim" / "telemetry" / "features.py", output_dir / "telemetry" / "features.py")
    _copy_hashed(Path(__file__).resolve(), output_dir / "generators" / "generate_instrumentation_plan.py")
    retirement_source = output_dir.parent / "verification" / "legacy-retirement" / "retirement.json"
    retirement_sha = None
    if retirement_source.exists():
        retirement_sha = _copy_hashed(retirement_source, output_dir / "verification" / "legacy-retirement" / "retirement.json")
    _write_bytes_hashed(output_dir / "remote_reconciliation" / "README.md", b"# Remote reconciliation\n\nRemote artifacts, process checks, H100 ownership, disk evidence, and source verification are unresolved in this offline snapshot. Root must add hash-bound evidence after integration.\n")
    _markdown_docs(output_dir, plan_sha=plan_sha, pilot_sha=pilot_sha, pilot_plan_sha=pilot_plan_sha)

    full_counts = Counter(row["suite"] for row in baseline_cases)
    sweep_counts = Counter(row["variation"]["knob"] for row in sweep_cases)
    run_manifest = {
        "schema_version": "assignment-instrumentation-run-manifest.v2",
        "snapshot_id": snapshot_id,
        "status": "offline_ready_remote_verification_pending",
        "launch_authorized": False,
        "offline_only": True,
        "purpose": "Candidate Phase 3 instrumentation package; remote restoration, pin/process/disk checks, and pilot gates remain unresolved.",
        "matrix": {
            "plan_id": config["plan_id"],
            "namespace": "assignment-case-v1",
            "full_case_count": len(cases),
            "baseline_case_count": len(baseline_cases),
            "baseline_by_suite": dict(sorted(full_counts.items())),
            "sweep_case_count": len(sweep_cases),
            "sweep_by_knob": dict(sorted(sweep_counts.items())),
            "baseline_settings": BASELINE,
            "baseline_reuse": "Step 1 baseline rows are reused at shared-baseline coordinates; the 96 plotting copies in historical sweep views are not new executions",
            "plotting_copies": {"count": 96, "new_executions": 0, "source": "historical sweep views"},
            "full_matrix_separate_from_pilot": True,
        },
        "pins": {
            "model": config["pins"]["model"],
            "model_revision": config["pins"]["model_revision"],
            "tokenizer_revision": config["pins"]["tokenizer_revision"],
            "swe_agent_revision": config["pins"]["swe_agent_revision"],
            "swe_bench_revision": config["pins"]["swe_bench_revision"],
            "vllm_version": config["pins"]["vllm_version"],
            "precision": "bfloat16",
            "client_max_input_tokens": 32768,
            "serving_max_model_len": 65536,
            "context_semantics": "client max_input_tokens=32768; serving max_model_len=65536; exact launch-field verification remains a pre-pilot gate",
            "vllm_tool_parser": "qwen3_coder",
            "evidence_path": "pin_evidence.json",
            "remote_verification": "unresolved",
        },
        "baseline_configuration": {
            **BASELINE,
            "top_p": 1.0,
            "seed": 0,
            "instrumentation_level": "v2",
            "client_max_input_tokens": 32768,
            "serving_max_model_len": 65536,
        },
        "separate_experiments": [{
            "name": "observation_limit_25000",
            "status": "original_sweep_treatment_only",
            "treatment_case_count": 24,
            "observation_length": 25000,
            "excluded_from_instrumentation_pilot": True,
            "excluded_from_separate_optimization_experiment": True,
        }],
        "pilot": {
            "namespace": "instrumentation-pilot-v1",
            "path": "pilot_cases.json",
            "sha256": pilot_sha,
            "plan_path": "pilot_plan.jsonl",
            "plan_sha256": pilot_plan_sha,
            "selection": {
                "algorithm": SELECTION_ALGORITHM,
                "seed": SELECTION_SEED,
                "repository_categories": selected_repositories,
                "category_justification": "predeclared historical instrumentation failure/operation regimes; deterministic task hash within each; outcome blind",
            },
            "case_count": 16,
            "suite_counts": {"lite": 8, "verified": 8},
            "case_ids": [entry["case_id"] for entry in pilot_entries],
            "pilot_case_ids": [entry["pilot_case_id"] for entry in pilot_entries],
            "gate": {
                "event_identity_coverage_fraction": 1.0,
                "successful_case_outer_e2e_interval_union_attribution_fraction_min": 0.95,
                "successful_case_unknown_residual_fraction_max": 0.05,
                "coverage_denominator": "actual outer E2E wall interval per successful case",
                "coverage_numerator": "union of measured phase intervals; UNKNOWN excluded",
                "category_label_count_is_not_a_gate": True,
                "closure_tolerance": "max(1 ms, 0.1% outer E2E wall interval)",
                "paired_overhead_median_relative_max": 0.05,
                "paired_overhead_p95_relative_max": 0.10,
                "replay_repetitions": 3,
                "replay_orders": ["AB", "BA", "AB"],
                "natural_trajectory_count": 16,
                "checker": "validation/check_instrumentation_pilot.py",
                "launch_authorized": False,
            },
        },
        "split": {
            "path": "split_manifest.json",
            "sha256": split_sha,
            "unit": "instance_cluster",
            "holdout_inventory_path": "holdout_inventory.json",
            "holdout_inventory_sha256": holdout_sha,
            "holdout_instance_id": HOLDOUT_INSTANCE_ID,
            "holdout_suite_copy_count": 2,
            "no_final_outcome_access": True,
        },
        "inputs": {
            "assignment_config": {"path": "assignment_steps_1_3.json", "sha256": config_copy_sha},
            "lite_manifest": {"path": "source_manifests/lite.jsonl", "sha256": lite_sha, "row_count": 300},
            "verified_manifest": {"path": "source_manifests/verified.jsonl", "sha256": verified_sha, "row_count": 500},
            "full_matrix_case_inventory": {"path": "full_matrix_case_inventory.jsonl", "sha256": plan_sha},
            "pilot_replay_plan": {"path": "pilot_replay_plan.json", "sha256": replay_sha},
            "evaluator_config": {"path": "evaluator_config.json", "sha256": evaluator_sha},
            "instrumentation_schema": {"path": "instrumentation_schema_v2.json", "sha256": schema_sha},
            "pin_evidence": {"path": "pin_evidence.json", "sha256": pin_sha},
            "development_inventory": {"path": "development_inventory.jsonl", "sha256": development_sha},
        },
        "execution_policy": {
            "concurrency": 1,
            "per_case_deadline_seconds": config["execution_limits"]["per_case_deadline_seconds"],
            "global_deadline_seconds": config["execution_limits"]["global_deadline_seconds"],
            "recovery": "append-only attempts keyed by exact resume_key; durable case export before cursor advance",
            "failure_retry": "see failure_retry_policy.md",
            "disk_budget": "see disk_budget_plan.md",
            "artifact_layout": "see artifact_hash_layout.md",
        },
        "instrumentation": {
            "schema": TELEMETRY_SCHEMA,
            "version": TELEMETRY_VERSION,
            "stream_schemas": {
                "lifecycle": LIFECYCLE_SCHEMA,
                "tool": TOOL_SCHEMA,
                "model": MODEL_SCHEMA,
                "hardware": HARDWARE_SCHEMA,
            },
            "feature_schema": FEATURE_SCHEMA,
            "feature_builder_id": FEATURE_BUILDER_ID,
            "schema_path": "instrumentation_schema_v2.json",
            "schema_sha256": schema_sha,
            "recorder_source_paths": ["telemetry/v2.py", "telemetry/features.py"],
            "pilot_checker": "validation/check_instrumentation_pilot.py",
            "disk_auditor": "validation/audit_v2_journals.py",
        },
        "historical_boundaries": {
            "frozen_d9_v3_semantic_median_immutable": True,
            "prior_d9_reports_hashes_immutable": True,
            "original_800_case_d1_measurements_immutable": True,
            "prior_submission_snapshots_immutable": True,
            "no_holdout_outcomes_read": True,
            "no_ssh_or_live_workload_in_generation": True,
        },
        "remote_verification": {
            "status": "unresolved_offline",
            "required_before_pilot": [
                "restore/reconcile remote artifacts against this source bundle",
                "recheck local and remote legacy monitor/TCP bridge noninterference",
                "verify Qwen model/tokenizer, SWE-agent, SWE-bench, vLLM, and evaluator revisions",
                "verify one dedicated H100 and no concurrent GPU traffic",
                "verify durable disk, continuous export, and artifact hashes",
            ],
        },
        "legacy_retirement": {
            "local_status": "done" if retirement_sha else "evidence_unavailable",
            "local_evidence_path": "verification/legacy-retirement/retirement.json" if retirement_sha else None,
            "local_evidence_sha256": retirement_sha,
            "scope": "nine historical local processes were retired; remote processes and any new replacements still require mandatory recheck",
            "remote_status": "unresolved",
        },
        "source_hash_finalization": "pending_root_after_integrations",
        "git": _git_metadata(),
    }
    manifest_path = output_dir / "run_manifest.json"
    manifest_sha = _write_json_hashed(manifest_path, run_manifest)
    readme = f"""# Offline v2 launch package ({snapshot_id})

Generated without SSH, H100 access, workload execution, evaluator execution, model serving, or holdout outcome access.

- Full matrix: 1,088 exact cases (`{plan_sha}`), comprising 800 baseline cases and 288 one-factor sweep cases.
- Baseline: call limit 30, max output tokens 2048, observation length 100,000, temperature 0.
- Pilot namespace: 16 exact baseline case IDs (`{pilot_sha}`), with `pilot_plan.jsonl` binding each to its original resume key.
- Fixed-work replay: three paired repetitions per pilot case, alternating AB/BA/AB (96 condition passes); restore each case's pretrajectory snapshot and invalidate unequal workload/output-token pairs. The 16 natural instrument-on trajectories are separate.
- 25,000 observation length: original sweep treatment only, 24 cases; excluded from pilot validation and separate optimization.
- Instrumentation schema: `{schema_sha}`. Run manifest: `{manifest_sha}`.

Remote source/pin/process/disk verification and live pilot evidence remain unresolved. Root must finalize integrated source hashes after the remaining artifacts are added.
"""
    _write_bytes_hashed(output_dir / "README.md", readme.encode("utf-8"))
    bundle, bundle_sha = _source_bundle_manifest(output_dir)
    return {
        "output_dir": str(output_dir),
        "full_matrix_sha256": plan_sha,
        "pilot_sha256": pilot_sha,
        "pilot_plan_sha256": pilot_plan_sha,
        "pilot_replay_sha256": replay_sha,
        "split_sha256": split_sha,
        "instrumentation_schema_sha256": schema_sha,
        "run_manifest_sha256": manifest_sha,
        "source_bundle_sha256": bundle_sha,
        "full_case_count": len(cases),
        "pilot_case_count": len(pilot_entries),
        "selected_repositories": selected_repositories,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "assignment_steps_1_3.json")
    parser.add_argument("--lite-tasks", type=Path, required=True)
    parser.add_argument("--verified-tasks", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snapshot-id", default=SNAPSHOT_ID)
    parser.add_argument("--pilot-repository-count", type=int, default=DEFAULT_PILOT_REPOSITORY_COUNT)
    parser.add_argument("--historical-split", type=Path)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--evaluator-adapter", type=Path, default=ROOT / "scripts" / "assignment" / "evaluate_swebench_case.py")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = generate_package(
            config_path=args.config,
            lite_tasks=args.lite_tasks,
            verified_tasks=args.verified_tasks,
            output_dir=args.output_dir,
            snapshot_id=args.snapshot_id,
            pilot_repository_count=args.pilot_repository_count,
            historical_split=args.historical_split,
            runtime_evidence=args.runtime_evidence,
            evaluator_adapter=args.evaluator_adapter,
        )
    except (OSError, PlanPackageError, json.JSONDecodeError) as exc:
        print(f"offline instrumentation plan: BLOCKED: {exc}", file=sys.stderr)
        return 2
    print("offline instrumentation plan: PASS")
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
