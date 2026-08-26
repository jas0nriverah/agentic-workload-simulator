from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.assignment import shard_plan
from tests.assignment.test_run_matrix import reviewed_runner_args, write_plan


class ShardPlanTests(unittest.TestCase):
    def test_shards_are_deterministic_disjoint_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _config, _config_sha256 = write_plan(root, count=5)
            first_manifest_path = root / "first" / "shards.json"
            first = shard_plan.shard(
                parent,
                parent_sidecar=Path(str(parent) + ".sha256"),
                shard_count=2,
                output_dir=root / "first",
                manifest_path=first_manifest_path,
            )
            second_manifest_path = root / "second" / "shards.json"
            second = shard_plan.shard(
                parent,
                parent_sidecar=Path(str(parent) + ".sha256"),
                shard_count=2,
                output_dir=root / "second",
                manifest_path=second_manifest_path,
            )

            self.assertEqual(first["parent_plan_sha256"], second["parent_plan_sha256"])
            self.assertEqual(first["coverage_sha256"], second["coverage_sha256"])
            first_keys: list[str] = []
            for entry in first["shards"]:
                path = first_manifest_path.parent / entry["path"]
                payload = path.read_bytes()
                self.assertEqual(
                    entry["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )
                rows = [json.loads(line) for line in payload.decode().splitlines()]
                keys = [row["resume_key"] for row in rows[1:]]
                first_keys.extend(keys)
                self.assertEqual(entry["resume_keys_sha256"], shard_plan._coverage_sha256(keys))
            self.assertEqual(len(first_keys), 5)
            self.assertEqual(len(first_keys), len(set(first_keys)))
            self.assertEqual(first["coverage_sha256"], shard_plan._coverage_sha256(first_keys))

            for first_entry, second_entry in zip(first["shards"], second["shards"]):
                first_payload = (first_manifest_path.parent / first_entry["path"]).read_bytes()
                second_payload = (second_manifest_path.parent / second_entry["path"]).read_bytes()
                self.assertEqual(first_payload, second_payload)

    def test_tampered_parent_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, _config, _config_sha256 = write_plan(root, count=3)
            shard_plan.shard(
                parent,
                parent_sidecar=Path(str(parent) + ".sha256"),
                shard_count=2,
                output_dir=root / "shards",
                manifest_path=root / "shards.json",
            )
            parent.write_bytes(parent.read_bytes() + b" ")
            with self.assertRaisesRegex(shard_plan.ShardError, "sidecar"):
                shard_plan.shard(
                    parent,
                    parent_sidecar=Path(str(parent) + ".sha256"),
                    shard_count=2,
                    output_dir=root / "other-shards",
                    manifest_path=root / "other-shards.json",
                )

    def test_run_matrix_requires_and_accepts_coverage_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, config, config_sha256 = write_plan(root, count=3)
            shards_path = root / "shards"
            manifest_path = root / "shards.json"
            shard_plan.shard(
                parent,
                parent_sidecar=Path(str(parent) + ".sha256"),
                shard_count=2,
                output_dir=shards_path,
                manifest_path=manifest_path,
            )
            shard_path = shards_path / "shard-000-of-002.jsonl"
            args = reviewed_runner_args(
                shard_path,
                config,
                config_sha256,
                root / "validation",
            )
            missing = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(missing.returncode, 1)
            self.assertIn("shards-manifest", missing.stderr)

            accepted = subprocess.run(
                args + ["--shards-manifest", str(manifest_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            validation = json.loads(accepted.stdout)
            self.assertEqual(validation["case_count"], 2)
            self.assertEqual(validation["shards_manifest_sha256"], hashlib.sha256(manifest_path.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
