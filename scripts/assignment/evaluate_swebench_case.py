#!/usr/bin/env python3
"""Run and validate the official SWE-bench evaluator for one instance.

This adapter deliberately treats the official evaluator as the only source of
the resolved outcome.  Agent output, process exit status, and log text are
never accepted as a substitute for the canonical evaluator report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping


SCHEMA = "assignment-official-evaluator.v1"
_COUNT_FIELDS = (
    "total_instances",
    "submitted_instances",
    "completed_instances",
    "resolved_instances",
    "unresolved_instances",
)
_ID_FIELDS = (
    "instance_ids",
    "submitted_ids",
    "completed_ids",
    "resolved_ids",
    "unresolved_ids",
    "error_ids",
)


class EvaluatorError(ValueError):
    """A required evaluator input, process result, or report is invalid."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise EvaluatorError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _read_records(path: Path, label: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise EvaluatorError(f"{label} does not exist: {path}")
    if path.suffix.lower() not in {".json", ".jsonl"}:
        raise EvaluatorError(f"{label} must be a .json or .jsonl file: {path}")
    try:
        if path.suffix.lower() == ".jsonl":
            values: list[Any] = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
            values = value if isinstance(value, list) else [value]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluatorError(f"cannot parse {label} {path}: {exc}") from exc
    if not values or any(not isinstance(item, dict) for item in values):
        raise EvaluatorError(f"{label} must contain a non-empty list of JSON objects")
    return [dict(item) for item in values]


def _matching_row(records: Iterable[Mapping[str, Any]], instance_id: str, label: str) -> dict[str, Any]:
    matches = [dict(item) for item in records if item.get("instance_id") == instance_id]
    if len(matches) != 1:
        raise EvaluatorError(
            f"{label} must contain exactly one row for {instance_id}; found {len(matches)}"
        )
    return matches[0]


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    path.write_bytes(payload)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _run(argv: list[str], report_dir: Path, timeout_seconds: int) -> tuple[int, bool]:
    stdout_path = report_dir / "evaluator.stdout.log"
    stderr_path = report_dir / "evaluator.stderr.log"
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(report_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise EvaluatorError(f"could not start official evaluator: {exc}") from exc
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _terminate_process_group(process)
        stdout, stderr = process.communicate()
        stdout = stdout or exc.stdout or b""
        stderr = stderr or exc.stderr or b""
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    return process.returncode if process.returncode is not None else 1, timed_out


def _report_candidates(report_dir: Path, run_id: str) -> list[Path]:
    candidates: list[Path] = []
    for path in sorted(report_dir.glob(f"*.{run_id}.json")):
        if path.is_file():
            candidates.append(path)
    results = report_dir / "results.json"
    if results.is_file():
        candidates.append(results)
    return candidates


def _report_state(report_dir: Path) -> dict[Path, tuple[int, int, int, int, int]]:
    """Capture directory entries so a report cannot be reused or relabeled.

    The state is captured immediately before launching the official evaluator.
    Keeping both the path and filesystem identity lets the post-run check reject
    an existing report that was overwritten in place, as well as an existing
    file that was renamed into a canonical report filename.
    """
    try:
        directory = report_dir.lstat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise EvaluatorError(f"cannot inspect report directory {report_dir}: {exc}") from exc
    if stat.S_ISLNK(directory.st_mode) or not stat.S_ISDIR(directory.st_mode):
        raise EvaluatorError(f"report directory must be a real directory: {report_dir}")

    state: dict[Path, tuple[int, int, int, int, int]] = {}
    try:
        entries = list(report_dir.iterdir())
    except OSError as exc:
        raise EvaluatorError(f"cannot inspect report directory {report_dir}: {exc}") from exc
    for path in entries:
        try:
            entry = path.lstat()
        except OSError as exc:
            raise EvaluatorError(f"cannot inspect report directory entry {path}: {exc}") from exc
        state[path] = (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns)
    return state


def _report_entry_state(path: Path) -> tuple[int, int, int, int, int]:
    try:
        entry = path.lstat()
    except OSError as exc:
        raise EvaluatorError(f"cannot inspect official report {path}: {exc}") from exc
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
        raise EvaluatorError(f"official report must be a regular file: {path}")
    return (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns)


def _id_list(report: Mapping[str, Any], field: str) -> list[str] | None:
    if field not in report:
        return None
    value = report[field]
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvaluatorError(f"official report field {field} must be a string list")
    return value


def _int_field(report: Mapping[str, Any], field: str, *, required: bool = False) -> int | None:
    if field not in report:
        if required:
            raise EvaluatorError(f"official report is missing {field}")
        return None
    value = report[field]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluatorError(f"official report field {field} must be a non-negative integer")
    return value


def _validate_report(report_path: Path, instance_id: str) -> tuple[dict[str, Any], bool, dict[str, int]]:
    try:
        value = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvaluatorError(f"official report is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluatorError("official report must be a JSON object")

    counts = {field: _int_field(value, field, required=field in _COUNT_FIELDS[:3]) for field in _COUNT_FIELDS}
    if counts["total_instances"] != 1 or counts["submitted_instances"] != 1 or counts["completed_instances"] != 1:
        raise EvaluatorError("official report counts must describe exactly one submitted, completed instance")
    if value.get("error_ids") not in (None, []):
        raise EvaluatorError("official report contains error_ids")

    for field in _ID_FIELDS:
        ids = _id_list(value, field)
        if ids is None or field == "error_ids":
            continue
        if field in {"instance_ids", "submitted_ids", "completed_ids"} and ids != [instance_id]:
            raise EvaluatorError(f"official report {field} does not identify exactly {instance_id}")
        if field == "resolved_ids" and ids not in ([], [instance_id]):
            raise EvaluatorError(f"official report {field} has an unexpected instance ID")
        if field == "unresolved_ids" and ids not in ([], [instance_id]):
            raise EvaluatorError(f"official report {field} has an unexpected instance ID")
    error_ids = _id_list(value, "error_ids")
    if error_ids:
        raise EvaluatorError("official report contains error_ids")

    resolved_count = counts["resolved_instances"]
    unresolved_count = counts["unresolved_instances"]
    resolved_ids = _id_list(value, "resolved_ids")
    unresolved_ids = _id_list(value, "unresolved_ids")
    if resolved_count is None:
        resolved_count = 1 if resolved_ids == [instance_id] else 0
    if unresolved_count is None:
        unresolved_count = 1 if unresolved_ids == [instance_id] else 0
    if resolved_count + unresolved_count != 1:
        raise EvaluatorError("official report must contain exactly one resolved or unresolved result")
    if resolved_ids is not None and (resolved_count == 1) != (resolved_ids == [instance_id]):
        raise EvaluatorError("resolved count and resolved IDs disagree")
    if unresolved_ids is not None and (unresolved_count == 1) != (unresolved_ids == [instance_id]):
        raise EvaluatorError("unresolved count and unresolved IDs disagree")
    return value, resolved_count == 1, {
        "total_instances": counts["total_instances"] or 0,
        "submitted_instances": counts["submitted_instances"] or 0,
        "completed_instances": counts["completed_instances"] or 0,
        "resolved_instances": resolved_count,
        "unresolved_instances": unresolved_count,
        "error_instances": len(error_ids or []),
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    dataset = Path(args.dataset).resolve()
    predictions = Path(args.predictions).resolve()
    report_dir = Path(args.report_dir).resolve()
    result_path = Path(args.result).resolve()
    if result_path.exists():
        raise EvaluatorError(f"refusing to overwrite existing result: {result_path}")
    if args.timeout_seconds <= 0:
        raise EvaluatorError("--timeout-seconds must be positive")
    if not args.instance_id.strip() or not args.run_id.strip():
        raise EvaluatorError("--instance-id and --run-id must be non-empty")

    dataset_row = _matching_row(_read_records(dataset, "dataset"), args.instance_id, "dataset")
    prediction_rows = _read_records(predictions, "predictions")
    if len(prediction_rows) != 1:
        raise EvaluatorError(f"predictions must contain exactly one row; found {len(prediction_rows)}")
    prediction = prediction_rows[0]
    if prediction.get("instance_id") != args.instance_id:
        raise EvaluatorError("prediction instance_id does not match --instance-id")
    for field in ("model_name_or_path", "model_patch"):
        if not isinstance(prediction.get(field), str) or not prediction[field].strip():
            raise EvaluatorError(f"prediction {field} must be non-empty")

    report_dir.mkdir(parents=True, exist_ok=True)
    evaluator_dataset = report_dir / "evaluator_dataset.json"
    evaluator_predictions = report_dir / "evaluator_predictions.json"
    _write_json(evaluator_dataset, [dataset_row])
    _write_json(evaluator_predictions, [prediction])
    argv = [
        str(args.evaluator_python), "-m", "swebench.harness.run_evaluation",
        "--dataset_name", str(evaluator_dataset), "--split", "test",
        "--predictions_path", str(evaluator_predictions), "--instance_ids", args.instance_id,
        "--max_workers", "1", "--timeout", str(args.timeout_seconds),
        "--cache_level", args.cache_level, "--clean", args.clean, "--run_id", args.run_id,
        "--namespace", "swebench", "--instance_image_tag", "latest",
        "--env_image_tag", "latest", "--report_dir", str(report_dir),
    ]
    command_hash = _sha256_bytes(_canonical(argv))
    pre_run_state = _report_state(report_dir)
    pre_run_candidates = _report_candidates(report_dir, args.run_id)
    if pre_run_candidates:
        names = ", ".join(str(path) for path in pre_run_candidates)
        raise EvaluatorError(f"official report target must be clean before evaluator launch: {names}")
    return_code, timed_out = _run(argv, report_dir, args.timeout_seconds)
    if timed_out:
        raise EvaluatorError(f"official evaluator timed out after {args.timeout_seconds}s")
    if return_code != 0:
        raise EvaluatorError(f"official evaluator exited with status {return_code}")

    post_run_state = _report_state(report_dir)
    candidates = _report_candidates(report_dir, args.run_id)
    if len(candidates) != 1:
        raise EvaluatorError(f"expected exactly one canonical evaluator report; found {len(candidates)}")
    pre_run_identities = {(state[0], state[1]) for state in pre_run_state.values()}
    new_candidates: list[Path] = []
    for path in candidates:
        state = post_run_state.get(path)
        if state is not None and path not in pre_run_state and (state[0], state[1]) not in pre_run_identities:
            new_candidates.append(path)
    if len(new_candidates) != 1:
        raise EvaluatorError("canonical evaluator report was not newly created by this invocation")
    report_path = new_candidates[0]
    _report_entry_state(report_path)
    report, resolved, counts = _validate_report(report_path, args.instance_id)
    result = {
        "schema_version": SCHEMA,
        "official_resolved": resolved,
        "submitted": True,
        "instance_id": args.instance_id,
        "run_id": args.run_id,
        "report_path": str(report_path),
        "report_sha256": sha256_file(report_path),
        "dataset_path": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "predictions_path": str(predictions),
        "predictions_sha256": sha256_file(predictions),
        "evaluator_dataset_sha256": sha256_file(evaluator_dataset),
        "evaluator_predictions_sha256": sha256_file(evaluator_predictions),
        "command_sha256": command_hash,
        "counts": counts,
        "evaluator_python": str(args.evaluator_python),
        "evaluator_cache_level": args.cache_level,
        "evaluator_clean": args.clean,
        "timeout_seconds": args.timeout_seconds,
    }
    _atomic_json(result_path, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--evaluator-python", type=Path, default=Path(sys.executable))
    parser.add_argument(
        "--cache-level",
        choices=("none", "base", "env", "instance"),
        default="env",
        help="SWE-bench image retention level; env is the bounded default for 500 GB workspaces",
    )
    parser.add_argument(
        "--clean",
        choices=("true", "false"),
        default="true",
        help="remove images above --cache-level after evaluation",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        evaluate(build_parser().parse_args(argv))
    except EvaluatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
