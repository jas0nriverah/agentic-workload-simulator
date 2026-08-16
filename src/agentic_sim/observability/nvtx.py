"""Optional NVTX annotations with a dependency-free no-op fallback.

NVTX is deliberately opt-in.  Normal control and thin runs do not import or
require the package, and an unavailable binding is represented as a no-op.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator, Optional


def _enabled() -> bool:
    return os.environ.get("AGENTIC_ENABLE_NVTX", "").strip().lower() in {"1", "true", "yes", "on"}


def capability() -> dict[str, object]:
    """Return the runtime capability without changing process state."""

    if not _enabled():
        return {"enabled": False, "available": False, "status": "disabled", "provenance": "unavailable"}
    try:
        import nvtx  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on host package
        return {"enabled": True, "available": False, "status": "unavailable", "error": type(exc).__name__, "provenance": "unavailable"}
    return {"enabled": True, "available": True, "status": "available", "module": getattr(nvtx, "__file__", None), "provenance": "derived"}


@contextlib.contextmanager
def range(name: str, *, category: Optional[str] = None) -> Iterator[None]:
    """Annotate a logical range when explicitly enabled; otherwise no-op."""

    if not isinstance(name, str) or not name.strip():
        raise ValueError("NVTX range name must be non-empty")
    if not _enabled():
        yield
        return
    try:
        import nvtx  # type: ignore
    except Exception:
        yield
        return
    kwargs = {"message": name}
    if category:
        kwargs["domain"] = category
    with nvtx.annotate(**kwargs):
        yield


def annotate(name: str, *, category: Optional[str] = None) -> None:
    """Emit an instantaneous annotation if the optional binding is active."""

    if not _enabled():
        return
    try:
        import nvtx  # type: ignore
        nvtx.mark(message=name, domain=category) if category else nvtx.mark(message=name)
    except Exception:
        return


__all__ = ["annotate", "capability", "range"]
