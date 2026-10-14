"""Persistent application settings stored as JSON in the data directory."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("PRINTBRIDGE_DATA", Path.cwd() / "data")).resolve()
CONFIG_PATH = DATA_DIR / "config.json"

SLICER_CANDIDATES = (
    "prusa-slicer",
    "PrusaSlicer",
    "prusaslicer",
    "PrusaSlicer.AppImage",
)


class PrinterConfig(BaseModel):
    kind: Literal["creality_box", "octoprint", "moonraker"] = "creality_box"
    url: str = Field("", description="Address of the print host, e.g. 192.168.1.50 or http://octopi.local")
    api_key: str = ""
    verify_tls: bool = True


class NotifyConfig(BaseModel):
    ntfy_url: str = Field("", description="ntfy topic URL, e.g. https://ntfy.sh/my-printer")
    ntfy_token: str = ""
    webhook_url: str = Field("", description="POSTs a JSON event (works with Home Assistant webhooks)")
    smtp_host: str = ""
    smtp_port: int = Field(587, ge=1, le=65535)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = ""
    email_to: str = Field("", description="Comma-separated recipients")
    on_start: bool = False
    on_finish: bool = True
    on_stop: bool = True
    on_error: bool = True


class MqttConfig(BaseModel):
    enabled: bool = False
    host: str = ""
    port: int = Field(1883, ge=1, le=65535)
    username: str = ""
    password: str = ""
    tls: bool = False
    topic_prefix: str = Field("printbridge", pattern=r"^[A-Za-z0-9_-]{1,40}$")
    discovery_prefix: str = Field("homeassistant", pattern=r"^[A-Za-z0-9_/-]{1,60}$")


class Settings(BaseModel):
    printer: PrinterConfig = PrinterConfig()
    slicer_path: str = ""
    slice_timeout: int = 900
    keep_days: int = Field(0, ge=0, le=3650, description="Delete finished jobs older than this; 0 keeps them")
    notify: NotifyConfig = NotifyConfig()
    mqtt: MqttConfig = MqttConfig()
    currency: str = Field("$", max_length=5)
    public_url: str = Field("", description="Address PrintBridge is reached at, used for links in emails")
    webcam_url: str = Field("", description="MJPEG stream or JPEG snapshot URL")


def detect_slicer() -> str:
    env = os.environ.get("PRINTBRIDGE_SLICER")
    if env:
        return env
    for name in SLICER_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return ""


def load_settings() -> Settings:
    if CONFIG_PATH.exists():
        settings = Settings.model_validate_json(CONFIG_PATH.read_text())
    else:
        settings = Settings()
        url = os.environ.get("PRINTBRIDGE_PRINTER_URL")
        if url:
            settings.printer.url = url
            settings.printer.kind = os.environ.get("PRINTBRIDGE_PRINTER_KIND", "creality_box")  # type: ignore[assignment]
            settings.printer.api_key = os.environ.get("PRINTBRIDGE_PRINTER_API_KEY", "")
    if not settings.slicer_path:
        settings.slicer_path = detect_slicer()
    return settings


def save_settings(settings: Settings) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings.model_dump(), indent=2))
    tmp.replace(CONFIG_PATH)
