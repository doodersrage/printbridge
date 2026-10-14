"""FastAPI application: upload models, slice them for the Ender-3 V3 SE, send to the printer."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from . import __version__, backup, calibration, combine, config, history, importer, notify, presets, profiles, spools, toolpaths
from .auth import COOKIE_NAME, PUBLIC_PATHS, AuthConfig, load_auth
from .gcode import parse_stats
from .jobs import IDLE_STATES, Job, JobStore
from .monitor import ACTIVE, Monitor
from .mqtt import MqttBridge
from .printers import PrinterError, PrinterStatus, RemoteFile, make_host
from .slicer import SliceOptions, SlicerError, check_options, export_preview, slice_model
from .threemf import ThreeMFError, extract_plate, inspect, read_plates, remove_stored_scale

ACCEPTED = {".3mf", ".stl", ".obj", ".gcode", ".zip"}
MAX_THUMBNAIL = 1024 * 1024
MAX_UPLOAD = 256 * 1024 * 1024
STATIC_DIR = Path(__file__).parent / "static"
PRUNE_INTERVAL = 3600

log = logging.getLogger("printbridge")


class State:
    settings: config.Settings
    auth: AuthConfig
    jobs: JobStore
    monitor: Monitor
    mqtt: MqttBridge
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
    state.mqtt = MqttBridge(lambda: state.settings.mqtt, mqtt_command, supports_light, lambda: state.settings.public_url)
    state.monitor.listeners.append(state.mqtt.publish_state)
    state.slice_lock = asyncio.Lock()
    state.preview_lock = asyncio.Lock()
    state.tasks = set()
    spawn(state.monitor.run())
    spawn(state.mqtt.run())
    spawn(prune_loop())
    yield
    for t in state.tasks:
        t.cancel()


app = FastAPI(title="PrintBridge", version=__version__, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=2048)


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def same_origin(request: Request) -> bool:
    """Reject cross-site writes: a page on another site must not drive the printer.

    Browsers send Origin on cross-site POSTs (including form posts, which need no
    CORS preflight). Requests without Origin come from scripts like curl, which
    a malicious web page cannot make.
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    from urllib.parse import urlsplit

    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(origin).netloc.lower() == host.split(",")[0].strip().lower()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if request.method in UNSAFE_METHODS and not same_origin(request):
        return JSONResponse({"detail": "Cross-site request blocked"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


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
    """The model file to slice: the upload, just the chosen plate of a multi-plate 3MF,
    and/or a copy without the scale stored in the file."""
    src = state.jobs.source_path(job)
    plate = job.options.plate if job.options else None
    if job.plates and plate is not None:
        dst = state.jobs.plate_path(job, plate)
        if not dst.exists():
            try:
                extract_plate(src, dst, plate)
            except (ThreeMFError, KeyError, OSError) as e:
                raise SlicerError(f"Could not extract plate {plate}: {e}") from e
        src = dst
    if job.options and job.options.original_size and job.source_info and job.source_info.scale:
        dst = state.jobs.dir(job) / f"unscaled-{plate or 0}.3mf"
        if not dst.exists():
            try:
                remove_stored_scale(src, dst)
            except (OSError, ValueError, KeyError) as e:
                raise SlicerError(f"Could not remove the stored scale: {e}") from e
        src = dst
    return src


async def measure(job: Job) -> list[float] | None:
    """Printed size (W, D, H) from the placed model, cached as the 3D preview."""
    path = state.jobs.preview_path(job)
    async with state.preview_lock:
        if not path.exists():
            await export_preview(state.settings.slicer_path, model_input(job), path, job.options or SliceOptions())
    return await asyncio.to_thread(combine.stl_size, path)


def clear_derived(job: Job) -> None:
    """Remove cached previews; they are rebuilt on demand from the new slice."""
    state.jobs.preview_path(job).unlink(missing_ok=True)
    state.jobs.toolpath_path(job).unlink(missing_ok=True)
    state.jobs.thumbnail_path(job).unlink(missing_ok=True)
    job.thumbnail = False


def supports_light() -> bool:
    try:
        return make_host(state.settings.printer).supports_light if state.settings.printer.url else False
    except PrinterError:
        return False


async def mqtt_command(command: str) -> None:
    h = make_host(state.settings.printer)
    if command in ("pause", "resume", "cancel"):
        await h.control(command)
    elif command == "cooldown":
        st = await state.monitor.current(max_age=2)
        if st.state in ACTIVE:
            raise PrinterError("Not cooling down while printing")
        await h.command("cooldown")
    else:
        await h.command(command)
    state.monitor.status = None


def add_job(filename: str, data: bytes, opts: SliceOptions) -> Job:
    """Create a job from an uploaded or downloaded file and start slicing it."""
    ext = Path(filename).suffix.lower()
    is_gcode = ext == ".gcode"
    job = state.jobs.create(filename, None if is_gcode else opts)
    dest = state.jobs.source_path(job)
    dest.write_bytes(data)
    return _finish_job(job, ext, opts)


def _finish_job(job: Job, ext: str, opts: SliceOptions) -> Job:
    dest = state.jobs.source_path(job)
    if ext == ".gcode":
        dest.rename(state.jobs.gcode_path(job))
        return state.jobs.update(job, source=job.gcode_name, state="ready", stats=parse_stats(state.jobs.gcode_path(job)))
    if ext == ".3mf":
        state.jobs.update(job, source_info=inspect(dest))
        if plates := read_plates(dest):
            plate = opts.plate if opts.plate in {p.id for p in plates} else plates[0].id
            state.jobs.update(job, plates=plates, options=opts.model_copy(update={"plate": plate}))
    spawn(run_slice(job))
    return job


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
            if job.calibration:
                calibration.post_process(out, job.calibration)
            try:
                size = await measure(job)
            except (SlicerError, combine.CombineError, OSError):
                size = None  # the size is informational; never fail a good slice over it
            jobs.update(job, state="ready", stats=parse_stats(out), size=size)
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


# Secret fields are never sent to the browser. The UI shows them as "set"; sending
# the placeholder (or leaving it empty with keep_secrets) keeps the stored value.
SECRET_FIELDS = (("printer", "api_key"), ("notify", "smtp_password"), ("notify", "ntfy_token"), ("mqtt", "password"))
SECRET_PLACEHOLDER = "••••••••"


def masked(settings: config.Settings) -> dict:
    data = settings.model_dump()
    for section, key in SECRET_FIELDS:
        data[section][key] = SECRET_PLACEHOLDER if data[section][key] else ""
    return data


@app.get("/api/settings")
async def get_settings() -> dict:
    return masked(state.settings)


@app.put("/api/settings")
async def put_settings(settings: config.Settings) -> dict:
    for section, key in SECRET_FIELDS:
        if getattr(getattr(settings, section), key) == SECRET_PLACEHOLDER:
            setattr(getattr(settings, section), key, getattr(getattr(state.settings, section), key))
    # The slicer runs as a program, so it is only set by the environment / auto-detection.
    settings.slicer_path = state.settings.slicer_path or config.detect_slicer()
    config.save_settings(settings)
    state.settings = settings
    state.monitor.status = None  # re-poll with the new printer settings
    state.mqtt.restart()
    return masked(settings)


@app.post("/api/notify/test")
async def notify_test() -> dict:
    cfg = state.settings.notify
    if not (cfg.ntfy_url or cfg.webhook_url or notify.email_configured(cfg)):
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


async def _read_upload(file: UploadFile, limit: int = MAX_UPLOAD) -> bytes:
    chunks, size = [], 0
    while chunk := await file.read(1 << 20):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "File too large")
        chunks.append(chunk)
    return b"".join(chunks)


class ImportRequest(BaseModel):
    url: str = Field(..., max_length=2000)
    options: SliceOptions = SliceOptions()


@app.post("/api/jobs/import", status_code=201)
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


@app.post("/api/jobs/combine", status_code=201)
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
    ignore_spool: bool = Field(False, description="Start even if the loaded spool looks too low or is another material")


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
        if not req.ignore_spool:
            problems = spools.check(job.stats.filament_g if job.stats else None,
                                    job.options.material if job.options else None)
            if problems:
                raise HTTPException(409, {"code": "spool", "message": " ".join(problems)})
    spawn(run_send(job, req.start))
    return state.jobs.update(job, state="sending")


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> None:
    job = require_job(job_id)
    if job.state in ("queued", "slicing", "sending"):
        raise HTTPException(409, f"Job is {job.state}")
    state.jobs.delete(job)


# ---- thumbnails -------------------------------------------------------------


@app.put("/api/jobs/{job_id}/thumbnail", status_code=204)
async def put_thumbnail(job_id: str, request: Request) -> None:
    """Store a PNG thumbnail rendered by the browser from the job's 3D preview."""
    job = require_job(job_id)
    data = await request.body()
    if len(data) > MAX_THUMBNAIL or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "Expected a PNG image under 1 MB")
    state.jobs.thumbnail_path(job).write_bytes(data)
    state.jobs.update(job, thumbnail=True)


@app.get("/api/jobs/{job_id}/thumbnail.png")
async def get_thumbnail(job_id: str) -> FileResponse:
    path = state.jobs.thumbnail_path(require_job(job_id))
    if not path.exists():
        raise HTTPException(404, "No thumbnail yet")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


# ---- calibration --------------------------------------------------------------


@app.get("/api/calibration/defaults")
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


@app.post("/api/calibration", status_code=201)
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


@app.post("/api/jobs/{job_id}/calibration/apply")
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


# ---- history & costs ----------------------------------------------------------


@app.get("/api/history")
async def get_history(limit: int = 200) -> dict:
    return history.summary(limit=max(1, min(limit, 2000)))


class CostsRequest(BaseModel):
    currency: str = Field("$", max_length=5)
    prices: dict[str, float] = {}


@app.get("/api/costs")
async def get_costs() -> dict:
    prices = {}
    for m in profiles.names("material"):
        try:
            prices[m] = float(profiles.read_values(profiles.resolve("material", m)).get("filament_cost", 0) or 0)
        except ValueError:
            prices[m] = 0.0
    return {"currency": state.settings.currency, "prices": prices}


@app.put("/api/costs")
async def put_costs(req: CostsRequest) -> dict:
    """Store prices (per kg) in the material profiles, where PrusaSlicer uses them for cost estimates."""
    for material, price in req.prices.items():
        if price < 0 or price > 10000:
            raise HTTPException(422, f"Price for {material} must be between 0 and 10000")
        try:
            current = profiles.read_values(profiles.resolve("material", material)).get("filament_cost")
            if current is None and price == 0:
                continue
            if current is None or float(current or 0) != price:
                profiles.set_values("material", material, {"filament_cost": f"{price:g}"})
        except profiles.ProfileError as e:
            raise HTTPException(422, str(e))
    if req.currency != state.settings.currency:
        state.settings.currency = req.currency
        config.save_settings(state.settings)
    return await get_costs()


# ---- webcam -------------------------------------------------------------------


@app.get("/api/webcam")
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


@app.get("/api/spools")
async def list_spools() -> list[spools.Spool]:
    return spools.load()


class SpoolRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)
    material: str = "pla"
    total_g: float = Field(1000, gt=0, le=20000)
    remaining_g: float | None = Field(None, ge=0, le=20000)
    loaded: bool = False


@app.post("/api/spools", status_code=201)
async def add_spool(req: SpoolRequest) -> list[spools.Spool]:
    try:
        spool = spools.Spool(name=req.name, material=req.material, total_g=req.total_g,
                             remaining_g=req.total_g if req.remaining_g is None else req.remaining_g)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False))
    return spools.upsert(spool, make_loaded=req.loaded)


@app.put("/api/spools/{spool_id}")
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


@app.delete("/api/spools/{spool_id}")
async def delete_spool(spool_id: str) -> list[spools.Spool]:
    return spools.delete(spool_id)


# ---- backup & integrations ----------------------------------------------------


@app.get("/api/backup")
async def get_backup() -> Response:
    stamp = time.strftime("%Y%m%d-%H%M")
    return Response(backup.create(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="printbridge-backup-{stamp}.zip"'})


@app.post("/api/restore")
async def post_restore(file: UploadFile = File(...)) -> dict:
    data = await _read_upload(file, backup.MAX_RESTORE)
    try:
        restored = backup.restore(data)
    except (backup.BackupError, ValidationError, ValueError) as e:
        raise HTTPException(422, str(e))
    slicer = state.settings.slicer_path
    state.settings = config.load_settings()
    if state.settings.slicer_path != slicer:
        state.settings.slicer_path = slicer
        config.save_settings(state.settings)
    state.monitor.status = None
    state.mqtt.restart()
    return {"restored": restored}


@app.get("/api/mqtt/status")
async def mqtt_status() -> dict:
    cfg = state.settings.mqtt
    return {"enabled": cfg.enabled, "connected": state.mqtt.connected, "error": state.mqtt.last_error}


# ---- printer --------------------------------------------------------------


@app.get("/api/printer")
async def printer_status() -> dict:
    st = await state.monitor.current()
    try:
        light = make_host(state.settings.printer).supports_light if state.settings.printer.url else False
    except PrinterError:
        light = False
    spool = spools.loaded()
    return {**st.model_dump(), "supports_light": light, "babystep_total": state.monitor.babystep_total,
            "webcam": bool(state.settings.webcam_url), "spool": spool.model_dump() if spool else None}


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
    command: Literal["preheat", "cooldown", "home", "fan_on", "fan_off", "light_on", "light_off", "gcode", "babystep"]
    nozzle: int = Field(0, ge=0, le=260)
    bed: int = Field(0, ge=0, le=100)
    gcode: str = Field("", max_length=500)
    z: float = Field(0, ge=-0.5, le=0.5, description="Live Z adjustment in mm for babystep")


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
