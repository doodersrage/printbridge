import sys
import textwrap
from pathlib import Path

import pytest

SAMPLE_GCODE = textwrap.dedent("""\
    ;LAYER_CHANGE
    G1 X1 Y1 E1
    ;LAYER_CHANGE
    G1 X2 Y2 E1
    ; filament used [mm] = 1751.93
    ; filament used [g] = 5.23
    ; estimated printing time (normal mode) = 1h 18m 41s
    ; layer_height = 0.2
    """)


@pytest.fixture
def sample_gcode(tmp_path: Path) -> Path:
    p = tmp_path / "x.gcode"
    p.write_text(SAMPLE_GCODE)
    return p


@pytest.fixture
def fake_slicer(tmp_path: Path) -> Path:
    """A stand-in for PrusaSlicer that writes canned G-code to --output."""
    script = tmp_path / "fake-slicer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if open(args[-1], 'rb').read().startswith(b'FAIL'):\n"
        "    print('All objects are outside of the print volume.'); sys.exit(0)\n"
        f"open(args[args.index('--output') + 1], 'w').write({SAMPLE_GCODE!r})\n"
    )
    script.chmod(0o755)
    return script
