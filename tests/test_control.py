"""The local administrative bearer must not follow redirects or ambient proxies."""

import asyncio
import urllib.error
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voxer import control
from voxer.token_store import write_json_atomic


async def test_control_bypasses_proxy_and_refuses_redirect(tmp_path, monkeypatch):
    received = []

    async def destination(request):
        received.append(request.headers.get("Authorization"))
        return web.json_response({"stage": "ready"})

    target_app = web.Application()
    target_app.router.add_get("/status", destination)
    target = TestServer(target_app)
    await target.start_server()

    async def redirect(_request):
        raise web.HTTPFound(str(target.make_url("/status")))

    origin_app = web.Application()
    origin_app.router.add_get("/status", redirect)
    origin = TestServer(origin_app)
    await origin.start_server()
    write_json_atomic(
        tmp_path / "control.json",
        {
            "origin": str(origin.make_url("/")).rstrip("/"),
            "secret": "synthetic-admin-secret",
        },
    )
    monkeypatch.setattr(
        control,
        "load_settings",
        lambda: SimpleNamespace(SETTINGS_FILE=str(tmp_path / "settings.json")),
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        with pytest.raises(urllib.error.HTTPError) as failure:
            await asyncio.to_thread(control.request_control, "/status")
        assert failure.value.code == 302  # The origin, not the proxy, answered.
        assert not received
    finally:
        await origin.close()
        await target.close()
