"""Wrapper around the PrusaSlicer command line interface."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

PROFILE_DIR = Path(__file__).parent / "profiles"
PRINTER_PROFILE = PROFILE_DIR / "ender3v3se.ini"
BED_CENTER = (110, 110)
BED_MESH_MARKER = "@BED_MESH@"

QUALITIES = {
    "draft": "Draft — 0.28 mm",
    "standard": "Standard — 0.20 mm",
    "fine": "Fine — 0.12 mm",
}
MATERIALS = {
    "pla": "PLA",
    "petg": "PETG",
    "tpu": "TPU (95A)",
}


class SliceOptions(BaseModel):
    quality: Literal["draft", "standard", "fine"] = "standard"
    material: Literal["pla", "petg", "tpu"] = "pla"
    infill: int = Field(15, ge=0, le=100)
    supports: Literal["none", "buildplate", "everywhere"] = "none"
    brim: float = Field(0, ge=0, le=20, description="Brim width in mm, 0 disables")
    arrange: bool = True
    probe_bed: bool = False
    nozzle_temp: int | None = Field(None, ge=150, le=260)
    bed_temp: int | None = Field(None, ge=0, le=100)


class SlicerError(RuntimeError):
    pass


@dataclass
class SliceResult:
    gcode_path: Path
    log: str


def printer_profile_text(probe_bed: bool) -> str:
    mesh = "G29 ; probe a fresh bed mesh" if probe_bed else "M420 S1 ; load saved bed mesh"
    return PRINTER_PROFILE.read_text().replace(BED_MESH_MARKER, mesh)


def build_command(slicer: str, model: Path, output: Path, printer_ini: Path, opts: SliceOptions) -> list[str]:
    cmd = [
        slicer,
        "--export-gcode",
        "--loglevel", "2",
        "--load", str(printer_ini),
        "--load", str(PROFILE_DIR / "quality" / f"{opts.quality}.ini"),
        "--load", str(PROFILE_DIR / "material" / f"{opts.material}.ini"),
        "--fill-density", f"{opts.infill}%",
        "--brim-width", str(opts.brim),
    ]
    if opts.supports == "none":
        cmd += ["--no-support-material"]
    else:
        cmd += ["--support-material", "--support-material-auto"]
        if opts.supports == "buildplate":
            cmd += ["--support-material-buildplate-only"]
        else:
            cmd += ["--no-support-material-buildplate-only"]
    if opts.nozzle_temp is not None:
        cmd += ["--temperature", str(opts.nozzle_temp), "--first-layer-temperature", str(opts.nozzle_temp)]
    if opts.bed_temp is not None:
        cmd += ["--bed-temperature", str(opts.bed_temp), "--first-layer-bed-temperature", str(opts.bed_temp)]
    if opts.arrange:
        cmd += ["--center", f"{BED_CENTER[0]},{BED_CENTER[1]}"]
    else:
        cmd += ["--dont-arrange"]
    cmd += ["--output", str(output), str(model)]
    return cmd


async def slice_model(slicer: str, model: Path, output: Path, opts: SliceOptions, timeout: int = 900) -> SliceResult:
    if not slicer:
        raise SlicerError("PrusaSlicer was not found. Install it or set the slicer path in Settings.")
    if not (Path(slicer).is_file() or shutil.which(slicer)):
        raise SlicerError(f"Slicer executable not found: {slicer}")

    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="printbridge-") as tmp:
        printer_ini = Path(tmp) / "printer.ini"
        printer_ini.write_text(printer_profile_text(opts.probe_bed))
        cmd = build_command(slicer, model, output, printer_ini, opts)
        # PrusaSlicer is a GUI app; make sure the CLI never tries to open a display.
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise SlicerError(f"Slicing timed out after {timeout}s")

    log = out.decode(errors="replace")
    if proc.returncode != 0 or not output.exists():
        tail = "\n".join(log.strip().splitlines()[-15:])
        code = f" (exit code {proc.returncode})" if proc.returncode else ""
        raise SlicerError(f"PrusaSlicer could not slice this model{code}:\n{tail}")
    return SliceResult(gcode_path=output, log=log)
