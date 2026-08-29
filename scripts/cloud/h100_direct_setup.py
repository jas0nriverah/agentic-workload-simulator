#!/usr/bin/env python3
"""Prepare and verify the pinned non-Docker H100 model runtime.

This helper is deliberately setup-only.  It installs no packages and starts
no process by itself; the shell entrypoint owns the Python installation.  It
only downloads the declared Hugging Face revision when the external cache
does not already contain a snapshot whose remote file metadata and local
hashes agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


class DirectSetupError(RuntimeError):
    """The pinned direct-runtime model snapshot is not safe to use."""


RUNTIME_REQUIREMENTS = {
    "vllm": ("0.10.0", "0.10.0"),
    "torch": ("2.7.1", "2.7.1"),
    "transformers": ("4.57.6", "5"),
    "tokenizers": ("0.22.2", "0.23"),
    "huggingface-hub": ("0.34.4", "1"),
}


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def _verify_runtime() -> dict[str, str]:
    from importlib import metadata

    actual = {}
    for package, (minimum, maximum) in RUNTIME_REQUIREMENTS.items():
        try:
            actual[package] = metadata.version(package)
        except metadata.PackageNotFoundError as exc:
            raise DirectSetupError(f"required runtime package is missing: {package}") from exc
        if not (_version_tuple(minimum) <= _version_tuple(actual[package]) < _version_tuple(maximum)):
            raise DirectSetupError(
                f"runtime package mismatch: {package} requires >= {minimum}, < {maximum}, "
                f"got {actual[package]}"
            )
    return actual


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_path(model_cache: Path, model: str, revision: str) -> Path:
    repo_dir = f"models--{model.replace('/', '--')}"
    snapshot = (model_cache / "hub" / repo_dir / "snapshots" / revision).resolve()
    cache_root = model_cache.resolve()
    try:
        snapshot.relative_to(cache_root)
    except ValueError as exc:
        raise DirectSetupError("model snapshot must remain inside MODEL_CACHE") from exc
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision.lower()):
        raise DirectSetupError("model revision must be a 40-hex immutable commit")
    return snapshot


def _remote_siblings(model: str, revision: str) -> list[Any]:
    try:
        from huggingface_hub import HfApi

        info = HfApi().model_info(repo_id=model, revision=revision, files_metadata=True)
    except Exception as exc:  # pragma: no cover - provider/network failure varies
        raise DirectSetupError(
            f"could not obtain pinned Hugging Face file metadata for {model}@{revision}"
        ) from exc
    siblings = getattr(info, "siblings", None)
    if not siblings:
        raise DirectSetupError("pinned Hugging Face revision returned no file metadata")
    return list(siblings)


def _sibling_value(sibling: Any, name: str) -> Any:
    if isinstance(sibling, Mapping):
        return sibling.get(name)
    return getattr(sibling, name, None)


def _lfs_sha256(sibling: Any) -> str | None:
    lfs = _sibling_value(sibling, "lfs")
    if isinstance(lfs, Mapping):
        value = lfs.get("sha256")
    else:
        value = getattr(lfs, "sha256", None)
    if value is None:
        return None
    value = str(value).lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise DirectSetupError("remote model metadata contains an invalid file hash")
    return value


def _safe_relative_file(snapshot: Path, relative_name: str) -> Path:
    relative = PurePosixPath(relative_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise DirectSetupError(f"remote model file escapes the snapshot: {relative_name}")
    path = snapshot / Path(*relative.parts)
    try:
        path.relative_to(snapshot)
    except ValueError as exc:
        raise DirectSetupError(f"remote model file escapes the snapshot: {relative_name}") from exc
    # Hugging Face snapshots use symlinks into the sibling ``blobs`` directory.
    # Permit those links only when their resolved target remains inside this
    # model's cache entry; an arbitrary link outside it is not a model file.
    cache_entry = snapshot.parent.parent.resolve()
    try:
        path.resolve().relative_to(cache_entry)
    except ValueError as exc:
        raise DirectSetupError(f"remote model file escapes the model cache: {relative_name}") from exc
    return path


def verify_snapshot(snapshot: Path, siblings: Iterable[Any]) -> dict[str, Any]:
    """Verify the local snapshot against remote sizes and available LFS hashes."""

    snapshot = snapshot.resolve()
    if not snapshot.is_dir():
        raise DirectSetupError(f"pinned model snapshot is missing: {snapshot}")
    local_hashes: dict[str, str] = {}
    hash_checked = 0
    sibling_count = 0
    for sibling in siblings:
        relative_name = str(_sibling_value(sibling, "rfilename") or "")
        if not relative_name:
            raise DirectSetupError("remote model metadata contains a file without rfilename")
        sibling_count += 1
        path = _safe_relative_file(snapshot, relative_name)
        if not path.is_file():
            raise DirectSetupError(f"pinned model file is missing: {path}")
        remote_size = _sibling_value(sibling, "size")
        if remote_size is not None and path.stat().st_size != int(remote_size):
            raise DirectSetupError(f"pinned model file size mismatch: {relative_name}")
        local_hash = _sha256(path)
        local_hashes[relative_name] = local_hash
        remote_hash = _lfs_sha256(sibling)
        if remote_hash is not None:
            hash_checked += 1
            if local_hash != remote_hash:
                raise DirectSetupError(f"pinned model file hash mismatch: {relative_name}")

    required = ("config.json", "tokenizer.json")
    for name in required:
        if name not in local_hashes:
            raise DirectSetupError(f"pinned tokenizer/model metadata is missing: {snapshot / name}")
    if not any(name.endswith(".safetensors") or name.endswith(".safetensors.index.json") for name in local_hashes):
        raise DirectSetupError("pinned model snapshot has no safetensors weights or index")
    canonical = json.dumps(local_hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "file_count": sibling_count,
        "hash_checked_file_count": hash_checked,
        "files_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _verify_tokenizer(snapshot: Path, revision: str) -> dict[str, Any]:
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            str(snapshot),
            local_files_only=True,
            revision=revision,
            trust_remote_code=False,
            use_fast=True,
        )
        if not hasattr(tokenizer, "all_special_tokens_extended"):
            raise DirectSetupError(
                "Transformers tokenizer is incompatible with vLLM 0.10.0: "
                "all_special_tokens_extended is unavailable"
            )
    except Exception as exc:  # pragma: no cover - tokenizer backend errors vary
        if isinstance(exc, DirectSetupError):
            raise
        raise DirectSetupError("pinned tokenizer could not be loaded from the local snapshot") from exc
    return {
        "loaded": True,
        "class": type(tokenizer).__name__,
        "revision": revision,
        "source": str(snapshot),
    }


def ensure_snapshot(
    *,
    model: str,
    revision: str,
    model_cache: Path,
    expected_snapshot: Path,
    state_output: Path | None = None,
) -> dict[str, Any]:
    """Reuse a verified snapshot or download only the exact pinned revision."""

    runtime = _verify_runtime()
    model_cache = model_cache.expanduser().resolve()
    expected = expected_snapshot.expanduser().resolve()
    computed = _snapshot_path(model_cache, model, revision)
    if expected != computed:
        raise DirectSetupError(
            f"MODEL_SNAPSHOT must be the exact revision path {computed}; got {expected}"
        )
    siblings = _remote_siblings(model, revision)
    reused = False
    try:
        verification = verify_snapshot(expected, siblings)
        reused = True
    except DirectSetupError:
        try:
            from huggingface_hub import snapshot_download

            resolved = Path(
                snapshot_download(
                    repo_id=model,
                    revision=revision,
                    cache_dir=str(model_cache / "hub"),
                    local_files_only=False,
                )
            ).resolve()
        except Exception as exc:  # pragma: no cover - provider/network failure varies
            raise DirectSetupError(
                f"could not download pinned Hugging Face revision {model}@{revision}"
            ) from exc
        if resolved != expected:
            raise DirectSetupError(f"Hugging Face resolved an unexpected snapshot path: {resolved}")
        verification = verify_snapshot(expected, siblings)

    tokenizer = _verify_tokenizer(expected, revision)
    result: dict[str, Any] = {
        "schema_version": "h100-direct-model.v1",
        "provenance": "setup",
        "model": model,
        "revision": revision,
        "model_cache": str(model_cache),
        "model_snapshot": str(expected),
        "reused_existing_snapshot": reused,
        "verification": verification,
        "tokenizer": tokenizer,
        "runtime": runtime,
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if state_output is not None:
        state_output = state_output.expanduser().resolve()
        state_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_output.with_name(f".{state_output.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(state_output)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--model-cache", required=True, type=Path)
    parser.add_argument("--model-snapshot", required=True, type=Path)
    parser.add_argument("--state-output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = ensure_snapshot(
            model=args.model,
            revision=args.revision.lower(),
            model_cache=args.model_cache,
            expected_snapshot=args.model_snapshot,
            state_output=args.state_output,
        )
    except DirectSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "Direct model ready: "
        f"model={result['model']} revision={result['revision']} "
        f"files={result['verification']['file_count']} "
        f"hash_checked={result['verification']['hash_checked_file_count']} "
        f"reused={str(result['reused_existing_snapshot']).lower()}"
    )
    print(f"Tokenizer ready: class={result['tokenizer']['class']} revision={result['tokenizer']['revision']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
