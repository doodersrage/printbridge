import base64
import importlib

import httpx
import pytest


async def make_client(tmp_path, monkeypatch, fake_slicer, **env):
    monkeypatch.setenv("PRINTBRIDGE_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("PRINTBRIDGE_SLICER", str(fake_slicer))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from printbridge import config, main

    importlib.reload(config)
    main = importlib.reload(main)
    ctx = main.app.router.lifespan_context(main.app)
    await ctx.__aenter__()
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
    c.main, c.ctx = main, ctx
    return c


@pytest.fixture
async def secured(tmp_path, monkeypatch, fake_slicer):
    monkeypatch.delenv("PRINTBRIDGE_SECRET_KEY", raising=False)
    c = await make_client(tmp_path, monkeypatch, fake_slicer, PRINTBRIDGE_USERNAME="maker", PRINTBRIDGE_PASSWORD="s3cret pass")
    yield c
    await c.aclose()
    await c.ctx.__aexit__(None, None, None)


async def login(c, user="maker", pw="s3cret pass"):
    return await c.post("/api/auth/login", json={"username": user, "password": pw})


async def test_unauthenticated_requests_are_blocked(secured):
    assert (await secured.get("/api/jobs")).status_code == 401
    r = await secured.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


async def test_public_paths_stay_open(secured):
    assert (await secured.get("/healthz")).status_code == 200
    assert (await secured.get("/login")).status_code == 200
    assert (await secured.get("/style.css")).status_code == 200


async def test_login_sets_session_cookie_and_logout_clears_it(secured):
    r = await login(secured)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert (await secured.get("/api/jobs")).status_code == 200
    assert (await secured.get("/")).status_code == 200
    await secured.post("/api/auth/logout")
    assert (await secured.get("/api/jobs")).status_code == 401


async def test_wrong_password_rejected(secured):
    assert (await login(secured, pw="nope")).status_code == 401
    assert (await login(secured, user="admin")).status_code == 401
    assert (await secured.get("/api/jobs")).status_code == 401


async def test_lockout_after_repeated_failures(secured):
    for _ in range(5):
        assert (await login(secured, pw="bad")).status_code == 401
    r = await login(secured)
    assert r.status_code == 429 and "Try again" in r.json()["detail"]


async def test_basic_auth_for_scripts(secured):
    good = base64.b64encode(b"maker:s3cret pass").decode()
    bad = base64.b64encode(b"maker:wrong").decode()
    assert (await secured.get("/api/jobs", headers={"Authorization": f"Basic {good}"})).status_code == 200
    assert (await secured.get("/api/jobs", headers={"Authorization": f"Basic {bad}"})).status_code == 401


async def test_tampered_or_expired_session_rejected(secured):
    auth = secured.main.state.auth
    token = auth.make_session()
    user, exp, sig = token.split("|")
    forged = f"{user}|{int(exp) + 10**6}|{sig}"
    assert not auth.check_session(forged)
    expired = f"{user}|1"
    assert not auth.check_session(f"{expired}|{auth._sign(expired)}")
    assert auth.check_session(token)


async def test_password_change_invalidates_sessions(secured):
    auth = secured.main.state.auth
    token = auth.make_session()
    auth.password = "rotated"
    assert not auth.check_session(token)


async def test_secret_key_persisted(secured, tmp_path):
    key = tmp_path / "data" / "secret_key"
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"


async def test_status_endpoint_reports_user(secured):
    await login(secured)
    assert (await secured.get("/api/auth/status")).json() == {"enabled": True, "username": "maker"}


async def test_password_file(tmp_path, monkeypatch, fake_slicer):
    pw = tmp_path / "pw"
    pw.write_text("from-file\n")
    monkeypatch.delenv("PRINTBRIDGE_PASSWORD", raising=False)
    c = await make_client(tmp_path, monkeypatch, fake_slicer, PRINTBRIDGE_PASSWORD_FILE=str(pw))
    try:
        assert (await c.get("/api/jobs")).status_code == 401
        assert (await login(c, user="admin", pw="from-file")).status_code == 200
    finally:
        await c.aclose()
        await c.ctx.__aexit__(None, None, None)


async def test_auth_disabled_without_password(tmp_path, monkeypatch, fake_slicer):
    monkeypatch.delenv("PRINTBRIDGE_PASSWORD", raising=False)
    monkeypatch.delenv("PRINTBRIDGE_PASSWORD_FILE", raising=False)
    c = await make_client(tmp_path, monkeypatch, fake_slicer)
    try:
        assert (await c.get("/api/jobs")).status_code == 200
        assert (await c.get("/api/auth/status")).json()["enabled"] is False
        assert (await c.get("/login")).status_code == 303
    finally:
        await c.aclose()
        await c.ctx.__aexit__(None, None, None)
