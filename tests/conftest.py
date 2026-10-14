import asyncio
import importlib
import sys
import textwrap
from pathlib import Path

import httpx
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


# ---- API test client and fake print host (shared by the API test modules) ----


@pytest.fixture
async def client(tmp_path, monkeypatch, fake_slicer):
    monkeypatch.setenv("PRINTBRIDGE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PRINTBRIDGE_SLICER", str(fake_slicer))
    from printbridge import config, main, runtime

    importlib.reload(config)
    main = importlib.reload(main)
    async with main.app.router.lifespan_context(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.main = main
            c.rt = runtime
            yield c


def set_printer(client, monkeypatch, state="Operational", online=True, file=None):
    from printbridge.printers import PrinterStatus

    st = PrinterStatus(online=online, state=state, file=file)

    async def current(max_age=0):
        return st

    monkeypatch.setattr(client.main.state.monitor, "current", current)
    client.main.state.monitor.status = st
    return st


class FakeHost:
    def __init__(self):
        self.sent, self.commands, self.gcode, self.deleted = [], [], [], []
        self.files = []
        self.supports_light = True

    async def upload(self, path, name, start):
        self.sent.append((path.name, name, start))

    async def command(self, command, nozzle=0, bed=0):
        self.commands.append((command, nozzle, bed))

    async def send_gcode(self, lines):
        self.gcode.extend(lines)

    async def list_files(self):
        return self.files

    async def delete_file(self, name):
        self.deleted.append(name)


@pytest.fixture
def fake_host(client, monkeypatch):
    h = FakeHost()
    monkeypatch.setattr(client.rt, "make_host", lambda cfg: h)
    client.main.state.settings.printer.url = "http://box"
    return h


async def wait_for(client, job_id, *states):
    for _ in range(100):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["state"] in states:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job stuck in {job['state']}")
