import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/plan_matrix.py"
CONFIG = ROOT / "configs/assignment_steps_1_3.json"
SPEC = importlib.util.spec_from_file_location("assignment_plan_matrix", SCRIPT)
assert SPEC and SPEC.loader
PLANNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLANNER)


def task_rows(suite: str, count: int = 12) -> list[dict[str, str]]:
    return [
        {
            "instance_id": f"{suite}_repo_{index % 3}__case-{index:03d}",
            "repo": f"{suite}_repo_{index % 3}",
            "problem_statement": f"deterministic fixture {index}",
            "suite": suite,
        }
        for index in range(count)
    ]


def write_jsonl(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class AssignmentMatrixPlannerTests(unittest.TestCase):
    def test_plan_is_deterministic_and_sidecar_matches_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lite = root / "lite.jsonl"
            verified = root / "verified.jsonl"
            write_jsonl(lite, list(reversed(task_rows("lite"))))
            write_jsonl(verified, task_rows("verified"))
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            command = [
                sys.executable,
                str(SCRIPT),
                "--config", str(CONFIG),
                "--lite-tasks", str(lite),
                "--verified-tasks", str(verified),
            ]
            one = subprocess.run(command + ["--output", str(first)], capture_output=True, text=True, check=False)
            self.assertEqual(one.returncode, 0, one.stdout + one.stderr)
            write_jsonl(lite, task_rows("lite"))
            two = subprocess.run(command + ["--output", str(second)], capture_output=True, text=True, check=False)
            self.assertEqual(two.returncode, 0, two.stdout + two.stderr)

            # Source manifests are intentionally hashed byte-for-byte, so normalize
            # both source files before comparing a second pair of plans.
            third = root / "third.jsonl"
            three = subprocess.run(command + ["--output", str(third)], capture_output=True, text=True, check=False)
            self.assertEqual(three.returncode, 0, three.stdout + three.stderr)
            self.assertEqual(second.read_bytes(), third.read_bytes())
            digest = hashlib.sha256(second.read_bytes()).hexdigest()
            self.assertEqual(Path(f"{second}.sha256").read_text(), f"{digest}  {second.name}\n")
            self.assertNotEqual(first.read_bytes(), second.read_bytes())

    def test_matrix_reuses_baseline_and_emits_step_3_policy_only(self):
        config = PLANNER.load_config(CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {suite: root / f"{suite}.jsonl" for suite in PLANNER.REQUIRED_SUITES}
            for suite, path in paths.items():
                write_jsonl(path, task_rows(suite))
            manifests = {
                suite: PLANNER.load_task_manifest(paths[suite], suite)
                for suite in PLANNER.REQUIRED_SUITES
            }
            rows = PLANNER.build_plan(config, manifests, config_sha256="a" * 64)

        header, cases = rows[0], rows[1:]
        self.assertEqual(header["record_type"], "plan")
        self.assertEqual(header["global_deadline_seconds"], 1209600)
        self.assertFalse(header["step_3_selection_policy"]["emit_execution_rows"])
        self.assertEqual(header["step_3_selection_policy"]["selection_after"], "completed_and_audited_step_1")
        self.assertEqual({row["suite"] for row in cases}, {"lite", "verified"})
        self.assertTrue(all(row["concurrency"] == 1 for row in cases))
        self.assertTrue(all(row["per_case_deadline_seconds"] == 5400 for row in cases))
        self.assertFalse(any(3 in row["steps"] for row in cases))
        self.assertEqual(len({row["resume_key"] for row in cases}), len(cases))

        baselines = [row for row in cases if row["cell_id"] == "shared-baseline"]
        shared = [row for row in baselines if row["steps"] == [1, 2]]
        sweeps = [row for row in cases if row["roles"] == ["step_2_sweep"]]
        self.assertEqual(len(baselines), 24)
        self.assertEqual(len(shared), 24)
        self.assertEqual(len(sweeps), 24 * 4 * 3)
        self.assertEqual(len(cases), 312)
        self.assertEqual(
            {row["variation"]["knob"] for row in sweeps},
            {"call_limit", "max_output_tokens", "observation_length", "temperature"},
        )

    def test_duplicate_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lite.jsonl"
            rows = task_rows("lite", 1) * 2
            write_jsonl(path, rows)
            with self.assertRaisesRegex(PLANNER.PlanError, "duplicate lite instance_id"):
                PLANNER.load_task_manifest(path, "lite")

    def test_missing_or_mislabeled_suite_is_rejected(self):
        config = PLANNER.load_config(CONFIG)
        with self.assertRaisesRegex(PLANNER.PlanError, "both Lite and Verified"):
            PLANNER.build_plan(config, {"lite": (task_rows("lite"), "a" * 64)}, config_sha256="b" * 64)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lite.jsonl"
            write_jsonl(path, task_rows("verified", 1))
            with self.assertRaisesRegex(PLANNER.PlanError, "declares suite"):
                PLANNER.load_task_manifest(path, "lite")

    def test_floating_revision_is_rejected(self):
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        floating = deepcopy(config)
        floating["pins"]["model_revision"] = "main"
        with self.assertRaisesRegex(PLANNER.PlanError, "floating or invalid revision"):
            PLANNER.validate_config(floating)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lite.jsonl"
            row = task_rows("lite", 1)[0]
            row["dataset_revision"] = "latest"
            write_jsonl(path, [row])
            with self.assertRaisesRegex(PLANNER.PlanError, "floating or invalid revision"):
                PLANNER.load_task_manifest(path, "lite")

    def test_too_few_step_2_tasks_fails_closed(self):
        config = PLANNER.load_config(CONFIG)
        manifests = {
            suite: (task_rows(suite, 11), suite[0] * 64)
            for suite in PLANNER.REQUIRED_SUITES
        }
        with self.assertRaisesRegex(PLANNER.PlanError, "Step 2 requires 12"):
            PLANNER.build_plan(config, manifests, config_sha256="c" * 64)

    def test_planner_contains_no_workload_execution_primitive(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("docker run", source)
        self.assertNotIn("nvidia-smi", source)


if __name__ == "__main__":
    unittest.main()
