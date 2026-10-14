"""Jobs: upload, import, combine, slicing, previews, sending, thumbnails and calibration prints."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from .. import calibration, combine, importer, profiles, spools, toolpaths
from ..jobs import IDLE_STATES, Job
from ..monitor import ACTIVE
from ..runtime import (
    ACCEPTED, MAX_THUMBNAIL, MAX_UPLOAD, _finish_job, _read_upload, add_job, host, model_input, require_job,
    run_send, run_slice, spawn, state, validate_options,
)
from ..slicer import SliceOptions, SlicerError, export_preview

router = APIRouter()


# ---- jobs -----------------------------------------------------------------


@router.get("/api/jobs")
async def list_jobs() -> list[Job]:
    return state.jobs.list()


@router.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile = File(...), options: str = Form("{}")) -> Job:
    """Upload a model (or G-code) and slice it. A zip creates one job per model inside; the first is returned."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ACCEPTED:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}; expected one of {', '.join(sorted(ACCEPTED))}")
    try:
        opts = SliceOptions.model_validate_json(options)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    if ext != ".gcode":
        validate_options(opts)

    if ext == ".zip":
        data = await _read_upload(file)
        try:
            files = importer.from_zip(data)
        except (importer.ImportError_, ValueError) as e:
            raise HTTPException(422, str(e))
        jobs = [add_job(name, blob, opts) for name, blob in files]
        return jobs[0]

    job = state.jobs.create(file.filename or f"model{ext}", None if ext == ".gcode" else opts)
    dest = state.jobs.source_path(job)
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                state.jobs.delete(job)
                raise HTTPException(413, "File too large")
            fh.write(chunk)
    return _finish_job(job, ext, opts)


class ImportRequest(BaseModel):
    url: str = Field(..., max_length=2000)
    options: SliceOptions = SliceOptions()


@router.post("/api/jobs/import", status_code=201)
async def import_job(req: ImportRequest) -> list[Job]:
    validate_options(req.options)
    try:
        files = await importer.download(req.url)
    except importer.ImportError_ as e:
        raise HTTPException(422, str(e))
    return [add_job(name, data, req.options) for name, data in files]


class CombineRequest(BaseModel):
    job_ids: list[str] = Field(..., min_length=2, max_length=20)
    name: str = Field("combined", max_length=80)
    options: SliceOptions = SliceOptions()


@router.post("/api/jobs/combine", status_code=201)
async def combine_jobs(req: CombineRequest) -> Job:
    """Lay several models out on one bed as a new job."""
    validate_options(req.options)
    sources = [require_job(i) for i in req.job_ids]
    if any(j.options is None for j in sources):
        raise HTTPException(400, "Uploaded G-code can't be combined; choose model jobs")
    meshes = []
    async with state.preview_lock:
        for j in sources:
            stl = state.jobs.dir(j) / "combine.stl"
            try:
                # Each model keeps its own scale, rotation and copies.
                await export_preview(state.settings.slicer_path, model_input(j), stl,
                                     j.options.model_copy(update={"arrange": True}))
                meshes.append(combine.read_stl(stl, j.name))
            except (SlicerError, combine.CombineError) as e:
                raise HTTPException(422, f"{j.name}: {e}")
            finally:
                stl.unlink(missing_ok=True)
    opts = req.options.model_copy(update={"scale": 100, "rotate": 0, "copies": 1, "plate": None, "arrange": True})
    job = state.jobs.create(f"{req.name}.3mf", opts)
    try:
        await asyncio.to_thread(combine.combine, meshes, state.jobs.source_path(job))
    except combine.CombineError as e:
        state.jobs.delete(job)
        raise HTTPException(422, str(e))
    spawn(run_slice(job))
    return job


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> Job:
    return require_job(job_id)


@router.post("/api/jobs/{job_id}/reslice")
async def reslice(job_id: str, options: SliceOptions) -> Job:
    job = require_job(job_id)
    if job.options is None:
        raise HTTPException(400, "Uploaded G-code cannot be re-sliced")
    if job.state not in IDLE_STATES:
        raise HTTPException(409, f"Job is {job.state}")
    validate_options(options)
    if job.plates:
        ids = {p.id for p in job.plates}
        plate = options.plate if options.plate in ids else job.options.plate
        options = options.model_copy(update={"plate": plate})
    else:
        options = options.model_copy(update={"plate": None})
    state.jobs.update(job, options=options, state="queued", stats=None, error=None, sent_at=None,
                      started_print=False, finished_at=None)
    spawn(run_slice(job))
    return job


@router.get("/api/jobs/{job_id}/gcode")
async def download_gcode(job_id: str) -> FileResponse:
    job = require_job(job_id)
    path = state.jobs.gcode_path(job)
    if not path.exists():
        raise HTTPException(404, "G-code not available")
    return FileResponse(path, media_type="text/x-gcode", filename=job.gcode_name)


@router.get("/api/jobs/{job_id}/preview.stl")
async def model_preview(job_id: str) -> FileResponse:
    job = require_job(job_id)
    if job.options is None:
        raise HTTPException(404, "No 3D model for uploaded G-code")
    path = state.jobs.preview_path(job)
    if not path.exists():
        async with state.preview_lock:
            if not path.exists():
                try:
                    await export_preview(state.settings.slicer_path, model_input(job), path, job.options)
                except SlicerError as e:
                    raise HTTPException(422, str(e))
    return FileResponse(path, media_type="model/stl", filename=f"{job.name}.stl")


@router.get("/api/jobs/{job_id}/toolpaths")
async def job_toolpaths(job_id: str) -> FileResponse:
    job = require_job(job_id)
    gcode = state.jobs.gcode_path(job)
    if not gcode.exists():
        raise HTTPException(404, "This job has not been sliced")
    path = state.jobs.toolpath_path(job)
    if not path.exists() or path.stat().st_mtime < gcode.stat().st_mtime:
        async with state.preview_lock:
            if not path.exists() or path.stat().st_mtime < gcode.stat().st_mtime:
                await asyncio.to_thread(toolpaths.build, gcode, path)
    return FileResponse(path, media_type="application/octet-stream")


class SendRequest(BaseModel):
    start: bool = True
    force: bool = Field(False, description="Start even if the printer reports it is busy")
    ignore_spool: bool = Field(False, description="Start even if the loaded spool looks too low or is another material")


@router.post("/api/jobs/{job_id}/send")
async def send_job(job_id: str, req: SendRequest) -> Job:
    job = require_job(job_id)
    if job.state not in ("ready", "sent", "printed", "stopped"):
        raise HTTPException(409, f"Job is {job.state}; only sliced jobs can be sent")
    host()  # validate printer config before going async
    if req.start:
        st = await state.monitor.current(max_age=2)
        if not st.online:
            raise HTTPException(409, f"Printer is {st.state.lower()}: {st.message or 'not reachable'}")
        if st.state in ACTIVE and not req.force:
            raise HTTPException(409, f"Printer is busy ({st.state.lower()} {st.file or 'a job'}). "
                                     "Wait for it to finish, or upload without starting.")
        if not req.ignore_spool:
            problems = spools.check(job.stats.filament_g if job.stats else None,
                                    job.options.material if job.options else None)
            if problems:
                raise HTTPException(409, {"code": "spool", "message": " ".join(problems)})
    spawn(run_send(job, req.start))
    return state.jobs.update(job, state="sending")


@router.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> None:
    job = require_job(job_id)
    if job.state in ("queued", "slicing", "sending"):
        raise HTTPException(409, f"Job is {job.state}")
    state.jobs.delete(job)


# ---- thumbnails -------------------------------------------------------------


@router.put("/api/jobs/{job_id}/thumbnail", status_code=204)
async def put_thumbnail(job_id: str, request: Request) -> None:
    """Store a PNG thumbnail rendered by the browser from the job's 3D preview."""
    job = require_job(job_id)
    data = await request.body()
    if len(data) > MAX_THUMBNAIL or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Expected a PNG image under 1 MB")
    state.jobs.thumbnail_path(job).write_bytes(data)
    state.jobs.update(job, thumbnail=True)


@router.get("/api/jobs/{job_id}/thumbnail.png")
async def get_thumbnail(job_id: str) -> FileResponse:
    path = state.jobs.thumbnail_path(require_job(job_id))
    if not path.exists():
        raise HTTPException(404, "No thumbnail yet")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


# ---- calibration --------------------------------------------------------------


@router.get("/api/calibration/defaults")
async def calibration_defaults(kind: calibration.Kind, material: str = "pla") -> dict:
    d = calibration.defaults(kind, material)
    return {"start": d[0], "end": d[1], "step": d[2]} if d else {}


class CalibrationRequest(BaseModel):
    kind: calibration.Kind
    material: str = "pla"
    quality: str = "standard"
    start: float = 0
    end: float = 0
    step: float = 0


@router.post("/api/calibration", status_code=201)
async def create_calibration(req: CalibrationRequest) -> Job:
    opts = SliceOptions(material=req.material, quality=req.quality, infill=15)
    validate_options(opts)
    if req.kind == "temperature":
        opts.nozzle_temp = int(req.start)
    if req.kind == "first_layer":
        opts.arrange = False  # the pads are placed at fixed bed positions
    if req.kind == "dimension":
        opts.infill = 20
    try:
        flh = float(profiles.read_values(profiles.resolve("quality", req.quality)).get("first_layer_height", 0.24))
        spec = calibration.CalibrationSpec(kind=req.kind, start=req.start, end=req.end, step=req.step,
                                           first_layer_height=flh)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors()))
    title = {"temperature": "temperature-tower", "retraction": "retraction-test", "first_layer": "first-layer-test",
             "dimension": "20mm-cube"}[req.kind]
    job = state.jobs.create(f"{title}-{req.material}.stl", opts)
    spec = calibration.generate(spec, state.jobs.source_path(job))
    state.jobs.update(job, calibration=spec)
    spawn(run_slice(job))
    return job


class CalibrationResult(BaseModel):
    value: float


@router.post("/api/jobs/{job_id}/calibration/apply")
async def apply_calibration(job_id: str, req: CalibrationResult) -> dict:
    """Save the chosen band's value into the job's material profile."""
    job = require_job(job_id)
    if not job.calibration or not job.options:
        raise HTTPException(400, "This job is not a calibration print")
    if req.value not in [b.value for b in job.calibration.bands]:
        raise HTTPException(422, "Choose one of the printed values")
    try:
        values = calibration.result_values(job.calibration.kind, req.value)
        profiles.set_values("material", job.options.material, values)
    except (ValueError, profiles.ProfileError) as e:
        raise HTTPException(422, str(e))
    return {"material": job.options.material, "saved": values}
