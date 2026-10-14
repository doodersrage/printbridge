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

from . import profiles

BED_CENTER = (110, 110)
BED_MESH_MARKER = "@BED_MESH@"


class SliceOptions(BaseModel):
    quality: str = Field("standard", pattern=profiles.NAME_RE.pattern)
    material: str = Field("pla", pattern=profiles.NAME_RE.pattern)
    infill: int = Field(15, ge=0, le=100)
    supports: Literal["none", "buildplate", "everywhere"] = "none"
    brim: float = Field(0, ge=0, le=20, description="Brim width in mm, 0 disables")
    arrange: bool = True
    probe_bed: bool = False
    nozzle_temp: int | None = Field(None, ge=150, le=260)
    bed_temp: int | None = Field(None, ge=0, le=100)
    scale: float = Field(100, ge=10, le=1000, description="Uniform scale in percent")
    rotate: float = Field(0, ge=-360, le=360, description="Rotation around Z in degrees")
    copies: int = Field(1, ge=1, le=50)
    plate: int | None = Field(None, ge=1, description="Plate to print from a multi-plate 3MF")


class SlicerError(RuntimeError):
    pass


@dataclass
class SliceResult:
    gcode_path: Path
    log: str


def printer_profile_text(probe_bed: bool) -> str:
    mesh = "G29 ; probe a fresh bed mesh" if probe_bed else "M420 S1 ; load saved bed mesh"
    return profiles.resolve("printer").read_text().replace(BED_MESH_MARKER, mesh)


def check_options(opts: SliceOptions) -> None:
    """Raise ProfileError if the options name a profile that does not exist."""
    profiles.resolve("quality", opts.quality)
    profiles.resolve("material", opts.material)


def build_command(slicer: str, model: Path, output: Path, printer_ini: Path, opts: SliceOptions) -> list[str]:
    cmd = [
        slicer,
        "--export-gcode",
        "--loglevel", "2",
        "--load", str(printer_ini),
        "--load", str(profiles.resolve("quality", opts.quality)),
        "--load", str(profiles.resolve("material", opts.material)),
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
    cmd += placement_args(opts)
    cmd += ["--output", str(output), str(model)]
    return cmd


def transform_args(opts: SliceOptions) -> list[str]:
    args = []
    if opts.scale != 100:
        args += ["--scale", f"{opts.scale:g}%"]
    if opts.rotate:
        args += ["--rotate", f"{opts.rotate:g}"]
    return args


def placement_args(opts: SliceOptions) -> list[str]:
    """Copies and bed placement, shared by slicing and the 3D preview."""
    args = []
    if opts.copies > 1:
        args += ["--duplicate", str(opts.copies)]
    if opts.arrange or opts.copies > 1:
        args += ["--center", f"{BED_CENTER[0]},{BED_CENTER[1]}"]
    else:
        args += ["--dont-arrange"]
    return args


async def prepare_model(slicer: str, model: Path, workdir: Path, opts: SliceOptions, timeout: int) -> Path:
    """Apply scale and rotation in a separate pass.

    The PrusaSlicer CLI makes copies (--duplicate) before it scales, whatever the
    argument order, so scaled copies would be spaced for the original size and
    overlap. Baking the transform into an intermediate 3MF first avoids that.
    """
    args = transform_args(opts)
    if not args:
        return model
    out = workdir / "transformed.3mf"
    code, log = await _run([slicer, "--export-3mf", "--loglevel", "2", *args, "--dont-arrange",
                            "--output", str(out), str(model)], timeout)
    if code != 0 or not out.exists():
        raise _failure(code, log)
    return out


def headless_env() -> dict[str, str]:
    # PrusaSlicer is a GUI app; make sure the CLI never tries to open a display.
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env


async def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=headless_env()
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SlicerError(f"PrusaSlicer timed out after {timeout}s")
    return proc.returncode or 0, out.decode(errors="replace")


def _check_slicer(slicer: str) -> None:
    if not slicer:
        raise SlicerError("PrusaSlicer was not found. Install it or set the slicer path in Settings.")
    if not (Path(slicer).is_file() or shutil.which(slicer)):
        raise SlicerError(f"Slicer executable not found: {slicer}")


def _failure(code: int, log: str) -> SlicerError:
    tail = "\n".join(log.strip().splitlines()[-15:])
    suffix = f" (exit code {code})" if code else ""
    return SlicerError(f"PrusaSlicer could not process this model{suffix}:\n{tail}")


async def export_preview(slicer: str, model: Path, output: Path, opts: SliceOptions, timeout: int = 120) -> Path:
    """Export the model as one binary STL, positioned on the bed exactly as it will be sliced."""
    _check_slicer(slicer)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="printbridge-") as tmp:
        src = await prepare_model(slicer, model, Path(tmp), opts, timeout)
        cmd = [slicer, "--export-stl", "--loglevel", "2", *placement_args(opts), "--output", str(output), str(src)]
        code, log = await _run(cmd, timeout)
    if code != 0 or not output.exists():
        raise _failure(code, log)
    return output


async def slice_model(slicer: str, model: Path, output: Path, opts: SliceOptions, timeout: int = 900) -> SliceResult:
    _check_slicer(slicer)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="printbridge-") as tmp:
        src = await prepare_model(slicer, model, Path(tmp), opts, timeout)
        printer_ini = Path(tmp) / "printer.ini"
        printer_ini.write_text(printer_profile_text(opts.probe_bed))
        code, log = await _run(build_command(slicer, src, output, printer_ini, opts), timeout)
    if code != 0 or not output.exists():
        raise _failure(code, log)
    return SliceResult(gcode_path=output, log=log)
