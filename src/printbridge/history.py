"""Print history: one JSON line per finished or stopped print, kept independently of jobs."""

from __future__ import annotations

import time
from datetime import datetime

from pydantic import BaseModel

from . import config


class PrintRecord(BaseModel):
    job_id: str | None = None
    name: str
    file: str | None = None
    material: str | None = None
    quality: str | None = None
    started_at: float | None = None
    ended_at: float
    duration: float | None = None  # seconds
    result: str  # "printed" | "stopped"
    progress: float | None = None
    filament_g: float | None = None
    cost: float | None = None
    spool: str | None = None


def _path():
    return config.DATA_DIR / "history.jsonl"


def append(record: PrintRecord) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(record.model_dump_json() + "\n")


def load() -> list[PrintRecord]:
    path = _path()
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(PrintRecord.model_validate_json(line))
        except ValueError:
            continue  # skip a damaged line rather than losing the whole history
    return out


def _totals(records: list[PrintRecord]) -> dict:
    printed = [r for r in records if r.result == "printed"]
    return {
        "prints": len(records),
        "printed": len(printed),
        "success_rate": round(100 * len(printed) / len(records), 1) if records else None,
        "hours": round(sum(r.duration or 0 for r in records) / 3600, 1),
        "filament_g": round(sum(r.filament_g or 0 for r in records), 1),
        "cost": round(sum(r.cost or 0 for r in records), 2),
    }


def summary(limit: int = 200, now: float | None = None) -> dict:
    records = load()
    now_dt = datetime.fromtimestamp(now or time.time())
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {
        "all": _totals(records),
        "month": _totals([r for r in records if r.ended_at >= month_start]),
        "records": [r.model_dump() for r in reversed(records[-limit:])],
    }
