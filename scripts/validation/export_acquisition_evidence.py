#!/usr/bin/env python3
"""Export and validate lossless, source-local acquisition evidence.

The exporter is deliberately a small evidence-preservation tool rather than a
report generator.  It copies the raw request/response bodies that a model
attempt references, decodes the retained BPF binary stream into one row per
operation, retains every v2 journal row, and writes explicit unknown joins for
anything that cannot be joined from saved identity.  It never turns an
aggregate into an individual operation and it never joins two source roots
implicitly.

The output directory is a new immutable-ish evidence bundle.  An existing
directory is rejected so a later run cannot overwrite historical evidence.
The bundle can be revalidated with :func:`validate_export` after the source
directory is unavailable; validation only reads the bundle itself.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.telemetry.bpf_work import iter_bpf_events  # noqa: E402


EXPORT_SCHEMA = "assignment.acquisition-evidence-export.v1"
ROW_SCHEMA = "assignment.acquisition-evidence-row.v1"
ASSERTION_SCHEMA = "assignment.acquisition-evidence-assertions.v1"
MAX_LINE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FILES = 200_000
MAX_SOURCE_BYTES = 8 * 1024 * 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")

TELEMETRY_STREAMS = {
    "lifecycle": "lifecycle_events.jsonl",
    "tool": "tool_events.jsonl",
    "model": "model_events.jsonl",
    "hardware": "hardware_snapshots.jsonl",
}
COMPONENT_JSONL_NAMES = {
    "native-vllm.jsonl",
    "serving-observer.jsonl",
    "request-events.jsonl",
    "events.jsonl",
    "case_result.jsonl",
    "case-result.jsonl",
    "evaluation_result.jsonl",
    "evaluator_result.jsonl",
}
COMPONENT_JSON_NAMES = {
    "capture.json",
    "local_cpu_profile.json",
    "requests-summary.json",
    "serving-config.json",
    "collector_config.json",
    "service_profile.export.json",
    "production_capture_audit.json",
    "action_capture_audit.json",
    "result.json",
    "run_metadata.json",
    "action_plan.json",
    "source_snapshot_manifest.json",
    "artifact_hashes.json",
    "artifact_sha256.json",
    "manifest.json",
    "output_manifest.json",
    "container_mounts.json",
    "case_result.json",
    "case-result.json",
    "evaluation_result.json",
    "evaluator_result.json",
    "evaluation.json",
    "predictions.json",
    "preds.json",
    "report.json",
    "task_result.json",
    "instance.json",
}
LABEL_NAMES = {
    "labels.jsonl",
    "case_labels.jsonl",
    "outcomes.jsonl",
    "evaluations.jsonl",
    "labels.json",
    "case_labels.json",
    "outcomes.json",
}
IDENTITY_FIELDS = ("run_id", "attempt_id", "case_id")
LOSS_FIELDS = (
    "lost_event_records",
    "lost_path_records",
    "lost_pending_records",
    "lineage_map_failures",
)
NON_ATTRIBUTING_PHASES = {
    "outer_swe_agent",
    "generic_wrapper",
    "unknown_residual",
    "e2e_reconciliation",
}

# These are exact reference manifests emitted by the acquisition helpers.  A
# source directory is never treated as an implicit inventory: only files named
# by one of these manifests (or by the bounded named evidence list below) are
# copied.  This keeps case/evaluator/config artifacts from being silently
# dropped while avoiding an unbounded recursive copy of arbitrary logs.
REFERENCE_MANIFEST_NAMES = {
    "artifact_hashes.json",
    "artifact_sha256.json",
    "capture.json",
    "manifest.json",
    "output_manifest.json",
    "acquisition_manifest.json",
    "evidence_manifest.json",
    "case_evidence_manifest.json",
    "runtime_manifest.json",
}


class ExportError(RuntimeError):
    """Evidence cannot be exported or validated without making a claim."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _safe_relative(path: str | Path) -> Path:
    value = Path(path)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise ExportError(f"unsafe relative artifact path: {path!r}")
    return value


def _source_id(root: Path) -> str:
    # The absolute location is provenance, not an identity join.  A source
    # root with the same bytes at another location intentionally gets another
    # id so component artifacts cannot silently collapse together.
    return "source-" + sha256_bytes(str(root.resolve()).encode("utf-8"))[:20]


def _read_json(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read JSON artifact {path}: {exc}") from exc
    return value


def _read_jsonl(path: Path, *, max_line_bytes: int = MAX_LINE_BYTES) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise ExportError(f"cannot open JSONL artifact {path}: {exc}") from exc
    with handle:
        for line_number, raw in enumerate(handle, 1):
            if len(raw) > max_line_bytes:
                raise ExportError(f"JSONL line exceeds bound at {path}:{line_number}")
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ExportError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ExportError(f"JSONL row is not an object at {path}:{line_number}")
            rows.append(value)
    return rows


def _write_bytes_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ExportError(f"refusing to overwrite export artifact: {path}")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write_bytes_new(path, _json_bytes(value))


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ExportError(f"refusing to overwrite export artifact: {path}")
    with path.open("xb") as handle:
        for row in rows:
            encoded = (canonical_json(dict(row)) + "\n").encode("utf-8")
            if len(encoded) > MAX_LINE_BYTES:
                raise ExportError(f"export row exceeds bound: {path}")
            handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _identity_from_mapping(value: Mapping[str, Any] | None) -> dict[str, str | None]:
    value = value or {}
    return {
        key: value.get(key) if isinstance(value.get(key), str) else None
        for key in IDENTITY_FIELDS
    }


def _merge_identity(*values: Mapping[str, Any] | None) -> dict[str, str | None]:
    result = {key: None for key in IDENTITY_FIELDS}
    for value in values:
        if not isinstance(value, Mapping):
            continue
        for key in IDENTITY_FIELDS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                if result[key] is not None and result[key] != candidate:
                    result[key] = "__CONFLICT__"
                elif result[key] is None:
                    result[key] = candidate
    return result


def _identity_complete(identity: Mapping[str, Any]) -> bool:
    return all(isinstance(identity.get(key), str) and identity.get(key) not in {"", "__CONFLICT__"} for key in IDENTITY_FIELDS)


def _path_under(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _file_descriptor(root: Path, path: Path, *, role: str, source_id: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink() or not _path_under(root, path):
        raise ExportError(f"source artifact is not a regular file below source root: {path}")
    stat = path.stat()
    if stat.st_size > MAX_SOURCE_BYTES:
        raise ExportError(f"source artifact exceeds file bound: {path}")
    return {
        "schema_version": ROW_SCHEMA,
        "source_id": source_id,
        "role": role,
        "relative_path": path.resolve().relative_to(root.resolve()).as_posix(),
        "source_path": str(path.resolve()),
        "bytes": stat.st_size,
        "sha256": sha256_file(path),
    }


def _manifest_reference_values(path: Path, value: Any) -> list[str]:
    """Return exact, local artifact references from a known manifest shape.

    The acquisition helpers use a few deliberately small manifest formats.  We
    keep their routing explicit instead of recursively walking arbitrary JSON;
    an evaluator result can therefore contain an unrelated ``path`` field
    without causing the exporter to copy an unexpected tree.
    """

    if not isinstance(value, Mapping):
        return []
    name = path.name
    references: list[str] = []

    if name in {"artifact_hashes.json", "artifact_sha256.json"}:
        # Hash manifests are maps from source-relative path to SHA-256.
        references.extend(key for key in value if isinstance(key, str))
        return references

    if name == "capture.json":
        entries = value.get("artifacts")
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, Mapping) and isinstance(entry.get("path"), str):
                    references.append(entry["path"])
        return references

    if name == "manifest.json":
        # The native serving archive manifest stores extracted members under
        # ``files[*].member``.  Fall back to a relative ``path`` only for the
        # exact simple-manifest shape used by some acquisition helpers.
        entries = value.get("files")
        if isinstance(entries, Mapping):
            for key, entry in entries.items():
                candidate = entry.get("member") if isinstance(entry, Mapping) else None
                if not isinstance(candidate, str):
                    candidate = key if isinstance(key, str) else None
                if isinstance(candidate, str):
                    references.append(candidate)
        return references

    if name == "output_manifest.json":
        entries = value.get("actions")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                for field in ("stdout_path", "stderr_path", "output_path"):
                    if isinstance(entry.get(field), str):
                        references.append(entry[field])
        return references

    # The remaining names are explicit acquisition/evidence manifests.  They
    # may use either ``artifacts`` or ``files`` as a list of path descriptors.
    entries = value.get("artifacts", value.get("files"))
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, str):
                references.append(entry)
            elif isinstance(entry, Mapping):
                for field in ("path", "relative_path", "artifact_path", "file"):
                    if isinstance(entry.get(field), str):
                        references.append(entry[field])
                        break
    elif isinstance(entries, Mapping):
        for key, entry in entries.items():
            candidate = None
            if isinstance(entry, Mapping):
                for field in ("path", "relative_path", "artifact_path", "file", "member"):
                    if isinstance(entry.get(field), str):
                        candidate = entry[field]
                        break
            if candidate is None and isinstance(key, str):
                candidate = key
            if isinstance(candidate, str):
                references.append(candidate)
    return references


def _manifest_referenced_files(root: Path) -> tuple[set[Path], list[dict[str, Any]]]:
    """Resolve exact manifest references and retain missing-reference audits."""

    selected: set[Path] = set()
    audits: list[dict[str, Any]] = []
    for manifest in sorted(root.rglob("*")):
        if (
            not manifest.is_file()
            or manifest.is_symlink()
            or manifest.name not in REFERENCE_MANIFEST_NAMES
        ):
            continue
        value = _read_json(manifest)
        for reference in _manifest_reference_values(manifest, value):
            try:
                relative = _safe_relative(reference)
            except ExportError as exc:
                audits.append({
                    "manifest": manifest.relative_to(root).as_posix(),
                    "relative_path": reference,
                    "status": "fail",
                    "reason": str(exc),
                })
                continue
            # output_manifest.json is written inside ``actions/`` but its
            # paths are relative to the instrument root.  Keep that one
            # explicit format exception local rather than searching arbitrary
            # ancestors for similarly named files.
            bases = [manifest.parent]
            if manifest.name == "output_manifest.json" and manifest.parent != root:
                bases.insert(0, manifest.parent.parent)
            candidates = [(base / relative).resolve() for base in bases]
            candidate = next(
                (
                    path
                    for path in candidates
                    if _path_under(root, path) and path.is_file() and not path.is_symlink()
                ),
                candidates[0],
            )
            if not _path_under(root, candidate) or not candidate.is_file() or candidate.is_symlink():
                audits.append({
                    "manifest": manifest.relative_to(root).as_posix(),
                    "relative_path": reference,
                    "status": "fail",
                    "reason": "manifest-referenced artifact missing or outside source root",
                })
                continue
            selected.add(candidate)
            audits.append({
                "manifest": manifest.relative_to(root).as_posix(),
                "relative_path": reference,
                "resolved_path": candidate.relative_to(root).as_posix(),
                "status": "pass",
            })
    return selected, audits


def _candidate_files(root: Path) -> list[Path]:
    # Do not recursively copy a source tree.  Only named evidence artifacts
    # can affect this export; unlisted files remain source-local provenance.
    selected: set[Path] = set()
    direct = {
        "telemetry_manifest.json",
        "bpf_collector_manifest.json",
        "work_summary.json",
        "raw_aggregates.jsonl",
        "raw_events.bin",
        "action_boundaries.jsonl",
        "runtime_actions.jsonl",
        "artifact_hashes.json",
        "result.json",
        "run_metadata.json",
        "action_plan.json",
        "persistent_shell_witness.json",
        "native_operations.json",
        "service_lifecycle.json",
        "source_snapshot_manifest.json",
        "invocation.json",
        "profile_report.json",
        "container_mounts.json",
    }
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        rel_parts = path.relative_to(root).parts
        if name in REFERENCE_MANIFEST_NAMES:
            selected.add(path)
        if name in direct or name in COMPONENT_JSONL_NAMES or name in COMPONENT_JSON_NAMES or name in LABEL_NAMES:
            selected.add(path)
        if "telemetry" in rel_parts and (
            name in set(TELEMETRY_STREAMS.values())
            or name == "telemetry_manifest.json"
            or name in {"artifact_hashes.json", "collector_config.json"}
            or "request_payloads" in rel_parts
            or "script_artifacts" in rel_parts
            or "serving_metrics" in rel_parts
        ):
            selected.add(path)
        if "linux_work" in rel_parts and name in {
            "raw_events.bin",
            "raw_aggregates.jsonl",
            "action_boundaries.jsonl",
            "bpf_collector_manifest.json",
            "work_summary.json",
            "native_bpf_sink.c",
        }:
            selected.add(path)
    # Follow exact references in capture/hash/archive/evidence manifests.  A
    # missing reference is retained as an audit row by _load_source rather than
    # being silently omitted from the export.
    referenced, _ = _manifest_referenced_files(root)
    selected.update(referenced)
    if len(selected) > MAX_SOURCE_FILES:
        raise ExportError(f"source evidence inventory exceeds {MAX_SOURCE_FILES} files: {root}")
    return sorted(selected)


def _declared_hash_audit(root: Path, path: Path) -> list[dict[str, Any]]:
    if path.name not in {"artifact_hashes.json", "artifact_sha256.json"}:
        return []
    value = _read_json(path)
    if not isinstance(value, Mapping):
        return [{"status": "unknown", "reason": "artifact_hashes.json is not an object"}]
    rows: list[dict[str, Any]] = []
    for relative, expected in value.items():
        if not isinstance(relative, str) or not isinstance(expected, str) or not HEX64.fullmatch(expected):
            rows.append({"relative_path": relative, "status": "fail", "reason": "invalid declared hash"})
            continue
        try:
            target = root / _safe_relative(relative)
        except ExportError as exc:
            rows.append({"relative_path": relative, "status": "fail", "reason": str(exc)})
            continue
        if not target.is_file() or target.is_symlink():
            rows.append({"relative_path": relative, "status": "fail", "reason": "declared artifact missing"})
            continue
        actual = sha256_file(target)
        rows.append({"relative_path": relative, "expected_sha256": expected, "actual_sha256": actual, "status": "pass" if actual == expected else "fail"})
    return rows


def _capture_hash_audit(root: Path, path: Path) -> list[dict[str, Any]]:
    """Check capture.json's exact artifact list against saved source bytes."""

    if path.name != "capture.json":
        return []
    value = _read_json(path)
    if not isinstance(value, Mapping) or not isinstance(value.get("artifacts"), list):
        return [{"manifest": path.relative_to(root).as_posix(), "status": "unknown", "reason": "capture.json has no artifact list"}]
    rows: list[dict[str, Any]] = []
    for entry in value["artifacts"]:
        if not isinstance(entry, Mapping):
            rows.append({"manifest": path.relative_to(root).as_posix(), "status": "fail", "reason": "capture artifact entry is not an object"})
            continue
        relative = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str) or not HEX64.fullmatch(expected):
            rows.append({"manifest": path.relative_to(root).as_posix(), "relative_path": relative, "status": "fail", "reason": "capture artifact path/hash is invalid"})
            continue
        try:
            target = (path.parent / _safe_relative(relative)).resolve()
        except ExportError as exc:
            rows.append({"manifest": path.relative_to(root).as_posix(), "relative_path": relative, "status": "fail", "reason": str(exc)})
            continue
        if not _path_under(root, target) or not target.is_file() or target.is_symlink():
            rows.append({"manifest": path.relative_to(root).as_posix(), "relative_path": relative, "status": "fail", "reason": "capture-declared artifact missing or outside source root"})
            continue
        actual = sha256_file(target)
        rows.append({
            "manifest": path.relative_to(root).as_posix(),
            "relative_path": relative,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "status": "pass" if actual == expected else "fail",
        })
    return rows


def _manifest_identity(root: Path, telemetry_rows: Sequence[Mapping[str, Any]], bpf_rows: Sequence[Mapping[str, Any]]) -> dict[str, str | None]:
    candidates: list[Mapping[str, Any]] = []
    for name in ("telemetry_manifest.json",):
        for path in [root / name, *root.glob(f"**/{name}")]:
            if path.is_file():
                value = _read_json(path)
                if isinstance(value, Mapping):
                    candidates.append(value)
    for row in telemetry_rows:
        candidates.append(row)
    for row in bpf_rows:
        if isinstance(row.get("identity"), Mapping):
            candidates.append(row["identity"])
        if isinstance(row.get("boundary"), Mapping):
            candidates.append(row["boundary"])
    identity = _merge_identity(*candidates)
    return identity


def _row_wrapper(
    source: Mapping[str, Any],
    *,
    stream: str,
    row_index: int,
    row: Mapping[str, Any],
    source_file: Mapping[str, Any],
) -> dict[str, Any]:
    identity = _merge_identity(source.get("identity"), row)
    return {
        "schema_version": ROW_SCHEMA,
        "source_id": source["source_id"],
        "source_class": source["source_class"],
        "stream": stream,
        "source_row_index": row_index,
        "source_file": source_file["relative_path"],
        "source_file_sha256": source_file["sha256"],
        "row_sha256": sha256_bytes(canonical_json(dict(row)).encode("utf-8")),
        "run_id": identity.get("run_id"),
        "attempt_id": identity.get("attempt_id"),
        "case_id": identity.get("case_id"),
        "provenance": row.get("provenance", "unknown"),
        "availability": row.get("availability", "unknown"),
        "record": dict(row),
    }


def _stream_paths(root: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for stream, name in TELEMETRY_STREAMS.items():
        matches = sorted(root.rglob(name))
        if matches:
            # A source root with two telemetry directories would be ambiguous.
            if len(matches) > 1:
                raise ExportError(f"multiple {name} artifacts under source root {root}")
            paths[stream] = matches[0]
    return paths


def _bpf_metadata(root: Path) -> Mapping[str, Any] | None:
    """Load the authoritative collector ABI, with summary fallback."""

    manifests = sorted(root.rglob("bpf_collector_manifest.json"))
    if len(manifests) == 1:
        value = _read_json(manifests[0])
        return value if isinstance(value, Mapping) else None

    # A closed collector always writes work_summary.json beside the raw
    # streams.  Preserve its raw-event schema/record size when the standalone
    # collector manifest was not copied into an older component bundle.
    summaries = sorted(root.rglob("work_summary.json"))
    for summary_path in summaries:
        value = _read_json(summary_path)
        if not isinstance(value, Mapping):
            continue
        stream = value.get("raw_event_stream")
        if not isinstance(stream, Mapping):
            continue
        if not isinstance(stream.get("record_size_bytes"), int):
            continue
        return {
            "schema_version": value.get("collector_schema", value.get("schema_version")),
            "identity": value.get("identity"),
            "program_sha256": value.get("program_sha256"),
            "raw_event_stream": dict(stream),
            "summary_source": summary_path.relative_to(root).as_posix(),
        }

    # Some diagnostic captures retain only production_capture_audit.json.  It
    # carries the native sink record size but may predate the explicit event
    # schema; the decoder spec can infer the schema from this size if needed.
    audits = sorted(root.rglob("production_capture_audit.json"))
    for audit_path in audits:
        value = _read_json(audit_path)
        if not isinstance(value, Mapping) or not isinstance(value.get("bpf"), Mapping):
            continue
        bpf = value["bpf"]
        native = bpf.get("native_sink")
        if not isinstance(native, Mapping) or not isinstance(native.get("record_size_bytes"), int):
            continue
        stream: dict[str, Any] = {
            "record_size_bytes": native["record_size_bytes"],
            "sha256": bpf.get("raw_stream_sha256"),
        }
        return {
            "schema_version": value.get("schema_version"),
            "identity": bpf.get("target_identity"),
            "program_sha256": bpf.get("program_sha256"),
            "raw_event_stream": stream,
            "summary_source": audit_path.relative_to(root).as_posix(),
        }
    return None


def _load_source(root_value: str | Path, source_index: int) -> dict[str, Any]:
    root = Path(root_value).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise ExportError(f"source root is not a real directory: {root}")
    source_id = _source_id(root)
    stream_paths = _stream_paths(root)
    stream_rows: dict[str, list[dict[str, Any]]] = {}
    wrappers: list[dict[str, Any]] = []
    source_files: list[dict[str, Any]] = []
    selected_paths = _candidate_files(root)
    total_bytes = 0
    descriptors_by_path: dict[Path, dict[str, Any]] = {}
    for path in selected_paths:
        descriptor = _file_descriptor(root, path, role="selected_source_artifact", source_id=source_id)
        total_bytes += int(descriptor["bytes"])
        if total_bytes > MAX_SOURCE_BYTES:
            raise ExportError(f"source inventory exceeds byte bound: {root}")
        descriptors_by_path[path] = descriptor
        source_files.append(descriptor)
    for stream, path in stream_paths.items():
        rows = _read_jsonl(path)
        stream_rows[stream] = rows
        source_file = descriptors_by_path.get(path)
        if source_file is None:
            source_file = _file_descriptor(root, path, role="telemetry_stream", source_id=source_id)
            source_files.append(source_file)
        for row_index, row in enumerate(rows):
            wrappers.append(_row_wrapper({"source_id": source_id, "source_class": "unknown", "identity": {}}, stream=stream, row_index=row_index, row=row, source_file=source_file))
    bpf_rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("raw_aggregates.jsonl")):
        if path.is_symlink():
            continue
        source_file = descriptors_by_path.get(path)
        if source_file is None:
            source_file = _file_descriptor(root, path, role="bpf_aggregate_journal", source_id=source_id)
            source_files.append(source_file)
        for row_index, row in enumerate(_read_jsonl(path)):
            row_copy = dict(row)
            row_copy["_source_file"] = source_file
            row_copy["_source_row_index"] = row_index
            bpf_rows.append(row_copy)
    identity = _manifest_identity(root, [row for rows in stream_rows.values() for row in rows], bpf_rows)
    # Re-wrap telemetry rows now that source-level identity is known.
    for wrapper in wrappers:
        wrapper["source_class"] = "unknown"
        for key in IDENTITY_FIELDS:
            if wrapper.get(key) is None:
                wrapper[key] = identity.get(key)
    has_v2 = bool(stream_rows) and any(
        str(row.get("schema_version", "")).startswith("assignment.telemetry.v2")
        for rows in stream_rows.values()
        for row in rows
    )
    has_bpf = bool(bpf_rows)
    has_model = bool(stream_rows.get("model"))
    has_lifecycle = bool(stream_rows.get("lifecycle"))
    if has_v2 and has_bpf and has_model and has_lifecycle:
        source_class = "v2_join_candidate"
    elif has_v2 and (has_model or has_lifecycle or has_bpf):
        source_class = "v2_component_evidence"
    elif has_bpf or stream_rows:
        source_class = "legacy_component_evidence"
    else:
        source_class = "metadata_only"
    for wrapper in wrappers:
        wrapper["source_class"] = source_class
    declared_audits: list[dict[str, Any]] = []
    _, manifest_reference_audits = _manifest_referenced_files(root)
    declared_audits.extend(manifest_reference_audits)
    for path in selected_paths:
        declared_audits.extend(_declared_hash_audit(root, path))
        declared_audits.extend(_capture_hash_audit(root, path))
    manifest_paths = sorted(root.rglob("telemetry_manifest.json"))
    telemetry_manifest: Mapping[str, Any] | None = None
    if len(manifest_paths) == 1:
        value = _read_json(manifest_paths[0])
        if isinstance(value, Mapping):
            telemetry_manifest = value
    bpf_manifest = _bpf_metadata(root)
    return {
        "source_id": source_id,
        "source_index": source_index,
        "root": root,
        "root_path": str(root),
        "identity": identity,
        "source_class": source_class,
        "telemetry_manifest": telemetry_manifest,
        "bpf_manifest": bpf_manifest if isinstance(bpf_manifest, Mapping) else None,
        "stream_paths": stream_paths,
        "stream_rows": stream_rows,
        "telemetry_wrappers": wrappers,
        "bpf_rows": bpf_rows,
        "source_files": source_files,
        "declared_hash_audits": declared_audits,
    }


def _source_file_for(source: Mapping[str, Any], path: Path, role: str) -> dict[str, Any]:
    for descriptor in source["source_files"]:
        if descriptor.get("relative_path") == path.resolve().relative_to(source["root"].resolve()).as_posix():
            return descriptor
    descriptor = _file_descriptor(source["root"], path, role=role, source_id=source["source_id"])
    source["source_files"].append(descriptor)
    return descriptor


def _event_args(row: Mapping[str, Any]) -> tuple[Any, str]:
    # v3 packets retain six raw syscall scalar words plus the decoder's
    # syscall-specific projection.  Keep both so a later ABI revision can be
    # reinterpreted from the saved raw values without rerunning the case.
    raw_scalar_args = row.get("raw_scalar_args")
    if isinstance(raw_scalar_args, list):
        return {
            "raw_scalar_args": list(raw_scalar_args),
            "scalar_args": row.get("scalar_args"),
        }, "measured_raw_scalar_args"
    for key in ("syscall_args", "arguments", "args"):
        if key in row:
            return row.get(key), "measured" if row.get(key) is not None else "unavailable"
    fields = {
        key: row[key]
        for key in row
        if key.startswith("arg") or key.startswith("syscall_arg")
    }
    if fields:
        return fields, "measured"
    return None, "unavailable_not_in_saved_record"


def _bpf_event_rows(source: dict[str, Any]) -> tuple[list[dict[str, Any]], Iterator[dict[str, Any]], list[dict[str, Any]], int]:
    """Return action rows, operation rows, and explicit join rows for one source."""
    aggregate_rows = source["bpf_rows"]
    root: Path = source["root"]
    binaries = sorted(root.rglob("raw_events.bin"))
    if not aggregate_rows and not binaries:
        return [], iter(()), [], 0
    joins: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    if not binaries:
        if not aggregate_rows:
            # There is no individual stream to recover and no aggregate
            # journal to preserve; the source simply has no BPF evidence.
            return [], iter(()), [], 0
        for index, raw in enumerate(aggregate_rows):
            token = raw.get("action_token")
            action_rows.append({
                "schema_version": ROW_SCHEMA,
                "source_id": source["source_id"],
                "source_class": source["source_class"],
                "raw_aggregate_row_index": raw.get("_source_row_index", index),
                "record_type": raw.get("record_type", "action"),
                "action_token": token,
                "run_id": source["identity"].get("run_id"),
                "attempt_id": source["identity"].get("attempt_id"),
                "case_id": source["identity"].get("case_id"),
                "aggregate_only": True,
                "operation_evidence_status": "unavailable_no_binary_stream",
                "raw_aggregate": {k: v for k, v in raw.items() if not k.startswith("_")},
            })
        joins.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "join_kind": "bpf_operation_stream",
            "status": "unknown",
            "reason": "raw_aggregates.jsonl exists but raw_events.bin is absent; aggregate-only fallback is forbidden",
        })
        return action_rows, iter(()), joins, 0
    if len(binaries) > 1:
        raise ExportError(f"multiple raw_events.bin streams under source root {root}")
    binary = binaries[0]
    descriptor = _source_file_for(source, binary, "bpf_binary_event_stream")
    if not aggregate_rows:
        joins.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "join_kind": "bpf_operation_stream",
            "status": "unknown",
            "reason": "raw_events.bin exists but raw_aggregates.jsonl has no rows; operations are retained with unknown action joins",
            "raw_event_stream_source_file": descriptor["relative_path"],
            "raw_event_stream_sha256": descriptor["sha256"],
        })
    record_size = None
    schema_version = None
    manifest = source.get("bpf_manifest")
    if isinstance(manifest, Mapping):
        stream_meta = manifest.get("raw_event_stream")
        if isinstance(stream_meta, Mapping):
            if isinstance(stream_meta.get("record_size_bytes"), int):
                record_size = int(stream_meta["record_size_bytes"])
            if isinstance(stream_meta.get("schema_version"), str):
                schema_version = stream_meta["schema_version"]
    if record_size is None:
        # A manifest is the authoritative ABI binding.  For older component
        # artifacts without one, permit inference only when exactly one of the
        # retained layouts divides the complete stream; ambiguous bytes are a
        # hard error rather than an ABI guess.
        from agentic_sim.telemetry.bpf_work import (  # type: ignore
            BPF_EVENT_RECORD_SIZE,
            BPF_EVENT_RECORD_SIZE_LEGACY,
            BPF_EVENT_SCHEMA,
            BPF_EVENT_SCHEMA_LEGACY,
        )

        size = binary.stat().st_size
        schema_sizes = {
            BPF_EVENT_SCHEMA: BPF_EVENT_RECORD_SIZE,
            BPF_EVENT_SCHEMA_LEGACY: BPF_EVENT_RECORD_SIZE_LEGACY,
        }
        if schema_version is not None:
            record_size = schema_sizes.get(schema_version)
            if record_size is None:
                raise ExportError(f"unsupported BPF schema in manifest: {schema_version}")
        else:
            candidates = [
                (BPF_EVENT_RECORD_SIZE, BPF_EVENT_SCHEMA),
                (BPF_EVENT_RECORD_SIZE_LEGACY, BPF_EVENT_SCHEMA_LEGACY),
            ]
            candidates = [item for item in candidates if size % item[0] == 0]
            if len(candidates) != 1:
                raise ExportError(
                    f"BPF binary layout is missing or ambiguous for {binary}; "
                    "record_size_bytes/schema_version must be retained in the manifest"
                )
            record_size, schema_version = candidates[0]
    elif schema_version is None:
        from agentic_sim.telemetry.bpf_work import (  # type: ignore
            BPF_EVENT_RECORD_SIZE,
            BPF_EVENT_RECORD_SIZE_LEGACY,
            BPF_EVENT_SCHEMA,
            BPF_EVENT_SCHEMA_LEGACY,
        )

        if record_size == BPF_EVENT_RECORD_SIZE:
            schema_version = BPF_EVENT_SCHEMA
        elif record_size == BPF_EVENT_RECORD_SIZE_LEGACY:
            schema_version = BPF_EVENT_SCHEMA_LEGACY
        else:
            raise ExportError(f"unsupported BPF record size in manifest: {record_size}")
    size = binary.stat().st_size
    if size % record_size:
        raise ExportError(f"BPF binary stream is not record aligned: {binary}")
    binary_hash = descriptor["sha256"]
    declared_stream_hash = None
    if isinstance(manifest, Mapping):
        stream_meta = manifest.get("raw_event_stream")
        if isinstance(manifest.get("raw_event_stream_sha256"), str):
            declared_stream_hash = manifest["raw_event_stream_sha256"]
        elif isinstance(stream_meta, Mapping) and isinstance(stream_meta.get("sha256"), str):
            declared_stream_hash = stream_meta["sha256"]
    if isinstance(declared_stream_hash, str) and HEX64.fullmatch(declared_stream_hash) and declared_stream_hash != binary_hash:
        raise ExportError(f"BPF raw event stream hash disagrees with saved binary: {binary}")
    latest_by_token: dict[int, tuple[int, Mapping[str, Any]]] = {}
    rows_by_token: dict[int, list[tuple[int, Mapping[str, Any]]]] = defaultdict(list)
    for index, raw in enumerate(aggregate_rows):
        token = raw.get("action_token")
        if isinstance(token, bool) or not isinstance(token, int):
            joins.append({
                "schema_version": ROW_SCHEMA,
                "source_id": source["source_id"],
                "join_kind": "bpf_action_identity",
                "raw_aggregate_row_index": raw.get("_source_row_index", index),
                "status": "unknown",
                "reason": "action_token is absent or non-integer",
            })
            continue
        token_value = int(token)
        rows_by_token[token_value].append((raw.get("_source_row_index", index), raw))
        current = latest_by_token.get(token_value)
        if current is None or raw.get("record_type") == "action_finalization":
            latest_by_token[token_value] = (raw.get("_source_row_index", index), raw)
    by_event_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source["stream_rows"].get("tool", []):
        event_id = row.get("event_id")
        if isinstance(event_id, str):
            by_event_id[event_id].append(row)
    decoded_token_counts: Counter[int] = Counter()
    decoded_total = 0
    try:
        for row in iter_bpf_events(
            binary,
            schema_version=schema_version,
            record_size_bytes=record_size,
        ):
            token = row.get("token")
            if isinstance(token, int) and not isinstance(token, bool):
                decoded_token_counts[int(token)] += 1
                decoded_total += 1
    except (OSError, ValueError, TypeError) as exc:
        raise ExportError(f"cannot decode individual BPF records from {binary}: {exc}") from exc
    # A non-deferred collector may retain decoded inline event rows in the
    # aggregate journal.  Use those rows only to fill ABI fields absent from
    # the binary decoder (not to create operations); the binary packet remains
    # the authoritative operation and timing source.
    inline_by_token_sequence: dict[tuple[int, int], Mapping[str, Any]] = {}
    for raw in aggregate_rows:
        token = raw.get("action_token")
        inline = raw.get("events")
        if not isinstance(token, int) or isinstance(token, bool) or not isinstance(inline, list):
            continue
        for item in inline:
            if isinstance(item, Mapping) and isinstance(item.get("sequence"), int):
                inline_by_token_sequence[(int(token), int(item["sequence"]))] = item
    for token, (latest_index, raw) in sorted(latest_by_token.items()):
        boundary = raw.get("boundary") if isinstance(raw.get("boundary"), Mapping) else {}
        event_id = boundary.get("event_id") if isinstance(boundary, Mapping) else None
        matched = by_event_id.get(event_id, []) if isinstance(event_id, str) else []
        join_status = "matched_tool_boundary_start" if matched else "unknown_no_tool_boundary"
        latest_stream = raw.get("binary_event_stream") if isinstance(raw.get("binary_event_stream"), Mapping) else {}
        expected = raw.get("required_event_count")
        token_count = int(decoded_token_counts.get(token, 0))
        range_start = latest_stream.get("offset_start", 0)
        range_end = latest_stream.get("offset_end", size)
        if not isinstance(range_start, int) or not isinstance(range_end, int) or not (0 <= range_start <= range_end <= size):
            range_status = "unknown_invalid_range"
        else:
            range_status = "range_valid"
        complete = raw.get("event_records_complete") is True and raw.get("aggregate_missing") is False
        losses = raw.get("raw_aggregate") if isinstance(raw.get("raw_aggregate"), Mapping) else {}
        loss_values = {field: losses.get(field, 0) for field in LOSS_FIELDS}
        operation_status = "complete" if complete and token_count == int(expected or token_count) and all(value in {0, None} for value in loss_values.values()) else "incomplete_or_censored"
        action_rows.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "source_class": source["source_class"],
            "raw_aggregate_row_index": latest_index,
            "record_type": raw.get("record_type", "action"),
            "action_token": token,
            "event_id": event_id,
            "command_sha256": raw.get("command_sha256"),
            "run_id": (raw.get("identity") or {}).get("run_id", source["identity"].get("run_id")) if isinstance(raw.get("identity"), Mapping) else source["identity"].get("run_id"),
            "attempt_id": (raw.get("identity") or {}).get("attempt_id", source["identity"].get("attempt_id")) if isinstance(raw.get("identity"), Mapping) else source["identity"].get("attempt_id"),
            "case_id": (raw.get("identity") or {}).get("case_id", source["identity"].get("case_id")) if isinstance(raw.get("identity"), Mapping) else source["identity"].get("case_id"),
            "status": boundary.get("status") if isinstance(boundary, Mapping) else "unknown",
            "start_mono_ns": boundary.get("start_mono_ns") if isinstance(boundary, Mapping) else None,
            "end_mono_ns": boundary.get("end_mono_ns") if isinstance(boundary, Mapping) else None,
            "required_event_count": expected,
            "decoded_token_record_count": token_count,
            "range_record_count": max(0, (range_end - range_start) // record_size) if range_status == "range_valid" else None,
            "range_byte_start": range_start,
            "range_byte_end": range_end,
            "event_records_complete": raw.get("event_records_complete"),
            "operation_evidence_status": operation_status,
            "aggregate_only": False,
            "raw_aggregate": {k: v for k, v in raw.items() if not k.startswith("_")},
            "raw_aggregate_sha256": sha256_bytes(canonical_json({k: v for k, v in raw.items() if not k.startswith("_")}).encode("utf-8")),
            "raw_aggregate_source_file": raw.get("_source_file", {}).get("relative_path") if isinstance(raw.get("_source_file"), Mapping) else None,
            "aggregate_row_indices_for_action_token": [index for index, _ in rows_by_token.get(token, [])],
            "aggregate_row_count_for_action_token": len(rows_by_token.get(token, [])),
            "raw_event_stream_source_file": descriptor["relative_path"],
            "raw_event_stream_sha256": binary_hash,
            "tool_join_status": join_status,
            "tool_join_event_ids": [row.get("event_id") for row in matched],
            "raw_event_schema_version": schema_version,
            "raw_event_record_size_bytes": record_size,
        })
        if not matched:
            joins.append({
                "schema_version": ROW_SCHEMA,
                "source_id": source["source_id"],
                "join_kind": "bpf_to_tool",
                "action_token": token,
                "event_id": event_id,
                "status": "unknown",
                "reason": "BPF boundary event_id has no saved telemetry tool row; no hash/time fallback was attempted",
            })
    def operations() -> Iterator[dict[str, Any]]:
        per_token_index: Counter[int] = Counter()
        with binary.open("rb") as handle:
            for global_index, row in enumerate(
                iter_bpf_events(
                    binary,
                    schema_version=schema_version,
                    record_size_bytes=record_size,
                )
            ):
                token = row.get("token")
                action = latest_by_token.get(int(token)) if isinstance(token, int) and not isinstance(token, bool) else None
                action_raw = action[1] if action is not None else None
                identity = _merge_identity(
                    source["identity"],
                    action_raw.get("identity") if isinstance(action_raw, Mapping) else None,
                )
                action_event_id = None
                if isinstance(action_raw, Mapping) and isinstance(action_raw.get("boundary"), Mapping):
                    action_event_id = action_raw["boundary"].get("event_id")
                action_join_status = "matched_action" if action is not None else "unknown_action_token"
                action_index = per_token_index[int(token)] if isinstance(token, int) and not isinstance(token, bool) else None
                if isinstance(token, int) and not isinstance(token, bool):
                    per_token_index[int(token)] += 1
                args, args_provenance = _event_args(row)
                if args_provenance == "unavailable_not_in_saved_record" and isinstance(token, int) and not isinstance(token, bool):
                    inline = inline_by_token_sequence.get((int(token), int(row.get("sequence", -1))))
                    if inline is not None:
                        args, args_provenance = _event_args(inline)
                        if args_provenance == "measured":
                            args_provenance = "measured_inline_event"
                raw_start = global_index * record_size
                raw_end = raw_start + record_size
                handle.seek(raw_start)
                packet = handle.read(record_size)
                if len(packet) != record_size:
                    raise ExportError(f"BPF packet disappeared during export: {binary}:{global_index}")
                yield {
                    "schema_version": ROW_SCHEMA,
                    "source_id": source["source_id"],
                    "source_class": source["source_class"],
                    "run_id": identity.get("run_id"),
                    "attempt_id": identity.get("attempt_id"),
                    "case_id": identity.get("case_id"),
                    "action_token": token,
                    "action_event_id": action_event_id,
                    "record_index": global_index,
                    "action_record_index": action_index,
                    "raw_event_offset_start": raw_start,
                    "raw_event_offset_end": raw_end,
                    "raw_event_packet_sha256": sha256_bytes(packet),
                    "raw_event_stream_sha256": binary_hash,
                    "raw_event_stream_source_file": descriptor["relative_path"],
                    "action_join_status": action_join_status,
                    "syscall_args": args,
                    "syscall_args_provenance": args_provenance,
                    "status": row.get("status"),
                    "status_name": row.get("status_name"),
                    "censored": row.get("censor_boundary_ns") is not None,
                    "kernel_start_ns": row.get("kernel_start_ns"),
                    "kernel_end_ns": row.get("kernel_end_ns"),
                    "duration_ns": row.get("duration_ns"),
                    "censor_boundary_ns": row.get("censor_boundary_ns"),
                    "timing_provenance": "measured_kernel_event",
                    "raw_event_schema_version": row.get("schema_version"),
                    "event_abi": row.get("event_abi"),
                    # The packet itself is measured even when its action
                    # boundary cannot be joined; keep that uncertainty in
                    # action_join_status rather than relabeling raw evidence.
                    "provenance": "measured",
                    "record": dict(row),
                }
    for token, action_rows_for_token in rows_by_token.items():
        if token not in decoded_token_counts:
            joins.append({
                "schema_version": ROW_SCHEMA,
                "source_id": source["source_id"],
                "join_kind": "bpf_action_to_binary",
                "action_token": token,
                "status": "unknown",
                "reason": "aggregate action token has no decoded binary operation records",
                "raw_aggregate_row_indices": [index for index, _ in action_rows_for_token],
            })
    return action_rows, operations(), joins, decoded_total


def _copy_payloads(source: dict[str, Any], output: Path, model_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload_rows: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []
    for row_index, row in enumerate(model_rows):
        if row.get("terminal") is not True or row.get("event_kind") != "model_request":
            continue
        request_id = row.get("physical_request_id") or row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "model_request_payload", "source_row_index": row_index, "status": "unknown", "reason": "terminal model row lacks physical request identity"})
            continue
        payload_meta = row.get("request_payload_artifact")
        payload_meta = payload_meta if isinstance(payload_meta, Mapping) else {}
        source_telemetry = source["stream_paths"].get("model")
        telemetry_root = source_telemetry.parent if source_telemetry is not None else source["root"]
        for kind in ("request", "response"):
            item = payload_meta.get(kind)
            if not isinstance(item, Mapping):
                payload_rows.append({
                    "schema_version": ROW_SCHEMA,
                    "source_id": source["source_id"],
                    "run_id": source["identity"].get("run_id"),
                    "attempt_id": source["identity"].get("attempt_id"),
                    "case_id": source["identity"].get("case_id"),
                    "physical_request_id": request_id,
                    "source_row_index": row_index,
                    "payload_kind": kind,
                    "status": "unknown",
                    "reason": "terminal model row has no payload metadata",
                    "provenance": "unavailable",
                })
                continue
            relative_text = item.get("artifact_path")
            if not isinstance(relative_text, str):
                payload_rows.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "physical_request_id": request_id, "source_row_index": row_index, "payload_kind": kind, "status": "unknown", "reason": "payload metadata has no artifact_path", "provenance": "unavailable"})
                continue
            try:
                relative = _safe_relative(relative_text)
            except ExportError as exc:
                raise ExportError(f"unsafe model payload reference in {source['root']}: {exc}") from exc
            candidates = [telemetry_root / relative, source["root"] / relative]
            payload_path = next((candidate for candidate in candidates if candidate.is_file() and not candidate.is_symlink() and _path_under(source["root"], candidate)), None)
            if payload_path is None:
                payload_rows.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "physical_request_id": request_id, "source_row_index": row_index, "payload_kind": kind, "status": "unknown", "reason": "declared payload artifact is missing", "declared_sha256": item.get("sha256"), "provenance": "unavailable"})
                joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "model_request_payload", "physical_request_id": request_id, "payload_kind": kind, "status": "unknown", "reason": "payload metadata could not be resolved to a saved file"})
                continue
            data = payload_path.read_bytes()
            actual_hash = sha256_bytes(data)
            declared_hash = item.get("sha256")
            if isinstance(declared_hash, str) and HEX64.fullmatch(declared_hash) and actual_hash != declared_hash:
                raise ExportError(f"model payload hash mismatch: {payload_path}")
            suffix = ".request.bin" if kind == "request" else ".response.bin"
            out_rel = Path("payloads") / source["source_id"] / (sha256_bytes(request_id.encode("utf-8"))[:32] + suffix)
            out_path = output / out_rel
            _write_bytes_new(out_path, data)
            payload_rows.append({
                "schema_version": ROW_SCHEMA,
                "source_id": source["source_id"],
                "source_class": source["source_class"],
                "run_id": row.get("run_id", source["identity"].get("run_id")),
                "attempt_id": row.get("attempt_id", source["identity"].get("attempt_id")),
                "case_id": row.get("case_id", source["identity"].get("case_id")),
                "physical_request_id": request_id,
                "source_row_index": row_index,
                "payload_kind": kind,
                "status": "pass",
                "provenance": "measured",
                "source_relative_path": payload_path.resolve().relative_to(source["root"].resolve()).as_posix(),
                "source_sha256": actual_hash,
                "declared_sha256": declared_hash,
                "bytes": len(data),
                "complete": item.get("complete"),
                "export_relative_path": out_rel.as_posix(),
                "export_sha256": sha256_file(out_path),
            })
    return payload_rows, joins


def _model_attempt_rows(source: Mapping[str, Any], model_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    joins: list[dict[str, Any]] = []
    for row in model_rows:
        request_id = row.get("physical_request_id") or row.get("request_id")
        if isinstance(request_id, str) and request_id:
            grouped[request_id].append(row)
        elif row.get("terminal") is True:
            joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "model_attempt_identity", "status": "unknown", "reason": "terminal model row has no physical request identity"})
    attempts: list[dict[str, Any]] = []
    for request_id, rows in sorted(grouped.items()):
        starts = [row for row in rows if row.get("terminal") is False]
        terminals = [row for row in rows if row.get("terminal") is True and row.get("event_kind") == "model_request"]
        terminal = terminals[-1] if terminals else None
        identity = _merge_identity(source.get("identity"), terminal, starts[0] if starts else None)
        if len(terminals) > 1:
            joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "model_attempt_identity", "physical_request_id": request_id, "status": "fail", "reason": "more than one terminal model row for one physical request"})
        attempts.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "source_class": source["source_class"],
            **identity,
            "physical_request_id": request_id,
            "logical_request_id": (terminal or starts[0] if terminal or starts else {}).get("logical_request_id"),
            "retry_index": (terminal or starts[0] if terminal or starts else {}).get("retry_index"),
            "retry_of": (terminal or starts[0] if terminal or starts else {}).get("retry_of"),
            "start_event_id": starts[0].get("event_id") if starts else None,
            "terminal_event_id": terminal.get("event_id") if terminal else None,
            "start_mono_ns": starts[0].get("start_mono_ns") if starts else (terminal or {}).get("start_mono_ns"),
            "end_mono_ns": terminal.get("end_mono_ns") if terminal else None,
            "duration_ms": terminal.get("duration_ms") if terminal else None,
            "status": terminal.get("status") if terminal else "incomplete",
            "terminal_present": terminal is not None,
            "physical_attempt_status": "complete" if terminal is not None else "incomplete_missing_terminal",
            "provenance": terminal.get("provenance", "unknown") if terminal else "unknown",
            "availability": terminal.get("availability", "unknown") if terminal else "unknown",
            "request_body_sha256": (terminal or starts[0] if terminal or starts else {}).get("request_body_sha256"),
            "response_body_sha256": (terminal or starts[0] if terminal or starts else {}).get("response_body_sha256"),
            "record": dict(terminal or starts[0] if terminal or starts else {}),
        })
    return attempts, joins


def _merge_intervals(intervals: Sequence[tuple[int, int]]) -> list[list[int]]:
    values = sorted((int(start), int(end)) for start, end in intervals if end > start)
    merged: list[list[int]] = []
    for start, end in values:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return merged


def _lifecycle_export(source: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    intervals: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []
    terminal_by_span: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    starts_by_span: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        span_id = row.get("span_id")
        if not isinstance(span_id, str):
            continue
        if row.get("terminal") is True:
            terminal_by_span[span_id].append(row)
        elif row.get("terminal") is False:
            starts_by_span[span_id].append(row)
    for span_id in sorted(set(terminal_by_span) | set(starts_by_span)):
        starts = starts_by_span.get(span_id, [])
        terminals = terminal_by_span.get(span_id, [])
        start = starts[0] if starts else (terminals[0] if terminals else {})
        terminal = terminals[-1] if terminals else None
        if len(starts) != 1 or len(terminals) != 1:
            joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "lifecycle_span", "span_id": span_id, "status": "unknown", "reason": f"expected one start and one terminal; starts={len(starts)} terminals={len(terminals)}"})
        start_ns = start.get("start_mono_ns")
        end_ns = terminal.get("end_mono_ns") if terminal else None
        valid = isinstance(start_ns, int) and not isinstance(start_ns, bool) and isinstance(end_ns, int) and not isinstance(end_ns, bool) and end_ns >= start_ns
        intervals.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "source_class": source["source_class"],
            "run_id": start.get("run_id", source["identity"].get("run_id")),
            "attempt_id": start.get("attempt_id", source["identity"].get("attempt_id")),
            "case_id": start.get("case_id", source["identity"].get("case_id")),
            "span_id": span_id,
            "parent_event_id": start.get("parent_event_id"),
            "phase": start.get("phase"),
            "event_kind": terminal.get("event_kind") if terminal else start.get("event_kind"),
            "start_event_id": start.get("event_id"),
            "terminal_event_id": terminal.get("event_id") if terminal else None,
            "start_mono_ns": start_ns,
            "end_mono_ns": end_ns,
            "duration_ms": terminal.get("duration_ms") if terminal else None,
            "status": terminal.get("status") if terminal else "incomplete",
            "provenance": terminal.get("provenance", start.get("provenance", "unknown")) if terminal else "unknown",
            "availability": terminal.get("availability", start.get("availability", "unknown")) if terminal else "unknown",
            "interval_status": "complete" if valid else "unknown_invalid_or_unclosed",
        })
    complete = [row for row in intervals if row["interval_status"] == "complete"]
    outer = [row for row in complete if row.get("phase") == "outer_swe_agent" and row.get("status") in {"success", "failure", "timeout", "unavailable"}]
    e2e: list[dict[str, Any]] = []
    if len(outer) != 1:
        e2e.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "run_id": source["identity"].get("run_id"), "attempt_id": source["identity"].get("attempt_id"), "case_id": source["identity"].get("case_id"), "status": "unknown", "reason": f"requires exactly one valid outer_swe_agent interval; observed {len(outer)}", "provenance": "derived"})
        joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "lifecycle_outer_e2e", "status": "unknown", "reason": "outer interval is absent or ambiguous"})
        return intervals, e2e, joins
    outer_row = outer[0]
    outer_start = int(outer_row["start_mono_ns"])
    outer_end = int(outer_row["end_mono_ns"])
    attributed = [
        row for row in complete
        if row.get("phase") not in NON_ATTRIBUTING_PHASES
        and row.get("event_kind") != "hardware_snapshot"
        and row.get("availability") == "measured"
        and row.get("start_mono_ns") is not None
        and row.get("end_mono_ns") is not None
    ]
    clipped = [(max(outer_start, int(row["start_mono_ns"])), min(outer_end, int(row["end_mono_ns"]))) for row in attributed if int(row["end_mono_ns"]) > outer_start and int(row["start_mono_ns"]) < outer_end]
    union = _merge_intervals(clipped)
    cursor = outer_start
    unknown: list[list[int]] = []
    for left, right in union:
        if left > cursor:
            unknown.append([cursor, left])
        cursor = max(cursor, right)
    if cursor < outer_end:
        unknown.append([cursor, outer_end])
    e2e.append({
        "schema_version": ROW_SCHEMA,
        "source_id": source["source_id"],
        "source_class": source["source_class"],
        "run_id": outer_row.get("run_id", source["identity"].get("run_id")),
        "attempt_id": outer_row.get("attempt_id", source["identity"].get("attempt_id")),
        "case_id": outer_row.get("case_id", source["identity"].get("case_id")),
        "status": "pass",
        "provenance": "derived",
        "outer_span_id": outer_row.get("span_id"),
        "outer_start_mono_ns": outer_start,
        "outer_end_mono_ns": outer_end,
        "outer_e2e_ms": (outer_end - outer_start) / 1_000_000,
        "measured_union_ms": sum(right - left for left, right in union) / 1_000_000,
        "unknown_residual_ms": sum(right - left for left, right in unknown) / 1_000_000,
        "closure_error_ms": (sum(right - left for left, right in union) + sum(right - left for left, right in unknown) - (outer_end - outer_start)) / 1_000_000,
        "measured_intervals": union,
        "unknown_intervals": unknown,
        "attributed_interval_count": len(attributed),
        "phase_union_ms": {
            phase: sum(right - left for left, right in _merge_intervals([(max(outer_start, int(row["start_mono_ns"])), min(outer_end, int(row["end_mono_ns"]))) for row in attributed if row.get("phase") == phase])) / 1_000_000
            for phase in sorted({row.get("phase") for row in attributed})
        },
        "nested_intervals_union_not_sum": True,
        "outer_wrapper_excluded_from_measured_union": True,
    })
    return intervals, e2e, joins


def _component_records(source: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    root: Path = source["root"]
    for path in _candidate_files(root):
        if path.name not in COMPONENT_JSONL_NAMES and path.name not in COMPONENT_JSON_NAMES and path.name not in LABEL_NAMES:
            continue
        descriptor = next((item for item in source["source_files"] if item.get("relative_path") == path.relative_to(root).as_posix()), None)
        if descriptor is None:
            descriptor = _file_descriptor(root, path, role="component_artifact", source_id=source["source_id"])
            source["source_files"].append(descriptor)
        if path.name in LABEL_NAMES:
            if path.suffix == ".jsonl":
                values = _read_jsonl(path)
                for index, value in enumerate(values):
                    labels.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "source_class": source["source_class"], "label_source": path.relative_to(root).as_posix(), "label_row_index": index, "label_status": "observed", "provenance": "measured", "record": value})
            else:
                value = _read_json(path)
                labels.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "source_class": source["source_class"], "label_source": path.relative_to(root).as_posix(), "label_row_index": 0, "label_status": "observed", "provenance": "measured", "record": value})
            continue
        if path.suffix == ".jsonl":
            values = _read_jsonl(path)
            for index, value in enumerate(values):
                rows.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "source_class": source["source_class"], "component_source": path.relative_to(root).as_posix(), "component_row_index": index, "source_file_sha256": descriptor["sha256"], "record_sha256": sha256_bytes(canonical_json(value).encode("utf-8")), "join_scope": "source_local_only", "record": value})
        else:
            value = _read_json(path)
            rows.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "source_class": source["source_class"], "component_source": path.relative_to(root).as_posix(), "component_row_index": 0, "source_file_sha256": descriptor["sha256"], "record_sha256": sha256_bytes(canonical_json(value).encode("utf-8")), "join_scope": "source_local_only", "record": value})
    if not labels:
        labels.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "source_class": source["source_class"], "label_status": "unknown", "provenance": "unavailable", "reason": "no explicit evaluator/outcome label artifact was supplied; component metadata is not a label"})
    return rows, labels


def _bindings(source: Mapping[str, Any], component_rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    hardware: list[dict[str, Any]] = []
    configs: list[dict[str, Any]] = []
    bindings: list[dict[str, Any]] = []
    tm = source.get("telemetry_manifest")
    if isinstance(tm, Mapping):
        hardware.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "telemetry_manifest_hardware", "provenance": "declared", "hardware_profile_sha256": tm.get("hardware_profile_sha256"), "raw_hardware_inventory": tm.get("raw_hardware_inventory"), "hardware_model_fields": tm.get("hardware_model_fields"), "record": dict(tm)})
    for row in source["stream_rows"].get("hardware", []):
        hardware.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "hardware_snapshot", "provenance": row.get("provenance", "unknown"), "availability": row.get("availability", "unknown"), "record": dict(row)})
    for row in component_rows:
        name = Path(str(row.get("component_source", ""))).name
        record = row.get("record")
        if name == "local_cpu_profile.json":
            hardware.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "local_cpu_profile", "provenance": "measured", "source_file_sha256": row.get("source_file_sha256"), "record": record})
        elif name == "container_mounts.json":
            hardware.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "container_mounts", "provenance": "measured", "source_file_sha256": row.get("source_file_sha256"), "record": record})
        elif name == "capture.json" and isinstance(record, Mapping) and str(record.get("schema_version", "")).startswith("assignment.acquisition-cpu"):
            hardware.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "cpu_capture_manifest", "provenance": "measured", "source_file_sha256": row.get("source_file_sha256"), "record": record})
        elif name == "capture.json" and isinstance(record, Mapping) and str(record.get("schema_version", "")).startswith("assignment.acquisition-model"):
            bindings.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "model_snapshot_binding", "provenance": "measured", "source_file_sha256": row.get("source_file_sha256"), "model_revision": record.get("model_revision"), "tokenizer_revision": record.get("tokenizer_revision"), "weight_file_bytes": record.get("weight_file_bytes"), "weight_shard_count": record.get("weight_shard_count"), "record": record})
    if isinstance(source.get("bpf_manifest"), Mapping):
        bpf = source["bpf_manifest"]
        bindings.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "bpf_collector_identity", "provenance": "declared", "identity": bpf.get("identity"), "program_sha256": bpf.get("program_sha256"), "raw_event_stream_sha256": bpf.get("raw_event_stream_sha256"), "record": dict(bpf)})
    for descriptor in source["source_files"]:
        rel = str(descriptor["relative_path"])
        if any(term in Path(rel).name.lower() for term in ("config", "manifest", "invocation", "dockerfile", "runtime", "source_snapshot", "metadata", "plan")):
            configs.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "configuration_or_runtime_input", "provenance": "declared", **descriptor})
        if "source" in rel.lower() or "git" in rel.lower() or Path(rel).name in {"artifact_hashes.json", "artifact_sha256.json", "source_snapshot_manifest.json", "capture.json"}:
            bindings.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "source_or_artifact_binding", "provenance": "measured", **descriptor})
    # Component evidence is intentionally source-local; do not infer a
    # production join from matching request ids in another root.
    if source["source_class"] != "v2_join_candidate":
        bindings.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "binding_kind": "join_scope", "provenance": "declared", "join_scope": "independent_component_evidence", "reason": "source lacks a complete local v2 lifecycle/model/BPF set; no cross-source join permitted"})
    return hardware, configs, bindings


def _copy_source_artifacts(source: Mapping[str, Any], output: Path) -> list[dict[str, Any]]:
    """Copy the bounded selected source inventory for saved-only recovery."""
    rows: list[dict[str, Any]] = []
    for descriptor in source["source_files"]:
        relative = _safe_relative(str(descriptor["relative_path"]))
        source_path = source["root"] / relative
        if not source_path.is_file() or source_path.is_symlink():
            raise ExportError(f"selected source artifact disappeared before export: {source_path}")
        data = source_path.read_bytes()
        digest = sha256_bytes(data)
        if digest != descriptor.get("sha256"):
            raise ExportError(f"selected source artifact changed during export: {source_path}")
        out_rel = Path("source_artifacts") / str(source["source_id"]) / relative
        out_path = output / out_rel
        _write_bytes_new(out_path, data)
        rows.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "binding_kind": "saved_source_artifact",
            "provenance": "measured",
            "source_relative_path": relative.as_posix(),
            "export_relative_path": out_rel.as_posix(),
            "bytes": len(data),
            "source_sha256": digest,
            "export_sha256": sha256_file(out_path),
            "role": descriptor.get("role"),
        })
    return rows


def _output_file_manifest(output: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "export_manifest.json":
            continue
        rows.append({"path": path.relative_to(output).as_posix(), "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return rows


def export_acquisition_evidence(source_dirs: Sequence[str | Path], output_dir: str | Path) -> dict[str, Any]:
    """Create a source-local lossless evidence export.

    ``source_dirs`` are never joined with one another.  The function returns
    the manifest written to ``output_dir/export_manifest.json``.  A structural
    export can pass while its ``limitations`` list says that PDF labels or
    hardware are unavailable; this is intentional and prevents a component
    diagnostic from being mislabeled as a production case.
    """
    if not source_dirs:
        raise ExportError("at least one source directory is required")
    output = Path(output_dir).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ExportError(f"refusing to overwrite existing export directory: {output}")
    if any(parent.is_symlink() for parent in (output.parent, *output.parent.parents)):
        raise ExportError("export path traverses a symlink")
    output.mkdir(parents=True, exist_ok=False)
    sources = [_load_source(value, index) for index, value in enumerate(source_dirs)]
    telemetry_rows: list[dict[str, Any]] = []
    bpf_actions: list[dict[str, Any]] = []
    bpf_operation_iters: list[Iterator[dict[str, Any]]] = []
    bpf_operation_count = 0
    model_attempts: list[dict[str, Any]] = []
    payload_rows: list[dict[str, Any]] = []
    lifecycle_intervals: list[dict[str, Any]] = []
    e2e_rows: list[dict[str, Any]] = []
    component_rows: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    hardware_bindings: list[dict[str, Any]] = []
    configuration_bindings: list[dict[str, Any]] = []
    source_bindings: list[dict[str, Any]] = []
    joins: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    source_manifest_rows: list[dict[str, Any]] = []
    for source in sources:
        source_manifest_rows.append({
            "schema_version": ROW_SCHEMA,
            "source_id": source["source_id"],
            "source_index": source["source_index"],
            "source_path": source["root_path"],
            "source_class": source["source_class"],
            "join_scope": "source_local_only",
            **source["identity"],
            "source_file_count": len(source["source_files"]),
            "source_bytes": sum(int(item["bytes"]) for item in source["source_files"]),
            "declared_hash_audit": source["declared_hash_audits"],
        })
        telemetry_rows.extend(source["telemetry_wrappers"])
        actions, operations, bpf_joins, operation_count = _bpf_event_rows(source)
        bpf_actions.extend(actions)
        bpf_operation_iters.append(operations)
        bpf_operation_count += operation_count
        joins.extend(bpf_joins)
        model_rows = source["stream_rows"].get("model", [])
        attempts, attempt_joins = _model_attempt_rows(source, model_rows)
        model_attempts.extend(attempts)
        joins.extend(attempt_joins)
        copied, payload_joins = _copy_payloads(source, output, model_rows)
        payload_rows.extend(copied)
        joins.extend(payload_joins)
        intervals, e2e, lifecycle_joins = _lifecycle_export(source, source["stream_rows"].get("lifecycle", []))
        lifecycle_intervals.extend(intervals)
        e2e_rows.extend(e2e)
        joins.extend(lifecycle_joins)
        components, source_labels = _component_records(source)
        component_rows.extend(components)
        labels.extend(source_labels)
        hw, cfg, src = _bindings(source, components)
        hardware_bindings.extend(hw)
        configuration_bindings.extend(cfg)
        source_bindings.extend(src)
        source_bindings.extend(_copy_source_artifacts(source, output))
        # Demonstrate strict source-local identity consistency.  A source may
        # have incomplete identity, but conflicting values are never hidden.
        for stream, rows in source["stream_rows"].items():
            for index, row in enumerate(rows):
                for key in IDENTITY_FIELDS:
                    value = row.get(key)
                    expected = source["identity"].get(key)
                    if value is not None and expected not in {None, value}:
                        joins.append({"schema_version": ROW_SCHEMA, "source_id": source["source_id"], "join_kind": "source_identity", "stream": stream, "source_row_index": index, "field": key, "status": "fail", "reason": "journal row identity differs from source identity", "row_value": value, "source_value": expected})
        assertions.append({"schema_version": ASSERTION_SCHEMA, "source_id": source["source_id"], "assertion": "source_files_hashable", "status": "pass", "count": len(source["source_files"])})
        assertions.append({"schema_version": ASSERTION_SCHEMA, "source_id": source["source_id"], "assertion": "source_local_join_scope", "status": "pass", "detail": "cross-source identity joins are disabled"})
        declared_failures = [row for row in source["declared_hash_audits"] if row.get("status") == "fail"]
        assertions.append({"schema_version": ASSERTION_SCHEMA, "source_id": source["source_id"], "assertion": "declared_artifact_hashes", "status": "fail" if declared_failures else "pass", "failures": declared_failures})
    # A source-local exporter must not accidentally match IDs from separate
    # roots.  Keep the collision information visible even when strings match.
    for key in IDENTITY_FIELDS:
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in source_manifest_rows:
            value = row.get(key)
            if isinstance(value, str):
                grouped[value].append(str(row["source_id"]))
        for value, source_ids in grouped.items():
            if len(source_ids) > 1:
                joins.append({"schema_version": ROW_SCHEMA, "join_kind": "cross_source_identity", "field": key, "value": value, "source_ids": source_ids, "status": "unknown", "reason": "matching identity strings across source roots are not joined without an explicit production manifest"})
    _write_jsonl(output / "sources.jsonl", source_manifest_rows)
    _write_jsonl(output / "telemetry_rows.jsonl", telemetry_rows)
    _write_jsonl(output / "bpf_actions.jsonl", bpf_actions)
    _write_jsonl(output / "bpf_operations.jsonl", itertools.chain.from_iterable(bpf_operation_iters))
    _write_jsonl(output / "model_attempts.jsonl", model_attempts)
    _write_jsonl(output / "model_payloads.jsonl", payload_rows)
    _write_jsonl(output / "lifecycle_intervals.jsonl", lifecycle_intervals)
    _write_jsonl(output / "e2e_reconstructions.jsonl", e2e_rows)
    _write_jsonl(output / "component_records.jsonl", component_rows)
    _write_jsonl(output / "workload_labels.jsonl", labels)
    _write_jsonl(output / "hardware_bindings.jsonl", hardware_bindings)
    _write_jsonl(output / "configuration_bindings.jsonl", configuration_bindings)
    _write_jsonl(output / "source_bindings.jsonl", source_bindings)
    _write_jsonl(output / "unknown_joins.jsonl", joins)
    assertions.extend([
        {"schema_version": ASSERTION_SCHEMA, "assertion": "bpf_operations_are_not_aggregate_fallback", "status": "pass" if all(not row.get("aggregate_only") for row in bpf_actions if row.get("operation_evidence_status") == "complete") else "fail", "detail": "aggregate-only actions remain explicitly marked and are never emitted as operation rows"},
        {"schema_version": ASSERTION_SCHEMA, "assertion": "payload_hash_roundtrip", "status": "pass" if all(row.get("status") != "pass" or row.get("source_sha256") == row.get("export_sha256") for row in payload_rows) else "fail", "count": sum(row.get("status") == "pass" for row in payload_rows)},
        {"schema_version": ASSERTION_SCHEMA, "assertion": "saved_source_hash_roundtrip", "status": "pass" if all(row.get("binding_kind") != "saved_source_artifact" or row.get("source_sha256") == row.get("export_sha256") for row in source_bindings) else "fail", "count": sum(row.get("binding_kind") == "saved_source_artifact" for row in source_bindings)},
        {"schema_version": ASSERTION_SCHEMA, "assertion": "unknown_joins_are_explicit", "status": "pass", "count": len(joins)},
        {"schema_version": ASSERTION_SCHEMA, "assertion": "no_cross_source_join", "status": "pass", "detail": "all rows carry source_id and every cross-root identity collision is an unknown join"},
    ])
    _write_jsonl(output / "reconstruction_assertions.jsonl", assertions)
    limitations: list[str] = []
    if not model_attempts:
        limitations.append("No saved physical model attempts were found in at least one component; this export does not claim D7-D9 GPU reconstruction for that source.")
    if not bpf_operation_count:
        limitations.append("No individual BPF operation rows were decoded for at least one source; aggregate-only records remain unavailable for D7-D9 CPU reconstruction.")
    if any(row.get("label_status") == "unknown" for row in labels):
        limitations.append("Evaluator/workload labels are unavailable or not supplied; unknown labels remain explicit and are not inferred from component metadata.")
    if any(row.get("status") == "unknown" for row in e2e_rows):
        limitations.append("At least one source lacks a uniquely identifiable outer SWE-agent interval; its E2E reconstruction is unknown.")
    fail_assertions = [row for row in assertions if row.get("status") == "fail"]
    manifest = {
        "schema_version": EXPORT_SCHEMA,
        "export_version": "lossless-v2-bounded-20260909",
        "source_count": len(sources),
        "sources": [row["source_id"] for row in source_manifest_rows],
        "source_local_join_only": True,
        "status": "fail" if fail_assertions else "pass",
        "limitations": limitations,
        "counts": {
            "telemetry_rows": len(telemetry_rows),
            "bpf_actions": len(bpf_actions),
            "bpf_operations": bpf_operation_count,
            "model_attempts": len(model_attempts),
            "model_payload_rows": len(payload_rows),
            "payload_complete_rows": sum(row.get("status") == "pass" for row in payload_rows),
            "lifecycle_intervals": len(lifecycle_intervals),
            "e2e_reconstructions": len(e2e_rows),
            "component_records": len(component_rows),
            "workload_labels": len(labels),
            "unknown_joins": len(joins),
        },
        "artifacts": {
            "sources": "sources.jsonl",
            "telemetry_rows": "telemetry_rows.jsonl",
            "bpf_actions": "bpf_actions.jsonl",
            "bpf_operations": "bpf_operations.jsonl",
            "model_attempts": "model_attempts.jsonl",
            "model_payloads": "model_payloads.jsonl",
            "lifecycle_intervals": "lifecycle_intervals.jsonl",
            "e2e_reconstructions": "e2e_reconstructions.jsonl",
            "component_records": "component_records.jsonl",
            "workload_labels": "workload_labels.jsonl",
            "hardware_bindings": "hardware_bindings.jsonl",
            "configuration_bindings": "configuration_bindings.jsonl",
            "source_bindings": "source_bindings.jsonl",
            "unknown_joins": "unknown_joins.jsonl",
            "reconstruction_assertions": "reconstruction_assertions.jsonl",
        },
        "method": {
            "bpf_operation_identity": ["source_id", "run_id", "attempt_id", "case_id", "action_token", "record_index", "action_record_index"],
            "physical_model_identity": ["source_id", "run_id", "attempt_id", "case_id", "physical_request_id", "retry_index"],
            "timing": "monotonic start/end retained; nested lifecycle intervals are unioned for E2E and phase unions are not additive",
            "unknown_policy": "unavailable, censored, missing, conflicting, and cross-source joins are explicit rows; no aggregate-only fallback",
            "legacy_policy": "sources without a complete v2 set are labeled legacy/component_evidence and are not promoted to a joined production case",
            "payload_policy": "request/response bytes are copied and SHA-256 checked when terminal model rows reference saved complete artifacts",
        },
        "assertion_failures": fail_assertions,
        "export_file_hashes": [],
    }
    # Write a provisional manifest, compute all non-manifest file hashes, then
    # rewrite the manifest once.  No source artifact is changed.
    _write_json(output / "export_manifest.json", manifest)
    manifest["export_file_hashes"] = _output_file_manifest(output)
    # The first manifest exists by design; replace only this new manifest with
    # an atomic create/rename, never an existing historical path.
    temporary = output / ".export_manifest.json.tmp"
    _write_bytes_new(temporary, _json_bytes(manifest))
    os.replace(temporary, output / "export_manifest.json")
    return manifest


def _bundle_jsonl(path: Path) -> list[dict[str, Any]]:
    return _read_jsonl(path)


def _bpf_decoder_spec(binary: Path, manifest: Mapping[str, Any] | None) -> tuple[str | None, int | None]:
    """Resolve the saved ABI binding for validator-side binary replay."""

    schema_version: str | None = None
    record_size: int | None = None
    if isinstance(manifest, Mapping):
        stream_meta = manifest.get("raw_event_stream")
        if isinstance(stream_meta, Mapping):
            if isinstance(stream_meta.get("schema_version"), str):
                schema_version = stream_meta["schema_version"]
            if isinstance(stream_meta.get("record_size_bytes"), int):
                record_size = int(stream_meta["record_size_bytes"])
    from agentic_sim.telemetry.bpf_work import (  # type: ignore
        BPF_EVENT_RECORD_SIZE,
        BPF_EVENT_RECORD_SIZE_LEGACY,
        BPF_EVENT_SCHEMA,
        BPF_EVENT_SCHEMA_LEGACY,
    )

    sizes = {
        BPF_EVENT_SCHEMA: BPF_EVENT_RECORD_SIZE,
        BPF_EVENT_SCHEMA_LEGACY: BPF_EVENT_RECORD_SIZE_LEGACY,
    }
    if schema_version is not None and record_size is None:
        record_size = sizes.get(schema_version)
    elif record_size is not None and schema_version is None:
        schema_version = next((name for name, size in sizes.items() if size == record_size), None)
    elif schema_version is None and record_size is None:
        candidates = [(name, size) for name, size in sizes.items() if binary.stat().st_size % size == 0]
        if len(candidates) == 1:
            schema_version, record_size = candidates[0]
    return schema_version, record_size


def _validate_bpf_binary_roundtrip(root: Path, loaded: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """Replay saved binary packets and compare every exported operation row.

    This is intentionally a second, saved-only proof: operation rows cannot
    pass merely because their count matches an aggregate.  The exact packet,
    stream hash, ABI binding, decoded record, token, and record index must all
    agree with the copied binary source.
    """

    errors: list[str] = []
    source_ids = {row.get("source_id") for row in loaded.get("sources", [])}
    operations_by_source: dict[Any, dict[int, Mapping[str, Any]]] = defaultdict(dict)
    for row_index, row in enumerate(loaded.get("bpf_operations", [])):
        source_id = row.get("source_id")
        record_index = row.get("record_index")
        if not isinstance(record_index, int) or isinstance(record_index, bool):
            continue
        if record_index in operations_by_source[source_id]:
            errors.append(f"duplicate BPF operation record index for source {source_id}: {record_index}")
        operations_by_source[source_id][record_index] = row

    binary_paths: dict[Any, Path] = {}
    manifest_paths: dict[Any, list[tuple[int, Path]]] = defaultdict(list)
    for row in loaded.get("source_bindings", []):
        if row.get("binding_kind") != "saved_source_artifact":
            continue
        source_id = row.get("source_id")
        relative = row.get("source_relative_path")
        exported = row.get("export_relative_path")
        if not isinstance(relative, str) or not isinstance(exported, str):
            continue
        try:
            path = root / _safe_relative(exported)
        except ExportError as exc:
            errors.append(f"invalid saved BPF artifact path for {source_id}: {exc}")
            continue
        name = Path(relative).name
        if name == "raw_events.bin":
            if source_id in binary_paths:
                errors.append(f"multiple saved raw_events.bin artifacts for source {source_id}")
            binary_paths[source_id] = path
        elif name == "bpf_collector_manifest.json":
            manifest_paths[source_id].append((0, path))
        elif name == "work_summary.json":
            manifest_paths[source_id].append((1, path))
        elif name == "production_capture_audit.json":
            manifest_paths[source_id].append((2, path))

    for source_id in source_ids:
        rows = operations_by_source.get(source_id, {})
        binary = binary_paths.get(source_id)
        if binary is None:
            if rows:
                errors.append(f"BPF operation rows for source {source_id} have no saved raw_events.bin")
            continue
        if not binary.is_file() or binary.is_symlink():
            errors.append(f"saved raw_events.bin is missing for source {source_id}")
            continue
        if not rows:
            # Empty streams are valid; non-empty streams without operation
            # rows are not, even if aggregate evidence is absent.
            if binary.stat().st_size:
                errors.append(f"saved raw_events.bin for source {source_id} has no exported operation rows")
            continue
        manifest_value: Mapping[str, Any] | None = None
        manifest_path = None
        for _, candidate in sorted(manifest_paths.get(source_id, []), key=lambda item: item[0]):
            if candidate.is_file() and not candidate.is_symlink():
                manifest_path = candidate
                break
        if manifest_path is not None:
            try:
                value = _read_json(manifest_path)
                if isinstance(value, Mapping):
                    if manifest_path.name == "production_capture_audit.json":
                        bpf = value.get("bpf")
                        native = bpf.get("native_sink") if isinstance(bpf, Mapping) else None
                        if isinstance(native, Mapping) and isinstance(native.get("record_size_bytes"), int):
                            manifest_value = {
                                "raw_event_stream": {
                                    "record_size_bytes": native["record_size_bytes"],
                                    "sha256": bpf.get("raw_stream_sha256") if isinstance(bpf, Mapping) else None,
                                }
                            }
                    else:
                        manifest_value = value
            except ExportError as exc:
                errors.append(f"cannot read saved BPF manifest for source {source_id}: {exc}")
        schema_version, record_size = _bpf_decoder_spec(binary, manifest_value)
        if schema_version is None or record_size is None:
            errors.append(f"saved BPF ABI is absent or ambiguous for source {source_id}")
            continue
        stream_hash = sha256_file(binary)
        declared_stream_hash = None
        if isinstance(manifest_value, Mapping):
            stream_meta = manifest_value.get("raw_event_stream")
            if isinstance(manifest_value.get("raw_event_stream_sha256"), str):
                declared_stream_hash = manifest_value["raw_event_stream_sha256"]
            elif isinstance(stream_meta, Mapping) and isinstance(stream_meta.get("sha256"), str):
                declared_stream_hash = stream_meta["sha256"]
        if isinstance(declared_stream_hash, str) and HEX64.fullmatch(declared_stream_hash) and declared_stream_hash != stream_hash:
            errors.append(f"saved BPF manifest stream hash mismatch for source {source_id}")
        seen: set[int] = set()
        try:
            with binary.open("rb") as handle:
                for record_index, decoded in enumerate(
                    iter_bpf_events(
                        binary,
                        schema_version=schema_version,
                        record_size_bytes=record_size,
                    )
                ):
                    row = rows.get(record_index)
                    if row is None:
                        errors.append(f"missing exported BPF operation row for source {source_id} record {record_index}")
                        continue
                    seen.add(record_index)
                    handle.seek(record_index * record_size)
                    packet = handle.read(record_size)
                    packet_hash = sha256_bytes(packet)
                    if row.get("raw_event_packet_sha256") != packet_hash:
                        errors.append(f"BPF packet hash mismatch for source {source_id} record {record_index}")
                    if row.get("raw_event_stream_sha256") != stream_hash:
                        errors.append(f"BPF stream hash mismatch for source {source_id} record {record_index}")
                    if row.get("raw_event_schema_version") not in {None, schema_version}:
                        errors.append(f"BPF ABI schema mismatch for source {source_id} record {record_index}")
                    if row.get("action_token") != decoded.get("token"):
                        errors.append(f"BPF action token mismatch for source {source_id} record {record_index}")
                    expected_record = row.get("record")
                    if not isinstance(expected_record, Mapping):
                        errors.append(f"BPF operation lacks decoded record for source {source_id} record {record_index}")
                    elif canonical_json(expected_record) != canonical_json(decoded):
                        errors.append(f"BPF decoded record mismatch for source {source_id} record {record_index}")
                    args, args_provenance = _event_args(decoded)
                    if row.get("syscall_args") != args:
                        errors.append(f"BPF syscall args mismatch for source {source_id} record {record_index}")
                    allowed_args_provenance = {args_provenance}
                    if args_provenance == "unavailable_not_in_saved_record":
                        # Historical v2 exports may have filled arguments from
                        # an inline aggregate event.  That source remains
                        # explicit; it is not treated as ABI evidence.
                        allowed_args_provenance.add("measured_inline_event")
                    if row.get("syscall_args_provenance") not in allowed_args_provenance:
                        errors.append(f"BPF syscall args provenance mismatch for source {source_id} record {record_index}")
                    for field in ("status", "kernel_start_ns", "kernel_end_ns", "duration_ns", "censor_boundary_ns"):
                        if field in row and row.get(field) != decoded.get(field):
                            errors.append(f"BPF {field} mismatch for source {source_id} record {record_index}")
        except (OSError, TypeError, ValueError) as exc:
            errors.append(f"cannot replay saved BPF stream for source {source_id}: {exc}")
            continue
        extra = set(rows) - seen
        for record_index in sorted(extra):
            errors.append(f"exported BPF operation row has no saved packet for source {source_id} record {record_index}")
    return errors


def validate_export(output_dir: str | Path) -> dict[str, Any]:
    """Reconstruct/check an export using only its saved bundle artifacts."""
    root = Path(output_dir).expanduser().resolve(strict=True)
    manifest_path = root / "export_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ExportError("export_manifest.json is missing")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != EXPORT_SCHEMA:
        raise ExportError("unsupported acquisition export schema")
    errors: list[str] = []
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise ExportError("export manifest has no artifact map")
    loaded: dict[str, list[dict[str, Any]]] = {}
    for name, relative in artifacts.items():
        if not isinstance(relative, str):
            errors.append(f"artifact path for {name} is not text")
            continue
        try:
            path = root / _safe_relative(relative)
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(f"missing export artifact {relative}")
            continue
        try:
            loaded[name] = _bundle_jsonl(path)
        except ExportError as exc:
            errors.append(str(exc))
    expected_ids = {row.get("source_id") for row in loaded.get("sources", [])}
    for name, rows in loaded.items():
        for index, row in enumerate(rows):
            if row.get("source_id") is not None and row.get("source_id") not in expected_ids:
                errors.append(f"{name}[{index}] references unknown source_id")
    # Validate immutable source-local identity and operation record indexes.
    op_by_token: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in loaded.get("bpf_operations", []):
        key = (row.get("source_id"), row.get("action_token"))
        op_by_token[key].append(row)
        if row.get("record_index") is None or row.get("action_record_index") is None:
            errors.append("BPF operation is missing global/action record index")
        args_prov = row.get("syscall_args_provenance")
        if args_prov is None:
            errors.append("BPF operation is missing syscall argument provenance")
        # New exports expose these fields directly.  Historical v1/v2
        # bundles placed the same decoder values under ``record``; accepting
        # that shape keeps old evidence revalidatable without relabeling it as
        # a newly acquired stream.
        record = row.get("record") if isinstance(row.get("record"), Mapping) else {}
        for field in (
            "status",
            "kernel_start_ns",
            "kernel_end_ns",
            "duration_ns",
            "censor_boundary_ns",
        ):
            if field not in row and field not in record:
                errors.append(f"BPF operation is missing {field} timing/status evidence")
    for key, rows in op_by_token.items():
        action_indexes = [row.get("action_record_index") for row in rows]
        if action_indexes != list(range(len(rows))):
            errors.append(f"BPF action record indexes are not lossless contiguous indexes for {key}")
    expected_by_token: dict[tuple[Any, Any], int] = {}
    for row in loaded.get("bpf_actions", []):
        if row.get("aggregate_only") is True:
            continue
        key = (row.get("source_id"), row.get("action_token"))
        expected = row.get("decoded_token_record_count")
        if isinstance(expected, int) and not isinstance(expected, bool):
            # A finalization row is represented by the authoritative action
            # row, but be defensive if an older export retained both rows.
            expected_by_token[key] = max(expected_by_token.get(key, 0), expected)
    for key, expected in expected_by_token.items():
        observed = len(op_by_token.get(key, []))
        if observed != expected:
            errors.append(f"BPF operation row count mismatch for {key}: observed={observed} expected={expected}")
    sources_by_id = {row.get("source_id"): row for row in loaded.get("sources", [])}
    for row in loaded.get("bpf_operations", []):
        source = sources_by_id.get(row.get("source_id"))
        if source is not None:
            for field in IDENTITY_FIELDS:
                if row.get(field) not in {None, source.get(field)}:
                    errors.append(f"BPF operation identity mismatch {row.get('source_id')} {field}")
    # Request body/response roundtrip is checked from exported bytes, not the
    # original source path.  This is the core saved-only assertion.
    for row in loaded.get("model_payloads", []):
        if row.get("status") != "pass":
            continue
        path_text = row.get("export_relative_path")
        if not isinstance(path_text, str):
            errors.append("complete model payload lacks export path")
            continue
        try:
            path = root / _safe_relative(path_text)
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(f"complete model payload file missing: {path_text}")
            continue
        actual = sha256_file(path)
        if actual != row.get("source_sha256") or actual != row.get("export_sha256"):
            errors.append(f"model payload hash roundtrip mismatch: {path_text}")
    for row in loaded.get("source_bindings", []):
        if row.get("binding_kind") != "saved_source_artifact":
            continue
        path_text = row.get("export_relative_path")
        if not isinstance(path_text, str):
            errors.append("saved source binding lacks export path")
            continue
        try:
            path = root / _safe_relative(path_text)
        except ExportError as exc:
            errors.append(str(exc))
            continue
        if not path.is_file() or path.is_symlink():
            errors.append(f"saved source artifact is missing: {path_text}")
            continue
        actual = sha256_file(path)
        if actual != row.get("source_sha256") or actual != row.get("export_sha256"):
            errors.append(f"saved source artifact hash roundtrip mismatch: {path_text}")
    errors.extend(_validate_bpf_binary_roundtrip(root, loaded))
    # E2E closure must hold exactly in integer nanoseconds; this rejects
    # accidental summation of nested intervals and preserves unknown residual.
    for row in loaded.get("e2e_reconstructions", []):
        if row.get("status") != "pass":
            continue
        outer = row.get("outer_e2e_ms")
        measured = row.get("measured_union_ms")
        unknown = row.get("unknown_residual_ms")
        closure = row.get("closure_error_ms")
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (outer, measured, unknown, closure)):
            errors.append("E2E reconstruction has non-finite timing")
        elif abs(float(closure)) > 1e-6:
            errors.append("E2E reconstruction does not close with measured union plus unknown complement")
    status = "pass" if not errors else "fail"
    result = {
        "schema_version": ASSERTION_SCHEMA,
        "status": status,
        "errors": errors,
        "saved_only": True,
        "counts": {name: len(rows) for name, rows in loaded.items()},
    }
    return result


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sources", nargs="*", type=Path, help="source acquisition directories")
    parser.add_argument("--output-dir", type=Path, required=False)
    parser.add_argument("--validate-only", type=Path)
    args = parser.parse_args()
    try:
        if args.validate_only is not None:
            result = validate_export(args.validate_only)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["status"] == "pass" else 1
        if args.output_dir is None:
            parser.error("--output-dir is required unless --validate-only is used")
        result = export_acquisition_evidence(args.sources, args.output_dir)
        validation = validate_export(args.output_dir)
        result["saved_only_validation"] = validation
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["status"] == "pass" and validation["status"] == "pass" else 1
    except ExportError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
