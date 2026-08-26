"""Focused contract tests for the offline assignment completion auditor."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.assignment.test_evaluate_predictions import labels_for, prepare_frozen, run_cli
from scripts.assignment.generate_step_figures import generate_figures
from scripts.assignment.plan_matrix import build_plan, load_config, render_jsonl
from scripts.assignment.reconcile_plan import _load_trajectories, reconcile
from scripts.assignment.adaptive_event_protocol import (
    AdaptiveEventProtocol,
    FrozenCalibrationModel,
    _record_digest as adaptive_record_digest,
    _write_frozen_json as write_adaptive_frozen_json,
    freeze_calibration_model,
    freeze_trajectory_prediction,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/audit_completion.py"
SPEC = importlib.util.spec_from_file_location("audit_completion", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sidecar(path: Path) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = path.with_name(path.name + ".sha256")
    result.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return result


class AuditCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.paths = self._make_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def _make_fixture(self) -> dict[str, Path]:
        root = self.root
        trajectories = root / "trajectories.csv"
        tools = root / "tool_events.csv"
        models = root / "model_events.csv"
        sweeps = root / "sweep_runs.csv"
        config = load_config(AUDIT.ASSIGNMENT_CONFIG)
        manifests = {
            suite: (
                [
                    {
                        "instance_id": f"{suite}__repo-{index:02d}",
                        "repository": f"{suite}/repo-{index:02d}",
                        "task_sha256": hashlib.sha256(f"{suite}-{index}".encode()).hexdigest(),
                    }
                    for index in range(12)
                ],
                hashlib.sha256(f"{suite}-manifest".encode()).hexdigest(),
            )
            for suite in ("lite", "verified")
        }
        plan_rows = build_plan(
            config,
            manifests,
            config_sha256=hashlib.sha256(AUDIT.ASSIGNMENT_CONFIG.read_bytes()).hexdigest(),
        )
        plan = root / "plan.jsonl"
        plan.write_bytes(render_jsonl(plan_rows))
        plan_sha = sidecar(plan)

        trajectory_rows = []
        tool_rows = []
        model_rows = []
        for index, case in enumerate(plan_rows[1:], 1):
            run_id = f"run-{index:03d}"
            variation = case["variation"]
            trajectory_rows.append({
                "schema_version": "assignment.trajectory.v1", "run_id": run_id,
                "suite": case["suite"], "repository": case["repository"], "category": "bugfix",
                "instance_id": case["instance_id"], "config_id": case["cell_id"],
                "repeat_id": "r01", "sweep_parameter": "" if variation is None else variation["knob"],
                "sweep_value": "" if variation is None else str(variation["value"]),
                "status": "completed", "submitted": "true", "official_resolved": "true",
                "e2e_wall_ms": "40", "tool_wall_ms": "10", "model_wall_ms": "20",
                "tool_model_ratio": "0.5", "tool_event_count": "1", "model_event_count": "1",
                "hardware_id": "h100", "model_revision": "model", "swe_agent_revision": "agent",
                "swe_bench_revision": "bench", "command_sha256": "a" * 64,
                "tool_events_path": "tool_events.jsonl", "model_events_path": "model_events.jsonl",
                "unavailable_reason": "", "provenance": "measured",
            })
            tool_rows.append({"event_id": f"tool-{index}", "run_id": run_id, "status": "completed", "operation_class": "read", "wall_ms": "10"})
            model_rows.append({"request_id": f"model-{index}", "run_id": run_id, "status": "completed", "input_tokens": "10", "output_tokens": "5", "context_tokens": "15", "wall_ms": "20"})
        self._write_csv(trajectories, list(AUDIT.TRAJECTORY_FIELDS), trajectory_rows)
        self._write_csv(
            tools,
            ["event_id", "run_id", "status", "operation_class", "wall_ms"],
            tool_rows,
        )
        self._write_csv(
            models,
            ["request_id", "run_id", "status", "input_tokens", "output_tokens", "context_tokens", "wall_ms"],
            model_rows,
        )
        sweep_rows = []
        for parameter in sorted(AUDIT.REQUIRED_SWEEP_PARAMETERS):
            for value in ("1", "2"):
                sweep_rows.append({
                    "run_id": f"s-{parameter}-{value}", "status": "completed", "sweep_parameter": parameter,
                    "sweep_value": value, "official_resolved": "true", "e2e_wall_ms": "40",
                    "tool_wall_ms": "10", "model_wall_ms": "20",
                })
        self._write_csv(
            sweeps,
            ["run_id", "status", "sweep_parameter", "sweep_value", "official_resolved", "e2e_wall_ms", "tool_wall_ms", "model_wall_ms"],
            sweep_rows,
        )

        reconciliation = root / "reconciliation.json"
        _remaining, reconciliation_report = reconcile(
            plan_rows[0],
            plan_rows[1:],
            _load_trajectories(trajectories),
            original_plan_sha256=hashlib.sha256(plan.read_bytes()).hexdigest(),
            trajectories_sha256=hashlib.sha256(trajectories.read_bytes()).hexdigest(),
        )
        write_json(reconciliation, reconciliation_report)

        selection = root / "selection.json"
        with trajectories.open(newline="", encoding="utf-8") as stream:
            raw_rows = list(csv.DictReader(stream))
        write_json(selection, AUDIT.recompute_step3_selection(raw_rows, hashlib.sha256(trajectories.read_bytes()).hexdigest()))

        inventory = root / "inventory.json"
        write_json(inventory, {
            "schema_version": "assignment.dataset-inventory.v1", "trajectory_count": len(trajectory_rows),
            "tool_event_count": len(tool_rows), "model_event_count": len(model_rows), "suite_counts": {"lite": 156, "verified": 156},
            "repository_count": 24, "config_count": 13,
            "hashes": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in {
                "trajectories": trajectories, "tool_events": tools, "model_events": models, "sweep_runs": sweeps,
            }.items()},
            "ratio_definition": "sum(tool_event.wall_ms) / sum(model_event.wall_ms)",
            "secondary_diagnostics_not_ratio": [],
        })
        inventory_sha = sidecar(inventory)

        generate_figures(
            trajectories_path=trajectories,
            tool_events_path=tools,
            model_events_path=models,
            sweep_runs_path=sweeps,
            output_dir=root,
            reconciliation_report_path=reconciliation,
        )
        figures = root / "assignment_report.json"

        manifest, _freeze_result = prepare_frozen(root)
        manifest_sha = manifest.with_suffix(".sha256")
        labels = root / "holdout_labels.json"
        write_json(labels, labels_for(manifest))
        labels_sha = labels.with_suffix(".sha256")
        labels_sha.write_text(
            f"{hashlib.sha256(labels.read_bytes()).hexdigest()}  {labels.name}\n",
            encoding="utf-8",
        )
        receipt = root / "prepare_receipt.json"
        evaluation = root / "evaluation.json"
        score = run_cli(
            "score",
            "--prediction-manifest",
            manifest,
            "--holdout-labels",
            labels,
            "--prepare-receipt",
            receipt,
            "--output",
            evaluation,
        )
        if score.returncode != 0:
            raise AssertionError(score.stdout + score.stderr)
        output = root / "audit.json"
        return {
            "plan": plan, "plan_sha": plan_sha, "reconciliation": reconciliation,
            "trajectories": trajectories, "tools": tools, "models": models, "sweeps": sweeps,
            "inventory": inventory, "inventory_sha": inventory_sha, "selection": selection,
            "figures": figures, "manifest": manifest, "manifest_sha": manifest_sha,
            "labels": labels, "labels_sha": labels_sha, "receipt": receipt,
            "evaluation": evaluation, "output": output,
        }

    @staticmethod
    def _adaptive_bindings() -> dict[str, str]:
        return {
            "split_manifest_sha256": "a" * 64,
            "runtime_manifest_sha256": "b" * 64,
            "hardware_profile_sha256": "c" * 64,
            "model_revision_sha256": "d" * 64,
        }

    def _make_adaptive_holdout(
        self,
        *,
        tool_label: dict[str, object] | None = None,
    ) -> tuple[Path, Path]:
        """Create a completed native adaptive root without a workload."""
        root = self.root / "adaptive-holdout"
        split_path = self.root / "adaptive-split.json"
        write_json(
            split_path,
            {
                "schema_version": "assignment.event-split-manifest.v1",
                "calibration_run_ids": ["cal-1"],
                "holdout_run_ids": ["holdout-1"],
            },
        )
        sidecar(split_path)
        hardware = {
            "schema_version": "assignment.hardware-profile.v1", "hardware_id": "test-h100",
            "architecture": "Hopper", "cpu_cores": 16, "cpu_threads": 32,
            "cpu_base_ghz": 3.0, "system_memory_gib": 128.0,
            "storage_read_mbps": 5000.0, "storage_write_mbps": 3000.0,
            "gpu_count": 1, "gpu_compute_capability": 9.0, "gpu_memory_gib": 80.0,
            "gpu_memory_bandwidth_gbps": 3350.0, "gpu_bf16_tflops": 989.0,
        }
        hardware_path = self.root / "adaptive-hardware.json"
        write_json(hardware_path, hardware)
        sidecar(hardware_path)
        values = {
            "split_manifest_sha256": hashlib.sha256(split_path.read_bytes()).hexdigest(),
            "runtime_manifest_sha256": "b" * 64,
            "hardware_profile_sha256": hashlib.sha256(hardware_path.read_bytes()).hexdigest(),
            "model_revision_sha256": "d" * 64,
        }
        model_path = self.root / "adaptive-calibration-model.json"
        blocks = {
            "tool_event": {"alpha": 0.1, "coefficients": [10.0] + [0.0] * 13},
            "model_event": {"alpha": 0.1, "coefficients": [20.0] + [0.0] * 7},
            "trajectory": {"alpha": 0.1, "coefficients": [100.0] + [0.0] * 4},
        }
        freeze_calibration_model(blocks, model_path, calibration_run_ids=["cal-1"], **values)

        prediction_path = self.root / "adaptive-e2e-prediction.json"
        freeze_trajectory_prediction(
            FrozenCalibrationModel.load(model_path),
            run_id="holdout-1",
            hardware=hardware,
            tool_events=[{
                "schema_version": "assignment.tool-event-input.v1",
                "event_id": "forecast-tool-0",
                "run_id": "holdout-1",
                "split": "holdout",
                "operation_class": "read",
                "declared_command_bytes": 120,
                "declared_read_bytes": 2048,
                "declared_write_bytes": 0,
                "declared_path_count": 2,
                "hardware": hardware,
            }],
            model_events=[{
                "schema_version": "assignment.model-event-input.v1",
                "request_id": "forecast-model-0",
                "run_id": "holdout-1",
                "split": "holdout",
                "input_tokens": 400,
                "context_tokens": 600,
                "max_output_tokens": 128,
                "hardware": hardware,
            }],
            output_path=prediction_path,
        )

        class Clock:
            def __init__(self) -> None:
                self.value = 100

            def witness(self) -> dict[str, object]:
                self.value += 1
                return {
                    "captured_at_utc": f"2026-08-25T00:00:{self.value:02d}Z",
                    "clock_id": "CLOCK_MONOTONIC_RAW",
                    "monotonic_ns": self.value,
                    "boot_id": "audit-test-boot",
                }

        protocol = AdaptiveEventProtocol(
            root,
            FrozenCalibrationModel.load(model_path),
            clock=Clock(),
            **values,
        )
        protocol.arm_trajectory(
            "holdout-1",
            predicted_e2e_ms=100,
            e2e_prediction_artifact_path=str(prediction_path),
            e2e_prediction_artifact_sha256=hashlib.sha256(prediction_path.read_bytes()).hexdigest(),
            calibration_model_path=str(model_path),
            hardware_profile_path=str(hardware_path),
        )
        tool = {
            "schema_version": "assignment.tool-event-input.v1", "event_id": "holdout-1-tool-0",
            "run_id": "holdout-1", "split": "holdout", "operation_class": "read",
            "declared_command_bytes": 120, "declared_read_bytes": 2048,
            "declared_write_bytes": 0, "declared_path_count": 2, "hardware": hardware,
        }
        model = {
            "schema_version": "assignment.model-event-input.v1", "request_id": "holdout-1-request-0",
            "run_id": "holdout-1", "split": "holdout", "input_tokens": 400,
            "context_tokens": 600, "max_output_tokens": 128, "hardware": hardware,
        }
        protocol.predict_event("tool", tool)
        protocol.reveal_event_label("tool", tool["event_id"], tool_label or {"observed_ms": 10})
        protocol.predict_event("model", model)
        protocol.reveal_event_label("model", model["request_id"], {"observed_ms": 20})
        protocol.freeze_prediction_manifest()
        protocol.reveal_trajectory_label(100)
        score_path = root / "adaptive_score.json"
        write_adaptive_frozen_json(score_path, protocol.score())
        return root, score_path

    @staticmethod
    def _rewrite_adaptive_journal(path: Path, records: list[dict[str, object]]) -> None:
        previous = ""
        for ordinal, record in enumerate(records):
            record["journal_ordinal"] = ordinal
            record["chain_prev_sha256"] = previous
            record["record_sha256"] = adaptive_record_digest(record)
            previous = record["record_sha256"]
        path.write_text(
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records),
            encoding="utf-8",
        )

    def _run(self, *extra: str) -> subprocess.CompletedProcess[str]:
        p = self.paths
        args = [
            sys.executable, str(SCRIPT), "--plan", p["plan"], "--plan-sha256", p["plan_sha"],
            "--reconciliation-report", p["reconciliation"], "--trajectories", p["trajectories"],
            "--tool-events", p["tools"], "--model-events", p["models"], "--sweep-runs", p["sweeps"],
            "--inventory", p["inventory"], "--inventory-sha256", p["inventory_sha"],
            "--step3-selection", p["selection"], "--figures-report", p["figures"],
            "--prediction-manifest", p["manifest"], "--holdout-labels", p["labels"],
            "--prepare-receipt", p["receipt"], "--evaluation-report", p["evaluation"],
            "--output", p["output"], *extra,
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True, check=False)

    def test_complete_evidence_passes_and_writes_deterministic_sidecar(self) -> None:
        result = self._run("--output-sha256-sidecar", self.root / "audit.sha256")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.paths["output"].read_text())
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["checks"]["dataset_inventory"]["sweep_run_count"], 8)
        self.assertTrue((self.root / "audit.sha256").is_file())
        self.assertEqual(report["protocol_types"], ["static_historical"])

    def test_completed_adaptive_holdout_is_independently_audited(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout()
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(self.paths["output"].read_text())
        self.assertEqual(report["protocol_types"], ["static_historical", "adaptive_live_holdout"])
        adaptive = report["checks"]["adaptive_holdout_protocol"]
        self.assertEqual(adaptive["protocol_type"], "adaptive_live_holdout")
        self.assertEqual(adaptive["event_counts"], {"tool": 1, "model": 1, "unavailable": 0})

    def test_adaptive_score_accepts_the_runner_sidecar_convention(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout()
        score.with_suffix(".sha256").replace(Path(str(score) + ".sha256"))
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_adaptive_manifest_tamper_fails_closed(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout()
        manifest = adaptive_root / "adaptive_prediction_manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("adaptive_holdout_protocol", self.paths["output"].read_text())

    def test_adaptive_second_prediction_before_label_fails_closed(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout()
        journal = adaptive_root / "adaptive_events.jsonl"
        records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        records[1] = dict(records[2])
        self._rewrite_adaptive_journal(journal, records)
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("second prediction", self.paths["output"].read_text())

    def test_adaptive_score_above_25_percent_fails_closed(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout(tool_label={"observed_ms": 14})
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not pass the 25%", self.paths["output"].read_text())

    def test_adaptive_unavailable_event_cannot_be_silently_dropped(self) -> None:
        adaptive_root, score = self._make_adaptive_holdout(
            tool_label={"status": "unavailable", "unavailable_reason": "trace unavailable"}
        )
        result = self._run(
            "--adaptive-holdout-root", adaptive_root,
            "--adaptive-score-report", score,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("adaptive score artifact", self.paths["output"].read_text())

    def test_mismatched_plan_hash_fails_closed(self) -> None:
        self.paths["plan_sha"].write_text("0" * 64 + "  plan.jsonl\n", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.paths["output"].read_text())["status"], "blocked")

    def test_remaining_cases_fail_closed(self) -> None:
        value = json.loads(self.paths["reconciliation"].read_text())
        value["remaining_case_count"] = 1
        write_json(self.paths["reconciliation"], value)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)

    def test_plan_must_bind_exact_checked_in_config(self) -> None:
        rows = [json.loads(line) for line in self.paths["plan"].read_text().splitlines()]
        rows[0]["config_sha256"] = "0" * 64
        self.paths["plan"].write_text(
            "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
            encoding="utf-8",
        )
        sidecar(self.paths["plan"])
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exact checked-in assignment config", self.paths["output"].read_text())

    def test_plan_matrix_cardinality_must_match_checked_in_config(self) -> None:
        rows = [json.loads(line) for line in self.paths["plan"].read_text().splitlines()]
        rows[0]["sources"]["lite"]["task_count"] -= 1
        self.paths["plan"].write_text(
            "\n".join(json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows) + "\n",
            encoding="utf-8",
        )
        sidecar(self.paths["plan"])
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline cardinality", self.paths["output"].read_text())

    def test_missing_sweep_coverage_fails_closed(self) -> None:
        value = json.loads(self.paths["figures"].read_text())
        value["coverage"]["observed_sweep_parameters"] = ["call_limit"]
        write_json(self.paths["figures"], value)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)

    def test_fabricated_sweep_count_fails_closed(self) -> None:
        value = json.loads(self.paths["figures"].read_text())
        value["counts"]["sweep_runs"] = 1
        write_json(self.paths["figures"], value)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)

    def test_missing_or_tampered_figure_fails_closed(self) -> None:
        target = self.root / "step1_repository_ratio.svg"
        target.write_text("<svg>tampered</svg>\n", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatch", self.paths["output"].read_text())

    def test_figure_report_aggregates_are_regenerated_not_hash_trusted(self) -> None:
        value = json.loads(self.paths["figures"].read_text())
        value["categories"][0]["average_e2e_wall_ms"] = 999.0
        write_json(self.paths["figures"], value)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("independent canonical-table regeneration", self.paths["output"].read_text())

    def test_event_above_25_percent_fails_closed(self) -> None:
        value = json.loads(self.paths["evaluation"].read_text())
        value["tool_events"][0]["absolute_percentage_error"] = 25.01
        value["tool_events"][0]["within_25_percent"] = False
        value["summaries"]["tool_events"]["all_available_within_25_percent"] = False
        value["summaries"]["tool_events"]["max_absolute_percentage_error"] = 25.01
        value["passed"] = False
        write_json(self.paths["evaluation"], value)
        self.paths["evaluation"].with_suffix(".sha256").write_text(
            f"{hashlib.sha256(self.paths['evaluation'].read_bytes()).hexdigest()}  evaluation.json\n",
            encoding="utf-8",
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("independent recomputation", self.paths["output"].read_text())

    def test_unavailable_row_fails_closed(self) -> None:
        value = json.loads(self.paths["evaluation"].read_text())
        value["coverage_complete"] = False
        value["summaries"]["model_events"]["unavailable_count"] = 1
        value["unavailable"]["model_events"] = [{"request_id": "model-1"}]
        value["passed"] = False
        write_json(self.paths["evaluation"], value)
        result = self._run()
        self.assertNotEqual(result.returncode, 0)

    def test_refuses_overwrite_without_force(self) -> None:
        self.paths["output"].write_text("existing\n", encoding="utf-8")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.paths["output"].read_text(), "existing\n")
        result = self._run("--force")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
