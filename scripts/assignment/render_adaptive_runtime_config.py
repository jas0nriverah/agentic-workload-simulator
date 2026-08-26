#!/usr/bin/env python3
"""Render one immutable adaptive holdout configuration outside the repository."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from agentic_sim.assignment.event_simulator import HardwareProfile  # noqa: E402
from scripts.assignment.adaptive_event_protocol import (  # noqa: E402
    FrozenCalibrationModel,
    verify_trajectory_prediction,
)


SCHEMA = "assignment.adaptive-runtime-config.v1"
SPLIT_SCHEMA = "assignment.event-split-manifest.v1"
CASE_SCHEMA = "assignment-steps-1-3-plan.v1"


class RenderError(ValueError):
    pass


def _fail(condition: bool, message: str) -> None:
    if not condition:
        raise RenderError(message)


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_hashed_json(path: Path, label: str) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    payload = path.read_bytes()
    digest = _sha(payload)
    candidates = list(dict.fromkeys((Path(str(path) + ".sha256"), path.with_suffix(".sha256"))))
    existing = [candidate for candidate in candidates if candidate.exists()]
    _fail(
        len(existing) == 1,
        f"{label} requires exactly one recognized SHA-256 sidecar",
    )
    sidecar = existing[0]
    _fail(
        sidecar.is_file() and not sidecar.is_symlink()
        and sidecar.read_text(encoding="utf-8") == f"{digest}  {path.name}\n",
        f"{label} or SHA-256 sidecar was tampered with",
    )
    value = json.loads(payload)
    _fail(isinstance(value, dict), f"{label} must be a JSON object")
    return value, digest


def _read_json(path: Path, label: str) -> dict[str, Any]:
    path = path.expanduser().resolve()
    _fail(path.is_file() and not path.is_symlink(), f"{label} must be a regular file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    _fail(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def _write_secure(path: Path, value: dict[str, Any]) -> str:
    payload = _canonical_bytes(value)
    digest = _sha(payload)
    sidecar = Path(str(path) + ".sha256")
    _fail(not path.exists() and not sidecar.exists(), f"refusing to overwrite frozen config: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fchmod(stream.fileno(), stat.S_IRUSR | stat.S_IWUSR)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        sidecar.write_text(f"{digest}  {path.name}\n", encoding="ascii")
        os.chmod(sidecar, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return digest


def render(args: argparse.Namespace) -> dict[str, Any]:
    case_path = args.case_spec.expanduser().resolve()
    case = _read_json(case_path, "case specification")
    _fail(case.get("schema_version") == CASE_SCHEMA, "unsupported case specification")
    resume_key = case.get("resume_key")
    _fail(isinstance(resume_key, str) and resume_key, "case resume_key is required")
    run_id = "assignment-" + hashlib.sha256(resume_key.encode("utf-8")).hexdigest()[:16]

    runtime_path = args.runtime_manifest.expanduser().resolve()
    runtime, runtime_sha = _read_hashed_json(runtime_path, "runtime manifest")
    _fail(runtime.get("schema_version") == "assignment-runtime-manifest.v1", "unsupported runtime manifest")
    repository = Path(runtime.get("repository_root", "")).expanduser().resolve()
    _fail(repository.is_dir(), "runtime manifest repository_root is unavailable")

    split_path = args.split_manifest.expanduser().resolve()
    split, split_sha = _read_hashed_json(split_path, "split manifest")
    _fail(
        set(split) == {"schema_version", "calibration_run_ids", "holdout_run_ids"}
        and split.get("schema_version") == SPLIT_SCHEMA,
        "unsupported split manifest",
    )
    calibration_ids = split.get("calibration_run_ids")
    holdout_ids = split.get("holdout_run_ids")
    _fail(isinstance(calibration_ids, list) and calibration_ids, "split calibration_run_ids are required")
    _fail(isinstance(holdout_ids, list) and run_id in holdout_ids, "case run_id is not a declared holdout")
    _fail(run_id not in calibration_ids, "case run_id overlaps calibration")

    hardware_path = args.hardware_profile.expanduser().resolve()
    hardware, hardware_sha = _read_hashed_json(hardware_path, "hardware profile")
    _fail(hardware.get("schema_version") == "assignment.hardware-profile.v1", "unsupported hardware profile")

    model_path = args.calibration_model.expanduser().resolve()
    model, model_sha = _read_hashed_json(model_path, "adaptive calibration model")
    _fail(
        model.get("schema_version") == "assignment.adaptive-calibration-model.v1"
        and model.get("provenance") == "calibration_only",
        "adaptive model is not calibration-only",
    )
    calibration_model = FrozenCalibrationModel.load(model_path)
    _fail(calibration_model.sha256 == model_sha, "adaptive calibration model hash changed during verification")
    bindings = {
        "split_manifest_sha256": split_sha,
        "runtime_manifest_sha256": runtime_sha,
        "hardware_profile_sha256": hardware_sha,
        "model_revision_sha256": _sha(str(runtime.get("model", {}).get("revision", "")).encode("utf-8")),
    }
    _fail(model.get("bindings") == bindings, "adaptive model bindings do not match the sealed inputs")
    _fail(sorted(model.get("calibration_run_ids", [])) == sorted(calibration_ids), "adaptive model calibration IDs do not match the split")
    hardware_profile = HardwareProfile.from_mapping(hardware)

    snapshot = args.tokenizer_snapshot.expanduser().resolve()
    _fail(snapshot.is_dir() and not args.tokenizer_snapshot.expanduser().is_symlink(), "tokenizer snapshot must be a directory")
    tokenizer_hashes: dict[str, str] = {}
    for name in ("tokenizer.json", "tokenizer_config.json"):
        candidate = snapshot / name
        _fail(candidate.is_file() and not candidate.is_symlink(), f"pinned tokenizer file is missing: {candidate}")
        tokenizer_hashes[name] = _sha(candidate.read_bytes())

    output = args.output.expanduser().resolve()
    protocol_root = args.protocol_root.expanduser().resolve()
    _fail(not _inside(output, repository), "adaptive runtime config must live outside the repository")
    _fail(not _inside(protocol_root, repository), "adaptive protocol root must live outside the repository")
    _fail(_inside(protocol_root, output.parent), "adaptive protocol root must stay inside the case output directory")
    _fail(not protocol_root.exists(), "adaptive protocol root must be fresh")
    e2e_prediction_path = args.e2e_prediction.expanduser().resolve()
    _fail(not _inside(e2e_prediction_path, repository), "adaptive E2E prediction must live outside the repository")
    e2e_prediction, e2e_prediction_sha = verify_trajectory_prediction(
        e2e_prediction_path,
        calibration_model,
        run_id=run_id,
        hardware=hardware_profile.to_mapping(),
    )

    revision = str(runtime["model"]["revision"])
    return {
        "schema_version": SCHEMA,
        "run_id": run_id,
        "protocol_root": str(protocol_root),
        "calibration_model_path": str(model_path),
        "split_manifest_path": str(split_path),
        "runtime_manifest_path": str(runtime_path),
        "hardware_profile_path": str(hardware_path),
        "bindings": bindings,
        "tokenizer": {
            "snapshot_path": str(snapshot),
            "revision": revision,
            "required_files_sha256": tokenizer_hashes,
        },
        "pre_trajectory_e2e": {
            "predicted_ms": float(e2e_prediction["predicted_ms"]),
            "prediction_artifact_path": str(e2e_prediction_path),
            "prediction_artifact_sha256": e2e_prediction_sha,
        },
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--case-spec", required=True, type=Path)
    result.add_argument("--runtime-manifest", required=True, type=Path)
    result.add_argument("--split-manifest", required=True, type=Path)
    result.add_argument("--hardware-profile", required=True, type=Path)
    result.add_argument("--calibration-model", required=True, type=Path)
    result.add_argument("--tokenizer-snapshot", required=True, type=Path)
    result.add_argument("--protocol-root", required=True, type=Path)
    result.add_argument(
        "--e2e-prediction",
        required=True,
        type=Path,
        help="hash-bound calibration-derived pre-trajectory E2E prediction artifact",
    )
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--validation-only", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        value = render(args)
        if args.validation_only:
            print(_canonical_bytes(value).decode("utf-8"), end="")
            return 0
        digest = _write_secure(args.output.expanduser().resolve(), value)
        print(json.dumps({"config": str(args.output.expanduser().resolve()), "sha256": digest}, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"NOT_READY: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
