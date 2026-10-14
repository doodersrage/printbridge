import ftplib
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


BOX_INFO = {
    "opt": "main", "fname": "Info", "function": "get",
    "wifipasswd": "do-not-leak", "ssid": "Creality-ABCD",
    "connect": 1, "tfCard": 1, "err": 0,
    "nozzleTemp": 209, "nozzleTemp2": 210, "bedTemp": 60, "bedTemp2": 60,
    "print": "/media/mmcblk0p1/creality/gztemp/cube.gcode",
    "printProgress": 37, "state": 1, "pause": 0,
    "printJobTime": 600, "printLeftTime": 1200,
}


class FakeFTP:
    def __init__(self):
        self.calls, self.stored = [], {}

    def login(self):
        self.calls.append("login")

    def cwd(self, d):
        self.calls.append(f"cwd {d}")

    def storbinary(self, cmd, fh, blocksize):
        self.stored[cmd.removeprefix("STOR ")] = fh.read()

    def size(self, name):
        return len(self.stored[name])

    def quit(self):
        self.calls.append("quit")


def box(handler, ftp=None, url="192.168.1.50"):
    calls, t = recorder(handler)
    from printbridge.printers import CrealityBox

    return calls, CrealityBox(PrinterConfig(kind="creality_box", url=url), t, ftp_factory=lambda host: ftp)


async def test_box_status_maps_fields_and_hides_secrets():
    calls, host = box(lambda r: httpx.Response(200, json=BOX_INFO))
    st = await host.status()
    req = calls[0]
    assert (req.url.host, req.url.port, req.url.path) == ("192.168.1.50", 81, "/protocal.csp")
    assert req.url.params["fname"] == "Info"
    assert st.state == "Printing" and st.file == "cube.gcode" and st.progress == 37
    assert st.nozzle.actual == 209 and st.nozzle.target == 210 and st.remaining == 1200
    assert "do-not-leak" not in st.model_dump_json()


async def test_box_status_paused_and_warnings():
    info = {**BOX_INFO, "pause": 1, "tfCard": 0, "connect": 0}
    _, host = box(lambda r: httpx.Response(200, json=info))
    st = await host.status()
    assert st.state == "Paused"
    assert "microSD" in st.message and "not connected" in st.message


async def test_box_err_field_is_not_reported_as_an_error():
    _, host = box(lambda r: httpx.Response(200, json={**BOX_INFO, "err": 1}))
    st = await host.status()
    assert st.state == "Printing" and st.message is None


async def test_box_idle_hides_placeholder_filename():
    info = {**BOX_INFO, "state": 0, "print": "Welcome to Creality"}
    _, host = box(lambda r: httpx.Response(200, json=info))
    st = await host.status()
    assert st.state == "Operational" and st.file is None and st.elapsed is None


async def test_box_upload_via_ftp_then_start(sample_gcode):
    ftp = FakeFTP()
    calls, host = box(lambda r: httpx.Response(200, json={"opt": "iot_conf"}), ftp, url="http://192.168.1.50/")
    await host.upload(sample_gcode, "cube.gcode", start=True)
    assert ftp.calls[:2] == ["login", "cwd /mmcblk0p1/creality/gztemp"]
    assert ftp.stored["cube.gcode"] == sample_gcode.read_bytes()
    params = calls[0].url.params
    assert params["fname"] == "net" and params["print"] == "/media/mmcblk0p1/creality/gztemp/cube.gcode"


async def test_box_upload_only_does_not_start(sample_gcode):
    calls, host = box(lambda r: httpx.Response(200), FakeFTP())
    await host.upload(sample_gcode, "cube.gcode", start=False)
    assert calls == []


async def test_box_ftp_failure_becomes_printer_error(sample_gcode):
    class Refused(FakeFTP):
        def login(self):
            raise ConnectionRefusedError("refused")

    _, host = box(lambda r: httpx.Response(200), Refused())
    with pytest.raises(PrinterError, match="FTP upload"):
        await host.upload(sample_gcode, "cube.gcode", start=True)


async def test_box_controls():
    calls, host = box(lambda r: httpx.Response(200))
    await host.control("pause")
    await host.control("resume")
    await host.control("cancel")
    assert [dict(c.url.params) for c in calls] == [
        {"fname": "net", "opt": "iot_conf", "function": "set", "pause": "1"},
        {"fname": "net", "opt": "iot_conf", "function": "set", "pause": "0"},
        {"fname": "net", "opt": "iot_conf", "function": "set", "stop": "1"},
    ]


def test_box_custom_port():
    _, host = box(lambda r: httpx.Response(200), url="http://10.0.0.9:8080")
    assert host.base_url == "http://10.0.0.9:8080" and host.hostname == "10.0.0.9"


async def test_box_upload_tolerates_missing_size_command(sample_gcode):
    class NoSize(FakeFTP):
        def size(self, name):
            raise ftplib.error_perm("502 SIZE not implemented")

    _, host = box(lambda r: httpx.Response(200), NoSize())
    await host.upload(sample_gcode, "cube.gcode", start=False)
