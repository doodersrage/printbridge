"""Slice jobs, persisted as one directory per job under the data directory."""

from __future__ import annotations

import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .gcode import GcodeStats
from .slicer import SliceOptions
from .calibration import CalibrationSpec
from .threemf import Plate

JobState = Literal["queued", "slicing", "ready", "failed", "sending", "sent", "printing", "printed", "stopped"]
# States in which the job's files are not being worked on and it may be deleted or re-sliced.
IDLE_STATES = ("ready", "failed", "sent", "printed", "stopped")


class Job(BaseModel):
    id: str
    name: str
    source: str
    created: float
    state: JobState = "queued"
    options: SliceOptions | None = None
    stats: GcodeStats | None = None
    error: str | None = None
    sent_at: float | None = None
    started_print: bool = False
    finished_at: float | None = None
    plates: list[Plate] | None = None
    thumbnail: bool = False
    calibration: CalibrationSpec | None = None

    @property
    def gcode_name(self) -> str:
        return f"{self.name}.gcode"


def safe_stem(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return stem[:80] or "model"


class JobStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        for meta in root.glob("*/job.json"):
            job = Job.model_validate_json(meta.read_text())
            if job.state in ("queued", "slicing", "sending"):
                job.state, job.error = "failed", "Interrupted by a server restart"
            self._jobs[job.id] = job

    def dir(self, job: Job) -> Path:
        return self.root / job.id

    def source_path(self, job: Job) -> Path:
        return self.dir(job) / job.source

    def gcode_path(self, job: Job) -> Path:
        return self.dir(job) / job.gcode_name

    def preview_path(self, job: Job) -> Path:
        return self.dir(job) / "preview.stl"

    def thumbnail_path(self, job: Job) -> Path:
        return self.dir(job) / "thumbnail.png"

    def toolpath_path(self, job: Job) -> Path:
        return self.dir(job) / "toolpaths.bin"

    def plate_path(self, job: Job, plate: int) -> Path:
        return self.dir(job) / f"plate-{plate}.3mf"

    def find_by_gcode_name(self, name: str) -> Job | None:
        """Most recently sent job whose G-code has this file name."""
        name = Path(name).name
        matches = [j for j in self._jobs.values() if j.gcode_name == name and j.sent_at]
        return max(matches, key=lambda j: j.sent_at or 0, default=None)

    def prune(self, max_age_days: int) -> list[str]:
        """Delete idle jobs older than max_age_days. Returns the deleted job ids."""
        if max_age_days <= 0:
            return []
        cutoff = time.time() - max_age_days * 86400
        old = [j for j in self._jobs.values() if j.state in IDLE_STATES and (j.finished_at or j.sent_at or j.created) < cutoff]
        for job in old:
            self.delete(job)
        return [j.id for j in old]

    def create(self, filename: str, options: SliceOptions | None) -> Job:
        ext = Path(filename).suffix.lower()
        job = Job(
            id=uuid.uuid4().hex[:12],
            name=safe_stem(filename),
            source=f"source{ext}",
            created=time.time(),
            options=options,
        )
        self.dir(job).mkdir(parents=True)
        self._jobs[job.id] = job
        self.save(job)
        return job

    def save(self, job: Job) -> None:
        (self.dir(job) / "job.json").write_text(job.model_dump_json(indent=2))

    def update(self, job: Job, **changes) -> Job:
        for k, v in changes.items():
            setattr(job, k, v)
        self.save(job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def delete(self, job: Job) -> None:
        self._jobs.pop(job.id, None)
        shutil.rmtree(self.dir(job), ignore_errors=True)
