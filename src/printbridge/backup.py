"""Export and restore PrintBridge's configuration as a zip file.

Included: settings (with printer, email and MQTT credentials), presets, profile
overrides and print history. Not included: jobs (models and G-code) and the
session signing key.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import zipfile

from . import config, presets, profiles

FILES = ("config.json", "presets.json", "history.jsonl", "spools.json")
MAX_RESTORE = 20 * 1024 * 1024


AUTO_DIR = "backups"
AUTO_PREFIX = "printbridge-auto-"


class BackupError(ValueError):
    pass


def auto_dir():
    return config.DATA_DIR / AUTO_DIR


def list_auto() -> list[dict]:
    d = auto_dir()
    if not d.exists():
        return []
    files = sorted(d.glob(f"{AUTO_PREFIX}*.zip"), reverse=True)
    return [{"name": f.name, "size": f.stat().st_size, "created": f.stat().st_mtime} for f in files]


def write_auto(keep: int, now: float | None = None) -> str | None:
    """Write today's automatic backup (once per day) and keep the newest `keep`."""
    d = auto_dir()
    d.mkdir(parents=True, exist_ok=True)
    name = f"{AUTO_PREFIX}{time.strftime('%Y-%m-%d', time.localtime(now))}.zip"
    written = None
    if not (d / name).exists():
        # A unique temporary name, so two writers can never trip over each other.
        fd, tmp_name = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".zip")
        with os.fdopen(fd, "wb") as fh:
            fh.write(create())
        os.replace(tmp_name, d / name)
        written = name
    for old in sorted(d.glob(f"{AUTO_PREFIX}*.zip"), reverse=True)[max(keep, 1):]:
        old.unlink()
    return written


def create() -> bytes:
    data_dir = config.DATA_DIR
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("printbridge-backup.json", json.dumps({"format": 1, "created": time.time()}))
        for name in FILES:
            if (data_dir / name).exists():
                z.write(data_dir / name, name)
        prof = profiles.override_dir()
        for path in sorted(prof.rglob("*.ini")) if prof.exists() else []:
            z.write(path, f"profiles/{path.relative_to(prof).as_posix()}")
    return buf.getvalue()


def restore(blob: bytes) -> list[str]:
    """Validate the whole archive first, then write it. Returns the restored file names."""
    if len(blob) > MAX_RESTORE:
        raise BackupError("Backup file is too large")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as e:
        raise BackupError("Not a PrintBridge backup (not a zip file)") from e
    names = z.namelist()
    if "printbridge-backup.json" not in names:
        raise BackupError("Not a PrintBridge backup")

    writes: dict[str, bytes] = {}
    for name in names:
        if name in ("printbridge-backup.json",) or name.endswith("/"):
            continue
        data = z.read(name)
        if name == "config.json":
            config.Settings.model_validate_json(data)
        elif name == "presets.json":
            presets._ADAPTER.validate_json(data)
        elif name == "history.jsonl":
            pass
        elif name == "spools.json":
            from . import spools

            spools._ADAPTER.validate_json(data)
        elif name.startswith("profiles/") and name.endswith(".ini"):
            parts = name.split("/")[1:]
            if parts == ["printer.ini"]:
                pass
            elif len(parts) == 2 and parts[0] in ("quality", "material") and profiles.NAME_RE.match(parts[1][:-4]):
                pass
            else:
                raise BackupError(f"Unexpected profile path in backup: {name}")
            profiles.validate_text(data.decode("utf-8", errors="replace"))
        else:
            raise BackupError(f"Unexpected file in backup: {name}")
        writes[name] = data

    data_dir = config.DATA_DIR
    for name, data in writes.items():
        path = data_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return sorted(writes)
