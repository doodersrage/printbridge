import io
import json
import time
import zipfile
from email import message_from_bytes, policy

import httpx
import pytest

from printbridge import backup, calibration, combine, history, importer, mqtt, notify
from printbridge.config import MqttConfig, NotifyConfig, Settings
from printbridge.jobs import JobStore
from printbridge.monitor import Monitor
from printbridge.printers import PrinterStatus, Temperature

from conftest import set_printer, wait_for


# ---- email ------------------------------------------------------------------


@pytest.fixture
def smtp_server():
    from aiosmtpd.controller import Controller

    class Handler:
        def __init__(self):
            self.messages = []

        async def handle_DATA(self, server, session, envelope):
            self.messages.append((envelope.rcpt_tos, envelope.content))
            return "250 OK"

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    handler = Handler()
    ctl = Controller(handler, hostname="127.0.0.1", port=port)
    ctl.start()
    yield port, handler
    ctl.stop()


async def test_email_notification_with_thumbnail(smtp_server):
    port, handler = smtp_server
    cfg = NotifyConfig(smtp_host="127.0.0.1", smtp_port=port, smtp_security="none",
                       email_from="printer@home.lan", email_to="me@example.com, you@example.com")
    errors = await notify.send(cfg, "finished", "cube finished in 1h 2m",
                               {"file": "cube.gcode", "filament": "12.3 g", "cost": "$0.31", "url": "http://pb.lan"},
                               image=b"\x89PNG\r\n\x1a\nfake")
    assert errors == []
    rcpts, raw = handler.messages[0]
    assert rcpts == ["me@example.com", "you@example.com"]
    msg = message_from_bytes(raw, policy=policy.default)
    assert msg["Subject"] == "✅ cube finished in 1h 2m" and msg["From"] == "printer@home.lan"
    text = next(p for p in msg.walk() if p.get_content_type() == "text/plain").get_payload(decode=True).decode()
    assert "Filament: 12.3 g" in text and "Cost: $0.31" in text and "http://pb.lan" in text
    assert any(p.get_content_type() == "image/png" for p in msg.walk())


async def test_email_failure_is_reported_not_raised():
    cfg = NotifyConfig(smtp_host="127.0.0.1", smtp_port=1, smtp_security="none", email_to="me@example.com")
    errors = await notify.send(cfg, "finished", "x")
    assert len(errors) == 1 and errors[0].startswith("email")


def test_email_requires_host_and_recipient():
    assert not notify.email_configured(NotifyConfig(smtp_host="smtp.x"))
    assert notify.email_configured(NotifyConfig(smtp_host="smtp.x", email_to="a@b.c"))


# ---- MQTT -------------------------------------------------------------------


def test_mqtt_discovery_and_state():
    cfg = MqttConfig(enabled=True, host="broker", topic_prefix="ender")
    msgs = dict(mqtt.discovery_messages(cfg, supports_light=False, url="http://pb.lan"))
    nozzle = msgs["homeassistant/sensor/ender/nozzle/config"]
    assert nozzle["state_topic"] == "ender/state" and nozzle["device_class"] == "temperature"
    assert nozzle["device"]["configuration_url"] == "http://pb.lan" and nozzle["unique_id"] == "ender_nozzle"
    assert msgs["homeassistant/button/ender/cancel/config"]["payload_press"] == "cancel"
    assert msgs["homeassistant/button/ender/light_on/config"] == {}  # removed when unsupported
    assert "light_on" in json.dumps(mqtt.discovery_messages(cfg, True))

    st = PrinterStatus(online=True, state="Printing", file="/media/x/cube.gcode", progress=42.26,
                       nozzle=Temperature(actual=209, target=210), remaining=600.4)
    payload = mqtt.state_payload(st, None)
    assert payload["printing"] and payload["progress"] == 42.3 and payload["file"] == "cube.gcode"
    assert payload["remaining"] == 600 and payload["nozzle_target"] == 210
    idle = mqtt.state_payload(PrinterStatus(online=True, state="Operational", progress=100), None)
    assert idle["progress"] == 0 and idle["file"] == ""


async def test_mqtt_bridge_publishes_changes_only_and_handles_commands():
    commands = []

    async def on_command(c):
        commands.append(c)

    bridge = mqtt.MqttBridge(lambda: MqttConfig(topic_prefix="pb"), on_command, lambda: True, lambda: "")
    published = []

    class FakeClient:
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, retain))

        @property
        def messages(self):
            async def gen():
                for p in (b"pause", b"explode", b"resume"):
                    yield type("M", (), {"payload": p})()
            return gen()

    bridge.client, bridge.connected = FakeClient(), True
    st = PrinterStatus(online=True, state="Operational")
    await bridge.publish_state(st, None)
    await bridge.publish_state(st, None)
    assert len(published) == 1 and published[0][0] == "pb/state" and published[0][2] is True
    await bridge._listen(bridge.client)
    assert commands == ["pause", "resume"]


# ---- calibration ------------------------------------------------------------


def test_calibration_spec_validation():
    assert calibration.CalibrationSpec(kind="temperature", start=220, end=190, step=5).values() == [220, 215, 210, 205, 200, 195, 190]
    assert calibration.CalibrationSpec(kind="retraction", start=0.2, end=1.0, step=0.2).values() == [0.2, 0.4, 0.6, 0.8, 1.0]
    with pytest.raises(ValueError, match="2 to 12 bands"):
        calibration.CalibrationSpec(kind="temperature", start=260, end=150, step=5)
    with pytest.raises(ValueError, match="between 150 and 260"):
        calibration.CalibrationSpec(kind="temperature", start=300, end=280, step=5)
    with pytest.raises(ValueError, match="step"):
        calibration.CalibrationSpec(kind="retraction", start=0.2, end=1, step=0)
    calibration.CalibrationSpec(kind="first_layer")


def test_calibration_generate_and_post_process(tmp_path):
    spec = calibration.generate(calibration.CalibrationSpec(kind="temperature", start=210, end=200, step=5), tmp_path / "t.stl")
    assert [(b.z_from, b.z_to, b.value) for b in spec.bands] == [(1.0, 9.0, 210), (9.0, 17.0, 205), (17.0, 25.0, 200)]
    mesh = combine.read_stl(tmp_path / "t.stl", "t")
    assert mesh.bounds()[4] == 0 and max(v[2] for v in mesh.vertices) == 25.0

    g = tmp_path / "t.gcode"
    g.write_text("".join(f";LAYER_CHANGE\n;Z:{z}\nG1 X1 Y1 E1\n" for z in (0.24, 1.2, 8.8, 9.2, 17.0, 24.8)))
    assert calibration.post_process(g, spec) == 3
    temps = [ln for ln in g.read_text().splitlines() if ln.startswith("M104")]
    assert [t.split()[1] for t in temps] == ["S210", "S205", "S200"]

    r = calibration.generate(calibration.CalibrationSpec(kind="retraction", start=0.4, end=0.8, step=0.4), tmp_path / "r.stl")
    g.write_text(";Z:0.2\nG1 E-.8 F2400\n;Z:0.8\nG1 E-.8 F2400\nG1 X5 E.2\nG1 E.8 F2400\n;Z:5.8\nG1 E-.8 F2400\n")
    assert calibration.post_process(g, r) == 3
    lines = g.read_text().splitlines()
    assert lines[1] == "G1 E-.8 F2400"  # base layers keep the profile value
    assert lines[3].startswith("G1 E-0.4 F2400") and lines[5].startswith("G1 E0.4 F2400") and lines[7].startswith("G1 E-0.8")


def test_first_layer_pads_cover_the_bed(tmp_path):
    spec = calibration.generate(calibration.CalibrationSpec(kind="first_layer", first_layer_height=0.2), tmp_path / "f.stl")
    b = combine.read_stl(tmp_path / "f.stl", "f").bounds()
    assert spec.bands == [] and b[0] == 22 and b[2] == 198 and max(v[2] for v in combine.read_stl(tmp_path / "f.stl", "f").vertices) == 0.2


# ---- combine ----------------------------------------------------------------


def test_pack_layout_and_errors():
    corners = combine.pack([(50, 40), (50, 40), (100, 20)])
    rects = [(x, y, x + w, y + d) for (x, y), (w, d) in zip(corners, [(50, 40), (50, 40), (100, 20)])]
    for i, a in enumerate(rects):
        assert a[0] >= 0 and a[1] >= 0 and a[2] <= 220 and a[3] <= 220
        for b in rects[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    with pytest.raises(combine.CombineError, match="does not fit"):
        combine.pack([(230, 10)])
    with pytest.raises(combine.CombineError, match="do not all fit"):
        combine.pack([(200, 150), (200, 150)])


def test_combine_writes_3mf(tmp_path):
    a = tmp_path / "a.stl"
    calibration.write_stl(a, calibration.box(100, 100, 5, 120, 110, 15))
    meshes = [combine.read_stl(a, "A"), combine.read_stl(a, "B & <c>")]
    out = combine.combine(meshes, tmp_path / "c.3mf")
    with zipfile.ZipFile(out) as z:
        model = z.read("3D/3dmodel.model").decode()
    assert model.count("<item ") == 2 and 'name="B &amp; &lt;c&gt;"' in model
    assert "-5.0000" in model  # lifted down to the bed
    with pytest.raises(combine.CombineError):
        combine.combine(meshes[:1], tmp_path / "x.3mf")


# ---- import -----------------------------------------------------------------


async def test_import_direct_file_and_zip(monkeypatch):
    async def no_dns(url):
        return None

    monkeypatch.setattr(importer, "check_destination", no_dns)
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("thing/files/part.stl", b"solid")
        z.writestr("thing/README.txt", b"hi")
        z.writestr("__MACOSX/._part.stl", b"junk")

    def handler(r: httpx.Request):
        if r.url.path == "/part.3mf":
            return httpx.Response(200, content=b"PK3mf", headers={"content-type": "application/octet-stream"})
        if r.url.path == "/dl":
            return httpx.Response(200, content=zbuf.getvalue(), headers={"content-disposition": 'attachment; filename="thing.zip"'})
        if r.url.path == "/page":
            return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})
        return httpx.Response(429)

    t = httpx.MockTransport(handler)
    assert await importer.download("https://example.com/part.3mf", t) == [("part.3mf", b"PK3mf")]
    assert await importer.download("https://example.com/dl", t) == [("part.stl", b"solid")]
    with pytest.raises(importer.ImportError_, match="web page"):
        await importer.download("https://example.com/page", t)
    with pytest.raises(importer.ImportError_, match="does not allow downloads"):
        await importer.download("https://www.thingiverse.com/thing:1/zip", t)
    with pytest.raises(importer.ImportError_, match="http"):
        importer.normalise("file:///etc/passwd")


# ---- backup -----------------------------------------------------------------


async def test_backup_roundtrip(client):
    from printbridge import config

    await client.put("/api/presets/Fast", json={"quality": "draft"})
    await client.put("/api/profile-files/material/asa", json={"text": "# ASA\ntemperature = 250\n"})
    s = (await client.get("/api/settings")).json()
    s["currency"] = "€"
    await client.put("/api/settings", json=s)
    blob = (await client.get("/api/backup")).content
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert {"config.json", "presets.json", "profiles/material/asa.ini"} <= set(names) and "secret_key" not in names

    await client.delete("/api/presets/Fast")
    (config.DATA_DIR / "profiles/material/asa.ini").unlink()
    r = await client.post("/api/restore", files={"file": ("b.zip", blob)})
    assert r.status_code == 200
    assert "Fast" in (await client.get("/api/presets")).json()
    assert (await client.get("/api/settings")).json()["currency"] == "€"
    assert "asa" in (await client.get("/api/profiles")).json()["materials"]


def test_restore_rejects_bad_archives(tmp_path):
    with pytest.raises(backup.BackupError, match="not a zip"):
        backup.restore(b"nope")
    for name, data in [("../../etc/x", b"x"), ("profiles/../config.json", b"{}"), ("config.json", b'{"printer": 5}')]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("printbridge-backup.json", "{}")
            z.writestr(name, data)
        with pytest.raises((backup.BackupError, ValueError)):
            backup.restore(buf.getvalue())


# ---- history ----------------------------------------------------------------


async def test_monitor_records_history(tmp_path, monkeypatch):
    from printbridge import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    store = JobStore(tmp_path / "jobs")
    job = store.create("cube.3mf", None)
    from printbridge.gcode import GcodeStats

    store.update(job, state="sent", sent_at=time.time(), stats=GcodeStats(filament_g=20.0, cost=0.5))
    sent = []

    async def fake_send(cfg, event, message, extra=None, transport=None, image=None):
        sent.append((event, extra))
        return []

    monkeypatch.setattr(notify, "send", fake_send)
    m = Monitor(lambda: Settings(currency="€"), store)
    for st in (PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=10, elapsed=60),
               PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=50, elapsed=1800),
               PrinterStatus(online=True, state="Operational", progress=50)):
        m.status = st
        await m._track(st)
    rec = history.load()[-1]
    assert rec.result == "stopped" and rec.progress == 50 and rec.filament_g == 10.0 and rec.cost == 0.25
    assert rec.duration == 1800 and rec.job_id == job.id
    assert sent[-1][1]["cost"] == "€0.25"
    summ = history.summary()
    assert summ["all"]["prints"] == 1 and summ["all"]["success_rate"] == 0.0 and summ["month"]["hours"] == 0.5


# ---- API: calibration, thumbnails, costs, babystep, zip upload --------------


async def test_calibration_api(client, tmp_path):
    d = (await client.get("/api/calibration/defaults", params={"kind": "temperature", "material": "petg"})).json()
    assert d == {"start": 250, "end": 220, "step": 5}
    r = await client.post("/api/calibration", json={"kind": "temperature", "material": "pla", "start": 215, "end": 205, "step": 5})
    assert r.status_code == 201
    job = await wait_for(client, r.json()["id"], "ready", "failed")
    assert job["calibration"]["kind"] == "temperature" and len(job["calibration"]["bands"]) == 3
    assert job["options"]["nozzle_temp"] == 215
    assert (await client.post(f"/api/jobs/{job['id']}/calibration/apply", json={"value": 999})).status_code == 422
    r = await client.post(f"/api/jobs/{job['id']}/calibration/apply", json={"value": 210})
    assert r.json()["saved"] == {"temperature": "210", "first_layer_temperature": "210"}
    assert (await client.get("/api/profiles")).json()["preheat"]["pla"]["nozzle"] == 210
    bad = await client.post("/api/calibration", json={"kind": "retraction", "start": 0.2, "end": 9, "step": 0.1})
    assert bad.status_code == 422 and "bands" in bad.json()["detail"]


async def test_thumbnail_api(client, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    assert (await client.get(f"/api/jobs/{job['id']}/thumbnail.png")).status_code == 404
    assert (await client.put(f"/api/jobs/{job['id']}/thumbnail", content=b"GIF89a")).status_code == 422
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    assert (await client.put(f"/api/jobs/{job['id']}/thumbnail", content=png)).status_code == 204
    assert (await client.get(f"/api/jobs/{job['id']}")).json()["thumbnail"] is True
    assert (await client.get(f"/api/jobs/{job['id']}/thumbnail.png")).content == png


async def test_costs_api(client):
    r = await client.put("/api/costs", json={"currency": "£", "prices": {"pla": 22.5, "petg": 0}})
    assert r.json()["currency"] == "£" and r.json()["prices"]["pla"] == 22.5
    files = (await client.get("/api/profile-files")).json()
    assert next(f for f in files if f["name"] == "pla")["overridden"] is True
    assert next(f for f in files if f["name"] == "petg")["overridden"] is False  # unchanged zero isn't written
    assert (await client.put("/api/costs", json={"prices": {"pla": -1}})).status_code == 422


async def test_babystep_allowed_while_printing(client, monkeypatch, fake_host):
    set_printer(client, monkeypatch, "Printing", file="x.gcode")
    r = await client.post("/api/printer/command", json={"command": "babystep", "z": -0.05})
    assert r.json()["babystep_total"] == -0.05 and fake_host.gcode == ["M290 Z-0.050"]
    r = await client.post("/api/printer/command", json={"command": "babystep", "z": 0.02})
    assert r.json()["babystep_total"] == -0.03
    assert (await client.post("/api/printer/command", json={"command": "babystep", "z": 2})).status_code == 422


async def test_zip_upload_creates_a_job_per_model(client, sample_gcode):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.3mf", b"PK a")
        z.writestr("dir/b.stl", b"solid b")
        z.writestr("c.gcode", sample_gcode.read_bytes())
        z.writestr("notes.pdf", b"%PDF")
    r = await client.post("/api/jobs", files={"file": ("bundle.zip", buf.getvalue())})
    assert r.status_code == 201
    names = sorted(j["name"] for j in (await client.get("/api/jobs")).json())
    assert names == ["a", "b", "c"]


async def test_combine_api_rejects_gcode_jobs(client, sample_gcode):
    g = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    m = (await client.post("/api/jobs", files={"file": ("m.3mf", b"PK")})).json()
    r = await client.post("/api/jobs/combine", json={"job_ids": [g["id"], m["id"]]})
    assert r.status_code == 400


def test_dimension_cube(tmp_path):
    spec = calibration.generate(calibration.CalibrationSpec(kind="dimension"), tmp_path / "c.stl")
    b = combine.read_stl(tmp_path / "c.stl", "c").bounds()
    assert spec.bands == [] and (b[0], b[1], b[2], b[3]) == (0, 0, 22, 20)
    with pytest.raises(ValueError):
        calibration.result_values("dimension", 20)


# ---- wrong-printer / wrong-size 3MF detection --------------------------------


def make_project_3mf(path, transform="1 0 0 0 1 0 0 0 1 50 50 0", extra=None, unit="millimeter"):
    model = (f'<?xml version="1.0"?><model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             '<metadata name="Application">BambuStudio-02.01.00.59</metadata>'
             '<resources><object id="1" type="model"><mesh><vertices/><triangles/></mesh></object></resources>'
             f'<build><item objectid="1" transform="{transform}"/></build></model>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return path


def test_inspect_detects_other_printer_and_scale(tmp_path):
    from printbridge.threemf import inspect

    bambu = make_project_3mf(tmp_path / "a.3mf", "0.8 0 0 0 0.8 0 0 0 0.8 90 90 0", {
        "Metadata/project_settings.config": json.dumps(
            {"printer_model": "Bambu Lab A1 mini", "printable_area": ["0x0", "180x0", "180x180", "0x180"]}),
    })
    info = inspect(bambu)
    assert info.printer == "Bambu Lab A1 mini" and info.bed == (180, 180) and info.scale == 0.8
    assert info.application.startswith("BambuStudio")
    assert any("A1 mini (180 × 180 mm bed)" in w for w in info.warnings)
    assert any("80%" in w for w in info.warnings)

    prusa = make_project_3mf(tmp_path / "p.3mf", extra={
        "Metadata/Slic3r_PE.config": "; printer_model = MK4S\n; bed_shape = 0x0,250x0,250x210,0x210\n"})
    info = inspect(prusa)
    assert info.printer == "MK4S" and info.bed == (250, 210) and info.scale is None

    ours = make_project_3mf(tmp_path / "o.3mf", extra={
        "Metadata/Slic3r_PE.config": "; printer_model = Creality Ender-3 V3 SE\n; bed_shape = 0x0,220x0,220x220,0x220\n"})
    assert inspect(ours).warnings == []

    inch = make_project_3mf(tmp_path / "i.3mf", unit="inch")
    assert any("inch units" in w for w in inspect(inch).warnings)


def test_remove_stored_scale_keeps_rotation(tmp_path):
    from printbridge.threemf import inspect, remove_stored_scale

    src = make_project_3mf(tmp_path / "r.3mf", "0 0.5 0 -0.5 0 0 0 0 0.5 40 20 0")
    out = remove_stored_scale(src, tmp_path / "fixed.3mf")
    with zipfile.ZipFile(out) as z:
        model = z.read("3D/3dmodel.model").decode()
    assert 'transform="0 1 0 -1 0 0 0 0 1 80 40 0"' in model
    assert inspect(out).scale is None


async def test_original_size_option_flow(client, tmp_path):
    src = make_project_3mf(tmp_path / "mini.3mf", "0.8 0 0 0 0.8 0 0 0 0.8 90 90 0", {
        "Metadata/project_settings.config": json.dumps({"printer_model": "Bambu Lab A1 mini"})})
    job = (await client.post("/api/jobs", files={"file": ("mini.3mf", src.read_bytes())})).json()
    job = await wait_for(client, job["id"], "ready")
    assert job["source_info"]["scale"] == 0.8 and job["options"]["original_size"] is False
    r = await client.post(f"/api/jobs/{job['id']}/reslice", json={**job["options"], "original_size": True})
    assert r.json()["options"]["original_size"] is True
    await wait_for(client, job["id"], "ready")
    jobs = client.main.state.jobs
    unscaled = jobs.dir(jobs.get(job["id"])) / "unscaled-0.3mf"
    with zipfile.ZipFile(unscaled) as z:
        assert 'transform="1 0 0 0 1 0 0 0 1 112.5 112.5 0"' in z.read("3D/3dmodel.model").decode()


def test_stl_size(tmp_path):
    p = calibration.write_stl(tmp_path / "s.stl", calibration.box(5, 5, 0, 25, 15, 7.5))
    assert combine.stl_size(p) == [20.0, 10.0, 7.5]


# ---- spools -----------------------------------------------------------------


async def test_spool_api_and_print_check(client, monkeypatch, fake_host, sample_gcode):
    r = await client.post("/api/spools", json={"name": "Black PLA", "material": "pla", "total_g": 1000, "remaining_g": 3, "loaded": True})
    assert r.status_code == 201 and r.json()[0]["loaded"] and r.json()[0]["remaining_g"] == 3
    r = await client.post("/api/spools", json={"name": "Grey PETG", "material": "petg", "total_g": 1000})
    petg = next(s for s in r.json() if s["name"] == "Grey PETG")
    assert petg["remaining_g"] == 1000 and not petg["loaded"]

    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()  # 5.23 g
    set_printer(client, monkeypatch, "Operational")
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "spool" and "3 g left" in r.json()["detail"]["message"]
    assert (await client.post(f"/api/jobs/{job['id']}/send", json={"start": False})).status_code == 200  # upload only is fine
    await wait_for(client, job["id"], "sent")
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True, "ignore_spool": True})
    assert r.status_code == 200

    # Loading another spool unloads the first; the printer card shows it.
    r = await client.put(f"/api/spools/{petg['id']}", json={**petg, "loaded": True})
    assert [s["name"] for s in r.json() if s["loaded"]] == ["Grey PETG"]
    assert (await client.get("/api/printer")).json()["spool"]["name"] == "Grey PETG"
    assert (await client.delete(f"/api/spools/{petg['id']}")).json()[0]["name"] == "Black PLA"


def test_spool_check_material_mismatch(tmp_path, monkeypatch):
    from printbridge import config, spools

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    assert spools.check(500, "pla") == []  # nothing tracked
    spools.upsert(spools.Spool(name="PETG", material="petg", remaining_g=800), make_loaded=True)
    assert spools.check(100, "petg") == []
    assert "sliced for PLA" in spools.check(100, "pla")[0]
    assert spools.consume(30).remaining_g == 770
    assert spools.consume(5000).remaining_g == 0


async def test_finished_print_consumes_spool(tmp_path, monkeypatch):
    from printbridge import config, spools
    from printbridge.gcode import GcodeStats

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    spools.upsert(spools.Spool(name="Black", material="pla", remaining_g=60), make_loaded=True)
    store = JobStore(tmp_path / "jobs")
    job = store.create("cube.3mf", None)
    store.update(job, state="sent", sent_at=time.time(), stats=GcodeStats(filament_g=20.0))
    sent = []

    async def fake_send(cfg, event, message, extra=None, transport=None, image=None):
        sent.append(extra)
        return []

    monkeypatch.setattr(notify, "send", fake_send)
    m = Monitor(lambda: Settings(), store)
    for st in (PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=5),
               PrinterStatus(online=True, state="Complete", progress=100)):
        m.status = st
        await m._track(st)
    assert spools.loaded().remaining_g == 40
    assert sent[-1]["spool"] == "40 g left on Black — running low"
    assert history.load()[-1].spool == "Black"


# ---- security hardening -------------------------------------------------------


async def test_cross_site_writes_are_blocked(client):
    body = {"quality": "draft"}
    r = await client.put("/api/presets/x", json=body, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = await client.post("/api/restore", files={"file": ("b.zip", b"x")}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert (await client.put("/api/presets/x", json=body, headers={"origin": "http://test"})).status_code == 200
    assert (await client.put("/api/presets/y", json=body)).status_code == 200  # scripts send no Origin
    assert (await client.get("/api/presets", headers={"origin": "http://evil.example"})).status_code == 200


async def test_security_headers(client):
    r = await client.get("/")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


async def test_settings_never_return_secrets(client):
    s = (await client.get("/api/settings")).json()
    s["printer"]["api_key"] = "octo-key"
    s["notify"]["smtp_password"] = "mail-pass"
    s["mqtt"]["password"] = "mqtt-pass"
    s["slicer_path"] = "/bin/sh"
    r = (await client.put("/api/settings", json=s)).json()
    assert r["printer"]["api_key"] == r["notify"]["smtp_password"] == r["mqtt"]["password"] == "••••••••"
    assert r["notify"]["ntfy_token"] == ""
    stored = client.main.state.settings
    assert stored.printer.api_key == "octo-key" and stored.slicer_path != "/bin/sh"
    # Sending the placeholder back keeps the secret; clearing the field removes it.
    r["notify"]["smtp_password"] = ""
    await client.put("/api/settings", json=r)
    assert client.main.state.settings.printer.api_key == "octo-key"
    assert client.main.state.settings.notify.smtp_password == ""


async def test_import_refuses_internal_addresses():
    for url in ("http://127.0.0.1:8000/x.stl", "http://localhost/x.stl", "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(importer.ImportError_, match="not allowed"):
            await importer.download(url)


async def test_import_checks_every_redirect(monkeypatch):
    checked = []

    async def check(url):
        checked.append(url)
        if "127.0.0.1" in url:
            raise importer.ImportError_("Links to 127.0.0.1 are not allowed")

    monkeypatch.setattr(importer, "check_destination", check)
    t = httpx.MockTransport(lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/secret.stl"}))
    with pytest.raises(importer.ImportError_, match="not allowed"):
        await importer.download("https://example.com/x.stl", t)
    assert checked == ["https://example.com/x.stl", "http://127.0.0.1/secret.stl"]


def test_zip_total_size_limit(monkeypatch):
    monkeypatch.setattr(importer, "MAX_UNZIPPED", 10)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.stl", b"x" * 8)
        z.writestr("b.stl", b"x" * 8)
    with pytest.raises(importer.ImportError_, match="512 MB total"):
        importer.from_zip(buf.getvalue())


async def test_remote_file_names_are_escaped():
    from printbridge.config import PrinterConfig
    from printbridge.printers import PrinterError, make_host

    seen = []
    t = httpx.MockTransport(lambda r: (seen.append(r.url.raw_path), httpx.Response(204))[1])
    octo = make_host(PrinterConfig(kind="octoprint", url="http://octopi"), t)
    await octo.delete_file("a b/c.gcode")
    assert seen[-1] == b"/api/files/local/a%20b%2Fc.gcode"
    for bad in ("../settings", "/etc/x", "a//b", ""):
        with pytest.raises(PrinterError):
            await octo.delete_file(bad)
    moon = make_host(PrinterConfig(kind="moonraker", url="http://klipper"), t)
    await moon.delete_file("sub/part.gcode")
    assert seen[-1] == b"/server/files/gcodes/sub/part.gcode"
