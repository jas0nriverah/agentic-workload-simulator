"""Offline parsing/provenance tests; no serving imports, queries or inference."""
import json
import unittest

from scripts.validation import serving_fingerprint as fp
from scripts.validation.collect_worker_fingerprints import build_serving_fingerprint


IDENTITY = {"hostname": "node.example", "boot_id": "boot", "server_pid": 123,
            "server_process_start_ticks": 456, "counter_epoch": "epoch-v4",
            "source_manifest_sha256": "a" * 64}
STARTUP = """INFO [api_server.py:1] vLLM API server version 0.10.0
INFO [config.py:2] Using max model len 65536
INFO [config.py:3] Chunked prefill is enabled with max_num_batched_tokens=8192.
INFO [core.py:4] Initializing a V1 LLM engine (v0.10.0) with config: model='/model', dtype=torch.bfloat16, max_seq_len=65536, tensor_parallel_size=1, pipeline_parallel_size=1, quantization=None, kv_cache_dtype=auto, enable_prefix_caching=True, chunked_prefill_enabled=True
INFO [serving_chat.py:5] Using default chat sampling params from model: {'repetition_penalty': 1.05, 'temperature': 0.7, 'top_k': 20, 'top_p': 0.8, 'api_key': 'SECRET'}
INFO [serving_completion.py:6] Using default completion sampling params from model: {'repetition_penalty': 1.05, 'top_k': 20}
"""


def receipt(**kwargs):
    return fp.build_receipt(identity=IDENTITY, argv=[], **kwargs)


class ExplicitArgvTests(unittest.TestCase):
    def test_worker_discovery_emits_partial_fingerprint_receipt(self):
        process = {
            "pid": 123,
            "start_ticks": 456,
            "allowlisted_argv": ["--max-model-len", "65536", "--enable-prefix-caching"],
            "cgroup": "0::/slurm/step_11/task_0",
        }
        receipt = build_serving_fingerprint(
            process,
            inventory={"hostname": "node.example", "boot_id": "boot"},
            source_sha256="b" * 64,
        )
        self.assertEqual(receipt["schema_version"], fp.SCHEMA)
        self.assertEqual(receipt["explicit_argv"]["max_model_len"]["value"], 65536)
        self.assertFalse(receipt["argv_capture_complete"])
        self.assertEqual(receipt["processes"]["api"]["pid"], 123)
        self.assertEqual(receipt["discovery_probe_source_sha256"], "b" * 64)

    def test_full_whitelist_and_secret_exclusion(self):
        argv = ["python", "-m", "vllm.entrypoints.openai.api_server", "--api-key", "SECRET",
                "--dtype=bfloat16", "--kv-cache-dtype", "auto", "--quantization", "fp8",
                "--tensor-parallel-size", "1", "--pipeline-parallel-size=2",
                "--max-model-len", "65536", "--enable-chunked-prefill=false",
                "--enable-prefix-caching", "--disable-log-stats",
                "--enable-prompt-tokens-details", "--generation-config", "/model",
                "--override-generation-config", '{"top_k":20,"repetition_penalty":1.05,"token":"SECRET"}']
        values = fp.parse_explicit_argv(argv)
        expected = {"dtype": "bfloat16", "kv_cache_dtype": "auto", "quantization": "fp8",
                    "tensor_parallel_size": 1, "pipeline_parallel_size": 2, "max_model_len": 65536,
                    "enable_chunked_prefill": False, "enable_prefix_caching": True,
                    "disable_log_stats": True, "enable_prompt_tokens_details": True,
                    "generation_config": "/model"}
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(values[key]["value"], value)
        self.assertEqual(values["override_generation_config"]["value"],
                         {"top_k": 20, "repetition_penalty": 1.05})
        self.assertNotIn("SECRET", json.dumps(values))

    def test_absent_is_not_false_or_a_default(self):
        values = fp.parse_explicit_argv([])
        for key in fp.PARAMETERS:
            with self.subTest(key=key):
                self.assertEqual(values[key], {"status": "unavailable", "reason": "flag_absent"})

    def test_partial_capture_does_not_establish_flag_absence(self):
        r = fp.build_receipt(identity=IDENTITY, argv=["--dtype=bfloat16"], argv_complete=False)
        self.assertEqual(r["explicit_argv"]["dtype"]["value"], "bfloat16")
        self.assertEqual(r["explicit_argv"]["enable_prefix_caching"]["reason"], "flag_not_in_partial_capture")
        self.assertFalse(r["argv_capture_complete"])

    def test_boolean_positive_negative_and_values(self):
        for flag, expected in [("--enable-prefix-caching", True),
                               ("--no-enable-prefix-caching", False),
                               ("--disable-prefix-caching", False),
                               ("--enable-prefix-caching=false", False)]:
            with self.subTest(flag=flag):
                self.assertEqual(fp.parse_explicit_argv([flag])["enable_prefix_caching"]["value"], expected)
        self.assertEqual(fp.parse_explicit_argv(["--no-enable-prefix-caching=false"])
                         ["enable_prefix_caching"]["status"], "unavailable")

    def test_repetition_conflict_and_end_of_options(self):
        for args in [["--max-model-len=32768", "--max-model-len=65536"],
                     ["--max-model-len=65536", "--max-model-len=65536"]]:
            with self.subTest(args=args):
                v = fp.parse_explicit_argv(args)["max_model_len"]
                self.assertEqual(v["status"], "unavailable")
                self.assertEqual(len(v["occurrences"]), 2)
        self.assertEqual(fp.parse_explicit_argv(["--", "--dtype=SECRET"])["dtype"]["reason"], "flag_absent")

    def test_malformed_does_not_echo_raw_values(self):
        for args, key in [(["--top-k=SECRET", "--port=SECRET"], "port"),
                          (["--dtype"], "dtype"),
                          (["--model=https://user:SECRET@host/model"], "model"),
                          (["--override-generation-config={SECRET"], "override_generation_config"),
                          (["--gpu-memory-utilization=nan"], "gpu_memory_utilization")]:
            with self.subTest(args=args):
                v = fp.parse_explicit_argv(args)
                self.assertEqual(v[key]["status"], "unavailable")
                self.assertNotIn("SECRET", json.dumps(v))


class SourceTests(unittest.TestCase):
    def test_literal_source_defaults_without_execution(self):
        text = """raise RuntimeError('must never run')
class EngineArgs:
    dtype: str = 'auto'
    tensor_parallel_size: int = 1
    enable_prefix_caching: bool = None
    kv_cache_dtype = resolve_dtype()
    disable_log_stats = False
    secret = 'SECRET'
cuda: str = '12.8'
__version__ = '2.7.1+cu128'
"""
        out = fp.literal_source_defaults(text, locator="/sealed/args.py", selectors={
            "dtype": "EngineArgs.dtype", "tensor_parallel_size": "EngineArgs.tensor_parallel_size",
            "enable_prefix_caching": "EngineArgs.enable_prefix_caching",
            "kv_cache_dtype": "EngineArgs.kv_cache_dtype", "disable_log_stats": "EngineArgs.disable_log_stats",
            "torch_cuda_build_version": "cuda", "torch_version": "__version__", "api_key": "EngineArgs.secret"})
        self.assertEqual(out["dtype"]["value"], "auto")
        self.assertEqual(out["dtype"]["source"]["sha256"], fp.digest(text))
        self.assertEqual(out["dtype"]["source"]["line"], 3)
        self.assertEqual(out["kv_cache_dtype"]["status"], "unavailable")
        self.assertIsNone(out["enable_prefix_caching"]["value"])
        self.assertEqual(out["torch_cuda_build_version"]["value"], "12.8")
        self.assertNotIn("SECRET", json.dumps(out))
        r = receipt(source_defaults=out)
        self.assertEqual(r["effective_serving_settings"]["dtype"]["status"], "unavailable")
        self.assertEqual(r["runtime_versions"]["torch_cuda_build_version"]["status"], "literal_source_default")
        self.assertEqual(r["runtime_versions"]["cuda_runtime_version"]["status"], "unavailable")

    def test_ambiguous_conditional_and_factory_defaults_are_unavailable(self):
        for text in ["x = True\nx = False", "if use_v1:\n    x = True", "x = field(default=True)",
                     "x = True\ntry:\n    run()\nexcept Exception:\n    x = False",
                     "def f():\n    x = True", "class Broken("]:
            with self.subTest(text=text):
                out = fp.literal_source_defaults(text, locator="source.py", selectors={"disable_log_stats": "x"})
                self.assertEqual(out["disable_log_stats"]["status"], "unavailable")

    def test_generation_source_and_override_mode_do_not_claim_application(self):
        g = fp.generation_config('{"top_k":20,"repetition_penalty":1.05,"api_key":"SECRET"}',
                                 locator="/model/generation_config.json", revision="b" * 40)
        r = fp.build_receipt(identity=IDENTITY, argv=["--generation-config=vllm"], generation=g)
        self.assertEqual(r["generation_config"]["settings"]["top_k"]["value"], 20)
        self.assertEqual(r["server_sampling_defaults"]["chat"]["top_k"]["status"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(fp.generation_config('[]', locator="x")["status"], "unavailable")

    def test_receipt_projects_nested_source_metadata(self):
        defaults = fp.literal_source_defaults("x = 'auto'", locator="/source.py", selectors={"dtype": "x"})
        defaults["dtype"]["source"]["environment"] = {"TOKEN": "SECRET"}
        defaults["dtype"]["full_argv"] = "SECRET"
        defaults["api_key"] = "SECRET"
        r = receipt(source_defaults=defaults)
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(r["source_defaults"]["dtype"]["value"], "auto")
        defaults["dtype"]["source"]["sha256"] = "invalid"
        self.assertEqual(receipt(source_defaults=defaults)["source_defaults"]["dtype"]["status"], "unavailable")


class StartupTests(unittest.TestCase):
    def test_actual_startup_settings_and_sampling_scopes(self):
        r = receipt(startup_text=STARTUP, startup_locator="launch.log", startup_identity=IDENTITY)
        expected = {"dtype": "bfloat16", "kv_cache_dtype": "auto", "quantization": None,
                    "tensor_parallel_size": 1, "pipeline_parallel_size": 1, "max_model_len": 65536,
                    "enable_chunked_prefill": True, "enable_prefix_caching": True,
                    "max_num_batched_tokens": 8192}
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(r["effective_serving_settings"][key]["value"], value)
                self.assertEqual(r["effective_serving_settings"][key]["status"], "startup_observed")
        self.assertEqual(r["server_sampling_defaults"]["chat"]["repetition_penalty"]["value"], 1.05)
        self.assertEqual(r["server_sampling_defaults"]["completion"]["top_k"]["value"], 20)
        self.assertEqual(r["server_sampling_defaults"]["responses"]["top_k"]["status"], "unavailable")
        self.assertNotIn("SECRET", json.dumps(r))
        self.assertEqual(r["effective_serving_settings"]["disable_log_stats"]["status"], "unavailable")

    def test_stale_or_missing_identity_cannot_resolve_active_settings(self):
        for field in fp.IDENTITY:
            with self.subTest(field=field):
                stale = dict(IDENTITY)
                stale[field] = "different"
                r = receipt(startup_text=STARTUP, startup_identity=stale)
                self.assertEqual(r["startup"]["identity_binding"], "unbound")
                self.assertEqual(r["effective_serving_settings"]["dtype"]["status"], "unavailable")
        self.assertEqual(receipt(startup_text=STARTUP)["startup"]["identity_binding"], "unbound")

    def test_conflicting_startup_not_last_value_wins(self):
        r = receipt(startup_text=STARTUP + "\nUsing max model len 32768\n", startup_identity=IDENTITY)
        self.assertEqual(r["effective_serving_settings"]["max_model_len"]["status"], "unavailable")

    def test_actual_observation_and_explicit_config_remain_separate(self):
        r = fp.build_receipt(identity=IDENTITY, argv=["--max-model-len=32768"],
                             startup_text=STARTUP, startup_identity=IDENTITY)
        self.assertEqual(r["explicit_argv"]["max_model_len"]["value"], 32768)
        self.assertEqual(r["effective_serving_settings"]["max_model_len"]["value"], 65536)
        r = fp.build_receipt(identity=IDENTITY, argv=["--dtype=auto"])
        self.assertEqual(r["effective_serving_settings"]["dtype"]["status"], "configured_argv_not_runtime_resolved")

    def test_truncated_engine_record_only_retains_complete_values(self):
        text = "Initializing a V1 LLM engine with config: dtype=torch.bfloat16, tensor_parallel_size=1, quantizati"
        v = fp.parse_startup(text, locator="retained.log")["settings"]
        self.assertEqual(v["dtype"]["value"], "bfloat16")
        self.assertNotIn("quantization", v)


class ProcessTests(unittest.TestCase):
    @staticmethod
    def stat(pid=123, ticks=456):
        return f"{pid} (python (worker)) S 1 " + "0 " * 17 + str(ticks) + " 0"

    def test_actual_affinity_and_environment_whitelist(self):
        r = fp.process_receipt(pid=123, start_ticks=456, stat=self.stat(),
            status="Name:\tpython\nCpus_allowed_list:\t24-31\n",
            environ=b"CUDA_VISIBLE_DEVICES=0\0SLURM_CPUS_PER_TASK=8\0AWS_SECRET_ACCESS_KEY=SECRET\0TOKEN=SECRET\0",
            cgroup="0::/slurm/step_11/task_0\n", cpuset_effective="24-31\n", cpu_max="max 100000\n",
            thread_statuses={"123": "Cpus_allowed_list:\t24-31\n", "124": "Cpus_allowed_list:\t24\n"})
        self.assertEqual(r["affinity"]["cpus"], list(range(24, 32)))
        self.assertEqual(r["threads"]["124"]["cpus"], [24])
        self.assertEqual(r["environment"], {"CUDA_VISIBLE_DEVICES": "0", "SLURM_CPUS_PER_TASK": "8"})
        self.assertNotIn("SECRET", json.dumps(r))
        r["environment"]["TOKEN"] = "SECRET"
        r["full_argv"] = "SECRET"
        assembled = receipt(processes={"api": r, "unknown_role": {"secret": "SECRET"}})
        self.assertNotIn("SECRET", json.dumps(assembled))
        self.assertEqual(assembled["processes"]["api"]["observed_thread_affinity_sets"], [list(range(24, 32)), [24]])

    def test_process_reuse_and_malformed_evidence(self):
        for stat in [self.stat(ticks=999), self.stat(pid=999), "corrupt"]:
            with self.subTest(stat=stat):
                p = fp.process_receipt(pid=123, start_ticks=456, stat=stat, status="Cpus_allowed_list:\t24-31")
                self.assertEqual(p["status"], "unavailable")
        p = fp.process_receipt(pid=123, start_ticks=456, stat=self.stat(), status="Cpus_allowed_list:\t31-24")
        self.assertEqual(p["affinity"]["status"], "unavailable")
        self.assertEqual(p["cpuset_effective"]["status"], "unavailable")
        self.assertEqual(p["threads"], {})

    def test_existing_worker_probe_reuse_and_api_binding(self):
        p = {"pid": 123, "start_ticks": 456, "affinity": list(range(24, 32)),
             "all_thread_affinity_sets": [list(range(24, 32))], "effective_cpuset": "24-31",
             "cpu_max": "max 100000", "runtime_environment": {"CUDA_VISIBLE_DEVICES": "0", "TOKEN": "SECRET"}}
        r = receipt(processes={"api": p})
        self.assertEqual(r["processes"]["api"]["cpuset_effective"]["cpus"], list(range(24, 32)))
        p["start_ticks"] = 999
        self.assertEqual(receipt(processes={"api": p})["processes"]["api"]["status"], "unavailable")

    def test_engine_parent_is_not_inferred_from_role_name(self):
        p = {"pid": 124, "start_ticks": 789, "affinity": [24], "ppid": 123}
        self.assertEqual(receipt(processes={"engine": p})["processes"]["engine"]["api_parent_binding"],
                         "observed_parent_matches_api")
        del p["ppid"]
        self.assertEqual(receipt(processes={"engine": p})["processes"]["engine"]["api_parent_binding"],
                         "unavailable_from_supplied_observation")


if __name__ == "__main__":
    unittest.main()
