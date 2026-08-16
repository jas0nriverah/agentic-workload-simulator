"""Canonical per-instance artifact paths and validation.

This is deliberately a small contract rather than a second experiment
orchestration system.  Raw files are kept even when a process fails halfway
through an attempt.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .json import atomic_json_dump, file_sha256


REQUIRED_FILES = (
    "config.json",
    "events.jsonl",
    "model_calls.jsonl",
    "tool_calls.jsonl",
    "prediction.json",
    "eval.json",
    "summary.json",
)
COUNTER_ARTIFACTS = ("counters.parquet", "counters.unavailable.json")
# The counter table is intentionally narrow: these fields are the minimum
# lossless representation needed to interpret a metric sample without
# inventing request-level attribution. Producers may add columns, but cannot
# omit these fields from a real Parquet artifact.
COUNTER_SCHEMA_FIELDS = frozenset({"metric_name", "value", "timestamp_mono_ns", "aggregation_scope"})

# Optional, content-addressed sidecars. They never become prerequisites for a
# Level-0 control run, but are included in inventories when produced.
OPTIONAL_SIDECARS = (
    "run_manifest.json",
    "hardware.json",
    "profile_manifest.json",
    "profiling_overhead.json",
    "memory_estimate.json",
    "trace.perfetto.json",
    "service_calibration.json",
    "vllm_metrics_start.json",
    "vllm_metrics_end.json",
    "vllm_metrics_delta.json",
    "vllm_metrics_start.prom",
    "vllm_metrics_end.prom",
    "telemetry_scrapes.jsonl",
)


class ArtifactContractError(ValueError):
    """The attempt does not satisfy the canonical artifact contract."""


@dataclass(frozen=True)
class ArtifactLayout:
    root: Path
    experiment_id: str
    dataset: str
    instance_id: str
    attempt_id: str

    @property
    def directory(self) -> Path:
        return self.root / "data" / "raw" / self.experiment_id / self.dataset / self.instance_id / self.attempt_id

    def path(self, name: str) -> Path:
        if name not in REQUIRED_FILES and name not in COUNTER_ARTIFACTS and name not in OPTIONAL_SIDECARS and "/" not in name:
            raise ArtifactContractError(f"unknown canonical artifact: {name}")
        return self.directory / name

    def ensure(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory


def attempt_layout(root: str | Path, experiment_id: str, dataset: str, instance_id: str, attempt_id: str = "attempt-001") -> ArtifactLayout:
    for label, value in (("experiment_id", experiment_id), ("dataset", dataset), ("instance_id", instance_id), ("attempt_id", attempt_id)):
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ArtifactContractError(f"unsafe {label}")
    return ArtifactLayout(Path(root), experiment_id, dataset, instance_id, attempt_id)


def _unavailable(name: str, reason: str = "not produced yet") -> dict[str, Any]:
    return {"schema_version": "cr6.artifact.v2", "artifact": name, "status": "unavailable", "provenance": "unavailable", "reason": reason}


def _parquet_state(path: Path) -> dict[str, Any]:
    """Validate a counter parquet without accepting JSON masquerading as parquet."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"state": "invalid", "reason": f"read_error:{type(exc).__name__}"}
    if len(raw) < 12 or raw[:4] != b"PAR1" or raw[-4:] != b"PAR1":
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"state": "invalid", "reason": "missing_PAR1_magic"}
        if isinstance(value, dict) and value.get("provenance") == "unavailable":
            return {"state": "legacy_unavailable", "reason": "legacy_json_in_counters.parquet"}
        return {"state": "invalid", "reason": "non_parquet_payload"}
    try:
        import pyarrow.parquet as parquet  # type: ignore
        table = parquet.read_table(path)
        names = set(table.schema.names)
    except ImportError:
        return {"state": "invalid", "reason": "parquet_reader_unavailable"}
    except Exception as exc:  # pyarrow reports corruption/schema errors here
        return {"state": "invalid", "reason": f"parquet_unreadable:{type(exc).__name__}"}
    if not names or any(not isinstance(name, str) or not name for name in names):
        return {"state": "invalid", "reason": "empty_or_unnamed_schema", "schema": sorted(names)}
    missing = sorted(COUNTER_SCHEMA_FIELDS.difference(names))
    if missing:
        return {"state": "invalid", "reason": "counter_schema_missing_fields", "schema": sorted(names), "missing_fields": missing}
    return {"state": "valid", "reason": "par1_readable_schema_valid", "schema": sorted(names), "rows": table.num_rows}


def counter_state(layout: ArtifactLayout) -> dict[str, Any]:
    """Return the single logical counter state without modifying either file."""
    parquet_path = layout.path("counters.parquet")
    unavailable_path = layout.path("counters.unavailable.json")
    present = [path for path in (parquet_path, unavailable_path) if path.is_file()]
    if len(present) > 1:
        return {"state": "conflict", "reason": "both counter logical states are present", "paths": [str(path) for path in present]}
    if parquet_path.is_file():
        return {"artifact": "counters.parquet", **_parquet_state(parquet_path)}
    if unavailable_path.is_file():
        try:
            value = json.loads(unavailable_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"artifact": "counters.unavailable.json", "state": "invalid", "reason": f"invalid_json:{type(exc).__name__}"}
        if isinstance(value, dict) and value.get("status") == "unavailable" and value.get("provenance") == "unavailable":
            return {"artifact": "counters.unavailable.json", "state": "unavailable", "reason": value.get("reason", "counter export unavailable")}
        return {"artifact": "counters.unavailable.json", "state": "invalid", "reason": "unavailable marker must be status/provenance unavailable"}
    return {"state": "missing", "reason": "no counter logical state"}


def initialize_attempt(layout: ArtifactLayout, config: Mapping[str, Any], *, overwrite: bool = False) -> None:
    """Create the immutable config and explicit placeholders for an attempt."""
    layout.ensure()
    config_path = layout.path("config.json")
    if config_path.exists() and not overwrite:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        if existing != dict(config):
            raise ArtifactContractError("attempt config exists with different content")
    elif not config_path.exists():
        atomic_json_dump(config_path, dict(config))
    for name in ("events.jsonl", "model_calls.jsonl", "tool_calls.jsonl"):
        layout.path(name).touch(exist_ok=True)
    counters = layout.path("counters.parquet")
    unavailable = layout.path("counters.unavailable.json")
    if not counters.exists() and not unavailable.exists():
        # A real parquet counter table is populated only when the pinned
        # telemetry interface is available. Never write JSON into a .parquet path.
        atomic_json_dump(unavailable, _unavailable("counters.unavailable.json", "counter export not available"))
    for name in ("prediction.json", "eval.json", "summary.json"):
        if not layout.path(name).exists():
            atomic_json_dump(layout.path(name), _unavailable(name))


def validate_artifacts(layout: ArtifactLayout, *, require_complete: bool = False) -> dict[str, Any]:
    """Return a manifest-like validation report without changing any artifact."""
    missing = [name for name in REQUIRED_FILES if not layout.path(name).is_file()]
    statuses: dict[str, str] = {}
    for name in ("prediction.json", "eval.json", "summary.json"):
        path = layout.path(name)
        if path.is_file():
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(obj, dict):
                    statuses[name] = "unavailable" if obj.get("provenance") == "unavailable" else str(obj.get("status", "present"))
                else:
                    statuses[name] = "present"
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                statuses[name] = "binary-or-invalid"
    counters = counter_state(layout)
    if counters["state"] == "missing":
        missing.append("counters.parquet|counters.unavailable.json")
    statuses["counters"] = counters["state"]
    complete = not missing and counters["state"] == "valid" and all(statuses.get(name) not in {"unavailable", "binary-or-invalid"} for name in ("prediction.json", "eval.json", "summary.json"))
    report = {"schema_version": "cr6.artifact-manifest.v2", "directory": str(layout.directory), "missing": missing, "statuses": statuses, "counter_state": counters, "complete": complete, "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if require_complete and not complete:
        raise ArtifactContractError(f"incomplete attempt artifacts: {report}")
    return report


def inventory(layout: ArtifactLayout) -> dict[str, Any]:
    """Hash present canonical files; raw streams are never modified."""
    names = (*REQUIRED_FILES, *COUNTER_ARTIFACTS, *OPTIONAL_SIDECARS)
    result = {name: {"path": str(layout.path(name)), "sha256": file_sha256(layout.path(name)), "bytes": layout.path(name).stat().st_size} for name in names if layout.path(name).is_file()}
    state = counter_state(layout)
    if state["state"] != "missing":
        result["counters"] = {"logical_state": state["state"], **state}
    return result
