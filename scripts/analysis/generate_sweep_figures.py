#!/usr/bin/env python3
"""Generate dependency-free SVG figures from the measured Lite sweep manifest.

The figures intentionally show only measured values. Infrastructure-failure
cells are retained in the derived summary and annotated in the temperature
figure instead of being silently treated as zeroes.
"""

from __future__ import annotations

import argparse
import base64
import json
from html import escape
from pathlib import Path
from typing import Any

GROUPS = {
    "calls": ("agent.model.per_instance_call_limit", "Call limit", "calls"),
    "tokens": ("completion_kwargs.max_tokens", "Completion max tokens", "tokens"),
    "observation": (
        "agent.templates.max_observation_length",
        "Maximum observation length",
        "tokens",
    ),
    "temperature": ("agent.model.temperature", "Temperature", "unitless"),
}
ACCENT = "#1769aa"
SUCCESS = "#18794e"
FAILURE = "#b42318"
GRID = "#d0d5dd"
TEXT = "#344054"


def _numeric_value(cell: dict[str, Any]) -> float | None:
    value = cell.get("value")
    return value if isinstance(value, (int, float)) else None


def _cells(manifest: dict[str, Any], knob: str) -> list[dict[str, Any]]:
    return [
        cell
        for cell in manifest["cells"]
        if cell.get("knob") == knob and _numeric_value(cell) is not None
    ]


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _svg_text(x: float, y: float, text: str, size: int = 13, **attrs: str) -> str:
    extra = " ".join(f'{key}="{escape(value)}"' for key, value in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" {extra}>{escape(text)}</text>'


def _figure(
    *,
    title: str,
    knob: str,
    unit: str,
    cells: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    source: str,
) -> str:
    width, height = 960, 600
    left, right, top, bottom = 92, 42, 82, 110
    plot_w, plot_h = width - left - right, height - top - bottom
    measured = [c for c in cells if isinstance(c.get("trajectory_wall_seconds"), (int, float))]
    max_y = max((float(c["trajectory_wall_seconds"]) for c in measured), default=1.0) * 1.18
    max_y = max(max_y, 1.0)
    x_values = [float(c["value"]) for c in cells]
    min_x, max_x = min(x_values, default=0.0), max(x_values, default=1.0)
    span = max(max_x - min_x, 1.0)

    def x(value: float) -> float:
        return left + (value - min_x) / span * plot_w

    def y(value: float) -> float:
        return top + plot_h - value / max_y * plot_h

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_text(left, 34, title, 22, fill=TEXT, **{"font-weight": "700"}),
        _svg_text(left, 58, f"Measured one-instance sweep; x-axis: {knob}", 13, fill=TEXT),
    ]
    for tick in range(6):
        value = max_y * tick / 5
        yy = y(value)
        lines.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{left + plot_w}" y2="{yy:.1f}" stroke="{GRID}"/>')
        lines.append(_svg_text(left - 12, yy + 5, f"{value:.0f}", 12, fill=TEXT, **{"text-anchor": "end"}))
    lines.extend(
        [
            f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="{TEXT}"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="{TEXT}"/>',
            _svg_text(left + plot_w / 2, height - 62, f"{knob} ({unit})", 14, fill=TEXT, **{"text-anchor": "middle"}),
            _svg_text(27, top + plot_h / 2, "Agent trajectory wall time (seconds)", 14, fill=TEXT, transform=f"rotate(-90 27 {top + plot_h / 2:.1f})", **{"text-anchor": "middle"}),
        ]
    )
    for cell in cells:
        xx = x(float(cell["value"]))
        lines.append(f'<line x1="{xx:.1f}" y1="{top + plot_h}" x2="{xx:.1f}" y2="{top + plot_h + 7}" stroke="{TEXT}"/>')
        lines.append(_svg_text(xx, top + plot_h + 26, _fmt(float(cell["value"])), 12, fill=TEXT, **{"text-anchor": "middle"}))
    measured.sort(key=lambda cell: float(cell["value"]))
    points = " ".join(f"{x(float(c['value'])):.1f},{y(float(c['trajectory_wall_seconds'])):.1f}" for c in measured)
    if points:
        lines.append(f'<polyline points="{points}" fill="none" stroke="{ACCENT}" stroke-width="3"/>')
    for cell in measured:
        xx, yy = x(float(cell["value"])), y(float(cell["trajectory_wall_seconds"]))
        resolved = bool(cell.get("official_resolved"))
        color = SUCCESS if resolved else FAILURE
        lines.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="7" fill="{color}"/>')
        status = "resolved" if resolved else "unresolved"
        lines.append(_svg_text(xx, yy - 13, status, 11, fill=color, **{"text-anchor": "middle"}))
    if failed:
        note = "Infrastructure failures retained: " + ", ".join(_fmt(float(c["value"])) for c in failed)
        lines.append(_svg_text(left, height - 28, note, 12, fill=FAILURE))
    lines.extend(
        [
            f'<line x1="{width - 260}" y1="48" x2="{width - 235}" y2="48" stroke="{ACCENT}" stroke-width="3"/>',
            _svg_text(width - 225, 53, "trajectory wall time", 12, fill=TEXT),
            f'<circle cx="{width - 98}" cy="48" r="6" fill="{SUCCESS}"/>',
            _svg_text(width - 88, 53, "resolved", 12, fill=TEXT),
            _svg_text(left, height - 8, f"Source: {source}; status labels are official SWE-bench evaluation outcomes.", 11, fill=TEXT),
            "</svg>",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = args.manifest.as_posix()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"schema_version": "modal-lite-sweep-figures.v1", "source": source, "figures": {}}
    for name, (knob, label, unit) in GROUPS.items():
        cells = _cells(manifest, knob)
        failed = [
            cell
            for cell in manifest["cells"]
            if cell.get("knob") == knob and _numeric_value(cell) is not None and "trajectory_wall_seconds" not in cell
        ]
        output = args.output_dir / f"lite-sweep-{name}.svg"
        if output.exists() and not args.force:
            raise SystemExit(f"refusing to overwrite {output}; use --force")
        output.write_text(
            _figure(title=f"Lite sweep: {label}", knob=label, unit=unit, cells=cells, failed=failed, source=source),
            encoding="utf-8",
        )
        summary["figures"][name] = {
            "knob": knob,
            "label": label,
            "unit": unit,
            "measured_cells": len(cells),
            "infrastructure_failure_cells": [cell["cell_id"] for cell in failed],
            "resolved_cells": [cell["cell_id"] for cell in cells if cell.get("official_resolved") == 1],
        }
    combined = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="980" viewBox="0 0 1400 980">',
        '<rect width="100%" height="100%" fill="white"/>',
        _svg_text(40, 38, "Lite sweep: combined measured results", 24, fill=TEXT, **{"font-weight": "700"}),
        _svg_text(40, 64, "Each panel shows trajectory wall time; point labels are official evaluator outcomes.", 13, fill=TEXT),
    ]
    for index, name in enumerate(GROUPS):
        figure = (args.output_dir / f"lite-sweep-{name}.svg").read_bytes()
        encoded = base64.b64encode(figure).decode("ascii")
        x_offset = 20 + (index % 2) * 690
        y_offset = 78 + (index // 2) * 450
        combined.append(
            f'<image x="{x_offset}" y="{y_offset}" width="660" height="412" '
            f'href="data:image/svg+xml;base64,{encoded}"/>'
        )
    combined.append("</svg>")
    combined_path = args.output_dir / "lite-sweep-combined.svg"
    if combined_path.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {combined_path}; use --force")
    combined_path.write_text("\n".join(combined), encoding="utf-8")
    summary["figures"]["combined"] = {
        "format": "self-contained SVG",
        "panels": list(GROUPS),
        "source": source,
    }
    summary_path = args.output_dir / "summary.json"
    if summary_path.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {summary_path}; use --force")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {len(summary['figures'])} SVG figures and {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
