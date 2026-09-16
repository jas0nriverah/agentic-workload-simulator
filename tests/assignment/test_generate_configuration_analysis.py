"""Regression tests for the deterministic, descriptive Step 2 panel."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.generate_configuration_analysis import (
    CONFIRMATION_PANEL_COUNT,
    CONFIRMATION_CASE_COUNT,
    CONFIGURATION_CANDIDATES,
    DEFAULT_BOOTSTRAP_SEED,
    build_report,
    build_confirmation_panel,
    candidate_settings,
    load_paired_rows,
    select_confirmation_additions,
    write_analysis,
)


ROOT = Path(__file__).resolve().parents[2]
INPUT = ROOT.parent / "h100-assignment-work-20260905" / "assignment" / "submission" / "20260908T140000Z-offline-v2" / "figures-input"


class ConfigurationAnalysisTests(unittest.TestCase):
    def _inputs(self):
        return load_paired_rows(
            trajectories_path=INPUT / "trajectories.csv",
            sweep_runs_path=INPUT / "sweep_runs.csv",
            sweep_metadata_path=INPUT / "sweep_metadata.jsonl",
            evaluator_provenance_path=INPUT / "evaluator_provenance.csv",
        )

    def test_panel_counts_and_25000_headline_are_source_bound(self):
        rows, source = self._inputs()
        report, _ = build_report(rows, source, bootstrap_seed=DEFAULT_BOOTSTRAP_SEED, bootstrap_repetitions=100)
        self.assertEqual(len(rows), 288)
        self.assertEqual(len(report["settings"]), 12)
        headline = report["headline_observation_length_25000"]
        self.assertEqual(
            {key: headline[key] for key in ("pair_count", "cluster_count", "discordance_count", "both_resolved_count", "faster_count")},
            {"pair_count": 24, "cluster_count": 23, "discordance_count": 0, "both_resolved_count": 16, "faster_count": 13},
        )
        self.assertEqual(source["trajectories"]["row_count"], 800)
        self.assertEqual(source["sweep_runs"]["row_count"], 384)
        self.assertFalse(source["historical_holdout"]["accessed"])

    def test_bootstrap_is_deterministic_and_zero_event_is_qualified(self):
        rows, source = self._inputs()
        first, _ = build_report(rows, source, bootstrap_seed=77, bootstrap_repetitions=250)
        second, _ = build_report(rows, source, bootstrap_seed=77, bootstrap_repetitions=250)
        self.assertEqual(first, second)
        point = next(item for item in first["settings"] if item["setting"]["label"] == "observation_length=25000")
        self.assertEqual(point["bootstrap"]["discordance_rate_ci"], [0.0, 0.0])
        bound = point["bootstrap"]["zero_discordance_upper_bound"]
        self.assertAlmostEqual(bound["value"], 1 - 0.05 ** (1 / 23))
        self.assertIn("independent instance clusters", bound["assumption"])
        self.assertIn("No noninferiority", first["interpretation"]["noninferiority"])

    def test_cli_artifacts_include_sidecars_and_markdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = write_analysis(
                trajectories_path=INPUT / "trajectories.csv",
                sweep_runs_path=INPUT / "sweep_runs.csv",
                sweep_metadata_path=INPUT / "sweep_metadata.jsonl",
                evaluator_provenance_path=INPUT / "evaluator_provenance.csv",
                output_dir=Path(temporary),
                bootstrap_repetitions=50,
            )
            self.assertEqual(result["settings"], 12)
            report = json.loads((Path(temporary) / "CONFIGURATION_ANALYSIS.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "complete_offline_descriptive")
            for name in ("CONFIGURATION_ANALYSIS.json", "CONFIGURATION_ANALYSIS.md", "bootstrap_ci.json", "paired_comparisons.csv", "README.md"):
                self.assertTrue((Path(temporary) / name).is_file())
                self.assertTrue((Path(temporary) / f"{name}.sha256").is_file())

    def test_confirmation_candidates_are_exactly_four_and_fixed(self):
        self.assertEqual(
            [item["candidate_id"] for item in CONFIGURATION_CANDIDATES],
            [
                "historical-control-call30-input32768",
                "expanded-call50-input61440",
                "expanded-call100-input61440",
                "expanded-call100-input61440-observation25000",
            ],
        )
        settings = [candidate_settings(item) for item in CONFIGURATION_CANDIDATES]
        self.assertEqual({item["call_limit"] for item in settings}, {30, 50, 100})
        self.assertEqual({item["max_input_tokens"] for item in settings}, {32768, 61440})
        self.assertEqual({item["observation_length"] for item in settings}, {25000, 100000})
        for item in settings:
            self.assertEqual(item["max_output_tokens"], 2048)
            self.assertEqual(item["temperature"], 0.0)
            self.assertEqual(item["top_p"], 1.0)
            self.assertEqual(item["seed"], 0)
            self.assertNotIn("serving_max_model_len", item)
        records = [
            __import__("scripts.assignment.generate_configuration_analysis", fromlist=["_candidate_record"])._candidate_record(item)
            for item in CONFIGURATION_CANDIDATES
        ]
        self.assertTrue(all(item["serving_configuration"] == {"max_model_len": 65536, "vllm_version": "0.10.0"} for item in records))
        self.assertTrue(all(set(item["final_configuration"]) == {"call_limit", "max_output_tokens", "observation_length", "temperature", "max_input_tokens", "top_p", "seed"} for item in records))

    def test_confirmation_additions_are_deterministic_category_diverse_clusters(self):
        rows = []
        for termination_class in ("exit_cost", "exit_context"):
            for index, category in enumerate(("repo/a", "repo/b", "repo/c", "repo/d", "repo/e")):
                rows.append(
                    {
                        "case_id": f"case-{termination_class}-{index}",
                        "suite": "lite" if index % 2 else "verified",
                        "repository": category,
                        "category": category,
                        "instance_id": f"instance-{termination_class}-{index}",
                        "native_exit_class": termination_class,
                    }
                )
        first = select_confirmation_additions(rows, excluded_case_ids=(), excluded_instance_ids=())
        second = select_confirmation_additions(rows, excluded_case_ids=(), excluded_instance_ids=())
        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)
        self.assertEqual(
            {item["panel_role"] for item in first},
            {"exit_cost", "exit_context"},
        )
        for role in ("exit_cost", "exit_context"):
            selected = [item for item in first if item["panel_role"] == role]
            self.assertEqual(len(selected), 4)
            self.assertEqual(len({item["category"] for item in selected}), 4)
            self.assertEqual(len({item["instance_id"] for item in selected}), 4)

    def test_confirmation_panel_real_inventory_has_96_cases_and_full_resume_specs(self):
        snapshot = ROOT.parent / "h100-assignment-work-20260905" / "assignment" / "submission" / "20260908T140000Z-offline-v2"
        canonical_rows, _, _ = __import__("scripts.assignment.generate_configuration_analysis", fromlist=["_read_csv"])._read_csv(snapshot / "figures-input" / "trajectories.csv")
        pilot = json.loads((snapshot / "live-plan" / "pilot_cases.json").read_text(encoding="utf-8"))["cases"]
        evidence = json.loads((snapshot / "configuration-analysis" / "TERMINATION_EVIDENCE_INDEX.json").read_text(encoding="utf-8"))["cases"]
        inventory = {}
        with (snapshot / "live-plan" / "full_matrix_case_inventory.jsonl").open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("record_type") == "case":
                    inventory[row["resume_key"]] = row
        panel = build_confirmation_panel(
            canonical_rows=canonical_rows,
            evidence_rows=evidence,
            existing_pilot_cases=pilot,
            source_case_specs=inventory,
        )
        self.assertEqual(panel["panel"]["instance_count"], CONFIRMATION_PANEL_COUNT)
        self.assertEqual(panel["panel"]["candidate_case_count"], CONFIRMATION_CASE_COUNT)
        self.assertEqual(len(panel["candidates"]), 4)
        self.assertEqual(len(panel["candidate_cases"]), 96)
        self.assertEqual(len({item["candidate_case_id"] for item in panel["candidate_cases"]}), 96)
        self.assertNotIn("sympy__sympy-12481", {item["instance_id"] for item in panel["panel"]["instances"]})
        for item in panel["panel"]["instances"]:
            self.assertEqual(item["resume_key"], item["historical_template_case_id"])
            self.assertEqual(len(item["source_case_spec_sha256"]), 64)
            self.assertEqual(len(item["termination_evidence"]["evidence_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
