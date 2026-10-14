"""Print notifications via ntfy, a generic JSON webhook, and email."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import httpx

from .config import NotifyConfig

log = logging.getLogger("printbridge.notify")

EVENTS = {
    "started": ("on_start", "Print started", "arrow_forward", "▶"),
    "finished": ("on_finish", "Print finished", "white_check_mark", "✅"),
    "stopped": ("on_stop", "Print stopped", "stop_button", "⏹"),
    "error": ("on_error", "Printer error", "warning", "⚠"),
    "test": (None, "PrintBridge test", "bell", "🔔"),
}


def email_configured(cfg: NotifyConfig) -> bool:
    return bool(cfg.smtp_host and cfg.email_to.strip())


def build_email(cfg: NotifyConfig, event: str, message: str, extra: dict, image: bytes | None) -> EmailMessage:
    _, title, _, icon = EVENTS[event]
    msg = EmailMessage()
    msg["Subject"] = f"{icon} {message}"
    msg["From"] = cfg.email_from or cfg.smtp_username or "printbridge@localhost"
    msg["To"] = ", ".join(a.strip() for a in cfg.email_to.split(",") if a.strip())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="printbridge")

    lines = [message, ""]
    for label, key in (("File", "file"), ("Print time", "duration"), ("Filament", "filament"),
                       ("Cost", "cost"), ("Spool", "spool"), ("Printer", "printer_state")):
        if extra.get(key):
            lines.append(f"{label}: {extra[key]}")
    if extra.get("url"):
        lines += ["", f"Open PrintBridge: {extra['url']}"]
    lines += ["", f"— PrintBridge · {title}"]
    msg.set_content("\n".join(lines))
    if image:
        msg.add_attachment(image, maintype="image", subtype="png", filename="print.png")
    return msg


def _deliver(cfg: NotifyConfig, msg: EmailMessage) -> None:
    timeout = 20
    if cfg.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=timeout,
                                                context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=timeout)
    try:
        if cfg.smtp_security == "starttls":
            server.starttls(context=ssl.create_default_context())
        if cfg.smtp_username:
            server.login(cfg.smtp_username, cfg.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            server.close()


async def send(cfg: NotifyConfig, event: str, message: str, extra: dict | None = None,
               transport: httpx.AsyncBaseTransport | None = None, image: bytes | None = None) -> list[str]:
    """Send an event to every configured target. Returns error strings instead of raising."""
    flag, title, tag, _ = EVENTS[event]
    if flag and not getattr(cfg, flag):
        return []
    extra = extra or {}
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
            payload = {"event": event, "title": title, "message": message, "time": time.time(), **extra}
            try:
                (await client.post(cfg.webhook_url, json=payload)).raise_for_status()
            except httpx.HTTPError as e:
                errors.append(f"webhook: {e}")
    if email_configured(cfg):
        try:
            await asyncio.to_thread(_deliver, cfg, build_email(cfg, event, message, extra, image))
        except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
            errors.append(f"email: {e}")
    for err in errors:
        log.warning("Notification failed: %s", err)
    return errors
