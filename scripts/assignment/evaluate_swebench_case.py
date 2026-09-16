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
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.runners.case_lifecycle import (  # noqa: E402
    CASE_DEADLINE_ENV,
    CASE_OWNER_ENV,
    deadline_from_env,
    deadline_with_timeout,
    remaining_seconds,
    run_owned_process,
)
from agentic_sim.telemetry import cpu_policy  # noqa: E402


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


PODMAN_COMPAT_WRAPPER = """from __future__ import annotations

import json
import os
import runpy
import re
import tarfile
import sys
from pathlib import Path
from pathlib import PurePosixPath

EVALUATOR_SOURCE_BINDING = None
if EVALUATOR_SOURCE_BINDING is not None:
    import hashlib
    project = Path(EVALUATOR_SOURCE_BINDING["project_path"])
    for record in EVALUATOR_SOURCE_BINDING["files"]:
        source = project / record["path"]
        if source.is_symlink() or not source.resolve().is_relative_to(project):
            raise RuntimeError("evaluator source escaped its reviewed project")
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError("evaluator source changed before child import")
    # The report directory is Python's usual sys.path[0]. Insert the validated
    # checkout before it, including when a wrapper is used instead of -m.
    sys.path.insert(0, str(project))

import swebench.harness.docker_utils as docker_utils
from docker.models.containers import ContainerCollection

if EVALUATOR_SOURCE_BINDING is not None:
    imported = []
    for name, module in list(sys.modules.items()):
        if name == "swebench" or name.startswith("swebench."):
            source = Path(module.__file__).resolve()
            if not source.is_relative_to(project):
                raise RuntimeError("evaluator imported outside its reviewed project")
            imported.append({"module": name, "path": str(source),
                             "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    report_dir = Path(sys.argv[sys.argv.index("--report_dir") + 1])
    (report_dir / "evaluator_import_binding.json").write_text(json.dumps({
        "project_path": str(project), "revision": EVALUATOR_SOURCE_BINDING["revision"],
        "source_manifest_sha256": EVALUATOR_SOURCE_BINDING["source_manifest_sha256"],
        "imported_modules": imported,
    }, sort_keys=True, indent=2) + "\\n")

CPU_POLICY_SOURCE_ROOT = None
placement = None
if os.environ.get("ASSIGNMENT_CPU_POLICY_RUNTIME_PATH") or os.environ.get("ASSIGNMENT_CPU_POLICY_RUNTIME_SHA256"):
    if CPU_POLICY_SOURCE_ROOT is None:
        raise RuntimeError("CPU policy wrapper source binding is missing")
    sys.path.insert(0, CPU_POLICY_SOURCE_ROOT)
    from agentic_sim.telemetry import cpu_policy
    placement = cpu_policy.from_environment()
    if not os.environ.get("ASSIGNMENT_CASE_OWNER"):
        raise RuntimeError("CPU placement requires assignment container ownership")

OWNER = os.environ.get("ASSIGNMENT_CASE_OWNER", "")
_original_create = ContainerCollection.create
def owned_create(collection, *args, **kwargs):
    if not re.fullmatch(r"[0-9a-f]{32}", OWNER):
        raise RuntimeError("valid assignment owner required before Docker creation")
    labels = dict(kwargs.get("labels") or {})
    if labels.get("agentic.assignment.owner", OWNER) != OWNER:
        raise RuntimeError("conflicting assignment container owner")
    labels["agentic.assignment.owner"] = OWNER
    kwargs["labels"] = labels
    kwargs["auto_remove"] = False
    if placement is not None:
        kwargs = cpu_policy.evaluator_docker_kwargs(placement, kwargs)
    container = _original_create(collection, *args, **kwargs)
    if placement is not None:
        container.reload()
        cpu_policy.verify_container(container.attrs, cpu_policy.CONTROL_CPUSET)
    return container


def copy_to_container(container, src: Path, dst: PurePosixPath):
    # Rootless Podman cannot chown archive members to the PACE host UID/GID
    # embedded by tarfile.add().  The evaluator always copies these files as
    # root into a root-owned container path, so normalize only archive
    # ownership while preserving contents and modes.
    tar_path = src.with_suffix(".tar")

    def normalize(member):
        member.uid = 0
        member.gid = 0
        member.uname = "root"
        member.gname = "root"
        return member

    with tarfile.open(tar_path, "w") as tar:
        tar.add(src, arcname=dst.name, filter=normalize)
    try:
        with tar_path.open("rb") as tar_file:
            data = tar_file.read()
        container.exec_run(f"mkdir -p {dst.parent}")
        container.put_archive(os.path.dirname(dst), data)
    finally:
        tar_path.unlink(missing_ok=True)


docker_utils.copy_to_container = copy_to_container


def _retention_root():
    try:
        return Path(sys.argv[sys.argv.index("--report_dir") + 1]) / "docker_cleanup"
    except (ValueError, IndexError):
        return None


def _safe_container_metadata(info):
    state = info.get("State") or {}
    config = info.get("Config") or {}
    return {
        "Id": info.get("Id"),
        "Name": info.get("Name"),
        "Image": info.get("Image"),
        "Created": info.get("Created"),
        "HostConfig": {key: (info.get("HostConfig") or {}).get(key) for key in
                       ("CpusetCpus", "CpuQuota", "CpuPeriod", "NanoCpus", "CpuShares")},
        "Config": {"Labels": {"agentic.assignment.owner": (config.get("Labels") or {}).get("agentic.assignment.owner")}},
        "State": {
            key: state.get(key)
            for key in ("Status", "Running", "StartedAt", "FinishedAt", "ExitCode", "Pid")
            if key in state
        },
    }


def retain_container(client, container, logger):
    # This function is called with the exact Container object returned by the
    # current harness invocation, so no name or before/after inventory lookup
    # is needed.  The owner label is still checked when supplied to avoid
    # retaining or stopping a container created outside this assignment.
    if not container:
        return
    owner = os.environ.get("ASSIGNMENT_CASE_OWNER", "").strip()
    try:
        info = client.api.inspect_container(container.id)
        labels = ((info.get("Config") or {}).get("Labels") or {})
        if owner and labels.get("agentic.assignment.owner") != owner:
            logger.error("refusing Docker cleanup for a container with a mismatched assignment owner")
            return
        root = _retention_root()
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
            (root / ("container-" + container.id[:16] + ".inspect.json")).write_text(
                json.dumps(_safe_container_metadata(info), indent=2, sort_keys=True) + "\\n",
                encoding="utf-8",
            )
            try:
                logs = container.logs()
                if isinstance(logs, str):
                    logs = logs.encode("utf-8", "replace")
                (root / ("container-" + container.id[:16] + ".logs")).write_bytes(logs or b"")
            except Exception as exc:
                logger.error("could not snapshot retained Docker logs: %s", exc)
        status = ((info.get("State") or {}).get("Status"))
        if status == "running":
            container.stop(timeout=15)
    except Exception as exc:
        logger.error("could not stop and retain owned Docker container: %s", exc)


def retain_images(*_args, **_kwargs):
    # Never let the upstream cache cleanup remove shared/preexisting images.
    return None


if OWNER:
    ContainerCollection.create = owned_create
    replacements = {
        "cleanup_container": retain_container,
        "clean_images": retain_images,
        "remove_image": retain_images,
    }
    originals = {name: getattr(docker_utils, name, None) for name in replacements}
    for name, replacement in replacements.items():
        setattr(docker_utils, name, replacement)
    # swebench.__init__ imports harness modules eagerly on the pinned
    # revision. Replace their already-bound aliases as well as docker_utils;
    # otherwise docker_build could still invoke the original remove cleanup.
    for module_name, module in list(sys.modules.items()):
        if module_name.startswith("swebench.harness") and module is not None and module is not docker_utils:
            for name, replacement in replacements.items():
                if getattr(module, name, None) is originals[name]:
                    setattr(module, name, replacement)
runpy.run_module("swebench.harness.run_evaluation", run_name="__main__")
"""


def compatibility_wrapper_source(source_binding: Mapping[str, Any] | None = None) -> str:
    return PODMAN_COMPAT_WRAPPER.replace(
        "CPU_POLICY_SOURCE_ROOT = None",
        "CPU_POLICY_SOURCE_ROOT = " + repr(str(ROOT / "src")),
    ).replace(
        "EVALUATOR_SOURCE_BINDING = None",
        "EVALUATOR_SOURCE_BINDING = " + repr(dict(source_binding) if source_binding else None),
    )


def evaluator_source_environment(
    args: argparse.Namespace, environment: Mapping[str, str], *, deadline: int | None = None,
) -> tuple[dict[str, str], dict[str, Any] | None]:
    """Bind imports to the same clean checkout/revision validated by the case runner."""
    env = dict(environment)
    supplied = getattr(args, "evaluator_project", None)
    revision = getattr(args, "evaluator_revision", None)
    if supplied is None and revision is None:
        if env.get(CASE_OWNER_ENV) or env.get(cpu_policy.RUNTIME_PATH_ENV):
            raise EvaluatorError("owned evaluator requires --evaluator-project and --evaluator-revision")
        return env, None  # Historical standalone fixture/API compatibility.
    if supplied is None or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise EvaluatorError("evaluator project requires its exact 40-character Git revision")
    supplied = Path(supplied)
    if not supplied.is_absolute() or supplied.is_symlink():
        raise EvaluatorError("evaluator project must be an absolute checkout path")
    project = supplied.resolve()
    if not (project / ".git").exists():
        raise EvaluatorError("evaluator project must be a Git checkout")

    def git(*arguments: str) -> str:
        timeout = min(15.0, remaining_seconds(deadline)) if deadline is not None else 15.0
        if timeout <= 0:
            raise EvaluatorError("case deadline expired before evaluator source validation")
        try:
            result = subprocess.run(["git", "-C", str(project), *arguments],
                                    env=env, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EvaluatorError("evaluator project Git validation failed") from exc
        if result.returncode:
            raise EvaluatorError("evaluator project Git validation failed")
        return result.stdout.strip()

    if git("rev-parse", "HEAD") != revision:
        raise EvaluatorError("evaluator project revision differs from the validated runtime")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise EvaluatorError("evaluator project must be clean before child import")
    files = []
    for relative in git("ls-files", "-z", "--", "swebench").split("\0"):
        if not relative:
            continue
        path = project / relative
        if path.is_symlink() or not path.resolve().is_relative_to(project) or not path.is_file():
            raise EvaluatorError("evaluator package source must stay inside the reviewed checkout")
        files.append({"path": relative, "sha256": sha256_file(path)})
    required = {"swebench/__init__.py", "swebench/harness/run_evaluation.py",
                "swebench/harness/docker_utils.py"}
    if not required <= {record["path"] for record in files}:
        raise EvaluatorError("evaluator checkout is missing required tracked package sources")
    binding = {"schema_version": "assignment.evaluator-source-binding.v1",
               "project_path": str(project), "revision": revision,
               "git_tree": git("rev-parse", "HEAD^{tree}"), "files": files,
               "source_manifest_sha256": _sha256_bytes(_canonical(files))}
    env["PYTHONPATH"] = str(project) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, binding


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


def _read_records(path: Path, label: str, *, mapping_values: bool = False) -> list[dict[str, Any]]:
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
            if isinstance(value, list):
                values = value
            elif mapping_values and isinstance(value, dict) and "instance_id" not in value:
                # SWE-agent's native ``preds.json`` format is a mapping from
                # instance ID to prediction row.  The official harness input
                # is the equivalent one-row list, so normalize that wrapper
                # without changing any prediction fields.
                values = list(value.values())
            else:
                values = [value]
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
    """Compatibility wrapper for callers that only have a Popen handle."""

    # Keep this private adapter name for existing integrations, but delegate
    # to the start-time-bound lifecycle implementation.  In particular, do
    # not assume the process group contains every nested ``setsid`` worker.
    from agentic_sim.runners.case_lifecycle import OwnedProcessTree

    OwnedProcessTree(process).terminate(term_grace_seconds=2.0, kill_grace_seconds=2.0)


def _argv_value(argv: Sequence[str], flag: str) -> str | None:
    try:
        index = argv.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return str(argv[index + 1])


def _cleanup_owned_container(
    report_dir: Path,
    *,
    instance_id: str | None,
    run_id: str | None,
) -> dict[str, Any]:
    """Delegate exact owner-label cleanup to the shared retention helper.

    ``owner`` is a fresh UUID for one outer case attempt, so it identifies
    both the SWE-ReX agent and this evaluator without relying on instance-name
    substrings or a before/after Docker inventory.  The shared helper verifies
    each full ID and label, records sanitized inspect/log evidence before
    stopping, and never removes containers or images.
    """

    owner = os.environ.get(CASE_OWNER_ENV, "").strip()
    if not owner:
        return {
            "status": "disabled",
            "reason": f"{CASE_OWNER_ENV} was not propagated",
            "removed": False,
            "retained": False,
        }
    if not instance_id or not run_id:
        return {
            "status": "unavailable",
            "reason": "evaluator argv lacks exact instance/run identity",
            "removed": False,
            "retained": False,
        }
    try:
        from agentic_sim.runners.owned_docker import cleanup_owned_containers

        outcome = dict(cleanup_owned_containers(owner, report_dir / "docker_cleanup"))
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"owned Docker cleanup failed: {exc}",
            "owner": owner,
            "removed": False,
            "retained": True,
            "cleanup_complete": False,
        }
    # Keep the adapter-level shape stable while exposing the shared helper's
    # complete report for audit and fail-closed callers.
    outcome.setdefault("removed", False)
    outcome.setdefault("retained", True)
    return outcome


def _run(
    argv: list[str],
    report_dir: Path,
    timeout_seconds: int,
    *,
    env: Mapping[str, str] | None = None,
    deadline_mono_ns: int | None = None,
) -> tuple[int, bool]:
    stdout_path = report_dir / "evaluator.stdout.log"
    stderr_path = report_dir / "evaluator.stderr.log"
    child_env = dict(env) if env is not None else os.environ.copy()
    inherited_deadline = deadline_mono_ns if deadline_mono_ns is not None else deadline_from_env(child_env)
    if inherited_deadline is None:
        effective_deadline = deadline_with_timeout(timeout_seconds)
        timeout_cap: int | None = timeout_seconds
    else:
        effective_deadline = inherited_deadline
        timeout_cap = None
        # Ensure a caller that passed the deadline explicitly still propagates
        # the same wire value to the official evaluator process.
        child_env[CASE_DEADLINE_ENV] = str(inherited_deadline)
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            outcome = run_owned_process(
                argv,
                cwd=str(report_dir),
                env=child_env,
                stdout=stdout,
                stderr=stderr,
                deadline_mono_ns=effective_deadline,
                timeout_seconds=timeout_cap,
            )
    except OSError as exc:
        raise EvaluatorError(f"could not start official evaluator: {exc}") from exc
    instance_id = _argv_value(argv, "--instance_ids")
    run_id = _argv_value(argv, "--run_id")
    cleanup = _cleanup_owned_container(report_dir, instance_id=instance_id, run_id=run_id) if os.environ.get(CASE_OWNER_ENV) else {
        "status": "not_requested",
        "cleanup_complete": True,
        "removed": False,
        "retained": False,
    }
    _atomic_json(
        report_dir / "evaluator.lifecycle.json",
        {
            "schema_version": "assignment-evaluator-lifecycle.v1",
            "returncode": outcome.returncode,
            "timed_out": outcome.timed_out,
            "deadline_mono_ns": outcome.deadline_mono_ns,
            "started_mono_ns": outcome.started_mono_ns,
            "ended_mono_ns": outcome.ended_mono_ns,
            "process_cleanup": dict(outcome.cleanup),
            "docker_cleanup": cleanup,
        },
    )
    code = outcome.returncode
    if code == 0 and (not outcome.cleanup.get("cleanup_complete") or not cleanup.get("cleanup_complete")):
        code = 1
    return code, outcome.timed_out


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


EMPTY_PATCH_MARKER = "assignment-official-evaluator.empty-patch-unresolved.v1"


def _score_empty_patch(
    args: argparse.Namespace,
    *,
    report_dir: Path,
    dataset: Path,
    predictions: Path,
    evaluator_dataset: Path,
    evaluator_predictions: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Score an empty submitted patch as unresolved without the harness.

    The official harness cannot produce a report for a prediction with no
    diff to apply, so this path writes the same canonical one-instance report
    the harness would have written and validates it through the identical
    reader.  ``command_sha256`` binds the marker for this path instead of a
    harness argv that was never executed.
    """

    candidates = _report_candidates(report_dir, args.run_id)
    if candidates:
        names = ", ".join(str(path) for path in candidates)
        raise EvaluatorError(f"official report target must be clean before evaluator launch: {names}")
    report_path = report_dir / f"model.{args.run_id}.json"
    _write_json(
        report_path,
        {
            "total_instances": 1,
            "submitted_instances": 1,
            "completed_instances": 1,
            "resolved_instances": 0,
            "unresolved_instances": 1,
            "error_ids": [],
            "resolved_ids": [],
            "unresolved_ids": [args.instance_id],
            "instance_ids": [args.instance_id],
            "submitted_ids": [args.instance_id],
            "completed_ids": [args.instance_id],
            "empty_patch": True,
        },
    )
    _report_entry_state(report_path)
    _, resolved, counts = _validate_report(report_path, args.instance_id)
    if resolved:
        raise EvaluatorError("empty submitted patch cannot be scored as resolved")
    return {
        "schema_version": SCHEMA,
        "official_resolved": False,
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
        "command_sha256": _sha256_bytes(
            _canonical(
                {
                    "marker": EMPTY_PATCH_MARKER,
                    "instance_id": args.instance_id,
                    "run_id": args.run_id,
                }
            )
        ),
        "counts": counts,
        "evaluator_python": str(args.evaluator_python),
        "evaluator_cache_level": args.cache_level,
        "evaluator_clean": args.clean,
        "timeout_seconds": timeout_seconds,
        "empty_patch_unresolved": True,
        "harness_invoked": False,
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

    # The matrix/case runner passes one absolute monotonic deadline through
    # the environment.  Derive the pinned harness's integer test timeout from
    # remaining time so ``--timeout`` cannot silently create a fresh full
    # evaluator window.  Standalone adapter invocations retain the historical
    # CLI-only behavior for local validation and tests.
    inherited_deadline = deadline_from_env(os.environ)
    if inherited_deadline is not None:
        remaining = remaining_seconds(inherited_deadline)
        if remaining <= 0:
            raise EvaluatorError(f"{CASE_DEADLINE_ENV} expired before evaluator launch")
        evaluator_timeout_seconds = min(args.timeout_seconds, max(1, math.ceil(remaining)))
    else:
        evaluator_timeout_seconds = args.timeout_seconds
    evaluator_env, source_binding = evaluator_source_environment(
        args, os.environ, deadline=inherited_deadline,
    )

    dataset_row = _matching_row(_read_records(dataset, "dataset"), args.instance_id, "dataset")
    prediction_rows = _read_records(predictions, "predictions", mapping_values=True)
    if len(prediction_rows) != 1:
        raise EvaluatorError(f"predictions must contain exactly one row; found {len(prediction_rows)}")
    prediction = prediction_rows[0]
    if prediction.get("instance_id") != args.instance_id:
        raise EvaluatorError("prediction instance_id does not match --instance-id")
    if not isinstance(prediction.get("model_name_or_path"), str) or not prediction["model_name_or_path"].strip():
        raise EvaluatorError("prediction model_name_or_path must be non-empty")
    # An agent that reaches its call limit without editing the repository
    # submits an empty patch.  That is a measured unresolved experiment
    # outcome, not an adapter input error, and the official harness has
    # nothing to apply, so normalize it and score it as submitted/unresolved.
    patch = prediction.get("model_patch")
    empty_patch = not isinstance(patch, str) or not patch.strip()
    if empty_patch:
        prediction["model_patch"] = ""

    report_dir.mkdir(parents=True, exist_ok=True)
    source_metadata: dict[str, Any] = {}
    if source_binding is not None:
        binding_path = report_dir / "evaluator_source_binding.json"
        _atomic_json(binding_path, source_binding)
        source_metadata = {
            "evaluator_project_path": source_binding["project_path"],
            "evaluator_project_revision": source_binding["revision"],
            "evaluator_source_binding_path": str(binding_path),
            "evaluator_source_binding_sha256": sha256_file(binding_path),
            "evaluator_source_manifest_sha256": source_binding["source_manifest_sha256"],
        }
    evaluator_dataset = report_dir / "evaluator_dataset.json"
    evaluator_predictions = report_dir / "evaluator_predictions.json"
    _write_json(evaluator_dataset, [dataset_row])
    _write_json(evaluator_predictions, [prediction])
    if empty_patch:
        result = _score_empty_patch(
            args,
            report_dir=report_dir,
            dataset=dataset,
            predictions=predictions,
            evaluator_dataset=evaluator_dataset,
            evaluator_predictions=evaluator_predictions,
            timeout_seconds=evaluator_timeout_seconds,
        )
        result.update(source_metadata)
        _atomic_json(result_path, result)
        return result
    # The wrapper is also the retention boundary for native Docker: when the
    # case owner is propagated, upstream SWE-bench cleanup is replaced with
    # exact-object stop-and-retain and shared-image cleanup is suppressed.  A
    # rootless Podman socket still gets the archive ownership compatibility
    # patch for standalone invocations.
    placement = cpu_policy.from_environment()
    if placement is not None and not os.environ.get(CASE_OWNER_ENV, "").strip():
        raise EvaluatorError("CPU placement requires assignment container ownership")
    use_rootless_podman_compat = source_binding is not None or placement is not None or bool(os.environ.get(CASE_OWNER_ENV, "").strip()) or os.environ.get("DOCKER_HOST", "").startswith("unix://")
    compatibility_wrapper: Path | None = None
    compatibility_wrapper_sha256: str | None = None
    if use_rootless_podman_compat:
        compatibility_wrapper = report_dir / ".swebench_podman_compat.py"
        compatibility_wrapper.write_text(compatibility_wrapper_source(source_binding), encoding="utf-8")
        compatibility_wrapper_sha256 = sha256_file(compatibility_wrapper)
        evaluator_entrypoint = str(compatibility_wrapper)
    else:
        evaluator_entrypoint = "-m"
    argv = [
        str(args.evaluator_python), evaluator_entrypoint,
        *( ["swebench.harness.run_evaluation"] if evaluator_entrypoint == "-m" else [] ),
        "--dataset_name", str(evaluator_dataset), "--split", "test",
        "--predictions_path", str(evaluator_predictions), "--instance_ids", args.instance_id,
        "--max_workers", "1", "--timeout", str(evaluator_timeout_seconds),
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
    return_code, timed_out = _run(
        argv,
        report_dir,
        evaluator_timeout_seconds,
        env=evaluator_env,
        deadline_mono_ns=inherited_deadline,
    )
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
        "timeout_seconds": evaluator_timeout_seconds,
    }
    if compatibility_wrapper is not None and compatibility_wrapper_sha256 is not None:
        result["compatibility_wrapper_path"] = str(compatibility_wrapper)
        result["compatibility_wrapper_sha256"] = compatibility_wrapper_sha256
    result.update(source_metadata)
    if source_binding is not None:
        import_binding = report_dir / "evaluator_import_binding.json"
        if not import_binding.is_file():
            raise EvaluatorError("evaluator child import binding is missing")
        result["evaluator_import_binding_path"] = str(import_binding)
        result["evaluator_import_binding_sha256"] = sha256_file(import_binding)
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
    parser.add_argument("--evaluator-project", type=Path,
                        help="exact clean SWE-bench checkout validated by the runtime")
    parser.add_argument("--evaluator-revision", help="runtime's pinned SWE-bench Git commit")
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
