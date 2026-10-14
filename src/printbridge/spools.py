"""Filament spool inventory: how much is left on the loaded spool."""

from __future__ import annotations

import json
import time
import uuid

from pydantic import BaseModel, Field, TypeAdapter

from . import config, profiles

LOW_GRAMS = 50


class Spool(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = Field(..., min_length=1, max_length=60)
    material: str = Field("pla", pattern=profiles.NAME_RE.pattern)
    total_g: float = Field(1000, gt=0, le=20000)
    remaining_g: float = Field(1000, ge=0, le=20000)
    loaded: bool = False
    created: float = Field(default_factory=time.time)


_ADAPTER = TypeAdapter(list[Spool])


def _path():
    return config.DATA_DIR / "spools.json"


def load() -> list[Spool]:
    path = _path()
    return _ADAPTER.validate_json(path.read_text()) if path.exists() else []


def save(spools: list[Spool]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([s.model_dump() for s in spools], indent=2))
    tmp.replace(path)


def loaded() -> Spool | None:
    return next((s for s in load() if s.loaded), None)


def upsert(spool: Spool, make_loaded: bool = False) -> list[Spool]:
    spools = [s for s in load() if s.id != spool.id]
    if make_loaded or spool.loaded:
        for s in spools:
            s.loaded = False
        spool.loaded = True
    spools.append(spool)
    spools.sort(key=lambda s: (not s.loaded, s.created))
    save(spools)
    return spools


def delete(spool_id: str) -> list[Spool]:
    spools = [s for s in load() if s.id != spool_id]
    save(spools)
    return spools


def consume(grams: float) -> Spool | None:
    """Subtract filament used by a print from the loaded spool."""
    spools = load()
    spool = next((s for s in spools if s.loaded), None)
    if spool is None or grams <= 0:
        return spool
    spool.remaining_g = round(max(0.0, spool.remaining_g - grams), 1)
    save(spools)
    return spool


def check(needed_g: float | None, material: str | None) -> list[str]:
    """Problems with printing a job from the loaded spool (empty if fine or untracked)."""
    spool = loaded()
    if spool is None:
        return []
    problems = []
    if needed_g and needed_g > spool.remaining_g:
        problems.append(f"The loaded spool ({spool.name}) has about {spool.remaining_g:.0f} g left; "
                        f"this print needs {needed_g:.0f} g.")
    if material and material != spool.material:
        problems.append(f"This job is sliced for {material.upper()}, but the loaded spool is {spool.material.upper()}.")
    return problems
