"""Focused provenance checks for the offline D9 prediction adapter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts/assignment/generate_d9_predicted_figures.py"
SPEC = importlib.util.spec_from_file_location("generate_d9_predicted_figures", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
D9 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = D9
SPEC.loader.exec_module(D9)


class RecoveredTrajectoryBindingTests(unittest.TestCase):
    def test_same_count_wrong_attempt_is_rejected_by_action_hash(self) -> None:
        run_id = "assignment-case-v1:test-recovery"
        instance_id = "django__django-test"
        action = "echo right"
        wrong_action = "echo wrong"  # Same UTF-8 byte count and execution time.
        event_id = f"{run_id}-tool-0000"
        expected = {
            event_id: {
                "run_id": run_id,
                "status": "completed",
                "command_sha256": hashlib.sha256(action.encode("utf-8")).hexdigest(),
                "command_bytes": str(len(action.encode("utf-8"))),
                "wall_ms": "100.0",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            trajectory = Path(directory) / instance_id / f"{instance_id}.traj"
            trajectory.parent.mkdir()
            trajectory.write_text(
                json.dumps(
                    {
                        "environment": instance_id,
                        "trajectory": [{"action": wrong_action, "execution_time": 0.1}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(D9.D9PredictionError, "action hash mismatch"):
                D9._load_recovered_trajectory_actions(
                    trajectory,
                    run_id=run_id,
                    instance_id=instance_id,
                    expected_event_rows=expected,
                )

    def test_recovered_action_requires_protocol_wall_time(self) -> None:
        run_id = "assignment-case-v1:test-recovery-time"
        instance_id = "django__django-test-time"
        action = "echo right"
        event_id = f"{run_id}-tool-0000"
        expected = {
            event_id: {
                "run_id": run_id,
                "status": "completed",
                "command_sha256": hashlib.sha256(action.encode("utf-8")).hexdigest(),
                "command_bytes": str(len(action.encode("utf-8"))),
                "wall_ms": "100.0",
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            trajectory = Path(directory) / instance_id / f"{instance_id}.traj"
            trajectory.parent.mkdir()
            trajectory.write_text(
                json.dumps(
                    {
                        "environment": instance_id,
                        "trajectory": [{"action": action, "execution_time": 0.2}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(D9.D9PredictionError, "execution_time.*wall_ms"):
                D9._load_recovered_trajectory_actions(
                    trajectory,
                    run_id=run_id,
                    instance_id=instance_id,
                    expected_event_rows=expected,
                )


if __name__ == "__main__":
    unittest.main()
