"""Settings, notifications, filament costs, backup and integrations status."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ValidationError
import time

from .. import backup, config, notify, profiles
from ..runtime import _read_upload, state

router = APIRouter()


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


@router.get("/api/settings")
async def get_settings() -> dict:
    return masked(state.settings)


@router.put("/api/settings")
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


@router.post("/api/notify/test")
async def notify_test() -> dict:
    cfg = state.settings.notify
    if not (cfg.ntfy_url or cfg.webhook_url or notify.email_configured(cfg)):
        raise HTTPException(400, "No notification target configured")
    errors = await notify.send(cfg, "test", "Notifications from PrintBridge are working.")
    if errors:
        raise HTTPException(502, "; ".join(errors))
    return {"ok": True}


# ---- filament costs ---------------------------------------------------------


class CostsRequest(BaseModel):
    currency: str = Field("$", max_length=5)
    prices: dict[str, float] = {}


@router.get("/api/costs")
async def get_costs() -> dict:
    prices = {}
    for m in profiles.names("material"):
        try:
            prices[m] = float(profiles.read_values(profiles.resolve("material", m)).get("filament_cost", 0) or 0)
        except ValueError:
            prices[m] = 0.0
    return {"currency": state.settings.currency, "prices": prices}


@router.put("/api/costs")
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


# ---- backup & integrations ----------------------------------------------------


@router.get("/api/backup")
async def get_backup() -> Response:
    stamp = time.strftime("%Y%m%d-%H%M")
    return Response(backup.create(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="printbridge-backup-{stamp}.zip"'})


@router.get("/api/backups")
async def list_auto_backups() -> list[dict]:
    return backup.list_auto()


@router.get("/api/backups/{name}")
async def get_auto_backup(name: str) -> FileResponse:
    names = {b["name"] for b in backup.list_auto()}
    if name not in names:  # only files from the listing, never arbitrary paths
        raise HTTPException(404, "Backup not found")
    return FileResponse(backup.auto_dir() / name, media_type="application/zip", filename=name)


@router.post("/api/restore")
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


@router.get("/api/mqtt/status")
async def mqtt_status() -> dict:
    cfg = state.settings.mqtt
    return {"enabled": cfg.enabled, "connected": state.mqtt.connected, "error": state.mqtt.last_error}
