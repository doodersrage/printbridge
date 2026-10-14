"""Background printer monitor.

Polls the print host, caches the latest status for the web UI, follows the
print that PrintBridge started through to completion, records it in the print
history, sends notifications, and feeds listeners such as the MQTT bridge.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from . import history, notify, spools
from .config import Settings
from .jobs import Job, JobStore
from .printers import PrinterError, PrinterStatus, make_host

log = logging.getLogger("printbridge.monitor")

POLL_SECONDS = 5
ACTIVE = ("Printing", "Paused")
Listener = Callable[[PrinterStatus, "Job | None"], Awaitable[None]]


def fmt_duration(sec: float | None) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}h {m}m" if h else f"{m}m"


class Monitor:
    def __init__(self, get_settings: Callable[[], Settings], jobs: JobStore):
        self.get_settings = get_settings
        self.jobs = jobs
        self.status: PrinterStatus | None = None
        self.updated = 0.0
        self.listeners: list[Listener] = []
        self.babystep_total = 0.0  # mm of live Z adjustment applied to the current print
        self.current_job: Job | None = None
        self._started_at: float | None = None
        self._prev: PrinterStatus | None = None
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    async def poll(self) -> PrinterStatus:
        settings = self.get_settings()
        if not settings.printer.url:
            st = PrinterStatus(online=False, state="Not configured")
        else:
            try:
                st = await make_host(settings.printer).status()
            except PrinterError as e:
                st = PrinterStatus(online=False, state="Offline", message=str(e))
        self.status, self.updated = st, time.time()
        await self._track(st)
        for listener in list(self.listeners):
            try:
                await listener(st, self.current_job)
            except Exception:
                log.exception("Status listener failed")
        return st

    async def current(self, max_age: float = POLL_SECONDS) -> PrinterStatus:
        async with self._lock:
            if self.status is None or time.time() - self.updated > max_age:
                await self.poll()
            return self.status  # type: ignore[return-value]

    async def run(self) -> None:
        while True:
            try:
                async with self._lock:
                    await self.poll()
            except Exception:  # never let the monitor die
                log.exception("Printer poll failed")
            await asyncio.sleep(POLL_SECONDS)

    # ---- notifications ------------------------------------------------------

    def _job_details(self, job: Job | None, fraction: float = 1.0) -> dict:
        if not job or not job.stats:
            return {}
        currency = self.get_settings().currency
        out = {}
        if job.stats.filament_g:
            out["filament_g"] = round(job.stats.filament_g * fraction, 1)
            out["filament"] = f"{out['filament_g']} g"
        if job.stats.cost:
            out["cost_value"] = round(job.stats.cost * fraction, 2)
            out["cost"] = f"{currency}{out['cost_value']:.2f}"
        return out

    async def _notify(self, event: str, message: str, file: str | None, job: Job | None = None,
                      extra: dict | None = None) -> None:
        settings = self.get_settings()
        data = {"file": Path(file).name if file else None,
                "printer_state": self.status.state if self.status else None,
                "url": settings.public_url or None, **(extra or {})}
        image = None
        if job and event in ("finished", "stopped"):
            thumb = self.jobs.thumbnail_path(job)
            image = thumb.read_bytes() if thumb.exists() else None
        await notify.send(settings.notify, event, message, data, image=image)

    # ---- state tracking -----------------------------------------------------

    async def _track(self, st: PrinterStatus) -> None:
        prev, self._prev = self._prev, st
        if not st.online:
            return  # an unreachable printer says nothing about the print

        err = st.message if st.message and "error" in st.message.lower() else None
        if err and err != self._last_error:
            await self._notify("error", err, st.file, self.current_job)
        self._last_error = err

        if prev is None or not prev.online:
            if st.state in ACTIVE and st.file:
                self._begin(st)
            return

        was_active, is_active = prev.state in ACTIVE, st.state in ACTIVE
        if is_active and (not was_active or (st.file and st.file != prev.file)):
            job = self._begin(st)
            name = job.name if job else Path(st.file or "print").stem
            await self._notify("started", f"{name} started printing", st.file, job)
        elif was_active and not is_active:
            await self._end(prev, st)

    def _begin(self, st: PrinterStatus) -> Job | None:
        self._started_at = time.time() - (st.elapsed or 0)
        self.babystep_total = 0.0
        self.current_job = self._mark(st.file, "printing")
        return self.current_job

    async def _end(self, prev: PrinterStatus, st: PrinterStatus) -> None:
        # Hosts keep reporting the final progress after a print ends, so a
        # finished print can go straight from <99% to idle between two polls.
        progress = max(prev.progress or 0, st.progress or 0)
        done = st.state == "Complete" or progress >= 99
        job = self._mark(prev.file, "printed" if done else "stopped", finished=True)
        name = job.name if job else Path(prev.file or "print").stem
        now = time.time()
        duration = prev.elapsed or ((now - self._started_at) if self._started_at else None)
        details = self._job_details(job, 1.0 if done else progress / 100)
        details["duration"] = fmt_duration(duration)
        spool = spools.consume(details.get("filament_g") or 0)
        if spool:
            left = f"{spool.remaining_g:.0f} g left on {spool.name}"
            details["spool"] = left + (" — running low" if spool.remaining_g < spools.LOW_GRAMS else "")

        history.append(history.PrintRecord(
            job_id=job.id if job else None,
            name=name,
            file=Path(prev.file).name if prev.file else None,
            material=job.options.material if job and job.options else None,
            quality=job.options.quality if job and job.options else None,
            started_at=self._started_at,
            ended_at=now,
            duration=duration,
            result="printed" if done else "stopped",
            progress=100.0 if done else round(progress, 1),
            filament_g=details.get("filament_g"),
            cost=details.get("cost_value"),
            spool=spool.name if spool else None,
        ))

        if done:
            took = details["duration"]
            await self._notify("finished", f"{name} finished{f' in {took}' if took else ''}", prev.file, job, details)
        else:
            await self._notify("stopped", f"{name} stopped at {progress:.0f}%", prev.file, job, details)
        self.current_job = None
        self._started_at = None
        self.babystep_total = 0.0

    def _mark(self, file: str | None, state: str, finished: bool = False) -> Job | None:
        if not file:
            return None
        job = self.jobs.find_by_gcode_name(Path(file).name)
        if job and job.state in ("sent", "printing", "printed", "stopped"):
            changes: dict = {"state": state}
            if finished:
                changes["finished_at"] = time.time()
            self.jobs.update(job, **changes)
        return job
