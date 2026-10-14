"""Slicer profiles: built-in defaults that can be overridden or extended from the data directory.

Overrides live in <data>/profiles/ with the same layout as the built-in profiles:

    printer.ini            machine profile (start/end G-code, limits, retraction)
    quality/<name>.ini     layer height, speeds, shells
    material/<name>.ini    temperatures, cooling, filament

A file there replaces the built-in profile of the same name; a new name adds a profile.
The first comment line of a quality/material file is its label in the UI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from . import config

BUILTIN_DIR = Path(__file__).parent / "profiles"
BUILTIN_PRINTER = "ender3v3se.ini"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
Kind = Literal["printer", "quality", "material"]
KINDS: tuple[Kind, ...] = ("printer", "quality", "material")
_LINE_RE = re.compile(r"^\s*(#.*|;.*|[A-Za-z0-9_]+\s*=.*)?$")


class ProfileError(ValueError):
    pass


def override_dir() -> Path:
    return config.DATA_DIR / "profiles"


def _rel(kind: Kind, name: str) -> Path:
    if kind == "printer":
        return Path("printer.ini")
    if not NAME_RE.match(name):
        raise ProfileError("Profile names may contain lowercase letters, digits, - and _")
    return Path(kind) / f"{name}.ini"


def builtin_path(kind: Kind, name: str) -> Path:
    if kind == "printer":
        return BUILTIN_DIR / BUILTIN_PRINTER
    return BUILTIN_DIR / _rel(kind, name)


def override_path(kind: Kind, name: str) -> Path:
    return override_dir() / _rel(kind, name)


def resolve(kind: Kind, name: str = "") -> Path:
    """The profile file to use: the override if present, otherwise the built-in one."""
    override = override_path(kind, name)
    if override.exists():
        return override
    builtin = builtin_path(kind, name)
    if builtin.exists():
        return builtin
    raise ProfileError(f"Unknown {kind} profile: {name}")


def label(path: Path) -> str:
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("#"):
            return line.lstrip("# ").strip()
        if line.strip():
            break
    return path.stem


def names(kind: Kind) -> list[str]:
    if kind == "printer":
        return ["printer"]
    found = {p.stem for p in (BUILTIN_DIR / kind).glob("*.ini")}
    found |= {p.stem for p in (override_dir() / kind).glob("*.ini")}
    return sorted(n for n in found if NAME_RE.match(n))


def choices(kind: Kind) -> dict[str, str]:
    """name -> label, keeping the built-in order (draft/standard/fine) where it applies."""
    order = {"draft": 0, "standard": 1, "fine": 2, "pla": 0, "petg": 1, "tpu": 2}
    return {n: label(resolve(kind, n)) for n in sorted(names(kind), key=lambda n: (order.get(n, 99), n))}


def read_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(errors="replace").splitlines():
        if "=" in line and not line.lstrip().startswith(("#", ";")):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def material_temps(name: str) -> dict[str, int]:
    v = read_values(resolve("material", name))

    def first(*keys: str, default: int) -> int:
        for k in keys:
            try:
                return int(float(v[k].split(",")[0]))
            except (KeyError, ValueError):
                continue
        return default

    return {
        "nozzle": first("first_layer_temperature", "temperature", default=200),
        "bed": first("first_layer_bed_temperature", "bed_temperature", default=60),
    }


def validate_text(text: str) -> None:
    if len(text) > 200_000:
        raise ProfileError("Profile is too large")
    for n, line in enumerate(text.splitlines(), 1):
        if not _LINE_RE.match(line):
            raise ProfileError(f"Line {n} is not 'key = value' or a comment: {line[:60]}")


def save_override(kind: Kind, name: str, text: str) -> Path:
    validate_text(text)
    path = override_path(kind, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n")
    return path


def remove_override(kind: Kind, name: str) -> None:
    override_path(kind, name).unlink(missing_ok=True)


def describe(kind: Kind, name: str) -> dict:
    builtin = builtin_path(kind, name)
    override = override_path(kind, name)
    active = resolve(kind, name)
    return {
        "kind": kind,
        "name": name,
        "label": label(active),
        "builtin": builtin.exists(),
        "overridden": override.exists(),
        "text": active.read_text(errors="replace"),
        "builtin_text": builtin.read_text(errors="replace") if builtin.exists() else None,
    }
