"""Background printer monitor.

Polls the print host, caches the latest status for the web UI, follows the
print that PrintBridge started through to completion, and sends notifications.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import notify
from .config import Settings
from .jobs import JobStore
from .printers import PrinterError, PrinterStatus, make_host

log = logging.getLogger("printbridge.monitor")

POLL_SECONDS = 5
ACTIVE = ("Printing", "Paused")


def _fmt_duration(sec: float | None) -> str:
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

    async def _notify(self, event: str, message: str, file: str | None) -> None:
        extra = {"file": file, "printer_state": self.status.state if self.status else None}
        await notify.send(self.get_settings().notify, event, message, extra)

    async def _track(self, st: PrinterStatus) -> None:
        prev, self._prev = self._prev, st
        if not st.online:
            return  # an unreachable printer says nothing about the print

        # Printer-reported errors.
        err = st.message if st.message and "error" in st.message.lower() else None
        if err and err != self._last_error:
            await self._notify("error", err, st.file)
        self._last_error = err

        if prev is None or not prev.online:
            if st.state in ACTIVE and st.file:
                self._mark(st.file, "printing")
            return

        was_active, is_active = prev.state in ACTIVE, st.state in ACTIVE
        if is_active and (not was_active or (st.file and st.file != prev.file)):
            job = self._mark(st.file, "printing")
            name = job.name if job else (st.file or "print")
            await self._notify("started", f"{name} started printing", st.file)
        elif was_active and not is_active:
            # Hosts keep reporting the final progress after a print ends, so a
            # finished print can go straight from <99% to idle between two polls.
            done = st.state == "Complete" or max(prev.progress or 0, st.progress or 0) >= 99
            job = self._mark(prev.file, "printed" if done else "stopped", finished=True)
            name = job.name if job else (prev.file or "print")
            if done:
                took = _fmt_duration(prev.elapsed)
                await self._notify("finished", f"{name} finished{f' in {took}' if took else ''}", prev.file)
            else:
                await self._notify("stopped", f"{name} stopped at {prev.progress or 0:.0f}%", prev.file)

    def _mark(self, file: str | None, state: str, finished: bool = False):
        if not file:
            return None
        job = self.jobs.find_by_gcode_name(Path(file).name)
        if job and job.state in ("sent", "printing", "printed", "stopped"):
            changes = {"state": state}
            if finished:
                changes["finished_at"] = time.time()
            self.jobs.update(job, **changes)
        return job
