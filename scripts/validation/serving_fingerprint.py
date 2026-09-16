"""Pure, allowlisted serving fingerprint builders. No imports of serving packages or I/O.

Callers supply bytes/text from their already authorized capture and an epoch identity.
Receipts describe evidence, never readiness or acceptance. Literal source defaults are
not proof of the running configuration. Startup observations are server defaults, not
the sampling settings of any individual request. Unknown values remain unavailable.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence


SCHEMA = "assignment.serving-fingerprint-receipt.v1"
SAMPLING = {
    "repetition_penalty": float, "top_k": int, "temperature": float,
    "top_p": float, "min_p": float, "max_tokens": int,
}
PARAMETERS = {
    "model": str, "served_model_name": str, "revision": str, "tokenizer": str,
    "tokenizer_revision": str, "generation_config": str,
    "dtype": str, "kv_cache_dtype": str, "quantization": str,
    "tensor_parallel_size": int, "pipeline_parallel_size": int,
    "max_model_len": int, "enable_chunked_prefill": bool,
    "enable_prefix_caching": bool, "disable_log_stats": bool,
    "enable_prompt_tokens_details": bool, "max_num_batched_tokens": int,
    "max_num_seqs": int, "gpu_memory_utilization": float,
    "seed": int, "enforce_eager": bool, "disable_log_requests": bool,
    "enable_request_id_headers": bool, "host": str, "port": int,
}
FLAGS = {"--" + key.replace("_", "-"): key for key in PARAMETERS}
FLAGS.update({"--no-" + key.replace("_", "-"): key
              for key, kind in PARAMETERS.items() if kind is bool})
# Retain this older spelling as explicit configuration evidence only.
FLAGS["--disable-prefix-caching"] = "enable_prefix_caching"
ENVIRONMENT = {
    "CUDA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER", "SLURM_JOB_ID", "SLURM_STEP_ID",
    "SLURM_CPUS_PER_TASK", "SLURM_CPUS_ON_NODE", "SLURM_CPU_BIND",
}
IDENTITY = {"hostname", "boot_id", "server_pid", "server_process_start_ticks",
            "counter_epoch", "source_manifest_sha256"}
VERSION_FIELDS = {"vllm_version", "torch_version", "torch_cuda_build_version",
                  "cuda_runtime_version", "cuda_driver_version"}
ENGINE_KEYS = {
    "dtype": "dtype", "kv_cache_dtype": "kv_cache_dtype",
    "quantization": "quantization", "tensor_parallel_size": "tensor_parallel_size",
    "pipeline_parallel_size": "pipeline_parallel_size", "max_seq_len": "max_model_len",
    "enable_prefix_caching": "enable_prefix_caching",
    "chunked_prefill_enabled": "enable_chunked_prefill",
}


def digest(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def _value(value: Any, kind: type, *, nullable: bool = False) -> Any:
    if value is None and nullable:
        return None
    if kind is bool:
        if type(value) is bool:
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
    elif kind in (int, float):
        if type(value) in (int, float) or isinstance(value, str):
            if kind is int and not re.fullmatch(r"[+-]?\d+", str(value)):
                raise ValueError("not an integer")
            result = kind(value)
            if math.isfinite(result):
                return result
    elif kind is str and isinstance(value, str) and value and not any(
            ord(c) < 32 for c in value):
        # Paths/IDs are allowed; credential-bearing URLs are not.
        if "://" not in value or not any(x in value for x in ("@", "?", "#")):
            return value
    raise ValueError("unsupported value")


def _record(value: Any, origin: str, source: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": origin, "value": value, "source": dict(source)}


def _source(text: str, locator: str) -> dict[str, str]:
    return {"locator": locator, "sha256": digest(text)}


def parse_explicit_argv(argv: Sequence[str], *, complete: bool = True) -> dict[str, Any]:
    """Export only recognized flags. Omitted flags never become false/defaults.

    Repeated/contradictory flags remain ambiguous without pretending to reproduce
    a particular release's argparse semantics. Unknown option values are skipped.
    """
    absence = "flag_absent" if complete else "flag_not_in_partial_capture"
    result = {key: unavailable(absence) for key in PARAMETERS}
    result["override_generation_config"] = unavailable(absence)
    occurrences: dict[str, list[dict[str, Any]]] = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        i += 1
        if token == "--":
            break
        flag, sep, raw = token.partition("=")
        key = FLAGS.get(flag)
        if flag == "--override-generation-config":
            key = "override_generation_config"
        if key is None:
            # Never examine the ordinary value of an unknown option as a flag.
            if not sep and token.startswith("--") and i < len(argv) and not argv[i].startswith("--"):
                i += 1
            continue
        kind = PARAMETERS.get(key, dict)
        negative = flag.startswith("--no-") or flag == "--disable-prefix-caching"
        if not sep:
            if kind is bool:
                raw = not negative
                if i < len(argv) and argv[i].lower() in {"true", "false"}:
                    raw = argv[i]
                    i += 1
            elif i < len(argv) and not argv[i].startswith("--"):
                raw = argv[i]
                i += 1
            else:
                raw = None
        try:
            if negative and (sep or isinstance(raw, str)):
                raise ValueError("negative flag with a value requires parser resolution")
            value = _sampling(json.loads(raw)) if kind is dict else _value(raw, kind)
            item = {"flag": flag, "value": value}
        except (ValueError, TypeError, json.JSONDecodeError):
            item = {"flag": flag, "status": "unavailable", "reason": "malformed_flag_value"}
        occurrences.setdefault(key, []).append(item)
    for key, found in occurrences.items():
        if len(found) != 1:
            result[key] = {**unavailable("repeated_flag_requires_parser_resolution"), "occurrences": found}
        elif "value" not in found[0]:
            result[key] = found[0]
        else:
            result[key] = {"status": "explicit_argv", **found[0]}
    return result


def _sampling(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("sampling config is not an object")
    out = {}
    for key, kind in SAMPLING.items():
        if key in value:
            try:
                out[key] = _value(value[key], kind)
            except ValueError:
                continue
    return out


def _safe_evidence(value: Any, kind: type) -> dict[str, Any]:
    """Project builder results again at receipt assembly; never forward extra fields."""
    if not isinstance(value, Mapping):
        return unavailable("evidence_not_supplied")
    if value.get("status") not in {"literal_source_default", "generation_config_source"}:
        return unavailable("source_default_unresolved")
    source = value.get("source", {})
    if not isinstance(source, Mapping) or not re.fullmatch(r"[a-f0-9]{64}", str(source.get("sha256", ""))):
        return unavailable("source_hash_unavailable")
    safe = {}
    for key in ("locator", "sha256", "symbol", "revision", "line"):
        if key in source:
            try:
                safe[key] = _value(source[key], int if key == "line" else str)
            except ValueError:
                continue
    try:
        parsed = _value(value.get("value"), kind, nullable=True)
    except ValueError:
        return unavailable("invalid_source_value")
    return _record(parsed, value["status"], safe)


def literal_source_defaults(text: str, *, locator: str,
                            selectors: Mapping[str, str]) -> dict[str, Any]:
    """Read specific module/Class.attribute literal assignments without execution.

    Selectors map receipt fields to qualified source symbols (EngineArgs.dtype,
    __version__, cuda, etc.). Calls, factories, references, nested control flow,
    and duplicate assignments stay unavailable. These are declarations only;
    constructor/device-dependent resolution requires separate runtime evidence.
    """
    allowed = {**PARAMETERS, **SAMPLING, **{key: str for key in VERSION_FIELDS}}
    selectors = {k: v for k, v in selectors.items() if k in allowed}
    out = {k: unavailable("literal_source_default_not_found") for k in selectors}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {k: unavailable("source_ast_invalid") for k in selectors}
    found: dict[str, list[tuple[ast.AST, bool]]] = {}

    def scan(body, prefix="", direct=True):
        for node in body:
            if isinstance(node, ast.ClassDef):
                scan(node.body, prefix + node.name + ".", direct)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        found.setdefault(prefix + target.id, []).append((node, direct))
            elif not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Conditional assignments are not unconditional defaults.
                for field in ("body", "orelse", "finalbody"):
                    scan(getattr(node, field, []), prefix, False)
                for handler in getattr(node, "handlers", []):
                    scan(handler.body, prefix, False)

    scan(tree.body)
    for key, symbol in selectors.items():
        matches = found.get(symbol, [])
        if len(matches) != 1 or not matches[0][1]:
            if matches:
                out[key] = unavailable("ambiguous_or_conditional_source_default")
            continue
        node, _ = matches[0]
        try:
            value = _value(ast.literal_eval(node.value), allowed[key],
                           nullable=key in {"quantization", "enable_prefix_caching", "enable_chunked_prefill"})
        except (ValueError, TypeError):
            out[key] = unavailable("source_default_requires_runtime_resolution")
            continue
        out[key] = _record(value, "literal_source_default", {
            **_source(text, locator), "symbol": symbol, "line": node.lineno})
    return out


def generation_config(text: str, *, locator: str, revision: str | None = None) -> dict[str, Any]:
    """Hash source bytes and export only whitelisted sampling defaults."""
    source = _source(text, locator)
    if revision:
        source["revision"] = revision
    try:
        values = _sampling(json.loads(text))
    except (ValueError, TypeError):
        return {"source": source, **unavailable("invalid_generation_config")}
    return {"source": source, "status": "source_document_only",
            "settings": {k: _record(v, "generation_config_source", source) for k, v in values.items()},
            "application": "not inferred; --generation-config and runtime observations determine use"}


def parse_startup(text: str, *, locator: str) -> dict[str, Any]:
    """Extract known engine/log settings; never export the log or unknown values.

    Multiple differing observations remain unavailable. Sampling defaults retain
    chat/completion/responses scope. A log supplied by the caller is not, by itself,
    proof of PID/epoch identity; build_receipt labels that binding separately.
    """
    source = _source(text, locator)
    observations: dict[str, list[dict[str, Any]]] = {}

    def add(key, value, n):
        observations.setdefault(key, []).append(_record(value, "startup_observed", {**source, "line": n}))

    for n, line in enumerate(text.splitlines(), 1):
        version = re.search(r"vLLM API server version ([\w.+-]+)", line)
        if version:
            add("vllm_version", version.group(1), n)
        context = re.search(r"Using max model len (\d+)\b", line)
        if context:
            add("max_model_len", int(context.group(1)), n)
        chunk = re.search(r"Chunked prefill is (enabled|disabled)(?: with max_num_batched_tokens=(\d+))?", line)
        if chunk:
            add("enable_chunked_prefill", chunk.group(1) == "enabled", n)
            if chunk.group(2):
                add("max_num_batched_tokens", int(chunk.group(2)), n)
        if "Initializing a V1 LLM engine" in line:
            for raw_key, key in ENGINE_KEYS.items():
                match = re.search(r"(?:\b|,\s*)" + raw_key + r"=([^,\s)]+)", line)
                if match:
                    raw = match.group(1).strip("'\"")
                    if raw.startswith("torch."):
                        raw = raw[len("torch."):]
                    try:
                        value = _value(None if raw == "None" else raw, PARAMETERS[key],
                                       nullable=key == "quantization")
                        add(key, value, n)
                    except ValueError:
                        pass
        if "Using default " in line and " sampling params from model: " in line:
            scope = next((s for s in ("chat", "completion", "responses")
                          if "serving_" + s + ".py:" in line), None)
            if scope:
                try:
                    values = _sampling(ast.literal_eval(line.split(" sampling params from model: ", 1)[1]))
                except (ValueError, SyntaxError):
                    continue
                for key, value in values.items():
                    add(scope + "." + key, value, n)
    out = {}
    for key, values in observations.items():
        if len({json.dumps(v["value"], sort_keys=True) for v in values}) > 1:
            out[key] = {**unavailable("conflicting_startup_observations"), "observations": values}
        else:
            out[key] = values[-1]
    return {"source": source, "settings": out}


def _cpus(text: str) -> list[int]:
    cpus = set()
    for part in text.strip().split(","):
        if not re.fullmatch(r"\d+(?:-\d+)?", part):
            raise ValueError("invalid cpu list")
        low, _, high = part.partition("-")
        a, b = int(low), int(high or low)
        # Parsing resource bound, not a hardware acceptance requirement.
        if not 0 <= a <= b <= 1048576:
            raise ValueError("invalid cpu range")
        cpus.update(range(a, b + 1))
    return sorted(cpus)


def process_receipt(*, pid: int, start_ticks: int, stat: str, status: str,
                    environ: bytes = b"", cgroup: str = "", cpuset_effective: str = "",
                    cpu_max: str = "", thread_statuses: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Whitelist supplied /proc evidence. Never serialize full environment or stat.

    Caller must capture matching process identity around reads to exclude PID reuse.
    This function verifies the supplied stat identity and records sampling limits.
    """
    out: dict[str, Any] = {"pid": pid, "start_ticks": start_ticks}
    try:
        stat_pid = int(stat.split("(", 1)[0].strip())
        tail = stat[stat.rfind(")") + 2:].split()
        if stat_pid != pid or int(tail[19]) != start_ticks:
            return {**out, **unavailable("process_identity_mismatch")}
    except (ValueError, IndexError):
        return {**out, **unavailable("invalid_proc_stat")}

    def affinity(value):
        match = re.search(r"^Cpus_allowed_list:\s*(.+)$", value, re.M)
        try:
            return {"status": "observed", "cpus": _cpus(match.group(1))} if match else unavailable("affinity_absent")
        except ValueError:
            return unavailable("invalid_cpu_list")

    out.update(status="observed", affinity=affinity(status))
    out["environment"] = {}
    for entry in environ.split(b"\0"):
        key, sep, val = entry.partition(b"=")
        name = key.decode("ascii", "replace")
        if sep and name in ENVIRONMENT:
            try:
                out["environment"][name] = _value(val.decode(), str)
            except (ValueError, UnicodeError):
                out["environment"][name] = unavailable("invalid_whitelisted_value")
    out["cgroup"] = cgroup.strip() or None
    try:
        out["cpuset_effective"] = {"status": "observed", "cpus": _cpus(cpuset_effective)}
    except ValueError:
        out["cpuset_effective"] = unavailable("effective_cpuset_unavailable")
    out["cpu_max"] = cpu_max.strip() if re.fullmatch(r"(?:max|\d+)\s+\d+\s*", cpu_max) else None
    out["threads"] = {str(tid): affinity(value) for tid, value in (thread_statuses or {}).items()
                      if str(tid).isdigit()}
    out["thread_scope"] = "supplied thread snapshots only; completeness not inferred"
    return out


def retained_process_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist an existing decoded process observation (no new /proc query).

    Accepts the earlier worker22 probe shape as well as process_receipt output.
    Does not manufacture absent threads, versions, environments or CPU limits.
    """
    out = {}
    for key in ("pid", "start_ticks"):
        if type(value.get(key)) is not int or value[key] <= 0:
            return unavailable("process_identity_unavailable")
        out[key] = value[key]
    if type(value.get("ppid")) is int:
        out["ppid"] = value["ppid"]
    if value.get("status") == "unavailable":
        return {**out, **unavailable("process_identity_not_verified")}
    out["status"] = "retained_observation"
    for key, aliases in {"affinity": ("affinity",),
                         "cpuset_effective": ("cpuset_effective", "effective_cpuset")}.items():
        raw = next((value[k] for k in aliases if k in value), None)
        if isinstance(raw, Mapping):
            raw = raw.get("cpus")
        try:
            if isinstance(raw, list) and raw and all(type(i) is int and i >= 0 for i in raw):
                cpus = sorted(set(raw))
            else:
                cpus = _cpus(raw) if isinstance(raw, str) else None
            out[key] = {"status": "observed", "cpus": cpus} if cpus else unavailable("not_observed")
        except ValueError:
            out[key] = unavailable("invalid_cpu_list")
    env = value.get("environment", value.get("runtime_environment", {}))
    out["environment"] = {}
    if isinstance(env, Mapping):
        for key in ENVIRONMENT:
            if key in env:
                try:
                    out["environment"][key] = _value(env[key], str)
                except ValueError:
                    continue
    raw_max = value.get("cpu_max", "")
    out["cpu_max"] = raw_max.strip() if isinstance(raw_max, str) and re.fullmatch(r"(?:max|\d+)\s+\d+\s*", raw_max) else None
    raw_cgroup = value.get("cgroup")
    out["cgroup"] = raw_cgroup.strip() if isinstance(raw_cgroup, str) else None
    masks = value.get("all_thread_affinity_sets", value.get("thread_affinity_sets", []))
    if isinstance(value.get("threads"), Mapping):
        masks = [v.get("cpus") for v in value["threads"].values() if isinstance(v, Mapping)]
    out["observed_thread_affinity_sets"] = []
    if isinstance(masks, list):
        for mask in masks:
            if isinstance(mask, list) and mask and all(type(i) is int and i >= 0 for i in mask):
                out["observed_thread_affinity_sets"].append(sorted(set(mask)))
    out["thread_scope"] = "supplied observed masks only; completeness not inferred"
    return out


def build_receipt(*, identity: Mapping[str, Any], argv: Sequence[str],
                  argv_complete: bool = True,
                  startup_text: str = "", startup_locator: str = "unavailable",
                  startup_identity: Mapping[str, Any] | None = None,
                  source_defaults: Mapping[str, Any] | None = None,
                  generation: Mapping[str, Any] | None = None,
                  processes: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build evidence layers; no default is silently upgraded to a runtime fact.

    source_defaults/generation/processes are outputs of the pure builders above.
    Runtime startup observations are selected only for an exact, complete binding
    supplied by the caller. This does not independently authenticate that binding.
    """
    ident = {}
    for k in sorted(IDENTITY):
        if k in identity:
            try:
                ident[k] = _value(identity[k], int if k in {"server_pid", "server_process_start_ticks"} else str)
            except ValueError:
                continue
    complete = (set(ident) == IDENTITY and all(ident.values()))
    bound = bool(complete and startup_identity is not None
                 and all(startup_identity.get(k) == v for k, v in ident.items()))
    explicit = parse_explicit_argv(argv, complete=argv_complete)
    startup = parse_startup(startup_text, locator=startup_locator)
    default_types = {**PARAMETERS, **SAMPLING, **{k: str for k in VERSION_FIELDS}}
    defaults = {k: _safe_evidence(v, default_types[k]) for k, v in (source_defaults or {}).items()
                if k in default_types}
    effective = {}
    for key in PARAMETERS:
        obs = startup["settings"].get(key)
        if bound and obs:
            effective[key] = obs
        elif explicit[key].get("status") == "explicit_argv":
            effective[key] = {**explicit[key], "status": "configured_argv_not_runtime_resolved"}
        else:
            effective[key] = unavailable("no_bound_runtime_observation_or_unique_explicit_flag")
    sampling = {}
    for scope in ("chat", "completion", "responses"):
        sampling[scope] = {k: startup["settings"].get(scope + "." + k, unavailable("not_observed"))
                           if bound else unavailable("startup_identity_unbound") for k in SAMPLING}
    versions = {key: (startup["settings"].get(key) if bound else None)
                or defaults.get(key) or unavailable("runtime_version_not_observed")
                for key in VERSION_FIELDS}
    gen = unavailable("source_not_supplied")
    if generation and isinstance(generation.get("settings"), Mapping):
        gen = {"status": "source_document_only", "settings": {
            k: _safe_evidence(v, SAMPLING[k]) for k, v in generation["settings"].items() if k in SAMPLING},
            "application": "not inferred from a source document"}
    proc = {k: retained_process_receipt(v) for k, v in (processes or {}).items()
            if k in {"api", "engine"} and isinstance(v, Mapping)}
    if "api" in proc and (proc["api"].get("pid") != ident.get("server_pid")
                           or proc["api"].get("start_ticks") != ident.get("server_process_start_ticks")):
        proc["api"] = unavailable("api_process_does_not_match_receipt_identity")
    if "engine" in proc:
        proc["engine"]["api_parent_binding"] = (
            "observed_parent_matches_api" if proc["engine"].get("ppid") == ident.get("server_pid")
            and "server_pid" in ident else "unavailable_from_supplied_observation")
    return {"schema_version": SCHEMA, "identity": ident,
            "explicit_argv": explicit, "argv_capture_complete": argv_complete,
            "source_defaults": defaults,
            "startup": {**startup, "identity_binding": "caller_supplied_match" if bound else "unbound"},
            "effective_serving_settings": effective,
            "server_sampling_defaults": sampling,
            "generation_config": gen,
            "runtime_versions": versions,
            "processes": proc,
            "limitations": ["No per-request sampling settings inferred from server defaults.",
                            "Static source defaults are declarations, not resolved runtime settings.",
                            "torch CUDA build version is distinct from loaded CUDA runtime/driver versions.",
                            "KV dtype auto is a configured mode, not proof of the resolved tensor storage dtype.",
                            "CPU affinity and version evidence are descriptive, not numeric readiness gates."]}
