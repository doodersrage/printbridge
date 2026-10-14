"""Multi-plate 3MF support (Bambu Studio / OrcaSlicer project files).

Those files put every plate's objects into one build, laid out side by side on a
virtual bed, and record which objects belong to which plate in
Metadata/model_settings.config. PrusaSlicer loads all of them at once, so to
print one plate we write a copy of the 3MF whose build only contains that
plate's items. The XML is edited textually to keep namespace prefixes such as
p:path exactly as the source wrote them.
"""

from __future__ import annotations

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
