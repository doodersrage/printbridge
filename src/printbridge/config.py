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


class Settings(BaseModel):
    printer: PrinterConfig = PrinterConfig()
    slicer_path: str = ""
    slice_timeout: int = 900


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
