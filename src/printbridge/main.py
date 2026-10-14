"""FastAPI application: upload models, slice them for the Ender-3 V3 SE, send to the printer."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import config
from .auth import COOKIE_NAME, PUBLIC_PATHS, AuthConfig, load_auth
from .gcode import parse_stats
from .jobs import Job, JobStore
from .printers import PrinterError, PrinterStatus, make_host
from .slicer import MATERIALS, QUALITIES, SliceOptions, SlicerError, export_preview, slice_model

ACCEPTED = {".3mf", ".stl", ".obj", ".gcode"}
MAX_UPLOAD = 256 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"


class State:
    settings: config.Settings
    auth: AuthConfig
    jobs: JobStore
    # PrusaSlicer is memory hungry; slice one model at a time.
    slice_lock: asyncio.Lock
    preview_lock: asyncio.Lock
    tasks: set[asyncio.Task]


state = State()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.settings = config.load_settings()
    state.auth = load_auth(config.DATA_DIR)
    state.jobs = JobStore(config.DATA_DIR / "jobs")
    state.slice_lock = asyncio.Lock()
    state.preview_lock = asyncio.Lock()
    state.tasks = set()
    yield
    for t in state.tasks:
        t.cancel()


app = FastAPI(title="PrintBridge", lifespan=lifespan)


@app.middleware("http")
async def require_login(request: Request, call_next):
    auth = state.auth
    path = request.url.path
    if (
        not auth.enabled
        or path in PUBLIC_PATHS
        or auth.check_session(request.cookies.get(COOKIE_NAME))
        or auth.check_basic(request.headers.get("authorization"))
    ):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# ---- auth -----------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/login", include_in_schema=False)
async def login_page() -> Response:
    if not state.auth.enabled:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/auth/login")
async def login(req: LoginRequest, request: Request) -> Response:
    auth = state.auth
    if not auth.enabled:
        return JSONResponse({"ok": True})
    client = request.client.host if request.client else "unknown"
    if wait := auth.locked_out(client):
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait} seconds.")
    if not auth.check_credentials(req.username, req.password):
        auth.record_failure(client)
        raise HTTPException(401, "Incorrect username or password")
    auth.clear_failures(client)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME,
        auth.make_session(),
        max_age=auth.session_seconds,
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure,
    )
    return resp


@app.post("/api/auth/logout")
async def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/api/auth/status")
async def auth_status() -> dict:
    return {"enabled": state.auth.enabled, "username": state.auth.username if state.auth.enabled else None}


def require_job(job_id: str) -> Job:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def host():
    try:
        return make_host(state.settings.printer)
    except PrinterError as e:
        raise HTTPException(400, str(e))


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


async def run_slice(job: Job) -> None:
    jobs = state.jobs
    async with state.slice_lock:
        jobs.update(job, state="slicing", error=None)
        # Placement options may have changed; the preview is regenerated on demand.
        jobs.preview_path(job).unlink(missing_ok=True)
        out = jobs.gcode_path(job)
        try:
            await slice_model(
                state.settings.slicer_path,
                jobs.source_path(job),
                out,
                job.options or SliceOptions(),
                state.settings.slice_timeout,
            )
            jobs.update(job, state="ready", stats=parse_stats(out))
        except SlicerError as e:
            jobs.update(job, state="failed", error=str(e))
        except Exception as e:  # keep the job record consistent on unexpected errors
            jobs.update(job, state="failed", error=f"Unexpected error: {e}")


async def run_send(job: Job, start: bool) -> None:
    jobs = state.jobs
    jobs.update(job, state="sending", error=None)
    try:
        await make_host(state.settings.printer).upload(jobs.gcode_path(job), job.gcode_name, start)
        jobs.update(job, state="sent", sent_at=time.time(), started_print=start)
    except PrinterError as e:
        jobs.update(job, state="ready", error=f"Send failed: {e}")


# ---- settings -------------------------------------------------------------


@app.get("/api/settings")
async def get_settings() -> config.Settings:
    return state.settings


@app.put("/api/settings")
async def put_settings(settings: config.Settings) -> config.Settings:
    if not settings.slicer_path:
        settings.slicer_path = config.detect_slicer()
    config.save_settings(settings)
    state.settings = settings
    return settings


@app.get("/api/profiles")
async def profiles() -> dict:
    return {
        "qualities": QUALITIES,
        "materials": MATERIALS,
        "defaults": SliceOptions().model_dump(),
        "accepted": sorted(ACCEPTED),
        "slicer_found": bool(state.settings.slicer_path),
    }


# ---- jobs -----------------------------------------------------------------


@app.get("/api/jobs")
async def list_jobs() -> list[Job]:
    return state.jobs.list()


@app.post("/api/jobs", status_code=201)
async def create_job(file: UploadFile = File(...), options: str = Form("{}")) -> Job:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ACCEPTED:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}; expected one of {', '.join(sorted(ACCEPTED))}")
    try:
        opts = SliceOptions.model_validate_json(options)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))

    is_gcode = ext == ".gcode"
    job = state.jobs.create(file.filename or f"model{ext}", None if is_gcode else opts)
    dest = state.jobs.source_path(job)
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                state.jobs.delete(job)
                raise HTTPException(413, "File too large")
            fh.write(chunk)

    if is_gcode:
        dest.rename(state.jobs.gcode_path(job))
        state.jobs.update(job, source=job.gcode_name, state="ready", stats=parse_stats(state.jobs.gcode_path(job)))
    else:
        spawn(run_slice(job))
    return job


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> Job:
    return require_job(job_id)


@app.post("/api/jobs/{job_id}/reslice")
async def reslice(job_id: str, options: SliceOptions) -> Job:
    job = require_job(job_id)
    if job.options is None:
        raise HTTPException(400, "Uploaded G-code cannot be re-sliced")
    if job.state in ("queued", "slicing", "sending"):
        raise HTTPException(409, f"Job is {job.state}")
    state.jobs.update(job, options=options, state="queued", stats=None, error=None)
    spawn(run_slice(job))
    return job


@app.get("/api/jobs/{job_id}/gcode")
async def download_gcode(job_id: str) -> FileResponse:
    job = require_job(job_id)
    path = state.jobs.gcode_path(job)
    if not path.exists():
        raise HTTPException(404, "G-code not available")
    return FileResponse(path, media_type="text/x-gcode", filename=job.gcode_name)


@app.get("/api/jobs/{job_id}/preview.stl")
async def model_preview(job_id: str) -> FileResponse:
    job = require_job(job_id)
    if job.options is None:
        raise HTTPException(404, "No 3D model for uploaded G-code")
    path = state.jobs.preview_path(job)
    if not path.exists():
        async with state.preview_lock:
            if not path.exists():
                try:
                    await export_preview(state.settings.slicer_path, state.jobs.source_path(job), path, job.options)
                except SlicerError as e:
                    raise HTTPException(422, str(e))
    return FileResponse(path, media_type="model/stl", filename=f"{job.name}.stl")


class SendRequest(BaseModel):
    start: bool = True


@app.post("/api/jobs/{job_id}/send")
async def send_job(job_id: str, req: SendRequest) -> Job:
    job = require_job(job_id)
    if job.state not in ("ready", "sent"):
        raise HTTPException(409, f"Job is {job.state}; only sliced jobs can be sent")
    host()  # validate printer config before going async
    spawn(run_send(job, req.start))
    return state.jobs.update(job, state="sending")


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> None:
    job = require_job(job_id)
    if job.state in ("slicing", "sending"):
        raise HTTPException(409, f"Job is {job.state}")
    state.jobs.delete(job)


# ---- printer --------------------------------------------------------------


@app.get("/api/printer")
async def printer_status() -> PrinterStatus:
    if not state.settings.printer.url:
        return PrinterStatus(online=False, state="Not configured")
    try:
        return await host().status()
    except PrinterError as e:
        return PrinterStatus(online=False, state="Offline", message=str(e))


class ControlRequest(BaseModel):
    action: str


@app.post("/api/printer/control")
async def printer_control(req: ControlRequest) -> dict:
    try:
        await host().control(req.action)
    except PrinterError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
