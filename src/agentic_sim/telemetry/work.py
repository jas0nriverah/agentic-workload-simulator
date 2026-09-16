"""Measured command work adapters used by the v2 SWE-agent hook.

The pinned SWE-agent environment returns command text and stdout from
``SWEEnv.communicate`` while discarding the SWE-ReX observation's exit code.
Some runtime implementations expose an additional command telemetry object;
this module consumes only that explicit object.  It never derives byte,
file, or subprocess counts from an action string, output text, or intended
paths.  When the runtime has no child probe, every count remains ``None`` with
``unavailable`` provenance.

The adapter also accepts a bounded ``strace -ff -ttt -T`` summary supplied by
an explicitly configured runtime probe.  Starting strace is a deployment
policy decision and is intentionally outside this module; parsing a supplied
summary cannot change the command being measured.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping


WORK_FIELDS = ("bytes_read", "bytes_written", "files_touched", "subprocess_count")
_METRIC_CONTAINERS = (
    "work_volume",
    "command_metrics",
    "runtime_metrics",
    "runtime_child_telemetry",
    "telemetry",
    "metrics",
    "resource_usage",
)
_ALIASES = {
    "bytes_read": ("bytes_read", "read_bytes"),
    "bytes_written": ("bytes_written", "write_bytes"),
    "files_touched": ("files_touched", "file_count", "files_observed"),
    "subprocess_count": ("subprocess_count", "child_process_count", "children_count"),
}


@dataclass(frozen=True)
class WorkMeasurement:
    """One command's measured work volumes and evidence status."""

    bytes_read: int | None
    bytes_written: int | None
    files_touched: int | None
    subprocess_count: int | None
    availability: Mapping[str, str]
    source: str | None
    probe: str | None
    reason: str | None = None
    binding: Mapping[str, Any] | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "files_touched": self.files_touched,
            "subprocess_count": self.subprocess_count,
            "measurement_availability": dict(self.availability),
            "work_volume_source": self.source,
            "work_volume_probe": self.probe,
            "work_volume_reason": self.reason,
            "work_probe_binding": dict(self.binding) if self.binding is not None else None,
        }


@dataclass(frozen=True)
class CommandProbeBinding:
    """Identity a child-work probe must bind to before counts are accepted."""

    event_id: str
    command: str
    start_mono_ns: int
    end_mono_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id:
            raise ValueError("probe event_id must be non-empty text")
        if not isinstance(self.command, str) or "\x00" in self.command:
            raise ValueError("probe command must be text without NUL")
        if (
            isinstance(self.start_mono_ns, bool)
            or not isinstance(self.start_mono_ns, int)
            or isinstance(self.end_mono_ns, bool)
            or not isinstance(self.end_mono_ns, int)
            or self.start_mono_ns < 0
            or self.end_mono_ns < self.start_mono_ns
        ):
            raise ValueError("probe interval must be non-negative and ordered")

    @property
    def command_sha256(self) -> str:
        return hashlib.sha256(self.command.encode("utf-8")).hexdigest()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "command_sha256": self.command_sha256,
            "start_mono_ns": self.start_mono_ns,
            "end_mono_ns": self.end_mono_ns,
        }


def _explicit_metrics(result: Any, runtime: Any | None) -> tuple[Mapping[str, Any] | None, str | None, str | None]:
    """Find a runtime-provided metrics mapping without inspecting output."""

    candidates: list[tuple[Any, str]] = []
    if isinstance(result, Mapping):
        candidates.append((result, "runtime_result"))
    else:
        candidates.append((result, "runtime_result"))
    for container in _METRIC_CONTAINERS:
        value = result.get(container) if isinstance(result, Mapping) else getattr(result, container, None)
        if isinstance(value, Mapping):
            candidates.append((value, container))

    # A runtime child probe may expose one last-command mapping on the runtime
    # object.  Call only synchronous providers; an async provider would need
    # to be awaited by the deployment and cannot be sampled safely here.
    if runtime is not None:
        for name in ("get_last_command_metrics", "get_command_metrics", "last_command_metrics"):
            provider = getattr(runtime, name, None)
            if callable(provider):
                try:
                    value = provider()
                except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                    continue
                if isinstance(value, Mapping):
                    candidates.insert(0, (value, name))
                    break
            elif isinstance(provider, Mapping):
                candidates.insert(0, (provider, name))
                break

    for value, probe in candidates:
        if not isinstance(value, Mapping):
            # Object attributes are handled as a single explicit result
            # mapping below; arbitrary object internals are not traversed.
            continue
        if any(alias in value for aliases in _ALIASES.values() for alias in aliases):
            source = value.get("source", value.get("provenance"))
            source_text = source if isinstance(source, str) and source else "runtime_child_probe"
            return value, source_text, probe
    return None, None, None


def _object_metric(result: Any, aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        value = getattr(result, alias, None)
        if value is not None:
            return value
    return None


def _unavailable(reason: str) -> WorkMeasurement:
    return WorkMeasurement(
        bytes_read=None,
        bytes_written=None,
        files_touched=None,
        subprocess_count=None,
        availability={field: "unavailable" for field in WORK_FIELDS},
        source=None,
        probe=None,
        reason=reason,
        binding=None,
    )


def _binding_mapping(binding: CommandProbeBinding | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    if isinstance(binding, CommandProbeBinding):
        return binding.to_mapping()
    if not isinstance(binding, Mapping):
        return None
    return dict(binding)


def _bound(
    metrics: Mapping[str, Any],
    binding: CommandProbeBinding | Mapping[str, Any] | None,
) -> tuple[bool, dict[str, Any] | None, str]:
    """Require event/hash/interval/process binding and measured provenance."""

    expected = _binding_mapping(binding)
    if expected is None:
        return False, None, "runtime work probe has no command binding"
    event_id = expected.get("event_id")
    command_hash = expected.get("command_sha256")
    start = expected.get("start_mono_ns")
    end = expected.get("end_mono_ns")
    if not isinstance(event_id, str) or not event_id:
        return False, None, "runtime work probe binding lacks event_id"
    if not isinstance(command_hash, str) or not command_hash:
        return False, None, "runtime work probe binding lacks command_sha256"
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end < start
    ):
        return False, None, "runtime work probe binding lacks an ordered interval"
    if metrics.get("event_id") != event_id:
        return False, None, "runtime work probe event_id does not match the action span"
    observed_hash = metrics.get("command_sha256")
    if observed_hash is None and isinstance(metrics.get("command"), str):
        observed_hash = hashlib.sha256(metrics["command"].encode("utf-8")).hexdigest()
    if observed_hash != command_hash:
        return False, None, "runtime work probe command hash does not match the guarded action"
    observed_start = metrics.get("start_mono_ns")
    observed_end = metrics.get("end_mono_ns")
    if (
        isinstance(observed_start, bool)
        or not isinstance(observed_start, int)
        or isinstance(observed_end, bool)
        or not isinstance(observed_end, int)
        or observed_start < start
        or observed_end < observed_start
        or observed_end > end
    ):
        return False, None, "runtime work probe interval is outside the action interval"
    pid = metrics.get("pid")
    cgroup = metrics.get("cgroup")
    if not (
        isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
    ) and not (isinstance(cgroup, str) and bool(cgroup.strip())):
        return False, None, "runtime work probe lacks a measured pid or cgroup binding"
    if metrics.get("provenance") != "measured":
        return False, None, "runtime work probe provenance is not measured"
    return True, {
        "event_id": event_id,
        "command_sha256": command_hash,
        "start_mono_ns": observed_start,
        "end_mono_ns": observed_end,
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "cgroup": cgroup if isinstance(cgroup, str) else None,
    }, ""


def measure_runtime_work(
    result: Any,
    runtime: Any | None = None,
    *,
    binding: CommandProbeBinding | Mapping[str, Any] | None = None,
) -> WorkMeasurement:
    """Extract explicit command work only after strict probe binding.

    A metrics object without matching event ID, exact guarded-command hash,
    contained monotonic interval, measured provenance, and a PID/cgroup is
    treated as unavailable.  This prevents a stale ``last_command_metrics``
    snapshot from being attached to a later action.
    """

    metrics, source, probe = _explicit_metrics(result, runtime)
    if metrics is None:
        return _unavailable("runtime child/work probe is unavailable")
    valid, observed_binding, binding_reason = _bound(metrics, binding)
    if not valid:
        return _unavailable(binding_reason)
    values: dict[str, int | None] = {}
    availability: dict[str, str] = {}
    for field in WORK_FIELDS:
        aliases = _ALIASES[field]
        value = None
        if metrics is not None:
            for alias in aliases:
                candidate = metrics.get(alias)
                if candidate is not None:
                    value = candidate
                    break
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values[field] = value
            availability[field] = "measured"
        else:
            values[field] = None
            availability[field] = "unavailable"
    return WorkMeasurement(
        bytes_read=values["bytes_read"],
        bytes_written=values["bytes_written"],
        files_touched=values["files_touched"],
        subprocess_count=values["subprocess_count"],
        availability=availability,
        source=source,
        probe=probe,
        reason=None,
        binding=observed_binding,
    )


def parse_strace_summary(
    summary: Mapping[str, Any],
    *,
    binding: CommandProbeBinding | Mapping[str, Any] | None = None,
) -> WorkMeasurement:
    """Validate an explicit bounded strace summary supplied by a probe.

    The probe must have already attributed syscall totals to this one command
    and recorded the ``strace -ff -ttt -T`` mode.  Missing syscall classes stay
    unavailable; this parser never treats a trace line count as a byte count.
    """

    if not isinstance(summary, Mapping):
        raise ValueError("strace summary must be a mapping")
    mode = summary.get("mode")
    if mode != "strace -ff -ttt -T":
        raise ValueError("strace summary mode must identify -ff -ttt -T")
    valid, observed_binding, reason = _bound(summary, binding)
    if not valid:
        raise ValueError(reason)
    values: dict[str, int | None] = {}
    availability: dict[str, str] = {}
    for field in WORK_FIELDS:
        value = summary.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            values[field] = value
            availability[field] = "measured"
        else:
            values[field] = None
            availability[field] = "unavailable"
    return WorkMeasurement(
        bytes_read=values["bytes_read"],
        bytes_written=values["bytes_written"],
        files_touched=values["files_touched"],
        subprocess_count=values["subprocess_count"],
        availability=availability,
        source="strace_summary",
        probe="strace -ff -ttt -T",
        reason=None,
        binding=observed_binding,
    )


__all__ = [
    "WORK_FIELDS",
    "CommandProbeBinding",
    "WorkMeasurement",
    "measure_runtime_work",
    "parse_strace_summary",
]
