#!/usr/bin/env python3
"""Write the minimal SWE-agent model request configuration fragment.

SWE-agent v1.1.0 accepts ``completion_kwargs`` in YAML, but its generated CLI
does not accept nested ``--agent.model.completion_kwargs.*`` options. Keeping
this writer tiny and JSON-compatible makes it usable before the paid runtime
environment is fully imported; JSON is valid YAML and requires no extra host
dependency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence


def write_config(path: Path, *, max_tokens: int, seed: int) -> str:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive")
    if seed < 0:
        raise ValueError("seed must be non-negative")
    document = {
        "agent": {
            "model": {
                "completion_kwargs": {"max_tokens": max_tokens, "seed": seed}
            }
        }
    }
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise ValueError(f"refusing to overwrite immutable request config: {path}")
    if not path.exists():
        path.write_text(encoded, encoding="utf-8")
    return encoded


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args(argv)
    write_config(args.output, max_tokens=args.max_tokens, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
