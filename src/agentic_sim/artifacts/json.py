"""Small file primitives used by the runner.

The raw event and call streams are intentionally never rewritten.  JSON
summaries are written atomically, while a completed attempt is never reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import fcntl
from pathlib import Path
from typing import Any, Mapping


_SECRET_KEY = re.compile(r"(?:authorization|api[_-]?key|access[_-]?token|secret|password|token)", re.I)
_REDACTED = "[REDACTED]"


def redact_secrets(value: Any, *, key: str = "") -> Any:
    """Return a copy with credentials removed from telemetry/config metadata."""
    if key and _SECRET_KEY.search(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact_secrets(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def append_jsonl(path: str | Path, record: Mapping[str, Any]) -> None:
    """Append one flushed JSON record without truncating an existing stream."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(redact_secrets(dict(record)), sort_keys=True, separators=(",", ":")) + "\n"
    # O_APPEND protects against two local writers interleaving at seek time.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            if hasattr(os, "writev"):
                os.writev(fd, [payload.encode("utf-8")])
            else:  # pragma: no cover - Python implementations without writev
                os.write(fd, payload.encode("utf-8"))
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def atomic_json_dump(path: str | Path, value: Mapping[str, Any]) -> None:
    """Write metadata atomically; callers enforce attempt immutability."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(redact_secrets(dict(value)), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, target)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
