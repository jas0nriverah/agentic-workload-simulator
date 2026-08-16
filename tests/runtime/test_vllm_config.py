import json
import tempfile
import unittest
from pathlib import Path

from agentic_sim.runtime.vllm_config import VLLMConfigError, resolve_vllm_config, validate_server_manifest


PINNED = {
    "VLLM_MODEL": "org/NonDefaultModel",
    "VLLM_MODEL_REVISION": "0123456789abcdef0123456789abcdef01234567",
    "VLLM_IMAGE": "registry.example/vllm:v0.10.0@sha256:" + "a" * 64,
    "VLLM_IMAGE_DIGEST": "sha256:" + "a" * 64,
    "VLLM_IMAGE_PLATFORM": "linux/amd64",
    "VLLM_VERSION": "0.10.0",
    "VLLM_TOOL_PARSER": "qwen3_coder",
    "VLLM_MAX_MODEL_LEN": "16384",
    "VLLM_HEALTH_CONTEXT": "4096",
    "VLLM_GPU_MEMORY_UTILIZATION": "0.75",
    "VLLM_TENSOR_PARALLEL_SIZE": "1",
    "VLLM_PORT": "8123",
}


class VLLMConfigTests(unittest.TestCase):
    def _manifest(self, values=PINNED):
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return Path(handle.name)

    def test_non_default_manifest_values_propagate(self):
        config = resolve_vllm_config(self._manifest(), environ={})
        self.assertEqual(config["model"], "org/NonDefaultModel")
        self.assertEqual(config["model_revision"], PINNED["VLLM_MODEL_REVISION"])
        self.assertEqual(config["image"], PINNED["VLLM_IMAGE"])
        self.assertEqual(config["max_model_len"], 16384)
        self.assertEqual(config["health_context"], 4096)
        self.assertEqual(config["gpu_memory_utilization"], 0.75)
        self.assertEqual(config["port"], 8123)

    def test_context_memory_parser_and_tp_are_validated(self):
        for key, value in (("VLLM_MAX_MODEL_LEN", "0"), ("VLLM_HEALTH_CONTEXT", "-1"), ("VLLM_TENSOR_PARALLEL_SIZE", "2"), ("VLLM_TOOL_PARSER", "auto")):
            values = dict(PINNED)
            values[key] = value
            with self.subTest(key=key), self.assertRaises(VLLMConfigError):
                resolve_vllm_config(self._manifest(values), environ={})
        values = dict(PINNED)
        values["VLLM_GPU_MEMORY_UTILIZATION"] = "1.0"
        with self.assertRaises(VLLMConfigError):
            resolve_vllm_config(self._manifest(values), environ={})

    def test_server_manifest_matches_resolved_values(self):
        config = resolve_vllm_config(self._manifest(), environ={})
        server = {
            "model": config["model"], "model_revision": config["model_revision"],
            "vllm_image": config["image"], "port": config["port"],
            "tool_parser": config["parser"], "max_model_len": config["max_model_len"],
            "gpu_memory_utilization": config["gpu_memory_utilization"],
            "tensor_parallel_size": config["tensor_parallel_size"],
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            json.dump(server, handle)
            path = Path(handle.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        validate_server_manifest(path, config)
        server["port"] = 9999
        path.write_text(json.dumps(server), encoding="utf-8")
        with self.assertRaises(VLLMConfigError):
            validate_server_manifest(path, config)


if __name__ == "__main__":
    unittest.main()
