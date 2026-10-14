"""Turn G-code into a compact binary of extrusion segments for the browser preview.

Format (little-endian):
    b"PBTP"                magic
    uint32                 header length N
    N bytes                JSON header: types, layers [{z, end}], segments, bounds, truncated
    padding to 4 bytes
    float32[segments * 4]  x1, y1, x2, y2 per segment
    uint8[segments]        feature type index per segment

Works with PrusaSlicer output and generic Marlin G-code (Cura, Creality Print):
layers are detected from the Z height extrusion happens at, and feature types
from ";TYPE:" comments where present.
"""

from __future__ import annotations

import json
import struct
from array import array
from pathlib import Path

MAX_SEGMENTS = 4_000_000
LAYER_EPS = 1e-4


def parse(path: Path) -> tuple[dict, array, array]:
    coords = array("f")
    types = array("B")
    type_names: list[str] = ["Extrusion"]
    type_index = {"Extrusion": 0}
    cur_type = 0
    layers: list[dict] = []
    layer_z: float | None = None

    x = y = z = e = 0.0
    abs_xyz, abs_e = True, True
    truncated = False
    nseg = 0

    with path.open("r", errors="replace") as fh:
        for raw in fh:
            c0 = raw[:1]
            if c0 == ";":
                if raw.startswith(";TYPE:"):
                    name = raw[6:].strip() or "Extrusion"
                    if name not in type_index:
                        if len(type_names) >= 255:
                            name = "Extrusion"
                        else:
                            type_index[name] = len(type_names)
                            type_names.append(name)
                    cur_type = type_index[name]
                continue
            if c0 not in "GM":
                continue
            line = raw.split(";", 1)[0]
            words = line.split()
            if not words:
                continue
            cmd = words[0]
            if cmd in ("G1", "G0", "G2", "G3"):
                nx, ny, nz, ne = x, y, z, None
                for w in words[1:]:
                    k = w[:1]
                    try:
                        v = float(w[1:])
                    except ValueError:
                        continue
                    if k == "X":
                        nx = v if abs_xyz else x + v
                    elif k == "Y":
                        ny = v if abs_xyz else y + v
                    elif k == "Z":
                        nz = v if abs_xyz else z + v
                    elif k == "E":
                        ne = v
                extruding = False
                if ne is not None:
                    de = ne - e if abs_e else ne
                    e = ne if abs_e else e + ne
                    extruding = de > 0 and (nx != x or ny != y)
                if extruding and cmd != "G0":
                    if layer_z is None or abs(nz - layer_z) > LAYER_EPS:
                        if layers:
                            layers[-1]["end"] = nseg
                        layer_z = nz
                        layers.append({"z": round(nz, 4), "end": nseg})
                    if nseg >= MAX_SEGMENTS:
                        truncated = True
                        break
                    coords.extend((x, y, nx, ny))
                    types.append(cur_type)
                    nseg += 1
                x, y, z = nx, ny, nz
            elif cmd == "G92":
                for w in words[1:]:
                    try:
                        v = float(w[1:])
                    except ValueError:
                        continue
                    k = w[:1]
                    if k == "E":
                        e = v
                    elif k == "X":
                        x = v
                    elif k == "Y":
                        y = v
                    elif k == "Z":
                        z = v
            elif cmd == "G90":
                abs_xyz = True
            elif cmd == "G91":
                abs_xyz = False
            elif cmd == "M82":
                abs_e = True
            elif cmd == "M83":
                abs_e = False

    if layers:
        layers[-1]["end"] = nseg
    xs, ys = coords[0::2], coords[1::2]
    bounds = [min(xs), min(ys), max(xs), max(ys)] if nseg else [0, 0, 0, 0]
    header = {
        "types": type_names,
        "layers": layers,
        "segments": nseg,
        "bounds": [round(b, 3) for b in bounds],
        "truncated": truncated,
    }
    return header, coords, types


def build(gcode: Path, out: Path) -> Path:
    header, coords, types = parse(gcode)
    blob = json.dumps(header, separators=(",", ":")).encode()
    pad = (-(8 + len(blob))) % 4
    tmp = out.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        fh.write(b"PBTP")
        fh.write(struct.pack("<I", len(blob) + pad))
        fh.write(blob + b" " * pad)
        if struct.pack("=I", 1) != struct.pack("<I", 1):
            coords.byteswap()
        coords.tofile(fh)
        types.tofile(fh)
    tmp.replace(out)
    return out
