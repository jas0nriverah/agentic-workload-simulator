from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.assignment.render_worker_runtime_inputs import (
    EXPECTED_WORKER_IDS,
    RenderError,
    prepare_inputs,
    write_plan,
)


class RenderWorkerRuntimeInputsTest(unittest.TestCase):
    def _fingerprints(self, *, duplicate_uuid: bool = False) -> dict[str, object]:
        nodes = []
        for worker_number, worker_id in enumerate(EXPECTED_WORKER_IDS):
            uuid = f"GPU-test-{worker_id}"
            if duplicate_uuid and worker_id == "01":
                uuid = "GPU-test-00"
            remote_port = 18200 + int(worker_id)
            job_id = str(900000 + worker_number)
            alias = "Qwen3-Coder-30B-A3B-Instruct" if worker_id == "09" else "Qwen/Qwen3-Coder-30B-A3B-Instruct"
            options = {
                "--dtype": "bfloat16",
                "--gpu-memory-utilization": "0.90",
                "--host": "127.0.0.1",
                "--max-model-len": "32768",
                "--model": "/models/Qwen3-Coder-30B-A3B-Instruct",
                "--port": str(remote_port),
                "--served-model-name": alias,
                "--tensor-parallel-size": "1",
            }
            nodes.append(
                {
                    "job_id": job_id,
                    "node": f"node-{worker_id}",
                    "returncode": 0,
                    "inventory": {
                        "boot_id": f"boot-{worker_id}",
                        "hostname": f"node-{worker_id}.example",
                        "gpus": {
                            "argv": ["nvidia-smi", "--query-gpu=fixture"],
                            "returncode": 0,
                            "stdout": f"0, {uuid}, NVIDIA H100 80GB HBM3, 81559, 595.71.05, 0000:00:00.0, 9.0, 1980, 2619, 700.00, 40\n",
                            "stderr": "",
                        },
                        "compute_processes": {
                            "argv": ["nvidia-smi", "--query-compute-apps=fixture"],
                            "returncode": 0,
                            "stdout": f"{7000 + worker_number}, {uuid}, 74406\n",
                            "stderr": "",
                        },
                        "serving_processes": [
                            {
                                "cgroup": f"0::/fixture/{worker_id}",
                                "cuda_visible_devices": "0",
                                "options": options,
                                "pid": 8000 + worker_number,
                                "slurm_job_id": job_id,
                                "start_ticks": 100000 + worker_number,
                            }
                        ],
                    },
                }
            )
        return {
            "captured_at": "2026-09-09T00:00:00+00:00",
            "nodes": nodes,
            "probe_scope": "existing_allocation_overlap_step",
            "probe_source_sha256": "fixture-source",
            "schema_version": "assignment.worker-fingerprint-discovery.v1",
            "scope": "fixture",
        }

    def _runtime_template(self) -> dict[str, object]:
        return {
            "schema_version": "assignment-runtime-manifest.v1",
            "required_branch": "example",
            "required_commit": "0" * 40,
            "repository_root": "/example",
            "pins": {
                "model_revision": "old",
                "tokenizer_revision": "old",
            },
            "model": {
                "name": "example",
                "revision": "old",
                "api_base": "http://127.0.0.1:8000/v1",
                "api_key": "EMPTY",
            },
            "runner": {"telemetry": {}},
            "hardware": {"probe_command": ["nvidia-smi"]},
        }

    def test_emits_exact_bindings_and_not_ready_templates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fingerprints_path = root / "fingerprints.json"
            template_path = root / "runtime.example.json"
            cpu_path = root / "cpu.json"
            fingerprints_path.write_text(json.dumps(self._fingerprints()), encoding="utf-8")
            template_path.write_text(json.dumps(self._runtime_template()), encoding="utf-8")
            cpu_path.write_text(
                json.dumps({
                    "schema_version": "local-cpu-inventory.v1",
                    "profile": {"architecture": "x86_64", "frequency_khz": None},
                    "raw": {"proc_cpuinfo": "raw-cpu"},
                }),
                encoding="utf-8",
            )

            plan = prepare_inputs(fingerprints_path, template_path, cpu_path)
            index = write_plan(plan, root / "out")

            self.assertEqual(index["worker_ids"], list(EXPECTED_WORKER_IDS))
            self.assertFalse(index["launchable"])
            self.assertEqual(index["readiness"]["observed_serving_max_model_len"], 32768)
            self.assertEqual(index["readiness"]["required_target_max_model_len"], 65536)
            self.assertEqual(index["excluded_allocations"], [])

            profile = json.loads(
                (root / "out/hardware-profiles/worker-09.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["identity"]["gpu_uuid"], "GPU-test-09")
            self.assertEqual(profile["identity"]["local_port"], 18109)
            self.assertEqual(profile["identity"]["remote_port"], 18209)
            self.assertEqual(profile["identity"]["model_alias"], "Qwen3-Coder-30B-A3B-Instruct")
            self.assertEqual(profile["gpu"]["observed_clocks_mhz"], {"sm": 1980, "memory": 2619})
            self.assertNotIn("gpu_memory_bandwidth_gbps", profile)
            self.assertNotIn("cpu_frequency_ghz", profile)
            self.assertFalse(profile["cpu_vm_profile"]["embedded"])

            template = json.loads(
                (root / "out/runtime-templates/worker-09.json").read_text(encoding="utf-8")
            )
            self.assertFalse(template["launchable"])
            self.assertEqual(template["endpoints"]["local"]["base_url"], "http://127.0.0.1:18109/v1")
            self.assertEqual(template["endpoints"]["remote"]["port"], 18209)
            self.assertEqual(template["allocation_identity"]["model_alias"], "Qwen3-Coder-30B-A3B-Instruct")
            self.assertEqual(template["observed_serving"]["max_model_len"], 32768)
            self.assertIn("source_65k_binding_pending", template["readiness"]["reason_codes"])
            self.assertIn("<PENDING_REMOTE_FIXED_MODEL_REVISION>", json.dumps(template))
            self.assertFalse((root / "out/runtime_manifest.json").exists())

            cpu_binding = json.loads(
                (root / "out/cpu_vm_profile_binding.json").read_text(encoding="utf-8")
            )
            self.assertFalse(cpu_binding["embedded_in_gpu_profiles"])
            self.assertEqual(cpu_binding["raw_manifest"]["profile"]["frequency_khz"], None)

    def test_duplicate_gpu_uuid_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fingerprints_path = root / "fingerprints.json"
            template_path = root / "runtime.example.json"
            fingerprints_path.write_text(
                json.dumps(self._fingerprints(duplicate_uuid=True)), encoding="utf-8"
            )
            template_path.write_text(json.dumps(self._runtime_template()), encoding="utf-8")

            with self.assertRaisesRegex(RenderError, "GPU UUID binding is not unique"):
                prepare_inputs(fingerprints_path, template_path)


if __name__ == "__main__":
    unittest.main()
