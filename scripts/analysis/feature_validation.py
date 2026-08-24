#!/usr/bin/env python3
"""Seal, fit, and score the H100 feature-only latency validation protocol.

The GPU entrypoint owns request collection.  This small standard-library
utility owns the offline artifact boundary around it:

* ``seal`` creates the immutable protocol/split/feature manifest before a
  request is launched;
* ``fit`` reads calibration row labels only and freezes predictions for every
  holdout before any holdout label is opened;
* ``score`` is deliberately a later operation and joins holdout labels only
  after the prediction/reveal receipt exists.

The implementation never silently drops a row.  Missing or unavailable rows
are reported explicitly and cause a non-zero exit for the required 100%
coverage protocol.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "configs" / "h100_final_validation.json"
DEFAULT_ARTIFACT_ROOT = ROOT / "artifacts" / "h100_final_validation"
ROW_SCHEMA = "h100-final-row.v1"


class ValidationError(ValueError):
    """A protocol or artifact contract violation."""


def _family(protocol: Mapping[str, Any]) -> str:
    return str(protocol["hardware"]["gpu_family"]).lower()


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON {path}: {exc}") from exc


def _dump(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_create(path: Path, payload: bytes) -> None:
    """Create once; never overwrite a sealed artifact."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValidationError(f"refusing to overwrite immutable artifact: {path}")
        return
    temporary = path.with_name(path.name + f".tmp.{os_getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def os_getpid() -> int:
    # Kept as a function to make the atomic-write helper easy to exercise and
    # avoid importing an otherwise broad process-management module.
    import os

    return os.getpid()


def load_protocol(config_path: Path) -> tuple[dict[str, Any], str, str]:
    protocol = _json(config_path)
    hardware = protocol.get("hardware", {})
    family = str(hardware.get("gpu_family", "")).lower()
    expected_schema = f"{family}-final-validation.v1"
    if not family or protocol.get("schema_version") != expected_schema:
        raise ValidationError("unsupported final-validation protocol schema")
    if protocol.get("launch_authorized") is not False:
        raise ValidationError("protocol launch_authorized must remain false in Git")
    names = " ".join(map(str, hardware.get("gpu_name_allowlist", [])))
    if str(hardware.get("gpu_family", "")).upper() not in names.upper():
        raise ValidationError("protocol hardware family is not represented in the GPU allowlist")
    calibration = protocol.get("calibration_configs")
    holdouts = protocol.get("sealed_holdouts")
    if not isinstance(calibration, list) or not 20 <= len(calibration) <= 30:
        raise ValidationError("calibration count must be 20..30")
    if not isinstance(holdouts, list) or not 10 <= len(holdouts) <= 12:
        raise ValidationError("holdout count must be 10..12")
    rows = calibration + holdouts
    ids = [row.get("case_id") for row in rows]
    if any(not isinstance(case_id, str) or not case_id for case_id in ids):
        raise ValidationError("all case IDs must be non-empty strings")
    if len(ids) != len(set(ids)):
        raise ValidationError("calibration and holdout case IDs must be unique")
    if any(row.get("split") != "calibration" for row in calibration):
        raise ValidationError("calibration rows have an invalid split")
    if any(row.get("split") != "sealed_holdout" for row in holdouts):
        raise ValidationError("holdout rows have an invalid split")
    request = protocol.get("request_protocol", {})
    if (
        request.get("concurrency"),
        request.get("warmup_requests"),
        request.get("measured_repetitions_per_case"),
        request.get("repetition_ids"),
    ) != (1, 2, 3, ["r01", "r02", "r03"]):
        raise ValidationError("request protocol must be serialized, 2 warmups, and 3 repeats")
    features = protocol.get("features", [])
    required_names = {
        "prompt_tokens",
        "max_output_tokens",
        "context_tokens",
        "tool_calls",
        "hardware_score",
        "prompt_output_interaction",
        "concurrency",
        "warm_state",
    }
    declared_names = {item.get("name") for item in features if isinstance(item, Mapping)}
    if not required_names.issubset(declared_names):
        raise ValidationError("protocol is missing implementation-aligned feature definitions")
    split_payload = {
        "protocol_id": protocol["protocol_id"],
        "calibration_configs": calibration,
        "sealed_holdouts": holdouts,
    }
    split_hash = _sha_bytes(
        json.dumps(split_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return protocol, sha256_file(config_path), split_hash


def feature_for_case(protocol: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    """Build only pre-execution features from a declared case."""

    if not {"input_tokens", "output_tokens", "case_id"}.issubset(case):
        raise ValidationError("case lacks declared input/output targets")
    context = int(case.get("context_tokens", 0))
    tools = int(case.get("tool_calls", 0))
    return {
        "schema_version": "simulator.feature-input.v1",
        "run_id": str(case["case_id"]),
        "prompt_tokens": int(case["input_tokens"]),
        "max_output_tokens": int(case["output_tokens"]),
        "context_tokens": context,
        "tool_calls": tools,
        "hardware_score": 1.0,
    }


def seal(config_path: Path, artifact_root: Path) -> dict[str, Any]:
    protocol, protocol_hash, split_hash = load_protocol(config_path)
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not (artifact_root / "split_manifest.json").exists():
        holdout_root = artifact_root / "holdout"
        if holdout_root.exists() and any(holdout_root.rglob("row.json")):
            raise ValidationError("cannot seal after holdout measurements already exist")
    config_copy = artifact_root / "protocol.config.json"
    _atomic_create(config_copy, config_path.read_bytes())
    _atomic_create(
        artifact_root / "protocol.sha256",
        f"{protocol_hash}  protocol.config.json\n".encode("utf-8"),
    )
    split = {
        "schema_version": f"{_family(protocol)}-final-split.v1",
        "protocol_id": protocol["protocol_id"],
        "protocol_sha256": protocol_hash,
        "split_sha256": split_hash,
        "sealed": True,
        "sealed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "calibration_case_ids": [row["case_id"] for row in protocol["calibration_configs"]],
        "sealed_holdout_case_ids": [row["case_id"] for row in protocol["sealed_holdouts"]],
    }
    split_path = artifact_root / "split_manifest.json"
    if split_path.exists():
        existing = _json(split_path)
        if existing.get("protocol_sha256") != protocol_hash or existing.get("split_sha256") != split_hash:
            raise ValidationError("existing split manifest does not match the sealed protocol")
    else:
        _atomic_create(split_path, _dump(split))
    feature_manifest = {
        "schema_version": f"{_family(protocol)}-feature-manifest.v1",
        "protocol_sha256": protocol_hash,
        "split_manifest_sha256": split_hash,
        "features_are_pre_execution_only": True,
        "calibration": [feature_for_case(protocol, row) for row in protocol["calibration_configs"]],
        "sealed_holdout": [feature_for_case(protocol, row) for row in protocol["sealed_holdouts"]],
    }
    _atomic_create(artifact_root / "feature_manifest.json", _dump(feature_manifest))
    return {"protocol_sha256": protocol_hash, "split_manifest_sha256": split_hash}


def _row_path(artifact_root: Path, split: str, case_id: str, repeat_id: str) -> Path:
    folder = "calibration" if split == "calibration" else "holdout"
    return artifact_root / folder / case_id / repeat_id / "row.json"


def _successful_wall_seconds(path: Path, row_schema: str = ROW_SCHEMA) -> float:
    row = _json(path)
    if row.get("schema_version") not in {None, row_schema}:
        raise ValidationError(f"unsupported row schema in {path}")
    if row.get("status") != "completed":
        raise ValidationError(f"row is unavailable, not a fit label: {path}")
    value = row.get("wall_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = row.get("observed_seconds")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"completed row lacks wall_ms: {path}")
        seconds = float(value)
    else:
        seconds = float(value) / 1000.0
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValidationError(f"completed row has invalid wall time: {path}")
    return seconds


def _case_times(
    artifact_root: Path,
    split: str,
    case: Mapping[str, Any],
    row_schema: str = ROW_SCHEMA,
) -> list[float]:
    values: list[float] = []
    for repeat_id in ("r01", "r02", "r03"):
        path = _row_path(artifact_root, split, str(case["case_id"]), repeat_id)
        if not path.is_file():
            raise ValidationError(f"missing {split} row: {path}")
        values.append(_successful_wall_seconds(path, row_schema))
    return values


def fit(config_path: Path, artifact_root: Path) -> dict[str, Any]:
    protocol, protocol_hash, split_hash = load_protocol(config_path)
    split_path = artifact_root / "split_manifest.json"
    if not split_path.is_file() or _json(split_path).get("split_sha256") != split_hash:
        raise ValidationError("run seal before fitting calibration rows")
    holdout_root = artifact_root / "holdout"
    if holdout_root.exists() and any(holdout_root.rglob("row.json")):
        raise ValidationError("holdout rows exist before prediction freeze")
    from agentic_sim.feature_simulator import FeatureCalibrationRecord, FeatureInput, FeatureLatencySimulator

    records: list[FeatureCalibrationRecord] = []
    fit_rows: list[dict[str, Any]] = []
    row_schema = f"{protocol['hardware']['gpu_family'].lower()}-final-row.v1"
    for case in protocol["calibration_configs"]:
        times = _case_times(artifact_root, "calibration", case, row_schema)
        median_seconds = statistics.median(times)
        features = FeatureInput.from_mapping(feature_for_case(protocol, case))
        records.append(
            FeatureCalibrationRecord(features=features, observed_seconds=median_seconds)
        )
        fit_rows.append(
            {
                "case_id": case["case_id"],
                "repeat_seconds": times,
                "median_seconds": median_seconds,
            }
        )
    model = FeatureLatencySimulator.fit(records)
    predictions = []
    for case in protocol["sealed_holdouts"]:
        features = FeatureInput.from_mapping(feature_for_case(protocol, case))
        prediction = model.predict(features)
        predictions.append(
            {
                "case_id": case["case_id"],
                "holdout_kind": case["holdout_kind"],
                "features": prediction["features"],
                "predicted_seconds": prediction["predicted_seconds"],
                "provenance": "predicted",
            }
        )
    derived = artifact_root / "derived"
    derived.mkdir(parents=True, exist_ok=True)
    fit_input_hash = _sha_bytes(_dump(fit_rows))
    model_obj = {
        "schema_version": "simulator.feature-model.v1",
        "protocol_sha256": protocol_hash,
        "split_manifest_sha256": split_hash,
        "fit_input_sha256": fit_input_hash,
        "model": model.to_mapping(),
        "calibration_case_medians": fit_rows,
    }
    _atomic_create(derived / "feature_model.json", _dump(model_obj))
    prediction_obj = {
        "schema_version": f"{_family(protocol)}-feature-predictions.v1",
        "protocol_sha256": protocol_hash,
        "split_manifest_sha256": split_hash,
        "fit_input_sha256": fit_input_hash,
        "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "features_are_pre_execution_only": True,
        "predictions": predictions,
    }
    prediction_path = derived / "prediction_manifest.json"
    _atomic_create(prediction_path, _dump(prediction_obj))
    _atomic_create(
        derived / "prediction_manifest.sha256",
        f"{sha256_file(prediction_path)}  prediction_manifest.json\n".encode("utf-8"),
    )
    return {"prediction_manifest": str(prediction_path), "prediction_sha256": sha256_file(prediction_path)}


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    rank = (len(values) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (rank - lower)


def _metrics(predicted: list[float], measured: list[float]) -> dict[str, float]:
    if len(predicted) != len(measured) or not predicted:
        raise ValidationError("metric vectors are empty or have different lengths")
    errors = [abs(p - m) for p, m in zip(predicted, measured)]
    ape = [100.0 * error / m for error, m in zip(errors, measured) if m > 0]
    return {
        "mape_percent": statistics.mean(ape),
        "mae_seconds": statistics.mean(errors),
        "rmse_seconds": math.sqrt(statistics.mean([error * error for error in errors])),
        "median_ape_percent": statistics.median(ape),
        "p95_ape_percent": _percentile(ape, 0.95),
        "max_ape_percent": max(ape),
        "mean_predicted_seconds": statistics.mean(predicted),
        "mean_measured_seconds": statistics.mean(measured),
    }


def score(config_path: Path, artifact_root: Path) -> dict[str, Any]:
    protocol, protocol_hash, split_hash = load_protocol(config_path)
    prediction_path = artifact_root / "derived" / "prediction_manifest.json"
    receipt_path = artifact_root / "holdout_reveal_receipt.json"
    if not prediction_path.is_file() or not receipt_path.is_file():
        raise ValidationError("holdout scoring requires prediction manifest and reveal receipt")
    prediction = _json(prediction_path)
    receipt = _json(receipt_path)
    if prediction.get("protocol_sha256") != protocol_hash or prediction.get("split_manifest_sha256") != split_hash:
        raise ValidationError("prediction manifest does not match the sealed protocol")
    if receipt.get("protocol_sha256") != protocol_hash or receipt.get("split_manifest_sha256") != split_hash:
        raise ValidationError("reveal receipt does not match the sealed protocol")
    if receipt.get("prediction_manifest_sha256") != sha256_file(prediction_path):
        raise ValidationError("reveal receipt does not hash the prediction manifest")
    forbidden = {
        "wall_ms",
        "cpu_activity_union_ms",
        "cuda_activity_union_ms",
        "kernel_duration_sum_ms",
        "kineto_wall_ms",
        "kineto_cpu_ms",
        "kineto_cuda_ms",
        "kineto_duration_ms",
        "measured_wall_time_ms",
        "measured_cpu_time_ms",
        "measured_cuda_time_ms",
        "measured_kineto_time_ms",
        "gpu_seconds_at_reference",
        "actual_prompt_tokens",
        "actual_completion_tokens",
        "observed_seconds",
    }

    def check_prediction(value: Any) -> None:
        if isinstance(value, Mapping):
            if forbidden.intersection(value):
                raise ValidationError("prediction manifest contains a measured label")
            for child in value.values():
                check_prediction(child)
        elif isinstance(value, list):
            for child in value:
                check_prediction(child)

    check_prediction(prediction)
    if not prediction_path.stat().st_mtime < receipt_path.stat().st_mtime:
        raise ValidationError("prediction artifact must predate holdout reveal receipt")
    by_id = {row.get("case_id"): row for row in prediction.get("predictions", [])}
    expected_ids = {row["case_id"] for row in protocol["sealed_holdouts"]}
    if set(by_id) != expected_ids:
        raise ValidationError("prediction manifest does not cover exactly all holdouts")
    cases: list[dict[str, Any]] = []
    row_schema = f"{protocol['hardware']['gpu_family'].lower()}-final-row.v1"
    for case in protocol["sealed_holdouts"]:
        measured = _case_times(artifact_root, "sealed_holdout", case, row_schema)
        predicted = float(by_id[case["case_id"]]["predicted_seconds"])
        repeat_metrics = _metrics([predicted] * len(measured), measured)
        cases.append(
            {
                "case_id": case["case_id"],
                "holdout_kind": case["holdout_kind"],
                "predicted_seconds": predicted,
                "measured_repeat_seconds": measured,
                "measured_median_seconds": statistics.median(measured),
                "repeat_mean_seconds": statistics.mean(measured),
                "repeat_std_seconds": statistics.stdev(measured) if len(measured) > 1 else 0.0,
                "repeat_cv_percent": 100.0 * statistics.stdev(measured) / statistics.mean(measured)
                if len(measured) > 1 and statistics.mean(measured) > 0
                else 0.0,
                "case_metrics": _metrics([predicted], [statistics.median(measured)]),
                "repeat_metrics": repeat_metrics,
            }
        )
    med_pred = [case["predicted_seconds"] for case in cases]
    med_measured = [case["measured_median_seconds"] for case in cases]
    overall = _metrics(med_pred, med_measured)
    interpolation = [case for case in cases if case["holdout_kind"] == "interpolation"]
    extrapolation = [case for case in cases if case["holdout_kind"] == "extrapolation"]
    metrics = {
        "schema_version": f"{_family(protocol)}-feature-holdout-metrics.v1",
        "provenance": "derived",
        "protocol_sha256": protocol_hash,
        "split_manifest_sha256": split_hash,
        "prediction_manifest_sha256": sha256_file(prediction_path),
        "coverage_percent": 100.0 * len(cases) / len(expected_ids),
        "case_median_wall": overall,
        "interpolation_wall": _metrics(
            [case["predicted_seconds"] for case in interpolation],
            [case["measured_median_seconds"] for case in interpolation],
        ),
        "extrapolation_wall": _metrics(
            [case["predicted_seconds"] for case in extrapolation],
            [case["measured_median_seconds"] for case in extrapolation],
        ),
        "cases": cases,
        "thresholds": protocol["scoring"]["acceptance_thresholds"],
    }
    path = artifact_root / "derived" / "holdout_metrics.json"
    _atomic_create(path, _dump(metrics))
    return {"metrics": str(path), "mape_percent": overall["mape_percent"], "coverage_percent": metrics["coverage_percent"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("seal", "fit", "score"):
        command = sub.add_parser(name)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        command.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "seal":
            result = seal(args.config, args.artifact_root)
        elif args.command == "fit":
            result = fit(args.config, args.artifact_root)
        else:
            result = score(args.config, args.artifact_root)
    except ValidationError as exc:
        print(f"VALIDATION ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
