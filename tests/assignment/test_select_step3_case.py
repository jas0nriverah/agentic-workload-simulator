import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/select_step3_case.py"


FIELDS = (
    "run_id", "suite", "repository", "category", "instance_id", "config_id",
    "repeat_id", "status", "submitted", "official_resolved", "e2e_wall_ms",
    "tool_wall_ms", "model_wall_ms", "tool_model_ratio", "tool_event_count",
    "model_event_count", "hardware_id", "model_revision", "swe_agent_revision",
    "swe_bench_revision", "command_sha256", "provenance",
)


def row(run_id: str, suite: str, instance_id: str, tool: float, model: float) -> dict[str, object]:
    return {
        "run_id": run_id, "suite": suite, "repository": "owner/repo",
        "category": "web", "instance_id": instance_id, "config_id": "shared-baseline",
        "repeat_id": "r0", "status": "completed", "submitted": "true",
        "official_resolved": "false", "e2e_wall_ms": tool + model + 10,
        "tool_wall_ms": tool, "model_wall_ms": model, "tool_model_ratio": tool / model,
        "tool_event_count": 2, "model_event_count": 2, "hardware_id": "h100",
        "model_revision": "a" * 40, "swe_agent_revision": "b" * 40,
        "swe_bench_revision": "c" * 40, "command_sha256": "d" * 64,
        "provenance": "measured",
    }


class Step3SelectionTests(unittest.TestCase):
    def _write(self, path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_selects_highest_ratio_with_deterministic_tie_breaker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "trajectories.csv"
            self._write(source, [
                row("z", "verified", "z", 200, 100),
                row("b", "lite", "b", 200, 100),
                row("a", "lite", "a", 100, 100),
            ])
            output = root / "selection.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--trajectories", str(source), "--output", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            value = json.loads(output.read_text())
            self.assertEqual(value["selected"]["run_id"], "b")
            self.assertEqual(value["eligible_count"], 3)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual((root / "selection.json.sha256").read_text(), f"{digest}  selection.json\n")

    def test_rejects_inconsistent_ratio_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "trajectories.csv"
            invalid = row("a", "lite", "a", 100, 100)
            invalid["tool_model_ratio"] = 9
            self._write(source, [invalid])
            output = root / "selection.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--trajectories", str(source), "--output", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("ratio does not equal", result.stderr)
            self.assertFalse(output.exists())

    def test_skips_sweep_and_incomplete_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "trajectories.csv"
            sweep = row("sweep", "lite", "a", 900, 100)
            sweep["config_id"] = "temperature=0.8"
            failed = row("failed", "lite", "b", 800, 100)
            failed["status"] = "failed"
            baseline = row("base", "verified", "c", 100, 100)
            self._write(source, [sweep, failed, baseline])
            output = root / "selection.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--trajectories", str(source), "--output", str(output)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(output.read_text())["selected"]["run_id"], "base")


if __name__ == "__main__":
    unittest.main()
