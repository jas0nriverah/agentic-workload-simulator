"""Resolve official SWE-bench labels from completed case artifacts.

Compile used to read only ``runner_attempts/attempt-*/evaluator_result.json``.
On the H100 matrix that file is missing for most unique-accepted originals; the
real report lives at case-root ``evaluator_result.json``.  This helper walks a
fixed fallback and records which file supplied each label.

Lookup order (first usable source wins):

1. latest-attempt ``evaluator_result.json``
2. case-root ``evaluator_result.json``
3. ``case_result.json["evaluator"]``

Raw artifacts are never modified.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SOURCE_ATTEMPT = "attempt_evaluator_result"
SOURCE_CASE_ROOT = "case_root_evaluator_result"
SOURCE_CASE_RESULT = "case_result_evaluator"
SOURCE_MISSING = "missing"


@dataclass(frozen=True)
class OfficialEval:
    submitted: bool
    official_resolved: bool
    source_kind: str
    source_path: str | None
    source_sha256: str | None
    note: str

    def to_mapping(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_attempt_dir(case_dir: Path, result: Mapping[str, Any]) -> Path | None:
    runner = result.get("runner") if isinstance(result.get("runner"), dict) else {}
    output_dir = runner.get("output_dir")
    if isinstance(output_dir, str) and output_dir:
        path = case_dir / output_dir
        if path.is_dir():
            return path
    attempts = sorted((case_dir / "runner_attempts").glob("attempt-*"), reverse=True)
    return attempts[0] if attempts else None


def _payload_from_eval_object(value: Any) -> dict[str, bool] | None:
    if not isinstance(value, dict):
        return None
    resolved = value.get("official_resolved")
    if not isinstance(resolved, bool):
        return None
    submitted = value.get("submitted")
    if not isinstance(submitted, bool):
        submitted = False
    return {"submitted": submitted, "official_resolved": resolved}


def _from_eval_file(path: Path, source_kind: str) -> OfficialEval | None:
    if not path.is_file() or path.is_symlink():
        return None
    payload = _payload_from_eval_object(_load_json(path))
    if payload is None:
        return None
    return OfficialEval(
        submitted=payload["submitted"],
        official_resolved=payload["official_resolved"],
        source_kind=source_kind,
        source_path=str(path),
        source_sha256=_sha256_file(path),
        note=source_kind,
    )


def _missing(note: str, path: Path | None = None) -> OfficialEval:
    return OfficialEval(
        submitted=False,
        official_resolved=False,
        source_kind=SOURCE_MISSING,
        source_path=str(path) if path is not None else None,
        source_sha256=None,
        note=note,
    )


def resolve_official_eval(case_result_path: Path) -> OfficialEval:
    """Load submitted / official_resolved with the documented fallback."""

    path = Path(case_result_path)
    result = _load_json(path)
    if not isinstance(result, dict):
        return _missing("unreadable_case_result", path)
    case_dir = path.parent

    attempt = latest_attempt_dir(case_dir, result)
    if attempt is not None:
        loaded = _from_eval_file(attempt / "evaluator_result.json", SOURCE_ATTEMPT)
        if loaded is not None:
            return loaded

    loaded = _from_eval_file(case_dir / "evaluator_result.json", SOURCE_CASE_ROOT)
    if loaded is not None:
        return loaded

    payload = _payload_from_eval_object(result.get("evaluator"))
    if payload is not None:
        return OfficialEval(
            submitted=payload["submitted"],
            official_resolved=payload["official_resolved"],
            source_kind=SOURCE_CASE_RESULT,
            source_path=str(path),
            source_sha256=_sha256_file(path),
            note=SOURCE_CASE_RESULT,
        )
    return _missing("no_usable_evaluator_source", path)
