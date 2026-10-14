"""Multi-plate 3MF support (Bambu Studio / OrcaSlicer project files).

Those files put every plate's objects into one build, laid out side by side on a
virtual bed, and record which objects belong to which plate in
Metadata/model_settings.config. PrusaSlicer loads all of them at once, so to
print one plate we write a copy of the 3MF whose build only contains that
plate's items. The XML is edited textually to keep namespace prefixes such as
p:path exactly as the source wrote them.
"""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel

MODEL_SETTINGS = "Metadata/model_settings.config"
MAIN_MODEL = "3D/3dmodel.model"
_ITEM_RE = re.compile(r"<item\b[^>]*?(?:/>|>.*?</item\s*>)", re.S)
_ATTR_RE = re.compile(r'\bobjectid\s*=\s*"([^"]+)"')
_OBJECT_RE = re.compile(r"<object\b[^>]*?(?:/>|>.*?</object\s*>)", re.S)
_ID_RE = re.compile(r'\bid\s*=\s*"([^"]+)"')
_COMPONENT_RE = re.compile(r"<component\b[^>]*>")


class Plate(BaseModel):
    id: int
    name: str = ""
    objects: list[str] = []


class ThreeMFError(ValueError):
    pass


def _meta(el: ET.Element) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in el.findall("metadata")}


def read_plates(path: Path) -> list[Plate]:
    """Plates in a Bambu/Orca 3MF, or [] if the file has fewer than two."""
    try:
        with zipfile.ZipFile(path) as z:
            if MODEL_SETTINGS not in z.namelist():
                return []
            root = ET.fromstring(z.read(MODEL_SETTINGS))
    except (zipfile.BadZipFile, ET.ParseError, OSError):
        return []

    names = {o.get("id", ""): _meta(o).get("name", "") for o in root.findall("object")}
    plates = []
    for el in root.findall("plate"):
        meta = _meta(el)
        try:
            pid = int(meta.get("plater_id", ""))
        except ValueError:
            continue
        objs = [_meta(mi).get("object_id", "") for mi in el.findall("model_instance")]
        plates.append(
            Plate(id=pid, name=meta.get("plater_name", ""), objects=[names.get(o) or f"Object {o}" for o in objs])
        )
    return sorted(plates, key=lambda p: p.id) if len(plates) > 1 else []


def _plate_instances(root: ET.Element, plate_id: int) -> set[tuple[str, int]]:
    for el in root.findall("plate"):
        if _meta(el).get("plater_id") == str(plate_id):
            out = set()
            for mi in el.findall("model_instance"):
                m = _meta(mi)
                try:
                    out.add((m.get("object_id", ""), int(m.get("instance_id", "0"))))
                except ValueError:
                    continue
            return out
    raise ThreeMFError(f"Plate {plate_id} not found")


def filter_build(model_xml: str, keep: set[tuple[str, int]]) -> str:
    """Drop <item> elements whose (objectid, nth occurrence) is not in keep."""
    seen: dict[str, int] = defaultdict(int)
    build_start = model_xml.find("<build")
    if build_start < 0:
        raise ThreeMFError("3MF has no build section")

    def repl(m: re.Match) -> str:
        oid_m = _ATTR_RE.search(m.group(0))
        if not oid_m:
            return m.group(0)
        oid = oid_m.group(1)
        idx = seen[oid]
        seen[oid] += 1
        return m.group(0) if (oid, idx) in keep else ""

    head, body = model_xml[:build_start], model_xml[build_start:]
    body = _ITEM_RE.sub(repl, body)
    return _prune_objects(head, {oid for oid, _ in keep}) + body


def _prune_objects(resources_xml: str, roots: set[str]) -> str:
    """Remove object definitions that nothing in the kept build references.

    PrusaSlicer refuses to load a 3MF that defines objects no build item uses.
    Components pointing into other files (p:path) are resolved there, so only
    same-file component references keep an object alive.
    """
    objects = {(_ID_RE.search(m.group(0)) or [None, None])[1]: m.group(0) for m in _OBJECT_RE.finditer(resources_xml)}
    alive, todo = set(), list(roots)
    while todo:
        oid = todo.pop()
        if oid in alive or oid not in objects:
            continue
        alive.add(oid)
        for comp in _COMPONENT_RE.findall(objects[oid]):
            if "path=" not in comp and (ref := _ATTR_RE.search(comp)):
                todo.append(ref.group(1))

    def repl(m: re.Match) -> str:
        oid = (_ID_RE.search(m.group(0)) or [None, None])[1]
        return m.group(0) if oid in alive else ""

    return _OBJECT_RE.sub(repl, resources_xml)


_PATH_RE = re.compile(r'\bp:path\s*=\s*"([^"]+)"')
_REL_RE = re.compile(r"<Relationship\b[^>]*/>", re.S)
_TARGET_RE = re.compile(r'\bTarget\s*=\s*"([^"]+)"')
MAIN_RELS = "3D/_rels/3dmodel.model.rels"


def extract_plate(src: Path, dst: Path, plate_id: int) -> Path:
    with zipfile.ZipFile(src) as z:
        root = ET.fromstring(z.read(MODEL_SETTINGS))
        keep = _plate_instances(root, plate_id)
        if not keep:
            raise ThreeMFError(f"Plate {plate_id} is empty")
        model = filter_build(z.read(MAIN_MODEL).decode("utf-8"), keep)

        # PrusaSlicer loads every .model part in the archive, so drop the mesh
        # files that only the other plates' objects referenced.
        used = {p.lstrip("/") for p in _PATH_RE.findall(model)}
        dropped = {
            n for n in z.namelist()
            if n.startswith("3D/") and n.endswith(".model") and n != MAIN_MODEL and n not in used
        }

        tmp = dst.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                if info.filename in dropped:
                    continue
                if info.filename == MAIN_MODEL:
                    out.writestr(info, model.encode("utf-8"))
                elif info.filename == MAIN_RELS:
                    rels = z.read(info).decode("utf-8")
                    rels = _REL_RE.sub(
                        lambda m: "" if (t := _TARGET_RE.search(m.group(0))) and t.group(1).lstrip("/") in dropped else m.group(0),
                        rels,
                    )
                    out.writestr(info, rels.encode("utf-8"))
                else:
                    with z.open(info) as fh, out.open(info, "w") as oh:
                        shutil.copyfileobj(fh, oh)
    tmp.replace(dst)
    return dst


# ---- inspection: what was this 3MF made for? ------------------------------------

_TRANSFORM_RE = re.compile(r'(<(?:item|component)\b[^>]*?\btransform\s*=\s*")([^"]+)(")', re.S)
_UNIT_RE = re.compile(r'<model\b[^>]*\bunit\s*=\s*"([^"]+)"')
_APP_RE = re.compile(r'<metadata\s+name\s*=\s*"Application"\s*>([^<]+)<')
OUR_BED = (220.0, 220.0)


class SourceInfo(BaseModel):
    application: str | None = None
    printer: str | None = None
    bed: tuple[float, float] | None = None
    units: str = "millimeter"
    scale: float | None = None  # stored uniform scale factor, when not 1
    warnings: list[str] = []


def _matrix(text: str) -> list[float] | None:
    try:
        m = [float(x) for x in text.split()]
    except ValueError:
        return None
    return m if len(m) == 12 else None


def _row_scales(m: list[float]) -> tuple[float, float, float]:
    # 3MF matrices are row-major with row vectors: rows 0-2 are the transformed axes.
    return tuple((m[i] ** 2 + m[i + 1] ** 2 + m[i + 2] ** 2) ** 0.5 for i in (0, 3, 6))  # type: ignore[return-value]


def _bed_from_points(points: list[str]) -> tuple[float, float] | None:
    try:
        xy = [tuple(float(v) for v in p.split("x")) for p in points]
        xs, ys = [p[0] for p in xy], [p[1] for p in xy]
        return (round(max(xs) - min(xs), 1), round(max(ys) - min(ys), 1))
    except (ValueError, IndexError):
        return None


def stored_scales(z: zipfile.ZipFile) -> list[float]:
    """Uniform scale factors stored in item/component transforms (1.0 means unscaled)."""
    out = []
    for name in z.namelist():
        if name.startswith("3D/") and name.endswith(".model"):
            for m in _TRANSFORM_RE.finditer(z.read(name).decode("utf-8", errors="replace")):
                mat = _matrix(m.group(2))
                if mat:
                    sx, sy, sz = _row_scales(mat)
                    out.append(round((sx * sy * sz) ** (1 / 3), 4))
    return out


def inspect(path: Path) -> SourceInfo:
    info = SourceInfo()
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return info
    with z:
        names = set(z.namelist())
        if MAIN_MODEL in names:
            head = z.read(MAIN_MODEL)[:4096].decode("utf-8", errors="replace")
            if m := _UNIT_RE.search(head):
                info.units = m.group(1)
            if m := _APP_RE.search(head):
                info.application = m.group(1).strip()

        # Bambu Studio / OrcaSlicer project settings (JSON)
        if "Metadata/project_settings.config" in names:
            try:
                cfg = json.loads(z.read("Metadata/project_settings.config"))
                info.printer = cfg.get("printer_model") or cfg.get("printer_settings_id") or None
                if isinstance(cfg.get("printable_area"), list):
                    info.bed = _bed_from_points(cfg["printable_area"])
            except (ValueError, AttributeError):
                pass
        # PrusaSlicer project settings ("; key = value" lines)
        if "Metadata/Slic3r_PE.config" in names:
            cfg = {}
            for line in z.read("Metadata/Slic3r_PE.config").decode("utf-8", errors="replace").splitlines():
                if line.startswith("; ") and " = " in line:
                    k, _, v = line[2:].partition(" = ")
                    cfg[k.strip()] = v.strip()
            info.printer = info.printer or cfg.get("printer_model") or cfg.get("printer_settings_id") or None
            if cfg.get("bed_shape"):
                info.bed = info.bed or _bed_from_points(cfg["bed_shape"].split(","))

        scales = [s for s in stored_scales(z) if abs(s - 1) > 0.005]
        if scales:
            info.scale = scales[0] if all(abs(s - scales[0]) < 0.005 for s in scales) else min(scales, key=lambda s: abs(s - 1))

    if info.printer and "ender-3 v3 se" in info.printer.lower():
        info.printer = None  # made for this printer; nothing to warn about
    if info.printer or (info.bed and info.bed != OUR_BED):
        bed = f" ({info.bed[0]:g} × {info.bed[1]:g} mm bed)" if info.bed else ""
        info.warnings.append(f"Made for {info.printer or 'another printer'}{bed}. Check the size in 3D view; "
                             "model sites often scale or rearrange files for a specific printer.")
    if info.scale:
        info.warnings.append(f"This file scales the model to {info.scale * 100:.0f}% of its original size.")
    if info.units != "millimeter":
        info.warnings.append(f"The file uses {info.units} units; it is converted to millimetres.")
    return info


def remove_stored_scale(src: Path, dst: Path) -> Path:
    """Copy a 3MF with the scale removed from every item/component transform.

    Rotation is kept. Translations are divided by the same factor, so parts laid
    out next to each other keep their relative spacing.
    """
    def fix(m: re.Match) -> str:
        mat = _matrix(m.group(2))
        if not mat:
            return m.group(0)
        sx, sy, sz = _row_scales(mat)
        if max(abs(sx - 1), abs(sy - 1), abs(sz - 1)) <= 0.005 or min(sx, sy, sz) == 0:
            return m.group(0)
        s = (sx * sy * sz) ** (1 / 3)
        rows = [v / n for i, n in zip((0, 3, 6), (sx, sy, sz)) for v in mat[i:i + 3]]
        out = rows + [v / s for v in mat[9:12]]
        return m.group(1) + " ".join(f"{v:.6g}" for v in out) + m.group(3)

    with zipfile.ZipFile(src) as z:
        tmp = dst.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
            for info in z.infolist():
                if info.filename.startswith("3D/") and info.filename.endswith(".model"):
                    out.writestr(info, _TRANSFORM_RE.sub(fix, z.read(info).decode("utf-8")).encode("utf-8"))
                else:
                    with z.open(info) as fh, out.open(info, "w") as oh:
                        shutil.copyfileobj(fh, oh)
    tmp.replace(dst)
    return dst
