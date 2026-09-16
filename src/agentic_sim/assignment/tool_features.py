"""Single sealed extractor for historical .traj actions and live step.action.

Historical reconstruction and the live SWE-agent hook MUST call this module.
The extractor never reads execution_time, observations, output tokens, or
evaluator labels.  ``cd /testbed && <real command>`` is reduced to the first
non-cd command so ``cd`` is never the predicted tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import shlex
from typing import Any, Mapping

from agentic_sim.assignment.event_simulator import OPERATION_CLASSES, EventSimulatorError

EXTRACTOR_SCHEMA = "assignment.tool-feature-extractor.v1"
EXTRACTOR_ID = "tool-feature-extractor.v1.cd-skip-20260908"
CD_NAMES = frozenset({"cd", "builtin", "chdir"})
FLAG_PREFIXES = ("-",)
PATHISH_MARKERS = ("/", ".", "~")


def extractor_source_sha256() -> str:
    """Hash of this file; freeze artifacts bind it so train/serve cannot drift."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _unquoted_split(action: str) -> list[str]:
    """Split a shell action on top-level ``&&``, ``;``, and ``|``."""
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    length = len(action)
    while i < length:
        char = action[i]
        if quote:
            buf.append(char)
            if char == quote and (quote != '"' or action[i - 1] != "\\"):
                quote = ""
            i += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            i += 1
            continue
        if char == "&" and i + 1 < length and action[i + 1] == "&":
            segments.append("".join(buf).strip())
            buf = []
            i += 2
            continue
        if char in {";", "|"}:
            segments.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(char)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        segments.append(tail)
    return [segment for segment in segments if segment]


def _tokenize(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _is_cd_segment(segment: str) -> bool:
    tokens = _tokenize(segment)
    if not tokens:
        return False
    name = Path(tokens[0]).name.lower()
    return name in {"cd", "chdir"} and all(
        token.lower() not in {"&&", ";", "|"} for token in tokens
    )


def _primary_segment(action: str) -> str:
    segments = _unquoted_split(action)
    if not segments:
        return action.strip()
    for segment in segments:
        if not _is_cd_segment(segment):
            return segment
    return segments[-1]


def _path_count(tokens: list[str]) -> int:
    count = 0
    for token in tokens[1:]:
        if not token or token.startswith(FLAG_PREFIXES):
            continue
        if any(marker in token for marker in PATHISH_MARKERS):
            count += 1
    return count


def _operation_class(tool: str, subcommand: str, lowered: str, tokens: list[str]) -> str:
    if any(mark in lowered for mark in ("pytest", "unittest", "tox ", "npm test", "cargo test")):
        return "test"
    if tool in {"rg", "grep", "ag", "ack", "search_file", "search_files", "search_dir"}:
        return "search"
    if tool in {"find", "ls", "tree", "du", "list_dir", "list_files"}:
        return "traversal"

    if tool in {"open_file", "view_file"}:
        path_arg = tokens[1] if len(tokens) > 1 else ""
        if "--view_range" in lowered or Path(path_arg).suffix:
            return "read"
        return "traversal"
    if tool in {"str_replace_editor", "edit_file"}:
        path_arg = tokens[2] if len(tokens) > 2 else ""
        if subcommand in {"view", "open", "scroll_up", "scroll_down"}:
            if "--view_range" in lowered or Path(path_arg).suffix:
                return "read"
            return "traversal"
        if subcommand in {"create", "write"}:
            return "write"
        if subcommand in {"str_replace", "insert", "edit", "replace"}:
            return "patch"
        return "shell"

    if any(mark in lowered for mark in ("apply_patch", "patch_file")) or lowered.startswith("sed -i") or "perl -pi" in lowered:
        return "patch"
    if tool in {"cat", "head", "tail", "less", "more", "read_file", "scroll_up", "scroll_down"} or lowered.startswith("sed -n "):
        return "read"
    if tool in {"create_file", "write_file", "touch", "mkdir", "cp", "mv"} or any(
        mark in lowered for mark in (">", "tee ")
    ):
        return "write"
    if lowered:
        return "shell"
    return "other"


@dataclass(frozen=True)
class ToolActionFeatures:
    """Pre-event tool features derived only from the chosen action string."""

    action: str
    primary_command: str
    tool_name: str
    subcommand: str
    command_prefix: str
    operation_class: str
    declared_command_bytes: int
    declared_path_count: int
    has_pipe: int
    has_glob: int
    command_sha256: str
    extractor_id: str
    extractor_sha256: str

    def __post_init__(self) -> None:
        if self.operation_class not in OPERATION_CLASSES:
            raise EventSimulatorError(f"unsupported operation_class: {self.operation_class}")

    def lookup_key(self) -> tuple[str, str, int, int, int, int]:
        return (
            self.operation_class,
            self.tool_name,
            self.declared_path_count,
            self.has_pipe,
            self.has_glob,
            self.declared_command_bytes // 32,
        )

    def prefix_key(self) -> tuple[str, str]:
        return (self.operation_class, self.command_prefix)

    def tool_key(self) -> tuple[str, str]:
        return (self.operation_class, self.tool_name)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": EXTRACTOR_SCHEMA,
            "extractor_id": self.extractor_id,
            "extractor_sha256": self.extractor_sha256,
            "tool_name": self.tool_name,
            "subcommand": self.subcommand,
            "command_prefix": self.command_prefix,
            "operation_class": self.operation_class,
            "declared_command_bytes": self.declared_command_bytes,
            "declared_path_count": self.declared_path_count,
            "has_pipe": self.has_pipe,
            "has_glob": self.has_glob,
            "command_sha256": self.command_sha256,
        }


def extract_tool_features(action: str) -> ToolActionFeatures:
    if not isinstance(action, str) or not action.strip():
        raise EventSimulatorError("tool action must be non-empty text")
    text = action.strip()
    primary = _primary_segment(text)
    tokens = _tokenize(primary)
    tool = Path(tokens[0]).name.lower() if tokens else "unknown"
    subcommand = tokens[1].lower() if len(tokens) > 1 else ""
    prefix = " ".join(tokens[:3]).lower() if tokens else tool
    lowered = primary.strip().lower()
    operation = _operation_class(tool, subcommand, lowered, tokens)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return ToolActionFeatures(
        action=text,
        primary_command=primary,
        tool_name=tool,
        subcommand=subcommand,
        command_prefix=prefix,
        operation_class=operation,
        declared_command_bytes=len(text.encode("utf-8")),
        declared_path_count=_path_count(tokens),
        has_pipe=1 if "|" in text else 0,
        has_glob=1 if any(mark in text for mark in "*?") else 0,
        command_sha256=digest,
        extractor_id=EXTRACTOR_ID,
        extractor_sha256=extractor_source_sha256(),
    )


def extract_tool_features_from_mapping(row: Mapping[str, Any], *, action_field: str = "action") -> ToolActionFeatures:
    action = row.get(action_field)
    if not isinstance(action, str):
        raise EventSimulatorError(f"{action_field} must be a non-empty string")
    return extract_tool_features(action)
