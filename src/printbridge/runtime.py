"""Shared runtime state and the job pipeline used by the API routes."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from fastapi import HTTPException, UploadFile

from . import backup, calibration, combine, config, importer, presets, profiles
from .auth import AuthConfig
from .gcode import parse_stats
from .jobs import Job, JobStore
from .monitor import ACTIVE, Monitor
from .mqtt import MqttBridge
from .printers import PrinterError, make_host
from .slicer import (
    SliceOptions,
    SlicerError,
    check_options,
    export_preview,
    slice_model,
)
from .threemf import (
    ThreeMFError,
    extract_plate,
    inspect,
    read_plates,
    remove_stored_scale,
)
from .watch import Watcher

ACCEPTED = {".3mf", ".stl", ".obj", ".gcode", ".zip"}
MAX_UPLOAD = 256 * 1024 * 1024
MAX_THUMBNAIL = 1024 * 1024
PRUNE_INTERVAL = 3600
WATCH_INTERVAL = 5
BACKUP_CHECK_INTERVAL = 3600

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
    watcher: "Watcher | None"


state = State()


async def prune_loop() -> None:
    while True:
        removed = state.jobs.prune(state.settings.keep_days)
        if removed:
            log.info("Deleted %d jobs older than %d days", len(removed), state.settings.keep_days)
        await asyncio.sleep(PRUNE_INTERVAL)


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


async def _read_upload(file: UploadFile, limit: int = MAX_UPLOAD) -> bytes:
    chunks, size = [], 0
    while chunk := await file.read(1 << 20):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "File too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def backup_loop() -> None:
    """Write a daily configuration backup into the data volume while enabled."""
    while True:
        if state.settings.auto_backup:
            try:
                written = await asyncio.to_thread(backup.write_auto, state.settings.auto_backup_keep)
                if written:
                    log.info("Wrote automatic backup %s", written)
            except OSError:
                log.exception("Automatic backup failed")
        await asyncio.sleep(BACKUP_CHECK_INTERVAL)


def _watch_import(filename: str, data: bytes) -> int:
    """Create jobs for a watch-folder file, sliced with the configured preset."""
    opts = presets.load().get(state.settings.watch_preset) or SliceOptions()
    if Path(filename).suffix.lower() == ".zip":
        files = importer.from_zip(data)  # raises ImportError_ (a ValueError) for zips without models
    else:
        files = [(filename, data)]
    for name, blob in files:
        add_job(name, blob, opts)
    return len(files)


async def watch_loop() -> None:
    from .watch import Watcher

    if config.WATCH_DIR is None:
        state.watcher = None
        return
    state.watcher = Watcher(config.WATCH_DIR, _watch_import)
    log.info("Watching %s for model files", config.WATCH_DIR)
    while True:
        if state.settings.watch_enabled:
            try:
                state.watcher.scan()
            except Exception:
                log.exception("Watch folder scan failed")
        await asyncio.sleep(WATCH_INTERVAL)
