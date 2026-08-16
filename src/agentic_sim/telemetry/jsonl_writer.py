"""Append-only JSONL writer with flush/fsync semantics."""

from __future__ import annotations

import json
import os
import fcntl
from pathlib import Path
from typing import Any, Mapping

from agentic_sim.artifacts.json import redact_secrets


class AppendOnlyJSONLWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Mapping[str, Any]) -> None:
        encoded = (json.dumps(redact_secrets(dict(record)), sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    write = append
