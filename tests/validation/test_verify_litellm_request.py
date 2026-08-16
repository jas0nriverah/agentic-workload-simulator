import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "verify_litellm_request", ROOT / "scripts/validation/verify_litellm_request.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class LiteLLMRequestTests(unittest.TestCase):
    def test_missing_linux_checkout_is_explicit_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            result = module.verify(Path(temp) / "missing")
        self.assertEqual(result["status"], "capability")
        self.assertNotEqual(result["status"], "pass")


if __name__ == "__main__":
    unittest.main()
