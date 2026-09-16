"""Contract tests for the assignment Steps 1--3 figure generator."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts/assignment/generate_step_figures.py"
SPEC = importlib.util.spec_from_file_location("generate_step_figures", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
FIGURES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIGURES)


class AssignmentFigureGeneratorTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _fixtures(self, root: Path) -> dict[str, Path]:
        paths = {
            name: root / f"{name}.csv"
            for name in ("trajectories", "tool_events", "model_events", "sweep_runs")
        }
        paths["selection"] = root / "step3_selection.json"
        self._write(
            paths["trajectories"],
            [
                "run_id", "suite", "repository", "category", "status",
                "official_resolved", "e2e_wall_ms", "submitted", "config_id",
                "instance_id", "repeat_id", "provenance",
            ],
            [
                {"run_id": "t1", "suite": "lite", "repository": "repo-a", "category": "search", "status": "completed", "official_resolved": True, "e2e_wall_ms": 100, "submitted": True, "config_id": "shared-baseline", "instance_id": "i1", "repeat_id": "r0", "provenance": "measured"},
                {"run_id": "t2", "suite": "verified", "repository": "repo-a", "category": "search", "status": "completed", "official_resolved": False, "e2e_wall_ms": 120, "submitted": True, "config_id": "shared-baseline", "instance_id": "i2", "repeat_id": "r0", "provenance": "measured"},
                {"run_id": "t3", "suite": "lite", "repository": "repo-b", "category": "edit", "status": "completed", "official_resolved": False, "e2e_wall_ms": 200, "submitted": True, "config_id": "shared-baseline", "instance_id": "i3", "repeat_id": "r0", "provenance": "measured"},
                {"run_id": "t4", "suite": "verified", "repository": "repo-b", "category": "edit", "status": "completed", "official_resolved": True, "e2e_wall_ms": 220, "submitted": True, "config_id": "shared-baseline", "instance_id": "i4", "repeat_id": "r0", "provenance": "measured"},
            ],
        )
        self._write(
            paths["tool_events"],
            ["event_id", "run_id", "status", "operation_class", "wall_ms"],
            [
                {"event_id": "tool-1", "run_id": "t1", "status": "completed", "operation_class": "read", "wall_ms": 20},
                {"event_id": "tool-2", "run_id": "t2", "status": "completed", "operation_class": "search", "wall_ms": 30},
                {"event_id": "tool-3", "run_id": "t3", "status": "completed", "operation_class": "write", "wall_ms": 80},
                {"event_id": "tool-4", "run_id": "t4", "status": "completed", "operation_class": "test", "wall_ms": 100},
            ],
        )
        self._write(
            paths["model_events"],
            [
                "request_id", "run_id", "status", "input_tokens", "output_tokens",
                "context_tokens", "wall_ms",
            ],
            [
                {"request_id": "model-1", "run_id": "t1", "status": "completed", "input_tokens": 100, "output_tokens": 20, "context_tokens": 120, "wall_ms": 60},
                {"request_id": "model-2", "run_id": "t2", "status": "completed", "input_tokens": 110, "output_tokens": 20, "context_tokens": 130, "wall_ms": 60},
                {"request_id": "model-3", "run_id": "t3", "status": "completed", "input_tokens": 120, "output_tokens": 30, "context_tokens": 150, "wall_ms": 100},
                {"request_id": "model-4", "run_id": "t4", "status": "completed", "input_tokens": 130, "output_tokens": 30, "context_tokens": 160, "wall_ms": 100},
            ],
        )
        self._write(
            paths["sweep_runs"],
            [
                "run_id", "status", "sweep_parameter", "sweep_value",
                "official_resolved", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms",
                "suite", "repository", "category", "instance_id", "repeat_id",
                "config_id", "provenance",
            ],
            [
                {"run_id": "s1", "status": "completed", "sweep_parameter": "max_steps", "sweep_value": "10", "official_resolved": False, "e2e_wall_ms": 90, "tool_wall_ms": 20, "model_wall_ms": 50, "suite": "lite", "repository": "repo-a", "category": "repo-a", "instance_id": "i1", "repeat_id": "r0", "config_id": "max_steps=10", "provenance": "measured"},
                {"run_id": "s2", "status": "completed", "sweep_parameter": "max_steps", "sweep_value": "20", "official_resolved": True, "e2e_wall_ms": 130, "tool_wall_ms": 30, "model_wall_ms": 70, "suite": "verified", "repository": "repo-a", "category": "repo-a", "instance_id": "i2", "repeat_id": "r0", "config_id": "max_steps=20", "provenance": "measured"},
                {"run_id": "s3", "status": "completed", "sweep_parameter": "max_tokens", "sweep_value": "256", "official_resolved": False, "e2e_wall_ms": 80, "tool_wall_ms": 20, "model_wall_ms": 40, "suite": "lite", "repository": "repo-b", "category": "repo-b", "instance_id": "i3", "repeat_id": "r0", "config_id": "max_tokens=256", "provenance": "measured"},
                {"run_id": "s4", "status": "completed", "sweep_parameter": "max_tokens", "sweep_value": "512", "official_resolved": True, "e2e_wall_ms": 140, "tool_wall_ms": 30, "model_wall_ms": 80, "suite": "verified", "repository": "repo-b", "category": "repo-b", "instance_id": "i4", "repeat_id": "r0", "config_id": "max_tokens=512", "provenance": "measured"},
            ],
        )
        self._write_selection(paths)
        return paths

    def _write_selection(self, paths: dict[str, Path], selected_run_id: str = "t4") -> None:
        with paths["trajectories"].open(newline="", encoding="utf-8") as stream:
            rows = {row["run_id"]: row for row in csv.DictReader(stream)}
        row = rows[selected_run_id]
        phase_ms = {"t1": (20.0, 60.0), "t2": (30.0, 60.0), "t3": (80.0, 100.0), "t4": (100.0, 100.0)}
        tool_ms, model_ms = phase_ms[selected_run_id]
        payload = {
            "schema_version": "assignment.step3-selection.v1",
            "source_trajectories_sha256": hashlib.sha256(paths["trajectories"].read_bytes()).hexdigest(),
            "metric": "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
            "selected": {
                "run_id": selected_run_id, "suite": row["suite"], "repository": row["repository"],
                "category": row["category"], "instance_id": row["instance_id"],
                "config_id": row["config_id"], "repeat_id": row["repeat_id"],
                "tool_wall_ms": tool_ms, "model_wall_ms": model_ms,
                "e2e_wall_ms": float(row["e2e_wall_ms"]), "tool_model_ratio": tool_ms / model_ms,
                "tool_event_count": 1, "model_event_count": 1,
            },
        }
        paths["selection"].write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        digest = hashlib.sha256(paths["selection"].read_bytes()).hexdigest()
        Path(str(paths["selection"]) + ".sha256").write_text(
            f"{digest}  {paths['selection'].name}\n", encoding="utf-8"
        )

    def _generate(
        self, paths: dict[str, Path], output: Path, *, force: bool = False, **kwargs: object
    ) -> dict:
        return FIGURES.generate_figures(
            trajectories_path=paths["trajectories"],
            tool_events_path=paths["tool_events"],
            model_events_path=paths["model_events"],
            sweep_runs_path=paths["sweep_runs"],
            output_dir=output,
            step3_selection_path=paths["selection"],
            force=force,
            **kwargs,
        )

    def test_generates_all_step_figures_and_exact_phase_ratios(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            output = root / "figures"
            summary = self._generate(paths, output)
            expected = {
                "step1_accuracy_vs_latency.svg", "step1_accuracy_vs_ratio.svg",
                "step1_sample_latency_vs_ratio.svg", "step1_repository_ratio.svg",
                "step2_max-steps.svg",
                "step2_max-tokens.svg", "step2_combined.svg",
                "step3_latency_breakdown.svg", "step3_tool_events.svg",
                "step3_model_tokens_vs_latency.svg", "assignment_report.json",
                "assignment_report.md",
            }
            self.assertEqual({path.name for path in output.iterdir()}, expected)
            self.assertEqual(summary["schema_version"], "assignment-step-figures.v1")
            self.assertEqual(
                {row["path"] for row in summary["figure_inventory"]},
                set(summary["figures"]),
            )
            for row in summary["figure_inventory"]:
                figure = output / row["path"]
                self.assertEqual(row["size_bytes"], figure.stat().st_size)
                self.assertEqual(row["sha256"], hashlib.sha256(figure.read_bytes()).hexdigest())
            self.assertEqual(
                summary["ratio_definition"],
                "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
            )
            categories = {row["category"]: row for row in summary["categories"]}
            self.assertAlmostEqual(categories["search"]["tool_model_ratio"], 50 / 120)
            self.assertAlmostEqual(categories["edit"]["tool_model_ratio"], 180 / 200)
            self.assertEqual(categories["search"]["accuracy_percent"], 50.0)
            self.assertFalse(summary["coverage"]["complete_assignment_matrix"])
            self.assertEqual(summary["coverage"]["step_3_selected_run_id"], "t4")
            ratio_svg = (output / "step1_repository_ratio.svg").read_text(encoding="utf-8")
            self.assertEqual(ratio_svg.count('data-sample="true"'), 4)
            self.assertIn("category = repository", ratio_svg)
            self.assertEqual(
                summary["suite_headline_metrics"]["lite"]["selected"],
                {"count": 2, "denominator": 2},
            )
            self.assertEqual(
                summary["suite_headline_metrics"]["verified"]["resolved"],
                {"count": 1, "denominator": 2},
            )
            self.assertEqual(
                json.loads((output / "assignment_report.json").read_text()), summary
            )
            combined = (output / "step2_combined.svg").read_text(encoding="utf-8")
            self.assertIn("Step 2: combined hyperparameter sensitivity", combined)
            self.assertIn('x="30.0" y="82.0"', combined)
            self.assertIn('x="500.0" y="82.0"', combined)
            self.assertIn('x="970.0" y="82.0"', combined)
            self.assertIn("Observed tool/model ratio", combined)
            self.assertIn("Observed E2E wall (ms)", combined)
            self.assertIn("Observed accuracy (%)", combined)

    def test_step2_preserves_category_curves_and_individual_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            summary = self._generate(paths, root / "figures")
            settings = summary["sweeps"]["max_steps"]
            self.assertEqual([setting["count"] for setting in settings], [1, 1])
            self.assertEqual(
                [len(setting["samples"]) for setting in settings], [1, 1]
            )
            self.assertEqual(
                {sample["category"] for setting in settings for sample in setting["samples"]},
                {"repo-a"},
            )
            svg = (root / "figures" / "step2_max-steps.svg").read_text(encoding="utf-8")
            self.assertEqual(svg.count('data-sample="true"'), 2)
            self.assertIn('data-category="repo-a"', svg)
            self.assertIn('data-value="10"', svg)
            self.assertIn('data-value="20"', svg)
            self.assertIn("Category curves", svg)

    def test_step2_rejects_duplicate_baseline_sample_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            with paths["sweep_runs"].open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            duplicate = dict(rows[0])
            duplicate["run_id"] = "s1-duplicate"
            rows.append(duplicate)
            self._write(paths["sweep_runs"], list(rows[0].keys()), rows)
            with self.assertRaisesRegex(FIGURES.DataContractError, "duplicate sample identity"):
                self._generate(paths, root / "figures")

    def test_d1_headline_metrics_are_separate_from_trace_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            d1 = root / "d1_headline_metrics.json"
            metrics = {
                "selected": {"count": 1, "denominator": 1},
                "submitted": {"count": 1, "denominator": 1},
                "completed": {"count": 1, "denominator": 1},
                "resolved": {"count": 1, "denominator": 1},
                "resolved_rate": {"numerator": 1, "denominator": 1, "percent": 100.0},
                "average_completed_e2e_wall_ms": 111.0,
            }
            d1.write_text(
                json.dumps(
                    {
                        "schema_version": "assignment.d1-headline-metrics.v1",
                        "latency_definition": "original accepted-case duration_ms",
                        "provenance": {"source": "accepted-case summaries"},
                        "suites": {"lite": metrics, "verified": metrics},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            summary = self._generate(
                paths, root / "figures", d1_headline_metrics_path=d1
            )
            self.assertEqual(
                summary["suite_headline_metrics"]["lite"]["average_completed_e2e_wall_ms"],
                111.0,
            )
            self.assertNotEqual(
                summary["suite_headline_metrics"]["lite"]["average_completed_e2e_wall_ms"],
                summary["trace_overlay_suite_metrics"]["lite"]["average_completed_e2e_wall_ms"],
            )
            self.assertEqual(summary["d1_headline_metrics"]["sha256"], FIGURES._sha256(d1))
            report = (root / "figures" / "assignment_report.md").read_text(encoding="utf-8")
            self.assertIn("D1 original accepted-case headline metrics", report)
            self.assertIn("Event-overlay trace metrics", report)

    def test_predicted_latency_mode_is_explicit_and_does_not_claim_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            with paths["trajectories"].open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                row["e2e_wall_ms"] = str(float(row["e2e_wall_ms"]) + 10.0)
            self._write(paths["trajectories"], list(rows[0].keys()), rows)
            summary = self._generate(paths, root / "figures", latency_kind="predicted")
            self.assertEqual(summary["latency_kind"], "predicted")
            self.assertEqual(summary["coverage"]["complete_assignment_matrix"], False)
            self.assertFalse(summary["coverage"]["predicted_latency_full_completeness_claim"])
            self.assertEqual(summary["latency_semantics"]["accuracy_values"], "observed official_resolved outcomes")
            svg = (root / "figures" / "step1_sample_latency_vs_ratio.svg").read_text(encoding="utf-8")
            self.assertIn("predicted", svg.lower())
            self.assertIn("observed", svg.lower())

    def test_step3_exposes_all_event_fields_and_unknown_residual(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            summary = self._generate(paths, root / "figures")
            self.assertEqual(len(summary["step3_events"]["tool_events"]), 1)
            self.assertEqual(len(summary["step3_events"]["model_events"]), 1)
            tool_svg = (root / "figures" / "step3_tool_events.svg").read_text(encoding="utf-8")
            model_svg = (root / "figures" / "step3_model_tokens_vs_latency.svg").read_text(encoding="utf-8")
            breakdown = (root / "figures" / "step3_latency_breakdown.svg").read_text(encoding="utf-8")
            self.assertIn("data-operation-class=", tool_svg)
            self.assertIn("input tokens=130", model_svg)
            self.assertIn("output tokens=30", model_svg)
            self.assertIn("context tokens=160", model_svg)
            self.assertIn("request proxy wall", model_svg)
            self.assertIn("unknown E2E residual", breakdown)

    def test_missing_column_fails_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            self._write(
                paths["trajectories"],
                ["run_id", "suite", "repository", "status", "official_resolved", "e2e_wall_ms"],
                [{"run_id": "t1", "suite": "lite", "repository": "repo-a", "status": "completed", "official_resolved": True, "e2e_wall_ms": 100}],
            )
            output = root / "figures"
            with self.assertRaisesRegex(FIGURES.DataContractError, "missing required columns: category"):
                self._generate(paths, output)
            self.assertFalse(output.exists())

    def test_invalid_timing_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            self._write(
                paths["tool_events"],
                ["event_id", "run_id", "status", "operation_class", "wall_ms"],
                [
                    {"event_id": f"tool-{index}", "run_id": f"t{index}", "status": "completed", "operation_class": "read", "wall_ms": -1 if index == 1 else 20}
                    for index in range(1, 5)
                ],
            )
            output = root / "figures"
            with self.assertRaisesRegex(FIGURES.DataContractError, "wall_ms must be > 0"):
                self._generate(paths, output)
            self.assertFalse(output.exists())

    def test_step3_selection_excludes_higher_ratio_sweep_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            with paths["trajectories"].open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            for row in rows:
                row["config_id"] = (
                    "call_limit=10" if row["run_id"] == "t3" else "shared-baseline"
                )
                row["provenance"] = "measured"
                if row["run_id"] == "t3":
                    row["e2e_wall_ms"] = "300"
            self._write(paths["trajectories"], [*rows[0].keys()], rows)
            with paths["tool_events"].open(newline="", encoding="utf-8") as stream:
                tool_rows = list(csv.DictReader(stream))
            for row in tool_rows:
                if row["run_id"] == "t3":
                    row["wall_ms"] = "180"
            self._write(paths["tool_events"], [*tool_rows[0].keys()], tool_rows)
            self._write_selection(paths)
            summary = self._generate(paths, root / "figures")
            self.assertEqual(summary["coverage"]["step_3_selected_run_id"], "t4")
            self.assertEqual(
                summary["coverage"]["step_3_selection_population"],
                "sealed selected trajectory; no independent top-row selection",
            )

    def test_selection_sidecar_and_binding_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            Path(str(paths["selection"]) + ".sha256").unlink()
            with self.assertRaisesRegex(FIGURES.DataContractError, "sidecar is required"):
                self._generate(paths, root / "figures")

            self._write_selection(paths)
            selection = json.loads(paths["selection"].read_text(encoding="utf-8"))
            selection["selected"]["run_id"] = "t1"
            paths["selection"].write_text(json.dumps(selection, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(FIGURES.DataContractError, "sidecar does not match"):
                self._generate(paths, root / "figures")

    def test_matrix_completion_requires_bound_reconciliation_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trajectories = root / "trajectories.csv"
            trajectories.write_text("run_id\nrun-1\n", encoding="utf-8")
            report = root / "reconciliation.json"
            report.write_text(
                json.dumps(
                    {
                        "schema_version": "assignment-plan-reconciliation-report.v1",
                        "trajectories_sha256": FIGURES._sha256(trajectories),
                        "original_case_count": 9,
                        "matched_case_count": 9,
                        "remaining_case_count": 0,
                        "rejected_or_ambiguous_case_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            coverage = FIGURES._matrix_coverage(
                reconciliation_report_path=report,
                trajectories_path=trajectories,
                observed_parameters=set(FIGURES.REQUIRED_SWEEP_PARAMETERS),
            )
            self.assertTrue(coverage["complete_assignment_matrix"])
            trajectories.write_text("run_id\nrun-2\n", encoding="utf-8")
            with self.assertRaisesRegex(
                FIGURES.DataContractError, "trajectories_sha256 does not match"
            ):
                FIGURES._matrix_coverage(
                    reconciliation_report_path=report,
                    trajectories_path=trajectories,
                    observed_parameters=set(FIGURES.REQUIRED_SWEEP_PARAMETERS),
                )

    def test_orphan_event_and_overwrite_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = self._fixtures(root)
            output = root / "figures"
            self._generate(paths, output)
            with self.assertRaises(FileExistsError):
                self._generate(paths, output)
            self._generate(paths, output, force=True)
            orphan_root = root / "orphan"
            orphan_root.mkdir()
            orphan_paths = self._fixtures(orphan_root)
            self._write(
                orphan_paths["tool_events"],
                ["event_id", "run_id", "status", "operation_class", "wall_ms"],
                [{"event_id": "orphan", "run_id": "missing", "status": "completed", "operation_class": "read", "wall_ms": 1}],
            )
            with self.assertRaisesRegex(FIGURES.DataContractError, "unknown run_id"):
                self._generate(orphan_paths, orphan_root / "figures")


if __name__ == "__main__":
    unittest.main()
