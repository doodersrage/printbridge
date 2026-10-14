"""Print notifications via ntfy and/or a generic JSON webhook."""

from __future__ import annotations

import logging
import time

import httpx

from .config import NotifyConfig

log = logging.getLogger("printbridge.notify")

EVENTS = {
    "started": ("on_start", "Print started", "arrow_forward"),
    "finished": ("on_finish", "Print finished", "white_check_mark"),
    "stopped": ("on_stop", "Print stopped", "stop_button"),
    "error": ("on_error", "Printer error", "warning"),
    "test": (None, "PrintBridge test", "bell"),
}


async def send(cfg: NotifyConfig, event: str, message: str, extra: dict | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> list[str]:
    """Send an event to every configured target. Returns error strings instead of raising."""
    flag, title, tag = EVENTS[event]
    if flag and not getattr(cfg, flag):
        return []
    errors = []
    async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
        if cfg.ntfy_url:
            headers = {"Title": title, "Tags": tag}
            if cfg.ntfy_token:
                headers["Authorization"] = f"Bearer {cfg.ntfy_token}"
            try:
                (await client.post(cfg.ntfy_url, content=message.encode(), headers=headers)).raise_for_status()
            except httpx.HTTPError as e:
                errors.append(f"ntfy: {e}")
        if cfg.webhook_url:
            payload = {"event": event, "title": title, "message": message, "time": time.time(), **(extra or {})}
            try:
                (await client.post(cfg.webhook_url, json=payload)).raise_for_status()
            except httpx.HTTPError as e:
                errors.append(f"webhook: {e}")
    for err in errors:
        log.warning("Notification failed: %s", err)
    return errors
