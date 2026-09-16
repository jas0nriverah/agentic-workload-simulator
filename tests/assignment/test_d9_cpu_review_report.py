import importlib
import unittest


REPORT = importlib.import_module("scripts.assignment.d9_cpu_review_report")


def _tool(run, prediction, observed, within25=True):
    return {
        "run_id": run,
        "predicted_ms": prediction,
        "observed_ms": observed,
        "within25": within25,
    }


def _trajectory(run, direct=100.0):
    return {
        "run_id": run,
        "predicted_tool_sum_ms": 10.0,
        "observed_tool_sum_ms": 10.0,
        "observed_e2e_ms": 100.0,
        "direct_predicted_e2e_ms": direct,
        "overhead_predicted_e2e_ms": direct,
        "legacy_predicted_e2e_ms": direct,
    }


class D9CpuReviewReportTests(unittest.TestCase):
    def test_joint_gate_is_per_run_intersection_not_marginal_product(self):
        tools = [_tool("pass", 10.0, 10.0), _tool("fail", 100.0, 10.0)]
        gpu = [_tool("pass", 10.0, 10.0), _tool("fail", 10.0, 10.0)]
        result = REPORT._joint_gates(
            tools,
            gpu,
            [_trajectory("pass"), _trajectory("fail")],
            incomplete=set(),
            source_incoherent=set(),
        )
        self.assertEqual(result["direct"]["all_scored"]["passing_runs"], 1)
        self.assertEqual(result["direct"]["all_scored"]["denominator_runs"], 2)
        self.assertEqual(result["direct"]["all_scored"]["rate"], 0.5)
        self.assertEqual(
            result["direct"]["events_only_cpu_and_gpu"]["all_scored"]["passing_runs"], 1
        )

    def test_source_incoherent_run_is_excluded_from_all_required_gate(self):
        tools = [_tool("a", 10.0, 10.0), _tool("b", 10.0, 10.0)]
        gpu = [_tool("a", 10.0, 10.0), _tool("b", 10.0, 10.0)]
        result = REPORT._joint_gates(
            tools,
            gpu,
            [_trajectory("a"), _trajectory("b")],
            incomplete=set(),
            source_incoherent={"a"},
        )
        required = result["direct"]["all_required"]
        self.assertEqual(required["eligible_runs"], 1)
        self.assertEqual(required["passing_runs"], 1)
        self.assertEqual(required["coverage_ineligible_union_run_ids"], ["a"])

    def test_source_incoherent_threshold_uses_closure_rows(self):
        diagnostics = {
            "source_conservation": {
                "closure_rows": [
                    {"run_id": "ok", "observed_tool_sum_ms": 10.0, "protocol_tool_ms": 10.0005},
                    {"run_id": "bad", "observed_tool_sum_ms": 10.0, "protocol_tool_ms": 10.002},
                ]
            }
        }
        runs, details = REPORT._source_incoherent_runs(diagnostics)
        self.assertEqual(runs, {"bad"})
        self.assertEqual(details[0]["run_id"], "bad")


if __name__ == "__main__":
    unittest.main()
