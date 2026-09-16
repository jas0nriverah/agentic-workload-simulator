#!/usr/bin/env python3
"""Verify pinned public Parquet sources and their exact canonical JSONL bytes.

Offline only. Never rewrites datasets or runtime manifests. The new proof
retains source and derived hashes and agreed per-row/per-field identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Sequence


SCHEMA = "assignment.verified-dataset-materialization.v1"
DATASET_REVISIONS = {
    "lite": "69611d31007e1c6731db8bd5b5c3f2d33f5bab6e",
    "verified": "91aa3ed51b709be6457e12d00300a6a596d4c6a3",
}
SOURCE_PARQUET_HASHES = {
    "lite": "f46f2e3f003f2552932393da4b223e1e0456a2c71eba8b73ae58f29646c1278b",
    "verified": "43ed5a3d1d98da36472c1ade65ddd2085d7b4ff694fcaf6a023a07c5c1f32f21",
}
JSONL_HASHES = {
    "lite": "7f54792b83bf491c0a905770a00ce7fa28836552d37c7ea0e9e2bae4c53f33fb",
    "verified": "52ccbc6ec0e03085f95191b261e0ed881cd6a0752a3c5247c1aba258ec2993da",
}
DATASET_NAMES = {"lite": "SWE-bench/SWE-bench_Lite", "verified": "SWE-bench/SWE-bench_Verified"}
ROW_COUNTS = {"lite": 300, "verified": 500}
MAX_FILE_BYTES = 64 * 1024 * 1024


class MaterializationError(ValueError):
    """A source pin, row identity, or derived-byte contract did not match."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise MaterializationError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _read(path: Path) -> bytes:
    _require(path.is_file(), f"missing regular input file: {path}")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    _require(len(raw) <= MAX_FILE_BYTES, f"input exceeds 64 MiB bound: {path}")
    return raw


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise MaterializationError(f"invalid JSON constant: {value}")


def verify_pair(
    source_path: Path,
    jsonl_path: Path,
    *,
    source_sha256: str,
    jsonl_sha256: str,
    expected_rows: int,
) -> dict[str, Any]:
    """Verify one caller-pinned pair. Production callers use verify_materialization.

    Decode the same source bytes that were hashed, then compare ordered rows,
    keys and canonical field hashes. Finally require exact canonical JSONL
    bytes and the independently pinned derived-byte digest.
    """
    source_raw, jsonl_raw = _read(source_path), _read(jsonl_path)
    _require(_sha(source_raw) == source_sha256, "source Parquet SHA-256 mismatch")
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise MaterializationError("PyArrow is required in the verifier interpreter") from exc
    try:
        parquet = pq.ParquetFile(pa.BufferReader(source_raw))
        _require(parquet.metadata.num_rows == expected_rows, "source row count mismatch")
        source_rows = parquet.read().to_pylist()
        jsonl_rows = [json.loads(line, object_pairs_hook=_object, parse_constant=_invalid_constant)
                      for line in jsonl_raw.decode("utf-8").splitlines() if line.strip()]
    except MaterializationError:
        raise
    except Exception as exc:
        # Report location/type, never public patches or field values.
        raise MaterializationError(f"dataset decoding failed ({type(exc).__name__})") from exc
    _require(len(jsonl_rows) == expected_rows, "JSONL row count mismatch")
    _require(all(isinstance(row, dict) for row in jsonl_rows), "JSONL rows must be objects")
    source_ids = [row.get("instance_id") for row in source_rows]
    jsonl_ids = [row.get("instance_id") for row in jsonl_rows]
    _require(all(isinstance(i, str) and i for i in source_ids + jsonl_ids), "invalid instance identity")
    _require(len(set(source_ids)) == expected_rows, "duplicate source instance identity")
    _require(len(set(jsonl_ids)) == expected_rows, "duplicate JSONL instance identity")
    _require(source_ids == jsonl_ids, "row identity/order mismatch")

    identities = []
    rederived = bytearray()
    for index, (source, derived) in enumerate(zip(source_rows, jsonl_rows)):
        _require(set(source) == set(derived), f"row {index} field set mismatch")
        fields = {}
        for key in sorted(source):
            try:
                source_value, derived_value = canonical(source[key]), canonical(derived[key])
            except (TypeError, ValueError) as exc:
                raise MaterializationError(f"row {index} field {key} is not canonical JSON") from exc
            _require(source_value == derived_value, f"row {index} field {key} mismatch")
            fields[key] = _sha(source_value)
        source_row = canonical(source)
        rederived.extend(source_row + b"\n")
        identities.append({"row_index": index, "instance_id": source_ids[index],
                           "source_and_jsonl_row_sha256": _sha(source_row),
                           "source_and_jsonl_field_sha256": fields})
    _require(bytes(rederived) == jsonl_raw, "JSONL bytes are not the canonical ordered materialization")
    _require(_sha(jsonl_raw) == jsonl_sha256, "derived JSONL SHA-256 mismatch")
    _require(_read(source_path) == source_raw, "source changed during verification")
    _require(_read(jsonl_path) == jsonl_raw, "JSONL changed during verification")
    return {
        "status": "verified",
        "source": {"path": str(source_path.absolute()), "resolved_path": str(source_path.resolve()),
                   "format": "parquet", "sha256": source_sha256, "bytes": len(source_raw)},
        "derived": {"path": str(jsonl_path.absolute()), "resolved_path": str(jsonl_path.resolve()),
                    "format": "jsonl", "sha256": jsonl_sha256, "bytes": len(jsonl_raw)},
        "row_count": expected_rows, "row_order_and_all_fields_equal": True,
        "canonical_rederived_sha256": _sha(bytes(rederived)),
        "ordered_instance_ids_sha256": _sha(canonical(source_ids)),
        "pyarrow_version": pa.__version__, "row_identity_hashes": identities,
    }


def verify_materialization(dataset_root: Path, suites: Sequence[str] = ("lite", "verified")) -> dict[str, Any]:
    """Renderer API: validate fixed source/revision/derived-byte pins without writes."""
    _require(bool(suites) and len(set(suites)) == len(suites), "suites must be nonempty and unique")
    _require(all(suite in DATASET_REVISIONS for suite in suites), "unknown dataset suite")
    verified = {}
    for suite in suites:
        revision = DATASET_REVISIONS[suite]
        filename = DATASET_NAMES[suite].split("/")[-1] + ".jsonl"
        pair = verify_pair(dataset_root / "raw" / f"{suite}-test-{revision}.parquet",
                           dataset_root / filename, source_sha256=SOURCE_PARQUET_HASHES[suite],
                           jsonl_sha256=JSONL_HASHES[suite], expected_rows=ROW_COUNTS[suite])
        verified[suite] = {"dataset": DATASET_NAMES[suite], "revision": revision, "split": "test", **pair}
    return {
        "schema_version": SCHEMA, "status": "verified", "suites": verified,
        "serialization": {"encoding": "utf-8", "ensure_ascii": False, "sort_keys": True,
                          "separators": [",", ":"], "row_order": "source", "row_terminator": "LF"},
        "verifier": {"path": str(Path(__file__).absolute()),
                     "sha256": _sha(Path(__file__).read_bytes()), "python": sys.version},
        "scope": "public acquisition identity; field values compared opaquely, no outcome analysis",
    }


def write_proof(path: Path, proof: dict[str, Any]) -> str:
    """Write a new proof plus hash sidecar; refuse either existing artifact."""
    sidecar = path.with_name(path.name + ".sha256")
    _require(not path.exists() and not path.is_symlink() and not sidecar.exists()
             and not sidecar.is_symlink(), "refusing to overwrite proof or hash sidecar")
    raw = (json.dumps(proof, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n").encode()
    digest = _sha(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    with sidecar.open("x", encoding="utf-8") as stream:
        stream.write(f"{digest}  {path.name}\n")
        stream.flush()
        os.fsync(stream.fileno())
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--suite", choices=tuple(DATASET_REVISIONS), action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        proof = verify_materialization(args.dataset_root, args.suite or ("lite", "verified"))
        digest = write_proof(args.output, proof)
    except (MaterializationError, OSError) as exc:
        print(f"verify_dataset_materialization: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": "verified", "proof_path": str(args.output.absolute()),
                      "proof_sha256": digest, "suites": {k: {"rows": v["row_count"],
                      "source_sha256": v["source"]["sha256"], "jsonl_sha256": v["derived"]["sha256"]}
                      for k, v in proof["suites"].items()}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
