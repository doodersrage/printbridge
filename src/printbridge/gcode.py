"""Extract summary statistics from sliced G-code."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

_TIME_RE = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$")
_FIELDS = {
    "filament_mm": re.compile(r"^; filament used \[mm\] = ([\d.]+)"),
    "filament_g": re.compile(r"^; (?:total )?filament used \[g\] = ([\d.]+)"),
    "layer_height": re.compile(r"^; layer_height = ([\d.]+)"),
}
_DURATION_RE = re.compile(r"(\d+)\s*([dhms])")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_TAIL_BYTES = 64 * 1024


class GcodeStats(BaseModel):
    print_time: str | None = None
    print_seconds: int | None = None
    filament_mm: float | None = None
    filament_g: float | None = None
    layer_height: float | None = None
    layers: int | None = None


def parse_duration(text: str) -> int:
    return sum(int(n) * _UNIT_SECONDS[u] for n, u in _DURATION_RE.findall(text))


def parse_stats(path: Path) -> GcodeStats:
    """PrusaSlicer writes its summary as comments at the end of the file."""
    stats = GcodeStats()
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - _TAIL_BYTES))
        tail = fh.read().decode(errors="replace").splitlines()
    for line in tail:
        if m := _TIME_RE.match(line):
            stats.print_time = m.group(1).strip()
            stats.print_seconds = parse_duration(stats.print_time)
            continue
        for field, rx in _FIELDS.items():
            if getattr(stats, field) is None and (m := rx.match(line)):
                setattr(stats, field, float(m.group(1)))

    layers = 0
    with path.open("r", errors="replace") as fh:
        for line in fh:
            if line.startswith(";LAYER_CHANGE"):
                layers += 1
    stats.layers = layers or None
    return stats
