#!/usr/bin/env python3
"""Generate a self-contained SVG comparison of measured deep profiles."""

from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path
from typing import Any

COLORS = {"Verified": "#1769aa", "Lite": "#18794e"}
TEXT = "#344054"
GRID = "#d0d5dd"


def _text(x: float, y: float, value: str, size: int = 13, **attrs: str) -> str:
    extra = " ".join(f'{key}="{escape(str(item))}"' for key, item in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" {extra}>{escape(value)}</text>'


def _bar_chart(
    *,
    x0: float,
    y0: float,
    width: float,
    height: float,
    title: str,
    unit: str,
    values: dict[str, dict[str, float]],
) -> list[str]:
    labels = list(next(iter(values.values())).keys())
    maximum = max(max(items.values()) for items in values.values()) * 1.2
    maximum = max(maximum, 1.0)
    left, right, top, bottom = x0 + 82, x0 + width - 18, y0 + 42, y0 + height - 54
    plot_width, plot_height = right - left, bottom - top
    lines = [_text(x0, y0 + 20, title, 17, fill=TEXT, **{"font-weight": "700"})]
    for tick in range(5):
        value = maximum * tick / 4
        yy = bottom - value / maximum * plot_height
        lines.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{right}" y2="{yy:.1f}" stroke="{GRID}"/>')
        lines.append(_text(left - 10, yy + 4, f"{value:.0f}", 11, fill=TEXT, **{"text-anchor": "end"}))
    lines.extend(
        [
            f'<line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="{TEXT}"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="{TEXT}"/>',
            _text(x0 + width / 2, y0 + height - 10, unit, 12, fill=TEXT, **{"text-anchor": "middle"}),
        ]
    )
    group_width = plot_width / len(labels)
    bar_width = min(28, group_width / (len(values) + 1))
    for index, label in enumerate(labels):
        center = left + group_width * (index + 0.5)
        lines.append(_text(center, bottom + 19, label, 11, fill=TEXT, **{"text-anchor": "middle"}))
        for series_index, (series, items) in enumerate(values.items()):
            value = items[label]
            xx = center + (series_index - (len(values) - 1) / 2) * (bar_width + 4) - bar_width / 2
            yy = bottom - value / maximum * plot_height
            lines.append(
                f'<rect x="{xx:.1f}" y="{yy:.1f}" width="{bar_width:.1f}" '
                f'height="{bottom - yy:.1f}" fill="{COLORS[series]}"/>'
            )
            lines.append(_text(xx + bar_width / 2, yy - 5, f"{value:.0f}", 10, fill=COLORS[series], **{"text-anchor": "middle"}))
    legend_x = right - 145
    for index, series in enumerate(values):
        xx = legend_x + index * 80
        lines.append(f'<rect x="{xx}" y="{y0 + 12}" width="10" height="10" fill="{COLORS[series]}"/>')
        lines.append(_text(xx + 15, y0 + 22, series, 11, fill=TEXT))
    return lines


def _profile_value(manifest: dict[str, Any], key: str) -> float:
    return float(manifest[key])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified", type=Path, required=True)
    parser.add_argument("--lite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    verified = json.loads(args.verified.read_text(encoding="utf-8"))
    lite = json.loads(args.lite.read_text(encoding="utf-8"))
    manifests = {"Verified": verified, "Lite": lite}
    phases = {
        name: {phase: value / 1000 for phase, value in manifest["phase_events_ms"].items()}
        for name, manifest in manifests.items()
    }
    tokens = {
        name: {
            "prompt": float(manifest["vllm_metrics"]["prompt_tokens_delta"]),
            "generation": float(manifest["vllm_metrics"]["generation_tokens_delta"]),
        }
        for name, manifest in manifests.items()
    }
    requests = {
        name: {"successful requests": float(manifest["vllm_metrics"]["request_success_delta"])}
        for name, manifest in manifests.items()
    }
    events = {
        name: {
            "raw lines": float(manifest["cpu_file_trace"]["raw_event_count"]),
            "parsed lines": float(manifest["cpu_file_trace"]["parsed_event_count"]),
        }
        for name, manifest in manifests.items()
    }
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="940" viewBox="0 0 1200 940">',
        '<rect width="100%" height="100%" fill="white"/>',
        _text(42, 42, "Deep-profile comparison: measured CPU and vLLM aggregates", 24, fill=TEXT, **{"font-weight": "700"}),
        _text(42, 68, "Two one-instance H100 runs; values are measured or reset-safe derived aggregates.", 13, fill=TEXT),
    ]
    svg += _bar_chart(x0=42, y0=92, width=1116, height=300, title="End-to-end phase durations", unit="Duration (seconds)", values=phases)
    svg += _bar_chart(x0=42, y0=414, width=1116, height=220, title="vLLM server-aggregate token totals", unit="Tokens", values=tokens)
    svg += _bar_chart(x0=42, y0=652, width=1116, height=120, title="vLLM successful request count", unit="Requests", values=requests)
    svg += _bar_chart(x0=42, y0=794, width=1116, height=120, title="CPU file-event trace volume", unit="strace lines", values=events)
    svg += [
        _text(42, 934, f"Source: {args.verified.as_posix()} and {args.lite.as_posix()}. No per-request GPU identity is inferred.", 11, fill=TEXT),
        "</svg>",
    ]
    if args.output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {args.output}; use --force")
    if args.summary_output.exists() and not args.force:
        raise SystemExit(f"refusing to overwrite {args.summary_output}; use --force")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.summary_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(svg), encoding="utf-8")
    summary = {
        "schema_version": "modal-deep-profile-comparison.v1",
        "provenance": "derived",
        "sources": [args.verified.as_posix(), args.lite.as_posix()],
        "phase_seconds": phases,
        "vllm_aggregate": tokens,
        "cpu_file_trace_lines": events,
        "interpretation": "Comparison of two measured one-instance runs; server aggregate counters are not per-request GPU timings.",
    }
    args.summary_output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {args.output} and {args.summary_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
