"""Semantic CPU event features and a conservative hierarchical predictor.

This module is deliberately independent from the measured-event extractor in
``cpu_event_model``.  The extractor here only looks at the selected action and
an optional repository identifier.  In particular, it never inspects output,
filesystem counts, subprocess counts, or any latency field.

The model is a semantic overlay on :class:`HierarchicalMedianModel`: the
existing model remains the predictor for compact operation classes while the
overlay is used for the historically broad ``shell``, ``test`` and
``traversal`` classes.  Every semantic table has explicit event and instance
support and stores a distribution rather than only a point estimate.  This
makes sparse backoff inspectable and keeps the overlay deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import ast
from bisect import bisect_left, bisect_right
from functools import lru_cache
import math
from pathlib import PurePosixPath
import re
import shlex
from statistics import median
from typing import Any, Mapping, Sequence

from agentic_sim.assignment.cpu_event_model import HierarchicalMedianModel


SEMANTIC_SCHEMA = "assignment.semantic-cpu.v1"
SEMANTIC_EXTRACTOR_ID = "semantic-cpu-extractor.v1.shlex-punctuation-20260908"
GATE_PERCENT = 25.0
ROUTED_OPERATION_CLASSES = frozenset({"shell", "test", "traversal"})

_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_KNOWN_EDITORS = frozenset(
    {
        "str_replace_editor",
        "edit_file",
        "open_file",
        "view_file",
        "apply_patch",
        "patch_file",
        "vim",
        "vi",
        "nvim",
        "nano",
        "emacs",
    }
)
_SEARCH_EXECUTABLES = frozenset(
    {"rg", "ripgrep", "grep", "egrep", "fgrep", "ag", "ack", "search_file", "search_files", "search_dir"}
)
_TRAVERSAL_EXECUTABLES = frozenset(
    {"find", "fd", "ls", "tree", "du", "list_dir", "list_files", "locate"}
)
_TEST_EXECUTABLES = frozenset({"pytest", "py.test", "unittest", "tox", "tox-uv"})
_GIT_PAGER_COMMANDS = frozenset({"log", "show", "diff"})
_ENV_WRAPPERS = frozenset({"env", "command", "sudo", "doas", "nice", "nohup"})
_FIND_PREDICATES = ("name", "type", "prune", "maxdepth")


def _finite_positive(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(number):
        return 1.0
    return max(1e-6, number)


def _clip_ms(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("prediction must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("prediction must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError("prediction must be finite")
    return max(1e-6, number)


def _observed_label(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("observed_ms must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("observed_ms must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("observed_ms must be a finite positive number")
    return number


def _ape(predicted: float, observed: float) -> float:
    return abs(predicted - observed) / max(observed, 1e-9) * 100.0


def _bucket_count(value: int) -> int:
    """Stable coarse bucket used for declared counts, never actual counts."""

    if value <= 0:
        return 0
    if value == 1:
        return 1
    if value == 2:
        return 2
    return 3


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    ordered = sorted(_finite_positive(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(quantile * (len(ordered) - 1)))))
    return ordered[index]


def _stable_median(values: Sequence[float]) -> float:
    finite = [_finite_positive(value) for value in values]
    return float(median(finite)) if finite else 1.0


def _gate_center(values: Sequence[float]) -> float:
    """Choose a point maximizing the fixed 25% gate.

    The gate is not tuned.  Candidate endpoints and observations contain an
    optimum for the finite interval-overlap problem.  Ties use minimum MAPE,
    then the smaller log-space point for a stable result.
    """

    observations = sorted(_finite_positive(value) for value in values)
    if not observations:
        return 1.0
    candidates = sorted(
        {
            point
            for observation in observations
            for point in (observation, observation * 0.75, observation * 1.25)
        }
    )
    reciprocals = [1.0 / observation for observation in observations]
    prefix = [0.0]
    for reciprocal in reciprocals:
        prefix.append(prefix[-1] + reciprocal)

    def coverage_and_mape(point: float) -> tuple[int, float]:
        # |p-y|/y <= .25 iff p/1.25 <= y <= p/.75.  The sorted reciprocal
        # prefix gives exact finite-sample MAPE in O(log n) per candidate.
        left = bisect_left(observations, point / 1.25)
        right = bisect_right(observations, point / 0.75)
        # Correct only boundary floating-point cases so the interval score
        # has precisely the same strict comparison as the gate itself.
        if left < right and not (abs(point - observations[left]) / observations[left] <= 0.25):
            left = bisect_right(observations, observations[left])
        if left < right and not (abs(point - observations[right - 1]) / observations[right - 1] <= 0.25):
            right = bisect_left(observations, observations[right - 1])
        count = right - left
        split = bisect_right(observations, point)
        below = point * prefix[split] - split
        above = (len(observations) - split) - point * (prefix[-1] - prefix[split])
        return count, below + above

    scored = [(point, *coverage_and_mape(point)) for point in candidates]
    maximum = max(item[1] for item in scored)
    tied = [item for item in scored if item[1] == maximum]
    min_mape = min(item[2] for item in tied)
    mape_tied = [item[0] for item in tied if abs(item[2] - min_mape) <= 1e-12]
    return float(min(mape_tied, key=lambda point: (math.log(max(point, 1e-12)), point)))


def _split_shell(action: str) -> tuple[list[str], list[str]]:
    """Split top-level shell commands while preserving quoted/escaped text.

    Returned separators are the operators between segments.  A single ``|``
    is a pipeline edge; ``||`` is intentionally different.  A backslash-
    escaped ``;`` (the common ``find -exec ... \;`` spelling) stays in its
    command, as do quoted separators.
    """

    segments: list[str] = []
    separators: list[str] = []
    buffer: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(action):
        char = action[index]
        if escaped:
            buffer.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            buffer.append(char)
            escaped = True
            index += 1
            continue
        if quote:
            buffer.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buffer.append(char)
            index += 1
            continue
        operator = ""
        if char in {"|", "&"} and index + 1 < len(action) and action[index + 1] == char:
            operator = char + char
        elif char in {"|", ";"}:
            operator = char
        elif char == "\n":
            operator = ";"
        if operator:
            segments.append("".join(buffer).strip())
            separators.append(operator)
            buffer = []
            index += len(operator)
            continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    segments.append(tail)
    return [segment for segment in segments if segment], separators


def _unquoted_operator(action: str, needle: str) -> bool:
    """Whether ``needle`` occurs outside quotes and escapes."""

    quote = ""
    escaped = False
    index = 0
    while index < len(action):
        char = action[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if action.startswith(needle, index):
            return True
        index += 1
    return False


def _tokenize(command: str) -> list[str]:
    """Conservative POSIX tokenization with shell punctuation preserved."""

    lexer = shlex.shlex(command, posix=True, punctuation_chars="|;&<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except (ValueError, TypeError):
        # Malformed quotes should not make prediction nondeterministic or
        # crash a live action hook.  This fallback remains deliberately crude.
        try:
            return shlex.split(command, posix=True)
        except ValueError:
            return command.split()


def _basename(token: str) -> str:
    value = token.strip()
    if not value:
        return "unknown"
    # PurePosixPath handles both absolute tools and ./tool without reading
    # the filesystem.  Backslashes are ordinary POSIX characters here.
    return PurePosixPath(value).name.lower() or "unknown"


def _is_assignment(token: str) -> bool:
    return bool(_ASSIGNMENT_RE.match(token))


def _strip_wrapper_prefix(tokens: Sequence[str]) -> tuple[int, dict[str, str]]:
    """Return executable index and environment assignments from wrappers."""

    index = 0
    env_values: dict[str, str] = {}
    while index < len(tokens) and _is_assignment(tokens[index]):
        name, value = tokens[index].split("=", 1)
        env_values[name.upper()] = value
        index += 1
    while index < len(tokens):
        name = _basename(tokens[index])
        if name not in _ENV_WRAPPERS:
            break
        index += 1
        if name == "env":
            while index < len(tokens):
                token = tokens[index]
                if _is_assignment(token):
                    key, value = token.split("=", 1)
                    env_values[key.upper()] = value
                    index += 1
                    continue
                if token in {"-i", "--ignore-environment"}:
                    env_values.clear()
                    index += 1
                    continue
                if token == "--":
                    index += 1
                    break
                if token in {"-u", "--unset"}:
                    index += 2
                    continue
                if token.startswith("--unset="):
                    index += 1
                    continue
                break
        elif name == "command":
            while index < len(tokens) and tokens[index] in {"-v", "-V", "--"}:
                index += 1
        elif name in {"sudo", "doas"}:
            # Common sudo/doas options.  Unknown options stop the scan rather
            # than risking that an operand is mistaken for the executable.
            while index < len(tokens) and tokens[index].startswith("-"):
                token = tokens[index]
                index += 1
                if token in {"-u", "-g", "-h", "-C", "--user", "--group", "--chdir"}:
                    index += 1
        elif name == "nice":
            if index < len(tokens) and tokens[index] == "-n":
                index += 2
        elif name == "nohup":
            pass
        while index < len(tokens) and _is_assignment(tokens[index]):
            key, value = tokens[index].split("=", 1)
            env_values[key.upper()] = value
            index += 1
    return index, env_values


@dataclass(frozen=True)
class _Command:
    text: str
    tokens: tuple[str, ...]
    executable: str
    executable_index: int
    assignments: tuple[tuple[str, str], ...]

    @property
    def active_tokens(self) -> tuple[str, ...]:
        return self.tokens[self.executable_index :]


def _command_info(text: str) -> _Command:
    tokens = _tokenize(text)
    index, assignments = _strip_wrapper_prefix(tokens)
    executable = _basename(tokens[index]) if index < len(tokens) else "unknown"
    return _Command(
        text=text,
        tokens=tuple(tokens),
        executable=executable,
        executable_index=index,
        assignments=tuple(sorted(assignments.items())),
    )


def _is_cd(command: _Command) -> bool:
    return command.executable in {"cd", "chdir"}


def _non_cd_commands(action: str) -> tuple[list[_Command], list[str]]:
    segments, separators = _split_shell(action)
    commands = [_command_info(segment) for segment in segments]
    return [command for command in commands if not _is_cd(command)], separators


def _first_command(commands: Sequence[_Command]) -> _Command:
    return commands[0] if commands else _Command("", tuple(), "unknown", 0, tuple())


def _git_subcommand(command: _Command) -> str:
    if command.executable != "git":
        return ""
    tokens = list(command.active_tokens[1:])
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1].lower() if index + 1 < len(tokens) else ""
        if token in {"-C", "-c", "--exec-path"}:
            index += 2
            continue
        if token.startswith(("--git-dir=", "--work-tree=", "--namespace=", "--config-env=")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token.lower()
    return ""


def _git_pager_mode(commands: Sequence[_Command], separators: Sequence[str], action: str) -> str:
    git_commands = [command for command in commands if command.executable == "git"]
    target = next((command for command in git_commands if _git_subcommand(command) in _GIT_PAGER_COMMANDS), None)
    if target is None:
        return "none"
    active = list(target.active_tokens)
    assignments = dict(target.assignments)
    if "--no-pager" in active or assignments.get("GIT_PAGER", "").strip().lower() in {"cat", "-", "none", "off"}:
        return "explicit_no_pager"
    for index, token in enumerate(active):
        if token.startswith("-c") and "pager" in token.lower() and "=cat" in token.lower():
            return "explicit_no_pager"
        if token == "-c" and index + 1 < len(active):
            config = active[index + 1].lower()
            if "pager" in config and "=cat" in config:
                return "explicit_no_pager"
    # Operators are computed on the complete action, but a single pipe or
    # redirect touching this command is enough for the semantic mode.
    if "|" in separators:
        return "piped"
    if _unquoted_operator(action, ">") or _unquoted_operator(action, "<"):
        return "redirected"
    return "tty_candidate"


def _actual_test_runner(command: _Command) -> str:
    executable = command.executable
    active = list(command.active_tokens)
    if executable in _TEST_EXECUTABLES:
        return {"tox-uv": "tox"}.get(executable, executable)
    if executable == "python" or executable == "python3" or executable.startswith("python3."):
        for index, token in enumerate(active[1:], start=1):
            if token in {"-c", "--command", "<<", "<<<"}:
                return ""
            if token in {"-m", "--module"} and index + 1 < len(active):
                module = active[index + 1].lower().split(".", 1)[0]
                if module in {"pytest", "unittest"}:
                    return module
                if module == "django" and "test" in active[index + 2 :]:
                    return "django"
                return ""
        for token in active[1:]:
            lower = token.lower()
            if lower.endswith("/manage.py") or lower == "manage.py":
                if "test" in active[active.index(token) + 1 :]:
                    return "manage.py"
            if lower.endswith("/tests/runtests.py") or lower == "tests/runtests.py" or lower == "runtests.py":
                return "runtests.py"
            if lower.endswith(".py"):
                return ""
    if executable == "manage.py" and "test" in active[1:]:
        return "manage.py"
    if executable == "runtests.py" or executable.endswith("/tests/runtests.py"):
        return "runtests.py"
    return ""


def _python_module_name(command: _Command) -> str:
    if command.executable not in {"python", "python3"} and not command.executable.startswith("python3."):
        return ""
    active = list(command.active_tokens)
    for index, token in enumerate(active):
        if token in {"-m", "--module"} and index + 1 < len(active):
            module = active[index + 1].strip()
            if module and not module.startswith("-"):
                return module
    return ""


def _is_editor(command: _Command) -> bool:
    return command.executable in _KNOWN_EDITORS or command.executable.startswith("str_replace_editor")


def _is_search(command: _Command) -> bool:
    if command.executable in _SEARCH_EXECUTABLES:
        return True
    return command.executable == "git" and _git_subcommand(command) == "grep"


def _editor_operation(command: _Command) -> str:
    if command.executable in {"apply_patch", "patch_file", "vim", "vi", "nvim", "nano", "emacs"}:
        return "editor_patch" if command.executable in {"apply_patch", "patch_file"} else "editor"
    active = list(command.active_tokens)
    subcommand = active[1].lower() if len(active) > 1 else ""
    if subcommand in {"view", "open", "scroll_up", "scroll_down"}:
        return "editor_view"
    if subcommand in {"create", "write"}:
        return "editor_write"
    if subcommand in {"str_replace", "insert", "edit", "replace"}:
        return "editor_patch"
    return "editor"


def _python_inline_imports(command: _Command, action: str, runner: str) -> tuple[str, ...]:
    if command.executable not in {"python", "python3"} and not command.executable.startswith("python3."):
        return tuple()
    active = list(command.active_tokens)
    if _unquoted_operator(action, "<<"):
        return ("opaque",)
    for index, token in enumerate(active):
        if token not in {"-c", "--command"}:
            continue
        if index + 1 >= len(active):
            return ("opaque",)
        source = active[index + 1]
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, TypeError):
            return ("opaque",)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".", 1)[0].lower() for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".", 1)[0].lower())
        return tuple(sorted(modules)) if modules else ("inline",)
    for index, token in enumerate(active):
        if token in {"-m", "--module"} and index + 1 < len(active):
            return (active[index + 1].split(".", 1)[0].lower(),)
    for token in active[1:]:
        lower = token.lower()
        if lower.endswith("/manage.py") or lower in {"manage.py", "tests/runtests.py", "runtests.py"}:
            return ("known_test_runner",)
        if lower.endswith(".py"):
            return ("script",)
    return ("python",)


def _metadata_operation(command: _Command) -> str | None:
    active = list(command.active_tokens)[1:]
    for token in active:
        if token in {"-c", "--command", "<<", "<<<"}:
            return None
        lower = token.lower()
        if lower in {"--version", "-version"}:
            return "version"
        if lower in {"--help", "-h", "-help"}:
            return "help"
    return None


def _predicate_flags(commands: Sequence[_Command]) -> tuple[str, ...]:
    flags: set[str] = set()
    for command in commands:
        if command.executable != "find":
            continue
        lowered = [token.lower() for token in command.active_tokens]
        for token in lowered:
            if token in {"-name", "-iname", "-path", "-ipath", "-regex", "-iregex"}:
                flags.add("name")
            elif token in {"-type", "-xtype"}:
                flags.add("type")
            elif token == "-prune":
                flags.add("prune")
            elif token in {"-maxdepth", "-mindepth"} or token.startswith(("-maxdepth=", "-mindepth=")):
                flags.add("maxdepth")
    return tuple(flag for flag in _FIND_PREDICATES if flag in flags)


def _find_exec_mode(commands: Sequence[_Command]) -> str:
    for command in commands:
        if command.executable != "find":
            continue
        active = list(command.active_tokens)
        for index, token in enumerate(active):
            if token not in {"-exec", "-execdir"}:
                continue
            tail = active[index + 1 :]
            if "+" in tail:
                return "batched"
            if ";" in tail:
                return "per_file"
    return "none"


def _recursive(commands: Sequence[_Command], action: str) -> int:
    del action  # Kept in the signature to make the source contract explicit.
    flags = {"-r", "-R", "--recursive", "--recurse", "-recursive"}
    for command in commands:
        active = list(command.active_tokens)
        if any(token in flags or token.lower() in {item.lower() for item in flags} for token in active):
            return 1
        if command.executable in {"find", "tree", "fd"}:
            return 1
        if command.executable in {"rg", "ripgrep"}:
            # rg is recursive by default when no explicit file is supplied.
            operands = _declared_operands(command)
            if not any(_looks_path(operand) and PurePosixPath(operand).suffix for operand in operands):
                return 1
        if command.executable == "git" and _git_subcommand(command) == "grep":
            return 1
    return 0


def _option_value_tokens(tokens: Sequence[str], start: int, executable: str) -> set[int]:
    """Indexes consumed as option values, used only for declared operands."""

    consumed: set[int] = set()
    options_with_value = {
        "-e",
        "--regexp",
        "--include",
        "--exclude",
        "--glob",
        "-g",
        "-f",
        "--file",
        "-C",
        "-A",
        "-B",
        "-m",
        "--max-count",
        "--maxdepth",
        "-mindepth",
        "-type",
        "-name",
        "-iname",
        "-path",
        "-ipath",
        "-regex",
        "-iregex",
    }
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token in options_with_value and index + 1 < len(tokens):
            consumed.add(index + 1)
            index += 2
            continue
        if executable == "git" and token in {"-C", "-c", "--exec-path"} and index + 1 < len(tokens):
            consumed.add(index + 1)
            index += 2
            continue
        index += 1
    return consumed


def _declared_operands(command: _Command) -> list[str]:
    if not command.tokens or command.executable == "unknown":
        return []
    active = list(command.active_tokens)[1:]
    if command.executable == "git":
        subcommand = _git_subcommand(command)
        removed_subcommand = False
        result: list[str] = []
        index = 0
        consumed = _option_value_tokens(active, 0, "git")
        while index < len(active):
            token = active[index]
            if not removed_subcommand and token == subcommand and not token.startswith("-"):
                removed_subcommand = True
                index += 1
                continue
            if index in consumed or token in {"--", "|", ">", ">>", "<", "<<"} or token.startswith("-"):
                index += 1
                continue
            result.append(token)
            index += 1
        return result
    consumed = _option_value_tokens(active, 0, command.executable)
    result = []
    for index, token in enumerate(active):
        if index in consumed or token in {"|", ">", ">>", "<", "<<", ";"}:
            continue
        if token.startswith("-"):
            continue
        result.append(token)
    return result


def _looks_path(token: str) -> bool:
    value = token.strip()
    if not value or value in {".", "..", "/", "~"}:
        return True
    if value.startswith(("/", "./", "../", "~/")) or "/" in value:
        return True
    if any(value.lower().endswith(suffix) for suffix in (".py", ".js", ".ts", ".json", ".yaml", ".yml", ".toml", ".txt", ".md", ".c", ".cc", ".cpp", ".go", ".rs")):
        return True
    return False


def _scope_depth(commands: Sequence[_Command], scope: str, repository: str) -> int:
    if scope != "subtree":
        return 0
    repository_value = repository.rstrip("/")
    root_base = PurePosixPath(repository_value).name if repository_value else ""
    for command in commands:
        for operand in _declared_operands(command):
            if not _looks_path(operand) or operand in {".", "..", "/"}:
                continue
            normalized = operand.rstrip("/")
            if repository_value and normalized.startswith(repository_value + "/"):
                relative = normalized[len(repository_value) + 1 :]
            elif normalized.startswith("/testbed/"):
                relative = normalized[len("/testbed/") :]
            elif normalized == root_base:
                relative = ""
            else:
                relative = normalized.lstrip("./")
            depth = len([part for part in PurePosixPath(relative).parts if part not in {"", ".", ".."}])
            return _bucket_count(depth)
    return 1


def _scope(
    commands: Sequence[_Command],
    semantic_class: str,
    runner: str,
    recursive: int,
    repository: str,
) -> tuple[str, int]:
    operands = [operand for command in commands for operand in _declared_operands(command)]
    if runner:
        if any("::" in operand for operand in operands) or any(
            not operand.lower().endswith(".py")
            and re.match(r"^(?:tests?\.)?[^/]+\.[^/]+$", operand)
            for operand in operands
        ):
            return "case", 0
        if any(command.executable in {"manage.py", "runtests.py"} for command in commands):
            return "file", 0
        if any(
            command.executable in {"python", "python3"}
            and any(token.lower().endswith(("/manage.py", "/tests/runtests.py", "manage.py", "tests/runtests.py")) for token in command.active_tokens)
            for command in commands
        ):
            return "file", 0
        files = [operand for operand in operands if _looks_path(operand) and operand not in {".", "..", "/"}]
        if files:
            return "file", 0
        roots = [operand for operand in operands if operand.rstrip("/") in {".", "/", "/testbed", repository.rstrip("/") if repository else ""}]
        if roots:
            return "root", 0
        directories = [operand for operand in operands if operand.lower() in {"tests", "test", "src", "app", "lib", "docs"}]
        if directories:
            return "subtree", _scope_depth(commands, "subtree", repository)
        return ("subtree", 3) if recursive else ("unspecified", 0)
    if semantic_class == "editor":
        files = [operand for operand in operands if _looks_path(operand) and operand not in {".", "..", "/"}]
        return ("file", 0) if files else ("unspecified", 0)
    if semantic_class in {"search", "traversal"}:
        path_operands = [operand for operand in operands if _looks_path(operand)]
        if path_operands:
            if any(
                PurePosixPath(operand).suffix
                and operand not in {".", "..", "/"}
                for operand in path_operands
            ):
                return "file", 0
            repository_value = repository.rstrip("/")
            repo_base = PurePosixPath(repository_value).name if repository_value else ""
            exact_root = any(
                operand.rstrip("/") in {".", "/", "/testbed", repository_value, repo_base}
                for operand in path_operands
            )
            return ("root", 0) if exact_root else ("subtree", _scope_depth(commands, "subtree", repository))
        return ("subtree", _scope_depth(commands, "subtree", repository)) if recursive else ("unspecified", 0)
    if any(command.executable == "git" and _git_subcommand(command) in _GIT_PAGER_COMMANDS for command in commands):
        return "root", 0
    return "unspecified", 0


def _semantic_class_and_operation(
    commands: Sequence[_Command], action: str
) -> tuple[str, str, str, tuple[str, ...]]:
    """Apply editor/search/test precedence and return class, operation, runner, imports."""

    command = _first_command(commands)
    if _is_editor(command):
        return "editor", _editor_operation(command), "", _python_inline_imports(command, action, "")
    if _is_search(command):
        operation = "git_grep" if command.executable == "git" else "search"
        return "search", operation, "", _python_inline_imports(command, action, "")
    # Metadata probes are cheap even when the executable is normally a test
    # runner (``pytest --help``); keep them out of the heavy test group.
    command = _first_command(commands)
    metadata = _metadata_operation(command)
    if metadata:
        return "metadata", metadata, "", _python_inline_imports(command, action, "")
    runner = _actual_test_runner(command)
    if runner:
        return "test", "test", runner, _python_inline_imports(command, action, runner)
    if command.executable in _TRAVERSAL_EXECUTABLES:
        operation = "find" if command.executable == "find" else "traversal"
        return "traversal", operation, "", _python_inline_imports(command, action, "")
    if command.executable == "git":
        subcommand = _git_subcommand(command)
        if subcommand == "grep":
            return "search", "git_grep", "", tuple()
        if subcommand in {"ls-files", "ls-tree"}:
            return "traversal", "git_" + subcommand.replace("-", "_"), "", tuple()
        return "shell", "git_" + subcommand if subcommand else "git", "", tuple()
    imports = _python_inline_imports(command, action, "")
    if command.executable in {"python", "python3"} or command.executable.startswith("python3."):
        active = list(command.active_tokens)
        module_name = _python_module_name(command)
        if imports == ("opaque",):
            operation = "python_inline_opaque"
        elif any(token in {"-c", "--command"} for token in active):
            operation = "python_inline"
        elif imports and imports[0] not in {"python", "script", "known_test_runner"}:
            operation = "python_module"
        elif imports == ("script",):
            operation = "python_script"
        else:
            operation = "python"
        runner = "module:" + module_name if module_name else ""
        return "shell", operation, runner, imports
    return "shell", "shell" if command.executable != "unknown" else "unknown", "", imports


def _semantic_features_uncached(action: str, repository: str = "") -> dict[str, Any]:
    """Extract deterministic semantic descriptors from an action.

    The return value intentionally contains no action text, repository text,
    exact target path, measured duration, or observed filesystem/subprocess
    count.  Tuple fields are kept as tuples for stable model keys.
    """

    if not isinstance(action, str) or not action.strip():
        action = ""
    text = action.strip()
    commands, separators = _non_cd_commands(text)
    if not commands:
        commands = [_Command("", tuple(), "unknown", 0, tuple())]
    semantic_class, operation, runner, python_imports = _semantic_class_and_operation(commands, text)
    executable = _first_command(commands).executable
    recursive = _recursive(commands, text)
    scope, depth = _scope(commands, semantic_class, runner, recursive, repository if isinstance(repository, str) else "")
    operands = [operand for command in commands for operand in _declared_operands(command)]
    pipeline = "|" in separators
    pipeline_commands: list[str] = []
    if pipeline:
        # The sequence is deliberately executable names only; wrappers and
        # environment assignments never become separate semantic stages.
        for command in commands:
            if command.executable != "unknown":
                pipeline_commands.append(command.executable)
    else:
        pipeline_commands = [executable] if executable != "unknown" else []
    pipeline_tuple = tuple(pipeline_commands)
    pager_mode = _git_pager_mode(commands, separators, text)
    predicate = _predicate_flags(commands)
    find_mode = _find_exec_mode(commands)
    metadata_operation = operation if semantic_class == "metadata" else None
    redirected = int(_unquoted_operator(text, ">") or _unquoted_operator(text, "<"))
    if pager_mode != "none":
        execution_mode = pager_mode
    elif pipeline:
        execution_mode = "piped"
    elif redirected:
        execution_mode = "redirected"
    else:
        execution_mode = "normal"
    return {
        "schema_version": SEMANTIC_SCHEMA,
        "extractor_id": SEMANTIC_EXTRACTOR_ID,
        "semantic_class": semantic_class,
        "semantic_operation": operation,
        "executable": executable,
        "runner": runner,
        "operation": operation,
        "scope": scope,
        "scope_depth_bucket": depth,
        "operand_count_bucket": _bucket_count(len(operands)),
        "operand_count": len(operands),
        "pipeline_executables": pipeline_tuple,
        "pipeline_stage_bucket": _bucket_count(len(pipeline_tuple)) if pipeline else 0,
        "pipeline_stage_count": len(pipeline_tuple) if pipeline else 0,
        "recursive": recursive,
        "find_exec_mode": find_mode,
        "find_predicate": predicate,
        "find_predicates": predicate,
        "find_predicate_name": int("name" in predicate),
        "find_predicate_type": int("type" in predicate),
        "find_predicate_prune": int("prune" in predicate),
        "find_predicate_maxdepth": int("maxdepth" in predicate),
        "git_pager_susceptibility": pager_mode,
        "git_pager_mode": pager_mode,
        "python_inline_imports": python_imports,
        "metadata_operation": metadata_operation,
        "redirected": redirected,
        "execution_mode": execution_mode,
        "mode": execution_mode,
    }


@lru_cache(maxsize=8192)
def _semantic_features_cached(action: str, repository: str) -> dict[str, Any]:
    return _semantic_features_uncached(action, repository)


def semantic_features(action: str, repository: str = "") -> dict[str, Any]:
    """Return a fresh, deterministic copy of cached semantic descriptors."""

    normalized_action = action.strip() if isinstance(action, str) else ""
    normalized_repository = repository if isinstance(repository, str) else ""
    return dict(_semantic_features_cached(normalized_action, normalized_repository))


def _repo_key(repository: Any) -> str:
    if repository is None:
        return ""
    value = str(repository).strip()
    return value.lower()


def _row_repository(row: Mapping[str, Any]) -> str:
    for key in ("repository", "repo", "repository_id", "repo_id"):
        value = row.get(key)
        if isinstance(value, str):
            return value
    return ""


def _fallback_features(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the same key vocabulary when an old row has no action text."""

    original = str(row.get("operation_class") or row.get("original_operation_class") or "shell")
    executable = str(row.get("tool_name") or row.get("launch_family") or "unknown").lower()
    if executable.startswith("editor:"):
        executable = executable.split(":", 1)[1] or "unknown"
    operation = str(row.get("subcommand") or original)
    pager = "piped" if int(row.get("has_pipe") or 0) else "none"
    if executable == "git" and operation in _GIT_PAGER_COMMANDS:
        pager = "piped" if int(row.get("has_pipe") or 0) else "tty_candidate"
    recursive = int(row.get("recursive") or 0)
    return {
        "schema_version": SEMANTIC_SCHEMA,
        "extractor_id": SEMANTIC_EXTRACTOR_ID,
        "semantic_class": original,
        "semantic_operation": operation,
        "executable": executable,
        "runner": "" if original != "test" else "test",
        "operation": operation,
        "scope": "unspecified",
        "scope_depth_bucket": 0,
        "operand_count_bucket": _bucket_count(int(row.get("declared_path_count") or 0)),
        "operand_count": int(row.get("declared_path_count") or 0),
        "pipeline_executables": (executable,) if executable != "unknown" else tuple(),
        "pipeline_stage_bucket": 1 if int(row.get("has_pipe") or 0) else 0,
        "pipeline_stage_count": 1 if int(row.get("has_pipe") or 0) else 0,
        "recursive": recursive,
        "find_exec_mode": "none",
        "find_predicate": tuple(),
        "find_predicates": tuple(),
        "find_predicate_name": 0,
        "find_predicate_type": 0,
        "find_predicate_prune": 0,
        "find_predicate_maxdepth": 0,
        "git_pager_susceptibility": pager,
        "git_pager_mode": pager,
        "python_inline_imports": tuple(),
        "metadata_operation": None,
        "redirected": 0,
        "execution_mode": pager if pager != "none" else "normal",
        "mode": pager if pager != "none" else "normal",
    }


def _features_for_row(row: Mapping[str, Any]) -> dict[str, Any]:
    action = row.get("action")
    if isinstance(action, str) and action.strip():
        return semantic_features(action, _row_repository(row))
    return _fallback_features(row)


def _has_action(row: Mapping[str, Any]) -> bool:
    action = row.get("action")
    return isinstance(action, str) and bool(action.strip())


def _original_class(row: Mapping[str, Any], features: Mapping[str, Any]) -> str:
    value = row.get("operation_class") or row.get("original_operation_class")
    return str(value) if value else str(features.get("semantic_class") or "shell")


def _baseline_row(row: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    operation_class = _original_class(row, features)
    out.setdefault("operation_class", operation_class)
    out.setdefault("tool_name", features.get("executable", "unknown"))
    out.setdefault("subcommand", features.get("operation", ""))
    out.setdefault("launch_family", out.get("tool_name") or "unknown")
    out.setdefault("recursive", int(features.get("recursive") or 0))
    out.setdefault("has_pipe", int(features.get("pipeline_stage_count") or 0) > 1)
    out.setdefault("declared_path_count", int(out.get("declared_path_count") or features.get("operand_count") or 0))
    return out


@dataclass
class _Distribution:
    values: list[float]
    instances: set[str]

    def summary(self, center: str) -> dict[str, Any]:
        values = [_finite_positive(value) for value in self.values]
        if center == "gate":
            prediction = _gate_center(values)
        else:
            prediction = _stable_median(values)
        return {
            "n": len(values),
            "distinct_instances": len(self.instances),
            "p10": _percentile(values, 0.10),
            "median": _stable_median(values),
            "p90": _percentile(values, 0.90),
            "prediction": prediction,
            "center": prediction,
            "within_25_rate": sum(_ape(prediction, value) <= GATE_PERCENT for value in values) / len(values)
            if values
            else None,
            "gate_coverage": sum(_ape(prediction, value) <= GATE_PERCENT for value in values) / len(values)
            if values
            else None,
            "empirical_gate_coverage": sum(_ape(prediction, value) <= GATE_PERCENT for value in values) / len(values)
            if values
            else None,
            "mean_ape": sum(_ape(prediction, value) for value in values) / len(values) if values else None,
        }


def _json_value(value: Any) -> Any:
    if isinstance(value, tuple):
        return {"__tuple__": [_json_value(item) for item in value]}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _from_json_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__tuple__"}:
        return tuple(_from_json_value(item) for item in value["__tuple__"])
    if isinstance(value, list):
        return [_from_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _from_json_value(item) for key, item in value.items()}
    return value


class SemanticCpuModel:
    """Semantic overlay with deterministic support-aware hierarchical backoff."""

    def __init__(
        self,
        center: str = "median",
        use_repository: bool = True,
        min_count: int = 8,
        min_instances: int = 3,
    ) -> None:
        if center not in {"median", "gate"}:
            raise ValueError("center must be 'median' or 'gate'")
        if int(min_count) <= 0 or int(min_instances) <= 0:
            raise ValueError("min_count and min_instances must be positive")
        self.center = center
        self.use_repository = bool(use_repository)
        self.min_count = int(min_count)
        self.min_instances = int(min_instances)
        self.baseline = HierarchicalMedianModel(min_count=self.min_count)
        self.tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {
            "repo_fine": {},
            "fine": {},
            "repo_coarse": {},
            "coarse": {},
            "operation_executable": {},
            "semantic_class": {},
        }
        self.global_distribution: dict[str, Any] = {
            "n": 0,
            "distinct_instances": 0,
            "p10": None,
            "median": 1.0,
            "p90": None,
            "prediction": 1.0,
            "center": 1.0,
            "within_25_rate": None,
            "gate_coverage": None,
            "empirical_gate_coverage": None,
            "mean_ape": None,
        }
        self._fitted = False

    @staticmethod
    def _instance_id(row: Mapping[str, Any]) -> str:
        value = row.get("instance_id")
        if value is not None and str(value).strip():
            return "instance:" + str(value)
        value = row.get("run_id")
        if value is not None and str(value).strip():
            return "run:" + str(value)
        return "__missing_instance__"

    @staticmethod
    def _fine_key(features: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(features.get("semantic_class") or "shell"),
            str(features.get("operation") or "unknown"),
            str(features.get("executable") or "unknown"),
            str(features.get("runner") or ""),
            str(features.get("execution_mode") or "normal"),
            str(features.get("git_pager_susceptibility") or "none"),
            int(features.get("operand_count_bucket") or 0),
            int(features.get("recursive") or 0),
            str(features.get("find_exec_mode") or "none"),
            tuple(features.get("python_inline_imports") or tuple()),
        )

    @staticmethod
    def _coarse_operation(features: Mapping[str, Any]) -> str:
        if str(features.get("executable")) == "git" and str(features.get("git_pager_susceptibility")) != "none":
            # ``show`` must be allowed to back off to a supported piped
            # ``log``/``diff`` group, never to an unpiped pager family.
            return "git_pager_family"
        return str(features.get("operation") or "unknown")

    @classmethod
    def _coarse_key(cls, features: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            str(features.get("semantic_class") or "shell"),
            str(features.get("executable") or "unknown"),
            str(features.get("runner") or ""),
            str(features.get("execution_mode") or "normal"),
            str(features.get("git_pager_susceptibility") or "none"),
            cls._coarse_operation(features),
            str(features.get("find_exec_mode") or "none"),
            tuple(features.get("python_inline_imports") or tuple()),
        )

    @staticmethod
    def _operation_executable_key(features: Mapping[str, Any]) -> tuple[Any, ...]:
        operation = str(features.get("operation") or "unknown")
        if str(features.get("executable")) == "git" and str(features.get("git_pager_susceptibility")) != "none":
            operation = "git_pager_family"
        return (
            str(features.get("semantic_class") or "shell"),
            operation,
            str(features.get("executable") or "unknown"),
            str(features.get("runner") or ""),
            str(features.get("execution_mode") or "normal"),
            str(features.get("git_pager_susceptibility") or "none"),
            str(features.get("find_exec_mode") or "none"),
            tuple(features.get("python_inline_imports") or tuple()),
        )

    @staticmethod
    def _class_key(features: Mapping[str, Any]) -> tuple[Any, ...]:
        return (str(features.get("semantic_class") or "shell"),)

    def _key_candidates(self, features: Mapping[str, Any], repository: str) -> list[tuple[str, tuple[Any, ...], int, int]]:
        fine = self._fine_key(features)
        coarse = self._coarse_key(features)
        operation_executable = self._operation_executable_key(features)
        semantic_class = self._class_key(features)
        candidates: list[tuple[str, tuple[Any, ...], int, int]] = []
        if self.use_repository and repository:
            candidates.append(("repo_fine", (repository,) + fine, self.min_count * 2, max(4, self.min_instances)))
        candidates.append(("fine", fine, self.min_count, self.min_instances))
        if self.use_repository and repository:
            candidates.append(("repo_coarse", (repository,) + coarse, self.min_count * 2, max(4, self.min_instances)))
        candidates.append(("coarse", coarse, self.min_count, self.min_instances))
        candidates.append(("operation_executable", operation_executable, self.min_count, self.min_instances))
        candidates.append(("semantic_class", semantic_class, self.min_count, self.min_instances))
        return candidates

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "SemanticCpuModel":
        materialized = list(rows)
        for row in materialized:
            if "observed_ms" not in row:
                raise ValueError("fit rows require observed_ms")
            _observed_label(row["observed_ms"])
        baseline_rows = [_baseline_row(row, _features_for_row(row)) for row in materialized]
        self.baseline.fit(baseline_rows)
        grouped: dict[str, dict[tuple[Any, ...], _Distribution]] = {
            level: defaultdict(lambda: _Distribution([], set())) for level in self.tables
        }
        global_distribution = _Distribution([], set())
        for row in materialized:
            observed = _observed_label(row["observed_ms"])
            features = _features_for_row(row)
            original = _original_class(row, features)
            if original not in ROUTED_OPERATION_CLASSES or not _has_action(row):
                continue
            repository = _repo_key(_row_repository(row))
            instance = self._instance_id(row)
            global_distribution.values.append(observed)
            global_distribution.instances.add(instance)
            for level, key, _event_threshold, _instance_threshold in self._key_candidates(features, repository):
                distribution = grouped[level][key]
                distribution.values.append(observed)
                distribution.instances.add(instance)
        self.tables = {level: {} for level in self.tables}
        for level, buckets in grouped.items():
            for key, distribution in buckets.items():
                self.tables[level][key] = distribution.summary(self.center)
        self.global_distribution = global_distribution.summary(self.center)
        self._fitted = True
        return self

    def _predict_baseline(self, row: Mapping[str, Any], features: Mapping[str, Any]) -> float:
        try:
            return _clip_ms(self.baseline.predict(_baseline_row(row, features)))
        except (KeyError, TypeError):
            return _clip_ms(self.global_distribution.get("prediction") or 1.0)

    def predict_details(self, row: Mapping[str, Any]) -> dict[str, Any]:
        features = _features_for_row(row)
        original = _original_class(row, features)
        if original not in ROUTED_OPERATION_CLASSES or not _has_action(row):
            prediction = self._predict_baseline(row, features)
            return {
                "prediction": prediction,
                "selected_level": "baseline",
                "selected_key": None,
                "support": {"n": 0, "distinct_instances": 0},
                "distribution": None,
                "semantic_features": dict(features),
                "fallback_reason": "compact_original_class" if original not in ROUTED_OPERATION_CLASSES else "missing_action",
            }
        repository = _repo_key(_row_repository(row))
        for level, key, event_threshold, instance_threshold in self._key_candidates(features, repository):
            table = self.tables.get(level, {})
            distribution = table.get(key)
            if not distribution:
                continue
            if int(distribution.get("n") or 0) < event_threshold:
                continue
            if int(distribution.get("distinct_instances") or 0) < instance_threshold:
                continue
            prediction = _clip_ms(distribution.get("prediction") or 1.0)
            return {
                "prediction": prediction,
                "selected_level": level,
                "selected_key": key,
                "support": {
                    "n": int(distribution.get("n") or 0),
                    "distinct_instances": int(distribution.get("distinct_instances") or 0),
                },
                "distribution": dict(distribution),
                "semantic_features": dict(features),
                "fallback_reason": None,
            }
        prediction = self._predict_baseline(row, features)
        return {
            "prediction": prediction,
            "selected_level": "baseline",
            "selected_key": None,
            "support": {"n": 0, "distinct_instances": 0},
            "distribution": None,
            "semantic_features": dict(features),
            "fallback_reason": "semantic_support",
        }

    def predict(self, row: Mapping[str, Any]) -> float:
        """Predict from inference fields only; a row label is ignored."""

        return float(self.predict_details(row)["prediction"])

    def to_mapping(self) -> dict[str, Any]:
        def encode_table(table: Mapping[tuple[Any, ...], Mapping[str, Any]]) -> list[dict[str, Any]]:
            return [
                {"key": _json_value(key), "distribution": dict(value)}
                for key, value in sorted(table.items(), key=lambda item: repr(item[0]))
            ]

        return {
            "kind": "semantic_cpu",
            "schema_version": SEMANTIC_SCHEMA,
            "extractor_id": SEMANTIC_EXTRACTOR_ID,
            "center": self.center,
            "use_repository": self.use_repository,
            "min_count": self.min_count,
            "min_instances": self.min_instances,
            "global_distribution": dict(self.global_distribution),
            "baseline": self.baseline.to_mapping(),
            "tables": {level: encode_table(table) for level, table in sorted(self.tables.items())},
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "SemanticCpuModel":
        model = cls(
            center=str(payload.get("center") or "median"),
            use_repository=bool(payload.get("use_repository", True)),
            min_count=int(payload.get("min_count") or 8),
            min_instances=int(payload.get("min_instances") or 3),
        )
        model.global_distribution = dict(payload.get("global_distribution") or model.global_distribution)
        baseline_payload = payload.get("baseline")
        if isinstance(baseline_payload, Mapping):
            model.baseline = HierarchicalMedianModel.from_mapping(baseline_payload)
        tables = payload.get("tables") or {}
        for level in model.tables:
            restored: dict[tuple[Any, ...], dict[str, Any]] = {}
            for item in tables.get(level, []) if isinstance(tables, Mapping) else []:
                if not isinstance(item, Mapping):
                    continue
                key = _from_json_value(item.get("key"))
                if isinstance(key, list):
                    key = tuple(key)
                if not isinstance(key, tuple):
                    key = (key,)
                restored[key] = dict(item.get("distribution") or {})
            model.tables[level] = restored
        model._fitted = True
        return model


__all__ = ["SEMANTIC_SCHEMA", "SEMANTIC_EXTRACTOR_ID", "semantic_features", "SemanticCpuModel"]
