"""FastAPI application: upload models, slice them for the Ender-3 V3 SE, send to the printer."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, config, notify, presets, profiles, toolpaths
from .auth import COOKIE_NAME, PUBLIC_PATHS, AuthConfig, load_auth
from .gcode import parse_stats
from .jobs import IDLE_STATES, Job, JobStore
from .monitor import ACTIVE, Monitor
from .printers import PrinterError, PrinterStatus, RemoteFile, make_host
from .slicer import SliceOptions, SlicerError, check_options, export_preview, slice_model
from .threemf import ThreeMFError, extract_plate, read_plates

ACCEPTED = {".3mf", ".stl", ".obj", ".gcode"}
MAX_UPLOAD = 256 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
PRUNE_INTERVAL = 3600

log = logging.getLogger("printbridge")


class State:
    settings: config.Settings
    auth: AuthConfig
    jobs: JobStore
    monitor: Monitor
    # PrusaSlicer is memory hungry; slice one model at a time.
    slice_lock: asyncio.Lock
    preview_lock: asyncio.Lock
    tasks: set[asyncio.Task]


state = State()


async def prune_loop() -> None:
    while True:
        removed = state.jobs.prune(state.settings.keep_days)
        if removed:
            log.info("Deleted %d jobs older than %d days", len(removed), state.settings.keep_days)
        await asyncio.sleep(PRUNE_INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.settings = config.load_settings()
    state.auth = load_auth(config.DATA_DIR)
    state.jobs = JobStore(config.DATA_DIR / "jobs")
    state.monitor = Monitor(lambda: state.settings, state.jobs)
    state.slice_lock = asyncio.Lock()
    state.preview_lock = asyncio.Lock()
    state.tasks = set()
    spawn(state.monitor.run())
    spawn(prune_loop())
    yield
    for t in state.tasks:
        t.cancel()


app = FastAPI(title="PrintBridge", version=__version__, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


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
    return {"ok": True, "version": __version__}


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


# ---- helpers --------------------------------------------------------------


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


def validate_options(opts: SliceOptions) -> None:
    try:
        check_options(opts)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))


def model_input(job: Job) -> Path:
    """The model file to slice: the upload, or just the chosen plate of a multi-plate 3MF."""
    src = state.jobs.source_path(job)
    plate = job.options.plate if job.options else None
    if not job.plates or plate is None:
        return src
    dst = state.jobs.plate_path(job, plate)
    if not dst.exists():
        try:
            extract_plate(src, dst, plate)
        except (ThreeMFError, KeyError, OSError) as e:
            raise SlicerError(f"Could not extract plate {plate}: {e}") from e
    return dst


def clear_derived(job: Job) -> None:
    """Remove cached previews; they are rebuilt on demand from the new slice."""
    state.jobs.preview_path(job).unlink(missing_ok=True)
    state.jobs.toolpath_path(job).unlink(missing_ok=True)


async def run_slice(job: Job) -> None:
    jobs = state.jobs
    async with state.slice_lock:
        jobs.update(job, state="slicing", error=None)
        clear_derived(job)
        out = jobs.gcode_path(job)
        try:
            await slice_model(
                state.settings.slicer_path,
                model_input(job),
                out,
                job.options or SliceOptions(),
                state.settings.slice_timeout,
            )
            jobs.update(job, state="ready", stats=parse_stats(out))
        except (SlicerError, profiles.ProfileError) as e:
            jobs.update(job, state="failed", error=str(e))
        except Exception as e:  # keep the job record consistent on unexpected errors
            jobs.update(job, state="failed", error=f"Unexpected error: {e}")


async def run_send(job: Job, start: bool) -> None:
    jobs = state.jobs
    jobs.update(job, state="sending", error=None)
    try:
        await make_host(state.settings.printer).upload(jobs.gcode_path(job), job.gcode_name, start)
        jobs.update(job, state="sent", sent_at=time.time(), started_print=start, finished_at=None)
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
    state.monitor.status = None  # re-poll with the new printer settings
    return settings


@app.post("/api/notify/test")
async def notify_test() -> dict:
    cfg = state.settings.notify
    if not (cfg.ntfy_url or cfg.webhook_url):
        raise HTTPException(400, "No notification target configured")
    errors = await notify.send(cfg, "test", "Notifications from PrintBridge are working.")
    if errors:
        raise HTTPException(502, "; ".join(errors))
    return {"ok": True}


# ---- profiles & presets ---------------------------------------------------


@app.get("/api/profiles")
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


@app.get("/api/profile-files")
async def list_profile_files() -> list[dict]:
    out = []
    for kind in profiles.KINDS:
        for name in profiles.choices(kind) if kind != "printer" else ["printer"]:
            d = profiles.describe(kind, name)
            out.append({k: d[k] for k in ("kind", "name", "label", "builtin", "overridden")})
    return out


@app.get("/api/profile-files/{kind}/{name}")
async def get_profile_file(kind: profiles.Kind, name: str) -> dict:
    try:
        return profiles.describe(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(404, str(e))


class ProfileText(BaseModel):
    text: str


@app.put("/api/profile-files/{kind}/{name}")
async def put_profile_file(kind: profiles.Kind, name: str, body: ProfileText) -> dict:
    try:
        profiles.save_override(kind, name, body.text)
        return profiles.describe(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))


@app.delete("/api/profile-files/{kind}/{name}")
async def delete_profile_file(kind: profiles.Kind, name: str) -> dict:
    """Reset a built-in profile to its default, or delete a custom one."""
    try:
        profiles.remove_override(kind, name)
    except profiles.ProfileError as e:
        raise HTTPException(422, str(e))
    if profiles.builtin_path(kind, name).exists():
        return profiles.describe(kind, name)
    return {"deleted": True}


@app.get("/api/presets")
async def list_presets() -> dict[str, SliceOptions]:
    return presets.load()


@app.put("/api/presets/{name}")
async def put_preset(name: str, options: SliceOptions) -> dict[str, SliceOptions]:
    if not presets.NAME_RE.match(name):
        raise HTTPException(422, "Invalid preset name")
    validate_options(options)
    data = presets.load()
    data[name] = options.model_copy(update={"plate": None})
    presets.save(data)
    return data


@app.delete("/api/presets/{name}")
async def delete_preset(name: str) -> dict[str, SliceOptions]:
    data = presets.load()
    data.pop(name, None)
    presets.save(data)
    return data


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
    if not is_gcode:
        validate_options(opts)
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
        return job

    if ext == ".3mf" and (plates := read_plates(dest)):
        plate = opts.plate if opts.plate in {p.id for p in plates} else plates[0].id
        state.jobs.update(job, plates=plates, options=opts.model_copy(update={"plate": plate}))
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
                    await export_preview(state.settings.slicer_path, model_input(job), path, job.options)
                except SlicerError as e:
                    raise HTTPException(422, str(e))
    return FileResponse(path, media_type="model/stl", filename=f"{job.name}.stl")


@app.get("/api/jobs/{job_id}/toolpaths")
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


@app.post("/api/jobs/{job_id}/send")
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
    spawn(run_send(job, req.start))
    return state.jobs.update(job, state="sending")


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> None:
    job = require_job(job_id)
    if job.state in ("queued", "slicing", "sending"):
        raise HTTPException(409, f"Job is {job.state}")
    state.jobs.delete(job)


# ---- printer --------------------------------------------------------------


@app.get("/api/printer")
async def printer_status() -> dict:
    st = await state.monitor.current()
    try:
        light = make_host(state.settings.printer).supports_light if state.settings.printer.url else False
    except PrinterError:
        light = False
    return {**st.model_dump(), "supports_light": light}


class ControlRequest(BaseModel):
    action: str


@app.post("/api/printer/control")
async def printer_control(req: ControlRequest) -> dict:
    try:
        await host().control(req.action)
    except PrinterError as e:
        raise HTTPException(502, str(e))
    state.monitor.status = None
    return {"ok": True}


class CommandRequest(BaseModel):
    command: Literal["preheat", "cooldown", "home", "fan_on", "fan_off", "light_on", "light_off", "gcode"]
    nozzle: int = Field(0, ge=0, le=260)
    bed: int = Field(0, ge=0, le=100)
    gcode: str = Field("", max_length=500)


# Commands that would interfere with a running print.
BLOCKED_WHILE_PRINTING = {"preheat", "cooldown", "home", "gcode"}


@app.post("/api/printer/command")
async def printer_command(req: CommandRequest) -> dict:
    h = host()
    if req.command in BLOCKED_WHILE_PRINTING:
        st = await state.monitor.current(max_age=2)
        if st.state in ACTIVE:
            raise HTTPException(409, "Not available while the printer is printing")
    try:
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


@app.get("/api/printer/files")
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


@app.post("/api/printer/files/delete")
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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
