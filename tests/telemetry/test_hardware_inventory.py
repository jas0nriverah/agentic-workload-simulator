from unittest.mock import patch
import hashlib
import json

import pytest

from agentic_sim.telemetry.hardware import local_cpu_profile


@pytest.mark.parametrize("cpuinfo,expected", [
    ("processor : 0\nmodel name : AMD EPYC 7B12\nprocessor : 1\n", "AMD EPYC 7B12"),
    ("processor : 0\nHardware : ARM board\n", "ARM board"),
    ("Processor : ARMv7 Processor rev 4\n", "ARMv7 Processor rev 4"),
    ("processor : 0\nprocessor : 1\n", None),
])
def test_cpu_model_is_not_logical_processor_index(cpuinfo, expected):
    def read(path, **kwargs):
        if str(path) == "/proc/cpuinfo":
            return cpuinfo
        raise FileNotFoundError(path)

    with patch("pathlib.Path.read_text", autospec=True, side_effect=read):
        assert local_cpu_profile()["model_name"] == expected


def test_cpu_clock_and_topology_remain_reconstructable_without_cpufreq():
    cpuinfo = "processor : 0\nmodel name : AMD EPYC 7B12\ncpu MHz : 2249.998\nphysical id : 0\ncore id : 3\ncpu cores : 16\nsiblings : 32\ncache size : 512 KB\n\nprocessor : 1\ncpu MHz : 2250.0\ncore id : 4\n"

    def read(path, **kwargs):
        if str(path) == "/proc/cpuinfo":
            return cpuinfo
        raise FileNotFoundError(path)

    with patch("pathlib.Path.read_text", autospec=True, side_effect=read):
        profile = local_cpu_profile()
    clock = profile["cpuinfo_clock"]
    assert clock["availability"] == "declared"
    assert clock["values"] == [2249.998, 2250.0]
    assert clock["minimum"] == 2249.998 and clock["maximum"] == 2250.0
    assert all(value is None for value in profile["frequency_khz"].values())
    assert profile["cpuinfo_descriptors"]["cpu_cores"] == "16"
    assert profile["cpuinfo_source"]["decoded_text"] == cpuinfo
    assert profile["cpuinfo_source"]["decoded_utf8_sha256"] == hashlib.sha256(cpuinfo.encode()).hexdigest()


@pytest.mark.parametrize("value", ["inf", "NaN", "-1", "0", "bad"])
def test_invalid_cpu_clock_stays_unavailable_and_json_is_finite(value):
    def read(path, **kwargs):
        if str(path) == "/proc/cpuinfo":
            return f"processor : 0\ncpu MHz : {value}\n"
        raise FileNotFoundError(path)

    with patch("pathlib.Path.read_text", autospec=True, side_effect=read):
        profile = local_cpu_profile()
    assert profile["cpuinfo_clock"]["availability"] == "unavailable"
    assert profile["cpuinfo_clock"]["values"] == []
    json.dumps(profile, allow_nan=False)
