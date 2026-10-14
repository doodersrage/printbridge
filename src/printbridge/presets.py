"""Named slicing presets, stored as JSON in the data directory."""

from __future__ import annotations

import json
import re

from pydantic import TypeAdapter

from . import config
from .slicer import SliceOptions

NAME_RE = re.compile(r"^[^\x00-\x1f/]{1,60}$")
_ADAPTER = TypeAdapter(dict[str, SliceOptions])


def _path():
    return config.DATA_DIR / "presets.json"


def load() -> dict[str, SliceOptions]:
    path = _path()
    if not path.exists():
        return {}
    return _ADAPTER.validate_json(path.read_text())


def save(presets: dict[str, SliceOptions]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: v.model_dump() for k, v in sorted(presets.items())}, indent=2))
    tmp.replace(path)
