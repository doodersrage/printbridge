"""Combine several models onto one bed.

PrusaSlicer's own --merge keeps each model's original position (and crashes on
some inputs), so the layout is done here: each model is exported as an STL with
its own scale/rotation/copies applied, the footprints are packed into rows on
the 220 x 220 mm bed, and the result is written as one multi-object 3MF.
"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

BED = 220.0
SPACING = 6.0
MARGIN = 5.0


class CombineError(ValueError):
    pass


@dataclass
class Mesh:
    name: str
    vertices: list[tuple[float, float, float]]
    triangles: list[tuple[int, int, int]]

    def bounds(self) -> tuple[float, float, float, float, float]:
        xs = [v[0] for v in self.vertices]
        ys = [v[1] for v in self.vertices]
        zs = [v[2] for v in self.vertices]
        return min(xs), min(ys), max(xs), max(ys), min(zs)


def stl_size(path: Path) -> list[float]:
    """Bounding-box size (W, D, H) of a binary STL without building a mesh."""
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if not count or len(data) != 84 + count * 50:
        raise CombineError("expected a binary STL")
    lo, hi = [float("inf")] * 3, [float("-inf")] * 3
    for rec in struct.iter_unpack("<12fH", data[84:]):
        for k in range(3):
            a, b, c = rec[3 + k], rec[6 + k], rec[9 + k]
            lo[k] = min(lo[k], a, b, c)
            hi[k] = max(hi[k], a, b, c)
    return [round(hi[k] - lo[k], 2) for k in range(3)]


def read_stl(path: Path, name: str) -> Mesh:
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else 0
    if len(data) != 84 + count * 50:
        raise CombineError(f"{name}: expected a binary STL")
    index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles = []
    for i in range(count):
        v = struct.unpack_from("<9f", data, 84 + i * 50 + 12)
        tri = []
        for j in range(3):
            key = (round(v[j * 3], 5), round(v[j * 3 + 1], 5), round(v[j * 3 + 2], 5))
            if key not in index:
                index[key] = len(vertices)
                vertices.append(key)
            tri.append(index[key])
        if len(set(tri)) == 3:
            triangles.append(tuple(tri))
    if not triangles:
        raise CombineError(f"{name}: model is empty")
    return Mesh(name, vertices, triangles)  # type: ignore[arg-type]


def pack(sizes: list[tuple[float, float]], bed: float = BED) -> list[tuple[float, float]]:
    """Shelf-pack footprints (w, d) onto the bed; returns the lower-left corner of each."""
    usable = bed - 2 * MARGIN
    for i, (w, d) in enumerate(sizes):
        if w > usable or d > usable:
            raise CombineError(f"Model {i + 1} is {w:.0f} × {d:.0f} mm and does not fit on the bed")
    order = sorted(range(len(sizes)), key=lambda i: -sizes[i][1])
    pos: dict[int, tuple[float, float]] = {}
    x = y = row_d = 0.0
    for i in order:
        w, d = sizes[i]
        if x > 0 and x + w > usable:
            x, y = 0.0, y + row_d + SPACING
            row_d = 0.0
        if y + d > usable:
            raise CombineError("The models do not all fit on one 220 × 220 mm bed")
        pos[i] = (x, y)
        x += w + SPACING
        row_d = max(row_d, d)
    # Centre the whole layout on the bed.
    width = max(pos[i][0] + sizes[i][0] for i in pos)
    depth = max(pos[i][1] + sizes[i][1] for i in pos)
    ox, oy = (bed - width) / 2, (bed - depth) / 2
    return [(pos[i][0] + ox, pos[i][1] + oy) for i in range(len(sizes))]


def write_3mf(out: Path, meshes: list[Mesh], offsets: list[tuple[float, float, float]]) -> Path:
    objects, items = [], []
    for n, (mesh, (dx, dy, dz)) in enumerate(zip(meshes, offsets), start=1):
        verts = "".join(f'<vertex x="{x:.5f}" y="{y:.5f}" z="{z:.5f}"/>' for x, y, z in mesh.vertices)
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.triangles)
        name = escape(mesh.name, {'"': "&quot;"})
        objects.append(f'<object id="{n}" name="{name}" type="model"><mesh><vertices>{verts}</vertices>'
                       f"<triangles>{tris}</triangles></mesh></object>")
        items.append(f'<item objectid="{n}" transform="1 0 0 0 1 0 0 0 1 {dx:.4f} {dy:.4f} {dz:.4f}"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f"<resources>{''.join(objects)}</resources><build>{''.join(items)}</build></model>")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
    return out


def combine(meshes: list[Mesh], out: Path) -> Path:
    """Lay the meshes out on the bed and write them to one 3MF."""
    if len(meshes) < 2:
        raise CombineError("Choose at least two models to combine")
    bounds = [m.bounds() for m in meshes]
    sizes = [(b[2] - b[0], b[3] - b[1]) for b in bounds]
    corners = pack(sizes)
    offsets = [(cx - b[0], cy - b[1], -b[4]) for (cx, cy), b in zip(corners, bounds)]
    return write_3mf(out, meshes, offsets)
