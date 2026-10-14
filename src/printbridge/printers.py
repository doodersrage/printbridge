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
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

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


class RemoteFile(BaseModel):
    name: str
    size: int | None = None
    modified: float | None = None


# Commands are sent as plain G-code where the host allows it.
def _gcode_for(command: str, nozzle: int = 0, bed: int = 0) -> list[str]:
    return {
        "preheat": [f"M104 S{nozzle}", f"M140 S{bed}"],
        "cooldown": ["M104 S0", "M140 S0"],
        "home": ["G28"],
        "fan_on": ["M106 S255"],
        "fan_off": ["M107"],
    }[command]


class PrintHost(ABC):
    supports_light = False

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

    @abstractmethod
    async def send_gcode(self, lines: list[str]) -> None: ...

    async def command(self, command: str, nozzle: int = 0, bed: int = 0) -> None:
        if command in ("light_on", "light_off"):
            raise PrinterError("This print host cannot switch the printer light")
        await self.send_gcode(_gcode_for(command, nozzle, bed))

    @abstractmethod
    async def list_files(self) -> list[RemoteFile]: ...

    @abstractmethod
    async def delete_file(self, name: str) -> None: ...


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

    async def send_gcode(self, lines: list[str]) -> None:
        await self._request("POST", "/api/printer/command", json={"commands": lines})

    async def list_files(self) -> list[RemoteFile]:
        files = (await self._request("GET", "/api/files/local", params={"recursive": "false"})).json().get("files", [])
        return [
            RemoteFile(name=f["name"], size=f.get("size"), modified=f.get("date"))
            for f in files
            if f.get("type") == "machinecode"
        ]

    async def delete_file(self, name: str) -> None:
        await self._request("DELETE", f"/api/files/local/{quote(name)}")


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

    async def send_gcode(self, lines: list[str]) -> None:
        await self._request("POST", "/printer/gcode/script", params={"script": "\n".join(lines)}, timeout=60.0)

    async def list_files(self) -> list[RemoteFile]:
        files = (await self._request("GET", "/server/files/list", params={"root": "gcodes"})).json().get("result", [])
        return [RemoteFile(name=f["path"], size=f.get("size"), modified=f.get("modified")) for f in files]

    async def delete_file(self, name: str) -> None:
        await self._request("DELETE", f"/server/files/gcodes/{quote(name)}")


class CrealityBox(PrintHost):
    """Creality Wi-Fi Box 2.0 (stock firmware).

    Protocol as used by Creality's own CrealityPrint slicer: an unauthenticated
    HTTP API on port 81 for status and control, and anonymous FTP onto the box's
    microSD card for uploads. The box then streams the file to the printer over USB.
    """

    supports_light = True
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
        # The box's "err" field is not an error flag: it reads 1 throughout normal,
        # healthy prints (seen on box firmware V3.04b06), so it is not reported.
        return PrinterStatus(
            online=True,
            state=state,
            nozzle=Temperature(actual=num("nozzleTemp"), target=num("nozzleTemp2")),
            bed=Temperature(actual=num("bedTemp"), target=num("bedTemp2")),
            file=(Path(str(d.get("print", ""))).name or None) if printing else None,
            progress=num("printProgress"),
            elapsed=num("printJobTime") if printing else None,
            remaining=num("printLeftTime") if printing else None,
            message="; ".join(messages) or None,
        )

    def _ftp_upload(self, gcode: Path, filename: str) -> None:
        ftp = self._ftp()  # anonymous login
        try:
            with gcode.open("rb") as fh:
                ftp.storbinary(f"STOR {filename}", fh, blocksize=64 * 1024)
            try:
                size = ftp.size(filename)
            except ftplib.error_perm:  # SIZE is optional in FTP; skip verification
                size = None
            if size is not None and size != gcode.stat().st_size:
                raise PrinterError(f"Upload incomplete: box has {size} of {gcode.stat().st_size} bytes")
        finally:
            self._close(ftp)

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

    async def send_gcode(self, lines: list[str]) -> None:
        for line in lines:
            await self._set(gcodeCmd=line)

    async def command(self, command: str, nozzle: int = 0, bed: int = 0) -> None:
        # Use the box's own parameters (as CrealityPrint does) where it has them.
        if command == "preheat":
            await self._set(nozzleTemp2=str(nozzle))
            await self._set(bedTemp2=str(bed))
        elif command == "cooldown":
            await self._set(nozzleTemp2="0")
            await self._set(bedTemp2="0")
        elif command in ("light_on", "light_off"):
            await self._set(led="1" if command == "light_on" else "0")
        elif command in ("fan_on", "fan_off"):
            await self._set(fan="1" if command == "fan_on" else "0")
        else:
            await super().command(command, nozzle, bed)

    def _ftp(self) -> ftplib.FTP:
        ftp = self._ftp_factory(self.hostname)
        ftp.login()
        ftp.cwd(self.FTP_DIR)
        return ftp

    @staticmethod
    def _close(ftp: ftplib.FTP) -> None:
        try:
            ftp.quit()
        except (OSError, ftplib.Error):
            ftp.close()

    def _ftp_list(self) -> list[RemoteFile]:
        ftp = self._ftp()
        try:
            lines: list[str] = []
            ftp.retrlines("LIST", lines.append)
        finally:
            self._close(ftp)
        return [f for line in lines if (f := _parse_list_line(line))]

    def _ftp_delete(self, name: str) -> None:
        ftp = self._ftp()
        try:
            ftp.delete(name)
        finally:
            self._close(ftp)

    async def list_files(self) -> list[RemoteFile]:
        try:
            return await asyncio.to_thread(self._ftp_list)
        except (OSError, ftplib.Error) as e:
            raise PrinterError(f"Could not list files on the Wi-Fi Box: {e}") from e

    async def delete_file(self, name: str) -> None:
        if "/" in name or name in (".", ".."):
            raise PrinterError("Invalid file name")
        try:
            await asyncio.to_thread(self._ftp_delete, name)
        except (OSError, ftplib.Error) as e:
            raise PrinterError(f"Could not delete {name}: {e}") from e


_MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_list_line(line: str) -> RemoteFile | None:
    """Parse a Unix-style FTP LIST line: '-rwxrwxrwx 1 0 0 1275644 Apr 18  2024 name'."""
    parts = line.split(None, 8)
    if len(parts) < 9 or not parts[0].startswith("-"):
        return None
    try:
        size = int(parts[4])
    except ValueError:
        size = None
    modified = None
    month, day, year_or_time = parts[5], parts[6], parts[7]
    try:
        now = datetime.now()
        if ":" in year_or_time:
            hh, mm = year_or_time.split(":")
            dt = datetime(now.year, _MONTHS[month], int(day), int(hh), int(mm))
            if dt > now:  # LIST omits the year for the last ~6 months
                dt = dt.replace(year=now.year - 1)
        else:
            dt = datetime(int(year_or_time), _MONTHS[month], int(day))
        modified = dt.timestamp()
    except (KeyError, ValueError):
        pass
    return RemoteFile(name=parts[8], size=size, modified=modified)


def make_host(cfg: PrinterConfig, transport: httpx.AsyncBaseTransport | None = None) -> PrintHost:
    return {"octoprint": OctoPrint, "moonraker": Moonraker, "creality_box": CrealityBox}[cfg.kind](cfg, transport)
