from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.assignment import sweagent_case_runner as runner


ROOT = Path(__file__).resolve().parents[2]


def digest(value):
    return hashlib.sha256(runner._canonical(value).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return runner.sha256_file(path)


class ConfirmationCaseRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build_bundle()

    def build_bundle(self, deadline=5400):
        """Portable 24 x 4 fixture with the actual renderer's schema/hash formulas."""
        self.plan_path = self.root / "snapshot/live-plan/confirmation.json"
        self.panel_path = self.root / "snapshot/configuration-analysis/panel.json"
        self.case_path = self.root / "output/case_spec.json"
        self.manifest_path = self.root / "runtime.json"
        self.runtime = json.loads((ROOT / "configs/assignment_runtime_manifest.example.json").read_text())
        self.runtime["runner"]["telemetry"]["remote_hardware_profile"] = {
            "path": str(self.root / "hardware.json"), "sha256": "a" * 64,
        }
        self.instrumentation = {
            "schema_version": runner.TELEMETRY_V2_SCHEMA,
            "manifest_schema": "assignment.telemetry.v2.manifest",
            "instrumentation_version": runner.TELEMETRY_V2_VERSION,
            "feature_schema": "assignment.d9-feature.v2",
        }
        serving = {"max_model_len": 65536, "vllm_version": "0.10.0"}
        coordinates = [
            ("historical-control-call30-input32768", 30, 32768, 100000),
            ("expanded-call50-input61440", 50, 61440, 100000),
            ("expanded-call100-input61440", 100, 61440, 100000),
            ("expanded-call100-input61440-observation25000", 100, 61440, 25000),
        ]
        candidates = []
        for name, calls, inputs, observations in coordinates:
            settings = dict(call_limit=calls, max_input_tokens=inputs, observation_length=observations,
                            max_output_tokens=2048, temperature=0.0, top_p=1.0, seed=0)
            candidates.append(dict(candidate_id=name, settings=settings, final_configuration=settings,
                                   settings_sha256=digest(settings), serving_configuration=serving))
        instances, declared, executions = [], [], []
        seed = "assignment-configuration-confirmation-panel-20260908-v1"
        for number in range(24):
            historical_id = "assignment-case-v1:" + digest(number)
            source = dict(record_type="case", schema_version=runner.CASE_SCHEMA,
                          plan_id="assignment-steps-1-3", resume_key=historical_id, suite="verified",
                          repository="django/django", instance_id=f"django__django-{7530 + number}",
                          task_sha256=digest(["task", number]), source_manifest_sha256="b" * 64,
                          cell_id="shared-baseline", concurrency=1, steps=[1], roles=["step_1_baseline"],
                          variation=None, settings={key: candidates[0]["settings"][key] for key in runner.SETTINGS})
            if deadline is not None:
                source["per_case_deadline_seconds"] = deadline
            panel_id = "configuration-confirmation-panel-v1:" + digest({
                "case_id": historical_id, "panel_role": "existing_instrumentation_pilot", "seed": seed,
            })
            common = dict(panel_case_id=panel_id, historical_template_case_id=historical_id,
                          instance_id=source["instance_id"], suite="verified", repository="django/django")
            instances.append(dict(**common, panel_role="existing_instrumentation_pilot",
                                  source_case_spec=source, source_case_spec_sha256=digest(source)))
            for candidate in candidates:
                candidate_id = candidate["candidate_id"]
                identity = "configuration-confirmation-case-v1:" + digest({
                    "candidate_id": candidate_id, "panel_case_id": panel_id,
                })
                lineage = digest(dict(candidate_id=candidate_id, settings=candidate["settings"],
                                      serving_configuration=serving, source_case_spec=source))
                row = dict(**common, **candidate, candidate_case_id=identity,
                           source_case_spec_sha256=digest(source), case_spec_sha256=lineage)
                declared.append(row)
                case = dict(**common, record_type="case", schema_version=runner.CONFIRMATION_CASE_SCHEMA,
                            plan_id=runner.CONFIRMATION_PLAN_ID, namespace=runner.CONFIRMATION_NAMESPACE,
                            case_id=identity, resume_key=identity, candidate_id=candidate_id,
                            task_sha256=source["task_sha256"], source_manifest_sha256=source["source_manifest_sha256"],
                            cell_id=source["cell_id"], settings=candidate["settings"],
                            final_configuration=candidate["settings"], serving_configuration=serving,
                            instrumentation=self.instrumentation, outcome_blind=True, outcomes_accessed=False,
                            source_case_spec_sha256=digest(source), case_spec_sha256=lineage)
                spec_path = self.plan_path.parent / f"cases/{len(executions):03d}.json"
                request_path = self.plan_path.parent / f"requests/{candidate_id}.json"
                request_hash = write_json(request_path, {"agent": {"model": {"completion_kwargs": {
                    "max_tokens": 2048, "top_p": 1.0, "seed": 0,
                }}}})
                executions.append(dict(**common, candidate_id=candidate_id, candidate_case_id=identity,
                                       settings=candidate["settings"], serving_configuration=serving,
                                       case_spec={"path": str(spec_path.relative_to(self.plan_path.parent)),
                                                  "sha256": write_json(spec_path, case)},
                                       request_config={"path": str(request_path.relative_to(self.plan_path.parent)),
                                                       "sha256": request_hash}))
                if len(executions) == 1:
                    self.case = copy.deepcopy(case)
                    self.original_path = spec_path
        self.panel = dict(schema_version="assignment.configuration-confirmation-panel.v1",
                          candidates=candidates, candidate_cases=declared, selection={"seed": seed},
                          panel=dict(instances=instances, instance_count=24, candidate_case_count=96,
                                     holdout_instance_id="sympy__sympy-12481", holdout_accessed=False,
                                     final_run_outcomes_used=False))
        self.plan = dict(schema_version=runner.CONFIRMATION_PLAN_SCHEMA, plan_id=runner.CONFIRMATION_PLAN_ID,
                         namespace=runner.CONFIRMATION_NAMESPACE, case_schema=runner.CONFIRMATION_CASE_SCHEMA,
                         execution_case_count=96, panel_instance_count=24, candidate_count=4,
                         cases=executions, pins=self.runtime["pins"], instrumentation=self.instrumentation,
                         runner={"concurrency": 1}, source_bindings={})
        self.seal()

    def seal(self):
        self.plan["source_bindings"]["configuration_panel"] = {
            "path": "../configuration-analysis/panel.json", "sha256": write_json(self.panel_path, self.panel),
        }
        self.plan["cases"][0]["case_spec"]["sha256"] = write_json(self.original_path, self.case)
        write_json(self.case_path, self.case)
        self.plan_hash = write_json(self.plan_path, self.plan)
        Path(str(self.plan_path) + ".sha256").write_text(f"{self.plan_hash}  {self.plan_path.name}\n")

    def load(self, **overrides):
        kwargs = dict(confirmation_plan=self.plan_path, confirmation_plan_sha256=self.plan_hash,
                      runtime_manifest=self.runtime)
        kwargs.update(overrides)
        return runner.load_case(self.case_path, **kwargs)

    def test_actual_schema_preserves_identity_and_maps_execution_without_writes(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        case = self.load()
        self.assertEqual(case["schema_version"], runner.CONFIRMATION_CASE_SCHEMA)
        self.assertEqual(case["resume_key"], self.case["case_id"])
        self.assertEqual(case["settings"], self.case["settings"])
        self.assertEqual((case["concurrency"], case["per_case_deadline_seconds"]), (1, 5400))
        self.assertEqual(case["roles"], ["configuration_confirmation"])
        self.assertEqual(case["steps"], [])
        self.assertIsNone(case["variation"])
        self.assertEqual(case["confirmation_binding"]["deadline_source"], "panel.source_case_spec.per_case_deadline_seconds")
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(runner._result(case, "completed")["confirmation"]["binding"], case["confirmation_binding"])

    def test_all_96_declared_coordinates_load(self):
        for row in self.plan["cases"]:
            with self.subTest(case_id=row["candidate_case_id"]):
                self.case_path.write_bytes((self.plan_path.parent / row["case_spec"]["path"]).read_bytes())
                self.assertEqual(self.load()["case_id"], row["candidate_case_id"])

    def test_plan_anchor_sidecar_and_staged_bytes_are_required(self):
        for kwargs in ({"confirmation_plan": None}, {"confirmation_plan_sha256": None},
                       {"confirmation_plan_sha256": "0" * 64}):
            with self.subTest(kwargs=kwargs), self.assertRaises(runner.CaseRunnerError):
                self.load(**kwargs)
        self.case_path.write_text(self.case_path.read_text() + "\n")
        with self.assertRaisesRegex(runner.CaseRunnerError, "staged confirmation case bytes"):
            self.load()
        self.seal()
        Path(str(self.plan_path) + ".sha256").write_text("tampered\n")
        with self.assertRaisesRegex(runner.CaseRunnerError, "tampered"):
            self.load()

    def test_resealed_foreign_case_is_not_a_member(self):
        self.case["case_id"] = self.case["resume_key"] = "configuration-confirmation-case-v1:" + "f" * 64
        self.seal()
        with self.assertRaisesRegex(runner.CaseRunnerError, "not a declared member"):
            self.load()

    def test_duplicate_and_incomplete_panel_rejected(self):
        original = copy.deepcopy(self.plan["cases"])
        for rows in (original[:-1], [original[0], *original[0:-1]]):
            self.plan["cases"] = rows
            self.seal()
            with self.assertRaises(runner.CaseRunnerError):
                self.load()

    def test_settings_and_lineage_cannot_be_changed_by_resealing_case(self):
        for key, value in (("task_sha256", "f" * 64), ("source_manifest_sha256", "f" * 64),
                           ("source_case_spec_sha256", "f" * 64), ("case_spec_sha256", "f" * 64),
                           ("candidate_id", "unknown"), ("outcomes_accessed", True)):
            with self.subTest(key=key):
                original = self.case[key]
                self.case[key] = value
                self.seal()
                with self.assertRaises(runner.CaseRunnerError):
                    self.load()
                self.case[key] = original
        self.case["settings"]["call_limit"] = 999
        self.seal()
        with self.assertRaisesRegex(runner.CaseRunnerError, "settings/hash mismatch"):
            self.load()

    def test_source_spec_semantic_hash_is_recomputed(self):
        self.panel["panel"]["instances"][0]["source_case_spec"]["task_sha256"] = "c" * 64
        self.seal()
        with self.assertRaisesRegex(runner.CaseRunnerError, "source case hash lineage"):
            self.load()

    def test_runtime_pin_legacy_mode_and_deadline_mismatch_rejected(self):
        baseline = copy.deepcopy(self.runtime)
        mutations = [lambda m: m["pins"].update(swe_agent_revision="e" * 40),
                     lambda m: m["runner"].update(telemetry=runner._legacy_telemetry_config()),
                     lambda m: m["deadlines"].update(per_case_seconds=5399)]
        for mutate in mutations:
            self.runtime = copy.deepcopy(baseline)
            mutate(self.runtime)
            with self.subTest(mutation=mutate), self.assertRaises(runner.CaseRunnerError):
                self.load()

    def test_absent_source_deadline_uses_explicit_runtime_deadline(self):
        self.build_bundle(deadline=None)
        self.runtime["deadlines"]["per_case_seconds"] = 1234
        case = self.load()
        self.assertEqual(case["per_case_deadline_seconds"], 1234)
        self.assertEqual(case["confirmation_binding"]["deadline_source"], "runtime.deadlines.per_case_seconds")
        for bad in (None, 0, True, 1.5):
            self.runtime["deadlines"]["per_case_seconds"] = bad
            with self.subTest(deadline=bad), self.assertRaises(runner.CaseRunnerError):
                self.load()

    def test_request_hash_settings_and_path_escape_rejected(self):
        ref = self.plan["cases"][0]["request_config"]
        request_path = self.plan_path.parent / ref["path"]
        request = json.loads(request_path.read_text())
        request["agent"]["model"]["completion_kwargs"]["seed"] = 1
        write_json(request_path, request)
        with self.assertRaisesRegex(runner.CaseRunnerError, "request config SHA-256 mismatch"):
            self.load()
        ref["sha256"] = runner.sha256_file(request_path)
        self.seal()
        with self.assertRaisesRegex(runner.CaseRunnerError, "request config seed"):
            self.load()
        outside = self.root / "outside.json"
        ref["sha256"] = write_json(outside, request)
        ref["path"] = str(outside)
        self.seal()
        with self.assertRaisesRegex(runner.CaseRunnerError, "escapes the planning snapshot"):
            self.load()

    def test_cli_validate_only_maps_case_and_records_binding_without_launch(self):
        self.runtime["runner"]["cpu_policy"] = runner.cpu_policy.policy_config("00")
        manifest_hash = write_json(self.manifest_path, self.runtime)
        Path(str(self.manifest_path) + ".sha256").write_text(f"{manifest_hash}  {self.manifest_path.name}\n")
        args = runner.parser().parse_args([
            "--case-spec", str(self.case_path), "--output-dir", str(self.case_path.parent),
            "--runtime-manifest", str(self.manifest_path), "--confirmation-plan", str(self.plan_path),
            "--confirmation-plan-sha256", self.plan_hash, "--cpu-docker", "--validate-only",
        ])
        # Files, plan binding, schema mapping and CLI are real. Machine/source
        # preflights are isolated so this test needs no Docker or model service.
        with patch.object(runner, "load_manifest", return_value=self.runtime), \
             patch.object(runner, "validate_checkout", return_value=(ROOT, {})), \
             patch.object(runner, "_verify_execution_integrity", return_value={}), \
             patch.object(runner, "validate_static_environment", return_value={}) as static, \
             patch.object(runner, "run_sweagent", side_effect=AssertionError("must not launch")), \
             patch.object(runner, "_start_request_proxy", side_effect=AssertionError("must not launch")):
            self.assertEqual(runner.execute(args), 0)
            self.assertEqual(static.call_args.args[1]["schema_version"], runner.CONFIRMATION_CASE_SCHEMA)
        validation = json.loads((self.case_path.parent / "validation.json").read_text())
        self.assertEqual(validation["confirmation_binding"]["plan_sha256"], self.plan_hash)
        self.assertEqual(validation["cpu_policy"]["worker_cpuset"], "0")
        self.assertIsNone(validation["control_affinity_cpus"])

    def test_execute_resolves_runtime_fallback_before_inherited_deadline(self):
        self.build_bundle(deadline=None)
        self.runtime["deadlines"]["per_case_seconds"] = 1234
        manifest_hash = write_json(self.manifest_path, self.runtime)
        Path(str(self.manifest_path) + ".sha256").write_text(f"{manifest_hash}  {self.manifest_path.name}\n")
        args = runner.parser().parse_args([
            "--case-spec", str(self.case_path), "--output-dir", str(self.case_path.parent),
            "--runtime-manifest", str(self.manifest_path), "--confirmation-plan", str(self.plan_path),
            "--confirmation-plan-sha256", self.plan_hash, "--execute",
        ])
        inherited = runner.deadline_with_timeout(10)

        def dispatch(_args):
            self.assertEqual(runner.deadline_from_env(), inherited)
            return 0

        with runner.deadline_environment(inherited), \
             patch.object(runner, "load_manifest", return_value=self.runtime), \
             patch.object(runner, "deadline_with_timeout", wraps=runner.deadline_with_timeout) as deadline, \
             patch.object(runner, "_execute", side_effect=dispatch):
            self.assertEqual(runner.execute(args), 0)
            deadline.assert_called_once_with(1234)


if __name__ == "__main__":
    unittest.main()
