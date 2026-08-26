import csv
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/reconcile_plan.py"
SPEC = importlib.util.spec_from_file_location("assignment_reconcile_plan", SCRIPT)
assert SPEC and SPEC.loader
RECONCILER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILER)
TRAJECTORY_FIELDS = RECONCILER.TRAJECTORY_FIELDS


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def case(suite: str, instance_id: str, cell_id: str, variation=None) -> dict:
    settings = {
        "call_limit": 30,
        "max_output_tokens": 2048,
        "observation_length": 100000,
        "temperature": 0.0,
    }
    if variation is not None:
        settings[variation["knob"]] = variation["value"]
    digest = hashlib.sha256(canonical([suite, instance_id, cell_id]).encode()).hexdigest()
    return {
        "record_type": "case",
        "schema_version": RECONCILER.PLAN_SCHEMA,
        "plan_id": "fixture-plan",
        "steps": [1] if variation is None else [2],
        "roles": ["step_1_baseline"] if variation is None else ["step_2_sweep"],
        "suite": suite,
        "instance_id": instance_id,
        "repository": "owner/repo",
        "task_sha256": "a" * 64,
        "source_manifest_sha256": "b" * 64,
        "cell_id": cell_id,
        "settings": settings,
        "variation": variation,
        "concurrency": 1,
        "per_case_deadline_seconds": 60,
        "resume_key": f"assignment-case-v1:{digest}",
    }


def write_plan(path: Path, cases: list[dict]) -> bytes:
    header = {
        "record_type": "plan",
        "schema_version": RECONCILER.PLAN_SCHEMA,
        "plan_id": "fixture-plan",
        "planning_only": True,
        "concurrency": 1,
        "per_case_deadline_seconds": 60,
        "global_deadline_seconds": 600,
        "execution_case_count": len(cases),
    }
    payload = ("\n".join(canonical(row) for row in [header, *cases]) + "\n").encode()
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    Path(f"{path}.sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return payload


def trajectory(run_id: str, suite: str, instance_id: str, config_id: str, **updates) -> dict[str, object]:
    row: dict[str, object] = {field: "" for field in TRAJECTORY_FIELDS}
    row.update({
        "schema_version": "assignment.trajectory.v1",
        "run_id": run_id,
        "suite": suite,
        "repository": "owner/repo",
        "category": "web",
        "instance_id": instance_id,
        "config_id": config_id,
        "repeat_id": "r0",
        "status": "completed",
        "submitted": "true",
        "official_resolved": "false",
        "e2e_wall_ms": "1000",
        "tool_wall_ms": "200",
        "model_wall_ms": "500",
        "tool_model_ratio": "0.4",
        "tool_event_count": "2",
        "model_event_count": "2",
        "hardware_id": "h100",
        "model_revision": "a" * 40,
        "swe_agent_revision": "b" * 40,
        "swe_bench_revision": "c" * 40,
        "command_sha256": "d" * 64,
        "tool_events_path": "tool_events.jsonl",
        "model_events_path": "model_events.jsonl",
        "provenance": "measured",
    })
    row.update(updates)
    return row


def write_trajectories(path: Path, rows: list[dict[str, object]]) -> bytes:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TRAJECTORY_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path.read_bytes()


def command(root: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--plan", str(root / "plan.jsonl"),
        "--trajectories", str(root / "trajectories.csv"),
        "--remaining-output", str(root / "remaining.jsonl"),
        "--report-output", str(root / "report.json"),
    ]


class ReconcilePlanTests(unittest.TestCase):
    def test_exact_baseline_and_sweep_match_leave_only_unmatched_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan_bytes = write_plan(root / "plan.jsonl", [
                case("lite", "owner__repo-1", "shared-baseline"),
                case(
                    "lite", "owner__repo-1", "temperature=0.2",
                    {"knob": "temperature", "value": 0.2},
                ),
                case("verified", "owner__repo-2", "shared-baseline"),
            ])
            csv_bytes = write_trajectories(root / "trajectories.csv", [
                trajectory("baseline", "lite", "owner__repo-1", "shared-baseline"),
                trajectory(
                    "sweep", "lite", "owner__repo-1", "temperature=0.2",
                    sweep_parameter="temperature", sweep_value="0.2",
                    provenance="derived_from_measured",
                ),
            ])
            result = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "plan.jsonl").read_bytes(), plan_bytes)
            self.assertEqual((root / "trajectories.csv").read_bytes(), csv_bytes)

            remaining = [json.loads(line) for line in (root / "remaining.jsonl").read_text().splitlines()]
            self.assertEqual(remaining[0]["execution_case_count"], 1)
            self.assertEqual(remaining[1]["instance_id"], "owner__repo-2")
            original_sha = hashlib.sha256(plan_bytes).hexdigest()
            self.assertEqual(remaining[0]["reconciliation"]["original_plan_sha256"], original_sha)
            remaining_sha = hashlib.sha256((root / "remaining.jsonl").read_bytes()).hexdigest()
            self.assertEqual(
                (root / "remaining.jsonl.sha256").read_text(),
                f"{remaining_sha}  remaining.jsonl\n",
            )
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["schema_version"], RECONCILER.REPORT_SCHEMA)
            self.assertEqual(report["remaining_plan_sha256"], remaining_sha)
            self.assertEqual([row["run_id"] for row in report["matched"]], ["baseline", "sweep"])
            self.assertEqual(len(report["unmatched"]), 1)
            self.assertEqual(report["rejected_or_ambiguous"], [])

    def test_duplicate_completed_matches_are_ambiguous_and_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_plan(root / "plan.jsonl", [case("lite", "owner__repo-1", "shared-baseline")])
            write_trajectories(root / "trajectories.csv", [
                trajectory("run-a", "lite", "owner__repo-1", "shared-baseline"),
                trajectory("run-b", "lite", "owner__repo-1", "shared-baseline", repeat_id="r1"),
            ])
            result = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["matched"], [])
            self.assertEqual(report["remaining_case_count"], 1)
            rejected = report["rejected_or_ambiguous"]
            self.assertEqual(len(rejected), 1)
            self.assertIn("multiple completed", rejected[0]["reason"])
            self.assertEqual([row["run_id"] for row in rejected[0]["eligible_matches"]], ["run-a", "run-b"])

    def test_missing_sweep_identity_is_a_reported_schema_gap_not_a_guess(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sweep_case = case(
                "lite", "owner__repo-1", "max_output_tokens=512",
                {"knob": "max_output_tokens", "value": 512},
            )
            write_plan(root / "plan.jsonl", [sweep_case])
            write_trajectories(root / "trajectories.csv", [
                trajectory("gap", "lite", "owner__repo-1", "max_output_tokens=512"),
            ])
            result = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["matched"], [])
            self.assertEqual(report["remaining_case_count"], 1)
            self.assertIn("identity conflict", report["rejected_or_ambiguous"][0]["reason"])
            self.assertEqual(report["rejected_or_ambiguous"][0]["conflicts"][0]["run_id"], "gap")

    def test_invalid_plan_sidecar_fails_without_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_plan(root / "plan.jsonl", [case("lite", "owner__repo-1", "shared-baseline")])
            Path(f"{root / 'plan.jsonl'}.sha256").write_text(f"{'0' * 64}  plan.jsonl\n")
            write_trajectories(root / "trajectories.csv", [])
            result = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("sidecar", result.stderr)
            self.assertFalse((root / "remaining.jsonl").exists())
            self.assertFalse((root / "report.json").exists())

    def test_refuses_overwrite_unless_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_plan(root / "plan.jsonl", [case("lite", "owner__repo-1", "shared-baseline")])
            write_trajectories(root / "trajectories.csv", [])
            (root / "report.json").write_text("preserve\n", encoding="utf-8")
            blocked = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(blocked.returncode, 2)
            self.assertEqual((root / "report.json").read_text(), "preserve\n")
            forced = subprocess.run(command(root) + ["--force"], capture_output=True, text=True, check=False)
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            self.assertEqual(json.loads((root / "report.json").read_text())["remaining_case_count"], 1)

    def test_noncanonical_trajectory_header_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_plan(root / "plan.jsonl", [case("lite", "owner__repo-1", "shared-baseline")])
            (root / "trajectories.csv").write_text("run_id,suite,instance_id,config_id\n", encoding="utf-8")
            result = subprocess.run(command(root), capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("canonical trajectory schema", result.stderr)
            self.assertFalse((root / "remaining.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
