"""Real local HTTP enrollment with synthetic Twitch exchange; no provider traffic."""

import re
import time
from unittest.mock import AsyncMock
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from twitchio import Scopes

from voxer.config import RuntimeSettings, load_settings, read_settings
from voxer.lifecycle import StartupStatus
from voxer.setup import Grant, SetupAdapter
from voxer.token_store import read_tokens


@pytest.fixture
async def setup_server(tmp_path):
    settings = RuntimeSettings.from_env(
        {
            "VOXER_SETTINGS_FILE": str(tmp_path / "settings.json"),
            "VOXER_TOKEN_FILE": str(tmp_path / "tokens.json"),
            "VOXER_OVERLAY_TOKEN": "o" * 43,
        }
    )
    adapter = SetupAdapter(settings, Scopes(["user:read:chat"]), StartupStatus())
    adapter.exchange = AsyncMock(
        return_value=Grant("123", "streamer", "private-access", "private-refresh")
    )
    client = TestClient(TestServer(adapter), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    client.session.headers.update({"Host": "localhost:4343", "Origin": adapter.origin})
    try:
        yield client, adapter
    finally:
        await client.close()


async def session(client, adapter):
    response = await client.post(
        "/control/open", headers={"Authorization": "Bearer " + adapter._control_secret}
    )
    url = (await response.json())["url"]
    response = await client.get(urlsplit(url).path + "?" + urlsplit(url).query)
    assert "key=" not in str(response.url)
    assert response.status == 200
    content = await response.text()
    csrf = re.search(r'name="csrf" value="([^"]+)"', content)[1]
    return csrf, url


async def grant(client, adapter, csrf):
    response = await client.post(
        "/credentials",
        data={"csrf": csrf, "client_id": "app", "client_secret": "secret"},
        allow_redirects=False,
    )
    assert response.status == 303
    response = await client.get("/oauth", allow_redirects=False)
    state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    response = await client.get(
        "/oauth/callback", params={"state": state, "code": "synthetic"}
    )
    assert response.status == 200
    assert "Confirm channel" in await response.text()


async def test_enrollment_is_private_browser_bound_and_durable(setup_server):
    client, adapter = setup_server
    assert (await client.get("/setup")).status == 403
    assert (await client.get("/oauth")).status == 403
    assert (await client.get("/status")).status == 403
    assert (await client.post("/control/open")).status == 403
    assert (await client.get("/readyz")).status == 503
    csrf, url = await session(client, adapter)
    # A capability is one-use, and it is removed from the browser URL.
    assert (
        await client.get(urlsplit(url).path + "?" + urlsplit(url).query)
    ).status == 403
    await grant(client, adapter, csrf)
    assert not adapter.enrolled.is_set()
    response = await client.post("/confirm", data={"csrf": csrf})
    assert response.status == 200
    assert adapter.enrolled.is_set()
    assert adapter.settings.BOT_ID == "123"
    saved = read_settings(adapter.settings.SETTINGS_FILE)
    assert saved["TWITCH_CLIENT_SECRET"] == "secret"
    assert saved["TWITCH_BOT_USERNAME"] == "streamer"
    assert (
        read_tokens(adapter.settings.TOKEN_FILE)["123"]["refresh"] == "private-refresh"
    )
    for path in (adapter.settings.SETTINGS_FILE, adapter.settings.TOKEN_FILE):
        from pathlib import Path

        assert Path(path).stat().st_mode & 0o777 == 0o600
    restarted = load_settings(
        {"VOXER_SETTINGS_FILE": adapter.settings.SETTINGS_FILE}, dotenv_path=None
    )
    assert restarted.BOT_ID == "123"
    assert restarted.NO_ANNOUNCE_USERS == frozenset({"streamer"})
    detail = await (await client.get("/status")).text()
    page = await response.text()
    for secret in ("private-access", "private-refresh", "secret"):
        assert secret not in detail
        assert secret not in page
    assert (await client.post("/confirm", data={"csrf": csrf})).status == 409
    assert (
        await client.post(
            "/credentials",
            data={"csrf": csrf, "client_id": "other", "client_secret": "other"},
        )
    ).status == 409


@pytest.mark.parametrize(
    "invalid", ["origin", "csrf", "host", "expired", "input", "oversized"]
)
async def test_credential_boundary_refuses_abuse(setup_server, invalid):
    client, adapter = setup_server
    csrf, _ = await session(client, adapter)
    data = {"csrf": csrf, "client_id": "app", "client_secret": "never-echo-this"}
    headers = {}
    if invalid == "origin":
        headers["Origin"] = "https://attacker.invalid"
    elif invalid == "csrf":
        data["csrf"] = "wrong"
    elif invalid == "host":
        headers["Host"] = "attacker.invalid"
    elif invalid == "expired":
        adapter._session = (adapter._session[0], csrf, 0)
    elif invalid == "input":
        data["client_id"] = "<script>"
    elif invalid == "oversized":
        data["client_secret"] = "x" * 5000
    response = await client.post("/credentials", data=data, headers=headers)
    assert response.status in {400, 403, 413, 422}
    assert not read_settings(adapter.settings.SETTINGS_FILE)
    assert "never-echo-this" not in await response.text()


async def test_expired_capability(setup_server):
    client, adapter = setup_server
    adapter._capability = ("expired", time.monotonic() - 1)
    assert (await client.get("/setup?key=expired")).status == 403


async def test_callback_without_browser_cookie_cannot_exchange(setup_server):
    client, adapter = setup_server
    csrf, _ = await session(client, adapter)
    await client.post(
        "/credentials",
        data={"csrf": csrf, "client_id": "app", "client_secret": "secret"},
        allow_redirects=False,
    )
    response = await client.get("/oauth", allow_redirects=False)
    state = parse_qs(urlsplit(response.headers["Location"]).query)["state"][0]
    client.session.cookie_jar.clear()
    assert (
        await client.get(
            "/oauth/callback", params={"state": state, "code": "synthetic"}
        )
    ).status == 400
    adapter.exchange.assert_not_awaited()


@pytest.mark.parametrize(
    "stage", ["credentials", "confirm", "settings-confirm", "version"]
)
async def test_state_read_failure_never_reports_success(setup_server, stage):
    client, adapter = setup_server
    csrf, _ = await session(client, adapter)
    if stage in {"confirm", "settings-confirm"}:
        await grant(client, adapter, csrf)
    path = Path(
        adapter.settings.TOKEN_FILE
        if stage == "confirm"
        else adapter.settings.SETTINGS_FILE
    )
    content = '{"version": 999}' if stage == "version" else "{malformed"
    path.write_text(content)
    if stage in {"confirm", "settings-confirm"}:
        response = await client.post("/confirm", data={"csrf": csrf})
    else:
        response = await client.post(
            "/credentials",
            data={"csrf": csrf, "client_id": "app", "client_secret": "secret"},
        )
    assert response.status == 503
    assert not adapter.enrolled.is_set()
    assert path.read_text() == content
    assert "malformed" not in await response.text()


async def test_enrollment_preserves_other_persisted_grants(setup_server):
    from voxer.token_store import write_json_atomic

    client, adapter = setup_server
    previous = {"user_id": "456", "token": "other-access", "refresh": "other-refresh"}
    write_json_atomic(adapter.settings.TOKEN_FILE, {"456": previous})
    csrf, _ = await session(client, adapter)
    await grant(client, adapter, csrf)
    response = await client.post("/confirm", data={"csrf": csrf})
    assert response.status == 200
    assert read_tokens(adapter.settings.TOKEN_FILE)["456"] == previous


async def test_persistence_failure_does_not_complete_enrollment(
    setup_server, monkeypatch
):
    client, adapter = setup_server
    csrf, _ = await session(client, adapter)
    await grant(client, adapter, csrf)

    def denied(*_):
        raise PermissionError("private-path")

    monkeypatch.setattr("voxer.setup.save_settings", denied)
    response = await client.post("/confirm", data={"csrf": csrf})
    assert response.status == 503
    assert not adapter.enrolled.is_set()
    assert adapter.settings.BOT_ID == ""
    assert adapter.status.ready is False
    assert "private-path" not in await response.text()


@pytest.mark.parametrize(
    "invalid", ["client", "scope", "account", "login", "id-format", "refresh", "none"]
)
async def test_enrollment_exchange_verifies_provider_identity(
    setup_server, monkeypatch, invalid
):
    _, adapter = setup_server
    adapter.settings = replace(
        adapter.settings,
        CLIENT_ID="app",
        CLIENT_SECRET="secret",
        BOT_ID="123",
        BOT_USERNAME="streamer",
    )
    validated = SimpleNamespace(
        client_id="app", user_id="123", login="streamer", scopes=["user:read:chat"]
    )
    if invalid == "client":
        validated.client_id = "other"
    elif invalid == "scope":
        validated.scopes = []
    elif invalid == "account":
        validated.user_id = "other"
    elif invalid == "login":
        # A legacy username-only installation still needs a matching login.
        adapter.settings = replace(adapter.settings, BOT_ID="")
        validated.login = "other"
    elif invalid == "id-format":
        adapter.settings = replace(adapter.settings, BOT_ID="")
        validated.user_id = "<not-numeric>"
    client = SimpleNamespace(
        _http=SimpleNamespace(
            user_access_token=AsyncMock(
                return_value=SimpleNamespace(
                    access_token="access", refresh_token="refresh"
                )
            ),
            validate_token=AsyncMock(return_value=validated),
        ),
        close=AsyncMock(),
    )
    monkeypatch.setattr("voxer.setup.Client", lambda **_: client)
    if invalid == "refresh":
        client._http.user_access_token.return_value.refresh_token = ""
    if invalid == "none":
        result = await SetupAdapter.exchange(adapter, "code")
        assert result.user_id == "123"
    else:
        with pytest.raises(ValueError):
            await SetupAdapter.exchange(adapter, "code")
    client.close.assert_awaited_once_with(save_tokens=False)
    client._http.user_access_token.assert_awaited_once_with(
        "code", redirect_uri=adapter.redirect_url
    )


@pytest.mark.parametrize("stage", ["stopping", "error"])
async def test_closed_runtime_rejects_authorization_but_keeps_health(
    setup_server, stage
):
    client, adapter = setup_server
    adapter.status.stage = stage
    response = await client.get("/oauth", allow_redirects=False)
    assert response.status == 503
    assert adapter.status.stage == stage
    assert (await client.get("/healthz")).status == 200
    assert (await client.get("/readyz")).status == 503


async def test_page_refreshes_startup_but_not_credential_entry(setup_server):
    client, adapter = setup_server
    _, page = await session(client, adapter)
    assert "http-equiv='refresh'" not in page
    adapter.settings = replace(
        adapter.settings,
        CLIENT_ID="app",
        CLIENT_SECRET="secret",
        BOT_USERNAME="streamer",
    )
    adapter.status.stage = "preparing_model"
    page = await (await client.get("/setup")).text()
    assert "http-equiv='refresh'" in page
    assert "Authorize Twitch again" not in page
    adapter.status.stage = "error"
    assert "http-equiv='refresh'" not in await (await client.get("/setup")).text()


async def test_runtime_readiness_is_independent_of_obs(setup_server):
    client, adapter = setup_server
    csrf, _ = await session(client, adapter)
    await grant(client, adapter, csrf)
    await client.post("/confirm", data={"csrf": csrf})
    adapter.status.model = adapter.status.chat = True
    adapter.status.stage = "ready"
    assert (await client.get("/readyz")).status == 200
    page = await (await client.get("/setup")).text()
    assert "waiting for OBS" in page
    assert "http://localhost:8080/simple?token=" in page
    for header in ("Cache-Control", "Referrer-Policy", "Content-Security-Policy"):
        assert header in (await client.get("/setup")).headers
