#!/usr/bin/env python3
"""Plan the deterministic Steps 1-3 assignment matrix without executing it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "assignment-steps-1-3-plan.v1"
REQUIRED_SUITES = ("lite", "verified")
REQUIRED_KNOBS = (
    "call_limit",
    "max_output_tokens",
    "observation_length",
    "temperature",
)
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class PlanError(ValueError):
    """Raised when a matrix cannot be planned without weakening its contract."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PlanError(message)


def _walk_revisions(value: Any, location: str = "config") -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{location}.{key}"
            if key == "revision" or key.endswith("_revision"):
                yield child, item
            yield from _walk_revisions(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_revisions(item, f"{location}[{index}]")


def validate_config(config: Mapping[str, Any]) -> None:
    _require(config.get("schema_version") == SCHEMA_VERSION, "unsupported config schema_version")
    _require(config.get("planning_only") is True, "config must be planning_only")
    _require(isinstance(config.get("plan_id"), str) and bool(config["plan_id"]), "plan_id is required")
    _require(isinstance(config.get("pins"), Mapping), "pins must be an object")
    for location, revision in _walk_revisions(config):
        _require(
            isinstance(revision, str) and REVISION_RE.fullmatch(revision) is not None,
            f"floating or invalid revision at {location}; require a 40-character lowercase commit",
        )

    limits = config.get("execution_limits")
    _require(isinstance(limits, Mapping), "execution_limits must be an object")
    _require(limits.get("concurrency") == 1, "assignment planning requires concurrency=1")
    per_case = limits.get("per_case_deadline_seconds")
    global_deadline = limits.get("global_deadline_seconds")
    _require(isinstance(per_case, int) and per_case > 0, "per-case deadline must be a positive integer")
    _require(isinstance(global_deadline, int) and global_deadline > 0, "global deadline must be a positive integer")
    _require(global_deadline >= per_case, "global deadline must not be shorter than one case deadline")

    step_1 = config.get("step_1")
    _require(isinstance(step_1, Mapping), "step_1 must be an object")
    _require(tuple(step_1.get("suite_order", [])) == REQUIRED_SUITES, "step_1 must declare Lite then Verified")
    suites = step_1.get("suites")
    _require(isinstance(suites, Mapping), "step_1.suites must be an object")
    _require(set(suites) == set(REQUIRED_SUITES), "step_1 must declare exactly Lite and Verified")
    baseline = step_1.get("baseline")
    _require(isinstance(baseline, Mapping), "step_1.baseline must be an object")
    _require(set(baseline) == set(REQUIRED_KNOBS), "baseline must define all four assignment knobs")

    step_2 = config.get("step_2")
    _require(isinstance(step_2, Mapping), "step_2 must be an object")
    _require(step_2.get("shared_baseline") == "reuse_step_1_baseline", "Step 2 must reuse the Step 1 baseline")
    selection = step_2.get("task_selection")
    _require(isinstance(selection, Mapping), "step_2.task_selection must be an object")
    _require(selection.get("algorithm") == "sha256_rank_v1", "unsupported Step 2 task selection algorithm")
    _require(isinstance(selection.get("seed"), str) and bool(selection["seed"]), "Step 2 selection seed is required")
    _require(
        isinstance(selection.get("tasks_per_suite"), int) and selection["tasks_per_suite"] > 0,
        "Step 2 tasks_per_suite must be a positive integer",
    )
    knobs = step_2.get("knobs")
    _require(isinstance(knobs, list), "step_2.knobs must be a list")
    _require(tuple(item.get("name") for item in knobs if isinstance(item, Mapping)) == REQUIRED_KNOBS,
             "Step 2 must declare the four knobs in deterministic order")
    for item in knobs:
        _require(isinstance(item, Mapping), "each Step 2 knob must be an object")
        name = item["name"]
        values = item.get("values")
        _require(isinstance(values, list) and len(values) >= 2, f"{name} must declare at least two values")
        _require(len({canonical_json(value) for value in values}) == len(values), f"{name} contains duplicate values")
        _require(baseline[name] in values, f"{name} values must contain the shared baseline")

    step_3 = config.get("step_3")
    _require(isinstance(step_3, Mapping), "step_3 must be an object")
    _require(step_3.get("emit_execution_rows") is False, "Step 3 may declare selection policy only")
    _require(step_3.get("selection_after") == "completed_and_audited_step_1", "Step 3 must wait for audited Step 1")
    _require(isinstance(step_3.get("selection_count"), int) and step_3["selection_count"] > 0,
             "Step 3 selection_count must be positive")


def load_config(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"cannot read config {path}: {exc}") from exc
    _require(isinstance(value, dict), "config root must be an object")
    validate_config(value)
    return value


def load_task_manifest(path: Path, suite: str) -> tuple[list[dict[str, Any]], str]:
    _require(suite in REQUIRED_SUITES, f"unsupported suite: {suite}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PlanError(f"cannot read {suite} task manifest {path}: {exc}") from exc
    _require(bool(raw), f"{suite} task manifest is empty")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanError(f"{suite} task manifest is not UTF-8: {exc}") from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        _require(bool(line.strip()), f"blank line in {suite} task manifest at line {line_number}")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PlanError(f"invalid JSON in {suite} task manifest at line {line_number}: {exc}") from exc
        _require(isinstance(row, dict), f"{suite} task manifest line {line_number} is not an object")
        instance_id = row.get("instance_id")
        _require(isinstance(instance_id, str) and bool(instance_id.strip()),
                 f"{suite} task manifest line {line_number} lacks instance_id")
        _require(instance_id == instance_id.strip(), f"{suite} instance_id has surrounding whitespace: {instance_id!r}")
        declared_suite = row.get("suite")
        _require(declared_suite in (None, suite),
                 f"{suite} task manifest line {line_number} declares suite={declared_suite!r}")
        for location, revision in _walk_revisions(row, f"{suite}[{line_number}]"):
            _require(
                isinstance(revision, str) and REVISION_RE.fullmatch(revision) is not None,
                f"floating or invalid revision at {location}; require a 40-character lowercase commit",
            )
        _require(instance_id not in seen, f"duplicate {suite} instance_id: {instance_id}")
        seen.add(instance_id)
        repository = row.get("repo") or row.get("repository") or instance_id.split("__", 1)[0]
        _require(isinstance(repository, str) and bool(repository.strip()),
                 f"{suite} task manifest line {line_number} has invalid repository")
        rows.append({
            "instance_id": instance_id,
            "repository": repository,
            "task_sha256": sha256_bytes(canonical_json(row).encode("utf-8")),
        })
    _require(bool(rows), f"missing required suite: {suite}")
    rows.sort(key=lambda row: row["instance_id"])
    return rows, sha256_bytes(raw)


def _selected_step_2_ids(tasks: list[dict[str, Any]], suite: str, selection: Mapping[str, Any]) -> set[str]:
    count = selection["tasks_per_suite"]
    _require(len(tasks) >= count, f"{suite} has {len(tasks)} tasks; Step 2 requires {count}")
    seed = selection["seed"]
    ranked = sorted(
        tasks,
        key=lambda task: (
            sha256_bytes(f"{seed}\0{suite}\0{task['instance_id']}".encode("utf-8")),
            task["instance_id"],
        ),
    )
    return {task["instance_id"] for task in ranked[:count]}


def _resume_key(plan_id: str, suite: str, instance_id: str, cell_id: str) -> str:
    identity = canonical_json([plan_id, suite, instance_id, cell_id]).encode("utf-8")
    return f"assignment-case-v1:{sha256_bytes(identity)}"


def _case_row(
    *,
    config: Mapping[str, Any],
    suite: str,
    task: Mapping[str, Any],
    source_sha256: str,
    cell_id: str,
    steps: list[int],
    roles: list[str],
    settings: Mapping[str, Any],
    variation: Mapping[str, Any] | None,
) -> dict[str, Any]:
    limits = config["execution_limits"]
    plan_id = config["plan_id"]
    return {
        "record_type": "case",
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan_id,
        "steps": steps,
        "roles": roles,
        "suite": suite,
        "instance_id": task["instance_id"],
        "repository": task["repository"],
        "task_sha256": task["task_sha256"],
        "source_manifest_sha256": source_sha256,
        "cell_id": cell_id,
        "settings": dict(settings),
        "variation": dict(variation) if variation is not None else None,
        "concurrency": limits["concurrency"],
        "per_case_deadline_seconds": limits["per_case_deadline_seconds"],
        "resume_key": _resume_key(plan_id, suite, task["instance_id"], cell_id),
    }


def build_plan(
    config: Mapping[str, Any],
    manifests: Mapping[str, tuple[list[dict[str, Any]], str]],
    *,
    config_sha256: str,
) -> list[dict[str, Any]]:
    validate_config(config)
    _require(set(manifests) == set(REQUIRED_SUITES), "both Lite and Verified task manifests are required")
    baseline = dict(config["step_1"]["baseline"])
    selection = config["step_2"]["task_selection"]
    selected = {
        suite: _selected_step_2_ids(manifests[suite][0], suite, selection)
        for suite in REQUIRED_SUITES
    }
    case_rows: list[dict[str, Any]] = []
    for suite in REQUIRED_SUITES:
        tasks, source_sha = manifests[suite]
        for task in tasks:
            shared = task["instance_id"] in selected[suite]
            case_rows.append(_case_row(
                config=config,
                suite=suite,
                task=task,
                source_sha256=source_sha,
                cell_id="shared-baseline",
                steps=[1, 2] if shared else [1],
                roles=["step_1_baseline", "step_2_shared_baseline"] if shared else ["step_1_baseline"],
                settings=baseline,
                variation=None,
            ))
            if not shared:
                continue
            for knob in config["step_2"]["knobs"]:
                name = knob["name"]
                for value in knob["values"]:
                    if value == baseline[name]:
                        continue
                    settings = dict(baseline)
                    settings[name] = value
                    case_rows.append(_case_row(
                        config=config,
                        suite=suite,
                        task=task,
                        source_sha256=source_sha,
                        cell_id=f"{name}={canonical_json(value)}",
                        steps=[2],
                        roles=["step_2_sweep"],
                        settings=settings,
                        variation={"knob": name, "value": value},
                    ))

    resume_keys = [row["resume_key"] for row in case_rows]
    _require(len(resume_keys) == len(set(resume_keys)), "duplicate resume keys generated")
    header = {
        "record_type": "plan",
        "schema_version": SCHEMA_VERSION,
        "plan_id": config["plan_id"],
        "planning_only": True,
        "config_sha256": config_sha256,
        "concurrency": config["execution_limits"]["concurrency"],
        "per_case_deadline_seconds": config["execution_limits"]["per_case_deadline_seconds"],
        "global_deadline_seconds": config["execution_limits"]["global_deadline_seconds"],
        "pins": config["pins"],
        "sources": {
            suite: {
                "dataset": config["step_1"]["suites"][suite]["dataset"],
                "revision": config["step_1"]["suites"][suite]["revision"],
                "manifest_sha256": manifests[suite][1],
                "task_count": len(manifests[suite][0]),
            }
            for suite in REQUIRED_SUITES
        },
        "step_2": {
            "shared_baseline": config["step_2"]["shared_baseline"],
            "task_selection": config["step_2"]["task_selection"],
            "selected_task_ids": {suite: sorted(selected[suite]) for suite in REQUIRED_SUITES},
            "knobs": config["step_2"]["knobs"],
        },
        "step_3_selection_policy": config["step_3"],
        "execution_case_count": len(case_rows),
    }
    return [header, *case_rows]


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


def write_plan(rows: list[dict[str, Any]], output: Path, sidecar: Path, *, force: bool = False) -> str:
    _require(output != sidecar, "output and SHA-256 sidecar paths must differ")
    if not force:
        _require(not output.exists(), f"refusing to overwrite {output}; pass --force")
        _require(not sidecar.exists(), f"refusing to overwrite {sidecar}; pass --force")
    payload = render_jsonl(rows)
    digest = sha256_bytes(payload)
    _atomic_write(output, payload)
    _atomic_write(sidecar, f"{digest}  {output.name}\n".encode("utf-8"))
    return digest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=Path)
    result.add_argument("--lite-tasks", required=True, type=Path, help="external Lite JSONL task manifest")
    result.add_argument("--verified-tasks", required=True, type=Path, help="external Verified JSONL task manifest")
    result.add_argument("--output", required=True, type=Path, help="planned JSONL output")
    result.add_argument("--sha256-sidecar", type=Path, help="default: <output>.sha256")
    result.add_argument("--force", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    sidecar = args.sha256_sidecar or Path(f"{args.output}.sha256")
    try:
        config = load_config(args.config)
        manifests = {
            "lite": load_task_manifest(args.lite_tasks, "lite"),
            "verified": load_task_manifest(args.verified_tasks, "verified"),
        }
        rows = build_plan(config, manifests, config_sha256=file_sha256(args.config))
        digest = write_plan(rows, args.output, sidecar, force=args.force)
        print(f"assignment matrix plan: PASS ({args.output})")
        print(f"execution_cases={len(rows) - 1} concurrency=1 sha256={digest}")
        print(f"sidecar={sidecar}")
        return 0
    except PlanError as exc:
        print(f"assignment matrix plan: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
