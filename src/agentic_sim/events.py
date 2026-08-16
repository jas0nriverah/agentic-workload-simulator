"""Minimal lossless event envelope used before the real telemetry schema freezes."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional
import json


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: str
    event_id: str
    run_id: str
    event_type: str
    start_time_ns: int
    end_time_ns: int
    provenance: str
    instance_id: Optional[str] = None
    step_id: Optional[int] = None
    request_id: Optional[str] = None
    action_id: Optional[str] = None
    source: str = "unknown"
    payload: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.end_time_ns < self.start_time_ns:
            raise ValueError("end_time_ns must be >= start_time_ns")
        if self.provenance not in {"measured", "derived", "simulated", "unavailable", "dev"}:
            raise ValueError(f"unsupported provenance: {self.provenance}")

    @property
    def duration_ms(self) -> float:
        return (self.end_time_ns - self.start_time_ns) / 1_000_000.0

    def to_json(self) -> str:
        value = asdict(self)
        value["duration_ms"] = self.duration_ms
        return json.dumps(value, sort_keys=True)
