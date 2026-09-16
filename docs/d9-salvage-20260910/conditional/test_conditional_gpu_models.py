#!/usr/bin/env python3
"""Focused tests for the bounded conditional GPU comparison."""
from __future__ import annotations

import itertools
import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_conditional_gpu_models as model  # noqa: E402


class ConditionalGpuModelTests(unittest.TestCase):
    def test_solver_enumerates_correlated_active_sets(self) -> None:
        # x0 and x1 are deliberately strongly correlated.  An active-set
        # implementation that permanently removes a negative slope can miss
        # the feasible optimum after the other correlated column is fixed.
        x = []
        y = []
        for index in range(1, 31):
            first = float(index)
            second = first + (0.02 if index % 2 else -0.02)
            x.append([first, second, float(index % 5)])
            y.append(1.0 + 2.0 * first + 0.1 * float(index % 5))
        weights = [1.0] * len(x)
        intercept, slopes = model._fit_weighted_nonnegative(x, y, weights)
        self.assertGreaterEqual(intercept, -100.0)
        self.assertTrue(all(value >= -1e-10 for value in slopes))

        # Independently enumerate the same constrained normal-equation
        # objective.  This checks both nonnegativity and re-entry correctness.
        width = len(x[0])
        gram = [[0.0 for _ in range(width + 1)] for _ in range(width + 1)]
        rhs = [0.0 for _ in range(width + 1)]
        for row, target in zip(x, y):
            vector = [1.0, *row]
            for left in range(width + 1):
                rhs[left] += vector[left] * target
                for right in range(width + 1):
                    gram[left][right] += vector[left] * vector[right]
        for feature in range(1, width + 1):
            gram[feature][feature] += model.RIDGE

        feasible_objectives = []
        for size in range(width + 1):
            for active in itertools.combinations(range(width), size):
                columns = [0, *(feature + 1 for feature in active)]
                reduced = [[gram[left][right] for right in columns] for left in columns]
                reduced_rhs = [rhs[index] for index in columns]
                try:
                    solved = model._solve_linear(reduced, reduced_rhs)
                except model.ValidationError:
                    continue
                if any(value < -1e-9 for value in solved[1:]):
                    continue
                coefficients = [0.0] * (width + 1)
                for column, value in zip(columns, solved):
                    coefficients[column] = value
                feasible_objectives.append(
                    sum(coefficients[i] * gram[i][j] * coefficients[j] for i in range(width + 1) for j in range(width + 1))
                    - 2.0 * sum(coefficients[i] * rhs[i] for i in range(width + 1))
                )
        observed_objective = sum(
            ([intercept, *slopes][i]) * gram[i][j] * ([intercept, *slopes][j])
            for i in range(width + 1) for j in range(width + 1)
        ) - 2.0 * sum([intercept, *slopes][i] * rhs[i] for i in range(width + 1))
        self.assertAlmostEqual(observed_objective, min(feasible_objectives), places=7)

    def test_features_use_declared_token_workload_and_exclude_target(self) -> None:
        row = {
            "input_tokens": 100,
            "context_tokens": 200,
            "output_tokens": 30,
            "max_output_tokens": 512,
            "observed_ms": 99.0,
        }
        values = model.feature_values(row, interaction=True)
        self.assertEqual(len(values), 4)
        self.assertTrue(all(math.isfinite(value) for value in values))
        self.assertNotIn("observed_ms", model.feature_names(True))
        self.assertIn("output_tokens", " ".join(model.feature_names(True)))


if __name__ == "__main__":
    unittest.main()
