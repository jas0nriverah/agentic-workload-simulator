#!/usr/bin/env python3
"""Privileged, local BCC capture smoke: real shell and raw binary validation.

Runs only a temporary persistent shell and short file operations. This is
capture validation, not a live SWE-agent pilot or an overhead acceptance test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from agentic_sim.telemetry.bpf_work import (
    BpfWorkCollector, ProcessTarget, iter_bpf_events,
    _run_bash_action_unmodified, _spawn_bash_fixture, _stop_bash_fixture,
    _write_durable_json,
    launch_bpf_work_service,
)


def run(output: Path, actions: int, *, service_mode: bool = False) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    process = _spawn_bash_fixture()
    collector = None
    service = None
    errors = []
    try:
        _run_bash_action_unmodified(process, ":", "__READY__", 5)
        target = ProcessTarget(pid=process.pid, run_id="local-capture-smoke",
                               attempt_id=output.name, case_id="persistent-shell",
                               mapping_source="owned_smoke_shell")
        if service_mode:
            service = launch_bpf_work_service(
                target, socket_path=output / "work.sock", trace_dir=output / "capture",
                startup_timeout_s=20,
            )
            collector = service.client
        else:
            collector = BpfWorkCollector.attach(target, output / "capture")
        fixture = output / "operation.txt"
        command = "printf sample > " + shlex.quote(str(fixture)) + "; cat " + shlex.quote(str(fixture))
        for index in range(actions):
            event_id = f"action-{index}"
            collector.start_action(event_id, command)
            _run_bash_action_unmodified(process, command, f"__DONE_{index}__", 5)
            collector.end_action(event_id, status="success")
        if service is not None:
            service.stop()
            summary = json.loads((output / "capture" / "work_summary.json").read_text())
            for artifact in (output / "capture").iterdir():
                if artifact.is_file():
                    with artifact.open("rb") as source:
                        source.read(1)
        else:
            summary = collector.close()
        stream_descriptor = summary.get("raw_event_stream")
        if not isinstance(stream_descriptor, dict):
            raise RuntimeError("collector summary lacks raw event stream ABI metadata")
        stream_schema = stream_descriptor.get("schema_version")
        stream_record_size = stream_descriptor.get("record_size_bytes")
        if not isinstance(stream_schema, str) or not isinstance(stream_record_size, int):
            raise RuntimeError("collector summary has incomplete raw event stream ABI metadata")
        by_token = {}
        stream = output / "capture" / "raw_events.bin"
        for event in iter_bpf_events(
            stream,
            schema_version=stream_schema,
            record_size_bytes=stream_record_size,
        ):
            entry = by_token.setdefault(event["token"], {"count": 0, "kinds": set()})
            entry["count"] += 1
            entry["kinds"].add(event["kind_name"])
        finals = {row["action_token"]: row for row in summary["action_finalizations"]}
        if len(summary["actions"]) != actions:
            errors.append("action count mismatch")
        for action in summary["actions"]:
            initial = action["raw"]
            raw = finals.get(action["action_token"], initial)
            observed = by_token.get(action["action_token"], {"count": 0, "kinds": set()})
            if observed["count"] != raw["required_event_count"] or not raw["event_records_complete"]:
                errors.append(f"{action['event_id']}: incomplete individual capture")
            if not {"read", "write", "open"} <= observed["kinds"]:
                errors.append(f"{action['event_id']}: missing expected operation kinds")
            descriptor = raw["binary_event_stream"]
            bounded = sum(1 for _ in iter_bpf_events(
                stream, token=action["action_token"],
                offset_start=descriptor["offset_start"], offset_end=descriptor["offset_end"],
                schema_version=descriptor["schema_version"],
                record_size_bytes=descriptor["record_size_bytes"],
            ))
            if bounded != raw["event_count"]:
                errors.append(f"{action['event_id']}: invalid binary byte-range binding")
        if set(by_token) != {row["action_token"] for row in summary["actions"]}:
            errors.append("unbound token in binary stream")
        result = {
            "schema_version": "assignment.local-bpf-capture-check.v1",
            "status": "pass" if not errors else "fail", "failures": errors,
            "actions": actions, "individual_records": sum(v["count"] for v in by_token.values()),
            "finalizations": len(finals), "native_sink": summary.get("native_sink"),
            "raw_stream_sha256": hashlib.sha256(stream.read_bytes()).hexdigest(),
            "capture_stop": summary.get("capture_stop"),
            "service_mode": service_mode,
            "service_returncode": service.process.returncode if service else None,
            "artifacts_readable_by_caller": True,
            "scope": "local capture smoke; no overhead or live pilot acceptance implied",
        }
        _write_durable_json(output / "result.json", result)
        return result
    finally:
        try:
            if service is not None and service.process.poll() is None:
                service.stop()
            elif service is None and collector is not None and not collector._closed:
                collector.close()
        finally:
            _stop_bash_fixture(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--actions", type=int, default=4)
    parser.add_argument("--service", action="store_true", help="test unprivileged caller with owned privileged collector")
    args = parser.parse_args()
    if not 1 <= args.actions <= 128:
        parser.error("actions must be between 1 and 128")
    result = run(args.output_dir, args.actions, service_mode=args.service)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "pass" else 1)
