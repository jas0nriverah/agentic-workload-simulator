from scripts.assignment.sweagent_case_runner import _model_hardware_from_remote_profile


def test_remote_gpu_hosts_cpu_frequency_cannot_become_tool_host_feature():
    profile = {'cpu_frequency_hz': 9_000_000_000, 'cpu_base_ghz': 9,
               'gpu_memory_bandwidth_gbps': 2000, 'gpu_bf16_tflops': 500}
    first = _model_hardware_from_remote_profile(profile, clock={'hostname': 'actual-tool-host'})
    profile.update(cpu_frequency_hz=1_000_000_000, cpu_base_ghz=1)
    second = _model_hardware_from_remote_profile(profile, clock={'hostname': 'actual-tool-host'})
    assert first == second
    assert first['cpu_frequency_hz'] is None
    assert first['cpu_frequency_source'] is None
    assert first['availability']['cpu_frequency_hz'] == 'unavailable'
    assert first['gpu_memory_bandwidth_bytes_per_s'] == 2_000_000_000_000
    assert first['gpu_compute_tflops'] == 500
