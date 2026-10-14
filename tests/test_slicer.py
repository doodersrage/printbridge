from pathlib import Path

import pytest

from printbridge.gcode import parse_duration, parse_stats
from printbridge.slicer import SliceOptions, SlicerError, build_command, printer_profile_text, slice_model


def test_parse_stats(sample_gcode):
    s = parse_stats(sample_gcode)
    assert s.print_seconds == 3600 + 18 * 60 + 41
    assert s.filament_g == 5.23
    assert s.layer_height == 0.2
    assert s.layers == 2


def test_parse_duration_days():
    assert parse_duration("1d 2h 3m 4s") == 86400 + 7200 + 180 + 4


def test_bed_mesh_marker_substituted():
    assert "G29" in printer_profile_text(True)
    assert "M420 S1" in printer_profile_text(False)
    assert "@BED_MESH@" not in printer_profile_text(False)


def test_build_command_defaults():
    cmd = build_command("ps", Path("m.3mf"), Path("o.gcode"), Path("p.ini"), SliceOptions())
    assert cmd[0] == "ps" and cmd[-1] == "m.3mf"
    assert "--no-support-material" in cmd
    assert cmd[cmd.index("--fill-density") + 1] == "15%"
    assert cmd[cmd.index("--center") + 1] == "110,110"
    assert cmd[cmd.index("--output") + 1] == "o.gcode"
    assert "--temperature" not in cmd


def test_build_command_overrides():
    opts = SliceOptions(supports="buildplate", arrange=False, nozzle_temp=215, bed_temp=65, quality="fine", material="petg")
    cmd = build_command("ps", Path("m.3mf"), Path("o.gcode"), Path("p.ini"), opts)
    assert "--support-material-buildplate-only" in cmd
    assert "--dont-arrange" in cmd and "--center" not in cmd
    assert cmd[cmd.index("--temperature") + 1] == "215"
    assert cmd[cmd.index("--first-layer-bed-temperature") + 1] == "65"
    assert any(a.endswith("quality/fine.ini") for a in cmd)
    assert any(a.endswith("material/petg.ini") for a in cmd)


def test_every_profile_file_exists():
    for q in ("draft", "standard", "fine"):
        for m in ("pla", "petg", "tpu"):
            cmd = build_command("ps", Path("m"), Path("o"), Path("p"), SliceOptions(quality=q, material=m))
            for i, a in enumerate(cmd):
                if a == "--load" and cmd[i + 1] != "p":
                    assert Path(cmd[i + 1]).is_file()


async def test_slice_model_success(fake_slicer, tmp_path):
    out = tmp_path / "o.gcode"
    (tmp_path / "m.3mf").write_bytes(b"PK")
    res = await slice_model(str(fake_slicer), tmp_path / "m.3mf", out, SliceOptions())
    assert res.gcode_path.exists()


async def test_slice_model_reports_failure(fake_slicer, tmp_path):
    (tmp_path / "fail.3mf").write_bytes(b"FAIL")
    with pytest.raises(SlicerError, match="outside of the print volume"):
        await slice_model(str(fake_slicer), tmp_path / "fail.3mf", tmp_path / "o.gcode", SliceOptions())


async def test_slice_model_missing_slicer(tmp_path):
    with pytest.raises(SlicerError, match="not found"):
        await slice_model("", tmp_path / "m.3mf", tmp_path / "o.gcode", SliceOptions())


async def test_export_preview_uses_same_placement(tmp_path, monkeypatch):
    from printbridge import slicer as slicer_mod

    seen = {}

    async def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        Path(cmd[cmd.index("--output") + 1]).write_bytes(b"solid")
        return 0, ""

    monkeypatch.setattr(slicer_mod, "_run", fake_run)
    monkeypatch.setattr(slicer_mod, "_check_slicer", lambda s: None)
    out = await slicer_mod.export_preview("ps", tmp_path / "m.3mf", tmp_path / "p.stl", SliceOptions(arrange=False))
    assert out.exists()
    assert seen["cmd"][:2] == ["ps", "--export-stl"] and "--dont-arrange" in seen["cmd"]
