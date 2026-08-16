import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "rehearse_linux", ROOT / "scripts/validation/rehearse_linux.py"
)
assert SPEC and SPEC.loader
rehearse = importlib.util.module_from_spec(SPEC)
sys.modules["rehearse_linux"] = rehearse
SPEC.loader.exec_module(rehearse)


class RehearsalTests(unittest.TestCase):
    def test_frozen_command_contract_includes_current_explicit_mappings(self):
        values = rehearse.load_env(ROOT / "cloud/lambda/instance_manifest.env.example")
        self.assertEqual(rehearse.check_pins(values)["status"], "pass")
        result = rehearse.check_command_contract(values)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["knobs"]["--agent.model.completion_kwargs.max_tokens"], "2048"
        )
        self.assertEqual(result["knobs"]["--agent.model.completion_kwargs.seed"], "0")
        self.assertEqual(
            result["knobs"]["--agent.templates.max_observation_length"], "100000"
        )

    def test_command_contract_rejects_invented_split_and_floating_api_key(self):
        values = rehearse.load_env(ROOT / "cloud/lambda/instance_manifest.env.example")
        values["SWE_AGENT_TELEMETRY_COMMAND"] = values["SWE_AGENT_COMMAND"].replace(
            "--instances.filter", "--instances.split test --instances.filter"
        )
        with self.assertRaises(rehearse.CheckFailure):
            rehearse.check_command_contract(values)
        values = rehearse.load_env(ROOT / "cloud/lambda/instance_manifest.env.example")
        values["SWE_AGENT_COMMAND"] = values["SWE_AGENT_COMMAND"].replace(
            '"$VLLM_API_KEY"', "real-secret-value"
        )
        values["SWE_AGENT_TELEMETRY_COMMAND"] = values["SWE_AGENT_COMMAND"]
        with self.assertRaises(rehearse.CheckFailure):
            rehearse.check_command_contract(values)

    def test_pins_reject_unresolved_image_digest(self):
        values = rehearse.load_env(ROOT / "cloud/lambda/instance_manifest.env.example")
        values["VLLM_IMAGE_DIGEST"] = ""
        with self.assertRaises(rehearse.CheckFailure):
            rehearse.check_pins(values)

    def test_safe_extract_rejects_traversal_and_links(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo("../escape.txt")
                payload = b"escape"
                info.size = len(payload)
                handle.addfile(info, io.BytesIO(payload))
            with self.assertRaises(rehearse.CheckFailure):
                rehearse.safe_extract(archive, root / "out", 1024)

    def test_inventory_hashes_json_without_rewriting_and_scans_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            trajectory = root / "attempt"
            trajectory.mkdir()
            artifact = trajectory / "trajectory.traj"
            original = b'{"step": 1}\n'
            artifact.write_bytes(original)
            result = rehearse.inventory_trajectory(trajectory, 1024)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(artifact.read_bytes(), original)
            artifact.write_text('{"token": "' + "x" * 24 + '"}\n', encoding="utf-8")
            with self.assertRaises(rehearse.CheckFailure):
                rehearse.inventory_trajectory(trajectory, 1024)

    def test_dataset_manifest_checks_revision_and_selected_hashes(self):
        values = rehearse.load_env(ROOT / "cloud/lambda/instance_manifest.env.example")
        manifest = {
            "lite": {
                "repo": "SWE-bench/SWE-bench_Lite",
                "revision": rehearse.EXPECTED["lite_revision"],
                "rows": 300,
                "selected": [
                    {"instance_id": rehearse.EXPECTED["lite_first"], "sha256": rehearse.EXPECTED["lite_first_hash"]},
                    {"instance_id": rehearse.EXPECTED["lite_gold"], "sha256": rehearse.EXPECTED["lite_gold_hash"]},
                ],
            },
            "verified": {
                "repo": "SWE-bench/SWE-bench_Verified",
                "revision": rehearse.EXPECTED["verified_revision"],
                "rows": 500,
                "selected": [
                    {"instance_id": rehearse.EXPECTED["verified_gold"], "sha256": rehearse.EXPECTED["verified_gold_hash"]}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "datasets.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(rehearse.check_dataset_manifest(path, values)["status"], "pass")
            manifest["lite"]["selected"][0]["sha256"] = "0" * 64
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(rehearse.CheckFailure):
                rehearse.check_dataset_manifest(path, values)


if __name__ == "__main__":
    unittest.main()
