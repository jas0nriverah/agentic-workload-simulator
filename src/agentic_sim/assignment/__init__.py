"""Assignment-native SWE-bench data contracts and aggregation helpers."""

from .tool_features import ToolActionFeatures, extract_tool_features, extractor_source_sha256
from .official_eval import OfficialEval, resolve_official_eval
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
    "OfficialEval",
    "canonical_sha256",
    "resolve_official_eval",
    "validate_model_event",
    "validate_tool_event",
    "validate_trajectory",
    "ToolActionFeatures",
    "extract_tool_features",
    "extractor_source_sha256",
]
