#!/usr/bin/env python3
"""Compile active Python sources without writing bytecode; import the library."""
import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    paths = sorted(path for folder in ("src", "scripts", "tests")
                   for path in (ROOT / folder).rglob("*.py"))
    for path in paths:
        compile(path.read_bytes(), str(path), "exec")
    sys.path.insert(0, str(ROOT / "src"))
    modules = []
    for path in sorted((ROOT / "src/agentic_sim").rglob("*.py")):
        parts = list(path.relative_to(ROOT / "src").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        name = ".".join(parts)
        importlib.import_module(name)
        modules.append(name)
    print(f"Compiled {len(paths)} Python files; imported {len(modules)} library modules")


if __name__ == "__main__":
    main()
