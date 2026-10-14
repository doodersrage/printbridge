"""FastAPI application: upload models, slice them for the Ender-3 V3 SE, send to the printer."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config
from .auth import COOKIE_NAME, PUBLIC_PATHS, load_auth
from .jobs import JobStore
from .monitor import Monitor
from .mqtt import MqttBridge
from .routes import auth, jobs, printer, profiles, settings
from .runtime import backup_loop, log, mqtt_command, prune_loop, spawn, state, supports_light

STATIC_DIR = Path(__file__).parent / "static"


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
    spawn(backup_loop())
    log.info("PrintBridge %s started", __version__)
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


for module in (auth, settings, profiles, jobs, printer):
    app.include_router(module.router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
