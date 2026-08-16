"""Small configuration helpers used by local and cloud smoke checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Union


def load_mapping(path: Union[str, Path]) -> Dict[str, Any]:
    """Load a simple JSON config before Linux dependency resolution."""
    config_path = Path(path)
    if config_path.suffix.lower() != ".json":
        raise ValueError("Initial bootstrap configs must be JSON")
    value = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Configuration root must be an object")
    return value


def config_hash(value: Dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
