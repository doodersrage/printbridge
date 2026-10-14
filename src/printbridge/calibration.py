"""Calibration prints for tuning the V3 SE profiles.

Each test is a generated model plus a G-code post-processing step:

temperature  A tower of bands, each with two pillars joined by a bridge. The
             nozzle temperature changes at every band, so stringing between the
             pillars, bridge sag and surface finish can be compared.
retraction   Two thin pillars far apart. The retraction length changes every
             band; pick the band with the least stringing between them.
first_layer  Five single-layer squares at the corners and the centre of the bed,
             for checking first-layer squish and levelling across the bed.
dimension    A 20 mm cube with an X marker, for checking that parts come out the
             right size.
"""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Kind = Literal["temperature", "retraction", "first_layer", "dimension"]
BAND_HEIGHT = {"temperature": 8.0, "retraction": 5.0, "first_layer": 0.0, "dimension": 0.0}
CUBE = 20.0
DEFAULTS = {
    "temperature": {"pla": (220, 190, 5), "petg": (250, 220, 5), "tpu": (235, 210, 5), "*": (230, 200, 5)},
    "retraction": {"*": (0.2, 1.6, 0.2), "tpu": (0.2, 1.2, 0.2)},
}
MAX_BANDS = 12


class Band(BaseModel):
    z_from: float
    z_to: float
    value: float


class CalibrationSpec(BaseModel):
    kind: Kind
    start: float = 0
    end: float = 0
    step: float = Field(0, ge=0)
    first_layer_height: float = 0.24
    bands: list[Band] = []

    @model_validator(mode="after")
    def _check(self) -> "CalibrationSpec":
        if self.kind in ("first_layer", "dimension"):
            return self
        if self.step <= 0:
            raise ValueError("step must be greater than 0")
        count = int(round(abs(self.end - self.start) / self.step)) + 1
        if count < 2 or count > MAX_BANDS:
            raise ValueError(f"choose a range that gives 2 to {MAX_BANDS} bands (this gives {count})")
        if self.kind == "temperature" and not (150 <= min(self.start, self.end) and max(self.start, self.end) <= 260):
            raise ValueError("temperatures must be between 150 and 260 °C")
        if self.kind == "retraction" and not (0 <= min(self.start, self.end) and max(self.start, self.end) <= 6):
            raise ValueError("retraction lengths must be between 0 and 6 mm")
        return self

    def values(self) -> list[float]:
        if self.kind in ("first_layer", "dimension"):
            return []
        count = int(round(abs(self.end - self.start) / self.step)) + 1
        sign = 1 if self.end >= self.start else -1
        return [round(self.start + sign * i * self.step, 3) for i in range(count)]


def defaults(kind: Kind, material: str) -> tuple[float, float, float] | None:
    table = DEFAULTS.get(kind)
    if not table:
        return None
    return table.get(material, table["*"])


# ---- mesh generation ------------------------------------------------------------

Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> list[Tri]:
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def write_stl(path: Path, tris: list[Tri]) -> Path:
    with path.open("wb") as fh:
        fh.write(b"PrintBridge calibration".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            ux, uy, uz = (b[i] - a[i] for i in range(3))
            vx, vy, vz = (c[i] - a[i] for i in range(3))
            n = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
            length = math.sqrt(sum(x * x for x in n)) or 1.0
            fh.write(struct.pack("<12fH", *(x / length for x in n), *a, *b, *c, 0))
    return path


def temperature_tower(bands: int) -> list[Tri]:
    tris = box(-2, -2, 0, 32, 12, 1.0)  # base for adhesion
    for i in range(bands):
        z0 = 1.0 + i * BAND_HEIGHT["temperature"]
        z1 = z0 + BAND_HEIGHT["temperature"]
        tris += box(0, 0, z0, 8, 10, z1)            # left pillar
        tris += box(22, 0, z0, 30, 10, z1)          # right pillar
        tris += box(8, 0, z1 - 1.6, 22, 10, z1)     # 14 mm bridge
        tris += box(8, 4, z0, 9.5, 6, z1 - 1.6)     # thin fin: shows stringing across the gap
    return tris


def retraction_towers(bands: int) -> list[Tri]:
    height = 0.6 + bands * BAND_HEIGHT["retraction"]
    return box(-5, -5, 0, 55, 5, 0.6) + box(-4, -4, 0.6, 4, 4, height) + box(42, -4, 0.6, 50, 4, height)


def first_layer_pads(layer_height: float, bed: float = 220, size: float = 30, inset: float = 22) -> list[Tri]:
    lo, hi, mid = inset, bed - inset - size, (bed - size) / 2
    tris: list[Tri] = []
    for x, y in ((lo, lo), (hi, lo), (lo, hi), (hi, hi), (mid, mid)):
        tris += box(x, y, 0, x + size, y + size, layer_height)
    return tris


def dimension_cube() -> list[Tri]:
    # A small bump on the +X face, 2 mm proud, so the X and Y sides can be told apart.
    return box(0, 0, 0, CUBE, CUBE, CUBE) + box(CUBE, 8, 8, CUBE + 2, 12, 12)


def generate(spec: CalibrationSpec, out: Path) -> CalibrationSpec:
    """Write the test model and fill in the band table."""
    values = spec.values()
    if spec.kind == "temperature":
        tris = temperature_tower(len(values))
        base = 1.0
    elif spec.kind == "retraction":
        tris = retraction_towers(len(values))
        base = 0.6
    elif spec.kind == "dimension":
        tris = dimension_cube()
        base = 0.0
    else:
        tris = first_layer_pads(spec.first_layer_height)
        base = 0.0
    write_stl(out, tris)
    h = BAND_HEIGHT[spec.kind]
    bands = [Band(z_from=round(base + i * h, 2), z_to=round(base + (i + 1) * h, 2), value=v) for i, v in enumerate(values)]
    return spec.model_copy(update={"bands": bands})


# ---- G-code post-processing ---------------------------------------------------

_Z_RE = re.compile(r"^;Z:([\d.]+)")
# A pure retract/deretract move as PrusaSlicer writes it with relative E: "G1 E-.8 F2400".
_RETRACT_RE = re.compile(r"^G1 E(-?)(\d*\.?\d+) F(\d+(?:\.\d+)?)\s*$")


def band_for(spec: CalibrationSpec, z: float) -> Band | None:
    for band in spec.bands:
        if z < band.z_to - 1e-6:
            return band if z >= band.z_from - 1e-6 else None
    return spec.bands[-1] if spec.bands else None


def post_process(gcode: Path, spec: CalibrationSpec) -> int:
    """Apply per-band values to sliced G-code. Returns the number of changes made."""
    if spec.kind in ("first_layer", "dimension") or not spec.bands:
        return 0
    out_lines: list[str] = []
    current: Band | None = None
    changes = 0
    with gcode.open("r", errors="replace") as fh:
        for line in fh:
            m = _Z_RE.match(line)
            if m:
                band = band_for(spec, float(m.group(1)))
                if band is not None and band is not current:
                    current = band
                    if spec.kind == "temperature":
                        out_lines.append(line)
                        out_lines.append(f"M104 S{band.value:g} ; calibration band {band.z_from:g}-{band.z_to:g} mm\n")
                        changes += 1
                        continue
            if spec.kind == "retraction" and current is not None:
                r = _RETRACT_RE.match(line)
                if r:
                    out_lines.append(f"G1 E{r.group(1)}{current.value:g} F{r.group(3)} ; calibration\n")
                    changes += 1
                    continue
            out_lines.append(line)
    tmp = gcode.with_suffix(".tmp")
    tmp.write_text("".join(out_lines))
    tmp.replace(gcode)
    return changes


def result_values(kind: Kind, value: float) -> dict[str, str]:
    """Material profile keys to store for a chosen calibration result."""
    if kind == "temperature":
        return {"temperature": f"{value:g}", "first_layer_temperature": f"{value:g}"}
    if kind == "retraction":
        return {"filament_retract_length": f"{value:g}"}
    raise ValueError("This test has no value to save")
