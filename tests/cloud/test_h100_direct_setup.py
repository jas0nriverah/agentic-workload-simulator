import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "scripts/cloud/h100_direct_setup.py"
MODEL = "Qwen/Qwen3-Coder-30B-A3B-Instruct"
REVISION = "b2cff646eb4bb1d68355c01b18ae02e7cf42d120"


def load_helper():
    spec = importlib.util.spec_from_file_location("h100_direct_setup", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class H100DirectSetupTests(unittest.TestCase):
    @staticmethod
    def _snapshot(root):
        cache = root / "cache"
        repo = cache / "hub" / "models--Qwen--Qwen3-Coder-30B-A3B-Instruct"
        blobs = repo / "blobs"
        snapshot = repo / "snapshots" / REVISION
        blobs.mkdir(parents=True)
        snapshot.mkdir(parents=True)

        config = b'{"model_type":"fixture"}\n'
        tokenizer = b'{"version":"fixture"}\n'
        weights = b"fixture-weights\n"
        (blobs / "config-hash").write_bytes(config)
        (blobs / "tokenizer-hash").write_bytes(tokenizer)
        (snapshot / "config.json").symlink_to("../../blobs/config-hash")
        (snapshot / "tokenizer.json").symlink_to("../../blobs/tokenizer-hash")
        (snapshot / "model.safetensors").write_bytes(weights)

        def sibling(name, data, *, lfs=False):
            return SimpleNamespace(
                rfilename=name,
                size=len(data),
                lfs=SimpleNamespace(sha256=hashlib.sha256(data).hexdigest()) if lfs else None,
            )

        siblings = [
            sibling("config.json", config),
            sibling("tokenizer.json", tokenizer),
            sibling("model.safetensors", weights, lfs=True),
        ]
        return cache, snapshot, siblings

    def test_snapshot_verification_accepts_huggingface_blob_links_and_is_deterministic(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as temp:
            _, snapshot, siblings = self._snapshot(Path(temp))
            first = helper.verify_snapshot(snapshot, siblings)
            second = helper.verify_snapshot(snapshot, list(reversed(siblings)))
            self.assertEqual(first, second)
            self.assertEqual(first["file_count"], 3)
            self.assertEqual(first["hash_checked_file_count"], 1)

    def test_snapshot_verification_rejects_links_outside_model_cache_entry(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, snapshot, siblings = self._snapshot(root)
            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            (snapshot / "escape.txt").symlink_to(outside)
            siblings.append(SimpleNamespace(rfilename="escape.txt", size=outside.stat().st_size, lfs=None))
            with self.assertRaisesRegex(helper.DirectSetupError, "escapes the model cache"):
                helper.verify_snapshot(snapshot, siblings)

    def test_matching_snapshot_is_reused_and_state_is_written_offline(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as temp:
            cache, snapshot, siblings = self._snapshot(Path(temp))
            state_path = Path(temp) / "state" / "direct_model.json"
            with patch.object(helper, "_verify_runtime", return_value={}), patch.object(
                helper, "_remote_siblings", return_value=siblings
            ), patch.object(
                helper,
                "_verify_tokenizer",
                return_value={"loaded": True, "class": "FixtureTokenizer", "revision": REVISION},
            ):
                result = helper.ensure_snapshot(
                    model=MODEL,
                    revision=REVISION,
                    model_cache=cache,
                    expected_snapshot=snapshot,
                    state_output=state_path,
                )
            self.assertTrue(result["reused_existing_snapshot"])
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["revision"], REVISION)

    def test_snapshot_path_must_be_exact_pinned_cache_path(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as temp:
            cache, snapshot, _ = self._snapshot(Path(temp))
            with self.assertRaises(helper.DirectSetupError):
                helper.ensure_snapshot(
                    model=MODEL,
                    revision=REVISION,
                    model_cache=cache,
                    expected_snapshot=snapshot.parent / "wrong-revision",
                )

    def test_future_downloads_use_the_established_hub_cache_layout(self):
        text = HELPER_PATH.read_text(encoding="utf-8")
        self.assertIn('cache_dir=str(model_cache / "hub")', text)
        self.assertIn('local_files_only=False', text)
        self.assertIn('revision=revision', text)


if __name__ == "__main__":
    unittest.main()
