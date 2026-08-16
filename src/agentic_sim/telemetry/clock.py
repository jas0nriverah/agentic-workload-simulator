"""Shared monotonic clock identity and timestamps for runtime artifacts.

Linux exposes ``CLOCK_MONOTONIC_RAW`` for measurements that should not be
slewed by NTP.  Some supported environments do not expose it, so selection is
explicit and recorded with every metadata-bearing artifact.  The fallback is
``CLOCK_MONOTONIC``; it is never silently represented as RAW.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import platform
import socket
import time
from functools import lru_cache
from typing import Any, Mapping


def _select_clock() -> tuple[int, str]:
    raw_id = getattr(time, "CLOCK_MONOTONIC_RAW", None)
    if raw_id is not None:
        try:
            time.clock_gettime_ns(raw_id)
        except (AttributeError, OSError, OverflowError, ValueError):
            pass
        else:
            return raw_id, "CLOCK_MONOTONIC_RAW"
    fallback_id = getattr(time, "CLOCK_MONOTONIC", None)
    if fallback_id is None:
        raise RuntimeError("the platform exposes no monotonic clock")
    try:
        time.clock_gettime_ns(fallback_id)
    except (AttributeError, OSError, OverflowError, ValueError) as exc:
        raise RuntimeError("CLOCK_MONOTONIC is unavailable") from exc
    return fallback_id, "CLOCK_MONOTONIC"


_CLOCK_ID, _CLOCK_NAME = _select_clock()


def monotonic_ns() -> int:
    """Return nanoseconds from the selected monotonic clock."""

    return time.clock_gettime_ns(_CLOCK_ID)


def clock_id() -> str:
    """Return the exact clock identifier used by :func:`monotonic_ns`."""

    return _CLOCK_NAME


def utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _boot_id() -> tuple[str | None, str]:
    path = "/proc/sys/kernel/random/boot_id"
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
    except (OSError, UnicodeError):
        value = ""
    if value:
        return value, path
    return None, "unavailable"


@lru_cache(maxsize=1)
def clock_metadata() -> dict[str, Any]:
    """Return stable host/boot identity and the selected clock metadata."""

    boot, boot_source = _boot_id()
    try:
        resolution_ns = int(time.clock_getres(_CLOCK_ID) * 1_000_000_000)
    except (AttributeError, OSError, OverflowError, ValueError):
        resolution_ns = None
    return {
        "clock_id": _CLOCK_NAME,
        "clock_source": "time.clock_gettime_ns",
        "clock_resolution_ns": resolution_ns,
        "hostname": socket.gethostname(),
        "boot_id": boot,
        "boot_id_source": boot_source,
        "platform": platform.system(),
    }


def clock_fields() -> Mapping[str, Any]:
    """Return a shallow copy suitable for embedding in a JSON row."""

    return dict(clock_metadata())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help="include one selected-clock sample")
    args = parser.parse_args(argv)
    value = {"schema_version": "telemetry.clock.v1", **clock_fields()}
    if args.sample:
        value["monotonic_ns"] = monotonic_ns()
        value["captured_at_utc"] = utc_now()
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["clock_fields", "clock_id", "clock_metadata", "main", "monotonic_ns", "utc_now"]
