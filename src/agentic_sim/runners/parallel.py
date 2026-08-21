"""Deterministic multi-worker planning for paid SWE-agent batches.

This module is deliberately additive.  The reviewed one-instance control
runner remains the authoritative baseline; these helpers only plan disjoint
workers and make the exact per-worker commands reproducible.  A worker owns
one GPU and one vLLM server, so parallel workers must be launched in separate
GPU-isolated hosts/Studios rather than by increasing tensor parallelism.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


class ParallelBatchError(ValueError):
    """A batch plan or worker command violates the parallel-run contract."""


ASSIGNMENT_ALGORITHM = "round_robin_v1"
COMPLETED_STATUSES = frozenset({"completed", "resolved", "unresolved", "empty_patch"})
_SAFE_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+$")


def canonical_json(value: Any) -> bytes:
    """Return the byte representation used for all row hashes."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows_from_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as parquet  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised on the pinned Linux host
        raise ParallelBatchError("Parquet planning requires the pinned pyarrow runtime") from exc
    try:
        rows = parquet.read_table(path).to_pylist()
    except Exception as exc:  # pragma: no cover - depends on remote dataset/runtime
        raise ParallelBatchError(f"unable to read source Parquet {path}: {exc}") from exc
    return [dict(row) for row in rows]


def load_instance_rows(path: str | Path) -> list[dict[str, Any]]:
    """Load a pinned JSON, JSONL, or Parquet dataset without changing it."""

    source = Path(path)
    if not source.is_file():
        raise ParallelBatchError(f"dataset file does not exist: {source}")
    if source.suffix.lower() == ".parquet":
        rows = _rows_from_parquet(source)
    else:
        raw = source.read_text(encoding="utf-8")
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if not isinstance(loaded, list):
            raise ParallelBatchError("dataset must be a JSON list, JSONL file, or Parquet table")
        rows = loaded
    if not rows:
        raise ParallelBatchError(f"dataset is empty: {source}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ParallelBatchError(f"dataset row {index} is not an object")
        instance_id = row.get("instance_id")
        if not isinstance(instance_id, str) or not _SAFE_INSTANCE_ID.fullmatch(instance_id):
            raise ParallelBatchError(f"dataset row {index} has an unsafe or missing instance_id: {instance_id!r}")
        if instance_id in seen:
            raise ParallelBatchError(f"dataset contains duplicate instance_id: {instance_id}")
        seen.add(instance_id)
        normalized.append(dict(row))
    return normalized


def row_hash(row: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json(dict(row)))


@dataclass(frozen=True)
class Shard:
    """One deterministic worker assignment."""

    batch_id: str
    experiment_id: str
    dataset: str
    source_dataset: str
    source_sha256: str
    shard_index: int
    shard_count: int
    instance_ids: tuple[str, ...]
    row_sha256: tuple[str, ...]
    completed_instance_ids: tuple[str, ...] = ()

    @property
    def worker_name(self) -> str:
        return f"worker-{self.shard_index:02d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker": self.worker_name,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "assignment_algorithm": ASSIGNMENT_ALGORITHM,
            "instance_ids": list(self.instance_ids),
            "row_sha256": list(self.row_sha256),
            "completed_instance_ids": list(self.completed_instance_ids),
            "source_dataset": self.source_dataset,
            "source_dataset_sha256": self.source_sha256,
            "batch_id": self.batch_id,
            "experiment_id": self.experiment_id,
            "dataset": self.dataset,
        }


def _validate_shard_args(shard_count: int, shard_index: int) -> None:
    if shard_count <= 0:
        raise ParallelBatchError("shard_count must be positive")
    if shard_index < 0 or shard_index >= shard_count:
        raise ParallelBatchError("shard_index must be within [0, shard_count)")


def plan_shards(
    rows: Sequence[Mapping[str, Any]],
    *,
    batch_id: str,
    experiment_id: str,
    dataset: str,
    source_dataset: str,
    source_sha256: str,
    shard_count: int,
    completed_instance_ids: Iterable[str] = (),
) -> list[Shard]:
    """Assign pending rows round-robin while preserving source order per shard."""

    if not batch_id or "/" in batch_id or "\\" in batch_id:
        raise ParallelBatchError("batch_id must be a safe non-empty path component")
    if not experiment_id or "/" in experiment_id or "\\" in experiment_id:
        raise ParallelBatchError("experiment_id must be a safe non-empty path component")
    _validate_shard_args(shard_count, 0)
    completed = set(completed_instance_ids)
    source_ids = [str(row.get("instance_id", "")) for row in rows]
    if len(source_ids) != len(set(source_ids)):
        raise ParallelBatchError("rows contain duplicate instance IDs")
    unknown = completed.difference(source_ids)
    if unknown:
        raise ParallelBatchError(f"resume set contains IDs absent from source dataset: {sorted(unknown)}")
    pending = [row for row in rows if row.get("instance_id") not in completed]
    buckets: list[list[Mapping[str, Any]]] = [[] for _ in range(shard_count)]
    for pending_index, row in enumerate(pending):
        buckets[pending_index % shard_count].append(row)
    result: list[Shard] = []
    for index, bucket in enumerate(buckets):
        result.append(
            Shard(
                batch_id=batch_id,
                experiment_id=experiment_id,
                dataset=dataset,
                source_dataset=str(source_dataset),
                source_sha256=source_sha256,
                shard_index=index,
                shard_count=shard_count,
                instance_ids=tuple(str(row["instance_id"]) for row in bucket),
                row_sha256=tuple(row_hash(row) for row in bucket),
                completed_instance_ids=tuple(sorted(completed)),
            )
        )
    return result


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def completed_instance_ids(
    work_root: str | Path,
    *,
    experiment_id: str,
    dataset: str,
    statuses: Iterable[str] = COMPLETED_STATUSES,
) -> set[str]:
    """Find only final, successful per-instance attempts under a work root.

    Failed, timed-out, or evaluator-error attempts are intentionally not
    skipped.  They remain available for a later retry with a new attempt ID.
    """

    allowed = set(statuses)
    result: set[str] = set()
    raw_root = Path(work_root) / "data" / "raw" / experiment_id / dataset
    if raw_root.is_dir():
        for summary_path in raw_root.glob("*/*/summary.json"):
            summary = _read_json(summary_path)
            if not isinstance(summary, dict) or summary.get("status") not in allowed:
                continue
            instance_id = summary.get("instance_id")
            if isinstance(instance_id, str) and summary.get("agent_returncode", 0) == 0 and summary.get("evaluator_returncode", 0) in (0, None):
                result.add(instance_id)
    for status_path in Path(work_root).glob("parallel/*/worker-*/worker_status.json"):
        status = _read_json(status_path)
        if not isinstance(status, dict) or status.get("experiment_id") != experiment_id or status.get("status") not in allowed:
            continue
        if status.get("agent_returncode") != 0 or status.get("evaluator_returncode") != 0:
            continue
        result.update(str(item) for item in status.get("instance_ids", []) if isinstance(item, str))
    return result


def write_json(path: str | Path, value: Any) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_bytes(canonical_json(value) + b"\n")
    temporary.replace(destination)


def write_batch_plan(
    plans: Sequence[Shard],
    rows: Sequence[Mapping[str, Any]],
    *,
    output_root: str | Path,
) -> Path:
    """Write portable worker files and a content-addressed batch manifest."""

    if not plans:
        raise ParallelBatchError("at least one shard is required")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    row_by_id = {str(row["instance_id"]): dict(row) for row in rows}
    for plan in plans:
        worker_root = root / plan.worker_name
        worker_rows = [row_by_id[instance_id] for instance_id in plan.instance_ids]
        write_json(worker_root / "instances.json", worker_rows)
        write_json(worker_root / "shard.json", plan.to_dict())
    all_ids = [str(row["instance_id"]) for row in rows]
    pending_ids = [instance_id for plan in plans for instance_id in plan.instance_ids]
    manifest = {
        "schema_version": "parallel-batch.v1",
        "batch_id": plans[0].batch_id,
        "experiment_id": plans[0].experiment_id,
        "dataset": plans[0].dataset,
        "source_dataset": plans[0].source_dataset,
        "source_dataset_sha256": plans[0].source_sha256,
        "row_count": len(rows),
        "all_instance_ids": all_ids,
        "pending_instance_ids": pending_ids,
        "completed_instance_ids": list(plans[0].completed_instance_ids),
        "shard_count": len(plans),
        "assignment_algorithm": ASSIGNMENT_ALGORITHM,
        "workers": [plan.to_dict() for plan in plans],
    }
    manifest_path = root / "batch_manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path


def validate_batch_manifest(value: Mapping[str, Any]) -> None:
    """Reject overlapping, missing, or reordered worker assignments."""

    if value.get("schema_version") != "parallel-batch.v1":
        raise ParallelBatchError("batch manifest schema_version must be parallel-batch.v1")
    if value.get("assignment_algorithm") != ASSIGNMENT_ALGORITHM:
        raise ParallelBatchError("unsupported parallel assignment algorithm")
    workers = value.get("workers")
    all_ids = value.get("all_instance_ids")
    pending_ids = value.get("pending_instance_ids")
    completed_ids = value.get("completed_instance_ids")
    if not isinstance(workers, list) or not workers:
        raise ParallelBatchError("batch manifest must contain at least one worker")
    if not all(isinstance(items, list) and all(isinstance(item, str) for item in items) for items in (all_ids, pending_ids, completed_ids)):
        raise ParallelBatchError("batch manifest ID lists must contain strings")
    if len(set(all_ids)) != len(all_ids) or len(set(completed_ids)) != len(completed_ids):
        raise ParallelBatchError("batch manifest contains duplicate IDs")
    if set(pending_ids).intersection(completed_ids) or set(pending_ids).union(completed_ids) != set(all_ids):
        raise ParallelBatchError("batch manifest pending/completed ID sets do not cover the source dataset")
    observed: list[str] = []
    for index, worker in enumerate(workers):
        if not isinstance(worker, Mapping) or worker.get("shard_index") != index or worker.get("shard_count") != len(workers):
            raise ParallelBatchError("batch manifest worker ordering/count is invalid")
        ids = worker.get("instance_ids")
        hashes = worker.get("row_sha256")
        if not isinstance(ids, list) or not isinstance(hashes, list) or len(ids) != len(hashes):
            raise ParallelBatchError(f"worker-{index:02d} has invalid IDs or row hashes")
        if not all(isinstance(item, str) for item in ids + hashes):
            raise ParallelBatchError(f"worker-{index:02d} contains non-string IDs or hashes")
        observed.extend(ids)
    if len(observed) != len(set(observed)):
        raise ParallelBatchError("worker assignments overlap")
    if observed != pending_ids:
        raise ParallelBatchError("worker assignment order does not match pending_instance_ids")


def _tokens(command: Sequence[str] | str) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    return [str(item) for item in command]


def _replace_flag(tokens: list[str], flag: str, value: str) -> None:
    try:
        index = tokens.index(flag)
    except ValueError as exc:
        raise ParallelBatchError(f"command is missing {flag}") from exc
    if index + 1 >= len(tokens):
        raise ParallelBatchError(f"command has no value after {flag}")
    tokens[index + 1] = value


def _remove_flag(tokens: list[str], flag: str, value_count: int = 1) -> None:
    while flag in tokens:
        index = tokens.index(flag)
        end = index + 1 + value_count
        if end > len(tokens):
            raise ParallelBatchError(f"command has no value after {flag}")
        del tokens[index:end]


def build_worker_agent_command(
    command: Sequence[str] | str,
    *,
    instances_path: str | Path,
    output_dir: str | Path,
    num_workers: int = 1,
) -> list[str]:
    """Rewrite a reviewed command for one disjoint worker shard."""

    if num_workers <= 0:
        raise ParallelBatchError("num_workers must be positive")
    tokens = _tokens(command)
    _replace_flag(tokens, "--instances.path", str(instances_path))
    _remove_flag(tokens, "--instances.filter")
    _replace_flag(tokens, "--output_dir", str(output_dir))
    _replace_flag(tokens, "--num_workers", str(num_workers))
    return tokens


def build_worker_evaluator_command(
    command: Sequence[str] | str,
    *,
    dataset_path: str | Path,
    predictions_path: str | Path,
    instance_ids: Sequence[str],
    report_dir: str | Path,
    run_id: str,
) -> list[str]:
    """Rewrite the official evaluator command for exactly one worker shard."""

    if not instance_ids:
        raise ParallelBatchError("cannot build an evaluator command for an empty shard")
    tokens = _tokens(command)
    dataset_flag = "--dataset_name" if "--dataset_name" in tokens else "--dataset_path"
    _replace_flag(tokens, dataset_flag, str(dataset_path))
    _replace_flag(tokens, "--predictions_path", str(predictions_path))
    _replace_flag(tokens, "--report_dir", str(report_dir))
    _replace_flag(tokens, "--run_id", run_id)
    try:
        start = tokens.index("--instance_ids")
    except ValueError as exc:
        raise ParallelBatchError("evaluator command is missing --instance_ids") from exc
    end = start + 1
    while end < len(tokens) and not tokens[end].startswith("--"):
        end += 1
    tokens[start + 1 : end] = list(instance_ids)
    return tokens


def expand_runtime_environment(tokens: Sequence[str], environment: Mapping[str, str] | None = None) -> list[str]:
    """Expand only the API-key placeholder; never expand arbitrary shell text."""

    env = dict(os.environ if environment is None else environment)
    result: list[str] = []
    for token in tokens:
        if token == "$VLLM_API_KEY" or token == "${VLLM_API_KEY}":
            value = env.get("VLLM_API_KEY")
            if not value:
                raise ParallelBatchError("VLLM_API_KEY is required at execution time and is never stored in a manifest")
            result.append(value)
        else:
            result.append(token)
    return result


def command_hash(tokens: Sequence[str], *, environment: Mapping[str, str] | None = None) -> str:
    """Hash a command while replacing a runtime API key with its placeholder."""

    env = dict(os.environ if environment is None else environment)
    redacted = ["$VLLM_API_KEY" if token and token == env.get("VLLM_API_KEY") else token for token in tokens]
    return sha256_bytes("\0".join(redacted).encode("utf-8"))
