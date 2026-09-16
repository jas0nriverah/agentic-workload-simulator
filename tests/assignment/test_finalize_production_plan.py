import hashlib
import importlib.util
import io
import json
import shutil
import tempfile
import tarfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/finalize_production_plan.py"
SPEC = importlib.util.spec_from_file_location("finalize_production_plan", SCRIPT)
assert SPEC and SPEC.loader
FINALIZER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FINALIZER)

from scripts.assignment import run_matrix  # noqa: E402


SNAPSHOT = (
    ROOT.parent
    / "h100-assignment-work-20260905"
    / "assignment"
    / "submission"
    / "20260908T140000Z-offline-v2"
)
CANDIDATE_ID = "expanded-call100-input61440"
REVIEW_FIELDS = sorted(FINALIZER.REVIEW_FIELDS)


def _write_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecar(path: Path) -> None:
    Path(f"{path}.sha256").write_text(f"{_sha(path)}  {path.name}\n", encoding="ascii")


@unittest.skipUnless(
    (SNAPSHOT / "live-plan/production-candidates" / f"{CANDIDATE_ID}.jsonl").is_file(),
    "offline production candidate inventory is unavailable",
)
class FinalizeProductionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="production-finalizer-")
        self.root = Path(self.temporary.name)
        source = SNAPSHOT / "live-plan/production-candidates"
        self.inventory = self.root / f"{CANDIDATE_ID}.jsonl"
        self.config = self.root / f"{CANDIDATE_ID}.config.json"
        shutil.copy2(source / self.inventory.name, self.inventory)
        shutil.copy2(source / self.config.name, self.config)
        self.contract = self.root / "acquisition-contract.json"
        shutil.copy2(ROOT / "configs/assignment_acquisition_contract.v2.json", self.contract)

        header, _cases = run_matrix.load_plan(self.inventory)
        self.header = header
        self.settings = header["candidate_final_configuration"]
        self.runtime = _write_json(self.root / "runtime.json", {
            "schema_version": "assignment-runtime-manifest.v1",
            "pins": dict(header["pins"] | {}),
            "model": {"name": header["pins"]["model"], "revision": header["pins"]["model_revision"]},
        })
        self.hardware = _write_json(self.root / "hardware.json", {
            "schema_version": "assignment.hardware-profile.v1",
            "hardware_id": "synthetic-test-only-h100",
            "gpu_count": 1,
        })
        archive = self.root / "repair-source.tar.gz"
        member_payload = b"synthetic source member for finalizer tests\n"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("synthetic.txt")
            member.size = len(member_payload)
            bundle.addfile(member, io.BytesIO(member_payload))
        archive_digest = _sha(archive)
        self.source_bundle = _write_json(self.root / "source_manifest.json", {
            "schema_version": "assignment.offline-source-bundle.v1",
            "repository": "test-only",
            "git_head": "0" * 40,
            "worktree_dirty_at_sealing": True,
            "test_only": True,
            "evidence_kind": "offline_test_fixture",
            "bundle": archive.name,
            "bundle_sha256": archive_digest,
            "files": [{"path": "synthetic.txt", "size_bytes": len(member_payload), "sha256": hashlib.sha256(member_payload).hexdigest()}],
        })
        self.acquisition = _write_json(self.root / "acquisition.json", {
            "schema_version": "assignment.acquisition-proof.v2",
            "status": "pass",
            "launch_authorized": False,
            "test_only": True,
            "evidence_kind": "offline_test_fixture",
            "contract_sha256": _sha(self.contract),
            "source_bundle_sha256": archive_digest,
            "outcomes_accessed": False,
            "requirements": [
                {"id": f"A{index:02d}", "status": "pass", "artifact_roles": ["test-fixture"]}
                for index in range(1, 16)
            ],
        })
        self.regression = _write_json(self.root / "regression.json", {
            "schema_version": "assignment.historical-regression-proof.v2",
            "status": "pass",
            "launch_authorized": False,
            "test_only": True,
            "evidence_kind": "offline_test_fixture",
            "contract_sha256": _sha(self.contract),
            "source_bundle_sha256": archive_digest,
            "outcomes_accessed": False,
            "regressions": [
                {"id": f"R{index:02d}", "status": "pass", "artifact_roles": ["test-fixture"]}
                for index in range(1, 22)
            ],
        })
        self.pilot = _write_json(self.root / "pilot.json", {
            "schema_version": "assignment.instrumentation-pilot-evidence.v2",
            "status": "passed",
            "launch_authorized": False,
            "test_only": True,
            "evidence_kind": "offline_test_fixture",
            "frozen_pilot_configuration": dict(self.settings),
            "selected_case_ids": [f"synthetic-pilot:{index:02d}" for index in range(16)],
            "review": {field: True for field in REVIEW_FIELDS},
            "source_bundle_sha256": archive_digest,
            "outcomes_accessed": False,
        })
        self.selection = _write_json(self.root / "selection.json", {
            "schema_version": "assignment-production-candidate-selection.v1",
            "status": "selected",
            "test_only": True,
            "evidence_kind": "offline_test_fixture",
            "plan_id": header["plan_id"],
            "candidate_id": header["candidate_id"],
            "selected_settings": dict(self.settings),
            "candidate_inventory_sha256": _sha(self.inventory),
            "candidate_config_sha256": _sha(self.config),
            "outcomes_accessed": False,
        })
        for path in (self.runtime, self.hardware, self.source_bundle, self.acquisition, self.regression, self.pilot, self.selection, self.contract):
            _sidecar(path)

    def tearDown(self):
        self.temporary.cleanup()

    def _finalize(self, *, allow_test_fixtures=True, **overrides):
        args = {
            "candidate_inventory": self.inventory,
            "candidate_config": self.config,
            "selection_record": self.selection,
            "runtime_manifest": self.runtime,
            "remote_hardware_profile": self.hardware,
            "acquisition_contract": self.contract,
            "acquisition_proof": self.acquisition,
            "historical_regression_proof": self.regression,
            "pilot_evidence": self.pilot,
            "source_bundle": self.source_bundle,
            "output_plan": self.root / "frozen.jsonl",
            "receipt": self.root / "freeze-receipt.json",
            "allow_test_fixtures": allow_test_fixtures,
        }
        args.update(overrides)
        return FINALIZER.finalize_production_plan(**args)

    def test_freeze_and_scheduler_validation_use_the_same_contract(self):
        result = self._finalize()
        self.assertTrue(result["test_only"])
        self.assertFalse(result["launch_authorized"])
        self.assertTrue(result["launch_review_required"])
        self.assertEqual(result["case_count"], 1088)
        frozen_header, frozen_cases = run_matrix.load_plan(self.root / "frozen.jsonl")
        self.assertEqual(frozen_header["status"], "frozen_for_execution")
        self.assertEqual(frozen_header["final_configuration_status"], "selected")
        self.assertEqual(len(frozen_cases), 1088)
        self.assertEqual(frozen_header["execution_binding"]["candidate_id"], CANDIDATE_ID)
        self.assertTrue(frozen_header["execution_binding"]["test_only"])
        self.assertEqual(_sha(self.root / "frozen.jsonl"), result["output_plan_sha256"])
        # A test-only freeze is structurally load-valid, but the scheduler
        # must refuse to execute it even with all ordinary CLI inputs.
        args = run_matrix.parser().parse_args([
            "--plan", str(self.root / "frozen.jsonl"),
            "--runner", str(run_matrix.REVIEWED_RUNNER),
            "--runtime-manifest", str(self.runtime),
            "--config", str(self.config),
            "--config-sha256", _sha(self.config),
            "--output-dir", str(self.root / "matrix-output"),
        ])
        with self.assertRaisesRegex(run_matrix.ExecutionError, "test-only frozen"):
            run_matrix.execute(args)

    def test_missing_proof_is_rejected(self):
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "acquisition proof"):
            self._finalize(acquisition_proof=self.root / "missing.json")

    def test_failed_proof_is_rejected(self):
        failed = json.loads(self.acquisition.read_text(encoding="utf-8"))
        failed["status"] = "pending"
        failed_path = _write_json(self.root / "failed-acquisition.json", failed)
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "not a successful"):
            self._finalize(acquisition_proof=failed_path)

    def test_hash_mismatch_is_rejected(self):
        self.config.write_text(self.config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "config_sha256"):
            self._finalize()

    def test_unselected_candidate_is_rejected(self):
        wrong = json.loads(self.selection.read_text(encoding="utf-8"))
        wrong["candidate_id"] = "historical-control-call30-input32768"
        wrong_path = _write_json(self.root / "wrong-selection.json", wrong)
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "different candidate"):
            self._finalize(selection_record=wrong_path)

    def test_test_only_evidence_requires_explicit_test_switch(self):
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "allow-test-fixtures"):
            self._finalize(allow_test_fixtures=False)

    def test_one_capture_with_only_acquisition_reviews_can_freeze_without_history(self):
        pilot = json.loads(self.pilot.read_text())
        pilot["selected_case_ids"] = ["synthetic-capture:one"]
        self.assertEqual(set(pilot["review"]), FINALIZER.check_instrumentation_pilot.ACQUISITION_REVIEW_FIELDS)
        _write_json(self.pilot, pilot)
        result = self._finalize(historical_regression_proof=None)
        header, cases = run_matrix.load_plan(Path(result["output_plan"]))
        self.assertNotIn("historical_regression_proof", header["execution_binding"])
        self.assertEqual(len(cases), 1088)
        self.assertFalse(any(action.required for action in FINALIZER._parser()._actions if action.dest == "historical_regression_proof"))

    def test_failed_partial_history_is_preserved_and_rehashed_by_scheduler(self):
        proof = json.loads(self.regression.read_text())
        proof.update(status="fail", regressions=[{"id": "R01", "status": "fail"}], source_bundle_sha256="f" * 64)
        _write_json(self.regression, proof)
        expected = _sha(self.regression)
        result = self._finalize()
        header, _ = run_matrix.load_plan(Path(result["output_plan"]))
        self.assertEqual(header["execution_binding"]["historical_regression_proof"]["sha256"], expected)
        self.assertEqual(json.loads(self.regression.read_text()), proof)
        self.regression.write_text(self.regression.read_text() + "\n")
        with self.assertRaisesRegex(run_matrix.ExecutionError, "SHA-256 does not match"):
            run_matrix.load_plan(Path(result["output_plan"]))

    def test_supplied_missing_history_is_not_silently_discarded(self):
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "historical regression advisory"):
            self._finalize(historical_regression_proof=self.root / "missing.json")

    def test_acquisition_review_and_capture_identity_still_block(self):
        original = json.loads(self.pilot.read_text())
        mutations = [
            lambda pilot: pilot["review"].pop("evaluator_correctness_verified"),
            lambda pilot: pilot.update(selected_case_ids=[]),
            lambda pilot: pilot.update(selected_case_ids=["same", "same"]),
            lambda pilot: pilot.update(selected_case_ids=[""]),
            lambda pilot: pilot["frozen_pilot_configuration"].update(call_limit=999),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                pilot = json.loads(json.dumps(original))
                mutate(pilot)
                path = _write_json(self.root / f"bad-pilot-{index}.json", pilot)
                with self.assertRaises(FINALIZER.FinalizationError):
                    self._finalize(pilot_evidence=path)
        result = self._finalize()
        header, _ = run_matrix.load_plan(Path(result["output_plan"]))
        for index, mutate in enumerate(mutations):
            with self.subTest(scheduler=index):
                pilot = json.loads(json.dumps(original))
                mutate(pilot)
                _write_json(self.pilot, pilot)
                header["execution_binding"]["pilot_evidence"]["sha256"] = _sha(self.pilot)
                with self.assertRaisesRegex(run_matrix.ExecutionError, "pilot evidence"):
                    run_matrix._validate_production_execution_binding(header)

    def test_dirty_live_source_uses_actual_archive_and_member_identity(self):
        bundle = json.loads(self.source_bundle.read_text())
        bundle.pop("test_only")
        bundle.pop("evidence_kind")
        bundle["git_head"] = "a" * 40
        _write_json(self.source_bundle, bundle)
        FINALIZER._validate_source_bundle(self.source_bundle, allow_test_fixtures=False)
        # Dirty ancestry is allowed; an incorrect member hash remains a defect.
        bundle["files"][0]["sha256"] = "b" * 64
        _write_json(self.source_bundle, bundle)
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "member hash mismatch"):
            FINALIZER._validate_source_bundle(self.source_bundle, allow_test_fixtures=False)

    def test_source_archive_and_member_tampering_rejected_in_both_paths(self):
        result = self._finalize()
        header, _ = run_matrix.load_plan(Path(result["output_plan"]))
        original = json.loads(self.source_bundle.read_text())
        for key, value in (("sha256", "c" * 64), ("size_bytes", 12345), ("path", "missing.txt")):
            with self.subTest(member=key):
                bundle = json.loads(json.dumps(original))
                bundle["files"][0][key] = value
                _write_json(self.source_bundle, bundle)
                header["execution_binding"]["source_bundle"]["sha256"] = _sha(self.source_bundle)
                with self.assertRaises(FINALIZER.FinalizationError):
                    FINALIZER._validate_source_bundle(self.source_bundle, allow_test_fixtures=True)
                with self.assertRaises(run_matrix.ExecutionError):
                    run_matrix._validate_production_execution_binding(header)
        _write_json(self.source_bundle, original)
        (self.root / original["bundle"]).write_bytes(b"changed archive")
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "archive does not match"):
            FINALIZER._validate_source_bundle(self.source_bundle, allow_test_fixtures=True)

    def test_historical_campaign_contract_rows_are_advisory(self):
        contract = json.loads(self.contract.read_text())
        contract.pop("historical_regressions")
        _write_json(self.contract, contract)
        FINALIZER._validate_contract(self.contract)
        contract["requirements"] = contract["requirements"][:-1]
        _write_json(self.contract, contract)
        with self.assertRaisesRegex(FINALIZER.FinalizationError, "requirement list"):
            FINALIZER._validate_contract(self.contract)

    def test_historical_synthetic_advisory_cannot_bind_live_plan(self):
        with self.assertRaisesRegex(run_matrix.ExecutionError, "test-only historical"):
            run_matrix._validate_advisory_regression(json.loads(self.regression.read_text()), test_only=False)


if __name__ == "__main__":
    unittest.main()
