#!/usr/bin/env python3
"""Generate deterministic, explicitly synthetic v2 telemetry fixtures.

The output is offline evidence for parser, identity, lifecycle, retry, script
state, and residual-closure checks.  It contains no live run claim and is
never used as a pilot acceptance result.  Existing output is treated as
immutable; choose a new directory when regenerating a fixture set.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agentic_sim.telemetry.features import build_tool_features, canonical_json  # noqa: E402
from agentic_sim.telemetry.script_state import ScriptStateLedger  # noqa: E402
from agentic_sim.telemetry.v2 import TelemetryV2  # noqa: E402


FIXTURE_SCHEMA = "assignment.telemetry.v2.synthetic-fixture.v1"
FIXTURE_CLOCK = {
    "hostname": "synthetic-v2-fixture",
    "boot_id": "synthetic-boot-20260908",
    "clock_id": "CLOCK_MONOTONIC_RAW",
    "source": "synthetic-fixed-clock",
}
FIXED_UTC = "2026-09-08T00:00:00.000Z"


def _canonical_dump(path: Path, value: Any) -> None:
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _normalise_journals(output: Path) -> None:
    """Replace wall-clock recording metadata with a fixed fixture timestamp."""

    for path in sorted(output.glob("*.jsonl")):
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "process_resources_at_record" in row:
                row["process_resources_at_record"] = {
                    "schema_version": "assignment.process-resource-snapshot.v1",
                    "availability": "unavailable",
                    "reason": "Synthetic fixture; no physical process-resource measurement is claimed.",
                }
            for key in ("started_at_utc", "ended_at_utc", "utc_recorded"):
                if key in row and row[key] is not None:
                    row[key] = FIXED_UTC
            rows.append(row)
        path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def _write_parser_cases(output: Path) -> None:
    commands = [
        "git diff",
        "git diff | head",
        "git diff | head || true",
        "python -m pytest tests/test_x.py",
        r"find . -exec python -m pytest {} \;",
        "echo 'pytest tests/x.py | cat'",
    ]
    cases = {
        "schema_version": FIXTURE_SCHEMA,
        "synthetic": True,
        "live_acceptance": False,
        "commands": [
            {
                "command": command,
                "features": build_tool_features(command),
            }
            for command in commands
        ],
    }
    _canonical_dump(output / "parser_cases.json", cases)


def _write_readme(output: Path) -> None:
    (output / "README.md").write_text(
        """# Synthetic v2 fixtures

Every file in this directory is generated offline by
`scripts/validation/generate_v2_fixtures.py`. The fixed clock, action/request
identities, failures, timeout, retry, script edit, parser structures, and
UNKNOWN complement are synthetic evidence for contract tests. They are not
measurements from a live pilot and do not establish any acceptance gate.

Regenerate into a new empty directory and audit with:

```text
PYTHONPATH=src python3 scripts/validation/generate_v2_fixtures.py --output <new-directory>
PYTHONPATH=src python3 scripts/validation/audit_v2_journals.py <new-directory>
```
""",
        encoding="utf-8",
    )


def _write_script_history(output: Path, ledger: ScriptStateLedger) -> None:
    _canonical_dump(
        output / "script_state_history.json",
        {
            "schema_version": FIXTURE_SCHEMA,
            "synthetic": True,
            "live_acceptance": False,
            "history": ledger.history(),
        },
    )


def generate(output: Path) -> dict[str, Any]:
    if output.exists():
        if not output.is_dir():
            raise ValueError(f"fixture output is not a directory: {output}")
        if any(output.iterdir()):
            raise ValueError(f"refusing to overwrite non-empty fixture output: {output}")
    output.mkdir(parents=True, exist_ok=False)
    recorder = TelemetryV2(
        output,
        run_id="synthetic-v2-run",
        attempt_id="synthetic-attempt-001",
        case_id="synthetic-case-lifecycle",
        instance_id="synthetic-instance",
        clock=FIXTURE_CLOCK,
        writer_role="synthetic-runner",
    )
    ledger = ScriptStateLedger()
    outer = recorder.start_outer(start_mono_ns=1_000)
    setup = recorder.start_phase("setup", start_mono_ns=1_010, parent_event_id=outer.pre_event_id)
    setup.finish(end_mono_ns=1_050)
    startup = recorder.start_phase("startup", start_mono_ns=1_050, parent_event_id=outer.pre_event_id)
    startup.finish(end_mono_ns=1_080)

    source_one = "#!/usr/bin/env python3\nprint('first')\n"
    source_two = "#!/usr/bin/env python3\nprint('second')\n"
    state_query = recorder.start_phase(
        "state_query",
        start_mono_ns=1_080,
        parent_event_id=outer.pre_event_id,
        event_kind="script_read",
        reason="synthetic native-container script read",
    )
    first_artifact = recorder.record_script_artifact(
        container_path="/fixture/run.py",
        content=source_one,
        generation=1,
    )
    first_descriptor = {
        "path": "/fixture/run.py",
        "sha256": first_artifact["sha256"],
        "size_bytes": first_artifact["size_bytes"],
        "content_artifact": {
            key: first_artifact[key]
            for key in ("artifact_path", "sha256", "encoding", "size_bytes", "truncated")
        },
    }
    first_state = ledger.snapshot([first_descriptor], source_event_id=state_query.pre_event_id, observed_at_mono_ns=1_085)
    state_query.finish(
        status="success",
        end_mono_ns=1_110,
        script_state=first_state,
        script_artifacts=[first_artifact],
        script_read_cap_bytes=256 * 1024,
        script_read_count=1,
    )
    _write_script_history(output, ledger)

    recorder.record_tool_intent(
        "python run.py",
        logical_operation_id="synthetic-operation-1",
        action_id="synthetic-intent-1",
        step_id=1,
        start_mono_ns=1_110,
        script_state=first_state,
    )
    failed_tool = recorder.begin_tool(
        "python run.py",
        action_id="synthetic-action-1",
        logical_operation_id="synthetic-operation-1",
        step_id=1,
        start_mono_ns=1_120,
        script_state=first_state,
        actual_action="python run.py",
    )
    recorder.end_tool(
        failed_tool,
        status="failure",
        end_mono_ns=1_200,
        error_type="CommandExitError",
        error_message="synthetic exit status 2",
        command_exit_code=2,
        command_exit_code_availability="measured",
        runtime_command="python run.py",
    )
    ledger.record_edit(
        ["/fixture/run.py"],
        source_event_id="synthetic-edit-1",
        observed_at_mono_ns=1_210,
    )

    first_request = recorder.begin_request(
        {"max_output_tokens": 8, "model": "synthetic-model"},
        logical_request_id="synthetic-logical-request",
        physical_request_id="synthetic-physical-request-0",
        step_id=1,
        start_mono_ns=1_220,
    )
    recorder.end_request(
        first_request,
        status="failure",
        end_mono_ns=1_250,
        error_type="HTTPError",
        error_message="synthetic upstream failure",
    )
    retry = recorder.start_phase(
        "retry",
        start_mono_ns=1_250,
        parent_event_id=first_request.pre_event_id,
        event_kind="model_retry",
        reason="synthetic physical retry",
    )
    retry.finish(end_mono_ns=1_260)
    second_request = recorder.begin_request(
        {"max_output_tokens": 8, "model": "synthetic-model"},
        logical_request_id="synthetic-logical-request",
        physical_request_id="synthetic-physical-request-1",
        retry_index=1,
        retry_of=first_request.identity["physical_request_id"],
        step_id=1,
        start_mono_ns=1_260,
    )
    recorder.end_request(
        second_request,
        end_mono_ns=1_330,
        response={"usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        response_sha256="a" * 64,
    )

    second_artifact = recorder.record_script_artifact(
        container_path="/fixture/run.py",
        content=source_two,
        generation=3,
    )
    second_state = ledger.snapshot(
        [
            {
                "path": "/fixture/run.py",
                "sha256": second_artifact["sha256"],
                "size_bytes": second_artifact["size_bytes"],
                "content_artifact": {
                    key: second_artifact[key]
                    for key in ("artifact_path", "sha256", "encoding", "size_bytes", "truncated")
                },
            }
        ],
        source_event_id="synthetic-state-2",
        observed_at_mono_ns=1_340,
    )
    timeout_tool = recorder.begin_tool(
        r"find . -exec python -m pytest {} \;",
        action_id="synthetic-action-2",
        logical_operation_id="synthetic-operation-2",
        retry_index=0,
        step_id=2,
        start_mono_ns=1_350,
        script_state=second_state,
    )
    recorder.end_tool(
        timeout_tool,
        status="timeout",
        end_mono_ns=1_400,
        error_type="CommandTimeoutError",
        error_message="synthetic bounded command timeout",
    )
    teardown = recorder.start_phase("teardown", start_mono_ns=1_900, parent_event_id=outer.pre_event_id)
    teardown.finish(end_mono_ns=1_950)
    recorder.finish_outer(status="success", end_mono_ns=2_000)
    recorder.reconcile_e2e()
    _write_script_history(output, ledger)
    _write_parser_cases(output)
    _write_readme(output)
    _normalise_journals(output)

    files: list[dict[str, Any]] = []
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        if path.name in {"fixture_manifest.json", "fixture_manifest.json.sha256"}:
            continue
        files.append(
            {
                "path": path.relative_to(output).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
        )
    manifest = {
        "schema_version": FIXTURE_SCHEMA,
        "synthetic": True,
        "live_acceptance": False,
        "purpose": "offline v2 contract and integration fixture; not a live pilot result",
        "clock": FIXTURE_CLOCK,
        "run_id": recorder.run_id,
        "attempt_id": recorder.attempt_id,
        "case_id": recorder.case_id,
        "files": files,
    }
    manifest_path = output / "fixture_manifest.json"
    _canonical_dump(manifest_path, manifest)
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    (output / "fixture_manifest.json.sha256").write_text(
        f"{manifest_digest}  {manifest_path.name}\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "SNAP" / "verification" / "v2-fixtures",
    )
    args = parser.parse_args()
    manifest = generate(args.output)
    print(json.dumps({"output": str(args.output), "files": len(manifest["files"]), "synthetic": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
