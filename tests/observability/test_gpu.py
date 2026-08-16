import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agentic_sim.observability.gpu import (
    collect_dcgmi_sample,
    collect_nvidia_smi_hardware,
    collect_nvidia_smi_sample,
    discover_dcgmi_capability,
    discover_dcgmi_fields,
)
from agentic_sim.observability.profilers import (
    build_nsys_command,
    build_strace_command,
    detect_tool_capability,
    discover_tool_capabilities,
)


class FakeRunner:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.returncode)


class GpuCollectionTests(unittest.TestCase):
    def test_hardware_fields_have_source_time_scope_and_units(self):
        runner = FakeRunner(
            "0, NVIDIA H100 PCIe, GPU-abc, 81920, 9.0, 350, 00000000:01:00.0, 1410, 1410, 1593\n"
        )
        record = collect_nvidia_smi_hardware(
            scope="host_capability", executable="/usr/bin/nvidia-smi", runner=runner
        )
        self.assertEqual(record["status"], "measured")
        self.assertEqual(record["scope"], "host_capability")
        self.assertEqual(record["device_count"]["value"], 1)
        fields = record["devices"][0]["fields"]
        self.assertEqual(fields["name"]["value"], "NVIDIA H100 PCIe")
        self.assertEqual(fields["memory_total_mib"]["unit"], "MiB")
        self.assertEqual(fields["memory_total_mib"]["source"], "nvidia-smi")
        self.assertTrue(fields["memory_total_mib"]["observed_at_utc"].endswith("Z"))
        self.assertIn("--query-gpu=index,name,uuid,memory.total,compute_cap,power.limit", runner.calls[0][0][1])

    def test_sample_marks_unsupported_or_malformed_fields_unavailable(self):
        runner = FakeRunner("0, NVIDIA H100 PCIe, GPU-abc, N/A, 71, 280.5, 1410, bad, 1593\n")
        record = collect_nvidia_smi_sample(scope="run_interval", executable="nvidia-smi", runner=runner)
        fields = record["devices"][0]["fields"]
        self.assertEqual(fields["memory_used_mib"]["status"], "unavailable")
        self.assertEqual(fields["clocks_sm_mhz"]["status"], "unavailable")
        self.assertEqual(fields["utilization_gpu_pct"]["value"], 71)
        self.assertEqual(fields["utilization_gpu_pct"]["scope"], "run_interval")

    def test_missing_nvidia_smi_is_explicitly_unavailable(self):
        with patch("agentic_sim.observability.gpu.shutil.which", return_value=None):
            record = collect_nvidia_smi_hardware()
        self.assertEqual(record["provenance"], "unavailable")
        self.assertEqual(record["error"], "executable_not_found")
        self.assertTrue(all(field["status"] == "unavailable" for field in record["devices"][0]["fields"].values()))

    def test_command_failure_keeps_field_level_error(self):
        runner = FakeRunner(returncode=1, stderr="driver unavailable")
        record = collect_nvidia_smi_sample(executable="nvidia-smi", runner=runner)
        self.assertEqual(record["status"], "unavailable")
        self.assertIn("command_exit:1", record["devices"][0]["fields"]["name"]["error"])


class CapabilityTests(unittest.TestCase):
    def test_dcgmi_missing_does_not_claim_profiling_fields(self):
        with patch("agentic_sim.observability.gpu.shutil.which", return_value=None):
            record = discover_dcgmi_capability()
        self.assertFalse(record["installed"])
        self.assertEqual(record["status"], "unavailable")
        self.assertEqual(record["profiling_fields"]["status"], "unverified")
        self.assertNotIn("counters", record["profiling_fields"])

    def test_dcgmi_version_probe_is_not_a_counter_claim(self):
        runner = FakeRunner(stdout="DCGM 3.3.8\n")
        record = discover_dcgmi_capability(executable="/usr/bin/dcgmi", runner=runner)
        self.assertEqual(record["status"], "available")
        self.assertIn("DCGM 3.3.8", record["version"]["value"])
        self.assertEqual(record["profiling_fields"]["provenance"], "unavailable")

    def test_dcgmi_field_discovery_and_sample_preserve_raw_host_evidence(self):
        discovery_runner = FakeRunner(stdout="Field ID 1001: SM active\nField ID 1002: DRAM active\n")
        discovered = discover_dcgmi_fields(executable="/usr/bin/dcgmi", runner=discovery_runner)
        self.assertEqual(discovered["status"], "available")
        self.assertEqual(discovered["supported_field_ids"], [1001, 1002])
        sample_runner = FakeRunner(stdout="1001 42\n1002 17\n")
        sample = collect_dcgmi_sample([1001, 1002], executable="/usr/bin/dcgmi", runner=sample_runner)
        self.assertEqual(sample["status"], "measured")
        self.assertEqual(sample["field_ids"], ["1001", "1002"])
        self.assertIn("dmon", sample_runner.calls[0][0])

    def test_tool_discovery_reports_absence_and_versions(self):
        def which(name):
            return "/opt/bin/" + name if name == "nsys" else None

        runner = FakeRunner(stdout="NVIDIA Nsight Systems version 2025.1\n")
        with patch("agentic_sim.observability.profilers.shutil.which", side_effect=which):
            record = discover_tool_capabilities(tools=("nsys", "strace"), runner=runner)
        self.assertEqual(record["tools"]["nsys"]["status"], "available")
        self.assertEqual(record["tools"]["strace"]["status"], "unavailable")
        self.assertEqual(record["tools"]["strace"]["error"], "executable_not_found")

    def test_tool_version_failure_is_installed_but_unavailable(self):
        runner = FakeRunner(returncode=1)
        record = detect_tool_capability("py-spy", executable="/opt/bin/py-spy", runner=runner)
        self.assertTrue(record["installed"])
        self.assertEqual(record["status"], "installed_unavailable")
        self.assertEqual(record["provenance"], "unavailable")


class ProfilerCommandTests(unittest.TestCase):
    def test_strace_builder_is_separate_and_hashes_deterministically(self):
        first = build_strace_command(
            ["python", "-c", "print('ok')"], output_path="/tmp/trace.log", mode="cpu-profile"
        )
        second = build_strace_command(
            ["python", "-c", "print('ok')"], output_path="/tmp/trace.log", mode="cpu-profile"
        )
        self.assertEqual(first["command"], [
            "strace", "-f", "-T", "-ttt", "-e", "trace=%file,%desc,%process", "-o",
            "/tmp/trace.log", "--", "python", "-c", "print('ok')",
        ])
        self.assertEqual(first["command_sha256"], second["command_sha256"])
        self.assertEqual(first["scope"], "profiled_attempt")
        self.assertEqual(first["provenance"], "derived")

    def test_nsys_builder_captures_cuda_nvtx_and_osrt(self):
        plan = build_nsys_command(["./agent"], output_path="/tmp/nsys/run", mode="gpu-profile")
        self.assertEqual(plan["command"][:7], [
            "nsys", "profile", "--trace=cuda,nvtx,osrt", "--sample=none",
            "--force-overwrite=true", "--output", "/tmp/nsys/run",
        ])
        self.assertEqual(plan["command"][7:], ["--", "./agent"])
        self.assertEqual(len(plan["command_sha256"]), 64)

    def test_deep_profile_labels_are_accepted_by_builders(self):
        self.assertEqual(build_strace_command(["echo", "x"], output_path="/tmp/x", mode="syscall")["mode"], "syscall")
        self.assertEqual(build_nsys_command(["echo", "x"], output_path="/tmp/x", mode="nsys")["mode"], "nsys")

    def test_control_and_thin_modes_are_rejected(self):
        for mode in ("control", "thin", "thin-telemetry", "uninstrumented"):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    build_strace_command(["echo", "x"], output_path="/tmp/x", mode=mode)
                with self.assertRaises(ValueError):
                    build_nsys_command(["echo", "x"], output_path="/tmp/x", mode=mode)

    def test_builders_reject_shell_strings_and_unsafe_tokens(self):
        with self.assertRaises(TypeError):
            build_strace_command("echo x", output_path="/tmp/x", mode="profiled")
        with self.assertRaises(ValueError):
            build_nsys_command(["echo", "x"], output_path="/tmp/x", mode="profiled", trace="cuda nvtx")
        with self.assertRaises(ValueError):
            build_strace_command(["echo", "x"], output_path="/tmp/x", mode="profiled", executable="strace -f")


if __name__ == "__main__":
    unittest.main()
