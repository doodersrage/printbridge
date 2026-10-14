import json
import struct
import time
import zipfile

import httpx
import pytest

from printbridge import notify, toolpaths
from printbridge.config import NotifyConfig, Settings
from printbridge.jobs import JobStore
from printbridge.monitor import Monitor
from printbridge.printers import PrinterStatus
from printbridge.threemf import extract_plate, filter_build, read_plates


# ---- monitor --------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path / "jobs")


def sent_job(store, name="cube"):
    job = store.create(f"{name}.3mf", None)
    return store.update(job, state="sent", sent_at=time.time(), started_print=True)


async def drive(monitor, monkeypatch, statuses):
    events = []

    async def fake_send(cfg, event, message, extra=None, transport=None):
        events.append((event, message))
        return []

    monkeypatch.setattr(notify, "send", fake_send)
    for st in statuses:
        monitor.status = st
        await monitor._track(st)
    return events


async def test_monitor_tracks_print_to_completion(store, monkeypatch):
    job = sent_job(store)
    m = Monitor(lambda: Settings(), store)
    events = await drive(m, monkeypatch, [
        PrinterStatus(online=True, state="Operational"),
        PrinterStatus(online=True, state="Printing", file="/media/x/cube.gcode", progress=1, elapsed=10),
        PrinterStatus(online=True, state="Printing", file="/media/x/cube.gcode", progress=99.5, elapsed=3600),
        PrinterStatus(online=True, state="Complete"),
    ])
    assert [e for e, _ in events] == ["started", "finished"]
    assert "cube finished in 1h 0m" in events[1][1]
    assert store.get(job.id).state == "printed" and store.get(job.id).finished_at


async def test_monitor_finish_reported_in_same_poll_as_idle(store, monkeypatch):
    job = sent_job(store)
    m = Monitor(lambda: Settings(), store)
    events = await drive(m, monkeypatch, [
        PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=97),
        PrinterStatus(online=True, state="Operational", progress=100),
    ])
    assert events[-1][0] == "finished" and store.get(job.id).state == "printed"


async def test_monitor_detects_cancelled_print(store, monkeypatch):
    job = sent_job(store)
    m = Monitor(lambda: Settings(), store)
    events = await drive(m, monkeypatch, [
        PrinterStatus(online=True, state="Operational"),
        PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=42),
        PrinterStatus(online=True, state="Operational"),
    ])
    assert events[-1] == ("stopped", "cube stopped at 42%")
    assert store.get(job.id).state == "stopped"


async def test_monitor_ignores_offline_blips_and_reports_errors(store, monkeypatch):
    sent_job(store)
    m = Monitor(lambda: Settings(), store)
    events = await drive(m, monkeypatch, [
        PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=10),
        PrinterStatus(online=False, state="Offline"),
        PrinterStatus(online=True, state="Printing", file="cube.gcode", progress=12),
        PrinterStatus(online=True, state="Printing", file="cube.gcode", message="Printer reported error 3"),
        PrinterStatus(online=True, state="Printing", file="cube.gcode", message="Printer reported error 3"),
    ])
    assert [e for e, _ in events] == ["error"]


# ---- notifications --------------------------------------------------------


async def test_notify_ntfy_and_webhook():
    seen = []
    t = httpx.MockTransport(lambda r: (seen.append(r), httpx.Response(200))[1])
    cfg = NotifyConfig(ntfy_url="https://ntfy.example/printer", ntfy_token="tok", webhook_url="http://ha/api/webhook/x")
    assert await notify.send(cfg, "finished", "cube finished", {"file": "cube.gcode"}, transport=t) == []
    ntfy_req, hook_req = seen
    assert ntfy_req.headers["Title"] == "Print finished" and ntfy_req.headers["Authorization"] == "Bearer tok"
    assert ntfy_req.content == b"cube finished"
    body = json.loads(hook_req.content)
    assert body["event"] == "finished" and body["file"] == "cube.gcode"


async def test_notify_respects_event_toggles_and_reports_errors():
    t = httpx.MockTransport(lambda r: httpx.Response(500))
    cfg = NotifyConfig(ntfy_url="https://ntfy.example/p", on_start=False)
    assert await notify.send(cfg, "started", "x", transport=t) == []
    errors = await notify.send(cfg, "finished", "x", transport=t)
    assert len(errors) == 1 and errors[0].startswith("ntfy")


# ---- toolpaths ------------------------------------------------------------


def read_bin(path):
    d = path.read_bytes()
    n = struct.unpack("<I", d[4:8])[0]
    header = json.loads(d[8:8 + n])
    seg = header["segments"]
    coords = struct.unpack(f"<{seg * 4}f", d[8 + n: 8 + n + seg * 16])
    types = d[8 + n + seg * 16:]
    return header, coords, types


def test_toolpaths_prusa_style(tmp_path):
    g = tmp_path / "a.gcode"
    g.write_text("\n".join([
        "M83", "G92 E0", ";TYPE:Custom", "G1 Z0.28", "G1 X1 Y20 E1", "G1 X1 Y145 E15",
        ";LAYER_CHANGE", ";Z:0.2", "G1 Z0.2", ";TYPE:External perimeter", "G1 X10 Y10", "G1 X20 Y10 E0.5",
        "G1 E-0.8", "G1 X30 Y30", "G1 E0.8", ";TYPE:Internal infill", "G1 X40 Y30 E0.3",
        "G1 Z0.4", ";TYPE:External perimeter", "G1 X20 Y10 E0.4 ; comment", "G1 Z0.6 F3000",
    ]))
    out = toolpaths.build(g, tmp_path / "a.bin")
    header, coords, types = read_bin(out)
    assert header["segments"] == 5
    assert [l["z"] for l in header["layers"]] == [0.28, 0.2, 0.4]
    assert [l["end"] for l in header["layers"]] == [2, 4, 5]
    names = header["types"]
    assert [names[t] for t in types] == ["Custom", "Custom", "External perimeter", "Internal infill", "External perimeter"]
    assert coords[8:12] == (10.0, 10.0, 20.0, 10.0)


def test_toolpaths_absolute_extrusion_and_relative_moves(tmp_path):
    g = tmp_path / "b.gcode"
    g.write_text("\n".join([
        "M82", "G90", "G1 Z0.2", "G1 X0 Y0", "G1 X10 Y0 E1", "G1 X20 Y0 E1",  # no extrusion (E unchanged)
        "G1 X20 Y10 E2", "G92 E0", "G91", "G1 X5 E1", "G90", "G1 X0 Y0 E0.5",  # E went backwards: retraction
    ]))
    header, coords, _ = read_bin(toolpaths.build(g, tmp_path / "b.bin"))
    assert header["segments"] == 3
    assert coords[-4:] == (20.0, 10.0, 25.0, 10.0)
    assert header["bounds"] == [0.0, 0.0, 25.0, 10.0]


# ---- 3MF plates -----------------------------------------------------------


def make_bambu(path):
    main = (
        '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"><resources>'
        '<object id="2"><components><component p:path="/3D/Objects/object_1.model" objectid="1"/></components></object>'
        '<object id="4"><components><component p:path="/3D/Objects/object_3.model" objectid="3"/></components></object>'
        '</resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 10 10 0"/>'
        '<item objectid="4" printable="1"><metadata name="x">y</metadata></item>'
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 300 10 0"/></build></model>'
    )
    cfg = (
        '<config><object id="2"><metadata key="name" value="small"/></object>'
        '<object id="4"><metadata key="name" value="big"/></object>'
        '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="2"/>'
        '<metadata key="instance_id" value="0"/></model_instance></plate>'
        '<plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value="Two"/>'
        '<model_instance><metadata key="object_id" value="4"/><metadata key="instance_id" value="0"/></model_instance>'
        '<model_instance><metadata key="object_id" value="2"/><metadata key="instance_id" value="1"/></model_instance></plate></config>'
    )
    rels = ('<Relationships><Relationship Target="/3D/Objects/object_1.model" Id="a"/>'
            '<Relationship Target="/3D/Objects/object_3.model" Id="b"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", main)
        z.writestr("3D/_rels/3dmodel.model.rels", rels)
        z.writestr("3D/Objects/object_1.model", "<model/>")
        z.writestr("3D/Objects/object_3.model", "<model/>")
        z.writestr("Metadata/model_settings.config", cfg)
    return path


def test_read_plates(tmp_path):
    plates = read_plates(make_bambu(tmp_path / "b.3mf"))
    assert [(p.id, p.name, p.objects) for p in plates] == [(1, "", ["small"]), (2, "Two", ["big", "small"])]


def test_read_plates_ignores_plain_3mf(tmp_path):
    p = tmp_path / "plain.3mf"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    assert read_plates(p) == []
    (tmp_path / "junk.3mf").write_bytes(b"not a zip")
    assert read_plates(tmp_path / "junk.3mf") == []


def test_extract_plate_keeps_only_that_plates_items_and_meshes(tmp_path):
    src = make_bambu(tmp_path / "b.3mf")
    with zipfile.ZipFile(extract_plate(src, tmp_path / "p1.3mf", 1)) as z:
        model = z.read("3D/3dmodel.model").decode()
        names = z.namelist()
        rels = z.read("3D/_rels/3dmodel.model.rels").decode()
    assert model.count("<item") == 1 and "10 10 0" in model and 'id="4"' not in model
    assert "3D/Objects/object_3.model" not in names and "object_3" not in rels
    assert 'xmlns:p=' in model and "p:path" in model  # namespace prefixes untouched

    with zipfile.ZipFile(extract_plate(src, tmp_path / "p2.3mf", 2)) as z:
        model = z.read("3D/3dmodel.model").decode()
        names = z.namelist()
    # Plate 2 has object 4 and the *second* instance of object 2.
    assert model.count("<item") == 2 and "300 10 0" in model and "1 10 10 0" not in model
    assert {"3D/Objects/object_1.model", "3D/Objects/object_3.model"} <= set(names)


def test_filter_build_leaves_resources_alone_when_shared():
    xml = ('<model><resources><object id="1"><mesh/></object><object id="5"><components>'
           '<component objectid="1"/></components></object></resources>'
           '<build><item objectid="5"/><item objectid="1"/></build></model>')
    out = filter_build(xml, {("5", 0)})
    assert '<object id="1">' in out and '<item objectid="1"/>' not in out


# ---- job retention --------------------------------------------------------


def test_prune_old_jobs(store):
    old = store.create("old.3mf", None)
    store.update(old, state="printed", created=time.time() - 40 * 86400, finished_at=time.time() - 35 * 86400)
    busy = store.create("busy.3mf", None)
    store.update(busy, state="printing", created=time.time() - 40 * 86400)
    fresh = store.create("fresh.3mf", None)
    store.update(fresh, state="ready")
    assert store.prune(0) == []
    assert store.prune(30) == [old.id]
    assert store.get(busy.id) and store.get(fresh.id) and not store.get(old.id)
