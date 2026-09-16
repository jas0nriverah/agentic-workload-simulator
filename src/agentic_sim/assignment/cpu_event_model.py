"""CPU/tool event latency models for assignment-level D9.

Uses only Step-3 logged descriptors: the chosen action string (tool, subcommand,
paths, pipes, globs, recursion flags, command bytes) plus hardware.  Duration
quantiles are diagnostics, not features.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from math import exp, isfinite, log, log1p
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence

from agentic_sim.assignment.event_simulator import (
    EventSimulatorError,
    ToolEventInput,
    _solve_ridge,
)
from agentic_sim.assignment.tool_features import (
    EXTRACTOR_ID,
    _primary_segment,
    _tokenize,
    _unquoted_split,
    extract_tool_features,
)

GATE = 25.0
FAMILIES = ("read", "write", "traversal", "search", "shell", "patch", "test", "other")
RECURSIVE_FLAGS = ("-r", "-R", "--recursive", "-recursive", "--recurse")
MAX_CPU_EVENT_MS = 45_000.0


def path_bucket(count: Any) -> int:
    n = int(count or 0)
    if n <= 0:
        return 0
    if n == 1:
        return 1
    if n <= 3:
        return 2
    return 3


def _clip_ms(value: float) -> float:
    if not isfinite(value):
        return 1.0
    return min(MAX_CPU_EVENT_MS, max(1e-6, value))


def _ape(pred: float, obs: float) -> float:
    return abs(pred - obs) / max(obs, 1e-9) * 100.0


def cpu_action_flags(action: str) -> dict[str, Any]:
    """Extra Step-3 descriptors derived only from the logged action text."""
    extracted = extract_tool_features(action)
    primary = extracted.primary_command
    tokens = _tokenize(primary)
    lowered = primary.lower()
    token_set = {token for token in tokens}
    recursive = int(
        any(flag in token_set for flag in RECURSIVE_FLAGS)
        or ("--recursive" in lowered)
        or (extracted.tool_name == "find" and "-maxdepth" not in lowered)
        or (extracted.tool_name in {"grep", "rg"} and any(t in {"-r", "-R"} for t in tokens))
    )
    n_segments = max(1, len(_unquoted_split(action)))
    n_pipes = action.count("|")
    is_python = int(extracted.tool_name in {"python", "python3"})
    is_find = int(extracted.tool_name == "find")
    is_editor = int(extracted.tool_name in {"str_replace_editor", "edit_file"})
    launch_family = extracted.tool_name
    if extracted.operation_class == "test":
        launch_family = "test"
    elif is_python:
        launch_family = "python"
    elif is_find:
        launch_family = "find"
    elif is_editor:
        launch_family = f"editor:{extracted.subcommand or 'other'}"
    return {
        "tool_name": extracted.tool_name,
        "subcommand": extracted.subcommand,
        "command_prefix": extracted.command_prefix,
        "operation_class": extracted.operation_class,
        "declared_command_bytes": extracted.declared_command_bytes,
        "declared_path_count": extracted.declared_path_count,
        "has_pipe": extracted.has_pipe,
        "has_glob": extracted.has_glob,
        "command_sha256": extracted.command_sha256,
        "extractor_id": EXTRACTOR_ID,
        "recursive": recursive,
        "n_segments": n_segments,
        "n_pipes": n_pipes,
        "is_python": is_python,
        "is_find": is_find,
        "is_editor": is_editor,
        "launch_family": launch_family,
        "byte_bucket": extracted.declared_command_bytes // 64,
        "log_bytes": log1p(extracted.declared_command_bytes),
    }


def enrich_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if row.get("launch_family") and row.get("operation_class") and "log_bytes" in row:
        return dict(row)
    action = row.get("action")
    if isinstance(action, str) and action.strip():
        flags = cpu_action_flags(action)
    else:
        flags = {
            "tool_name": row.get("tool_name") or "unknown",
            "subcommand": row.get("subcommand") or "",
            "command_prefix": row.get("command_prefix") or "",
            "operation_class": row.get("operation_class") or "other",
            "declared_command_bytes": int(row.get("declared_command_bytes") or 0),
            "declared_path_count": int(row.get("declared_path_count") or 0),
            "has_pipe": int(row.get("has_pipe") or 0),
            "has_glob": int(row.get("has_glob") or 0),
            "command_sha256": row.get("command_sha256") or "",
            "extractor_id": EXTRACTOR_ID,
            "recursive": int(row.get("recursive") or 0),
            "n_segments": int(row.get("n_segments") or 1),
            "n_pipes": int(row.get("n_pipes") or 0),
            "is_python": int(row.get("is_python") or 0),
            "is_find": int(row.get("is_find") or 0),
            "is_editor": int(row.get("is_editor") or 0),
            "launch_family": row.get("launch_family") or row.get("tool_name") or "unknown",
            "byte_bucket": int(row.get("declared_command_bytes") or 0) // 64,
            "log_bytes": log1p(int(row.get("declared_command_bytes") or 0)),
        }
    out = dict(row)
    out.update(flags)
    return out


def _median(values: Sequence[float], default: float) -> float:
    finite = [float(item) for item in values if isfinite(float(item))]
    if not finite:
        return default
    return float(median(finite))


def _percentile(values: Sequence[float], q: float) -> float | None:
    ordered = sorted(float(item) for item in values if isfinite(float(item)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def summarize(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    if not pairs:
        return {
            "n": 0,
            "within_25_rate": None,
            "mean_ape": None,
            "median_ape": None,
            "max_ape": None,
            "sum_pred_minus_obs": 0.0,
        }
    apes = [_ape(pred, obs) for pred, obs in pairs]
    return {
        "n": len(pairs),
        "within_25_rate": sum(item <= GATE for item in apes) / len(apes),
        "mean_ape": sum(apes) / len(apes),
        "median_ape": float(median(apes)),
        "max_ape": max(apes),
        "sum_pred_minus_obs": sum(pred - obs for pred, obs in pairs),
    }


class ClassMedianModel:
    def __init__(self) -> None:
        self.class_medians: dict[str, float] = {}
        self.global_median = 1.0

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "ClassMedianModel":
        buckets: dict[str, list[float]] = defaultdict(list)
        walls: list[float] = []
        for row in rows:
            wall = float(row["observed_ms"])
            buckets[str(row["operation_class"])].append(wall)
            walls.append(wall)
        self.global_median = _median(walls, 1.0)
        self.class_medians = {key: _median(values, self.global_median) for key, values in buckets.items()}
        return self

    def predict(self, row: Mapping[str, Any]) -> float:
        return _clip_ms(self.class_medians.get(str(row["operation_class"]), self.global_median))


class HierarchicalMedianModel:
    """Per-family median backoff from Step-3 descriptors.

    Exact command-sha lookup is intentionally omitted: identical command text
    can still have wildly different runtimes, and n=2 sha medians produced
    20000%+ APE tails.  Byte-count singleton buckets are also omitted.
    """

    def __init__(self, min_count: int = 8) -> None:
        self.min_count = min_count
        self.prefix: dict[tuple[Any, ...], float] = {}
        self.flags: dict[tuple[Any, ...], float] = {}
        self.sub: dict[tuple[Any, ...], float] = {}
        self.tool: dict[tuple[Any, ...], float] = {}
        self.classes: dict[str, float] = {}
        self.global_median = 1.0

    def _prefix_key(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            row["operation_class"],
            row.get("launch_family") or row.get("tool_name"),
            row.get("subcommand") or "",
            int(row.get("recursive") or 0),
            int(row.get("has_pipe") or 0),
            path_bucket(row.get("declared_path_count")),
        )

    def _flags_key(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            row["operation_class"],
            row.get("launch_family") or row.get("tool_name"),
            int(row.get("recursive") or 0),
            int(row.get("has_pipe") or 0),
            path_bucket(row.get("declared_path_count")),
        )

    def _sub_key(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (row["operation_class"], row.get("tool_name"), row.get("subcommand") or "")

    def _tool_key(self, row: Mapping[str, Any]) -> tuple[Any, ...]:
        return (row["operation_class"], row.get("tool_name"))

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "HierarchicalMedianModel":
        prefix_b: dict[tuple[Any, ...], list[float]] = defaultdict(list)
        flags_b: dict[tuple[Any, ...], list[float]] = defaultdict(list)
        sub_b: dict[tuple[Any, ...], list[float]] = defaultdict(list)
        tool_b: dict[tuple[Any, ...], list[float]] = defaultdict(list)
        class_b: dict[str, list[float]] = defaultdict(list)
        walls: list[float] = []
        for row in rows:
            wall = float(row["observed_ms"])
            walls.append(wall)
            prefix_b[self._prefix_key(row)].append(wall)
            flags_b[self._flags_key(row)].append(wall)
            sub_b[self._sub_key(row)].append(wall)
            tool_b[self._tool_key(row)].append(wall)
            class_b[str(row["operation_class"])].append(wall)
        self.global_median = _median(walls, 1.0)
        self.prefix = {
            key: _median(values, self.global_median)
            for key, values in prefix_b.items()
            if len(values) >= self.min_count
        }
        self.flags = {
            key: _median(values, self.global_median)
            for key, values in flags_b.items()
            if len(values) >= self.min_count
        }
        self.sub = {
            key: _median(values, self.global_median)
            for key, values in sub_b.items()
            if len(values) >= max(6, self.min_count - 2)
        }
        self.tool = {
            key: _median(values, self.global_median)
            for key, values in tool_b.items()
            if len(values) >= 4
        }
        self.classes = {key: _median(values, self.global_median) for key, values in class_b.items()}
        return self

    def predict(self, row: Mapping[str, Any]) -> float:
        prefix = self._prefix_key(row)
        if prefix in self.prefix:
            return _clip_ms(self.prefix[prefix])
        sub = self._sub_key(row)
        if sub in self.sub:
            return _clip_ms(self.sub[sub])
        flags = self._flags_key(row)
        if flags in self.flags:
            return _clip_ms(self.flags[flags])
        tool = self._tool_key(row)
        if tool in self.tool:
            return _clip_ms(self.tool[tool])
        return _clip_ms(self.classes.get(str(row["operation_class"]), self.global_median))

    def to_mapping(self) -> dict[str, Any]:
        def encode(table: Mapping[tuple[Any, ...], float]) -> list[dict[str, Any]]:
            return [
                {"key": list(key), "ms": value}
                for key, value in sorted(table.items(), key=lambda item: [str(part) for part in item[0]])
            ]

        return {
            "kind": "hierarchical_median",
            "min_count": self.min_count,
            "global_median": self.global_median,
            "classes": dict(sorted(self.classes.items())),
            "tool": encode(self.tool),
            "sub": encode(self.sub),
            "flags": encode(self.flags),
            "prefix": encode(self.prefix),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "HierarchicalMedianModel":
        model = cls(min_count=int(payload.get("min_count") or 8))
        model.global_median = float(payload.get("global_median") or 1.0)
        model.classes = {str(key): float(value) for key, value in dict(payload.get("classes") or {}).items()}

        def decode(items: Any) -> dict[tuple[Any, ...], float]:
            table: dict[tuple[Any, ...], float] = {}
            for item in items or []:
                table[tuple(item["key"])] = float(item["ms"])
            return table

        model.tool = decode(payload.get("tool"))
        model.sub = decode(payload.get("sub"))
        model.flags = decode(payload.get("flags") or payload.get("fine"))
        model.prefix = decode(payload.get("prefix"))
        return model


class FamilyLogLinearModel:
    """Separate log-linear model per operation family."""

    def __init__(self) -> None:
        self.coef: dict[str, tuple[float, ...]] = {}
        self.fallback = ClassMedianModel()

    def _design(self, row: Mapping[str, Any]) -> tuple[float, ...]:
        return (
            1.0,
            float(row.get("log_bytes") or log1p(int(row.get("declared_command_bytes") or 0))),
            float(int(row.get("declared_path_count") or 0)),
            float(int(row.get("recursive") or 0)),
            float(int(row.get("has_pipe") or 0)),
            float(int(row.get("has_glob") or 0)),
            float(int(row.get("n_segments") or 1)),
            float(int(row.get("is_python") or 0)),
            float(int(row.get("is_find") or 0)),
        )

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "FamilyLogLinearModel":
        self.fallback.fit(rows)
        grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["operation_class"])].append(row)
        for family, items in grouped.items():
            if len(items) < 12:
                continue
            design = [self._design(row) for row in items]
            targets = [log(max(float(row["observed_ms"]), 1e-3)) for row in items]
            try:
                self.coef[family] = _solve_ridge(design, targets, 1e-2)
            except EventSimulatorError:
                continue
        return self

    def predict(self, row: Mapping[str, Any]) -> float:
        family = str(row["operation_class"])
        coef = self.coef.get(family)
        if coef is None:
            return self.fallback.predict(row)
        design = self._design(row)
        log_ms = sum(a * b for a, b in zip(coef, design))
        return _clip_ms(exp(min(max(log_ms, 0.0), 10.5)))


class LaunchPlusWorkModel:
    """Fixed process-launch overhead plus family-specific work term."""

    def __init__(self) -> None:
        self.launch: dict[str, float] = {}
        self.work: dict[str, tuple[float, float]] = {}
        self.global_launch = 1.0

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "LaunchPlusWorkModel":
        launch_b: dict[str, list[float]] = defaultdict(list)
        work_b: dict[str, list[tuple[float, float]]] = defaultdict(list)
        short: list[float] = []
        for row in rows:
            wall = float(row["observed_ms"])
            bytes_ = max(int(row.get("declared_command_bytes") or 0), 1)
            family = str(row.get("launch_family") or row.get("tool_name") or "unknown")
            op = str(row["operation_class"])
            if bytes_ <= 80 and int(row.get("recursive") or 0) == 0 and int(row.get("has_pipe") or 0) == 0:
                launch_b[family].append(wall)
                short.append(wall)
            work_b[op].append((log1p(bytes_), wall))
        self.global_launch = _median(short, 120.0)
        self.launch = {key: _median(values, self.global_launch) for key, values in launch_b.items() if len(values) >= 4}
        for op, items in work_b.items():
            if len(items) < 12:
                continue
            design = [(1.0, x) for x, _y in items]
            targets = [log(max(y, 1e-3)) for _x, y in items]
            try:
                intercept, slope = _solve_ridge(design, targets, 1e-2)
            except EventSimulatorError:
                continue
            self.work[op] = (intercept, slope)
        return self

    def predict(self, row: Mapping[str, Any]) -> float:
        family = str(row.get("launch_family") or row.get("tool_name") or "unknown")
        op = str(row["operation_class"])
        launch = self.launch.get(family, self.global_launch)
        coef = self.work.get(op)
        bytes_ = max(int(row.get("declared_command_bytes") or 0), 1)
        if coef is None:
            return _clip_ms(launch)
        intercept, slope = coef
        work = exp(min(max(intercept + slope * log1p(bytes_), 0.0), 10.5))
        recursive = 1.35 if int(row.get("recursive") or 0) else 1.0
        pipe = 1.15 if int(row.get("has_pipe") or 0) else 1.0
        return _clip_ms(max(launch, work * recursive * pipe))


class MixtureCpuModel:
    """Per-family predictor: hierarchical median for fast ops, log-linear for heavy tails."""

    FAST = {"read", "write", "patch", "search"}
    HEAVY = {"test", "shell", "traversal", "other"}

    def __init__(self) -> None:
        self.fast = HierarchicalMedianModel(min_count=6)
        self.heavy = FamilyLogLinearModel()
        self.launch = LaunchPlusWorkModel()

    def fit(self, rows: Sequence[Mapping[str, Any]]) -> "MixtureCpuModel":
        fast_rows = [row for row in rows if str(row["operation_class"]) in self.FAST]
        heavy_rows = [row for row in rows if str(row["operation_class"]) in self.HEAVY]
        if fast_rows:
            self.fast.fit(fast_rows)
        if heavy_rows:
            self.heavy.fit(heavy_rows)
        self.launch.fit(rows)
        return self

    def predict(self, row: Mapping[str, Any]) -> float:
        op = str(row["operation_class"])
        if op in self.FAST:
            return self.fast.predict(row)
        loglin = self.heavy.predict(row)
        launch = self.launch.predict(row)
        if op == "test":
            return max(loglin, launch)
        if int(row.get("recursive") or 0) or int(row.get("is_find") or 0) or int(row.get("has_pipe") or 0):
            return max(loglin, launch)
        if launch > 0 and abs(loglin - launch) / max(launch, 1.0) > 4:
            return min(loglin, launch)
        return 0.5 * (loglin + launch)


def row_from_tool_input(features: ToolEventInput) -> dict[str, Any]:
    """Map a ToolEventInput onto the CPU-model row schema."""
    tool = features.tool_name or "unknown"
    sub = features.subcommand or ""
    prefix = features.command_prefix or ""
    lowered = prefix.lower()
    if features.operation_class == "test":
        launch = "test"
    elif tool in {"python", "python3"}:
        launch = "python"
    elif tool == "find":
        launch = "find"
    elif tool in {"str_replace_editor", "edit_file"}:
        launch = f"editor:{sub or 'other'}"
    else:
        launch = tool
    tokens = lowered.split()
    recursive = int(
        any(flag in tokens for flag in RECURSIVE_FLAGS)
        or (tool == "find" and "maxdepth" not in lowered)
        or (tool in {"grep", "rg"} and any(flag in tokens for flag in ("-r", "-R")))
    )
    return {
        "run_id": features.run_id,
        "event_id": features.event_id,
        "operation_class": features.operation_class,
        "tool_name": tool,
        "subcommand": sub,
        "command_prefix": prefix,
        "declared_command_bytes": features.declared_command_bytes,
        "declared_path_count": features.declared_path_count,
        "has_pipe": features.has_pipe,
        "has_glob": features.has_glob,
        "command_sha256": features.command_sha256,
        "recursive": recursive,
        "n_segments": 1,
        "n_pipes": features.has_pipe,
        "is_python": int(tool in {"python", "python3"}),
        "is_find": int(tool == "find"),
        "is_editor": int(tool in {"str_replace_editor", "edit_file"}),
        "launch_family": launch,
        "byte_bucket": features.declared_command_bytes // 64,
        "log_bytes": log1p(features.declared_command_bytes),
    }


def class_mean_predict(train: Sequence[Mapping[str, Any]], row: Mapping[str, Any]) -> float:
    values = [float(item["observed_ms"]) for item in train if item["operation_class"] == row["operation_class"]]
    if not values:
        values = [float(item["observed_ms"]) for item in train]
    return sum(values) / len(values)


MODEL_FACTORIES: dict[str, Callable[[], Any]] = {
    "class_median": ClassMedianModel,
    "hierarchical_median": HierarchicalMedianModel,
    "family_loglinear": FamilyLogLinearModel,
    "launch_plus_work": LaunchPlusWorkModel,
    "mixture_per_family": MixtureCpuModel,
}
