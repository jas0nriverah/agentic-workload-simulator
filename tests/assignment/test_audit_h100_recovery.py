import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/assignment/audit_h100_recovery.py"
SPEC = importlib.util.spec_from_file_location("audit_h100_recovery", SCRIPT)
assert SPEC and SPEC.loader
AUDITOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDITOR)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


class H100RecoveryAuditTests(unittest.TestCase):
    def make_complete_evidence(self, root: Path) -> list[Path]:
        run = root / "artifacts/batches/lite-diverse-6/run-001"
        case_id = "astropy__astropy-12907"
        paths = [
            run / "trajectory.traj",
            run / "request_proxy.jsonl",
            run / "runner_state.json",
            run / "evaluation/report.json",
            run / "trace_summary.json",
        ]
        write_json(paths[0], {
            "environment": case_id,
            "info": {"model_stats": {"tokens_sent": 120, "tokens_received": 30}},
            "trajectory": [{"action": "pytest -q", "execution_time": 2.5}],
        })
        write_jsonl(paths[1], [{
            "run_id": "run-001",
            "path": "/v1/chat/completions",
            "request_id": "request-1",
            "duration_ms": 400.0,
            "prompt_tokens": 120,
            "completion_tokens": 30,
        }])
        write_json(paths[2], {
            "schema_version": "runner-state.v1",
            "run_id": "run-001",
            "instance_id": case_id,
            "status": "completed",
            "duration_ms": 3200.0,
        })
        write_json(paths[3], {
            case_id: {
                "patch_exists": True,
                "patch_successfully_applied": True,
                "resolved": True,
                "tests_status": {},
            }
        })
        write_json(paths[4], {
            "schema_version": "trace-summary.v1",
            "run_id": "run-001",
            "instance_id": case_id,
            "kernel_duration_sum_ms": 50.0,
        })
        return paths

    def test_classifies_complete_and_partial_assignment_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "eic-work"
            source_paths = self.make_complete_evidence(root)
            partial = root / "artifacts/batches/verified-diverse-6/run-002/trajectory.traj"
            write_json(partial, {
                "environment": "django__django-11099",
                "trajectory": [{"action": "rg bug", "execution_time": 1.0}],
                "info": {},
            })

            report = AUDITOR.audit_roots([root])
            records = {(row["run_id"], row["case_id"]): row for row in report["records"]}
            complete = records[("run-001", "astropy__astropy-12907")]
            self.assertTrue(complete["e2e_recoverable"])
            self.assertTrue(complete["tool_events_recoverable"])
            self.assertTrue(complete["model_request_events_recoverable"])
            self.assertTrue(complete["token_counts_recoverable"])
            self.assertTrue(complete["official_outcome_present"])
            self.assertTrue(complete["complete_assignment_row_recoverable"])
            self.assertEqual(
                {item["relative_path"] for item in complete["sources"]},
                {str(p.resolve().relative_to(root.resolve())) for p in source_paths[:-1]},
            )

            incomplete = records[("run-002", "django__django-11099")]
            self.assertTrue(incomplete["tool_events_recoverable"])
            self.assertFalse(incomplete["e2e_recoverable"])
            self.assertFalse(incomplete["model_request_events_recoverable"])
            self.assertFalse(incomplete["token_counts_recoverable"])
            self.assertFalse(incomplete["complete_assignment_row_recoverable"])

    def test_output_is_deterministic_and_source_hashes_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_root = base / "z-root"
            second_root = base / "a-root"
            paths = self.make_complete_evidence(first_root)
            write_json(second_root / "run-x/worker_state.json", {
                "run_id": "run-x",
                "instance_id": "flask__flask-5014",
                "duration_ms": 123.0,
            })
            one = AUDITOR.audit_roots([first_root, second_root])
            two = AUDITOR.audit_roots([second_root, first_root])
            self.assertEqual(one, two)
            serialized_one = json.dumps(one, indent=2, sort_keys=True) + "\n"
            serialized_two = json.dumps(two, indent=2, sort_keys=True) + "\n"
            self.assertEqual(serialized_one.encode(), serialized_two.encode())

            all_sources = [source for row in one["records"] for source in row["sources"]]
            all_sources.extend(one["unassigned_sources"])
            by_path = {source["path"]: source for source in all_sources}
            trajectory = paths[0].resolve()
            trajectory_key = next(
                key for key in by_path if key.endswith(str(trajectory.relative_to(first_root.resolve())))
            )
            self.assertEqual(by_path[trajectory_key]["sha256"], hashlib.sha256(trajectory.read_bytes()).hexdigest())
            self.assertFalse(any(str(base) in key for key in by_path))

    def test_cli_atomic_output_does_not_mutate_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "evidence"
            sources = self.make_complete_evidence(root)
            before = {
                path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in sources
            }
            before_names = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            output = base / "reports/recovery.json"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root), "--json-out", str(output)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["schema_version"], AUDITOR.SCHEMA_VERSION)
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            self.assertEqual(
                Path(f"{output}.sha256").read_text(),
                f"{digest}  {output.name}\n",
            )
            self.assertFalse(any(output.parent.glob(f".{output.name}.*.tmp")))
            after_names = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            self.assertEqual(before_names, after_names)
            for path, (contents, modified) in before.items():
                self.assertEqual(path.read_bytes(), contents)
                self.assertEqual(path.stat().st_mtime_ns, modified)

    def test_skips_caches_weights_raw_traces_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "evidence"
            root.mkdir()
            cache = root / ".cache/hidden.json"
            weight = root / "weights/model.safetensors"
            raw_trace = root / "run/capture.nsys-rep"
            metadata = root / "run/trace_metadata.json"
            write_json(cache, {"run_id": "must-not-appear"})
            weight.parent.mkdir(parents=True)
            weight.write_bytes(b"weights")
            raw_trace.parent.mkdir(parents=True)
            raw_trace.write_bytes(b"raw trace")
            write_json(metadata, {"schema_version": "trace-summary.v1", "kernel_count": 3})
            link = root / "linked.json"
            link.symlink_to(metadata)

            report = AUDITOR.audit_roots([root])
            reasons = {item["reason"] for item in report["skipped_paths"]}
            self.assertIn("cache_or_model_directory", reasons)
            self.assertIn("model_weight", reasons)
            self.assertIn("raw_trace_payload_skipped_by_default", reasons)
            self.assertIn("symlink_file", reasons)
            self.assertEqual(report["unassigned_sources"][0]["kind"], "trace_metadata")
            self.assertTrue(report["unassigned_sources"][0]["path"].endswith("run/trace_metadata.json"))

    def test_rejects_missing_root_and_enforces_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with self.assertRaisesRegex(AUDITOR.AuditError, "root does not exist"):
                AUDITOR.audit_roots([base / "missing"])
            root = base / "root"
            write_json(root / "one.json", {"run_id": "one"})
            write_json(root / "two.json", {"run_id": "two"})
            with self.assertRaisesRegex(AUDITOR.AuditError, "candidate file limit exceeded"):
                AUDITOR.audit_roots([root], max_files=1)


if __name__ == "__main__":
    unittest.main()
