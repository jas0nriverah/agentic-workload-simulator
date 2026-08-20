import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "inventory_sweagent_output", ROOT / "scripts/validation/inventory_sweagent_output.py"
)
assert SPEC and SPEC.loader
inventory_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory_module)


class InventoryTests(unittest.TestCase):
    def test_inventory_hashes_and_reports_json_types_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "trajectory.traj").write_bytes(b'{"step":1}\n')
            (root / "preds.json").write_bytes(b'[{"instance_id":"i1","model_patch":""}]\n')
            (root / "config.json").write_bytes(b'{"temperature":0.0,"enabled":true}\n')
            (root / "agent.log").write_bytes(b"trajectory output\n")
            (root / "status.json").write_bytes(b'{"status":"completed"}\n')
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            result = inventory_module.inventory(root, 1024)
            self.assertTrue(result["byte_preserving"])
            self.assertEqual(set(result["artifact_kinds"]), {"trajectory", "predictions", "config", "log", "status"})
            config = next(item for item in result["files"] if item["path"] == "config.json")
            self.assertEqual(config["json"]["top_level_keys"], ["enabled", "temperature"])
            trajectory = next(item for item in result["files"] if item["path"] == "trajectory.traj")
            self.assertEqual(trajectory["json"]["format"], "json")
            self.assertEqual(trajectory["json"]["records"], 1)
            self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)

    def test_inventory_rejects_secret_and_missing_required_kinds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "trajectory.traj").write_text('{"api_key":"' + "x" * 24 + '"}\n', encoding="utf-8")
            with self.assertRaises(inventory_module.CheckFailure):
                inventory_module.inventory(root, 1024)


if __name__ == "__main__":
    unittest.main()
