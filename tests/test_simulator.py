import unittest

from agentic_sim.simulator import CalibrationRecord, HardwareLatencySimulator, SimulatorError


class SimulatorTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            CalibrationRecord("a", 11.0, 2.0, 8.0),
            CalibrationRecord("b", 13.0, 3.0, 9.0),
            CalibrationRecord("c", 15.0, 4.0, 10.0),
        ]

    def test_fit_predict_and_holdout_are_explicitly_derived(self):
        model = HardwareLatencySimulator.fit(self.records[:2])
        prediction = model.predict(self.records[2], target_score=2.0)
        self.assertEqual(prediction["provenance"], "simulated")
        self.assertAlmostEqual(prediction["predicted_seconds"], 10.0)
        evaluation = model.evaluate(self.records[2:], target_score=1.0)
        self.assertEqual(evaluation["provenance"], "derived")
        self.assertEqual(evaluation["holdout_run_ids"], ["c"])
        self.assertGreaterEqual(evaluation["mean_absolute_error_seconds"], 0.0)

    def test_aggregate_only_or_underconstrained_inputs_are_rejected(self):
        with self.assertRaises(SimulatorError):
            CalibrationRecord.from_mapping({"run_id": "aggregate", "observed_seconds": 1.0, "provenance": "derived"})
        with self.assertRaises(SimulatorError):
            HardwareLatencySimulator.fit(self.records[:1])
        with self.assertRaises(SimulatorError):
            HardwareLatencySimulator.fit(self.records).predict(self.records[0], target_score=0)


if __name__ == "__main__":
    unittest.main()
