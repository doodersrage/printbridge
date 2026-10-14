"""Printer status and control, printer storage, webcam relay, spools and print history."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .. import history, spools
from ..monitor import ACTIVE
from ..printers import PrinterError, RemoteFile
from ..runtime import host, state, supports_light

router = APIRouter()


# ---- printer --------------------------------------------------------------


@router.get("/api/printer")
async def printer_status() -> dict:
    st = await state.monitor.current()
    light = supports_light()
    spool = spools.loaded()
    return {**st.model_dump(), "supports_light": light, "babystep_total": state.monitor.babystep_total,
            "webcam": bool(state.settings.webcam_url), "spool": spool.model_dump() if spool else None}


class ControlRequest(BaseModel):
    action: str


@router.post("/api/printer/control")
async def printer_control(req: ControlRequest) -> dict:
    try:
        await host().control(req.action)
    except PrinterError as e:
        raise HTTPException(502, str(e))
    state.monitor.status = None
    return {"ok": True}


class CommandRequest(BaseModel):
    command: Literal["preheat", "cooldown", "home", "fan_on", "fan_off", "light_on", "light_off", "gcode", "babystep"]
    nozzle: int = Field(0, ge=0, le=260)
    bed: int = Field(0, ge=0, le=100)
    gcode: str = Field("", max_length=500)
    z: float = Field(0, ge=-0.5, le=0.5, description="Live Z adjustment in mm for babystep")


# Commands that would interfere with a running print.
BLOCKED_WHILE_PRINTING = {"preheat", "cooldown", "home", "gcode"}


@router.post("/api/printer/command")
async def printer_command(req: CommandRequest) -> dict:
    h = host()
    if req.command in BLOCKED_WHILE_PRINTING:
        st = await state.monitor.current(max_age=2)
        if st.state in ACTIVE:
            raise HTTPException(409, "Not available while the printer is printing")
    try:
        if req.command == "babystep":
            if not req.z:
                raise HTTPException(422, "Enter a non-zero Z adjustment")
            # M290 babysteps Z during a print; positive moves the nozzle away from the bed.
            await h.send_gcode([f"M290 Z{req.z:+.3f}"])
            state.monitor.babystep_total = round(state.monitor.babystep_total + req.z, 3)
            return {"ok": True, "babystep_total": state.monitor.babystep_total}
        if req.command == "gcode":
            lines = [ln.strip() for ln in req.gcode.replace(";", "\n").splitlines() if ln.strip()]
            if not lines:
                raise HTTPException(422, "Enter a G-code command")
            await h.send_gcode(lines)
        else:
            await h.command(req.command, req.nozzle, req.bed)
    except PrinterError as e:
        raise HTTPException(502, str(e))
    state.monitor.status = None
    return {"ok": True}


class PrinterFile(RemoteFile):
    job_id: str | None = None
    printing: bool = False


@router.get("/api/printer/files")
async def printer_files() -> list[PrinterFile]:
    try:
        files = await host().list_files()
    except PrinterError as e:
        raise HTTPException(502, str(e))
    st = state.monitor.status
    current = Path(st.file).name if st and st.state in ACTIVE and st.file else None
    by_name = {j.gcode_name: j.id for j in state.jobs.list() if j.sent_at}
    out = [
        PrinterFile(**f.model_dump(), job_id=by_name.get(Path(f.name).name), printing=Path(f.name).name == current)
        for f in files
    ]
    return sorted(out, key=lambda f: (f.job_id is None, -(f.modified or 0), f.name))


class DeleteFilesRequest(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=500)


@router.post("/api/printer/files/delete")
async def delete_printer_files(req: DeleteFilesRequest) -> dict:
    st = await state.monitor.current(max_age=2)
    current = Path(st.file).name if st.state in ACTIVE and st.file else None
    h = host()
    deleted, errors = [], []
    for name in req.names:
        if Path(name).name == current:
            errors.append(f"{name}: currently printing")
            continue
        try:
            await h.delete_file(name)
            deleted.append(name)
        except PrinterError as e:
            errors.append(str(e))
    return {"deleted": deleted, "errors": errors}


# ---- webcam -------------------------------------------------------------------


@router.get("/api/webcam")
async def webcam() -> Response:
    """Proxy the configured webcam so the browser only ever talks to PrintBridge."""
    url = state.settings.webcam_url
    if not url:
        raise HTTPException(404, "No webcam configured")
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None), follow_redirects=True)
    try:
        resp = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        raise HTTPException(502, f"Webcam unreachable: {e}")
    if resp.status_code >= 400:
        await resp.aclose()
        await client.aclose()
        raise HTTPException(502, f"Webcam returned HTTP {resp.status_code}")

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    # "identity" keeps the gzip middleware from buffering the live stream.
    headers = {"Cache-Control": "no-store", "Content-Encoding": "identity"}
    return StreamingResponse(body(), media_type=resp.headers.get("content-type", "image/jpeg"), headers=headers)


# ---- spools -------------------------------------------------------------------


@router.get("/api/spools")
async def list_spools() -> list[spools.Spool]:
    return spools.load()


class SpoolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    material: str = "pla"
    total_g: float = Field(1000, gt=0, le=20000)
    remaining_g: float | None = Field(None, ge=0, le=20000)
    loaded: bool = False


@router.post("/api/spools", status_code=201)
async def add_spool(req: SpoolRequest) -> list[spools.Spool]:
    try:
        spool = spools.Spool(name=req.name, material=req.material, total_g=req.total_g,
                             remaining_g=req.total_g if req.remaining_g is None else req.remaining_g)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    return spools.upsert(spool, make_loaded=req.loaded)


@router.put("/api/spools/{spool_id}")
async def update_spool(spool_id: str, req: SpoolRequest) -> list[spools.Spool]:
    current = next((s for s in spools.load() if s.id == spool_id), None)
    if current is None:
        raise HTTPException(404, "Spool not found")
    try:
        updated = current.model_copy(update={
            "name": req.name, "material": req.material, "total_g": req.total_g,
            "remaining_g": current.remaining_g if req.remaining_g is None else req.remaining_g,
        })
        spools.Spool.model_validate(updated.model_dump())
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    return spools.upsert(updated, make_loaded=req.loaded)


@router.delete("/api/spools/{spool_id}")
async def delete_spool(spool_id: str) -> list[spools.Spool]:
    return spools.delete(spool_id)


# ---- history & costs ----------------------------------------------------------


@router.get("/api/history")
async def get_history(limit: int = 200) -> dict:
    return history.summary(limit=max(1, min(limit, 2000)))
