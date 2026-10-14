"""End-to-end browser tests: a real Chromium drives PrintBridge with a real PrusaSlicer.

Skipped unless PRINTBRIDGE_E2E=1. Either:
  * PRINTBRIDGE_SLICER points at PrusaSlicer and the suite starts PrintBridge itself, or
  * PRINTBRIDGE_E2E_URL points at a running PrintBridge (e.g. the Docker image) whose
    print host is http://127.0.0.1:8766 with API key "e2e-key" (the mock started here).

Needs `playwright` (and a Chromium: `playwright install chromium`, or set
PRINTBRIDGE_E2E_CHROMIUM to a system Chromium).
"""

from __future__ import annotations

import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import pytest

if os.environ.get("PRINTBRIDGE_E2E") != "1":
    pytest.skip("browser tests run with PRINTBRIDGE_E2E=1", allow_module_level=True)
playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect

from printbridge import calibration  # noqa: E402

HERE = Path(__file__).parent
FIXTURES = HERE.parent / "fixtures"
MOCK = "http://127.0.0.1:8766"
APP_PORT = 8746


# ---- helpers ------------------------------------------------------------------------

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
ROOT_RELS = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
             "</Relationships>")


def package(z: zipfile.ZipFile) -> None:
    """The two files every 3MF package has."""
    z.writestr("[Content_Types].xml", CONTENT_TYPES)
    z.writestr("_rels/.rels", ROOT_RELS)


def free_lan_ip() -> str:
    """This machine's outward-facing address (link import refuses loopback)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def get_json(url: str):
    return json.loads(urllib.request.urlopen(url, timeout=10).read())


def post_json(url: str, body: dict | None = None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read() or b"null")


def wait_until(pred, what: str, timeout: float = 120):
    # Polls the server only. Don't use it while waiting for a browser dialog: the sync
    # Playwright API only delivers dialog events during its own calls.
    end = time.time() + timeout
    while time.time() < end:
        if result := pred():
            return result
        time.sleep(0.4)
    raise AssertionError(f"timed out waiting for {what}")


def write_bambu_plates(path: Path) -> Path:
    """Two plates (20 mm and 30 mm cubes), Bambu/Orca production-extension layout."""
    def cube(oid: int, s: float) -> str:
        v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0), (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
        f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        verts = "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
        tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
        return (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{NS}" xmlns:p="{P}"><resources>'
                f'<object id="{oid}" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles>'
                "</mesh></object></resources><build/></model>")
    NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
    P = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
    main = (f'<?xml version="1.0"?><model unit="millimeter" xmlns="{NS}" xmlns:p="{P}" requiredextensions="p">'
            '<metadata name="Application">BambuStudio-01.10.00.00</metadata><resources>'
            '<object id="2" type="model"><components><component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>'
            '<object id="4" type="model"><components><component p:path="/3D/Objects/object_3.model" objectid="3"/></components></object>'
            '</resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 118 118 0"/>'
            '<item objectid="4" transform="1 0 0 0 1 0 0 0 1 425 113 0"/></build></model>')
    cfg = ('<config><object id="2"><metadata key="name" value="small_cube"/></object>'
           '<object id="4"><metadata key="name" value="big_cube"/></object>'
           '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="2"/>'
           '<metadata key="instance_id" value="0"/></model_instance></plate>'
           '<plate><metadata key="plater_id" value="2"/><model_instance><metadata key="object_id" value="4"/>'
           '<metadata key="instance_id" value="0"/></model_instance></plate></config>')
    rels = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/Objects/object_1.model" Id="a" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            '<Relationship Target="/3D/Objects/object_3.model" Id="b" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
            "</Relationships>")
    with zipfile.ZipFile(path, "w") as z:
        package(z)
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr("3D/Objects/object_1.model", cube(1, 20))
        z.writestr("3D/Objects/object_3.model", cube(3, 30))
        z.writestr("Metadata/model_settings.config", cfg)
    return path


def write_a1mini_3mf(path: Path) -> Path:
    """A 40 x 20 x 10 mm box stored at 80% scale in a project set up for a Bambu A1 mini."""
    v = [(0, 0, 0), (40, 0, 0), (40, 20, 0), (0, 20, 0), (0, 0, 10), (40, 0, 10), (40, 20, 10), (0, 20, 10)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
         (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    verts = "".join(f'<vertex x="{a}" y="{b}" z="{c}"/>' for a, b, c in v)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in f)
    model = ('<?xml version="1.0"?><model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><object id="1" type="model"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
             '</resources><build><item objectid="1" transform="0.8 0 0 0 0.8 0 0 0 0.8 74 82 0"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        package(z)
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/project_settings.config",
                   json.dumps({"printer_model": "Bambu Lab A1 mini", "printable_area": ["0x0", "180x0", "180x180", "0x180"]}))
    return path


# ---- fixtures -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def models(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("models")
    tab = calibration.write_stl(d / "cube.stl", calibration.box(0, 0, 0, 20, 20, 20) + calibration.box(20, 5, 12, 30, 15, 20))
    bundle = d / "bundle.zip"
    with zipfile.ZipFile(bundle, "w") as z:
        z.write(tab, "parts/cube-a.stl")
        z.write(FIXTURES / "two-blocks.3mf", "parts/blocks-b.3mf")
        z.writestr("readme.txt", "hi")
    www = d / "www"
    www.mkdir()
    (www / "linked-cube.stl").write_bytes(tab.read_bytes())
    return {"cube": tab, "bundle": bundle, "plates": write_bambu_plates(d / "plates.3mf"),
            "a1mini": write_a1mini_3mf(d / "esp32-case-a1mini.3mf"), "www": www}


@pytest.fixture(scope="module")
def mock_printer():
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "mock_printer:app", "--port", "8766", "--log-level", "warning"],
                            cwd=HERE)
    wait_until(lambda: _up(MOCK + "/_commands"), "mock printer", 30)
    yield MOCK
    proc.terminate()


@pytest.fixture(scope="module")
def file_server(models):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(models["www"]), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8767), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://{free_lan_ip()}:8767"
    srv.shutdown()


@pytest.fixture(scope="module")
def smtp():
    from aiosmtpd.controller import Controller

    class Handler:
        messages: list[bytes] = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append(envelope.content)
            return "250 OK"

    h = Handler()
    ctl = Controller(h, hostname="127.0.0.1", port=8825)
    ctl.start()
    yield h
    ctl.stop()


def _up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=2)
        return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def app_url(tmp_path_factory, mock_printer):
    if url := os.environ.get("PRINTBRIDGE_E2E_URL"):
        wait_until(lambda: _up(url + "/healthz"), "PrintBridge", 60)
        yield url.rstrip("/")
        return
    if not os.environ.get("PRINTBRIDGE_SLICER"):
        pytest.skip("set PRINTBRIDGE_SLICER (or PRINTBRIDGE_E2E_URL)")
    env = {**os.environ, "PRINTBRIDGE_DATA": str(tmp_path_factory.mktemp("data")), "PRINTBRIDGE_PORT": str(APP_PORT),
           "PRINTBRIDGE_PRINTER_KIND": "octoprint", "PRINTBRIDGE_PRINTER_URL": MOCK, "PRINTBRIDGE_PRINTER_API_KEY": "e2e-key"}
    proc = subprocess.Popen([sys.executable, "-c", "from printbridge import serve; serve()"], env=env)
    url = f"http://127.0.0.1:{APP_PORT}"
    wait_until(lambda: _up(url + "/healthz"), "PrintBridge", 60)
    yield url
    proc.terminate()


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        exe = os.environ.get("PRINTBRIDGE_E2E_CHROMIUM") or None
        b = p.chromium.launch(executable_path=exe, args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        yield b
        b.close()


@pytest.fixture
def page(browser, app_url, mock_printer):
    post_json(MOCK + "/_reset")
    pg = browser.new_page(viewport={"width": 1280, "height": 1000})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.answers = []
    pg.dialog_handler = None

    def on_dialog(d):
        if pg.dialog_handler:
            return pg.dialog_handler(d)
        d.accept(pg.answers.pop(0) if pg.answers else None) if d.type == "prompt" else d.accept()

    pg.on("dialog", on_dialog)
    pg.goto(app_url)
    expect(pg.locator("#printer-state")).to_have_text("Operational", timeout=15000)
    yield pg
    assert pg.errors == []
    pg.close()


def jobs(app_url):
    return get_json(app_url + "/api/jobs")


def job_named(app_url, name, state="ready", timeout=150):
    def find():
        for j in jobs(app_url):
            if j["name"] == name:
                if j["state"] == "failed":
                    raise AssertionError(f"{name} failed: {j['error']}")
                if j["state"] == state:
                    return j
        return None
    return wait_until(find, f"{name} to be {state}", timeout)


def row(pg, name, state=None):
    r = pg.locator(".job").filter(has=pg.locator(".job-name", has_text=re.compile(f"^{re.escape(name)}$")))
    if state:  # wait for the page's own job list (polled every 2 s) to catch up
        expect(r.locator(".job-state")).to_have_text(state, timeout=15000)
    return r


# ---- tests --------------------------------------------------------------------------


def test_upload_slice_preview_and_print(page, app_url, models):
    pg = page
    pg.fill("input[name=scale]", "150")
    pg.fill("input[name=copies]", "2")
    pg.set_input_files("#file", str(models["cube"]))
    pg.click("#upload-btn")
    job = job_named(app_url, "cube")
    assert job["options"]["scale"] == 150 and job["options"]["copies"] == 2 and job["stats"]["filament_g"] > 0

    row(pg, "cube", "ready").locator('[data-act="view"]').click()
    pg.wait_for_function("document.getElementById('viewer-status').hidden", timeout=30000)
    assert "×" in pg.inner_text("#viewer-info")
    pg.click("#tab-toolpaths")
    pg.wait_for_function("!document.getElementById('layer-bar').hidden && document.getElementById('viewer-status').hidden",
                         timeout=30000)
    assert re.search(r"\d+ layers", pg.inner_text("#viewer-info"))
    pg.click("#viewer-close")

    pg.click('[data-cmd="preheat"]')
    wait_until(lambda: "M104 S210" in get_json(MOCK + "/_commands"), "preheat")

    row(pg, "cube").locator('[data-act="print"]').click()
    job_named(app_url, "cube", "printing", 30)
    expect(pg.locator('[data-cmd="preheat"]')).to_be_disabled(timeout=12000)  # busy guard
    post_json(MOCK + "/_set", {"state": "Operational", "progress": 100.0})
    job_named(app_url, "cube", "printed", 30)
    assert get_json(app_url + "/api/history")["all"]["printed"] == 1


def test_multi_plate_and_wrong_printer_3mf(page, app_url, models):
    pg = page
    pg.set_input_files("#file", str(models["plates"]))
    pg.click("#upload-btn")
    first = job_named(app_url, "plates")
    row(pg, "plates", "ready").locator(".job-plate").select_option("2")
    second = wait_until(lambda: next((j for j in jobs(app_url) if j["name"] == "plates" and j["options"]["plate"] == 2
                                      and j["state"] == "ready"), None), "plate 2")
    assert second["stats"]["filament_g"] > first["stats"]["filament_g"]

    pg.set_input_files("#file", str(models["a1mini"]))
    pg.click("#upload-btn")
    job = job_named(app_url, "esp32-case-a1mini")
    assert job["size"] == [32.0, 16.0, 8.0]
    r = row(pg, "esp32-case-a1mini", "ready")
    expect(r.locator(".warn-text")).to_contain_text("Bambu Lab A1 mini", timeout=10000)
    r.locator('[data-act="orig-size"]').click()
    job = wait_until(lambda: next((j for j in jobs(app_url) if j["name"] == "esp32-case-a1mini"
                                   and j["options"]["original_size"] and j["state"] == "ready" and j["size"]), None), "orig size")
    assert job["size"] == [40.0, 20.0, 10.0]


def test_imports_combine_calibration_and_spools(page, app_url, models, file_server):
    pg = page
    pg.set_input_files("#file", str(models["bundle"]))
    pg.click("#upload-btn")
    pg.fill("#import-url", f"{file_server}/linked-cube.stl")
    pg.click("#import-btn")
    for name in ("cube-a", "blocks-b", "linked-cube"):
        job_named(app_url, name)
    pg.fill("#import-url", f"{app_url}/healthz")
    pg.click("#import-btn")
    expect(pg.locator("#upload-err")).to_contain_text("not allowed")

    wait_until(lambda: all(j["thumbnail"] for j in jobs(app_url) if j["state"] == "ready"), "thumbnails", 60)

    row(pg, "cube-a", "ready").locator(".job-select").check()
    row(pg, "blocks-b").locator(".job-select").check()
    pg.answers.append("my-plate")
    pg.click("#combine-btn")
    job_named(app_url, "my-plate")

    pg.click("#open-calibrate")
    expect(pg.locator("#calibrate-form input[name=start]")).to_have_value("220", timeout=5000)
    pg.locator("#calibrate-form button[type=submit]").click()
    job_named(app_url, "temperature-tower-pla")
    r = row(pg, "temperature-tower-pla", "ready")
    expect(r.locator(".cal-result")).to_be_visible(timeout=6000)
    r.locator(".cal-pick").select_option("210")
    r.locator('[data-act="cal-save"]').click()
    wait_until(lambda: get_json(app_url + "/api/profiles")["preheat"]["pla"]["nozzle"] == 210, "calibration saved", 10)

    pg.click("#open-spools")
    pg.fill("#spool-form input[name=name]", "Nearly empty")
    pg.fill("#spool-form input[name=remaining_g]", "1")
    pg.click("#spool-form button[type=submit]")
    pg.click("#spools-close")
    messages = []
    pg.dialog_handler = lambda d: (messages.append(d.message), d.accept() if len(messages) < 2 else d.dismiss())
    row(pg, "linked-cube", "ready").locator('[data-act="print"]').click()
    # Wait through Playwright (not time.sleep) so it can deliver the next dialog event.
    for _ in range(50):
        if len(messages) >= 2:
            break
        pg.wait_for_timeout(200)
    assert "Nearly empty" in messages[1] and "Print anyway?" in messages[1]
    assert next(j for j in jobs(app_url) if j["name"] == "linked-cube")["state"] == "ready"


def test_settings_email_backup_and_mobile(page, app_url, smtp, browser):
    pg = page
    pg.click("#open-settings")
    pg.locator("details.sub summary").click()
    pg.fill("input[name=smtp_host]", "127.0.0.1")
    pg.fill("input[name=smtp_port]", "8825")
    pg.select_option("select[name=smtp_security]", "none")
    pg.fill("input[name=email_to]", "me@example.com")
    pg.click("#notify-test")
    expect(pg.locator("#notify-result")).to_have_text("Sent ✓", timeout=15000)
    assert smtp.messages and b"PrintBridge test" in smtp.messages[-1]
    with pg.expect_download() as dl:
        pg.click("a[href='/api/backup']")
    assert "config.json" in zipfile.ZipFile(dl.value.path()).namelist()
    pg.click("#settings-cancel")

    mobile = browser.new_page(viewport={"width": 390, "height": 844})
    mobile.goto(app_url)
    mobile.wait_for_timeout(1500)
    assert mobile.evaluate("document.documentElement.scrollWidth") <= 390
    mobile.close()
