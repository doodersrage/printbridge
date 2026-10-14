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


async def wait_for(client, job_id, *states):
    for _ in range(100):
        job = (await client.get(f"/api/jobs/{job_id}")).json()
        if job["state"] in states:
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job stuck in {job['state']}")


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

    sent = []

    class FakeHost:
        async def upload(self, path, name, start):
            sent.append((path.name, name, start))

    monkeypatch.setattr(client.main, "make_host", lambda cfg: FakeHost())
    r = await client.post(f"/api/jobs/{job['id']}/send", json={"start": True})
    assert r.status_code == 200
    job = await wait_for(client, job["id"], "sent", "ready")
    assert job["state"] == "sent" and job["started_print"] is True
    assert sent == [("My_Cube.gcode", "My_Cube.gcode", True)]


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
