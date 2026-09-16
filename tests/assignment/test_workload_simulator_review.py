import unittest

from agentic_sim.assignment.event_simulator import (
    EventSimulatorError,
    HardwareProfile,
    ModelEventInput,
    ToolEventInput,
)
from agentic_sim.assignment.workload_simulator import WorkloadSimulator, WorkloadToolInput


def hardware(*, ghz=3.0, threads=32, cores=16):
    return HardwareProfile.from_mapping(
        {
            "schema_version": "assignment.hardware-profile.v1",
            "hardware_id": "review-host",
            "architecture": "Hopper",
            "cpu_cores": cores,
            "cpu_threads": threads,
            "cpu_base_ghz": ghz,
            "system_memory_gib": 128.0,
            "storage_read_mbps": 5000.0,
            "storage_write_mbps": 3000.0,
            "gpu_count": 1,
            "gpu_compute_capability": 9.0,
            "gpu_memory_gib": 80.0,
            "gpu_memory_bandwidth_gbps": 3350.0,
            "gpu_bf16_tflops": 989.0,
        }
    )


def tool(run_id, ordinal, action="echo hello", *, split="calibration", ghz=3.0, instance_id=None):
    return WorkloadToolInput.from_action(
        action,
        event_id=f"{run_id}-tool-{ordinal}",
        run_id=run_id,
        split=split,
        hardware=hardware(ghz=ghz),
        repository="review-repo",
        instance_id=instance_id or run_id,
    )


def model(run_id, ordinal, *, split="calibration"):
    return ModelEventInput.from_mapping(
        {
            "schema_version": "assignment.model-event-input.v1",
            "request_id": f"{run_id}-model-{ordinal}",
            "run_id": run_id,
            "split": split,
            "input_tokens": 100 + ordinal,
            "context_tokens": 200 + ordinal,
            "max_output_tokens": 64,
            "output_tokens": 8 + ordinal,
            "hardware": hardware().to_mapping(),
        }
    )


def fitted(*, timeout=False, cpu_center="median"):
    tools = []
    models = []
    trajectories = []
    for run_number in range(4):
        run_id = f"cal-{run_number}"
        action = "git log --oneline" if timeout else "echo hello"
        tool_count = run_number + 1
        model_count = 4 - run_number
        for ordinal in range(tool_count):
            tools.append((tool(run_id, ordinal, action, instance_id=f"instance-{run_number}"), 20_000.0 if timeout else 50.0))
        for ordinal in range(model_count):
            models.append((model(run_id, ordinal), 20.0))
        measured_sum = tool_count * (20_000.0 if timeout else 50.0) + model_count * 20.0
        overhead = 10.0 + 2.0 * tool_count + 3.0 * model_count
        trajectories.append((run_id, measured_sum + overhead))
    return WorkloadSimulator.fit(
        tools,
        models,
        trajectories,
        select_alpha=False,
        cpu_center=cpu_center,
    )


class WorkloadSimulatorReviewTests(unittest.TestCase):
    def test_action_roundtrip_and_cached_flags_are_canonical(self):
        item = tool("r", 0, "cd /repo && git log --oneline | less")
        mapping = item.to_mapping()
        mapping["tool_name"] = "cat"
        mapping["operation_class"] = "read"
        restored = WorkloadToolInput.from_mapping(mapping)
        self.assertEqual(restored, item)
        self.assertEqual(restored.to_mapping(), item.to_mapping())

    def test_action_input_rejects_measured_labels(self):
        mapping = tool("r", 0).to_mapping()
        mapping["observed_ms"] = 1.0
        with self.assertRaisesRegex(EventSimulatorError, "target-derived"):
            WorkloadToolInput.from_mapping(mapping)

    def test_semantic_fit_does_not_use_action_substrings_as_class(self):
        tools = []
        models = []
        trajectories = []
        for index in range(16):
            run_id = f"class-{index}"
            action = "echo 'pytest is text'" if index < 8 else "pytest tests/test_x.py"
            tool_ms = 50.0 if index < 8 else 300.0
            tools.append((tool(run_id, 0, action), tool_ms))
            models.append((model(run_id, 0), 20.0))
            trajectories.append((run_id, tool_ms + 20.0 + 10.0))
        simulator = WorkloadSimulator.fit(tools, models, trajectories, select_alpha=False)
        shell = tool("hold", 0, "echo 'pytest is text'", split="holdout")
        test = tool("hold", 1, "pytest tests/test_x.py", split="holdout")
        self.assertNotEqual(simulator.predict_tool_ms(shell), simulator.predict_tool_ms(test))

    def test_frequency_changes_normal_work_but_not_threads(self):
        simulator = fitted()
        reference = tool("hold", 0, ghz=3.0, split="holdout")
        slower = tool("hold", 1, ghz=1.5, split="holdout")
        slower_more_threads = WorkloadToolInput.from_mapping(
            {
                **slower.to_mapping(),
                "hardware": {
                    **slower.hardware.to_mapping(),
                    "cpu_threads": 64,
                    "cpu_cores": 32,
                },
            }
        )
        self.assertAlmostEqual(
            simulator.predict_tool_ms(slower),
            2.0 * simulator.predict_tool_ms(reference),
            delta=1e-6,
        )
        self.assertAlmostEqual(
            simulator.predict_tool_ms(slower),
            simulator.predict_tool_ms(slower_more_threads),
            delta=1e-6,
        )

    def test_git_pager_timeout_is_wallclock_invariant(self):
        simulator = fitted(timeout=True)
        reference = tool("hold", 0, "git log --oneline", split="holdout", ghz=3.0)
        slower = tool("hold", 1, "git log --oneline", split="holdout", ghz=1.5)
        self.assertGreaterEqual(simulator.predict_tool_ms(reference), 20_000.0)
        self.assertAlmostEqual(
            simulator.predict_tool_ms(reference),
            simulator.predict_tool_ms(slower),
            delta=1e-6,
        )

    def test_additive_event_sum_and_separate_overhead(self):
        simulator = fitted()
        self.assertEqual(simulator.predict_event_sum_ms(11.0, 7.0), 18.0)
        self.assertAlmostEqual(simulator.predict_e2e_ms(11.0, 7.0, 2.0, 2.0), 18.0 + simulator.predict_overhead_ms(2.0, 2.0))
        payload = simulator.to_mapping()
        self.assertEqual(payload["schema_version"], "assignment.workload-simulator.v3")
        self.assertIn("overhead", payload)
        self.assertIn("reference_cpu", payload)
        self.assertTrue(all(value >= 0 for value in payload["overhead"]["coefficients"]))

    def test_mixed_action_and_legacy_roundtrip_preserves_fallback(self):
        tools = []
        models = []
        trajectories = []
        for index in range(4):
            run_id = f"mixed-{index}"
            action = tool(run_id, 0, "echo hello")
            legacy = ToolEventInput.from_mapping(
                {
                    key: value
                    for key, value in tool(run_id, 1, "cat README.md").to_mapping().items()
                    if key not in {"action", "repository", "instance_id"}
                }
            )
            tools.extend(((action, 50.0), (legacy, 80.0)))
            models.append((model(run_id, 0), 20.0))
            trajectories.append((run_id, 50.0 + 80.0 + 20.0 + 10.0))
        simulator = WorkloadSimulator.fit(tools, models, trajectories, select_alpha=False)
        probe = ToolEventInput.from_mapping(
            {
                key: value
                for key, value in tool("hold", 0, "cat README.md", split="holdout").to_mapping().items()
                if key not in {"action", "repository", "instance_id"}
            }
        )
        restored = WorkloadSimulator.from_mapping(simulator.to_mapping())
        self.assertIsNotNone(restored.legacy_cpu_model)
        self.assertEqual(simulator.predict_tool_ms(probe), restored.predict_tool_ms(probe))

    def test_v3_mapping_hash_and_overhead_coefficients_are_checked(self):
        payload = fitted().to_mapping()
        tampered = dict(payload)
        tampered["cpu_ref_ghz"] = 99.0
        with self.assertRaisesRegex(EventSimulatorError, "model_sha256"):
            WorkloadSimulator.from_mapping(tampered)
        tampered = dict(payload)
        tampered["overhead"] = dict(payload["overhead"])
        tampered["overhead"]["coefficients"] = [float("nan"), 0.0, 0.0]
        # Refreshing the digest simulates a structurally valid but unusable
        # payload and exercises the coefficient validation independently.
        from agentic_sim.assignment.event_simulator import canonical_sha256

        tampered["model_sha256"] = canonical_sha256(
            {key: value for key, value in tampered.items() if key != "model_sha256"}
        )
        with self.assertRaisesRegex(EventSimulatorError, "overhead coefficients"):
            WorkloadSimulator.from_mapping(tampered)

    def test_boundary_and_id_validation(self):
        tools = [(tool("r0", 0), 20.0), (tool("r1", 0), 20.0)]
        models = [(model("r0", 0), 20.0), (model("r1", 0), 20.0)]
        with self.assertRaisesRegex(EventSimulatorError, "below measured"):
            WorkloadSimulator.fit(tools, models, [("r0", 10.0), ("r1", 40.0)], select_alpha=False)
        with self.assertRaisesRegex(EventSimulatorError, "unique"):
            WorkloadSimulator.fit(tools, models, [("r0", 50.0), ("r0", 50.0)], select_alpha=False)


if __name__ == "__main__":
    unittest.main()
