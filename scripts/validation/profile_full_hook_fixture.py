#!/usr/bin/env python3
"""Diagnostic source timers for exactly one immutable full-hook CPU off/on pair.

No gate verdict is calculated. Real hooks, fsyncs, BPF capture, snapshot reset,
placement and teardown still run. Timing wrappers live only in these owned
processes; production files and frozen fixtures are never rewritten.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import functools
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT, ROOT / "src"):
    sys.path.insert(0, str(directory))
SCHEMA = "assignment.full-hook-source-profile.diagnostic.v1"


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Timers:
    """Inclusive wall intervals, nesting IDs and exact source locations.

    Context-local parents preserve async nesting. No per-event BPF callbacks
    are instrumented and no diagnostic records are written during actions.
    """

    def __init__(self, role):
        self.role = role
        self.rows = []
        self.patches = []
        self.parent = contextvars.ContextVar("profile_parent", default=None)
        self.bounds = {}

    def begin(self, label, source, detail=None):
        row = {"id": len(self.rows), "parent": self.parent.get(), "label": label,
               "source": source, "detail": detail, "pid": os.getpid(),
               "tid": threading.get_native_id(), "start_ns": time.perf_counter_ns()}
        self.rows.append(row)
        return row, self.parent.set(row["id"])

    def end(self, state, error=None):
        row, token = state
        row["end_ns"] = time.perf_counter_ns()
        row["error"] = type(error).__name__ if error is not None else None
        self.parent.reset(token)

    def wrap(self, owner, name, *, label=None, details=None):
        raw = inspect.getattr_static(owner, name)
        kind = type(raw) if isinstance(raw, (staticmethod, classmethod)) else None
        function = raw.__func__ if kind else raw
        if not inspect.isfunction(function):
            raise TypeError(f"not a Python function: {owner}.{name}")
        if getattr(function, "_diagnostic_timer", False):
            return
        source = f"{function.__code__.co_filename}:{function.__code__.co_firstlineno}"
        label = label or f"{function.__module__}.{function.__qualname__}"

        def start(args, kwargs):
            return self.begin(label, source, details(args, kwargs) if details else None)

        @functools.wraps(function)
        def sync(*args, **kwargs):
            state = start(args, kwargs)
            error = None
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self.end(state, error)

        @functools.wraps(function)
        async def asynchronous(*args, **kwargs):
            state = start(args, kwargs)
            error = None
            try:
                return await function(*args, **kwargs)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self.end(state, error)

        replacement = asynchronous if inspect.iscoroutinefunction(function) else sync
        replacement._diagnostic_timer = True
        setattr(owner, name, kind(replacement) if kind else replacement)
        self.patches.append((owner, name, raw))

    def methods(self, owner, names):
        for name in names.split():
            self.wrap(owner, name)

    def fsync(self):
        original = os.fsync

        def measured(fd):
            caller = sys._getframe(1)
            source = f"{caller.f_code.co_filename}:{caller.f_lineno}"
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                target = "unavailable"
            state = self.begin("os.fsync", source, target)
            error = None
            try:
                return original(fd)
            except BaseException as exc:
                error = exc
                raise
            finally:
                self.end(state, error)

        os.fsync = measured
        self.patches.append((os, "fsync", original))

    def restore(self):
        for owner, name, original in reversed(self.patches):
            setattr(owner, name, original)

    def export(self):
        return {"schema_version": SCHEMA, "role": self.role, "diagnostic_only": True,
                "clock": "host time.perf_counter_ns (CLOCK_MONOTONIC)",
                "bounds": self.bounds, "rows": self.rows}


def instrument_common(p):
    from agentic_sim.telemetry import bpf_work as bpf, linux_work, v2
    p.fsync()
    p.methods(v2.AppendOnlyWriter, "append write")
    p.methods(v2.TelemetryV2, "_append _begin finish_span _write_manifest _store_bytes record_script_artifact finish_outer")
    p.wrap(v2, "capture_process_resources")
    p.methods(linux_work, "_append_jsonl _snapshot_process_tree")
    p.methods(bpf, "_write_durable_json _capture_service_identity launch_bpf_work_service")
    p.wrap(bpf.BpfWorkClient, "_call", details=lambda a, k: a[1].get("op"))
    p.methods(bpf.BpfWorkServiceProcess, "stop")


def instrument_runtime(p, runtime):
    from agentic_sim.telemetry import sweagent_hooks as hooks
    remote = importlib.import_module("swerex.runtime.remote")
    p.methods(runtime["SWEEnv"], "start close communicate read_file")
    p.methods(runtime["DockerDeployment"], "start stop")
    p.methods(remote.RemoteRuntime, "read_file write_file execute close upload")
    p.wrap(remote.RemoteRuntime, "run_in_session", details=lambda a, k: getattr(a[1], "command", None))
    p.wrap(remote.RemoteRuntime, "_request", details=lambda a, k: a[1])
    p.methods(runtime["live"], "run_action container_inspect")
    p.methods(hooks, "_persistent_shell_observation _map_container_pid_to_host")
    p.methods(hooks.SWEAgentTelemetryHook,
              "_start_work_collection _end_work_collection _resolve_work_target _ensure_work_collector "
              "_stop_work_service _query_container_working_directory _native_script_snapshot "
              "_refresh_script_state_before_action _invalidate_after_action on_init on_run_start "
              "on_setup_attempt on_setup_done on_step_start on_actions_generated on_action_started "
              "on_action_executed on_step_done on_run_done")
    p.methods(hooks.SWEAgentEnvironmentTelemetryHook, "on_close")


def child(args):
    from scripts.validation import fixed_work_adapter as adapter
    p = Timers("controller")
    instrument_common(p)
    p.methods(adapter, "_run_sweenv_actions _write_json _write_bytes _append_jsonl _validate_full_sweenv_capture")
    load = adapter._load_pinned_swe_runtime

    def profiled_load(*a, **k):
        runtime = load(*a, **k)
        instrument_runtime(p, runtime)
        return runtime

    adapter._load_pinned_swe_runtime = profiled_load
    # Replace only the owned BPF service entry point, preserving sudo, env,
    # Python executable, identity, target PID, UDS and all serve arguments.
    original_popen = subprocess.Popen
    launches = []

    def profiled_popen(argv, *a, **k):
        if isinstance(argv, (tuple, list)):
            argv = list(argv)
            for index in range(len(argv) - 2):
                if argv[index:index + 3] == ["-m", "agentic_sim.telemetry.bpf_work", "serve"]:
                    original = list(argv)
                    argv[index:index + 2] = [str(Path(__file__).resolve()), "service",
                                           "--profile-output", str(args.output_dir / "service_profile.json"), "--"]
                    launches.append({"original": original, "diagnostic": list(argv)})
                    break
        return original_popen(argv, *a, **k)

    subprocess.Popen = profiled_popen
    # Trace only this function's lines to read its actual existing timing
    # locals. No copied/redefined work boundary or source modification.
    code = adapter._cpu_condition_sweenv.__code__

    def boundary_trace(frame, event, arg):
        if frame.f_code is not code:
            return None
        for name in ("startup_started", "work_started", "work_ended"):
            value = frame.f_locals.get(name)
            if isinstance(value, int):
                p.bounds[name] = value
        return boundary_trace

    error = None
    sys.settrace(boundary_trace)
    try:
        adapter.run_adapter(args.manifest, args.fixture_id, args.mode, args.scratch_dir,
                            args.output_dir, args.output_dir / "replay_result.json", 0)
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        adapter._write_blocked(args.output_dir, exc)
        raise
    finally:
        sys.settrace(None)
        subprocess.Popen = original_popen
        adapter._load_pinned_swe_runtime = load
        p.restore()
        save(args.output_dir / "controller_profile.json", {**p.export(), "error": error, "service_launches": launches})
    return 0


def service(args):
    from agentic_sim.telemetry import bpf_work as bpf, container_resources
    p = Timers("service")
    instrument_common(p)
    p.methods(bpf.BpfWorkCollector,
              "attach start_action end_action close _container_resources _capture_event_boundary "
              "_drain_perf_events _flush_raw_event_stream _start_raw_event_sync _join_raw_event_sync "
              "_stop_perf_poller _close_perf_buffers _write_raw_action _finalize_deferred _detach_bpf "
              "_snapshot_aggregate _snapshot_paths _snapshot_pending _delete_action_maps")
    p.wrap(bpf.BpfWorkService, "_dispatch", details=lambda a, k: a[1].get("op"))
    p.wrap(container_resources, "capture_container_resources")
    # These are a bounded set of tracepoint detach calls, not event callbacks.
    p.methods(bpf._require_bcc(), "detach_tracepoint cleanup")
    try:
        return bpf.main(args.serve_args[1:] if args.serve_args[:1] == ["--"] else args.serve_args)
    finally:
        p.restore()
        started = time.perf_counter_ns()
        save(args.profile_output, p.export())
        ended = time.perf_counter_ns()
        # Export happens before supervisor exit, hence its measured duration
        # overlaps controller stop/wait. It is profiler overhead, not capture.
        save(args.profile_output.with_suffix(".export.json"), {"start_ns": started, "end_ns": ended,
             "note": "Includes primary profile fsync; this small metadata export is additional profiler overhead."})


def union_ns(intervals):
    total, stop = 0, -1
    for start, end in sorted(intervals):
        total += max(0, end - max(start, stop))
        stop = max(stop, end)
    return total


def summarize(profile, bounds):
    groups = {}
    for row in profile["rows"]:
        if "end_ns" not in row:
            raise ValueError("unfinished source timer")
        for phase, low, high in (("startup", bounds["startup_started"], bounds["work_started"]),
                                 ("work", bounds["work_started"], bounds["work_ended"]),
                                 ("post_work", bounds["work_ended"], 2**63 - 1)):
            start, end = max(low, row["start_ns"]), min(high, row["end_ns"])
            if end <= start:
                continue
            key = (phase, row["label"], row["source"], json.dumps(row["detail"]))
            group = groups.setdefault(key, {"phase": phase, "label": row["label"], "source": row["source"],
                "detail": row["detail"], "calls": 0, "inclusive_ns": 0, "exclusive_ns": 0, "intervals": []})
            children = [(max(start, child["start_ns"]), min(end, child["end_ns"]))
                        for child in profile["rows"] if child["parent"] == row["id"]
                        and child["start_ns"] < end and child["end_ns"] > start]
            group["calls"] += 1
            group["inclusive_ns"] += end - start
            group["exclusive_ns"] += end - start - union_ns(children)
            group["intervals"].append((start, end))
    result = []
    for group in groups.values():
        group["union_ms"] = union_ns(group.pop("intervals")) / 1e6
        for key in ("inclusive", "exclusive"):
            group[key + "_ms"] = group.pop(key + "_ns") / 1e6
        result.append(group)
    return sorted(result, key=lambda row: (row["phase"], -row["inclusive_ms"]))


def sources():
    paths = list((ROOT / "src/agentic_sim/telemetry").glob("*.py"))
    paths += [ROOT / "scripts/validation" / name for name in (
        "fixed_work_adapter.py", "check_persistent_shell_capture.py", "run_instrumentation_replay.py",
        "profile_full_hook_fixture.py")]
    return {str(path): digest(path) for path in paths}


def pair(args):
    from scripts.validation import run_instrumentation_replay as replay
    from agentic_sim.telemetry.cpu_policy import runtime_placement
    manifest = replay.load_manifest(args.manifest)
    case = next(case for case in manifest["cases"] if case["case_id"] == args.fixture_id)
    before = replay._fixture_fingerprint(case)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report = {"schema_version": SCHEMA, "diagnostic_only": True, "gate_evaluated": False,
              "fixture_id": args.fixture_id, "manifest_sha256": digest(args.manifest),
              "fixture_before": before, "sources_before": sources(), "conditions": {},
              "limitations": ["Selective instrumentation perturbs timing; this is not acceptance evidence.",
                  "Inclusive parent/child times and controller/service times overlap; never sum them.",
                  "Exclusive times subtract only instrumented direct children, not all internal operations.",
                  "Service profile export is inside controller teardown; separately reported profiler overhead.",
                  "Only one off/on file pair; cannot re-estimate gate medians or test-fixture delta."]}
    save(args.output_dir / "invocation.json", {"argv": sys.argv, **report})
    with runtime_placement(args.runtime_manifest, args.runtime_sha256, active=True):
        for mode in ("instrument_off", "instrument_on"):
            output = args.output_dir / mode
            output.mkdir()
            scratch = output / "scratch"
            reset = replay._extract_snapshot(case["pretrajectory_snapshot"], scratch)
            argv = [sys.executable, str(Path(__file__).resolve()), "child", "--manifest", str(args.manifest),
                    "--fixture-id", args.fixture_id, "--mode", mode, "--scratch-dir", str(scratch),
                    "--output-dir", str(output)]
            env = replay._condition_env(case, mode=mode, repeat=0, scratch_dir=scratch, output_dir=output)
            env["ASSIGNMENT_TELEMETRY_V2_AUTO"] = "0"
            with (output / "stdout.log").open("x") as stdout, (output / "stderr.log").open("x") as stderr:
                process = subprocess.run(argv, env=env, cwd=ROOT, stdout=stdout, stderr=stderr, timeout=180)
            result, errors = replay._validate_result(output / "replay_result.json", case, mode=mode, repeat=0, expected=before)
            profile = json.loads((output / "controller_profile.json").read_text())
            condition = {"argv": argv, "returncode": process.returncode, "result": result,
                         "validation_errors": errors, "scratch_reset": reset,
                         "controller": summarize(profile, profile["bounds"]) if not profile["error"] else []}
            if (output / "service_profile.json").exists():
                condition["service"] = summarize(json.loads((output / "service_profile.json").read_text()), profile["bounds"])
                condition["service_profile_export"] = json.loads((output / "service_profile.export.json").read_text())
            report["conditions"][mode] = condition
            save(output / "diagnostic_summary.json", condition)
            print(json.dumps({"mode": mode, "returncode": process.returncode, "errors": errors,
                              "work_wall_ms": (result or {}).get("work_wall_ms")}), flush=True)
            if process.returncode or errors:
                break
    report["fixture_after"] = replay._fixture_fingerprint(case)
    report["sources_after"] = sources()
    report["fixture_unchanged"] = report["fixture_before"] == report["fixture_after"]
    report["source_changes"] = [key for key, value in report["sources_before"].items()
                                if report["sources_after"].get(key) != value]
    report["pair_valid"] = len(report["conditions"]) == 2 and report["fixture_unchanged"] and not report["source_changes"] and all(
        not row["validation_errors"] and row["returncode"] == 0 for row in report["conditions"].values())
    save(args.output_dir / "profile_report.json", report)
    return 0 if report["pair_valid"] else 2


def self_test():
    import tempfile
    import types
    p = Timers("test")
    namespace = types.SimpleNamespace()

    def fail():
        raise ValueError("preserved")

    async def inner(value):
        return value + 1

    async def outer(value):
        return await namespace.inner(value)

    namespace.fail, namespace.inner, namespace.outer = fail, inner, outer
    p.methods(namespace, "fail inner outer")
    p.fsync()
    try:
        assert asyncio.run(namespace.outer(4)) == 5
        try:
            namespace.fail()
        except ValueError as exc:
            assert str(exc) == "preserved"
        else:
            raise AssertionError("exception swallowed")
        with tempfile.TemporaryFile() as stream:
            stream.write(b"actual durability call")
            stream.flush()
            os.fsync(stream.fileno())
        assert p.rows[1]["parent"] == p.rows[0]["id"]
        assert p.rows[2]["error"] == "ValueError"
        assert p.rows[3]["label"] == "os.fsync"
        assert union_ns([(0, 4), (2, 7), (8, 9)]) == 8
    finally:
        p.restore()
    assert namespace.inner is inner
    print("self-test passed: async nesting, return values, exceptions, real fsync, restore, interval union")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("pair", "child"):
        item = sub.add_parser(command)
        item.add_argument("--manifest", type=Path, required=True)
        item.add_argument("--fixture-id", choices=["cpu-file-traversal-v1"], default="cpu-file-traversal-v1")
        item.add_argument("--output-dir", type=Path, required=True)
        if command == "pair":
            item.add_argument("--runtime-manifest", type=Path, required=True)
            item.add_argument("--runtime-sha256", required=True)
        else:
            item.add_argument("--mode", choices=["instrument_off", "instrument_on"], required=True)
            item.add_argument("--scratch-dir", type=Path, required=True)
    item = sub.add_parser("service")
    item.add_argument("--profile-output", type=Path, required=True)
    item.add_argument("serve_args", nargs=argparse.REMAINDER)
    sub.add_parser("self-test")
    args = parser.parse_args()
    return {"pair": pair, "child": child, "service": service}.get(args.command, lambda _: self_test())(args)


if __name__ == "__main__":
    raise SystemExit(main())
