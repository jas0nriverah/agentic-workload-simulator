#!/usr/bin/env python3
"""Read-only G0 environment inventory; safe on macOS, Linux, and Lambda."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def command_output(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()[:4000]


def main() -> int:
    result = {
        "cwd": str(Path.cwd()),
        "user": os.environ.get("USER") or os.environ.get("USERNAME"),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "docker_path": shutil.which("docker"),
        "uv_path": shutil.which("uv"),
        "git_path": shutil.which("git"),
        "nvidia_smi_path": shutil.which("nvidia-smi"),
        "nvidia_smi": command_output(["nvidia-smi"]),
        "docker_version": command_output(["docker", "--version"]),
        "disk": shutil.disk_usage(Path.cwd())._asdict(),
    }
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
