import json

import httpx
import pytest

from printbridge.config import PrinterConfig
from printbridge.printers import PrinterError, make_host


def recorder(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request)

    return calls, httpx.MockTransport(wrapped)


async def test_octoprint_upload_and_start(sample_gcode):
    calls, t = recorder(lambda r: httpx.Response(201, json={"done": True}))
    host = make_host(PrinterConfig(kind="octoprint", url="http://octopi/", api_key="k"), t)
    await host.upload(sample_gcode, "cube.gcode", start=True)
    req = calls[0]
    assert req.url.path == "/api/files/local"
    assert req.headers["X-Api-Key"] == "k"
    body = req.content.decode()
    assert 'filename="cube.gcode"' in body
    assert 'name="print"\r\n\r\ntrue' in body


async def test_octoprint_status():
    def handler(r: httpx.Request):
        if r.url.path == "/api/job":
            return httpx.Response(200, json={
                "state": "Printing",
                "job": {"file": {"name": "cube.gcode"}},
                "progress": {"completion": 42.5, "printTime": 600, "printTimeLeft": 900},
            })
        return httpx.Response(200, json={"temperature": {"tool0": {"actual": 210.2, "target": 210}, "bed": {"actual": 59.8, "target": 60}}})

    _, t = recorder(handler)
    st = await make_host(PrinterConfig(kind="octoprint", url="http://octopi"), t).status()
    assert st.state == "Printing" and st.file == "cube.gcode" and st.progress == 42.5
    assert st.nozzle.actual == 210.2 and st.bed.target == 60


async def test_octoprint_not_connected_to_serial():
    def handler(r: httpx.Request):
        if r.url.path == "/api/job":
            return httpx.Response(200, json={"state": "Offline", "job": {}, "progress": {}})
        return httpx.Response(409, text="Printer is not operational")

    _, t = recorder(handler)
    st = await make_host(PrinterConfig(kind="octoprint", url="http://octopi"), t).status()
    assert st.state == "Offline" and "not connected" in st.message


async def test_octoprint_controls():
    calls, t = recorder(lambda r: httpx.Response(204))
    host = make_host(PrinterConfig(kind="octoprint", url="http://octopi"), t)
    await host.control("resume")
    assert json.loads(calls[0].content) == {"command": "pause", "action": "resume"}
    with pytest.raises(PrinterError):
        await host.control("explode")


async def test_moonraker_upload_and_status(sample_gcode):
    def handler(r: httpx.Request):
        if r.url.path == "/server/files/upload":
            return httpx.Response(201, json={"result": {}})
        return httpx.Response(200, json={"result": {"status": {
            "print_stats": {"state": "printing", "filename": "cube.gcode", "print_duration": 100.0},
            "virtual_sdcard": {"progress": 0.25},
            "extruder": {"temperature": 205.0, "target": 205.0},
            "heater_bed": {"temperature": 60.0, "target": 60.0},
            "webhooks": {"state": "ready"},
        }}})

    calls, t = recorder(handler)
    host = make_host(PrinterConfig(kind="moonraker", url="http://klipper"), t)
    await host.upload(sample_gcode, "cube.gcode", start=False)
    assert calls[0].url.path == "/server/files/upload"
    assert 'name="print"\r\n\r\nfalse' in calls[0].content.decode()
    st = await host.status()
    assert st.state == "Printing" and st.progress == 25 and st.remaining == pytest.approx(300)


async def test_moonraker_control_path():
    calls, t = recorder(lambda r: httpx.Response(200, json={"result": "ok"}))
    await make_host(PrinterConfig(kind="moonraker", url="http://klipper"), t).control("cancel")
    assert calls[0].url.path == "/printer/print/cancel"


async def test_http_error_becomes_printer_error():
    _, t = recorder(lambda r: httpx.Response(403, text="Invalid API key"))
    with pytest.raises(PrinterError, match="403"):
        await make_host(PrinterConfig(kind="octoprint", url="http://octopi"), t).status()


def test_missing_url():
    with pytest.raises(PrinterError):
        make_host(PrinterConfig(url=""))
