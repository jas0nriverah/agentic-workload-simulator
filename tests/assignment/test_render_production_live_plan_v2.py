"""Checks for the concrete, offline production live-plan artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.render_production_live_plan_v2 import render_live_plan


ROOT = Path(__file__).resolve().parents[2]
LIVE = (
    ROOT.parent
    / "h100-assignment-work-20260905"
    / "assignment"
    / "submission"
    / "20260908T140000Z-offline-v2"
    / "live-plan"
)


def read_json(name: str):
    return json.loads((LIVE / name).read_text(encoding="utf-8"))


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProductionLivePlanTests(unittest.TestCase):
    def test_renderer_emits_current_guidance_and_supersedes_legacy_recovery(self):
        with tempfile.TemporaryDirectory(prefix="production-live-plan-v2-") as temporary:
            staged = Path(temporary) / "snapshot"
            shutil.copytree(ROOT.parent / "h100-assignment-work-20260905" / "assignment" / "submission" / "20260908T140000Z-offline-v2", staged)
            rendered = render_live_plan(snapshot_root=staged, repo_root=ROOT)
            live_plan = staged / "live-plan"
            expected_environment = json.loads((ROOT / "configs" / "noninteractive_tool_environment.v1.json").read_text())["env_variables"]
            production_fragment = json.loads((ROOT / "cloud" / "lambda" / "sweagent_request.yaml").read_text())
            self.assertEqual(production_fragment["agent"]["tools"]["env_variables"], expected_environment)
            for fragment in (live_plan / "confirmation-request-configs").glob("*.json"):
                self.assertEqual(json.loads(fragment.read_text())["agent"]["tools"]["env_variables"], expected_environment)

            readme = live_plan / "README.md"
            recovery = live_plan / "recovery_resume.v2.md"
            self.assertTrue(recovery.is_file())
            self.assertIn("recovery_resume.v2.md", readme.read_text(encoding="utf-8"))
            self.assertIn("recovery_resume.md", readme.read_text(encoding="utf-8"))
            recovery_text = recovery.read_text(encoding="utf-8")
            for expected in (
                "explicitly superseded",
                "24 instances × the four candidates",
                "96 trajectories",
                "24 condition passes",
                "observation_length=25000",
                "finalize_production_plan.py",
                "--execute --acknowledge-paid-gpu-work",
                "PACE_SOCKET",
                "Missing proof versus missing code",
            ):
                self.assertIn(expected, recovery_text)

            self.assertEqual(file_sha(recovery), recovery.with_suffix(recovery.suffix + ".sha256").read_text(encoding="ascii").split()[0])
            workflow = json.loads((live_plan / "production_execution_workflow.v2.json").read_text(encoding="utf-8"))
            self.assertEqual(workflow["operator_guidance"]["current_recovery"]["path"], "recovery_resume.v2.md")
            self.assertEqual(workflow["operator_guidance"]["legacy_recovery"]["status"], "legacy_superseded")
            self.assertEqual(workflow["operator_guidance"]["current_recovery"]["sha256"], file_sha(recovery))
            run_manifest = json.loads((live_plan / "production_run_manifest.v2.json").read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["operator_guidance"]["current_readme"]["sha256"], file_sha(readme))
            self.assertEqual(rendered["artifacts"]["operator_guidance_recovery"]["sha256"], file_sha(recovery))

    def test_candidate_inventories_are_complete_and_fresh(self):
        manifest = read_json("production_candidate_inventory_manifest.json")
        self.assertEqual(manifest["candidate_count"], 4)
        self.assertEqual(manifest["final_candidate_selection"], "pending_Astra_live_confirmation")
        for entry in manifest["candidate_inventories"]:
            path = LIVE / entry["path"]
            self.assertEqual(file_sha(path), entry["sha256"])
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["execution_case_count"], 1088)
            cases = rows[1:]
            self.assertEqual(len(cases), 1088)
            self.assertEqual(sum(row["cell_id"] == "shared-baseline" for row in cases), 800)
            self.assertEqual(sum(row["cell_id"] != "shared-baseline" for row in cases), 288)
            self.assertEqual(len({row["case_id"] for row in cases}), 1088)
            self.assertTrue(all(row["case_id"].startswith("assignment-production-v2:") for row in cases))
            self.assertTrue(all(set(row["settings"]) == {"call_limit", "max_output_tokens", "observation_length", "temperature", "max_input_tokens", "top_p", "seed"} for row in cases))
            self.assertTrue(all("official_resolved" not in row and "status" not in row for row in cases))

    def test_confirmation_plan_has_literal_effective_settings_and_hash_bound_specs(self):
        plan = read_json("configuration_confirmation_execution_plan.json")
        self.assertEqual(plan["execution_case_count"], 96)
        self.assertEqual(plan["panel_instance_count"], 24)
        self.assertEqual(plan["candidate_selection"], "pending_Astra_live_confirmation")
        self.assertEqual(set(plan["canonical_configuration_keys"]), {"call_limit", "max_output_tokens", "observation_length", "temperature", "max_input_tokens", "top_p", "seed"})
        for row in plan["cases"]:
            settings = row["settings"]
            command = row["effective_sweagent_command"]
            self.assertEqual(command[command.index("--agent.model.per_instance_call_limit") + 1], str(settings["call_limit"]))
            self.assertEqual(command[command.index("--agent.model.max_input_tokens") + 1], str(settings["max_input_tokens"]))
            self.assertEqual(command[command.index("--agent.model.max_output_tokens") + 1], str(settings["max_output_tokens"]))
            self.assertEqual(command[command.index("--agent.templates.max_observation_length") + 1], str(settings["observation_length"]))
            self.assertEqual(command[command.index("--agent.model.temperature") + 1], str(settings["temperature"]))
            request = LIVE / row["request_config"]["path"]
            self.assertEqual(file_sha(request), row["request_config"]["sha256"])
            request_value = json.loads(request.read_text(encoding="utf-8"))
            completion = request_value["agent"]["model"]["completion_kwargs"]
            self.assertEqual(completion["max_tokens"], settings["max_output_tokens"])
            self.assertEqual(completion["top_p"], settings["top_p"])
            self.assertEqual(completion["seed"], settings["seed"])
            spec = LIVE / row["case_spec"]["path"]
            self.assertEqual(file_sha(spec), row["case_spec"]["sha256"])

    def test_overhead_plan_is_four_fixtures_twelve_pairs_twenty_four_condition_passes(self):
        plan = read_json("overhead_replay_plan.v2.json")
        self.assertEqual(plan["fixture_count"], 4)
        self.assertEqual(plan["pair_count"], 12)
        self.assertEqual(plan["condition_pass_count"], 24)
        self.assertEqual(len(plan["passes"]), 12)
        self.assertEqual(len(set(plan["fixture_ids"])), 4)
        self.assertEqual({row["repeat"] for row in plan["passes"]}, {0, 1, 2})
        self.assertIn("individual_cpu_operation_records", plan["required_review_fields"])
        self.assertIn("raw_model_request_records", plan["required_review_fields"])
        self.assertIsNone(plan["passes"][0]["full_production_capture_enabled"])
        self.assertEqual(plan["status"], "pending_live_fixture_capture")

    def test_split_excludes_exactly_twenty_four_panel_clusters_and_seals_holdout(self):
        split = read_json("production_split_manifest.v2.json")
        self.assertEqual(split["production_case_count"], 1088)
        self.assertEqual(split["clusters_excluded_from_final_d9"], 24)
        self.assertEqual(len(split["development_excluded_clusters"]), 24)
        self.assertEqual(split["sealed_holdout"]["instance_id"], "sympy__sympy-12481")
        self.assertFalse(split["outcomes_accessed"])
        self.assertTrue(split["no_evaluator_labels"])
        self.assertEqual(sum(item["case_count"] for item in split["partition_counts"].values()), 1088)
        self.assertEqual(sum(item["cluster_count"] for item in split["partition_counts"].values()), split["cluster_count"])
        self.assertEqual(len(split["case_assignments"]), 1088)
        self.assertTrue(all("official_resolved" not in item and "status" not in item for item in split["case_assignments"]))

    def test_proof_templates_are_explicitly_pending_and_cover_contract_ids(self):
        contract = read_json("assignment_acquisition_contract.v2.json")
        expected_requirements = {row["id"] for row in contract["requirements"]}
        expected_regressions = {row["id"] for row in contract["historical_regressions"]}
        self.assertEqual(file_sha(LIVE / "assignment_acquisition_contract.v2.json"), "5aa1930c4c311d0e92e2f90b0163bed5f3c85d932b45209f8ade869233670446")
        acquisition = read_json("proof-templates/acquisition_proof.pending.json")
        regression = read_json("proof-templates/historical_regression_proof.pending.json")
        self.assertEqual({row["id"] for row in acquisition["requirements"]}, expected_requirements)
        self.assertEqual({row["id"] for row in regression["regressions"]}, expected_regressions)
        self.assertTrue(all(row["status"] == "pending" and row["artifact_roles"] == [] for row in acquisition["requirements"]))
        self.assertTrue(all(row["status"] == "pending" and row["artifact_roles"] == [] for row in regression["regressions"]))
        self.assertEqual(acquisition["status"], "pending_live_evidence")
        self.assertEqual(regression["status"], "pending_live_evidence")
        self.assertFalse(acquisition["launch_authorized"])
        self.assertFalse(regression["launch_authorized"])
        roles = read_json("production_proof_role_manifest.v2.json")
        self.assertEqual(set(roles["required_roles"]), {"pilot_inventory", "event_journals", "overhead_replay", "source_bundle", "remote_reconciliation", "feature_parity", "run_manifest", "acquisition_contract", "acquisition_proof", "historical_regression_proof", "raw_cpu_record_inventory", "raw_model_record_inventory", "offline_test_report", "full_matrix_inventory"})
        self.assertFalse(roles["launch_authorized"])


if __name__ == "__main__":
    unittest.main()
