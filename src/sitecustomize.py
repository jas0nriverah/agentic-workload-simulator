"""Opt-in site hook for the assignment's reviewed v2 SWE-agent run."""

import os
import sys

from agentic_sim.telemetry.autoinstrument import install_from_env


if os.environ.get("ASSIGNMENT_TELEMETRY_V2_SUPERVISOR") != "1":
    try:
        install_from_env()
    except BaseException as exc:
        # CPython's site loader reports ordinary sitecustomize exceptions and
        # continues.  A requested v2 workload must stop before importing the
        # agent if activation cannot be installed; the parent runner
        # separately verifies the durable handshake marker after launch.
        if os.environ.get("ASSIGNMENT_TELEMETRY_V2_AUTO") == "1":
            print(
                f"fatal: required v2 SWE-agent instrumentation unavailable: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(78) from None
        raise
