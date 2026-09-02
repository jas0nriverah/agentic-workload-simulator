#!/usr/bin/env python3
"""Generate a deterministic outcome-only baseline figure.

This is intentionally separate from the assignment latency figures: the
compact historical population table contains outcomes and repository labels,
but not same-trajectory tool/model timing.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path


WIDTH = 1200
ROW_HEIGHT = 32
LEFT = 320
RIGHT = 120
TOP = 90
BOTTOM = 70


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    required = {"suite", "repository", "completed_instances", "resolved_instances"}
    if not rows or not required.issubset(rows[0]):
        missing = sorted(required.difference(rows[0] if rows else set()))
        raise ValueError(f"missing required columns: {', '.join(missing)}")
    return rows


def make_svg(rows: list[dict[str, str]]) -> str:
    rows = sorted(rows, key=lambda row: (row["suite"], row["repository"]))
    height = TOP + BOTTOM + ROW_HEIGHT * len(rows)
    plot_width = WIDTH - LEFT - RIGHT
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}">',
        "<title>Historical H100 baseline resolved rate by repository</title>",
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="40" y="38" font-family="sans-serif" font-size="24" '
        'font-weight="bold">Historical H100 baseline: resolved rate</text>',
        '<text x="40" y="64" font-family="sans-serif" font-size="14">'
        "Outcome-only compact cohort; not a full-suite or latency result</text>",
        f'<line x1="{LEFT}" y1="{TOP - 10}" x2="{LEFT}" y2="{height - BOTTOM}" '
        'stroke="#333"/>',
    ]
    for index in range(0, 101, 25):
        x = LEFT + plot_width * index / 100
        parts.append(
            f'<line x1="{x:.1f}" y1="{TOP - 10}" x2="{x:.1f}" '
            f'y2="{height - BOTTOM}" stroke="#ddd"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{height - 38}" text-anchor="middle" '
            f'font-family="sans-serif" font-size="12">{index}%</text>'
        )
    for index, row in enumerate(rows):
        y = TOP + index * ROW_HEIGHT
        rate = float(row["resolved_rate_percent"])
        bar_width = plot_width * rate / 100
        label = f'{row["suite"].title()} — {row["repository"]}'
        parts.append(
            f'<text x="{LEFT - 10}" y="{y + 20}" text-anchor="end" '
            f'font-family="sans-serif" font-size="12">{html.escape(label)}</text>'
        )
        parts.append(
            f'<rect x="{LEFT}" y="{y + 5}" width="{bar_width:.1f}" height="20" '
            f'fill="#3568a8"/>'
        )
        parts.append(
            f'<text x="{LEFT + bar_width + 6:.1f}" y="{y + 20}" '
            f'font-family="sans-serif" font-size="12">{rate:.1f}% '
            f'({row["resolved_instances"]}/{row["completed_instances"]})</text>'
        )
    parts.append(
        f'<text x="{WIDTH / 2}" y="{height - 12}" text-anchor="middle" '
        'font-family="sans-serif" font-size="12">Officially resolved cases / completed cases</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = read_rows(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = args.output_dir / "baseline_resolved_rate.svg"
    json_path = args.output_dir / "baseline_resolved_rate.json"
    svg_path.write_text(make_svg(rows), encoding="utf-8")
    summary = {
        "figure": "baseline_resolved_rate",
        "source": str(args.input),
        "rows": len(rows),
        "scope": "historical H100 compact outcome cohort",
        "timing_data_included": False,
        "full_suite_claim": False,
        "rows_by_suite": {
            suite: sum(row["suite"] == suite for row in rows)
            for suite in sorted({row["suite"] for row in rows})
        },
    }
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {svg_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
