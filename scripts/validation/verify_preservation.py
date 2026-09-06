#!/usr/bin/env python3
"""Read-only verification of the original compact recovery snapshot."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "project/preservation/full_matrix_20260906.json"
MANIFEST_SHA256 = "8050dd781800196f5e863e4f5d7e34d92691678e4ebdcadb19304a602fb374b3"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify(root=ROOT):
    raw = (root / MANIFEST).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256, "preservation manifest changed")
    manifest = json.loads(raw)
    for item in manifest["files"]:
        data = (root / item["path"]).read_bytes()
        require(len(data) == item["size"], "size changed: " + item["path"])
        require(hashlib.sha256(data).hexdigest() == item["sha256"],
                "hash changed: " + item["path"])
    archive = root / manifest["archive"]
    ledgers = sorted(archive.glob("worker-*/run_state.json"))
    require(len(ledgers) == 16, "worker count changed")
    observed = {}
    result_paths = set()
    for ledger in ledgers:
        state = json.loads(ledger.read_bytes())
        completed, failed = state["completed_case_results"], state["failed_cases"]
        require(len(completed) + len(failed) == state["case_count"] == 68,
                "worker case count changed")
        for status, entries in (("completed", completed), ("failed", failed)):
            for case_id, entry in entries.items():
                require(case_id not in observed, "duplicate/overlapping case ID: " + case_id)
                observed[case_id] = (status, ledger.relative_to(root).as_posix())
                if status == "completed":
                    result = ledger.parent / "cases" / f"{entry['case_index']:05d}" / "case_result.json"
                    require(hashlib.sha256(result.read_bytes()).hexdigest() == entry["result_sha256"],
                            "ledger result hash mismatch")
                    require(json.loads(result.read_bytes())["resume_key"] == case_id,
                            "result case identity mismatch")
                    result_paths.add(result)
    expected = {row["case_id"]: (row["status"], row["ledger"]) for row in manifest["cases"]}
    require(observed == expected, "original case identities/statuses changed")
    require(len(observed) == 1088, "total case count changed")
    require(sum(status == "completed" for status, _ in observed.values()) == 602,
            "completed case count changed")
    require(set(archive.glob("worker-*/cases/*/case_result.json")) == result_paths,
            "archived result set changed")
    return {"total": 1088, "completed": 602, "remaining": 486, "overlap": 0,
            "preserved_hashes_match": True}


if __name__ == "__main__":
    print(json.dumps(verify(), sort_keys=True))
