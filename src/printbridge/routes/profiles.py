"""Slicer profiles, profile files and presets."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import presets, profiles
from ..runtime import ACCEPTED, state, validate_options
from ..slicer import SliceOptions

router = APIRouter()


# ---- profiles & presets ---------------------------------------------------


@router.get("/api/profiles")
async def get_profiles() -> dict:
    materials = profiles.choices("material")
    return {
        "qualities": profiles.choices("quality"),
        "materials": materials,
        "preheat": {m: profiles.material_temps(m) for m in materials},
        "defaults": SliceOptions().model_dump(),
        "accepted": sorted(ACCEPTED),
        "slicer_found": bool(state.settings.slicer_path),
    }


@router.get("/api/profile-files")
async def list_profile_files() -> list[dict]:
    out = []
    for kind in profiles.KINDS:
        for name in profiles.choices(kind) if kind != "printer" else ["printer"]:
            d = profiles.describe(kind, name)
            out.append({k: d[k] for k in ("kind", "name", "label", "builtin", "overridden")})
    return out


@router.get("/api/profile-files/{kind}/{name}")
async def get_profile_file(kind: profiles.Kind, name: str) -> dict:
    try:
        return profiles.describe(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(404, str(e))


class ProfileText(BaseModel):
    text: str


@router.put("/api/profile-files/{kind}/{name}")
async def put_profile_file(kind: profiles.Kind, name: str, body: ProfileText) -> dict:
    try:
        profiles.save_override(kind, name, body.text)
        return profiles.describe(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))


@router.delete("/api/profile-files/{kind}/{name}")
async def delete_profile_file(kind: profiles.Kind, name: str) -> dict:
    """Reset a built-in profile to its default, or delete a custom one."""
    try:
        profiles.remove_override(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))
    if profiles.builtin_path(kind, name).exists():
        return profiles.describe(kind, name)
    return {"deleted": True}


@router.get("/api/presets")
async def list_presets() -> dict[str, SliceOptions]:
    return presets.load()


@router.put("/api/presets/{name}")
async def put_preset(name: str, options: SliceOptions) -> dict[str, SliceOptions]:
    if not presets.NAME_RE.match(name):
        raise HTTPException(422, "Invalid preset name")
    validate_options(options)
    data = presets.load()
    data[name] = options.model_copy(update={"plate": None})
    presets.save(data)
    return data


@router.delete("/api/presets/{name}")
async def delete_preset(name: str) -> dict[str, SliceOptions]:
    data = presets.load()
    data.pop(name, None)
    presets.save(data)
    return data
