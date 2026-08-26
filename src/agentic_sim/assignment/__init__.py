"""Assignment-native SWE-bench data contracts and aggregation helpers."""

from .schema import (
    MODEL_EVENT_FIELDS,
    TOOL_EVENT_FIELDS,
    TRAJECTORY_FIELDS,
    AssignmentContractError,
    canonical_sha256,
    validate_model_event,
    validate_tool_event,
    validate_trajectory,
)

__all__ = [
    "MODEL_EVENT_FIELDS",
    "TOOL_EVENT_FIELDS",
    "TRAJECTORY_FIELDS",
    "AssignmentContractError",
    "canonical_sha256",
    "validate_model_event",
    "validate_tool_event",
    "validate_trajectory",
]
