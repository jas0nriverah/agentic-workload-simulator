import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.analysis.feature_validation import ValidationError, load_protocol, seal
from scripts.cloud import a100_execution as execution
from scripts.validation.verify_preservation import MANIFEST, ROOT, verify


class PreservationTests(unittest.TestCase):
    def test_original_602_results_and_1088_identities(self):
        self.assertEqual(verify(), {"total": 1088, "completed": 602, "remaining": 486,
                                   "overlap": 0, "preserved_hashes_match": True})

    def test_changed_result_and_ledger_are_rejected(self):
        baseline = json.loads((ROOT / MANIFEST).read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Mutate copies only; never touch the original evidence.
            for record in baseline["files"] + [{"path": MANIFEST}]:
                target = root / record["path"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / record["path"], target)
            row = next(row for row in baseline["cases"] if row["status"] == "completed")
            for name in (row["result"]["path"], row["ledger"]):
                with self.subTest(path=name):
                    path = root / name
                    original = path.read_bytes()
                    path.write_bytes(original + b" ")
                    with self.assertRaisesRegex(ValueError, "changed"):
                        verify(root)
                    path.write_bytes(original)

    def test_current_state_matches_snapshot_and_historical_notices(self):
        state = json.loads((ROOT / "project/CURRENT_STATE.json").read_text())
        counts = verify()
        self.assertEqual(state["experiment"]["total"], counts["total"])
        self.assertEqual(state["experiment"]["completed_preserved"], counts["completed"])
        self.assertEqual(state["experiment"]["remaining_failed"], counts["remaining"])
        self.assertIs(state["experiment"]["execution_authorized"], False)
        evidence = json.loads((ROOT / state["validation"]["h100"]["evidence"]).read_text())
        self.assertEqual(evidence["status"], "completed")
        self.assertEqual(evidence["integrity_status"], state["validation"]["h100"]["integrity_status"])
        for name in state["historical_documents"]:
            notice = "\n".join((ROOT / name).read_text().splitlines()[:3]).lower()
            self.assertIn("historical", notice)
            self.assertIn("current_state.json", notice)

    def test_default_root_discovery_excludes_broken_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "pyproject.toml", root / "pyproject.toml")
            (root / "tests").mkdir()
            (root / "tests/test_active.py").write_text("def test_active(): pass\n")
            (root / "project/archive").mkdir(parents=True)
            (root / "project/archive/test_bad.py").write_text("invalid python !!!")
            (root / "project/archive/test_output.txt").write_text(">>> invalid python !!!\n")
            env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
            result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q"],
                                    cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 test collected", result.stdout)
            self.assertNotIn("test_bad", result.stdout)


class ResumeSealTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "config.json"
        shutil.copyfile(execution.CONFIG, self.config)
        self.config_patch = patch.object(execution, "CONFIG", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        seal(self.config, self.root)
        _, protocol_hash, split_hash = load_protocol(self.config)
        (self.root / "derived").mkdir()
        self.prediction = self.root / "derived/prediction_manifest.json"
        self.prediction.write_text(json.dumps({"protocol_sha256": protocol_hash,
                                              "split_manifest_sha256": split_hash,
                                              "predictions": []}))
        self.sidecar = self.root / "derived/prediction_manifest.sha256"
        self.update_sidecar()
        execution.load_or_create_state(self.root, "all", 14400, False)
        execution.prove_before_reveal(self.root)

    def update_sidecar(self):
        self.sidecar.write_text(hashlib.sha256(self.prediction.read_bytes()).hexdigest()
                                + "  prediction_manifest.json\n")

    def test_unchanged_resume_accepts_and_does_not_replace_proof(self):
        proof = self.root / "pre_reveal_proof.json"
        original = proof.read_bytes()
        execution.load_or_create_state(self.root, "holdout", 14400, True)
        execution.prove_before_reveal(self.root)
        self.assertEqual(proof.read_bytes(), original)

    def test_prediction_tampering_even_with_updated_sidecar_rejected(self):
        self.prediction.write_text(self.prediction.read_text() + " ")
        self.update_sidecar()
        with self.assertRaisesRegex(RuntimeError, "pre-reveal proof"):
            execution.load_or_create_state(self.root, "holdout", 14400, True)
        with self.assertRaises(RuntimeError):
            execution.prove_before_reveal(self.root)

    def test_protocol_and_split_bytes_tampering_rejected_before_execution(self):
        for path in (self.config, self.root / "protocol.config.json",
                     self.root / "split_manifest.json", self.sidecar):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + (b"x" if path == self.sidecar else b" "))
                with self.assertRaises(RuntimeError):
                    execution.load_or_create_state(self.root, "holdout", 14400, True)
                path.write_bytes(original)

    def test_split_case_ids_cannot_change_while_retaining_declared_hash(self):
        path = self.root / "split_manifest.json"
        data = json.loads(path.read_text())
        data["sealed_holdout_case_ids"][0] = "replacement-case"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "split/case"):
            execution.verify_before_reveal(self.root)
        with self.assertRaisesRegex(ValidationError, "split manifest"):
            seal(self.config, self.root)

    def test_legacy_proof_cannot_authorize_resume(self):
        path = self.root / "pre_reveal_proof.json"
        proof = json.loads(path.read_text())
        proof["schema_version"] = "a100-pre-reveal-proof.v1"
        path.write_text(json.dumps(proof))
        with self.assertRaisesRegex(RuntimeError, "pre-reveal proof"):
            execution.load_or_create_state(self.root, "holdout", 14400, True)

    def test_driver_rejects_tampering_before_any_analysis_or_rows(self):
        manifest = self.root / "startup.env"
        manifest.write_text(f"ARTIFACT_ROOT={self.root}\nMAX_WALL_CLOCK_SECONDS=14400\n")
        self.prediction.write_text(self.prediction.read_text() + " ")
        with patch.object(execution, "run_rows") as rows, patch.object(execution, "run_analysis") as analysis:
            with self.assertRaises(RuntimeError):
                execution.main(["--manifest", str(manifest), "--resume", "--phase", "all"])
            rows.assert_not_called()
            analysis.assert_not_called()
