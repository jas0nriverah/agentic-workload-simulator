"""Small CLI for G0; larger commands are added only when their gates are ready."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
from typing import List, Optional

from . import __version__


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-sim")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(json.dumps({
            "python": sys.version,
            "platform": platform.platform(),
            "docker": shutil.which("docker"),
            "nvidia_smi": shutil.which("nvidia-smi"),
        }, indent=2, sort_keys=True))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
