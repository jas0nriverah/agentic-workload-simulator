#!/usr/bin/env python3
"""Create deterministic, disjoint worker files for a parallel SWE-agent batch.

Planning is local and side-effect limited: it reads the pinned dataset, writes
JSON shard files plus a manifest, and never starts vLLM, SWE-agent, Docker, or
an evaluator.  Each worker can then be launched in a separate one-GPU host.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.parallel import (  # noqa: E402
    ParallelBatchError,
    completed_instance_ids,
    file_sha256,
    load_instance_rows,
    plan_shards,
    write_batch_plan,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--dataset", required=True, type=Path, help="Pinned full Lite/Verified JSON, JSONL, or Parquet dataset")
    result.add_argument("--output-root", required=True, type=Path, help="Portable batch directory for manifests and worker files")
    result.add_argument("--batch-id", required=True, help="Safe immutable batch identifier")
    result.add_argument("--experiment-id", required=True, help="Experiment identifier recorded in every worker manifest")
    result.add_argument("--dataset-name", required=True, choices=("lite", "verified"))
    result.add_argument("--shards", required=True, type=int, help="Number of one-GPU workers")
    result.add_argument("--resume", action="store_true", help="Skip only final successful attempts already under --work-root")
    result.add_argument("--work-root", type=Path, help="Existing work root scanned by --resume")
    result.add_argument("--allow-empty-shards", action="store_true", help="Permit more workers than pending rows")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        rows = load_instance_rows(args.dataset)
        source_sha = file_sha256(args.dataset)
        completed: set[str] = set()
        if args.resume:
            if args.work_root is None:
                raise ParallelBatchError("--resume requires --work-root")
            completed = completed_instance_ids(args.work_root, experiment_id=args.experiment_id, dataset=args.dataset_name)
        plans = plan_shards(
            rows,
            batch_id=args.batch_id,
            experiment_id=args.experiment_id,
            dataset=args.dataset_name,
            source_dataset=str(args.dataset),
            source_sha256=source_sha,
            shard_count=args.shards,
            completed_instance_ids=completed,
        )
        if not args.allow_empty_shards and any(not plan.instance_ids for plan in plans):
            raise ParallelBatchError(
                "pending rows are fewer than --shards; use fewer one-GPU workers or pass --allow-empty-shards"
            )
        manifest = write_batch_plan(plans, rows, output_root=args.output_root)
        pending = sum(len(plan.instance_ids) for plan in plans)
        print(f"parallel batch plan: PASS ({manifest})")
        print(f"source_rows={len(rows)} pending_rows={pending} completed_rows={len(completed)} shards={len(plans)}")
        for plan in plans:
            print(f"{plan.worker_name}: {len(plan.instance_ids)} instances")
        return 0
    except ParallelBatchError as exc:
        print(f"parallel batch plan: BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
