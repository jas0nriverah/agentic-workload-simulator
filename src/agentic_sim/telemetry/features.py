"""Leakage-safe, deterministic v2 feature construction.

The v2 measurement stream deliberately keeps features which are known at an
action/request boundary separate from observations which become known after
execution.  Both the trainer and the serving adapter use these functions;
there is no second, subtly different parser in the live path.

This module does not read a repository or inspect a result.  A caller may pass
an explicit state snapshot that was captured immediately before execution, but
future script contents, timings, status labels, and evaluator outcomes are
rejected as feature inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from pathlib import Path
from typing import Any, Mapping, Sequence

from agentic_sim.assignment.tool_features import extract_tool_features


FEATURE_SCHEMA = "assignment.d9-feature.v2"
FEATURE_BUILDER_ID = "d9-feature-builder.v2.action-boundary-20260908"

_LEAKED_FIELDS = frozenset(
    {
        "wall_ms",
        "duration_ms",
        "observed_ms",
        "execution_time",
        "output_tokens",
        "completion_tokens",
        "response_tokens",
        "status",
        "success",
        "failure",
        "timeout",
        "evaluator",
        "official_resolved",
        "result",
        "observation",
        "future_state",
        "post_state",
    }
)

# The state and hardware objects are intentionally narrower than the event
# records.  A caller may keep a rich raw inventory in telemetry, but only
# these fields may cross the prospective model boundary.
_STATE_FIELDS = frozenset(
    {
        "status",
        "generation",
        "paths",
        "revisions",
        "source_event_id",
        "reason",
        "observed_at_mono_ns",
        "availability",
    }
)
_STATE_PATH_FIELDS = frozenset({"path", "sha256", "size_bytes", "content_artifact"})
_SCRIPT_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_path",
        "sha256",
        "encoding",
        "size_bytes",
        "truncated",
        "hash_basis",
        "byte_exact",
    }
)
_HARDWARE_FIELDS = frozenset(
    {
        "cpu_frequency_hz",
        "cpu_frequency_source",
        "gpu_memory_bandwidth_bytes_per_s",
        "gpu_compute_tflops",
        "clock",
        "availability",
    }
)
_HARDWARE_AVAILABILITY = {"measured", "declared", "unavailable", "derived"}
_MODEL_FIELDS = frozenset(
    {
        "input_tokens",
        "context_tokens",
        "max_output_tokens",
        "temperature",
        "top_p",
        "seed",
        "model",
        "model_revision",
        "tokenizer_revision",
        "request_sha256",
        "mode",
        "hardware",
    }
)


def canonical_json(value: Any) -> str:
    """Serialize feature values exactly as the v2 train/serve contract does."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        # A malformed action is still a real action.  Preserve its bytes and
        # expose a conservative unknown tokenization instead of guessing.
        return command.split()


def _operator_segments(command: str) -> list[tuple[str, str | None]]:
    """Split top-level shell operators and retain the edge before each part.

    This is deliberately a small syntactic scanner.  It does not claim to be
    a shell parser and therefore never turns a quoted ``|`` or ``&&`` into a
    pipeline edge.  The retained operator lets consumers distinguish a pipe
    from a conditional/sequence edge (``git diff | head || true``).
    """

    result: list[tuple[str, str | None]] = []
    current: list[str] = []
    quote = ""
    escaped = False
    pending_operator: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            index += 1
            continue
        operator: str | None = None
        if command.startswith("&&", index):
            operator = "&&"
        elif command.startswith("||", index):
            operator = "||"
        elif char in {";", "|"}:
            operator = char
        if operator is not None:
            text = "".join(current).strip()
            if text:
                result.append((text, pending_operator))
            current = []
            pending_operator = operator
            index += len(operator)
            continue
        current.append(char)
        index += 1
    text = "".join(current).strip()
    if text:
        result.append((text, pending_operator))
    return result


def _segments(command: str) -> list[str]:
    """Compatibility view of :func:`_operator_segments` without operators."""

    return [segment for segment, _operator in _operator_segments(command)]


def _primary(command: str) -> str:
    parts = _segments(command)
    if not parts:
        return command.strip()
    for part in parts:
        tokens = _tokens(part)
        if tokens and Path(tokens[0]).name.lower() not in {"cd", "chdir"}:
            return part
    return parts[-1]


def _path_tokens(tokens: Sequence[str]) -> list[str]:
    """Return path/work-scope arguments with conservative flag handling."""

    paths: list[str] = []
    skip_next = False
    # Values to options such as -C and --include are not work paths.
    value_flags = {
        "-C",
        "--directory",
        "--include",
        "--exclude",
        "--glob",
        "--max-count",
        "-k",
        "--filter",
    }
    for token in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        if token in {"&&", "||", ";", "|"}:
            continue
        # Shell executable words are not useful scope.  Keep explicit path
        # spellings and the conventional test/module targets.
        if any(marker in token for marker in ("/", "\\", ".", "~", "*") ):
            paths.append(token)
        elif token in {".", ".."}:
            paths.append(token)
    return paths


_SUBCOMMAND_DISPATCHERS = frozenset(
    {
        "git",
        "npm",
        "yarn",
        "pnpm",
        "cargo",
        "go",
        "mvn",
        "gradle",
        "bazel",
        "docker",
        "podman",
        "kubectl",
        "helm",
        "gh",
        "pip",
        "pip3",
        "conda",
    }
)


def _semantic_subcommand(tokens: Sequence[str]) -> str | None:
    """Return a dispatcher subcommand, never an arbitrary positional path.

    Commands such as ``cat list`` and ``find .`` have arguments, but neither
    argument is a subcommand.  Python's ``-m`` module is represented by the
    separate ``module`` field.  Keeping this distinction makes the field
    useful to a serving model without pretending that every second token is a
    command verb.
    """

    if not tokens:
        return None
    executable = Path(tokens[0]).name.lower()
    if executable not in _SUBCOMMAND_DISPATCHERS:
        return None
    index = 1
    # A small option scanner handles the common global options whose values
    # precede the dispatch verb.  Unknown options are skipped conservatively.
    value_options = {
        "git": {"-C", "--git-dir", "--work-tree", "-c"},
        "docker": {"--context", "-H"},
        "kubectl": {"--context", "--namespace", "-n"},
        "pip": {"--python"},
        "pip3": {"--python"},
    }.get(executable, set())
    while index < len(tokens):
        token = str(tokens[index])
        if token in value_options:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower()
    return None


def _stage_descriptor(tokens: Sequence[str]) -> dict[str, Any]:
    executable = Path(tokens[0]).name if tokens else None
    module = _module(tokens)
    test = _test_descriptor("", tokens)
    traversal = _traversal_descriptor(" ".join(str(token) for token in tokens), tokens)
    child = _find_exec_descriptor(tokens)
    operation_class = _semantic_operation_class(
        "shell",
        executable or "",
        test["test_runner"],
        traversal["traversal_mode"],
        child.get("test_runner") if isinstance(child, Mapping) else None,
    )
    return {
        "executable": executable,
        "subcommand": _semantic_subcommand(tokens),
        "module": module,
        "argv": list(tokens),
        "argv_count": len(tokens),
        "operation_class": operation_class,
        "test_runner": test["test_runner"],
        "test_scope": test["test_scope"],
        "test_scope_count": test["test_scope_count"],
        "find_exec_child": child,
        "operator_before": None,
        "pipe_to_next": False,
        "find_exec": bool(executable and executable.lower() == "find" and any(item in {"-exec", "-execdir"} for item in tokens)),
    }


def _pipeline(command: str) -> dict[str, Any]:
    parts = _operator_segments(command)
    stages: list[dict[str, Any]] = []
    operators: list[str] = []
    for index, (part, operator_before) in enumerate(parts):
        tokens = _tokens(part)
        if not tokens:
            continue
        if operator_before is not None:
            operators.append(operator_before)
        next_operator = parts[index + 1][1] if index + 1 < len(parts) else None
        stage = _stage_descriptor(tokens)
        stage["operator_before"] = operator_before
        stage["pipe_to_next"] = next_operator == "|"
        stages.append(stage)
    pipe_edges = sum(1 for _segment, operator in parts if operator == "|")
    pipeline_lengths: list[int] = []
    current_length = 0
    for index, (_segment, _operator_before) in enumerate(parts):
        if index >= len(stages):
            continue
        current_length += 1
        next_operator = parts[index + 1][1] if index + 1 < len(parts) else None
        if next_operator != "|":
            if current_length > 1:
                pipeline_lengths.append(current_length)
            current_length = 0
    if current_length > 1:
        pipeline_lengths.append(current_length)
    return {
        "has_pipeline": int(pipe_edges > 0),
        "has_conditional": int(any(operator in {"&&", "||", ";"} for _segment, operator in parts)),
        "operators": operators,
        "pipeline_edge_count": pipe_edges,
        "pipeline_stage_count": max(pipeline_lengths, default=0),
        "pipeline_count": len(pipeline_lengths),
        "stage_count": len(stages),
        "stages": stages,
    }


def _test_descriptor(command: str, tokens: Sequence[str]) -> dict[str, Any]:
    del command  # Classification must use executable tokens, not substrings.
    runner: str | None = None
    executable = Path(tokens[0]).name.lower() if tokens else ""
    lowered_tokens = [str(token).lower() for token in tokens]
    if executable == "pytest":
        runner = "pytest"
    elif executable == "python" or re.fullmatch(r"python\d+(?:\.\d+)?", executable):
        if len(lowered_tokens) > 2 and lowered_tokens[1] == "-m" and lowered_tokens[2] in {"pytest", "unittest"}:
            runner = lowered_tokens[2]
        elif len(lowered_tokens) > 2 and Path(lowered_tokens[1]).name == "setup.py" and "test" in lowered_tokens[2:]:
            runner = "setup.py"
    elif executable == "unittest":
        runner = "unittest"
    elif executable == "tox":
        runner = "tox"
    elif executable in {"npm", "yarn", "pnpm"} and (
        len(lowered_tokens) > 1 and (lowered_tokens[1] == "test" or (lowered_tokens[1:3] == ["run", "test"]))
    ):
        runner = executable
    elif executable == "cargo" and len(lowered_tokens) > 1 and lowered_tokens[1] == "test":
        runner = "cargo"
    elif executable == "go" and len(lowered_tokens) > 1 and lowered_tokens[1] == "test":
        runner = "go"
    elif executable in {"mvn", "gradle", "bazel"} and "test" in lowered_tokens[1:]:
        runner = executable

    scope: list[str] = []
    module_index = next(
        (index for index, token in enumerate(lowered_tokens[:-1]) if token == "-m"),
        None,
    )
    for index, token in enumerate(tokens):
        if token in {"-k", "--filter", "--tests", "--testNamePattern"} and index + 1 < len(tokens):
            scope.append(tokens[index + 1])
        elif runner is not None and not token.startswith("-"):
            lowered = str(token).lower()
            if index == 0 or (module_index is not None and index in {module_index, module_index + 1}):
                continue
            if lowered in {"test", "{}", "\\;", ";", "+", "\\+"}:
                continue
            if token.endswith((".py", ".js", ".ts", ".go", ".rs")) or "/" in token:
                scope.append(token)
            elif runner in {"pytest", "unittest"} and "." in token:
                # unittest accepts dotted module/class selectors, which do
                # not necessarily contain a filesystem suffix.
                scope.append(token)
    if runner is None:
        scope = []
    return {
        "test_runner": runner,
        "test_scope": scope,
        "test_scope_count": len(scope),
    }


def _find_exec_descriptor(tokens: Sequence[str]) -> dict[str, Any] | None:
    """Describe the declared child command of a syntactic ``find -exec``.

    The number of files selected by ``find`` is runtime data and therefore
    remains null with explicit unavailable provenance.  The child command and
    its module/test runner are still known before execution and are retained
    for attribution.
    """

    marker_index = next((index for index, token in enumerate(tokens) if token in {"-exec", "-execdir"}), None)
    if marker_index is None:
        return None
    child: list[str] = []
    for token in tokens[marker_index + 1 :]:
        if token in {";", "+", "\\;", "\\+"}:
            break
        child.append(str(token))
    if not child:
        return {
            "executable": None,
            "subcommand": None,
            "module": None,
            "argv": [],
            "argv_count": 0,
            "test_runner": None,
            "test_scope": [],
            "dynamic": True,
            "dynamic_child_count": None,
            "dynamic_child_count_availability": "unavailable",
        }
    descriptor = _stage_descriptor(child)
    return {
        "executable": descriptor["executable"],
        "subcommand": descriptor["subcommand"],
        "module": descriptor["module"],
        "argv": descriptor["argv"],
        "argv_count": descriptor["argv_count"],
        "test_runner": descriptor["test_runner"],
        "test_scope": descriptor["test_scope"],
        "dynamic": any(token in {"{}", "\u007b\u007d"} for token in child),
        "dynamic_child_count": None,
        "dynamic_child_count_availability": "unavailable",
    }


def _traversal_descriptor(command: str, tokens: Sequence[str]) -> dict[str, Any]:
    lowered = command.lower()
    executable = Path(tokens[0]).name.lower() if tokens else ""
    find_exec = executable == "find" and any(token in {"-exec", "-execdir"} for token in tokens)
    if find_exec:
        mode = "find-exec"
    elif executable == "find":
        mode = "find"
    elif executable in {"ls", "tree", "du", "fd", "locate"}:
        mode = "directory-traversal"
    elif "glob" in lowered or "*" in command or "?" in command:
        mode = "glob"
    else:
        mode = None
    return {
        "traversal_mode": mode,
        "find_exec": bool(find_exec),
        "find_exec_kind": next((token for token in ("-exec", "-execdir") if token in tokens), None),
    }


def _module(tokens: Sequence[str]) -> str | None:
    executable = Path(tokens[0]).name.lower() if tokens else ""
    if executable != "python" and not re.fullmatch(r"python\d+(?:\.\d+)?", executable):
        return None
    for index, token in enumerate(tokens[:-1]):
        if token == "-m":
            return tokens[index + 1]
    return None


def _semantic_operation_class(
    extracted_class: str,
    executable: str,
    test_runner: str | None,
    traversal_mode: str | None,
    child_test_runner: str | None = None,
) -> str:
    """Correct substring-based historical classes with token semantics."""

    name = Path(executable).name.lower()
    if traversal_mode is not None or name in {"find", "ls", "tree", "du", "fd", "locate"}:
        return "traversal"
    if test_runner is not None:
        return "test"
    if name in {"rg", "grep", "ag", "ack"}:
        return "search"
    if name in {"cat", "head", "tail", "less", "more"}:
        return "read"
    if extracted_class == "test" or child_test_runner is not None:
        # A word such as ``pytest`` inside an echo/grep argument is not a
        # test execution.  A find child is retained separately so its test
        # runner remains visible without relabelling the parent traversal.
        return "shell"
    return extracted_class


def _validate_state(state: Mapping[str, Any] | None) -> dict[str, Any]:
    if state is None:
        return {
            "status": "unknown",
            "generation": 0,
            "paths": [],
            "revisions": {},
            "source_event_id": None,
            "reason": "state ledger did not establish a pre-action snapshot",
            "observed_at_mono_ns": None,
            "availability": {"script_state": "unavailable"},
        }
    unknown = sorted(set(str(key) for key in state).difference(_STATE_FIELDS))
    if unknown:
        raise ValueError(f"script state has unsupported fields: {', '.join(unknown)}")
    result = dict(state)
    status = result.get("status")
    if status not in {"known", "unknown", "invalidated"}:
        raise ValueError("script state status must be known, unknown, or invalidated")
    if isinstance(result.get("generation"), bool) or not isinstance(result.get("generation"), int) or result["generation"] < 0:
        raise ValueError("script state generation must be a non-negative integer")
    for key in ("source_event_id", "reason"):
        if result.get(key) is not None and not isinstance(result[key], str):
            raise ValueError(f"script state {key} must be text or null")
    observed = result.get("observed_at_mono_ns")
    if observed is not None and (isinstance(observed, bool) or not isinstance(observed, int) or observed < 0):
        raise ValueError("script state observed_at_mono_ns must be a non-negative integer or null")
    paths = result.get("paths", [])
    if not isinstance(paths, list):
        raise ValueError("script state paths must be a list")
    for entry in paths:
        if isinstance(entry, str):
            continue
        if not isinstance(entry, Mapping) or set(entry).difference(_STATE_PATH_FIELDS):
            raise ValueError(
                "script state path entries must be strings or path/sha256/size_bytes/content_artifact objects"
            )
        if not isinstance(entry.get("path"), str) or not entry["path"]:
            raise ValueError("script state path entry requires a non-empty path")
        digest = entry.get("sha256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise ValueError("script state sha256 must be a SHA-256 string or null")
        size = entry.get("size_bytes")
        if size is not None and (isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError("script state size_bytes must be a non-negative integer or null")
        artifact = entry.get("content_artifact")
        if artifact is not None:
            if not isinstance(artifact, Mapping) or set(artifact).difference(_SCRIPT_ARTIFACT_FIELDS):
                raise ValueError("script content_artifact has unsupported fields")
            artifact_path = artifact.get("artifact_path")
            if artifact_path is not None and (
                not isinstance(artifact_path, str) or not artifact_path
            ):
                raise ValueError("script content_artifact artifact_path must be text or null")
            artifact_digest = artifact.get("sha256")
            if artifact_digest is not None and (
                not isinstance(artifact_digest, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", artifact_digest)
            ):
                raise ValueError("script content_artifact sha256 must be a SHA-256 string or null")
            encoding = artifact.get("encoding")
            if encoding is not None and (not isinstance(encoding, str) or not encoding):
                raise ValueError("script content_artifact encoding must be text or null")
            artifact_size = artifact.get("size_bytes")
            if artifact_size is not None and (
                isinstance(artifact_size, bool) or not isinstance(artifact_size, int) or artifact_size < 0
            ):
                raise ValueError("script content_artifact size_bytes must be a non-negative integer or null")
            truncated = artifact.get("truncated", False)
            if not isinstance(truncated, bool):
                raise ValueError("script content_artifact truncated must be boolean")
            hash_basis = artifact.get("hash_basis")
            if hash_basis is not None and (not isinstance(hash_basis, str) or not hash_basis):
                raise ValueError("script content_artifact hash_basis must be text or null")
            byte_exact = artifact.get("byte_exact")
            if byte_exact is not None and not isinstance(byte_exact, bool):
                raise ValueError("script content_artifact byte_exact must be boolean or null")
    revisions = result.get("revisions", {})
    if not isinstance(revisions, Mapping):
        raise ValueError("script state revisions must be a mapping")
    for path, digest in revisions.items():
        if not isinstance(path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise ValueError("script state revisions must map paths to SHA-256 strings")
    availability = result.get("availability", {})
    if availability is None:
        availability = {}
    if not isinstance(availability, Mapping):
        raise ValueError("script state availability must be a mapping")
    allowed_availability = {"script_state", "paths", "revisions"}
    if set(availability).difference(allowed_availability):
        raise ValueError("script state availability has unsupported fields")
    for field, value in availability.items():
        if value not in _HARDWARE_AVAILABILITY:
            raise ValueError(f"script state availability for {field} is unsupported")
    result["availability"] = dict(availability)
    reject_leaky_features({key: value for key, value in result.items() if key != "status"})
    return result


def _validate_hardware(hardware: Mapping[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(str(key) for key in hardware).difference(_HARDWARE_FIELDS))
    if unknown:
        raise ValueError(f"hardware has unsupported model fields: {', '.join(unknown)}")
    result = dict(hardware)
    availability = result.get("availability", {})
    if availability is None:
        availability = {}
    if not isinstance(availability, Mapping):
        raise ValueError("hardware availability must be a mapping")
    for key, value in availability.items():
        if key not in _HARDWARE_FIELDS or value not in _HARDWARE_AVAILABILITY:
            raise ValueError(f"invalid hardware availability for {key}")
    result["availability"] = dict(availability)
    for key in ("cpu_frequency_hz", "gpu_memory_bandwidth_bytes_per_s", "gpu_compute_tflops"):
        value = result.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise ValueError(f"hardware {key} must be non-negative or null")
        if value is not None:
            try:
                finite = math.isfinite(value)
            except (OverflowError, ValueError):
                finite = False
            if not finite or value < 0:
                raise ValueError(f"hardware {key} must be a finite non-negative number or null")
    if result.get("cpu_frequency_source") is not None and not isinstance(result["cpu_frequency_source"], str):
        raise ValueError("hardware cpu_frequency_source must be text or null")
    if result.get("clock") is not None and not isinstance(result["clock"], Mapping):
        raise ValueError("hardware clock must be a mapping or null")
    # A thread count is deliberately absent from _HARDWARE_FIELDS.  It may be
    # retained in raw inventory, but cannot silently become a scaling term.
    reject_leaky_features(result)
    return result


def build_tool_features(
    action: str,
    *,
    script_state: Mapping[str, Any] | None = None,
    action_id: str | None = None,
) -> dict[str, Any]:
    """Build features known before a tool executes.

    ``action`` must be the exact text supplied to the hook.  The returned
    mapping intentionally has no timing, outcome, or post-execution fields.
    """

    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be non-empty text")
    extracted = extract_tool_features(action)
    primary = _primary(action.strip())
    tokens = _tokens(primary)
    pipeline = _pipeline(action.strip())
    test = _test_descriptor(primary, tokens)
    traversal = _traversal_descriptor(primary, tokens)
    find_exec_child = _find_exec_descriptor(tokens)
    operation_class = _semantic_operation_class(
        extracted.operation_class,
        tokens[0] if tokens else "",
        test["test_runner"],
        traversal["traversal_mode"],
        find_exec_child.get("test_runner") if isinstance(find_exec_child, Mapping) else None,
    )
    result = {
        "schema_version": FEATURE_SCHEMA,
        "builder_id": FEATURE_BUILDER_ID,
        "action_id": action_id,
        "action": action,
        "action_sha256": hashlib.sha256(action.encode("utf-8")).hexdigest(),
        "primary_command": primary,
        "tool_name": extracted.tool_name,
        # The historical extractor retains a convenient prefix token, but a
        # model-facing subcommand is only populated for a known dispatcher.
        "subcommand": _semantic_subcommand(tokens),
        "module": _module(tokens),
        "command_prefix": extracted.command_prefix,
        "operation_class": operation_class,
        "declared_command_bytes": extracted.declared_command_bytes,
        "declared_path_count": extracted.declared_path_count,
        "has_pipe": int(pipeline["has_pipeline"]),
        "has_glob": int(extracted.has_glob),
        "path_scope": _path_tokens(tokens),
        "pipeline": pipeline,
        **traversal,
        **test,
        "find_exec_child": find_exec_child,
        "script_state": _validate_state(script_state),
    }
    # A second canonical serialization is useful when a feature row is joined
    # across processes.  It is a hash of the exact pre-execution object.
    result["feature_sha256"] = canonical_sha256(result)
    return result


def build_model_features(
    request: Mapping[str, Any],
    *,
    hardware: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build model request features from request-boundary inputs.

    Prompt/content bytes are not retained.  Usage fields are accepted only
    in a separate retrospective label path, so a serving prediction cannot
    accidentally see realized output tokens or latency.
    """

    if not isinstance(request, Mapping):
        raise ValueError("request must be a mapping")
    leaked = sorted(_LEAKED_FIELDS.intersection(str(key) for key in request))
    if leaked:
        raise ValueError(f"request contains post-execution feature fields: {', '.join(leaked)}")

    def optional_nonnegative(name: str) -> int | None:
        value = request.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer or null")
        return value

    input_tokens = optional_nonnegative("input_tokens")
    context_tokens = optional_nonnegative("context_tokens")
    max_output_tokens = optional_nonnegative("max_output_tokens")
    for name, lower, upper in (("temperature", 0.0, 2.0), ("top_p", 0.0, 1.0)):
        value = request.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not lower <= value <= upper):
            raise ValueError(f"{name} must be between {lower} and {upper} or null")
    seed = request.get("seed")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        raise ValueError("seed must be a non-negative integer or null")
    for name in ("model", "model_revision", "tokenizer_revision", "request_sha256", "mode"):
        value = request.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{name} must be text or null")
    request_digest = request.get("request_sha256")
    if request_digest is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", request_digest):
        raise ValueError("request_sha256 must be a SHA-256 string")
    if context_tokens is None and input_tokens is not None:
        # Context and prompt are semantically distinct.  We do not silently
        # copy one into the other.
        context_reason = "context token count not exposed by serving stack"
    else:
        context_reason = None
    requested_hardware = hardware if hardware is not None else request.get("hardware")
    if requested_hardware is not None and not isinstance(requested_hardware, Mapping):
        raise ValueError("hardware must be a mapping or null")
    hw = _validate_hardware(dict(requested_hardware or {}))
    reject_leaky_features({key: value for key, value in request.items() if key not in {"hardware"}})
    unknown = sorted(set(str(key) for key in request).difference(_MODEL_FIELDS))
    if unknown:
        raise ValueError(f"request has unsupported prospective fields: {', '.join(unknown)}")
    if request.get("mode") not in {None, "prospective"}:
        raise ValueError("build_model_features only accepts prospective mode")
    result = {
        "schema_version": FEATURE_SCHEMA,
        "builder_id": FEATURE_BUILDER_ID,
        "mode": "prospective",
        "input_tokens": input_tokens,
        "context_tokens": context_tokens,
        "max_output_tokens": max_output_tokens,
        "temperature": request.get("temperature"),
        "top_p": request.get("top_p"),
        "seed": seed,
        "model": request.get("model"),
        "model_revision": request.get("model_revision"),
        "tokenizer_revision": request.get("tokenizer_revision"),
        "request_sha256": request_digest,
        "token_semantics": {
            "input_tokens": "prompt/input token count when exposed",
            "context_tokens": "serving context length; never inferred from input_tokens",
            "max_output_tokens": "declared request generation cap",
        },
        "availability": {
            "context_tokens": context_reason,
        },
        "hardware": hw,
    }
    result["feature_sha256"] = canonical_sha256(result)
    return result


def reject_leaky_features(value: Mapping[str, Any]) -> None:
    """Fail closed when a prospective feature mapping contains target data."""

    leaked = sorted(_LEAKED_FIELDS.intersection(str(key) for key in value))
    if leaked:
        raise ValueError(f"leaky feature fields: {', '.join(leaked)}")
    for key, item in value.items():
        if isinstance(item, Mapping):
            reject_leaky_features(item)
        elif isinstance(item, (list, tuple)):
            for entry in item:
                if isinstance(entry, Mapping):
                    reject_leaky_features(entry)


_TOOL_VECTOR_FIELDS = (
    "tool_name",
    "subcommand",
    "module",
    "command_prefix",
    "operation_class",
    "declared_command_bytes",
    "declared_path_count",
    "has_pipe",
    "has_glob",
    "test_runner",
    "test_scope_count",
    "traversal_mode",
    "find_exec",
    "pipeline_edge_count",
    "pipeline_stage_count",
    "pipeline_count",
    "stage_count",
)
_MODEL_VECTOR_FIELDS = (
    "input_tokens",
    "context_tokens",
    "max_output_tokens",
    "temperature",
    "top_p",
    "seed",
    "model",
    "model_revision",
    "tokenizer_revision",
)


def tool_model_vector(features: Mapping[str, Any]) -> dict[str, Any]:
    """Project an action envelope into the learned-vector whitelist.

    Identity, raw action text, timestamps, script source IDs, availability
    strings, and any outcome fields remain in the event envelope but are not
    silently exposed as predictors.
    """

    result: dict[str, Any] = {}
    for field in _TOOL_VECTOR_FIELDS:
        if field in features:
            result[field] = features[field]
    pipeline = features.get("pipeline")
    if isinstance(pipeline, Mapping):
        for field in ("pipeline_edge_count", "pipeline_stage_count", "pipeline_count", "stage_count"):
            if field not in result and field in pipeline:
                result[field] = pipeline[field]
    return result


def model_vector(features: Mapping[str, Any]) -> dict[str, Any]:
    """Project a model-request envelope into stable request/workload terms."""

    result = {field: features[field] for field in _MODEL_VECTOR_FIELDS if field in features}
    hardware = features.get("hardware")
    if isinstance(hardware, Mapping):
        # Keep only terms that a model policy may consume.  The hardware
        # availability map and clock provenance stay descriptive metadata.
        for field in (
            "cpu_frequency_hz",
            "gpu_memory_bandwidth_bytes_per_s",
            "gpu_compute_tflops",
        ):
            if field in hardware:
                result[f"hardware_{field}"] = hardware[field]
    return result


def serialize_model_vector(features: Mapping[str, Any]) -> bytes:
    """Canonical train/serve bytes for the model vector projection."""

    return (canonical_json(model_vector(features)) + "\n").encode("utf-8")


def serialize_tool_vector(features: Mapping[str, Any]) -> bytes:
    return (canonical_json(tool_model_vector(features)) + "\n").encode("utf-8")


def build_conditional_replay_features(
    request: Mapping[str, Any],
    *,
    output_tokens: int | None = None,
    duration_ms: float | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Build explicitly retrospective conditional-replay features.

    Realized labels are useful for replay diagnostics only.  Keeping this
    function separate from :func:`build_model_features` prevents a serving
    caller from accidentally passing a target back into a prospective model.
    """

    result = build_model_features(request)
    if output_tokens is not None and (isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or output_tokens < 0):
        raise ValueError("output_tokens must be a non-negative integer or null")
    if duration_ms is not None and (isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) or duration_ms < 0):
        raise ValueError("duration_ms must be non-negative or null")
    if status is not None and status not in {"success", "failure", "timeout", "unavailable"}:
        raise ValueError("status must be success, failure, timeout, or unavailable")
    result.pop("feature_sha256", None)
    result["mode"] = "conditional_replay"
    result["conditional_replay"] = {
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "status": status,
    }
    result["feature_sha256"] = canonical_sha256(result)
    return result


__all__ = [
    "FEATURE_SCHEMA",
    "FEATURE_BUILDER_ID",
    "build_tool_features",
    "build_model_features",
    "build_conditional_replay_features",
    "model_vector",
    "serialize_model_vector",
    "serialize_tool_vector",
    "tool_model_vector",
    "canonical_json",
    "canonical_sha256",
    "reject_leaky_features",
]
