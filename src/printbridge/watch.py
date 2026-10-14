"""Watch folder: model files dropped into a folder become jobs automatically.

Point your browser's downloads (or a network share / Syncthing folder) at the
watched folder and every model you download is imported and sliced. The folder
is polled rather than watched with inotify, so it also works on network shares.

A file is imported once its size and modification time have stopped changing,
which skips downloads still in progress. Afterwards it is moved to
"imported/" (or "failed/" with a note). If the folder is read-only, imported
files are remembered instead so they are not imported twice.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from . import config

log = logging.getLogger("printbridge.watch")

MODEL_EXTS = {".3mf", ".stl", ".obj", ".gcode", ".zip"}
PARTIAL_EXTS = {".crdownload", ".part", ".partial", ".download", ".tmp", ".opdownload"}
SETTLE_SECONDS = 3.0
SUBDIRS = ("imported", "failed")


def _seen_path() -> Path:
    return config.DATA_DIR / "watch-seen.json"


def _load_seen() -> set[str]:
    try:
        return set(json.loads(_seen_path().read_text()))
    except (OSError, ValueError):
        return set()


def _save_seen(seen: set[str]) -> None:
    path = _seen_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(seen)[-5000:]))


def _key(p: Path) -> str:
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def _unique(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    stem, suffix, n = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = dest_dir / f"{stem}-{n}{suffix}"
        n += 1
    return dest


class Watcher:
    def __init__(self, folder: Path, importer: Callable[[str, bytes], int]):
        self.folder = folder
        self.importer = importer  # (filename, data) -> number of jobs created; raises ValueError on bad files
        self._sizes: dict[Path, tuple[int, float]] = {}
        self.seen = _load_seen()
        self.last_error: str | None = None
        self.imported = 0

    def candidates(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        out = []
        for p in sorted(self.folder.iterdir()):
            if p.is_symlink() or not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() in PARTIAL_EXTS or p.suffix.lower() not in MODEL_EXTS:
                continue
            out.append(p)
        return out

    def scan(self, now: float | None = None) -> list[str]:
        """Import every settled file once. Returns the names imported this pass."""
        now = now or time.time()
        done = []
        current = set()
        for p in self.candidates():
            current.add(p)
            try:
                st = p.stat()
                key = _key(p)
            except OSError:
                continue
            if key in self.seen:
                continue
            sig = (st.st_size, st.st_mtime)
            if self._sizes.get(p) != sig or now - st.st_mtime < SETTLE_SECONDS:
                self._sizes[p] = sig  # still being written; look again next pass
                continue
            self._import(p, key)
            done.append(p.name)
        self._sizes = {p: s for p, s in self._sizes.items() if p in current}
        return done

    def _import(self, p: Path, key: str) -> None:
        try:
            count = self.importer(p.name, p.read_bytes())
            self.imported += count
            log.info("Imported %s from the watch folder (%d job(s))", p.name, count)
            self._file_away(p, "imported", key)
        except (ValueError, OSError) as e:
            self.last_error = f"{p.name}: {e}"
            log.warning("Could not import %s: %s", p.name, e)
            self._file_away(p, "failed", key, note=str(e))

    def _file_away(self, p: Path, sub: str, key: str, note: str | None = None) -> None:
        try:
            dest_dir = self.folder / sub
            dest_dir.mkdir(exist_ok=True)
            dest = _unique(dest_dir, p.name)
            shutil.move(str(p), dest)
            if note:
                dest.with_name(dest.name + ".error.txt").write_text(note + "\n")
        except OSError:
            # Read-only folder: remember the file so it is not imported again.
            self.seen.add(key)
            _save_seen(self.seen)
