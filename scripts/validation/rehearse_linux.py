#!/usr/bin/env python3
"""Free Linux rehearsal for the pinned cloud/runtime contracts.

This validator is deliberately stdlib-only.  It never downloads a model or
dataset, starts a service, pulls an image, calls a provider API, or mutates
the repository.  Missing host-only tools are reported as ``capability`` in a
normal local run and are failures under ``--strict`` (the CI mode).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "python": "3.11",
    "swe_agent": "0f3acafacabc0def8cc76b4e48acb4b6cf302cb9",
    "swe_bench": "726c5461e2ef52d83cf1ea2107870a8bb3328d57",
    "vllm": "6d8d0a24c02bfd84d46b3016b865a44f048ae84b",
    "vllm_image_digest": "sha256:05a31dc4185b042e91f4d2183689ac8a87bd845713d5c3f987563c5899878271",
    "vllm_platform": "linux/amd64",
    "lite_revision": "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
    "verified_revision": "91aa3ed51b709be6457e12d00300a6a596d4c6a3",
    "lite_rows": "300",
    "verified_rows": "500",
    "lite_first": "astropy__astropy-12907",
    "lite_first_hash": "3ca941a2f9a10a97ca2813ccb6b0406ac6209f3be34894c2eab241516fdeed61",
    "lite_gold": "astropy__astropy-14182",
    "lite_gold_hash": "f7ad14f23bd5d8419a1903d196edce904164768f94caad4670bdb9fd03d77dc5",
    "verified_gold": "astropy__astropy-14365",
    "verified_gold_hash": "c428d68361b240d5b520e96cfbc4d4145527e40b49468d63f3986c3f1a646e64",
    "lock_sha256": "7e1177bf4c0b4efe4d64895f39b340413336b77e02d2f72bbf5aad387accc9cc",
    "temperature": "0.0",
    "max_input_tokens": "32768",
    "max_output_tokens": "2048",
    "call_limit": "30",
    "seed": "0",
    "max_observation_length": "100000",
}
PIN_RE = re.compile(r"^[0-9a-f]{40}$")
SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)['\"]?(?:api[_-]?key|token|secret|password)['\"]?\s*[:=]\s*['\"]?(?!local-only-placeholder|fixture|REDACTED|do-not-print-this-token)[A-Za-z0-9_./+=-]{16,}"),
)
ABSOLUTE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])/(?:home|Users|private|tmp|var/tmp)/[^\s'\"`]+"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s'\"`]+"),
)
DEVELOPER_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])/Users/[^\s'\"`]+"),
    re.compile(r"(?<![A-Za-z0-9_])/home/(?!ubuntu(?:/|\b))[^\s'\"`]+"),
    re.compile(r"(?<![A-Za-z0-9_])/private/var/[^\s'\"`]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]Users[\\/][^\s'\"`]+"),
)
PROHIBITED_IMPORTS = re.compile(r"(?m)^\s*(?:from|import)\s+(?:vllm|torch|transformers|openai|opentelemetry|pynvml|dcgm)\b")


class CheckFailure(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command_result(
    argv: Sequence[str], *, cwd: Path | None = None, timeout: float = 30.0,
    environment: dict[str, str] | None = None,
) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(argv), cwd=str(cwd) if cwd else None, capture_output=True, text=True,
            timeout=timeout, check=False, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode, output[-4000:]


def git_value(repo: Path, *args: str) -> tuple[int, str]:
    return command_result(["git", "-C", str(repo), *args], timeout=15)


def safe_relative(path: str) -> bool:
    candidate = Path(path)
    return not candidate.is_absolute() and ".." not in candidate.parts


def check_repo_pin(repo: Path | None, expected: str, label: str) -> dict[str, Any]:
    if repo is None or not repo.exists():
        return {"status": "capability", "detail": f"{label} checkout not supplied"}
    if not (repo / ".git").exists():
        raise CheckFailure(f"{label} path is not a git checkout: {repo}")
    rc, head = git_value(repo, "rev-parse", "HEAD")
    if rc or head != expected:
        raise CheckFailure(f"{label} HEAD is not pinned: expected {expected}, got {head!r}")
    rc, branch = git_value(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    if rc == 0 or branch:
        raise CheckFailure(f"{label} checkout is not detached: {branch!r}")
    return {"status": "pass", "detail": f"detached at {expected}"}


def find_executable(name: str, root: Path | None) -> str | None:
    candidates = []
    if root:
        candidates.extend([root / ".venv/bin" / name, root / "venv/bin" / name])
    candidates.append(Path(shutil.which(name) or ""))
    for candidate in candidates:
        if str(candidate) and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def check_external_cli(repo: Path | None, revision: str, label: str, python_bin: str | None) -> dict[str, Any]:
    pin = check_repo_pin(repo, revision, label)
    if pin["status"] != "pass":
        return pin
    if label == "SWE-agent":
        executable = find_executable("sweagent", repo)
        if not executable:
            raise CheckFailure("detached SWE-agent checkout has no installed sweagent executable")
        commands = [
            [executable, "--help"],
            [executable, "run-batch", "--config", str(repo / "config/default.yaml"), "--help"],
        ]
        config_command = [executable, "--print_config"]
        config_alt = [executable, "run-batch", "--config", str(repo / "config/default.yaml"), "--print_config"]
    else:
        python = python_bin or sys.executable
        if not (repo / "swebench/harness/run_evaluation.py").is_file():
            raise CheckFailure(f"detached {label} checkout lacks the evaluator module")
        commands = [[python, "-m", "swebench.harness.run_evaluation", "--help"]]
        config_command = [python, "-m", "swebench.harness.run_evaluation", "--help"]
        config_alt = config_command
    for argv in commands:
        rc, output = command_result(argv, cwd=repo, timeout=30)
        if rc:
            raise CheckFailure(f"{label} CLI path failed ({' '.join(argv)}): {output}")
    rc, output = command_result(config_command, cwd=repo, timeout=30)
    if rc:
        rc, output = command_result(config_alt, cwd=repo, timeout=30)
    if rc:
        raise CheckFailure(f"{label} print_config/help path failed: {output}")
    return {"status": "pass", "detail": f"detached pin and {label} help/config paths passed"}


def command_tokens(value: str, key: str) -> list[str]:
    if not value:
        raise CheckFailure(f"manifest is missing {key}")
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise CheckFailure(f"{key} is not shell-tokenizable: {exc}") from exc


def option(tokens: Sequence[str], name: str) -> str | None:
    try:
        return tokens[tokens.index(name) + 1]
    except (ValueError, IndexError):
        return None


def check_command_contract(env: dict[str, str]) -> dict[str, Any]:
    control = command_tokens(env.get("SWE_AGENT_COMMAND", ""), "SWE_AGENT_COMMAND")
    thin = command_tokens(env.get("SWE_AGENT_TELEMETRY_COMMAND", ""), "SWE_AGENT_TELEMETRY_COMMAND")
    if control != thin:
        raise CheckFailure("uninstrumented and thin-telemetry SWE-agent commands differ")
    required = ["run-batch", "--config", "--instances.type", "--instances.path", "--instances.filter", "--agent.model.name", "--agent.model.api_base", "--agent.model.api_key", "--num_workers"]
    missing = [item for item in required if item not in control]
    if missing:
        raise CheckFailure(f"SWE-agent command missing required options: {missing}")
    if "--instances.split" in control:
        raise CheckFailure("unsupported invented --instances.split option is present")
    expected = {
        "--agent.model.temperature": EXPECTED["temperature"],
        "--agent.model.max_input_tokens": EXPECTED["max_input_tokens"],
        "--agent.model.max_output_tokens": EXPECTED["max_output_tokens"],
        "--agent.model.per_instance_call_limit": EXPECTED["call_limit"],
        "--agent.model.completion_kwargs.max_tokens": EXPECTED["max_output_tokens"],
        "--agent.model.completion_kwargs.seed": EXPECTED["seed"],
        "--agent.templates.max_observation_length": EXPECTED["max_observation_length"],
    }
    mismatches = {key: (option(control, key), value) for key, value in expected.items() if option(control, key) != value}
    if mismatches:
        raise CheckFailure(f"four-knob command contract mismatch: {mismatches}")
    api_key = option(control, "--agent.model.api_key")
    if api_key not in {"$VLLM_API_KEY", "${VLLM_API_KEY}"}:
        raise CheckFailure("SWE-agent command must use environment-provided $VLLM_API_KEY")
    if option(control, "--agent.model.completion_kwargs.max_tokens") != option(control, "--agent.model.max_output_tokens"):
        raise CheckFailure("completion_kwargs.max_tokens must equal the fixed max_output_tokens guard")
    path = option(control, "--instances.path")
    if not path or not Path(path).name.endswith((".json", ".jsonl")):
        raise CheckFailure("SWE-agent instances path must be a local JSON/JSONL file")
    return {"status": "pass", "detail": "control/thin command equality and explicit knob mappings passed", "knobs": expected}


def check_host(env: dict[str, str], root: Path) -> dict[str, Any]:
    system, machine = platform.system(), platform.machine().lower()
    if system != "Linux" or machine not in {"x86_64", "amd64"}:
        return {"status": "capability", "detail": f"host is {system}/{machine}; Linux x86-64 rehearsal unavailable"}
    expected_python = env.get("PYTHON_VERSION", EXPECTED["python"])
    actual_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual_python != expected_python:
        raise CheckFailure(f"Python runtime is {actual_python}, expected {expected_python}")
    lock = root / "cloud/lambda/requirements-linux-x86_64.txt"
    expected_hash = env.get("PYTHON_LOCK_SHA256", EXPECTED["lock_sha256"])
    if not lock.is_file() or sha256(lock) != expected_hash:
        raise CheckFailure("Linux Python lock is missing or hash does not match the manifest")
    return {"status": "pass", "detail": f"Linux x86-64 Python {actual_python}; lock sha256={expected_hash}"}


def check_pins(env: dict[str, str]) -> dict[str, Any]:
    revisions = {
        "SWE_AGENT_REVISION": EXPECTED["swe_agent"],
        "SWE_BENCH_REVISION": EXPECTED["swe_bench"],
        "VLLM_MODEL_REVISION": "b2cff646eb4bb1d68355c01b18ae02e7cf42d120",
        "VLLM_REVISION": EXPECTED["vllm"],
        "LITE_DATASET_REVISION": EXPECTED["lite_revision"],
        "VERIFIED_DATASET_REVISION": EXPECTED["verified_revision"],
    }
    for key, expected in revisions.items():
        actual = env.get(key, expected if key == "VLLM_REVISION" else "")
        if not PIN_RE.fullmatch(actual) or actual != expected:
            raise CheckFailure(f"{key} is unresolved or differs from the frozen pin")
    digests = {
        "VLLM_IMAGE_DIGEST": EXPECTED["vllm_image_digest"],
        "EVALUATOR_LITE_FIRST_DIGEST": "sha256:483f26c8c89a879560ed3f2e47e470343a5a0b8bf5e08d8fe3ec7eac9201df88",
        "EVALUATOR_LITE_GOLD_DIGEST": "sha256:1caa6363958e49791e9dc4c838fbfd8e8e134b7992e20e90def10072cb920c25",
        "EVALUATOR_VERIFIED_GOLD_DIGEST": "sha256:ac22529003ab4df5a84eb0e6be4b269b691c0f3b4aca582161bdfb581e1e9305",
    }
    for key, expected in digests.items():
        if env.get(key) != expected or not SHA_RE.fullmatch(env.get(key, "")):
            raise CheckFailure(f"{key} is unresolved or differs from the frozen digest")
    image = env.get("VLLM_IMAGE", "")
    if "@" not in image or image.rsplit("@", 1)[1] != env["VLLM_IMAGE_DIGEST"]:
        raise CheckFailure("VLLM_IMAGE must contain the reviewed digest, not a floating tag")
    if env.get("VLLM_IMAGE_PLATFORM") != EXPECTED["vllm_platform"]:
        raise CheckFailure("VLLM_IMAGE_PLATFORM must be linux/amd64")
    hashes = {
        "PYTHON_LOCK_SHA256": EXPECTED["lock_sha256"],
        "LITE_DATASET_SHA256": "4c6a0f689c8b4ba32f4232d611b0c9a86d2fe379e4beb85c23d7c051f3652790",
        "LITE_FIRST_DATASET_SHA256": EXPECTED["lite_first_hash"],
        "LITE_GOLD_DATASET_SHA256": EXPECTED["lite_gold_hash"],
        "VERIFIED_DATASET_SHA256": "889bccf7ada1a43d211050ac666f3b31032997209afb10dccdc6ea52128a8435",
        "VERIFIED_GOLD_DATASET_SHA256": EXPECTED["verified_gold_hash"],
    }
    for key, expected in hashes.items():
        if env.get(key) != expected or not re.fullmatch(r"[0-9a-f]{64}", env.get(key, "")):
            raise CheckFailure(f"{key} is unresolved or differs from the frozen hash")
    return {"status": "pass", "detail": "all source, model, dataset, lock, image, and platform pins are immutable"}


def check_dataset_manifest(path: Path | None, env: dict[str, str]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"status": "capability", "detail": "resolved dataset manifest not supplied; no dataset/model download attempted"}
    value = json.loads(path.read_text(encoding="utf-8"))
    expected_sections = {
        "lite": (env.get("LITE_DATASET_REPO", "SWE-bench/SWE-bench_Lite"), EXPECTED["lite_revision"], EXPECTED["lite_rows"], [(EXPECTED["lite_first"], EXPECTED["lite_first_hash"]), (EXPECTED["lite_gold"], EXPECTED["lite_gold_hash"])]),
        "verified": (env.get("VERIFIED_DATASET_REPO", "SWE-bench/SWE-bench_Verified"), EXPECTED["verified_revision"], EXPECTED["verified_rows"], [(EXPECTED["verified_gold"], EXPECTED["verified_gold_hash"])]),
    }
    for name, (repo, revision, rows, selected) in expected_sections.items():
        section = value.get(name)
        if not isinstance(section, dict) or section.get("repo") != repo or section.get("revision") != revision or str(section.get("rows")) != rows:
            raise CheckFailure(f"dataset {name} manifest repo/revision/row count is not pinned")
        selected_values = {str(item.get("instance_id")): item.get("sha256") for item in section.get("selected", [])}
        for instance_id, digest in selected:
            if selected_values.get(instance_id) != digest:
                raise CheckFailure(f"dataset {name} selected row hash mismatch for {instance_id}")
    return {"status": "pass", "detail": "Lite/Verified revisions, counts, selected IDs, and row hashes passed"}


def inspect_registry(images: Iterable[tuple[str, str]], retries: int = 3) -> list[dict[str, Any]]:
    docker = shutil.which("docker")
    output: list[dict[str, Any]] = []
    for reference, digest in images:
        if not reference:
            raise CheckFailure("registry image reference is missing")
        if not digest or not SHA_RE.fullmatch(digest):
            raise CheckFailure(f"registry image {reference} has no immutable sha256 digest")
        if not docker:
            output.append({"image": reference, "status": "capability", "detail": "docker unavailable; registry inspect not run"})
            continue
        target = reference.split("@", 1)[0] + "@" + digest
        last = ""
        for attempt in range(retries):
            rc, text = command_result([docker, "manifest", "inspect", "--verbose", target], timeout=30)
            last = text
            if rc == 0 and digest in text:
                if "linux" not in text.lower() or "amd64" not in text.lower():
                    raise CheckFailure(f"registry manifest lacks an explicit linux/amd64 platform: {target}")
                break
            time.sleep(0.25 * (attempt + 1))
        else:
            raise CheckFailure(f"registry digest/platform inspection failed after {retries} attempts for {target}: {last}")
        output.append({"image": target, "status": "pass", "detail": "digest and linux/amd64 manifest text verified"})
    return output


def safe_extract(archive: Path, destination: Path, max_file_bytes: int) -> list[str]:
    if not archive.is_file():
        raise CheckFailure(f"bundle archive is missing: {archive}")
    destination.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    try:
        handle = tarfile.open(archive, "r:*")
    except (tarfile.TarError, OSError) as exc:
        raise CheckFailure(f"cannot inspect bundle archive: {exc}") from exc
    with handle:
        for member in handle.getmembers():
            if not safe_relative(member.name) or member.issym() or member.islnk():
                raise CheckFailure(f"unsafe bundle member: {member.name}")
            if member.isfile() and member.size > max_file_bytes:
                raise CheckFailure(f"bundle member exceeds size limit: {member.name}")
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise CheckFailure(f"bundle member escapes extraction root: {member.name}") from exc
            names.append(member.name)
        handle.extractall(destination)
    return names


def scan_tree(root: Path, max_file_bytes: int, *, reject_absolute_paths: bool = True) -> dict[str, Any]:
    files = [path for path in root.rglob("*") if path.is_file() and not path.is_symlink()]
    oversized: list[str] = []
    secrets: list[str] = []
    absolute: list[str] = []
    for path in files:
        if path.stat().st_size > max_file_bytes:
            oversized.append(str(path.relative_to(root)))
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            secrets.append(str(path.relative_to(root)))
        if any(pattern.search(text) for pattern in ABSOLUTE_PATTERNS):
            absolute.append(str(path.relative_to(root)))
    if oversized or secrets or (reject_absolute_paths and absolute):
        raise CheckFailure(f"artifact scan failed: oversized={oversized}, secrets={secrets}, absolute_paths={absolute}")
    detail = "no secrets, absolute paths, or oversized files" if reject_absolute_paths else "no secrets or oversized files"
    return {"status": "pass", "detail": f"scanned {len(files)} files; {detail}"}


def check_source_hygiene(root: Path, max_file_bytes: int) -> dict[str, Any]:
    """Scan the exact tracked/release candidate tree for private paths/secrets."""
    rc, output = git_value(root, "ls-files", "-co", "--exclude-standard", "-z")
    if rc:
        raise CheckFailure(f"could not enumerate release files: {output}")
    names = [name for name in output.split("\0") if name]
    oversized: list[str] = []
    secrets: list[str] = []
    private_paths: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        if path.stat().st_size > max_file_bytes:
            oversized.append(name)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            secrets.append(name)
        # This validator necessarily contains the path signatures it detects;
        # scanning its own regex literals would be a false positive.
        if name != "scripts/validation/rehearse_linux.py" and any(pattern.search(text) for pattern in DEVELOPER_PATH_PATTERNS):
            private_paths.append(name)
    if oversized or secrets or private_paths:
        raise CheckFailure(f"release source scan failed: oversized={oversized}, secrets={secrets}, private_paths={private_paths}")
    return {"status": "pass", "detail": f"scanned {len(names)} tracked/release files for secrets, private paths, and oversized files"}


def inventory_trajectory(root: Path | None, max_file_bytes: int) -> dict[str, Any]:
    if root is None or not root.exists():
        return {"status": "capability", "detail": "first-trajectory artifact root not supplied"}
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    if not files:
        raise CheckFailure("trajectory inventory root is empty")
    records = []
    for path in files:
        if path.stat().st_size > max_file_bytes:
            raise CheckFailure(f"trajectory file exceeds size limit: {path}")
        suffix = path.suffix.lower()
        if suffix in {".json", ".jsonl", ".traj"}:
            if suffix == ".json":
                json.loads(path.read_text(encoding="utf-8"))
            else:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        json.loads(line)
        records.append({"path": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": sha256(path)})
    # SWE-agent's own configs/logs may legitimately contain the target host's
    # absolute paths. The inventory contract requires a secret scan and byte
    # preservation; private-path hygiene is enforced on the release source.
    scan_tree(root, max_file_bytes, reject_absolute_paths=False)
    return {"status": "pass", "detail": f"inventoried {len(records)} files without rewriting bytes", "files": records}


def run_cloud_dry_runs(root: Path, manifest: Path, scratch: Path) -> dict[str, Any]:
    scripts = sorted((root / "scripts/cloud").glob("*.sh"))
    failures: dict[str, str] = {}
    commands: list[list[str]] = []
    archive = scratch / "dry-bundle.tar.gz"
    content = scratch / "bundle-content.txt"
    content.write_text("rehearsal\n", encoding="utf-8")
    collection = scratch / "collection"
    collection.mkdir()
    (collection / "artifact.txt").write_text("rehearsal\n", encoding="utf-8")
    digest = sha256(collection / "artifact.txt")
    (collection / "SHA256SUMS").write_text(f"{digest}  artifact.txt\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(collection, arcname=".collection")
    archive_digest = sha256(archive)
    external = archive.with_suffix(archive.suffix + ".sha256")
    external.write_text(f"{archive_digest}  {archive.name}\n", encoding="utf-8")
    for script in scripts:
        name = script.name
        args = ["bash", str(script), "--dry-run"]
        if name in {"lambda_bootstrap.sh", "lambda_download_assets.sh", "lambda_healthcheck.sh", "lambda_run_first_experiment.sh", "lambda_start_vllm.sh"}:
            args += ["--manifest", str(manifest)]
        elif name == "lambda_run_gold_smoke.sh":
            # The gold wrapper validates real selected rows before printing its
            # plan.  Without a resolved datasets.json, invoke its intentional
            # no-assets dry-run path; this is a capability result, not a fake
            # dataset validation.
            dataset_manifest = os.environ.get("REHEARSAL_DATASET_MANIFEST", "")
            if dataset_manifest and Path(dataset_manifest).is_file():
                args += ["--manifest", str(manifest)]
        elif name == "verify_lambda_archive_local.sh":
            args += ["--archive", str(archive), "--manifest", str(external)]
        elif name == "lambda_collect_results.sh":
            args += ["--source-root", str(scratch), "--output-dir", str(scratch / "out")]
        commands.append(args)
        rc, output = command_result(args, cwd=root, timeout=45)
        if rc:
            failures[name] = output
    if failures:
        raise CheckFailure(f"cloud dry-runs failed: {failures}")
    return {"status": "pass", "detail": f"{len(scripts)} cloud command dry-runs passed", "commands": commands}


def check_static(root: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    environment = os.environ.copy()
    source_path = str(root / "src")
    environment["PYTHONPATH"] = source_path + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    test_commands = [
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        # tests/cloud intentionally remains a flat test directory; discover
        # it explicitly so Linux CI cannot silently omit release/collection
        # contracts merely because it has no __init__.py.
        [sys.executable, "-m", "unittest", "discover", "-s", "tests/cloud", "-p", "test*.py"],
    ]
    for test_command in test_commands:
        rc, output = command_result(test_command, cwd=root, timeout=180, environment=environment)
        if rc:
            raise CheckFailure(f"unit tests failed ({' '.join(test_command)}): {output}")
    checks["unit"] = "pass (core + tests/cloud discovery)"
    py_files = sorted([*root.joinpath("src").rglob("*.py"), *root.joinpath("scripts").rglob("*.py")])
    for path in py_files:
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            raise CheckFailure(f"compile check failed for {path}: {exc}") from exc
    checks["compile"] = f"pass ({len(py_files)} files)"
    ruff = shutil.which("ruff")
    if ruff:
        rc, output = command_result([ruff, "check", "src", "scripts", "tests"], cwd=root, timeout=120)
        if rc:
            raise CheckFailure(f"Ruff failed: {output}")
        checks["ruff"] = "pass"
    else:
        checks["ruff"] = "capability: ruff unavailable"
    shellcheck = shutil.which("shellcheck")
    shell_files = sorted([*(root / "scripts/cloud").glob("*.sh"), *(root / "scripts/observability").glob("*.sh")])
    if shellcheck:
        rc, output = command_result([shellcheck, *map(str, shell_files)], cwd=root, timeout=120)
        if rc:
            raise CheckFailure(f"ShellCheck failed: {output}")
        checks["shellcheck"] = "pass"
    else:
        checks["shellcheck"] = "capability: shellcheck unavailable"
    rc, output = command_result(["git", "diff", "--check"], cwd=root, timeout=30)
    if rc:
        raise CheckFailure(f"git diff --check failed: {output}")
    checks["diff"] = "pass"
    return {"status": "pass", "detail": checks}


def check_prohibited_dependencies(root: Path) -> dict[str, Any]:
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*(?:vllm|torch|transformers|openai|opentelemetry|pynvml|dcgm)\b", pyproject):
        raise CheckFailure("prohibited runtime dependency appears in pyproject.toml")
    lock = root / "cloud/lambda/requirements-linux-x86_64.txt"
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("--hash"):
            continue
        if line.startswith(("git+", "http://", "https://")) or (" @ " in line):
            raise CheckFailure(f"prohibited VCS/URL dependency in lock: {line}")
    for path in [*root.joinpath("src").rglob("*.py"), *root.joinpath("scripts").rglob("*.py")]:
        text = path.read_text(encoding="utf-8")
        if PROHIBITED_IMPORTS.search(text):
            raise CheckFailure(f"prohibited runtime import in {path}")
    return {"status": "pass", "detail": "no prohibited runtime dependency/import or VCS requirement"}


def parse_images(env: dict[str, str]) -> list[tuple[str, str]]:
    result = []
    for ref_key, digest_key in (("VLLM_IMAGE", "VLLM_IMAGE_DIGEST"), ("EVALUATOR_LITE_FIRST_IMAGE", "EVALUATOR_LITE_FIRST_DIGEST"), ("EVALUATOR_LITE_GOLD_IMAGE", "EVALUATOR_LITE_GOLD_DIGEST"), ("EVALUATOR_VERIFIED_GOLD_IMAGE", "EVALUATOR_VERIFIED_GOLD_DIGEST")):
        reference, digest = env.get(ref_key, ""), env.get(digest_key, "")
        if not reference or not digest:
            raise CheckFailure(f"{ref_key}/{digest_key} is unresolved")
        if "@" in reference and reference.rsplit("@", 1)[1] != digest:
            raise CheckFailure(f"{ref_key} digest does not match {digest_key}")
        result.append((reference, digest))
    return result


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.resolve()
    env = load_env(args.manifest)
    checks: dict[str, Any] = {}
    agent_root = args.swe_agent_root.resolve() if args.swe_agent_root else None
    bench_root = args.swe_bench_root.resolve() if args.swe_bench_root else None

    def evaluate(name: str, function: Any) -> None:
        try:
            checks[name] = function()
        except CheckFailure as exc:
            checks[name] = {"status": "fail", "detail": str(exc)}

    evaluate("host_runtime", lambda: check_host(env, root))
    evaluate("pins", lambda: check_pins(env))
    evaluate("command_contract", lambda: check_command_contract(env))
    evaluate("swe_agent", lambda: check_external_cli(agent_root, env.get("SWE_AGENT_REVISION", EXPECTED["swe_agent"]), "SWE-agent", args.python_bin))
    evaluate("swe_bench", lambda: check_external_cli(bench_root, env.get("SWE_BENCH_REVISION", EXPECTED["swe_bench"]), "SWE-bench", args.python_bin))
    evaluate("datasets", lambda: check_dataset_manifest(args.dataset_manifest, env))
    evaluate("static", lambda: check_static(root))
    evaluate("source_hygiene", lambda: check_source_hygiene(root, args.max_file_bytes))
    evaluate("dependencies", lambda: check_prohibited_dependencies(root))
    evaluate("registry", lambda: inspect_registry(parse_images(env)))
    with tempfile.TemporaryDirectory(prefix="agentic-rehearsal-") as temp:
        scratch = Path(temp)
        evaluate("cloud_dry_runs", lambda: run_cloud_dry_runs(root, args.manifest, scratch))
        if args.bundle:
            def bundle_check() -> dict[str, Any]:
                extraction = scratch / "bundle"
                names = safe_extract(args.bundle.resolve(), extraction, args.max_file_bytes)
                scan_tree(extraction, args.max_file_bytes)
                return {"status": "pass", "detail": f"clean extraction and scan of {len(names)} members"}
            evaluate("bundle", bundle_check)
        else:
            checks["bundle"] = {"status": "capability", "detail": "bundle archive not supplied"}
        if args.trajectory_root:
            evaluate("trajectory", lambda: inventory_trajectory(args.trajectory_root.resolve(), args.max_file_bytes))
        else:
            checks["trajectory"] = {"status": "capability", "detail": "trajectory root not supplied"}
    checks["capabilities"] = {"status": "pass", "detail": {tool: bool(shutil.which(tool)) for tool in ("docker", "ruff", "shellcheck", "nvidia-smi", "zstd")}}
    return {"schema_version": "linux-rehearsal.v1", "strict": args.strict, "root": str(root), "model_download_attempted": False, "checks": checks}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--manifest", type=Path, default=ROOT / "cloud/lambda/instance_manifest.env.example")
    parser.add_argument("--python-bin", help="pinned external-environment Python for SWE-bench help checks")
    parser.add_argument("--swe-agent-root", type=Path)
    parser.add_argument("--swe-bench-root", type=Path)
    parser.add_argument("--dataset-manifest", type=Path)
    parser.add_argument("--trajectory-root", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--max-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--strict", action="store_true", help="turn capability-only missing host tools/artifacts into failures")
    parser.add_argument("--report", type=Path, help="optional JSON report path")
    args = parser.parse_args(argv)
    try:
        report = run(args)
        failures: list[str] = []
        required_capabilities = {"host_runtime", "swe_agent", "swe_bench", "registry"}
        for name, value in report["checks"].items():
            values = value if isinstance(value, list) else [value]
            if any(isinstance(item, dict) and item.get("status") == "fail" for item in values):
                failures.append(name)
            if args.strict and name in required_capabilities and any(isinstance(item, dict) and item.get("status") == "capability" for item in values):
                failures.append(f"{name}:capability")
        report["status"] = "fail" if failures else "pass"
        report["failures"] = failures
    except CheckFailure as exc:
        report = {"schema_version": "linux-rehearsal.v1", "strict": args.strict, "status": "fail", "failures": [str(exc)], "model_download_attempted": False}
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
