import asyncio
import importlib

import httpx
import pytest


@pytest.fixture
async def client(tmp_path, monkeypatch, fake_slicer):
    monkeypatch.setenv("PRINTBRIDGE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PRINTBRIDGE_SLICER", str(fake_slicer))
    from printbridge import config, main

    importlib.reload(config)
    main = importlib.reload(main)
    async with main.app.router.lifespan_context(main.app):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.main = main
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
    monkeypatch.setattr(client.main, "make_host", lambda cfg: h)
    client.main.state.settings.printer.url = "http://box"
    return h


async def wait_for(client, job_id, *states):
    for _ in range(100):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["state"] in states:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job stuck in {job['state']}")


async def test_healthz_reports_version(client):
    from printbridge import __version__

    r = (await client.get("/healthz")).json()
    assert r == {"ok": True, "version": __version__} and __version__ == "0.1.0"


async def test_index_served(client):
    r = await client.get("/")
    assert r.status_code == 200 and "PrintBridge" in r.text


async def test_upload_slice_send(client, monkeypatch):
    r = await client.post(
        "/api/jobs",
        files={"file": ("My Cube!.3mf", b"PK\x03\x04fake", "model/3mf")},
        data={"options": '{"quality": "draft", "infill": 30}'},
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["name"] == "My_Cube"
    job = await wait_for(client, job["id"], "ready", "failed")
    assert job["state"] == "ready", job["error"]
    assert job["stats"]["filament_g"] == 5.23

    g = await client.get(f"/api/jobs/{job['id']}/gcode")
    assert g.status_code == 200 and ";LAYER_CHANGE" in g.text

    h = FakeHost()
    monkeypatch.setattr(client.main, "make_host", lambda cfg: h)
    set_printer(client, monkeypatch, "Operational")
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True})
    assert r.status_code == 200
    job = await wait_for(client, job["id"], "sent", "ready")
    assert job["state"] == "sent" and job["started_print"] is True
    assert h.sent == [("My_Cube.gcode", "My_Cube.gcode", True)]


async def test_slice_failure_is_reported(client):
    r = await client.post("/api/jobs", files={"file": ("fail.3mf", b"FAIL")})
    job = await wait_for(client, r.json()["id"], "ready", "failed")
    assert job["state"] == "failed" and "outside of the print volume" in job["error"]


async def test_gcode_upload_skips_slicing(client, sample_gcode):
    r = await client.post("/api/jobs", files={"file": ("part.gcode", sample_gcode.read_bytes())})
    job = r.json()
    assert job["state"] == "ready" and job["options"] is None and job["stats"]["layers"] == 2


async def test_rejects_unknown_type_and_bad_options(client):
    r = await client.post("/api/jobs", files={"file": ("x.step", b"x")})
    assert r.status_code == 400
    r = await client.post("/api/jobs", files={"file": ("x.3mf", b"x")}, data={"options": '{"infill": 500}'})
    assert r.status_code == 422


async def test_send_requires_printer_config(client, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": False})
    assert r.status_code == 400 and "No printer URL" in r.json()["detail"]


async def test_settings_roundtrip_and_status(client):
    s = (await client.get("/api/settings")).json()
    s["printer"].update(kind="moonraker", url="http://127.0.0.1:9")
    r = await client.put("/api/settings", json=s)
    assert r.status_code == 200
    st = (await client.get("/api/printer")).json()
    assert st["online"] is False and st["state"] == "Offline"


async def test_delete_job(client, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    assert (await client.delete(f"/api/jobs/{job['id']}")).status_code == 204
    assert (await client.get(f"/api/jobs/{job['id']}")).status_code == 404


async def test_reslice_failed_job(client):
    job = (await client.post("/api/jobs", files={"file": ("cube.3mf", b"FAIL")})).json()
    await wait_for(client, job["id"], "failed")
    client.main.state.jobs.source_path(client.main.state.jobs.get(job["id"])).write_bytes(b"PK ok now")
    r = await client.post(f"/api/jobs/{job['id']}/reslice", json={"quality": "fine", "infill": 40})
    assert r.status_code == 200
    job = await wait_for(client, job["id"], "ready", "failed")
    assert job["state"] == "ready" and job["options"]["quality"] == "fine" and job["error"] is None


async def test_reslice_rejects_gcode_jobs(client, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    r = await client.post(f"/api/jobs/{job['id']}/reslice", json={})
    assert r.status_code == 400


async def test_model_preview(client):
    job = (await client.post("/api/jobs", files={"file": ("cube.3mf", b"PK model")})).json()
    await wait_for(client, job["id"], "ready")
    r = await client.get(f"/api/jobs/{job['id']}/preview.stl")
    assert r.status_code == 200 and r.headers["content-type"] == "model/stl"
    # Cached after the first request, and cleared when the job is re-sliced.
    path = client.main.state.jobs.preview_path(client.main.state.jobs.get(job["id"]))
    assert path.exists()
    await client.post(f"/api/jobs/{job['id']}/reslice", json={"arrange": False})
    await wait_for(client, job["id"], "ready")
    assert not path.exists()


async def test_model_preview_errors(client, sample_gcode):
    g = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    assert (await client.get(f"/api/jobs/{g['id']}/preview.stl")).status_code == 404
    bad = (await client.post("/api/jobs", files={"file": ("bad.3mf", b"FAIL")})).json()
    await wait_for(client, bad["id"], "failed")
    r = await client.get(f"/api/jobs/{bad['id']}/preview.stl")
    assert r.status_code == 422 and "outside of the print volume" in r.json()["detail"]


async def test_print_blocked_while_printer_busy(client, monkeypatch, fake_host, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    set_printer(client, monkeypatch, "Printing", file="other.gcode")
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True})
    assert r.status_code == 409 and "busy" in r.json()["detail"]
    # Upload-only is still allowed, and force overrides the guard.
    assert (await client.post(f"/api/jobs/{job['id']}/send", json={"start": False})).status_code == 200
    await wait_for(client, job["id"], "sent")
    assert (await client.post(f"/api/jobs/{job['id']}/send", json={"start": True, "force": True})).status_code == 200


async def test_print_blocked_when_printer_offline(client, monkeypatch, fake_host, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    set_printer(client, monkeypatch, "Offline", online=False)
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True})
    assert r.status_code == 409 and "offline" in r.json()["detail"]


async def test_printer_commands(client, monkeypatch, fake_host):
    set_printer(client, monkeypatch, "Operational")
    assert (await client.post("/api/printer/command", json={"command": "preheat", "nozzle": 210, "bed": 60})).status_code == 200
    assert (await client.post("/api/printer/command", json={"command": "gcode", "gcode": "G28; M114"})).status_code == 200
    assert fake_host.commands == [("preheat", 210, 60)] and fake_host.gcode == ["G28", "M114"]
    set_printer(client, monkeypatch, "Printing", file="x.gcode")
    assert (await client.post("/api/printer/command", json={"command": "home"})).status_code == 409
    assert (await client.post("/api/printer/command", json={"command": "light_on"})).status_code == 200


async def test_printer_files_and_cleanup(client, monkeypatch, fake_host, sample_gcode):
    from printbridge.printers import RemoteFile

    job = (await client.post("/api/jobs", files={"file": ("mine.gcode", sample_gcode.read_bytes())})).json()
    set_printer(client, monkeypatch, "Operational")
    await client.post(f"/api/jobs/{job['id']}/send", json={"start": False})
    await wait_for(client, job["id"], "sent")
    fake_host.files = [RemoteFile(name="cloud.gcode", size=5), RemoteFile(name="mine.gcode", size=9), RemoteFile(name="busy.gcode")]
    files = (await client.get("/api/printer/files")).json()
    assert files[0]["name"] == "mine.gcode" and files[0]["job_id"] == job["id"]
    set_printer(client, monkeypatch, "Printing", file="/media/busy.gcode")
    r = (await client.post("/api/printer/files/delete", json={"names": ["mine.gcode", "busy.gcode"]})).json()
    assert r["deleted"] == ["mine.gcode"] and "currently printing" in r["errors"][0]


async def test_toolpaths_endpoint(client, sample_gcode):
    job = (await client.post("/api/jobs", files={"file": ("p.gcode", sample_gcode.read_bytes())})).json()
    r = await client.get(f"/api/jobs/{job['id']}/toolpaths")
    assert r.status_code == 200 and r.content[:4] == b"PBTP"


async def test_presets_roundtrip(client):
    r = await client.put("/api/presets/PETG parts", json={"material": "petg", "infill": 40, "plate": 3})
    assert r.status_code == 200 and r.json()["PETG parts"]["infill"] == 40 and r.json()["PETG parts"]["plate"] is None
    assert "PETG parts" in (await client.get("/api/presets")).json()
    assert (await client.put("/api/presets/bad", json={"material": "unobtainium"})).status_code == 422
    assert (await client.delete("/api/presets/PETG parts")).json() == {}


async def test_profile_override_and_custom_profile(client):
    files = (await client.get("/api/profile-files")).json()
    assert {"kind": "quality", "name": "draft", "label": "Draft — 0.28 mm, fast", "builtin": True, "overridden": False} in files
    text = "# Draft but hotter\nlayer_height = 0.3\n"
    r = await client.put("/api/profile-files/quality/draft", json={"text": text})
    assert r.status_code == 200 and r.json()["overridden"] and r.json()["label"] == "Draft but hotter"
    assert (await client.get("/api/profiles")).json()["qualities"]["draft"] == "Draft but hotter"
    assert (await client.delete("/api/profile-files/quality/draft")).json()["overridden"] is False

    r = await client.put("/api/profile-files/material/asa", json={"text": "# ASA\ntemperature = 250\nfirst_layer_bed_temperature = 100\n"})
    assert r.status_code == 200
    p = (await client.get("/api/profiles")).json()
    assert p["materials"]["asa"] == "ASA" and p["preheat"]["asa"] == {"nozzle": 250, "bed": 100}
    assert (await client.delete("/api/profile-files/material/asa")).json() == {"deleted": True}

    assert (await client.put("/api/profile-files/quality/x", json={"text": "<script>"})).status_code == 422
    from printbridge import profiles
    with pytest.raises(profiles.ProfileError):
        profiles.override_path("quality", "../evil")
    assert (await client.post("/api/jobs", files={"file": ("m.3mf", b"PK")}, data={"options": '{"quality": "nope"}'})).status_code == 422


async def test_multi_plate_3mf(client, tmp_path):
    import zipfile

    src = tmp_path / "plates.3mf"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("3D/3dmodel.model", '<model><resources><object id="1"/><object id="2"/></resources>'
                   '<build><item objectid="1"/><item objectid="2"/></build></model>')
        z.writestr("Metadata/model_settings.config",
                   '<config><object id="1"><metadata key="name" value="A"/></object>'
                   '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="1"/></model_instance></plate>'
                   '<plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value="Second"/>'
                   '<model_instance><metadata key="object_id" value="2"/></model_instance></plate></config>')
    job = (await client.post("/api/jobs", files={"file": ("plates.3mf", src.read_bytes())})).json()
    job = await wait_for(client, job["id"], "ready", "failed")
    assert [p["id"] for p in job["plates"]] == [1, 2] and job["options"]["plate"] == 1
    assert job["plates"][0]["objects"] == ["A"] and job["plates"][1]["name"] == "Second"
    r = await client.post(f"/api/jobs/{job['id']}/reslice", json={"plate": 2})
    assert r.json()["options"]["plate"] == 2
    job = await wait_for(client, job["id"], "ready", "failed")
    jobs = client.main.state.jobs
    plate_file = jobs.plate_path(jobs.get(job["id"]), 2)
    assert plate_file.exists()
    with zipfile.ZipFile(plate_file) as z:
        model = z.read("3D/3dmodel.model").decode()
    assert 'objectid="2"' in model and 'objectid="1"' not in model
    # A re-slice from the form (no plate) keeps the chosen plate.
    r = await client.post(f"/api/jobs/{job['id']}/reslice", json={"infill": 30})
    assert r.json()["options"]["plate"] == 2


async def test_notify_test_requires_target(client):
    assert (await client.post("/api/notify/test")).status_code == 400
