"""Clients for the print hosts that put an Ender-3 V3 SE on the network.

The V3 SE has no network interface of its own; it is reached over USB through
a host such as OctoPrint (Raspberry Pi + stock Marlin) or Klipper/Moonraker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from pydantic import BaseModel

from .config import PrinterConfig

UPLOAD_TIMEOUT = httpx.Timeout(30.0, write=300.0)


class PrinterError(RuntimeError):
    pass


class Temperature(BaseModel):
    actual: float | None = None
    target: float | None = None


class PrinterStatus(BaseModel):
    online: bool
    state: str
    nozzle: Temperature = Temperature()
    bed: Temperature = Temperature()
    file: str | None = None
    progress: float | None = None  # 0..100
    elapsed: float | None = None
    remaining: float | None = None
    message: str | None = None


class PrintHost(ABC):
    def __init__(self, cfg: PrinterConfig, transport: httpx.AsyncBaseTransport | None = None):
        if not cfg.url:
            raise PrinterError("No printer URL configured")
        self.cfg = cfg
        self._transport = transport

    def client(self, timeout: httpx.Timeout | float = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.cfg.url.rstrip("/"),
            headers=self.headers(),
            timeout=timeout,
            verify=self.cfg.verify_tls,
            transport=self._transport,
        )

    def headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.cfg.api_key} if self.cfg.api_key else {}

    async def _request(self, method: str, path: str, **kw) -> httpx.Response:
        timeout = kw.pop("timeout", 10.0)
        try:
            async with self.client(timeout) as c:
                r = await c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise PrinterError(f"Could not reach print host: {e}") from e
        if r.status_code >= 400:
            raise PrinterError(f"{method} {path} failed ({r.status_code}): {r.text[:300]}")
        return r

    @abstractmethod
    async def status(self) -> PrinterStatus: ...

    @abstractmethod
    async def upload(self, gcode: Path, filename: str, start: bool) -> None: ...

    @abstractmethod
    async def control(self, action: str) -> None: ...


class OctoPrint(PrintHost):
    async def status(self) -> PrinterStatus:
        job = (await self._request("GET", "/api/job")).json()
        state = job.get("state", "Unknown")
        st = PrinterStatus(online=True, state=state)
        try:
            printer = (await self._request("GET", "/api/printer", params={"exclude": "sd"})).json()
            temps = printer.get("temperature", {})
            st.nozzle = Temperature(**{k: temps.get("tool0", {}).get(k) for k in ("actual", "target")})
            st.bed = Temperature(**{k: temps.get("bed", {}).get(k) for k in ("actual", "target")})
        except PrinterError:
            # 409 means OctoPrint is up but not connected to the printer's serial port.
            st.message = "OctoPrint is not connected to the printer"
        st.file = (job.get("job") or {}).get("file", {}).get("name")
        progress = job.get("progress") or {}
        st.progress = progress.get("completion")
        st.elapsed = progress.get("printTime")
        st.remaining = progress.get("printTimeLeft")
        return st

    async def upload(self, gcode: Path, filename: str, start: bool) -> None:
        with gcode.open("rb") as fh:
            await self._request(
                "POST",
                "/api/files/local",
                files={"file": (filename, fh, "application/octet-stream")},
                data={"select": "true", "print": "true" if start else "false"},
                timeout=UPLOAD_TIMEOUT,
            )

    async def control(self, action: str) -> None:
        body = {
            "pause": {"command": "pause", "action": "pause"},
            "resume": {"command": "pause", "action": "resume"},
            "cancel": {"command": "cancel"},
        }.get(action)
        if body is None:
            raise PrinterError(f"Unsupported action: {action}")
        await self._request("POST", "/api/job", json=body)


class Moonraker(PrintHost):
    _STATES = {
        "standby": "Operational",
        "printing": "Printing",
        "paused": "Paused",
        "complete": "Complete",
        "cancelled": "Cancelled",
        "error": "Error",
    }

    async def status(self) -> PrinterStatus:
        r = await self._request(
            "GET",
            "/printer/objects/query",
            params={"print_stats": "", "extruder": "", "heater_bed": "", "virtual_sdcard": "", "webhooks": ""},
        )
        s = r.json()["result"]["status"]
        ps = s.get("print_stats", {})
        hooks = s.get("webhooks", {})
        klipper_state = hooks.get("state", "ready")
        state = self._STATES.get(ps.get("state", ""), ps.get("state", "Unknown"))
        if klipper_state != "ready":
            state = f"Klipper {klipper_state}"
        progress = s.get("virtual_sdcard", {}).get("progress")
        elapsed = ps.get("print_duration")
        remaining = None
        if progress and elapsed and progress > 0.01:
            remaining = elapsed / progress - elapsed
        return PrinterStatus(
            online=True,
            state=state,
            nozzle=Temperature(actual=s.get("extruder", {}).get("temperature"), target=s.get("extruder", {}).get("target")),
            bed=Temperature(actual=s.get("heater_bed", {}).get("temperature"), target=s.get("heater_bed", {}).get("target")),
            file=ps.get("filename") or None,
            progress=progress * 100 if progress is not None else None,
            elapsed=elapsed,
            remaining=remaining,
            message=hooks.get("state_message") if klipper_state != "ready" else ps.get("message") or None,
        )

    async def upload(self, gcode: Path, filename: str, start: bool) -> None:
        with gcode.open("rb") as fh:
            await self._request(
                "POST",
                "/server/files/upload",
                files={"file": (filename, fh, "application/octet-stream")},
                data={"root": "gcodes", "print": "true" if start else "false"},
                timeout=UPLOAD_TIMEOUT,
            )

    async def control(self, action: str) -> None:
        if action not in ("pause", "resume", "cancel"):
            raise PrinterError(f"Unsupported action: {action}")
        await self._request("POST", f"/printer/print/{action}")


def make_host(cfg: PrinterConfig, transport: httpx.AsyncBaseTransport | None = None) -> PrintHost:
    return {"octoprint": OctoPrint, "moonraker": Moonraker}[cfg.kind](cfg, transport)
