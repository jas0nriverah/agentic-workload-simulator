"""Run a bounded live SWE-agent persistent-shell and native BPF check.

This check uses the pinned SWE-agent checkout and SWE-ReX runtime to exercise
one real Docker-backed persistent bash session.  It deliberately does not run
model inference, a SWE-bench production case, or a GPU workload.  The result
is evidence for the pager, script-state, cwd, failure, timeout, and immediate
recovery paths only.

The command must receive a new output directory.  All runtime journals and
the BPF binary stream are retained there so a reviewer can validate the
claims from the existing hook and collector artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SWE_AGENT_ROOT = Path(
    "/home/riverahernandezjason/h100-assignment-work-20260905/repos/SWE-agent"
)
DEFAULT_BASE_IMAGE = (
    "swebench/sweb.eval.x86_64.psf_1776_requests-1724@"
    "sha256:e369005d38858ea90f843d853cb8427a2681b7513b846d12a34cb8d52c00763e"
)
DEFAULT_IMAGE = "assignment-persistent-shell-swe-rex-1-4-0:20260909"
EXPECTED_SWE_AGENT_COMMIT = "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9"
EXPECTED_SWE_AGENT_VERSION = "1.1.0"
EXPECTED_SWE_REX_VERSION = "1.4.0"
LOSS_FIELDS = (
    "lost_event_records",
    "lost_pending_records",
    "lineage_map_failures",
    "lost_path_records",
)


class ValidationError(RuntimeError):
    """A required live observation was absent or inconsistent."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValidationError(f"expected JSON object at {path}:{line_number}")
        rows.append(value)
    return rows


def run_process(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def git_value(root: Path, *args: str) -> str:
    result = run_process(["git", "-C", str(root), *args])
    if result.returncode != 0:
        raise ValidationError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def image_inspect(image: str) -> dict[str, Any]:
    result = run_process(["docker", "image", "inspect", image])
    if result.returncode != 0:
        raise ValidationError(f"Docker image is unavailable: {image}: {result.stderr.strip()}")
    value = json.loads(result.stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValidationError(f"Docker image inspect was ambiguous: {image}")
    return value[0]


def build_pinned_image(output: Path, image: str, base_image: str, *, skip_build: bool) -> dict[str, Any]:
    dockerfile = "\n".join(
        (
            f"FROM {base_image}",
            "RUN /opt/miniconda3/bin/python3 -m pip install --no-cache-dir --disable-pip-version-check swe-rex==1.4.0",
            'RUN /opt/miniconda3/bin/python3 -c "import swerex; assert swerex.__version__ == \'1.4.0\'; print(swerex.__version__)"',
            "",
        )
    )
    (output / "pinned.Dockerfile").write_text(dockerfile, encoding="utf-8")
    build_result = None
    if not skip_build:
        build_result = run_process(["docker", "build", "--pull=false", "-t", image, "-"], input_text=dockerfile)
        (output / "docker_build.stdout.log").write_text(build_result.stdout, encoding="utf-8")
        (output / "docker_build.stderr.log").write_text(build_result.stderr, encoding="utf-8")
        if build_result.returncode != 0:
            raise ValidationError(f"pinned Docker image build failed: {build_result.stderr[-2000:]}")
    inspected = image_inspect(image)
    write_json(output / "docker_image_inspect.json", inspected)
    image_id = str(inspected.get("Id", ""))
    if not image_id.startswith("sha256:"):
        raise ValidationError(f"Docker image has no immutable image ID: {image}")
    verify = run_process(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "/opt/miniconda3/bin/python3",
            "-c",
            "import swerex; print(swerex.__version__)",
        ]
    )
    (output / "docker_image_version.stdout.log").write_text(verify.stdout, encoding="utf-8")
    (output / "docker_image_version.stderr.log").write_text(verify.stderr, encoding="utf-8")
    if verify.returncode != 0 or verify.stdout.strip() != EXPECTED_SWE_REX_VERSION:
        raise ValidationError(
            "derived Docker image did not report SWE-ReX 1.4.0: "
            f"rc={verify.returncode} stdout={verify.stdout!r} stderr={verify.stderr[-500:]!r}"
        )
    return {
        "image": image,
        "image_id": image_id,
        "repo_digests": inspected.get("RepoDigests", []),
        "base_image": base_image,
        "dockerfile_sha256": sha256_bytes(dockerfile.encode("utf-8")),
        "built_by_check": not skip_build,
        "container_version_stdout": verify.stdout.strip(),
        "build_returncode": build_result.returncode if build_result is not None else None,
    }


def container_inspect(container_name: str | None) -> dict[str, Any] | None:
    if not isinstance(container_name, str) or not container_name:
        return None
    result = run_process(["docker", "inspect", container_name])
    if result.returncode != 0:
        return {
            "container_name": container_name,
            "inspect_returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }
    value = json.loads(result.stdout)
    return {
        "container_name": container_name,
        "inspect_returncode": result.returncode,
        "inspect": value,
    }


def printf_write_command(path: str, content: str) -> str:
    escaped = content.replace("\\", "\\\\").replace("\n", "\\n")
    return f"printf %b {shlex.quote(escaped)} > {shlex.quote(path)}"


def expected_script_state(state: Mapping[str, Any], path: str, content: str) -> None:
    if state.get("status") != "known":
        raise ValidationError(f"script state is not known for {path}: {state}")
    paths = state.get("paths")
    if not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], Mapping):
        raise ValidationError(f"script state did not retain one descriptor for {path}")
    descriptor = paths[0]
    encoded = content.encode("utf-8")
    expected_digest = sha256_bytes(encoded)
    if descriptor.get("path") != path:
        raise ValidationError(f"script state path mismatch: {descriptor.get('path')!r} != {path!r}")
    if descriptor.get("sha256") != expected_digest:
        raise ValidationError(f"script prestate hash mismatch for {path}")
    if descriptor.get("size_bytes") != len(encoded):
        raise ValidationError(f"script prestate size mismatch for {path}")
    artifact = descriptor.get("content_artifact")
    if not isinstance(artifact, Mapping):
        raise ValidationError(f"script prestate has no retained content artifact for {path}")
    if artifact.get("sha256") != expected_digest or artifact.get("size_bytes") != len(encoded):
        raise ValidationError(f"script artifact metadata mismatch for {path}")
    if artifact.get("truncated") is not False:
        raise ValidationError(f"script artifact is marked truncated for {path}")
    if artifact.get("hash_basis") != "decoded_text_utf8_reencoding" or artifact.get("byte_exact") is not False:
        raise ValidationError(f"script artifact provenance mismatch for {path}")


def artifact_path_from_descriptor(telemetry_dir: Path, descriptor: Mapping[str, Any]) -> Path:
    artifact = descriptor.get("content_artifact")
    if not isinstance(artifact, Mapping) or not isinstance(artifact.get("artifact_path"), str):
        raise ValidationError("script descriptor does not point to a content artifact")
    relative = Path(str(artifact["artifact_path"]))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValidationError(f"unsafe script artifact path: {relative}")
    path = telemetry_dir / relative
    if not path.is_file() or path.is_symlink():
        raise ValidationError(f"script content artifact is missing: {path}")
    return path


def validate_script_reads(
    telemetry_dir: Path,
    lifecycle_rows: list[dict[str, Any]],
    expected: list[tuple[str, str, str]],
) -> dict[str, Any]:
    reads = [
        row
        for row in lifecycle_rows
        if row.get("event_kind") == "script_read" and row.get("terminal") is True
    ]
    if len(reads) != len(expected):
        raise ValidationError(f"expected {len(expected)} native script_read terminals, observed {len(reads)}")
    evidence: list[dict[str, Any]] = []
    previous_generation = 0
    for row, (label, path, content) in zip(reads, expected):
        state = row.get("script_state")
        if not isinstance(state, Mapping):
            raise ValidationError(f"script_read has no script_state: {label}")
        expected_script_state(state, path, content)
        generation = state.get("generation")
        if not isinstance(generation, int) or generation <= previous_generation:
            raise ValidationError(f"script state generation did not advance at {label}")
        previous_generation = generation
        if row.get("status") != "success" or row.get("availability") != "measured":
            raise ValidationError(f"native script_read is not measured success at {label}")
        if row.get("script_read_count") != 1:
            raise ValidationError(f"native script_read count mismatch at {label}")
        container_cwd = row.get("script_container_cwd")
        if not isinstance(container_cwd, str) or not container_cwd.startswith("/"):
            raise ValidationError(f"native script_read has no absolute cwd witness at {label}")
        witness = row.get("script_cwd_witness")
        if not isinstance(witness, Mapping) or witness.get("status") != "measured":
            raise ValidationError(f"native script_read cwd witness is not measured at {label}")
        witness_source = witness.get("source")
        if witness_source == "bpf_service_procfs":
            snapshot = witness.get("service_snapshot")
            if (
                not isinstance(snapshot, Mapping)
                or snapshot.get("status") != "measured"
                or snapshot.get("container_cwd") != container_cwd
                or not isinstance(snapshot.get("namespace_proof"), Mapping)
                or not isinstance(snapshot.get("identity"), Mapping)
            ):
                raise ValidationError(f"procfs cwd witness lacks a bound namespace proof at {label}")
        elif witness_source != "swerex_pwd":
            raise ValidationError(f"unknown cwd witness source {witness_source!r} at {label}")
        descriptor = state["paths"][0]
        artifact_path = artifact_path_from_descriptor(telemetry_dir, descriptor)
        artifact_bytes = artifact_path.read_bytes()
        if artifact_bytes != content.encode("utf-8"):
            raise ValidationError(f"retained current prestate bytes do not match {label}")
        evidence.append(
            {
                "label": label,
                "event_id": row.get("event_id"),
                "source_event_id": state.get("source_event_id"),
                "generation": generation,
                "path": path,
                "container_cwd": container_cwd,
                "cwd_witness_source": witness_source,
                "cwd_witness_shell_action": witness_source == "swerex_pwd",
                "sha256": sha256_bytes(artifact_bytes),
                "artifact_path": str(artifact_path.relative_to(telemetry_dir)),
                "artifact_bytes": len(artifact_bytes),
            }
        )
    return {"count": len(evidence), "reads": evidence}


def validate_tool_rows(
    tool_rows: list[dict[str, Any]],
    action_records: list[dict[str, Any]],
) -> dict[str, Any]:
    terminals = [
        row
        for row in tool_rows
        if row.get("event_kind") == "tool_event" and row.get("terminal") is True
    ]
    if len(terminals) != len(action_records):
        raise ValidationError(f"expected {len(action_records)} terminal tool rows, observed {len(terminals)}")
    intents = [
        row for row in tool_rows if row.get("event_kind") == "tool_intent" and row.get("terminal") is True
    ]
    intents_by_action_id: dict[str, list[dict[str, Any]]] = {}
    for row in intents:
        action_id = row.get("action_id")
        if isinstance(action_id, str):
            intents_by_action_id.setdefault(action_id, []).append(row)
    by_label: dict[str, dict[str, Any]] = {}
    for record in action_records:
        label = str(record["label"])
        intent_rows = intents_by_action_id.get(str(record.get("action_id")), [])
        if len(intent_rows) != 1:
            raise ValidationError(f"could not identify exactly one durable intent row for {label}")
        intent = intent_rows[0]
        if intent.get("action") != record["command"] or intent.get("action_sha256") != record["command_sha256"]:
            raise ValidationError(f"declared intent identity mismatch for {label}")
        candidates = [
            row
            for row in terminals
            if row.get("action") == record["command"] and row.get("action_id") == record.get("action_id")
        ]
        if len(candidates) != 1:
            raise ValidationError(f"could not identify exactly one terminal row for {label}")
        row = candidates[0]
        command = str(record["command"])
        digest = sha256_bytes(command.encode("utf-8"))
        if row.get("action_sha256") != digest:
            raise ValidationError(f"declared action hash mismatch for {label}")
        if row.get("actual_action") != command or row.get("actual_action_sha256") != digest:
            raise ValidationError(f"guarded actual action identity mismatch for {label}")
        if row.get("runtime_command") != command:
            raise ValidationError(f"runtime command identity mismatch for {label}")
        if row.get("action_id") != record.get("action_id"):
            raise ValidationError(f"physical action identity mismatch for {label}")
        if row.get("work_collector_event_id") != record.get("pre_event_id"):
            raise ValidationError(f"collector event identity is not the measured tool pre-event for {label}")
        observed_status = row.get("status")
        if observed_status != record["expected_status"]:
            raise ValidationError(
                f"unexpected status for {label}: observed={observed_status!r} expected={record['expected_status']!r}"
            )
        by_label[label] = {
            "label": label,
            "event_id": row.get("event_id"),
            "pre_event_id": record.get("pre_event_id"),
            "action_id": row.get("action_id"),
            "action_sha256": digest,
            "status": observed_status,
            "command_exit_code": row.get("command_exit_code"),
            "command_exit_code_availability": row.get("command_exit_code_availability"),
            "command_timeout": row.get("command_timeout"),
            "script_state": row.get("features", {}).get("script_state") if isinstance(row.get("features"), Mapping) else None,
            "work_collector_event_id": row.get("work_collector_event_id"),
            "work_collector_status": row.get("work_collector_status"),
        }
    return {"count": len(by_label), "by_label": by_label}


def validate_interrupt_control(
    lifecycle_rows: list[dict[str, Any]],
    timeout_record: Mapping[str, Any],
) -> dict[str, Any]:
    controls = [
        row
        for row in lifecycle_rows
        if row.get("event_kind") == "bash_interrupt_control" and row.get("terminal") is True
    ]
    if len(controls) != 1:
        raise ValidationError(f"expected one measured BashInterruptAction control span, observed {len(controls)}")
    row = controls[0]
    if row.get("status") != "success":
        raise ValidationError(f"BashInterruptAction control span did not succeed: {row}")
    if row.get("control_action") != "bash_interrupt":
        raise ValidationError("BashInterruptAction control identity is missing")
    if row.get("runtime_action_class") != "BashInterruptAction" or row.get("runtime_action_type") != "bash_interrupt":
        raise ValidationError("BashInterruptAction runtime type identity is incomplete")
    if row.get("command") is not None or row.get("command_sha256") is not None:
        raise ValidationError("BashInterruptAction lifecycle control invented a command identity")
    if row.get("command_identity_available") is not False:
        raise ValidationError("BashInterruptAction command identity availability is not explicit")
    if row.get("parent_event_id") != timeout_record.get("pre_event_id"):
        raise ValidationError("BashInterruptAction control was not parented to the timed-out tool action")
    return {
        "event_id": row.get("event_id"),
        "span_id": row.get("span_id"),
        "status": row.get("status"),
        "parent_event_id": row.get("parent_event_id"),
        "runtime_action_class": row.get("runtime_action_class"),
        "runtime_action_type": row.get("runtime_action_type"),
        "command_identity_available": row.get("command_identity_available"),
    }


def _cpu_usage_usec(resource: Mapping[str, Any]) -> int:
    files = resource.get("files")
    cpu_stat = files.get("cpu.stat") if isinstance(files, Mapping) else None
    raw = cpu_stat.get("raw") if isinstance(cpu_stat, Mapping) else None
    if not isinstance(raw, str):
        raise ValidationError("container resource snapshot has no raw cpu.stat")
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0] == "usage_usec":
            try:
                value = int(fields[1])
            except ValueError as exc:
                raise ValidationError("container cpu.stat usage_usec is not an integer") from exc
            if value < 0:
                raise ValidationError("container cpu.stat usage_usec is negative")
            return value
    raise ValidationError("container cpu.stat has no usage_usec")


def validate_container_resource_samples(
    linux_dir: Path,
    tool_rows: list[dict[str, Any]],
    action_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate measured cgroup context around a short busy/sleep fixture."""

    summary = read_json(linux_dir / "work_summary.json")
    manifest = read_json(linux_dir / "bpf_collector_manifest.json")
    summary_by_event: dict[str, Mapping[str, Any]] = {}
    for item in summary.get("actions", []):
        raw = item.get("raw") if isinstance(item, Mapping) else None
        boundary = raw.get("boundary") if isinstance(raw, Mapping) else None
        event_id = boundary.get("event_id") if isinstance(boundary, Mapping) else None
        if isinstance(event_id, str) and isinstance(raw, Mapping):
            summary_by_event[event_id] = raw
    tool_event_by_label: dict[str, str] = {}
    for record in action_records:
        label = str(record["label"])
        rows = [
            row
            for row in tool_rows
            if row.get("event_kind") == "tool_event"
            and row.get("terminal") is True
            and row.get("work_collector_event_id") == record.get("pre_event_id")
        ]
        if len(rows) != 1:
            raise ValidationError(f"resource fixture has no unique tool boundary for {label}")
        tool_event_by_label[label] = str(rows[0]["work_collector_event_id"])

    identity = manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise ValidationError("resource fixture BPF manifest has no identity")
    evidence: dict[str, Any] = {}
    for record in action_records:
        label = str(record["label"])
        event_id = tool_event_by_label[label]
        raw = summary_by_event.get(event_id)
        if not isinstance(raw, Mapping):
            raise ValidationError(f"resource fixture BPF summary is missing {label}")
        boundary = raw.get("boundary")
        if not isinstance(boundary, Mapping):
            raise ValidationError(f"resource fixture boundary is missing for {label}")
        start_snapshot = boundary.get("start_snapshot")
        end_snapshot = boundary.get("end_snapshot")
        start_resource = start_snapshot.get("container_resources") if isinstance(start_snapshot, Mapping) else None
        end_resource = end_snapshot.get("container_resources") if isinstance(end_snapshot, Mapping) else None
        if not isinstance(start_resource, Mapping) or not isinstance(end_resource, Mapping):
            raise ValidationError(f"container resource context is missing for {label}")
        if start_resource.get("status") != "measured" or end_resource.get("status") != "measured":
            raise ValidationError(f"container resource context is not measured for {label}")
        for resource in (start_resource, end_resource):
            if resource.get("target_pid") != identity.get("pid"):
                raise ValidationError(f"container resource target PID mismatch for {label}")
            if resource.get("target_start_ticks") != identity.get("start_ticks"):
                raise ValidationError(f"container resource start-ticks mismatch for {label}")
            if resource.get("boot_id") != identity.get("boot_id"):
                raise ValidationError(f"container resource boot identity mismatch for {label}")
            if not isinstance(resource.get("cgroup_path"), str) or not resource["cgroup_path"].startswith("/sys/fs/cgroup/"):
                raise ValidationError(f"container resource cgroup path is not mounted and bound for {label}")
            files = resource.get("files")
            host_context = resource.get("host_context")
            if not isinstance(files, Mapping) or not isinstance(host_context, Mapping):
                raise ValidationError(f"container resource context is incomplete for {label}")
            for name in (
                "cpu.stat",
                "io.stat",
                "memory.current",
                "memory.max",
                "cpu.max",
                "cpuset.cpus.effective",
                "cpu.pressure",
                "io.pressure",
                "memory.pressure",
            ):
                if name not in files:
                    raise ValidationError(f"container resource file {name} is absent for {label}")
            for name in ("pressure/cpu", "pressure/io", "pressure/memory"):
                if name not in host_context:
                    raise ValidationError(f"host pressure context {name} is absent for {label}")
        cpu_start = _cpu_usage_usec(start_resource)
        cpu_end = _cpu_usage_usec(end_resource)
        cpu_delta_usec = cpu_end - cpu_start
        if cpu_delta_usec < 0:
            raise ValidationError(f"container cpu.stat usage decreased for {label}")
        start_ns = boundary.get("start_mono_ns")
        end_ns = boundary.get("end_mono_ns")
        if not isinstance(start_ns, int) or not isinstance(end_ns, int) or end_ns < start_ns:
            raise ValidationError(f"resource fixture wall bracket is invalid for {label}")
        wall_ms = (end_ns - start_ns) / 1_000_000
        cpu_ms = cpu_delta_usec / 1_000
        evidence[label] = {
            "event_id": event_id,
            "status": boundary.get("status"),
            "start_status": start_resource.get("status"),
            "end_status": end_resource.get("status"),
            "cpu_usage_start_usec": cpu_start,
            "cpu_usage_end_usec": cpu_end,
            "cpu_delta_ms": cpu_ms,
            "wall_ms": wall_ms,
            "cpu_to_wall_ratio": cpu_ms / wall_ms if wall_ms else None,
            "cgroup_path": end_resource.get("cgroup_path"),
            "cgroup_files": sorted(str(name) for name in end_resource["files"]),
            "host_context": sorted(str(name) for name in end_resource["host_context"]),
            "scope": end_resource.get("scope"),
            "post_event_context": True,
            "prospective_feature": False,
        }
    busy = evidence.get("busy_resource_fixture")
    sleep = evidence.get("sleep_resource_fixture")
    if not isinstance(busy, Mapping) or not isinstance(sleep, Mapping):
        raise ValidationError("busy/sleep resource fixture labels are missing")
    if float(busy["cpu_delta_ms"]) <= float(sleep["cpu_delta_ms"]):
        raise ValidationError(f"busy fixture did not exceed sleep CPU delta: {evidence}")
    return {
        "sample_count": len(evidence),
        "samples": evidence,
        "busy_cpu_exceeded_sleep": True,
        "status_measured": all(
            sample["start_status"] == "measured" and sample["end_status"] == "measured"
            for sample in evidence.values()
        ),
    }


def validate_bpf(
    linux_dir: Path,
    telemetry_dir: Path,
    tool_rows: list[dict[str, Any]],
    script_evidence: Mapping[str, Any],
    expected_action_records: list[dict[str, Any]],
    expected_case_id: str = "persistent-shell-live-check",
) -> dict[str, Any]:
    from agentic_sim.telemetry.bpf_work import (
        BPF_EVENT_ABI,
        BPF_EVENT_SCHEMA,
        BPF_EVENT_RECORD_SIZE,
        iter_bpf_events,
    )

    expected_record_size = BPF_EVENT_RECORD_SIZE

    manifest = read_json(linux_dir / "bpf_collector_manifest.json")
    summary = read_json(linux_dir / "work_summary.json")
    lifecycle = read_json(linux_dir / "service_lifecycle.json")
    boundary_rows = read_jsonl(linux_dir / "action_boundaries.jsonl")
    stream_path = linux_dir / "raw_events.bin"
    if not stream_path.is_file():
        raise ValidationError("native BPF binary stream is missing")
    stream_sha = sha256_file(stream_path)
    stream_size = stream_path.stat().st_size
    if manifest.get("backend") != "bcc" or summary.get("backend") != "bcc":
        raise ValidationError("BPF backend is not the required BCC backend")
    native_sink = manifest.get("native_sink")
    if not isinstance(native_sink, Mapping):
        raise ValidationError("BPF manifest has no native sink descriptor")
    if native_sink.get("event_schema_version") != BPF_EVENT_SCHEMA:
        raise ValidationError("native sink event schema is not the current v3 ABI")
    if native_sink.get("event_abi") != BPF_EVENT_ABI:
        raise ValidationError("native sink scalar-argument ABI is missing or unsupported")
    if native_sink.get("record_size_bytes") != expected_record_size:
        raise ValidationError(f"native sink record size is not {expected_record_size} bytes")
    if not isinstance(native_sink.get("source_sha256"), str) or not isinstance(native_sink.get("library_sha256"), str):
        raise ValidationError("native sink source/library hashes are absent")
    for key in ("source_path", "library_path"):
        value = native_sink.get(key)
        if not isinstance(value, str) or not Path(value).is_file():
            raise ValidationError(f"native sink {key} is not retained")
    raw_stream = summary.get("raw_event_stream")
    if not isinstance(raw_stream, Mapping):
        raise ValidationError("BPF summary has no raw event stream descriptor")
    if raw_stream.get("schema_version") != BPF_EVENT_SCHEMA:
        raise ValidationError("BPF summary event schema is not the current v3 ABI")
    if raw_stream.get("event_abi") != BPF_EVENT_ABI:
        raise ValidationError("BPF summary scalar-argument ABI is missing or unsupported")
    if raw_stream.get("record_size_bytes") != expected_record_size:
        raise ValidationError(f"BPF summary record size is not {expected_record_size} bytes")
    if raw_stream.get("bytes_written") != stream_size or stream_size % expected_record_size:
        raise ValidationError("BPF binary stream size does not match native record framing")
    if manifest.get("raw_event_stream_sha256") != stream_sha or raw_stream.get("sha256") != stream_sha:
        raise ValidationError("BPF binary stream hash does not match manifest and summary")
    if lifecycle.get("status") != "stopped" or lifecycle.get("service_returncode") != 0:
        raise ValidationError(f"BPF service did not stop cleanly: {lifecycle}")
    identity = manifest.get("identity")
    if not isinstance(identity, Mapping):
        raise ValidationError("BPF manifest has no process identity")
    if identity.get("mapping_source") != "swerex_docker_persistent_bash_nspid":
        raise ValidationError(f"BPF target was not the mapped Docker persistent shell: {identity}")
    for key in ("pid", "container_pid", "pid_namespace", "start_ticks", "pid_namespace_inode"):
        if identity.get(key) in (None, "", 0):
            raise ValidationError(f"BPF persistent-shell identity is missing {key}")
    if identity.get("case_id") != expected_case_id:
        raise ValidationError(f"BPF identity case_id is not the expected live-check case: {expected_case_id}")

    expected_by_id: dict[str, dict[str, Any]] = {}
    action_labels_by_event_id = {
        str(record.get("pre_event_id")): str(record.get("label"))
        for record in expected_action_records
        if isinstance(record.get("pre_event_id"), str)
    }
    for row in tool_rows:
        if row.get("event_kind") == "tool_event" and row.get("terminal") is True:
            event_id = row.get("work_collector_event_id")
            if isinstance(event_id, str):
                expected_by_id[event_id] = {
                    "kind": "tool",
                    "command": row.get("runtime_command"),
                    "status": row.get("status"),
                    "label": action_labels_by_event_id.get(event_id, "tool"),
                }
    for item in script_evidence.get("reads", []):
        source_event_id = item.get("source_event_id")
        # Only the native-shell fallback runs a real ``pwd`` action that the
        # collector captures.  The service procfs witness has no shell action,
        # so it must not be expected in (or leak into) the BPF join.
        if isinstance(source_event_id, str) and item.get("cwd_witness_shell_action", True):
            expected_by_id[source_event_id] = {
                "kind": "script_state_query",
                "command": "pwd",
                "status": "success",
                "label": str(item.get("label")),
            }
    if not expected_by_id:
        raise ValidationError("no measured hook boundaries were available to join to BPF")

    summary_actions = summary.get("actions")
    if not isinstance(summary_actions, list):
        raise ValidationError("BPF summary actions are missing")
    summaries_by_id: dict[str, dict[str, Any]] = {}
    all_raw_rows = []
    for action in summary_actions:
        if not isinstance(action, Mapping) or not isinstance(action.get("raw"), Mapping):
            raise ValidationError("BPF summary contains an invalid action row")
        raw = dict(action["raw"])
        event_id = raw.get("boundary", {}).get("event_id") if isinstance(raw.get("boundary"), Mapping) else None
        if not isinstance(event_id, str):
            raise ValidationError("BPF action has no boundary event identity")
        summaries_by_id[event_id] = raw
        all_raw_rows.append(raw)
    finals = summary.get("action_finalizations", [])
    if not isinstance(finals, list):
        raise ValidationError("BPF action_finalizations is not a list")
    for raw in finals:
        if not isinstance(raw, Mapping):
            raise ValidationError("BPF finalization row is not an object")
        event_id = raw.get("boundary", {}).get("event_id") if isinstance(raw.get("boundary"), Mapping) else None
        if isinstance(event_id, str):
            summaries_by_id[event_id] = dict(raw)
        all_raw_rows.append(dict(raw))
    missing = sorted(set(expected_by_id).difference(summaries_by_id))
    extra = sorted(set(summaries_by_id).difference(expected_by_id))
    if missing or extra:
        raise ValidationError(f"BPF/hook identity join mismatch: missing={missing} extra={extra}")

    boundaries_by_id: dict[str, list[dict[str, Any]]] = {}
    for row in boundary_rows:
        event_id = row.get("event_id")
        if isinstance(event_id, str):
            boundaries_by_id.setdefault(event_id, []).append(row)
    observed_tokens: dict[int, int] = {}
    total_decoded = 0
    action_evidence: list[dict[str, Any]] = []
    for event_id, expected in expected_by_id.items():
        raw = summaries_by_id[event_id]
        boundary = raw.get("boundary")
        if not isinstance(boundary, Mapping):
            raise ValidationError(f"BPF raw row has no boundary: {event_id}")
        command = expected["command"]
        if boundary.get("command") != command or raw.get("command_sha256") != sha256_bytes(str(command).encode("utf-8")):
            raise ValidationError(f"BPF command identity mismatch: {event_id}")
        if boundary.get("status") != expected["status"]:
            raise ValidationError(f"BPF status mismatch: {event_id}")
        if raw.get("event_storage") != "binary" or raw.get("event_records_complete") is not True:
            raise ValidationError(f"BPF individual event evidence is incomplete: {event_id}")
        for field in LOSS_FIELDS:
            aggregate = raw.get("raw_aggregate")
            value = aggregate.get(field) if isinstance(aggregate, Mapping) else None
            if value != 0:
                raise ValidationError(f"BPF loss/map field {field}={value!r} for {event_id}")
        if raw.get("perf_lost_events") != 0 or raw.get("event_callback_errors"):
            raise ValidationError(f"BPF perf transport loss for {event_id}")
        token = raw.get("action_token")
        descriptor = raw.get("binary_event_stream")
        if not isinstance(token, int) or not isinstance(descriptor, Mapping):
            raise ValidationError(f"BPF binary binding is absent: {event_id}")
        if descriptor.get("schema_version") != BPF_EVENT_SCHEMA or descriptor.get("record_size_bytes") != expected_record_size:
            raise ValidationError(f"BPF binary range ABI differs from the collector stream: {event_id}")
        if descriptor.get("event_abi") != BPF_EVENT_ABI:
            raise ValidationError(f"BPF binary range scalar-argument ABI is missing or unsupported: {event_id}")
        start = descriptor.get("offset_start")
        end = descriptor.get("offset_end")
        record_count = descriptor.get("record_count")
        if not all(isinstance(value, int) for value in (start, end, record_count)):
            raise ValidationError(f"BPF binary range is malformed: {event_id}")
        if (end - start) % expected_record_size:
            raise ValidationError(f"BPF binary range/count mismatch: {event_id}")
        ranged = list(
            iter_bpf_events(
                stream_path,
                offset_start=start,
                offset_end=end,
                schema_version=BPF_EVENT_SCHEMA,
                record_size_bytes=expected_record_size,
            )
        )
        ranged_token_count = sum(1 for row in ranged if row.get("token") == token)
        if len(ranged) != (end - start) // expected_record_size or ranged_token_count != record_count:
            raise ValidationError(f"BPF binary range/count mismatch: {event_id}")
        token_events = list(
            iter_bpf_events(
                stream_path,
                token=token,
                schema_version=BPF_EVENT_SCHEMA,
                record_size_bytes=expected_record_size,
            )
        )
        required = raw.get("required_event_count")
        if not isinstance(required, int) or len(token_events) < required:
            raise ValidationError(f"BPF token evidence is shorter than its required count: {event_id}")
        observed_tokens[token] = len(token_events)
        total_decoded += len(ranged)
        boundary_events = boundaries_by_id.get(event_id, [])
        if not any(row.get("phase") == "start" for row in boundary_events) or not any(
            row.get("phase") == "end" for row in boundary_events
        ):
            raise ValidationError(f"BPF boundary journal lacks start/complete rows: {event_id}")
        action_evidence.append(
            {
                "event_id": event_id,
                "label": expected["label"],
                "kind": expected["kind"],
                "command_sha256": raw.get("command_sha256"),
                "status": boundary.get("status"),
                "action_token": token,
                "required_event_count": required,
                "range_record_count": len(ranged),
                "range_token_record_count": ranged_token_count,
                "token_record_count": len(token_events),
                "deferred_quiescence": raw.get("deferred_quiescence"),
                "finalization": raw.get("record_type") == "action_finalization",
            }
        )
    if set(observed_tokens) != {int(raw.get("action_token")) for raw in all_raw_rows if isinstance(raw.get("action_token"), int)}:
        raise ValidationError("BPF binary stream contains an unbound action token")
    if not total_decoded:
        raise ValidationError("BPF native binary stream decoded zero records")
    all_events = list(
        iter_bpf_events(
            stream_path,
            schema_version=BPF_EVENT_SCHEMA,
            record_size_bytes=expected_record_size,
        )
    )
    if len(all_events) != stream_size // expected_record_size:
        raise ValidationError("BPF native binary stream global record framing mismatch")
    expected_tokens = {
        int(raw.get("action_token"))
        for raw in all_raw_rows
        if isinstance(raw.get("action_token"), int)
    }
    if {int(row["token"]) for row in all_events} != expected_tokens:
        raise ValidationError("BPF native binary stream contains a token without a retained action identity")
    return {
        "backend": "bcc",
        "service_status": lifecycle.get("status"),
        "service_returncode": lifecycle.get("service_returncode"),
        "target_identity": dict(identity),
        "native_sink": dict(native_sink),
        "program_sha256": manifest.get("program_sha256"),
        "action_count": len(summary_actions),
        "finalization_count": len(finals),
        "boundary_row_count": len(boundary_rows),
        "raw_record_count": len(all_events),
        "decoded_range_record_count": total_decoded,
        "raw_stream_sha256": stream_sha,
        "actions": sorted(action_evidence, key=lambda item: str(item["event_id"])),
        "all_loss_fields_zero": True,
        "native_binary_transport": True,
        "individual_records_fully_decoded": True,
    }


class ProbeTools:
    def guard_multiline_input(self, action: str) -> str:
        """Preserve the exact submitted action for this non-model probe."""

        return action


class ProbeAgent:
    def __init__(self, env: Any):
        self._env = env
        self.tools = ProbeTools()
        self._n_consecutive_timeouts = 0


def run_action(
    *,
    label: str,
    command: str,
    expected_status: str,
    env: Any,
    hook: Any,
    output: Path,
    timeout: float = 25.0,
    check: str = "warn",
    interrupt_on_timeout: bool = False,
) -> dict[str, Any]:
    step = {"action": command}
    hook.on_step_start()
    hook.on_actions_generated(step=step)
    hook.on_action_started(step=step)
    pre_event_id = hook._tool_span.pre_event_id if hook._tool_span is not None else None
    action_id = hook._tool_span.identity.get("action_id") if hook._tool_span is not None else None
    started_at = time.time_ns()
    stdout = ""
    exception: dict[str, str] | None = None
    interrupt: dict[str, Any] | None = None
    try:
        stdout = env.communicate(command, timeout=timeout, check=check)
    except Exception as exc:  # noqa: BLE001 - retain bounded runtime failures in the evidence journal
        exception = {"type": type(exc).__name__, "message": str(exc)[:512]}
        if interrupt_on_timeout and "timeout" in type(exc).__name__.lower():
            try:
                env.interrupt_session()
                hook.agent._n_consecutive_timeouts += 1
                interrupt = {"attempted": True, "succeeded": True}
            except Exception as interrupt_exc:  # noqa: BLE001 - record interrupt-path failure without hiding it
                interrupt = {
                    "attempted": True,
                    "succeeded": False,
                    "error_type": type(interrupt_exc).__name__,
                    "error": str(interrupt_exc)[:512],
                }
        elif interrupt_on_timeout:
            interrupt = {"attempted": False, "succeeded": False, "reason": "runtime did not raise a timeout"}
    runtime = {
        "runtime_command": hook._runtime_command,
        "runtime_exit_code": hook._runtime_exit_code,
        "runtime_exit_code_observed": hook._runtime_exit_code_observed,
        "runtime_timeout": hook._runtime_timeout,
        "runtime_error": hook._runtime_error,
        "work_collector_event_id": hook._work_collector_event_id,
    }
    callback_error = None
    try:
        hook.on_action_executed(step={"action": command, "exit_status": "timeout" if hook._runtime_timeout else None})
        hook.on_step_done(step=step, info={})
    except BaseException as exc:
        callback_error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        raise
    record = {
        "label": label,
        "command": command,
        "command_sha256": sha256_bytes(command.encode("utf-8")),
        "expected_status": expected_status,
        "timeout_seconds": timeout,
        "check": check,
        "stdout": stdout,
        "stdout_sha256": sha256_bytes(stdout.encode("utf-8")),
        "stdout_bytes": len(stdout.encode("utf-8")),
        "exception": exception,
        "interrupt": interrupt,
        "runtime": runtime,
        "pre_event_id": pre_event_id,
        "action_id": action_id,
        "callback_error": callback_error,
        "recorded_at_wall_ns": time.time_ns(),
        "started_at_wall_ns": started_at,
    }
    append_jsonl(output / "runtime_actions.jsonl", record)
    return record


def live_run(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    swe_root = Path(args.swe_agent_root).expanduser().resolve()
    if not (swe_root / "sweagent").is_dir() or not (swe_root / ".venv" / "bin" / "python").exists():
        raise ValidationError(f"pinned SWE-agent checkout/.venv is unavailable: {swe_root}")
    sys.path.insert(0, str(swe_root))
    sys.path.insert(0, str(ROOT / "src"))

    import sweagent
    import swerex
    from sweagent.environment.swe_env import SWEEnv
    from swerex.deployment.config import DockerDeploymentConfig
    from swerex.deployment.docker import DockerDeployment

    from agentic_sim.telemetry.sweagent_hooks import (
        SWEAgentEnvironmentTelemetryHook,
        SWEAgentTelemetryHook,
    )
    from agentic_sim.telemetry.v2 import TelemetryV2

    agent_commit = git_value(swe_root, "rev-parse", "HEAD")
    if agent_commit != EXPECTED_SWE_AGENT_COMMIT:
        raise ValidationError(f"SWE-agent checkout is not pinned: {agent_commit}")
    if sweagent.__version__ != EXPECTED_SWE_AGENT_VERSION or swerex.__version__ != EXPECTED_SWE_REX_VERSION:
        raise ValidationError(
            f"pinned versions mismatch: sweagent={sweagent.__version__} swerex={swerex.__version__}"
        )
    image = build_pinned_image(output, args.image, args.base_image, skip_build=args.no_build_image)
    run_id = f"persistent-shell-live-{output.name}"
    telemetry_dir = output / "telemetry"
    linux_dir = output / "linux_work"
    run_metadata: dict[str, Any] = {
        "schema_version": "assignment.persistent-shell-live-check.metadata.v1",
        "run_id": run_id,
        "attempt_id": "attempt-001",
        "case_id": "persistent-shell-live-check",
        "instance_id": "docker-persistent-bash",
        "model_inference": False,
        "production_case": False,
        "agent_callback_replay": True,
        "swe_agent_root": str(swe_root),
        "swe_agent_commit": agent_commit,
        "swe_agent_version": sweagent.__version__,
        "swe_rex_version": swerex.__version__,
        "swe_agent_worktree_status": git_value(swe_root, "status", "--short"),
        "image": image,
        "host_python": sys.executable,
        "host_uid": os.getuid(),
        "host_pid_namespace": os.readlink(f"/proc/{os.getpid()}/ns/pid"),
        "output_dir": str(output),
        "previous_smoke_reference": "/tmp/assignment-docker-unpriv-bcc-rerun-ldm7agbb",
    }
    write_json(output / "run_metadata.json", run_metadata)

    script_v1 = "import os\nprint('script-v1 cwd=' + os.getcwd())\n"
    script_v2 = "import os\nprint('script-v2 cwd=' + os.getcwd())\n"
    relative_v1 = "import os\nprint('relative-v1 cwd=' + os.getcwd())\n"
    suffix = uuid.uuid4().hex[:10]
    work_dir = f"/tmp/assignment-persistent-shell-live-{suffix}"
    script_dir = f"{work_dir}/subdir"
    script_path = f"{script_dir}/run.py"
    relative_path = f"{script_dir}/relative.py"
    pager_export = "export GIT_PAGER=cat PAGER=cat MANPAGER=cat"
    pager_witness = "printf 'GIT_PAGER=%s PAGER=%s MANPAGER=%s\\n' \"$GIT_PAGER\" \"$PAGER\" \"$MANPAGER\""
    git_unpiped = "cd /testbed && git log --oneline -n 4"
    git_piped = "git log --oneline -n 4 | cat"
    script_v1_action = f"python3 {shlex.quote(script_path)}"
    edit_action = printf_write_command(script_path, script_v2)
    script_v2_action = f"python3 {shlex.quote(script_path)}"
    cd_script_dir = f"cd {shlex.quote(script_dir)}"
    relative_action = "python3 relative.py"
    failure_action = "python3 -c 'import sys; print(\"failure-witness\"); sys.exit(7)'"
    timeout_action = "python3 -c 'import time; time.sleep(2)'"
    recovery_action = "printf 'recovered cwd=%s\\n' \"$PWD\""
    cwd_action = "pwd"
    action_plan = [
        {"label": "mkdir_script_dir", "command": f"mkdir -p {shlex.quote(script_dir)}", "expected_status": "success"},
        {"label": "pager_export", "command": pager_export, "expected_status": "success"},
        {"label": "pager_environment_witness", "command": pager_witness, "expected_status": "success"},
        {"label": "git_unpiped", "command": git_unpiped, "expected_status": "success"},
        {"label": "git_piped", "command": git_piped, "expected_status": "success"},
        {"label": "script_v1_execute", "command": script_v1_action, "expected_status": "success"},
        {"label": "script_v2_edit", "command": edit_action, "expected_status": "success"},
        {"label": "script_v2_execute", "command": script_v2_action, "expected_status": "success"},
        {"label": "cd_script_dir", "command": cd_script_dir, "expected_status": "success"},
        {"label": "relative_script_execute", "command": relative_action, "expected_status": "success"},
        {"label": "intentional_failure", "command": failure_action, "expected_status": "failure"},
        {"label": "intentional_timeout", "command": timeout_action, "expected_status": "timeout"},
        {"label": "recovery_after_timeout", "command": recovery_action, "expected_status": "success"},
        {"label": "cwd_after_recovery", "command": cwd_action, "expected_status": "success"},
    ]
    write_json(
        output / "action_plan.json",
        {
            "schema_version": "assignment.persistent-shell-live-check.plan.v1",
            "work_dir": work_dir,
            "script_path": script_path,
            "relative_path": relative_path,
            "script_contents": {
                "v1_sha256": sha256_bytes(script_v1.encode("utf-8")),
                "v1_bytes": len(script_v1.encode("utf-8")),
                "v2_sha256": sha256_bytes(script_v2.encode("utf-8")),
                "v2_bytes": len(script_v2.encode("utf-8")),
                "relative_v1_sha256": sha256_bytes(relative_v1.encode("utf-8")),
                "relative_v1_bytes": len(relative_v1.encode("utf-8")),
            },
            "actions": action_plan,
            "native_operations": [
                {"label": "native_write_script_v1", "path": script_path, "sha256": sha256_bytes(script_v1.encode("utf-8"))},
                {"label": "native_read_script_after_edit", "path": script_path},
                {"label": "native_write_relative_script", "path": relative_path, "sha256": sha256_bytes(relative_v1.encode("utf-8"))},
            ],
            "hook_state_query_command": "pwd",
        },
    )

    telemetry = TelemetryV2(
        telemetry_dir,
        run_id=run_id,
        attempt_id="attempt-001",
        case_id="persistent-shell-live-check",
        instance_id="docker-persistent-bash",
        model=None,
        model_revision=None,
    )
    deployment = DockerDeployment.from_config(
        DockerDeploymentConfig(
            image=args.image,
            pull="never",
            remove_container=True,
            remove_images=False,
            python_standalone_dir=None,
        )
    )
    env = SWEEnv(deployment=deployment, repo=None, post_startup_commands=[])
    environment_hook = SWEAgentEnvironmentTelemetryHook(telemetry)
    env.add_hook(environment_hook)
    agent = ProbeAgent(env)
    hook = SWEAgentTelemetryHook(telemetry)
    agent_started = False
    env_started = False
    close_error: dict[str, str] | None = None
    action_records: list[dict[str, Any]] = []
    native_records: list[dict[str, Any]] = []
    setup_target: dict[str, Any] | None = None
    container_name: str | None = None
    try:
        env.start()
        env_started = True
        container_name = deployment.container_name
        if not isinstance(container_name, str) or not container_name:
            raise ValidationError("SWE-ReX Docker deployment did not expose a running container name")
        inspect = container_inspect(container_name)
        write_json(output / "docker_container_inspect_before_close.json", inspect or {})
        os.environ["ASSIGNMENT_TELEMETRY_V2_REQUIRED"] = "1"
        collector_config = {
            "backend": "bcc",
            "trace_format": "bcc raw individual syscall and process events plus action aggregates v2",
            "attach_existing_process": True,
            "require_persistent_runtime_pid": True,
            "output_dir": str(linux_dir),
            "socket_path": str(linux_dir / "collector.sock"),
            "session": "default",
            "startup_timeout_s": 20,
            "python_executable": "/usr/bin/python3",
        }
        write_json(
            output / "collector_bootstrap_config.json",
            {
                "schema_version": "assignment.persistent-shell-live-check.collector-config.v1",
                "explicit_target_supplied": "target" in collector_config,
                "config": collector_config,
            },
        )
        os.environ["ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG"] = json.dumps(
            collector_config,
            sort_keys=True,
        )
        hook.on_init(agent=agent)
        hook.on_run_start()
        agent_started = True
        hook.on_setup_attempt()
        discovered_target = getattr(env, "_assignment_v2_process_target", None)
        if not isinstance(discovered_target, Mapping):
            raise ValidationError("required collector did not persist its discovered persistent-shell target")
        setup_target = dict(discovered_target)
        required_target_fields = (
            "host_pid",
            "container_pid",
            "pid_namespace",
            "mapping_source",
        )
        if any(discovered_target.get(key) in (None, "", 0) for key in required_target_fields):
            raise ValidationError(f"discovered persistent-shell target is incomplete: {discovered_target}")
        write_json(
            output / "persistent_shell_witness.json",
            {
                "container_name": container_name,
                "container_pid": discovered_target["container_pid"],
                "pid_namespace": discovered_target["pid_namespace"],
                "host_pid": discovered_target["host_pid"],
                "mapping_source": discovered_target["mapping_source"],
                "witness_observed_by_required_collector_bootstrap": True,
                "explicit_target_supplied": False,
            },
        )
        hook.on_setup_done()

        for item in action_plan[:5]:
            action_records.append(
                run_action(
                    label=str(item["label"]),
                    command=str(item["command"]),
                    expected_status=str(item["expected_status"]),
                    env=env,
                    hook=hook,
                    output=output,
                )
            )
        env.write_file(script_path, script_v1)
        native_records.append(
            {
                "label": "native_write_script_v1",
                "path": script_path,
                "sha256": sha256_bytes(script_v1.encode("utf-8")),
                "bytes": len(script_v1.encode("utf-8")),
                "api": "SWEEnv.write_file -> SWE-ReX WriteFileRequest",
            }
        )
        for item in action_plan[5:8]:
            record = run_action(
                label=str(item["label"]),
                command=str(item["command"]),
                expected_status=str(item["expected_status"]),
                env=env,
                hook=hook,
                output=output,
            )
            action_records.append(record)
            if item["label"] == "script_v2_execute":
                current = env.read_file(script_path, encoding="utf-8", errors="strict")
                native_records.append(
                    {
                        "label": "native_read_script_after_edit",
                        "path": script_path,
                        "sha256": sha256_bytes(current.encode("utf-8")),
                        "bytes": len(current.encode("utf-8")),
                        "content_matches_v2": current == script_v2,
                        "api": "SWEEnv.read_file -> SWE-ReX ReadFileRequest",
                    }
                )
        action_records.append(
            run_action(
                label="cd_script_dir",
                command=cd_script_dir,
                expected_status="success",
                env=env,
                hook=hook,
                output=output,
            )
        )
        env.write_file(relative_path, relative_v1)
        native_records.append(
            {
                "label": "native_write_relative_script",
                "path": relative_path,
                "sha256": sha256_bytes(relative_v1.encode("utf-8")),
                "bytes": len(relative_v1.encode("utf-8")),
                "api": "SWEEnv.write_file -> SWE-ReX WriteFileRequest",
            }
        )
        action_records.append(
            run_action(
                label="relative_script_execute",
                command=relative_action,
                expected_status="success",
                env=env,
                hook=hook,
                output=output,
            )
        )
        action_records.append(
            run_action(
                label="intentional_failure",
                command=failure_action,
                expected_status="failure",
                env=env,
                hook=hook,
                output=output,
            )
        )
        action_records.append(
            run_action(
                label="intentional_timeout",
                command=timeout_action,
                expected_status="timeout",
                env=env,
                hook=hook,
                output=output,
                timeout=0.5,
                check="ignore",
                interrupt_on_timeout=True,
            )
        )
        action_records.append(
            run_action(
                label="recovery_after_timeout",
                command=recovery_action,
                expected_status="success",
                env=env,
                hook=hook,
                output=output,
            )
        )
        action_records.append(
            run_action(
                label="cwd_after_recovery",
                command=cwd_action,
                expected_status="success",
                env=env,
                hook=hook,
                output=output,
            )
        )
    finally:
        if env_started:
            inspect = container_inspect(container_name or deployment.container_name)
            write_json(output / "docker_container_inspect_pre_shutdown.json", inspect or {})
            try:
                env.close()
            except Exception as exc:  # noqa: BLE001 - preserve cleanup failure in the bounded result
                close_error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        if agent_started and getattr(telemetry, "_outer", None) is not None and not telemetry._outer.closed:
            telemetry.finish_outer(
                status="failure" if close_error else "success",
                error_type=close_error.get("type") if close_error else None,
                error_message=close_error.get("message") if close_error else None,
            )
    if close_error is not None:
        raise ValidationError(f"environment close failed: {close_error}")
    write_json(output / "native_operations.json", {"operations": native_records})
    write_json(output / "script_ledger_history.json", {"history": hook.script_state.history()})
    lifecycle_rows = read_jsonl(telemetry_dir / "lifecycle_events.jsonl")
    tool_rows = read_jsonl(telemetry_dir / "tool_events.jsonl")
    script_evidence = validate_script_reads(
        telemetry_dir,
        lifecycle_rows,
        [
            ("script_v1_execute", script_path, script_v1),
            ("script_v2_execute", script_path, script_v2),
            ("relative_script_execute", relative_path, relative_v1),
        ],
    )
    tool_evidence = validate_tool_rows(tool_rows, action_records)
    action_by_label = {str(row["label"]): row for row in action_records}
    interrupt_control_evidence = validate_interrupt_control(
        lifecycle_rows,
        action_by_label["intentional_timeout"],
    )
    bpf_evidence = validate_bpf(linux_dir, telemetry_dir, tool_rows, script_evidence, action_records)
    output_checks = {
        "pager_environment_persisted": "GIT_PAGER=cat PAGER=cat MANPAGER=cat\n"
        in action_by_label["pager_environment_witness"]["stdout"],
        "git_unpiped_succeeded": action_by_label["git_unpiped"]["runtime"]["runtime_exit_code"] == 0,
        "git_piped_succeeded": action_by_label["git_piped"]["runtime"]["runtime_exit_code"] == 0,
        "git_unpiped_output_observed": action_by_label["git_unpiped"]["stdout_bytes"] > 0,
        "git_piped_output_observed": action_by_label["git_piped"]["stdout_bytes"] > 0,
        "script_v1_output_observed": "script-v1 cwd=/testbed" in action_by_label["script_v1_execute"]["stdout"],
        "script_v2_output_observed": "script-v2 cwd=/testbed" in action_by_label["script_v2_execute"]["stdout"],
        "relative_script_output_observed": f"relative-v1 cwd={script_dir}" in action_by_label["relative_script_execute"]["stdout"],
        "intentional_failure_observed": action_by_label["intentional_failure"]["runtime"]["runtime_exit_code"] == 7,
        "timeout_observed": action_by_label["intentional_timeout"]["runtime"]["runtime_timeout"] is True,
        "interrupt_succeeded": action_by_label["intentional_timeout"]["interrupt"]["succeeded"] is True,
        "interrupt_control_span_observed": interrupt_control_evidence["status"] == "success",
        "same_shell_recovery_observed": f"recovered cwd={script_dir}" in action_by_label["recovery_after_timeout"]["stdout"],
        "same_shell_cwd_observed": action_by_label["cwd_after_recovery"]["stdout"].strip() == script_dir,
    }
    failed_checks = sorted(key for key, value in output_checks.items() if not value)
    if failed_checks:
        raise ValidationError(f"live behavior checks failed: {failed_checks}")
    result = {
        "schema_version": "assignment.persistent-shell-live-check.result.v1",
        "status": "pass",
        "scope": {
            "model_inference": False,
            "production_case": False,
            "gpu_inference": False,
            "actual_docker_persistent_shell": True,
            "pinned_swe_agent_commit": agent_commit,
            "swe_agent_version": sweagent.__version__,
            "swe_rex_version": swerex.__version__,
        },
        "output_checks": output_checks,
        "tool_evidence": tool_evidence,
        "script_evidence": script_evidence,
        "interrupt_control_evidence": interrupt_control_evidence,
        "bpf_evidence": bpf_evidence,
        "native_operations": native_records,
        "container_name": container_name,
        "setup_target": setup_target,
        "artifact_paths": {
            "telemetry": str(telemetry_dir),
            "linux_work": str(linux_dir),
            "runtime_actions": str(output / "runtime_actions.jsonl"),
            "persistent_shell_witness": str(output / "persistent_shell_witness.json"),
            "collector_bootstrap_config": str(output / "collector_bootstrap_config.json"),
        },
        "limitations": [
            "This is one CPU-only Docker persistent-shell run with a bounded command set; it does not establish all acquisition gates or production-case coverage.",
            "The timeout path proves one CommandTimeoutError, one BashInterruptAction, and one following command in the same live session; it does not claim full interruption/recovery coverage.",
            "BashInterruptAction is retained as a measured lifecycle control span with no command identity; it is intentionally excluded from BPF command-action joins because SWE-ReX supplies no command for the control action.",
            "Git commands use finite -n 4 output. The persistent environment variables and both unpiped/piped commands were observed, but this does not claim every interactive program is pager-free.",
            "SWEEnv.read_file returns decoded text; the existing hook records decoded_text_utf8_reencoding and byte_exact=false, which the validator checks.",
            "BPF path and byte fields retain the collector's syscall-facing semantics and selected tracepoint coverage.",
        ],
        "defects_for_root": [
            "The required collector bootstrap now resolves the target through one explicitly measured persistent-shell state-query span while collection is suspended only for that span; root should retain this sequencing when serving the production entrypoint.",
            "BashInterruptAction has no command field in SWE-ReX 1.4.0. The hook now records its measured lifecycle control interval and explicit no-command identity; it must remain separate from command-level BPF action joins.",
        ],
        "hashes": {
            "raw_events_bin": bpf_evidence["raw_stream_sha256"],
            "bpf_program": bpf_evidence["program_sha256"],
            "native_sink_source": bpf_evidence["native_sink"]["source_sha256"],
            "native_sink_library": bpf_evidence["native_sink"]["library_sha256"],
            "pinned_dockerfile": sha256_file(output / "pinned.Dockerfile"),
            "swe_agent_checkout_head": agent_commit,
        },
    }
    write_json(output / "result.json", result)
    return result


def resource_fixture_run(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    """Run only the bounded live cgroup busy/sleep resource fixture."""

    swe_root = Path(args.swe_agent_root).expanduser().resolve()
    if not (swe_root / "sweagent").is_dir() or not (swe_root / ".venv" / "bin" / "python").exists():
        raise ValidationError(f"pinned SWE-agent checkout/.venv is unavailable: {swe_root}")
    sys.path.insert(0, str(swe_root))
    sys.path.insert(0, str(ROOT / "src"))

    import sweagent
    import swerex
    from sweagent.environment.swe_env import SWEEnv
    from swerex.deployment.config import DockerDeploymentConfig
    from swerex.deployment.docker import DockerDeployment

    from agentic_sim.telemetry.sweagent_hooks import (
        SWEAgentEnvironmentTelemetryHook,
        SWEAgentTelemetryHook,
    )
    from agentic_sim.telemetry.v2 import TelemetryV2

    agent_commit = git_value(swe_root, "rev-parse", "HEAD")
    if agent_commit != EXPECTED_SWE_AGENT_COMMIT:
        raise ValidationError(f"SWE-agent checkout is not pinned: {agent_commit}")
    if sweagent.__version__ != EXPECTED_SWE_AGENT_VERSION or swerex.__version__ != EXPECTED_SWE_REX_VERSION:
        raise ValidationError(
            f"pinned versions mismatch: sweagent={sweagent.__version__} swerex={swerex.__version__}"
        )
    image = build_pinned_image(output, args.image, args.base_image, skip_build=args.no_build_image)
    run_id = f"resource-fixture-{output.name}"
    telemetry_dir = output / "telemetry"
    linux_dir = output / "linux_work"
    write_json(
        output / "run_metadata.json",
        {
            "schema_version": "assignment.container-resources-live-fixture.metadata.v1",
            "run_id": run_id,
            "attempt_id": "attempt-001",
            "case_id": "container-resources-live-fixture",
            "instance_id": "docker-persistent-bash",
            "model_inference": False,
            "production_case": False,
            "agent_callback_replay": True,
            "swe_agent_root": str(swe_root),
            "swe_agent_commit": agent_commit,
            "swe_agent_version": sweagent.__version__,
            "swe_rex_version": swerex.__version__,
            "image": image,
            "output_dir": str(output),
            "previous_live_run": "/tmp/assignment-persistent-shell-live-20260909-run7",
        },
    )
    busy_command = "python3 -c 'print(\"busy-witness\", sum(i * i for i in range(8000000)))'"
    sleep_command = "python3 -c 'import time; time.sleep(0.4); print(\"sleep-witness\")'"
    action_plan = [
        {
            "label": "busy_resource_fixture",
            "command": busy_command,
            "expected_status": "success",
        },
        {
            "label": "sleep_resource_fixture",
            "command": sleep_command,
            "expected_status": "success",
        },
    ]
    write_json(
        output / "action_plan.json",
        {
            "schema_version": "assignment.container-resources-live-fixture.plan.v1",
            "actions": action_plan,
            "measurement": "container cgroup cpu.stat delta versus caller monotonic wall boundary",
            "resource_context_scope": "post_event_context_not_prospective_feature",
        },
    )
    telemetry = TelemetryV2(
        telemetry_dir,
        run_id=run_id,
        attempt_id="attempt-001",
        case_id="container-resources-live-fixture",
        instance_id="docker-persistent-bash",
        model=None,
        model_revision=None,
    )
    deployment = DockerDeployment.from_config(
        DockerDeploymentConfig(
            image=args.image,
            pull="never",
            remove_container=True,
            remove_images=False,
            python_standalone_dir=None,
        )
    )
    env = SWEEnv(deployment=deployment, repo=None, post_startup_commands=[])
    env.add_hook(SWEAgentEnvironmentTelemetryHook(telemetry))
    agent = ProbeAgent(env)
    hook = SWEAgentTelemetryHook(telemetry)
    agent_started = False
    env_started = False
    close_error: dict[str, str] | None = None
    action_records: list[dict[str, Any]] = []
    setup_target: dict[str, Any] | None = None
    container_name: str | None = None
    try:
        env.start()
        env_started = True
        container_name = deployment.container_name
        if not isinstance(container_name, str) or not container_name:
            raise ValidationError("SWE-ReX Docker deployment did not expose a running container name")
        write_json(
            output / "docker_container_inspect_before_close.json",
            container_inspect(container_name) or {},
        )
        os.environ["ASSIGNMENT_TELEMETRY_V2_REQUIRED"] = "1"
        collector_config = {
            "backend": "bcc",
            "trace_format": "bcc raw individual syscall and process events plus action aggregates v2",
            "attach_existing_process": True,
            "require_persistent_runtime_pid": True,
            "output_dir": str(linux_dir),
            "socket_path": str(linux_dir / "collector.sock"),
            "session": "default",
            "startup_timeout_s": 20,
            "python_executable": "/usr/bin/python3",
        }
        write_json(
            output / "collector_bootstrap_config.json",
            {
                "schema_version": "assignment.container-resources-live-fixture.collector-config.v1",
                "explicit_target_supplied": "target" in collector_config,
                "config": collector_config,
            },
        )
        os.environ["ASSIGNMENT_TELEMETRY_V2_CPU_COLLECTOR_CONFIG"] = json.dumps(
            collector_config,
            sort_keys=True,
        )
        hook.on_init(agent=agent)
        hook.on_run_start()
        agent_started = True
        hook.on_setup_attempt()
        discovered_target = getattr(env, "_assignment_v2_process_target", None)
        if not isinstance(discovered_target, Mapping):
            raise ValidationError("required collector did not persist its discovered persistent-shell target")
        setup_target = dict(discovered_target)
        if any(
            discovered_target.get(key) in (None, "", 0)
            for key in ("host_pid", "container_pid", "pid_namespace", "mapping_source")
        ):
            raise ValidationError(f"discovered persistent-shell target is incomplete: {discovered_target}")
        write_json(
            output / "persistent_shell_witness.json",
            {
                "container_name": container_name,
                "container_pid": discovered_target["container_pid"],
                "pid_namespace": discovered_target["pid_namespace"],
                "host_pid": discovered_target["host_pid"],
                "mapping_source": discovered_target["mapping_source"],
                "witness_observed_by_required_collector_bootstrap": True,
                "explicit_target_supplied": False,
            },
        )
        hook.on_setup_done()
        for item in action_plan:
            action_records.append(
                run_action(
                    label=str(item["label"]),
                    command=str(item["command"]),
                    expected_status=str(item["expected_status"]),
                    env=env,
                    hook=hook,
                    output=output,
                    timeout=5.0,
                    check="warn",
                )
            )
    finally:
        if env_started:
            write_json(
                output / "docker_container_inspect_pre_shutdown.json",
                container_inspect(container_name or deployment.container_name) or {},
            )
            try:
                env.close()
            except Exception as exc:  # noqa: BLE001 - preserve cleanup failure in the fixture result
                close_error = {"type": type(exc).__name__, "message": str(exc)[:512]}
        if agent_started and getattr(telemetry, "_outer", None) is not None and not telemetry._outer.closed:
            telemetry.finish_outer(
                status="failure" if close_error else "success",
                error_type=close_error.get("type") if close_error else None,
                error_message=close_error.get("message") if close_error else None,
            )
    if close_error is not None:
        raise ValidationError(f"environment close failed: {close_error}")
    tool_rows = read_jsonl(telemetry_dir / "tool_events.jsonl")
    script_evidence = {"count": 0, "reads": []}
    tool_evidence = validate_tool_rows(tool_rows, action_records)
    bpf_evidence = validate_bpf(
        linux_dir,
        telemetry_dir,
        tool_rows,
        script_evidence,
        action_records,
        expected_case_id="container-resources-live-fixture",
    )
    resource_evidence = validate_container_resource_samples(linux_dir, tool_rows, action_records)
    result = {
        "schema_version": "assignment.container-resources-live-fixture.result.v1",
        "status": "pass",
        "scope": {
            "model_inference": False,
            "production_case": False,
            "gpu_inference": False,
            "actual_docker_persistent_shell": True,
            "pinned_swe_agent_commit": agent_commit,
            "swe_agent_version": sweagent.__version__,
            "swe_rex_version": swerex.__version__,
        },
        "setup_target": setup_target,
        "tool_evidence": tool_evidence,
        "resource_evidence": resource_evidence,
        "bpf_evidence": bpf_evidence,
        "artifact_paths": {
            "telemetry": str(telemetry_dir),
            "linux_work": str(linux_dir),
            "collector_bootstrap_config": str(output / "collector_bootstrap_config.json"),
            "persistent_shell_witness": str(output / "persistent_shell_witness.json"),
        },
        "limitations": [
            "This is a two-action CPU-only Docker fixture; it does not add production-case or acquisition-gate coverage.",
            "Cgroup resource values are whole-target-container interval context and are retained after the action boundary, not prospective feature inputs.",
            "The busy/sleep comparison is one short sample on one host and is not a scheduler or capacity benchmark.",
        ],
        "hashes": {
            "raw_events_bin": bpf_evidence["raw_stream_sha256"],
            "bpf_program": bpf_evidence["program_sha256"],
            "native_sink_source": bpf_evidence["native_sink"]["source_sha256"],
            "native_sink_library": bpf_evidence["native_sink"]["library_sha256"],
            "pinned_dockerfile": sha256_file(output / "pinned.Dockerfile"),
            "swe_agent_checkout_head": agent_commit,
        },
    }
    write_json(output / "result.json", result)
    return result


def hash_artifacts(output: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(output.rglob("*")):
        if not path.is_file() or path.name == "artifact_hashes.json":
            continue
        hashes[str(path.relative_to(output))] = sha256_file(path)
    return hashes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="new directory under /tmp for all evidence")
    parser.add_argument("--swe-agent-root", type=Path, default=DEFAULT_SWE_AGENT_ROOT)
    parser.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--no-build-image", action="store_true", help="reuse --image after verifying its SWE-ReX version")
    parser.add_argument(
        "--resource-fixture-only",
        action="store_true",
        help="run only the bounded Docker busy/sleep cgroup resource fixture",
    )
    args = parser.parse_args()
    output = args.output_dir.expanduser().resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"output directory already exists; provide a new directory: {output}")
    result: dict[str, Any]
    try:
        result = resource_fixture_run(args, output) if args.resource_fixture_only else live_run(args, output)
    except Exception as exc:  # noqa: BLE001 - persist any bounded-run failure as an artifact
        error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        (output / "fatal_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        result = {
            "schema_version": "assignment.persistent-shell-live-check.result.v1",
            "status": "fail",
            "scope": {"model_inference": False, "production_case": False, "gpu_inference": False},
            "error": error,
            "limitations": ["Live validation stopped before all bounded checks completed."],
        }
        write_json(output / "result.json", result)
    finally:
        write_json(output / "artifact_hashes.json", hash_artifacts(output))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
