"""Chronological, leakage-safe script state for v2 action records.

The ledger is deliberately a small evidence ledger.  It records hashes of
files that were actually observed before an action and marks the state
invalidated when a mutation cannot be tracked.  It never infers byte/file
counts from an intended path or from a parsed shell command.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import monotonic_ns


_SCRIPT_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_path",
        "sha256",
        "encoding",
        "size_bytes",
        "truncated",
        "hash_basis",
        "byte_exact",
    }
)


def _sha256(path: Path) -> tuple[str | None, int | None]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
    except (OSError, ValueError):
        return None, None
    return digest.hexdigest(), size


@dataclass(frozen=True)
class ScriptState:
    status: str
    generation: int
    paths: tuple[dict[str, Any], ...]
    revisions: Mapping[str, str]
    source_event_id: str | None
    reason: str | None
    observed_at_mono_ns: int | None
    availability: Mapping[str, str]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generation": self.generation,
            "paths": [dict(item) for item in self.paths],
            "revisions": dict(self.revisions),
            "source_event_id": self.source_event_id,
            "reason": self.reason,
            "observed_at_mono_ns": self.observed_at_mono_ns,
            "availability": dict(self.availability),
        }

    as_mapping = to_mapping


class ScriptStateLedger:
    """Track pre-action file revisions in chronological order.

    ``root`` is optional because production hooks often receive already
    observed state from the environment.  ``snapshot`` accepts either paths
    (which it hashes) or explicit ``{"path", "sha256", "size_bytes"}``
    descriptors.  Missing/unreadable files are retained with null evidence.
    """

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else None
        self._generation = 0
        self._state = ScriptState(
            status="unknown",
            generation=0,
            paths=(),
            revisions={},
            source_event_id=None,
            reason="state ledger did not establish a pre-action snapshot",
            observed_at_mono_ns=None,
            availability={"script_state": "unavailable"},
        )
        self._history: list[dict[str, Any]] = []

    @property
    def generation(self) -> int:
        return self._generation

    @staticmethod
    def _normalise_paths(paths: Iterable[str | Path | Mapping[str, Any]]) -> list[str | Mapping[str, Any]]:
        result: list[str | Mapping[str, Any]] = []
        for value in paths:
            if isinstance(value, Mapping):
                result.append(value)
            elif isinstance(value, (str, Path)):
                result.append(str(value))
            else:
                raise TypeError("script paths must be strings, paths, or descriptors")
        return result

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        if self.root is not None and not path.is_absolute():
            path = self.root / path
        return path

    def snapshot(
        self,
        paths: Iterable[str | Path | Mapping[str, Any]],
        *,
        source_event_id: str | None = None,
        observed_at_mono_ns: int | None = None,
    ) -> dict[str, Any]:
        """Record a pre-action snapshot and return the safe state mapping."""

        descriptors: list[dict[str, Any]] = []
        revisions: dict[str, str] = {}
        unavailable = False
        for item in self._normalise_paths(paths):
            if isinstance(item, Mapping):
                allowed = {"path", "sha256", "size_bytes", "content_artifact"}
                if set(item).difference(allowed) or not isinstance(item.get("path"), str):
                    raise ValueError(
                        "script descriptor must contain path, sha256, size_bytes, and optional content_artifact only"
                    )
                path_value = str(item["path"])
                digest = item.get("sha256")
                size = item.get("size_bytes")
                artifact = item.get("content_artifact")
                if digest is not None and (
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
                ):
                    raise ValueError("script descriptor sha256 must be a SHA-256 string or null")
                if size is not None and (
                    isinstance(size, bool) or not isinstance(size, int) or size < 0
                ):
                    raise ValueError("script descriptor size_bytes must be a non-negative integer or null")
                if artifact is not None:
                    self._validate_artifact(artifact)
            else:
                path_value = str(item)
                digest, size = _sha256(self._resolve(path_value))
            descriptor: dict[str, Any] = {"path": path_value, "sha256": digest, "size_bytes": size}
            if isinstance(item, Mapping) and item.get("content_artifact") is not None:
                descriptor["content_artifact"] = dict(item["content_artifact"])
            descriptors.append(descriptor)
            if digest is None:
                unavailable = True
            else:
                revisions[path_value] = digest
        self._generation += 1
        self._state = ScriptState(
            status="unknown" if unavailable else "known",
            generation=self._generation,
            paths=tuple(descriptors),
            revisions=revisions,
            source_event_id=source_event_id,
            reason="one or more script paths were not observable" if unavailable else None,
            observed_at_mono_ns=monotonic_ns() if observed_at_mono_ns is None else observed_at_mono_ns,
            availability={"script_state": "unavailable" if unavailable else "measured"},
        )
        self._history.append({"event": "snapshot", **self._state.to_mapping()})
        return self._state.to_mapping()

    @staticmethod
    def _validate_artifact(value: Any) -> None:
        if not isinstance(value, Mapping) or set(value).difference(_SCRIPT_ARTIFACT_FIELDS):
            raise ValueError("script content_artifact has unsupported fields")
        artifact_path = value.get("artifact_path")
        if artifact_path is not None and (not isinstance(artifact_path, str) or not artifact_path):
            raise ValueError("script content_artifact artifact_path must be text or null")
        digest = value.get("sha256")
        if digest is not None and (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
        ):
            raise ValueError("script content_artifact sha256 must be a SHA-256 string or null")
        encoding = value.get("encoding")
        if encoding is not None and (not isinstance(encoding, str) or not encoding):
            raise ValueError("script content_artifact encoding must be text or null")
        size = value.get("size_bytes")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError("script content_artifact size_bytes must be a non-negative integer or null")
        if not isinstance(value.get("truncated", False), bool):
            raise ValueError("script content_artifact truncated must be boolean")
        hash_basis = value.get("hash_basis")
        if hash_basis is not None and (not isinstance(hash_basis, str) or not hash_basis):
            raise ValueError("script content_artifact hash_basis must be text or null")
        byte_exact = value.get("byte_exact")
        if byte_exact is not None and not isinstance(byte_exact, bool):
            raise ValueError("script content_artifact byte_exact must be boolean or null")

    observe = snapshot
    observe_before_action = snapshot

    def record_edit(
        self,
        paths: Iterable[str | Path | Mapping[str, Any]] = (),
        *,
        tracked: bool = False,
        source_event_id: str | None = None,
        reason: str | None = None,
        observed_at_mono_ns: int | None = None,
    ) -> dict[str, Any]:
        """Record an edit and invalidate prior revisions unless fully tracked.

        A caller may mark an edit as tracked only when it has a complete
        before/after observation.  The default is intentionally conservative.
        """

        if observed_at_mono_ns is not None and (
            isinstance(observed_at_mono_ns, bool)
            or not isinstance(observed_at_mono_ns, int)
            or observed_at_mono_ns < 0
        ):
            raise ValueError("observed_at_mono_ns must be a non-negative integer or null")

        self._generation += 1
        if tracked:
            # ``snapshot`` owns generation advancement.  Calling it here keeps
            # a fully tracked edit at exactly one new chronological revision.
            self._generation -= 1
            return self.snapshot(paths, source_event_id=source_event_id)
        values = [str(item.get("path")) if isinstance(item, Mapping) else str(item) for item in paths]
        self._state = ScriptState(
            status="invalidated",
            generation=self._generation,
            paths=tuple({"path": value, "sha256": None, "size_bytes": None} for value in values),
            revisions={},
            source_event_id=source_event_id,
            reason=reason or "script mutation was observed without a complete tracked revision",
            observed_at_mono_ns=monotonic_ns() if observed_at_mono_ns is None else observed_at_mono_ns,
            availability={"script_state": "unavailable"},
        )
        self._history.append({"event": "edit", **self._state.to_mapping()})
        return self._state.to_mapping()

    edit = record_edit

    def invalidate(
        self,
        reason: str = "script mutation cannot be tracked",
        *,
        source_event_id: str | None = None,
        observed_at_mono_ns: int | None = None,
    ) -> dict[str, Any]:
        return self.record_edit(
            (),
            tracked=False,
            source_event_id=source_event_id,
            reason=reason,
            observed_at_mono_ns=observed_at_mono_ns,
        )

    def current(self) -> dict[str, Any]:
        return self._state.to_mapping()

    state = current

    def history(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._history]


__all__ = ["ScriptState", "ScriptStateLedger"]
