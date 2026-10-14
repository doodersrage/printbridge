"""Download models from a direct file link, and unpack zip archives of models."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

import httpx

MODEL_EXTS = (".3mf", ".stl", ".obj", ".gcode")
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_FILES = 20
_BLOCKING_SITES = ("thingiverse.com", "printables.com", "makerworld.com", "cults3d.com")
_DISPOSITION = re.compile(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", re.I)


class ImportError_(ValueError):
    pass


def normalise(url: str) -> str:
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ImportError_("Enter an http(s) link to a model file")
    return url


def _filename(resp: httpx.Response, url: str) -> str:
    if m := _DISPOSITION.search(resp.headers.get("content-disposition", "")):
        return unquote(m.group(1)).strip()
    return unquote(PurePosixPath(urlsplit(str(resp.url)).path).name) or "download"


async def download(url: str, transport: httpx.AsyncBaseTransport | None = None) -> list[tuple[str, bytes]]:
    """Return (filename, data) for each model file behind the link."""
    url = normalise(url)
    headers = {"User-Agent": "PrintBridge (+https://github.com/doodersrage/printbridge)"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0),
                                     headers=headers, transport=transport) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    host = urlsplit(str(resp.url)).netloc
                    if resp.status_code in (401, 403, 429) and any(h in host for h in _BLOCKING_SITES):
                        raise ImportError_(f"{host} does not allow downloads from apps (HTTP {resp.status_code}). "
                                           "Download the file or zip in your browser and drop it here instead.")
                    raise ImportError_(f"Download failed: HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("text/html"):
                    raise ImportError_("That link opens a web page, not a file. Use a direct download link, "
                                       "or download the file in your browser and drop it here.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise ImportError_("File is larger than 256 MB")
                    chunks.append(chunk)
                name = _filename(resp, url)
    except httpx.HTTPError as e:
        raise ImportError_(f"Download failed: {e}") from e
    data = b"".join(chunks)

    if zipfile.is_zipfile(io.BytesIO(data)) and not name.lower().endswith(".3mf"):
        return from_zip(data)
    if Path(name).suffix.lower() not in MODEL_EXTS:
        raise ImportError_(f"Downloaded '{name}', which is not a 3MF, STL, OBJ, G-code or zip file")
    return [(name, data)]


def from_zip(data: bytes) -> list[tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = PurePosixPath(info.filename).name
            if info.is_dir() or name.startswith(".") or "__MACOSX" in info.filename:
                continue
            if Path(name).suffix.lower() in MODEL_EXTS:
                if info.file_size > MAX_DOWNLOAD:
                    raise ImportError_(f"{name} is larger than 256 MB")
                out.append((name, z.read(info)))
            if len(out) >= MAX_FILES:
                break
    if not out:
        raise ImportError_("The zip file contains no 3MF, STL, OBJ or G-code files")
    return out
