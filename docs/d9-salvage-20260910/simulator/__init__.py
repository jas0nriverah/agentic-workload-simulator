"""Runnable, offline-only D9 simulator and strict score helpers.

The package deliberately keeps the retained historical proxy targets separate
from native/lifecycle targets that do not yet have an independently calibrated
model.  It is imported by ``run.py`` and can also be loaded directly from the
salvage directory in a review checkout.
"""

from .hardware_profile import (
    HARDWARE_SCHEMA,
    HardwareProfile,
    HardwareProfileError,
    default_hardware_profile,
    hardware_profile_sha256,
)
from .strict_metrics import MetricsError, score_bundle, score_rows
from .d9_simulator import (
    ASSIGNMENT_TARGETS,
    D9Simulator,
    PredictionContractError,
    predict_request,
)

__all__ = [
    "D9Simulator",
    "ASSIGNMENT_TARGETS",
    "HARDWARE_SCHEMA",
    "HardwareProfile",
    "HardwareProfileError",
    "MetricsError",
    "PredictionContractError",
    "default_hardware_profile",
    "hardware_profile_sha256",
    "predict_request",
    "score_bundle",
    "score_rows",
]
