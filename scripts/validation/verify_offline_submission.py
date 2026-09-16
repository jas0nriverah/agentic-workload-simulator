#!/usr/bin/env python3
"""Read-only artifact acceptance checks for the offline derivative snapshot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def require(value, message):
    if not value:
        raise ValueError(message)


def verify(snapshot: Path):
    checks = {}
    historical = read_json(snapshot / "verification/historical-before.json")
    for row in historical:
        require(digest(row["path"]) == row["sha256"], f"Historical mutation: {row['path']}")
    frozen = read_json(snapshot / "verification/frozen-source-check.json")
    for row in frozen:
        require(digest(row["path"]) == row["expected_sha256"], f"Frozen source mutation: {row['path']}")
    checks["historical_hashes_verified"] = len(historical)
    checks["frozen_sources_verified"] = len(frozen)

    reports = {}
    for mode, folder in (("observed", snapshot / "figures"), ("predicted", snapshot / "d9-predicted/figures")):
        report = read_json(folder / "assignment_report.json")
        reports[mode] = report
        require(report["latency_kind"] == mode, f"Wrong timing mode: {folder}")
        require(len(report["figure_inventory"]) == 12, f"Missing required figure: {folder}")
        for row in report["figure_inventory"]:
            require(digest(folder / row["path"]) == row["sha256"], f"Stale figure: {folder / row['path']}")
        for key, value in report["source_sha256"].items():
            require(digest(report["sources"][key]) == value, f"Stale {mode} source: {key}")
        checks[f"{mode}_figures_verified"] = 12

    observed = reports["observed"]
    samples = [sample for cells in observed["sweeps"].values() for cell in cells for sample in cell["samples"]]
    copied = [row for row in samples if "::step2-baseline::" in row["run_id"]]
    require(len(samples) == 384 and len(copied) == 96, "Wrong sweep/copy count")
    require(all(row["provenance"] != "measured" and "baseline" in row["provenance"] for row in copied), "Copied baseline mislabeled measured")
    require(len(observed["sweeps"]) == 4, "Wrong parameter count")
    for parameter, cells in observed["sweeps"].items():
        require(len(cells) == 4, f"Wrong value count: {parameter}")
        require(all(len(cell["samples"]) == 24 and len(cell["categories"]) == 8 for cell in cells), f"Missing sample/category panels: {parameter}")
    checks["copied_baselines_correctly_labeled"] = 96

    headline = read_json(snapshot / "figures-input/d1_headline_metrics.json")
    for suite, count, total, mean in (("lite", 100, 300, 160.9619677790433), ("verified", 198, 500, 147.33076228760785)):
        for source in (headline["suites"], observed["suite_headline_metrics"]):
            require(source[suite]["resolved"] == {"count": count, "denominator": total}, f"D1 labels changed: {suite}")
            require(math.isclose(source[suite]["average_completed_e2e_wall_s"], mean, abs_tol=1e-10, rel_tol=0), f"D1 headline changed: {suite}")
    checks["d1_original_headline_preserved"] = True

    manifest = read_json(snapshot / "d9-predicted/d9_predicted_manifest.json")
    coverage = manifest["coverage"]
    require(coverage["baseline_predicted_runs"] + coverage["baseline_unknown_runs"] + manifest["holdout_exclusion"]["baseline_source_rows_excluded"] == 800, "Prediction coverage does not close")
    require(manifest["v3_model"]["file_sha256_before"] == manifest["v3_model"]["file_sha256_after"] == "01fb1dddd83c3ac71c5685f341e96764849dff01bd3c8c9cdebd91932aa15965", "Frozen v3 changed")
    checks["prediction_coverage"] = coverage

    canonical = {}
    with (snapshot / "figures-input/trajectories.csv").open() as stream:
        canonical = {row["run_id"]: row for row in csv.DictReader(stream)}
    with (snapshot / "d9-predicted/figures-input/trajectories.csv").open() as stream:
        predicted = list(csv.DictReader(stream))
    for row in predicted:
        require(row["instance_id"] != "sympy__sympy-12481", "Holdout entered prediction export")
        require(row["official_resolved"] == canonical[row["run_id"]]["official_resolved"], "Observed accuracy was replaced")
    checks["predicted_accuracy_remains_observed"] = True

    for filename in ("ASSIGNMENT_REPORT.md", "COMPLIANCE.md"):
        path = snapshot / filename
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text()):
            if "://" in target or target.startswith("#"):
                continue
            require((path.parent / target.split("#")[0]).is_file(), f"Unresolved link in {filename}: {target}")
    checks["report_links_resolve"] = True
    require((snapshot / "configuration-analysis/CONFIGURATION_ANALYSIS.md").is_file(), "Configuration statistics missing")
    checks["configuration_report_present"] = True
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    print(json.dumps({"status": "pass", "checks": verify(args.snapshot)}, indent=2, sort_keys=True))
