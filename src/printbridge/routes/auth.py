"""Login, logout and the public health check."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .. import __version__
from ..auth import COOKIE_NAME
from ..runtime import state

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "static"

router = APIRouter()


# ---- auth -----------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/login", include_in_schema=False)
async def login_page() -> Response:
    if not state.auth.enabled:
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@router.post("/api/auth/login")
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


@router.post("/api/auth/logout")
async def logout() -> Response:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


@router.get("/api/auth/status")
async def auth_status() -> dict:
    return {"enabled": state.auth.enabled, "username": state.auth.username if state.auth.enabled else None}
