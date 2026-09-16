#!/usr/bin/env python3
"""Validate the D9 next-sample roster against frozen split metadata.

This intentionally reads only the split manifest's cluster metadata.  It does
not open case artifacts, labels, ledgers, queues, or raw CPU/GPU evidence, and
it never launches an acquisition command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


DEFAULT_SPLIT = Path(
    "/home/riverahernandezjason/h100-assignment-work-20260905/assignment/"
    "submission/20260908T140000Z-offline-v2/live-plan/"
    "production_split_manifest.v2.json"
)


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object at {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=here / "sample_plan.json")
    parser.add_argument("--split-manifest", type=Path, default=DEFAULT_SPLIT)
    args = parser.parse_args()

    plan = load_json(args.plan)
    split = load_json(args.split_manifest)
    clusters = {
        row["instance_id"]: row
        for row in split.get("clusters", [])
        if isinstance(row, dict) and "instance_id" in row
    }
    errors: list[str] = []

    expected_split_sha = plan["protected_partitions"]["split_manifest_sha256"]
    actual_split_sha = sha256_file(args.split_manifest)
    if actual_split_sha != expected_split_sha:
        errors.append(
            "pinned split manifest SHA mismatch: "
            f"plan={expected_split_sha} actual={actual_split_sha}"
        )
    provenance_hashes = {
        row.get("sha256")
        for row in plan.get("source_provenance", [])
        if row.get("path", "").endswith("production_split_manifest.v2.json")
    }
    if expected_split_sha not in provenance_hashes:
        errors.append("split manifest SHA is missing from source_provenance")

    if plan.get("scope", {}).get("no_runs_launched") is not True:
        errors.append("plan must declare no_runs_launched=true")
    if split.get("outcomes_accessed") is not False:
        errors.append("split manifest does not declare outcomes_accessed=false")

    expected: dict[str, str] = {}
    for instance_id in plan["instance_roster"]["current_train_instance_ids"]:
        expected[instance_id] = "train_calibration"
    for row in plan["instance_roster"]["targeted_train_ids"]:
        expected[row["instance_id"]] = "train_calibration"
    for instance_id in plan["instance_roster"]["metadata_only_fallback_ids"]:
        expected[instance_id] = "train_calibration"
    for instance_id in plan["sample_plan"]["untouched_validation"]["existing_metadata_only_instances"]:
        expected[instance_id] = "final_evaluation"
    expected[plan["protected_partitions"]["sealed_holdout"]["instance_id"]] = "sealed_holdout"

    for instance_id, partition in sorted(expected.items()):
        row = clusters.get(instance_id)
        if row is None:
            errors.append(f"missing split metadata for {instance_id}")
            continue
        if row.get("partition") != partition:
            errors.append(
                f"{instance_id}: expected {partition}, found {row.get('partition')}"
            )
        if row.get("outcome_accessed") is not False:
            errors.append(f"{instance_id}: protected/outcome metadata is not false")

    expected_counts = plan["protected_partitions"]
    split_counts = split.get("partition_counts", {})
    for partition in (
        "train_calibration",
        "final_evaluation",
        "confirmation_development_excluded",
        "sealed_holdout",
    ):
        expected_partition = expected_counts[partition]
        actual_partition = split_counts.get(partition, {})
        for key in ("clusters", "cases"):
            if expected_partition[key] != actual_partition.get(f"{key[:-1]}_count", actual_partition.get(key)):
                errors.append(
                    f"{partition}.{key}: plan={expected_partition[key]} "
                    f"split={actual_partition.get(f'{key[:-1]}_count', actual_partition.get(key))}"
                )

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "ok",
                "checked_instance_references": len(expected),
                "train_references": sum(p == "train_calibration" for p in expected.values()),
                "protected_final_references": sum(p == "final_evaluation" for p in expected.values()),
                "sealed_holdout_reference": plan["protected_partitions"]["sealed_holdout"]["instance_id"],
                "split_manifest_sha256": actual_split_sha,
                "outcomes_opened": False,
                "runs_launched": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
