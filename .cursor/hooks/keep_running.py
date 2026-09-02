#!/usr/bin/env python3
"""Continue the active six-H100 collection until its final audit passes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


EXPERIMENT_ROOT = Path(
    os.environ.get(
        "H100_EXPERIMENT_ROOT",
        "/mnt/eic-work/assignment/cpu-docker-canary-20260830-v3/results-full",
    )
)


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def is_complete() -> bool:
    final = read_json(EXPERIMENT_ROOT / "final_status.json")
    return bool(
        final
        and final.get("status") == "complete"
        and final.get("validated") is True
        and final.get("ready_for_analysis") is True
    )


def main() -> int:
    # Stop hooks must fail open when the experiment is absent or the hook
    # cannot inspect it; they should never block unrelated project sessions.
    if is_complete():
        print(json.dumps({}))
    else:
        print(
            json.dumps(
                {
                    "followup_message": (
                        "The six-H100 experiment is incomplete. Continue from its CURRENT "
                        "state at /mnt/eic-work/assignment/cpu-docker-canary-20260830-v3/results-full: "
                        "inspect worker state and processes, preserve valid completed artifacts, "
                        "recover only missing or failed work, and keep monitoring until final "
                        "validation writes final_status.json with status=complete, validated=true, "
                        "and ready_for_analysis=true. Do not restart healthy workers."
                    )
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
