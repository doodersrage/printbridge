"""A small OctoPrint stand-in for the browser tests: status, upload, controls, files."""

import time

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile

app = FastAPI()
API_KEY = "e2e-key"
S: dict = {}
FILES: dict[str, int] = {}


def reset():
    S.clear()
    S.update(file=None, state="Operational", progress=None, temps=[24.0, 0.0, 23.0, 0.0], commands=[])
    FILES.clear()
    FILES.update({"old_cloud_print.gcode": 7_049_278, "benchy.gcode": 2_210_000})


reset()


def auth(r: Request):
    if r.headers.get("x-api-key") != API_KEY:
        raise HTTPException(403, "Invalid API key")


@app.get("/api/job")
def job(r: Request):
    auth(r)
    return {"state": S["state"], "job": {"file": {"name": S["file"]}},
            "progress": {"completion": S["progress"], "printTime": 1260, "printTimeLeft": 2400}}


@app.get("/api/printer")
def printer(r: Request):
    auth(r)
    t = S["temps"]
    return {"temperature": {"tool0": {"actual": t[0], "target": t[1]}, "bed": {"actual": t[2], "target": t[3]}}}


@app.post("/api/files/local")
async def upload(r: Request, file: UploadFile = File(...), print: str = Form("false")):
    auth(r)
    FILES[file.filename] = len(await file.read())
    if print == "true":
        S.update(file=file.filename, state="Printing", progress=34.0)
    return {"done": True}


@app.get("/api/files/local")
def files(r: Request):
    auth(r)
    return {"files": [{"name": n, "size": s, "date": time.time() - 86400 * i, "type": "machinecode"}
                      for i, (n, s) in enumerate(FILES.items())]}


@app.delete("/api/files/local/{name}")
def delete(name: str, r: Request):
    auth(r)
    FILES.pop(name, None)
    return {}


@app.post("/api/printer/command")
async def command(r: Request):
    auth(r)
    body = await r.json()
    S["commands"].extend(body["commands"])
    for c in body["commands"]:
        if c.startswith("M104"):
            S["temps"][1] = float(c.split("S")[1])
        if c.startswith("M140"):
            S["temps"][3] = float(c.split("S")[1])
    return {}


@app.post("/api/job")
async def control(r: Request):
    auth(r)
    b = await r.json()
    S["state"] = {"cancel": "Operational"}.get(b["command"], "Paused" if b.get("action") == "pause" else "Printing")
    return {}


# test-only helpers
@app.get("/_commands")
def commands():
    return S["commands"]


@app.post("/_set")
async def set_state(r: Request):
    S.update(await r.json())
    return {}


@app.post("/_reset")
def do_reset():
    reset()
    return {}
