"""Clients for the print hosts that put an Ender-3 V3 SE on the network.

The V3 SE has no network interface of its own; it is reached over USB through
a host such as the Creality Wi-Fi Box 2.0, OctoPrint (Raspberry Pi + stock
Marlin) or Klipper/Moonraker.
"""

from __future__ import annotations

import asyncio
import ftplib
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

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


class CrealityBox(PrintHost):
    """Creality Wi-Fi Box 2.0 (stock firmware).

    Protocol as used by Creality's own CrealityPrint slicer: an unauthenticated
    HTTP API on port 81 for status and control, and anonymous FTP onto the box's
    microSD card for uploads. The box then streams the file to the printer over USB.
    """

    HTTP_PORT = 81
    FTP_DIR = "/mmcblk0p1/creality/gztemp"
    PRINT_DIR = "/media/mmcblk0p1/creality/gztemp"
    _STATES = {0: "Operational", 1: "Printing", 2: "Complete", 3: "Operational", 4: "Operational", 5: "Paused"}

    def __init__(
        self,
        cfg: PrinterConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        ftp_factory: Callable[[str], ftplib.FTP] | None = None,
    ):
        super().__init__(cfg, transport)
        url = cfg.url if "://" in cfg.url else f"http://{cfg.url}"
        parts = urlsplit(url)
        if not parts.hostname:
            raise PrinterError(f"Invalid Wi-Fi Box address: {cfg.url}")
        self.hostname = parts.hostname
        self.base_url = f"http://{self.hostname}:{parts.port or self.HTTP_PORT}"
        self._ftp_factory = ftp_factory or (lambda host: ftplib.FTP(host, timeout=30))

    def client(self, timeout: httpx.Timeout | float = 10.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=self._transport)

    async def _set(self, **params: str) -> None:
        await self._request("GET", "/protocal.csp", params={"fname": "net", "opt": "iot_conf", "function": "set", **params})

    async def status(self) -> PrinterStatus:
        r = await self._request("GET", "/protocal.csp", params={"fname": "Info", "opt": "main", "function": "get"})
        try:
            d = r.json()
        except ValueError as e:
            raise PrinterError("Unexpected response from the Wi-Fi Box") from e

        # Only read the fields we need: this payload also contains the Wi-Fi password.
        def num(key: str) -> float | None:
            try:
                return float(d[key])
            except (KeyError, TypeError, ValueError):
                return None

        state_id = int(num("state") or 0)
        state = self._STATES.get(state_id, f"State {state_id}")
        if num("pause") == 1 and state_id == 1:
            state = "Paused"
        printing = state in ("Printing", "Paused")
        messages = []
        if num("connect") == 0:
            messages.append("Wi-Fi Box is not connected to the printer")
        if num("tfCard") == 0:
            messages.append("No microSD card in the Wi-Fi Box; uploads need one")
        if num("err"):
            messages.append(f"Printer reported error {int(num('err'))}")
        return PrinterStatus(
            online=True,
            state=state,
            nozzle=Temperature(actual=num("nozzleTemp"), target=num("nozzleTemp2")),
            bed=Temperature(actual=num("bedTemp"), target=num("bedTemp2")),
            file=(Path(str(d.get("print", ""))).name or None) if printing else None,
            progress=num("printProgress") if printing else None,
            elapsed=num("printJobTime") if printing else None,
            remaining=num("printLeftTime") if printing else None,
            message="; ".join(messages) or None,
        )

    def _ftp_upload(self, gcode: Path, filename: str) -> None:
        ftp = self._ftp_factory(self.hostname)
        try:
            ftp.login()  # anonymous
            ftp.cwd(self.FTP_DIR)
            with gcode.open("rb") as fh:
                ftp.storbinary(f"STOR {filename}", fh, blocksize=64 * 1024)
            try:
                size = ftp.size(filename)
            except ftplib.error_perm:  # SIZE is optional in FTP; skip verification
                size = None
            if size is not None and size != gcode.stat().st_size:
                raise PrinterError(f"Upload incomplete: box has {size} of {gcode.stat().st_size} bytes")
        finally:
            try:
                ftp.quit()
            except (OSError, ftplib.Error):
                ftp.close()

    async def upload(self, gcode: Path, filename: str, start: bool) -> None:
        try:
            await asyncio.to_thread(self._ftp_upload, gcode, filename)
        except (OSError, ftplib.Error) as e:
            raise PrinterError(f"FTP upload to the Wi-Fi Box failed: {e}") from e
        if start:
            await self._set(print=f"{self.PRINT_DIR}/{filename}")

    async def control(self, action: str) -> None:
        params = {"pause": {"pause": "1"}, "resume": {"pause": "0"}, "cancel": {"stop": "1"}}.get(action)
        if params is None:
            raise PrinterError(f"Unsupported action: {action}")
        await self._set(**params)


def make_host(cfg: PrinterConfig, transport: httpx.AsyncBaseTransport | None = None) -> PrintHost:
    return {"octoprint": OctoPrint, "moonraker": Moonraker, "creality_box": CrealityBox}[cfg.kind](cfg, transport)
