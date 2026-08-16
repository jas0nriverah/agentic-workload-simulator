#!/usr/bin/env python3
"""Write an assumption-explicit, estimated model-memory preflight report."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.observability.memory import estimate_memory  # noqa: E402


def _write(path: Path, value: dict, *, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"refusing to overwrite memory estimate: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-revision")
    parser.add_argument("--precision", default="bf16")
    parser.add_argument("--precision-bytes", type=float)
    parser.add_argument("--parameter-count", type=float)
    parser.add_argument("--weight-bytes", type=float)
    parser.add_argument("--context-length", type=int)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--num-layers", type=int)
    parser.add_argument("--num-kv-heads", type=int)
    parser.add_argument("--head-dim", type=float)
    parser.add_argument("--kv-bytes-per-element", type=float)
    parser.add_argument("--runtime-margin-fraction", type=float, default=0.20)
    parser.add_argument("--exclude-kv-cache", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        print(f"DRY-RUN: estimate weights + KV cache + runtime margin for revision={args.model_revision or '<provided at runtime>'}; write estimated report to {args.output}")
        print("DRY-RUN: actual H100/vLLM fit remains authoritative; no model download or GPU allocation")
        return 0
    report = estimate_memory(
        model_revision=args.model_revision,
        precision=args.precision,
        precision_bytes=args.precision_bytes,
        parameter_count=args.parameter_count,
        weight_bytes=args.weight_bytes,
        context_length=args.context_length,
        batch_size=args.batch_size,
        num_layers=args.num_layers,
        num_kv_heads=args.num_kv_heads,
        head_dim=args.head_dim,
        kv_bytes_per_element=args.kv_bytes_per_element,
        runtime_margin_fraction=args.runtime_margin_fraction,
        include_kv_cache=not args.exclude_kv_cache,
    )
    _write(args.output, report, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
