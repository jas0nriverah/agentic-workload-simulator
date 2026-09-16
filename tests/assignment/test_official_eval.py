import json
import unittest
from pathlib import Path
import tempfile

from agentic_sim.assignment.official_eval import (
    SOURCE_ATTEMPT,
    SOURCE_CASE_RESULT,
    SOURCE_CASE_ROOT,
    SOURCE_MISSING,
    resolve_official_eval,
)


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _eval_doc(*, resolved: bool, submitted: bool = True) -> dict[str, object]:
    return {
        "schema_version": "assignment-official-evaluator.v1",
        "official_resolved": resolved,
        "submitted": submitted,
        "instance_id": "repo__issue-1",
        "run_id": "run-1",
        "counts": {
            "total_instances": 1,
            "submitted_instances": 1,
            "completed_instances": 1,
            "resolved_instances": 1 if resolved else 0,
            "unresolved_instances": 0 if resolved else 1,
            "error_instances": 0,
        },
    }


def _case_result(*, output_dir: str = "runner_attempts/attempt-001") -> dict[str, object]:
    return {
        "status": "completed",
        "runner": {"output_dir": output_dir},
        "evaluator": {
            "status": "completed",
            "submitted": True,
            "official_resolved": False,
            "result_path": f"{output_dir}/evaluator_result.json",
        },
    }


class OfficialEvalFallbackTests(unittest.TestCase):
    def test_prefers_attempt_evaluator_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            _write(case / "case_result.json", _case_result())
            _write(
                case / "runner_attempts/attempt-001/evaluator_result.json",
                _eval_doc(resolved=True),
            )
            _write(case / "evaluator_result.json", _eval_doc(resolved=False))
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertTrue(loaded.official_resolved)
            self.assertTrue(loaded.submitted)
            self.assertEqual(loaded.source_kind, SOURCE_ATTEMPT)
            self.assertTrue(loaded.source_path.endswith("attempt-001/evaluator_result.json"))
            self.assertEqual(len(loaded.source_sha256 or ""), 64)

    def test_falls_back_to_case_root_evaluator_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            _write(case / "case_result.json", _case_result())
            (case / "runner_attempts/attempt-001").mkdir(parents=True)
            _write(case / "evaluator_result.json", _eval_doc(resolved=True))
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertTrue(loaded.official_resolved)
            self.assertEqual(loaded.source_kind, SOURCE_CASE_ROOT)
            self.assertTrue(loaded.source_path.endswith("/evaluator_result.json"))
            self.assertNotIn("attempt-", loaded.source_path.split("/")[-2])

    def test_falls_back_to_case_result_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            result = _case_result()
            result["evaluator"]["official_resolved"] = True
            _write(case / "case_result.json", result)
            (case / "runner_attempts/attempt-001").mkdir(parents=True)
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertTrue(loaded.official_resolved)
            self.assertEqual(loaded.source_kind, SOURCE_CASE_RESULT)
            self.assertTrue(loaded.source_path.endswith("case_result.json"))

    def test_corrupt_attempt_file_does_not_block_case_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            _write(case / "case_result.json", _case_result())
            _write(case / "runner_attempts/attempt-001/evaluator_result.json", {"not": "an eval"})
            _write(case / "evaluator_result.json", _eval_doc(resolved=True, submitted=True))
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertTrue(loaded.official_resolved)
            self.assertEqual(loaded.source_kind, SOURCE_CASE_ROOT)

    def test_missing_sources_are_unresolved_not_true(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            result = _case_result()
            result["evaluator"] = {"status": "missing"}
            _write(case / "case_result.json", result)
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertFalse(loaded.official_resolved)
            self.assertFalse(loaded.submitted)
            self.assertEqual(loaded.source_kind, SOURCE_MISSING)

    def test_does_not_follow_symlinked_eval_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            case = Path(temporary)
            _write(case / "case_result.json", _case_result())
            target = case / "outside.json"
            _write(target, _eval_doc(resolved=True))
            attempt = case / "runner_attempts/attempt-001"
            attempt.mkdir(parents=True)
            (attempt / "evaluator_result.json").symlink_to(target)
            _write(case / "evaluator_result.json", _eval_doc(resolved=False))
            loaded = resolve_official_eval(case / "case_result.json")
            self.assertFalse(loaded.official_resolved)
            self.assertEqual(loaded.source_kind, SOURCE_CASE_ROOT)


if __name__ == "__main__":
    unittest.main()
