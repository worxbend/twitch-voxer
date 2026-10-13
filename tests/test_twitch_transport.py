"""Pinned TwitchIO contracts over local HTTP; never contact Twitch."""

from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer, make_mocked_request
from twitchio import Client, HTTPException, Scopes
from twitchio.http import Route

from voxer.oauth import SecureOAuthAdapter
from voxer.twitch_transport import install_form_oauth


async def test_oauth_owned_session_is_closed(monkeypatch):
    async def token(_request):
        return web.json_response(
            {"access_token": "access", "token_type": "bearer", "expires_in": 7200}
        )

    app = web.Application()
    app.router.add_post("/oauth2/token", token)
    async with TestServer(app) as server:
        session = aiohttp.ClientSession()
        original = session.request
        monkeypatch.setattr(
            session,
            "request",
            lambda method, url, **kwargs: original(
                method, server.make_url(urlsplit(str(url)).path), **kwargs
            ),
        )
        monkeypatch.setattr(aiohttp, "ClientSession", lambda **_: session)
        client = Client(client_id="app", client_secret="secret")
        install_form_oauth(client)
        try:
            await client._http._init_session()
            await client._http._init_session()  # Idempotent SDK initialization.
            await client._http.client_credentials_token()
        finally:
            await client.close(save_tokens=False)
            assert session.closed


async def test_oauth_form_bodies_isolated_validation_refresh_and_session_ownership(
    monkeypatch,
):
    posts, urls, validation_headers = [], [], []
    redirect = False

    async def token(request):
        assert not request.query
        assert request.content_type == "application/x-www-form-urlencoded"
        assert "Authorization" not in request.headers
        posts.append(dict(await request.post()))
        if redirect:
            raise web.HTTPFound("/destination")
        return web.json_response(
            {
                "access_token": "access",
                "refresh_token": "rotated",
                "expires_in": 7200,
                "scope": ["user:read:chat"],
                "token_type": "bearer",
            }
        )

    async def validate(request):
        authorization = request.headers.get("Authorization")
        validation_headers.append(authorization)
        if authorization != "OAuth access":
            return web.json_response(
                {"client_id": "app", "scopes": [], "expires_in": 7200}
            )
        return web.json_response(
            {
                "client_id": "app",
                "user_id": "123",
                "login": "streamer",
                "scopes": ["user:read:chat"],
                "expires_in": 7200,
            }
        )

    async def helix(request):
        assert request.content_type == "application/json"
        assert await request.json() == {"message": "synthetic"}
        assert request.headers["Authorization"] == "Bearer access"
        return web.json_response({"data": []})

    app = web.Application()
    app.router.add_post("/oauth2/token", token)
    app.router.add_post("/oauth2/revoke", token)
    app.router.add_get("/oauth2/validate", validate)
    app.router.add_post("/helix/chat/messages", helix)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        original = session.request

        def local_request(method, url, **kwargs):
            urls.append(str(url))
            assert urlsplit(str(url)).hostname in {"id.twitch.tv", "api.twitch.tv"}
            return original(method, server.make_url(urlsplit(str(url)).path), **kwargs)

        monkeypatch.setattr(session, "request", local_request)
        client = Client(client_id="app", client_secret="secret", session=session)
        install_form_oauth(client)
        try:
            await client._http.client_credentials_token()
            client._http._app_token = "app-bearer"
            adapter = SecureOAuthAdapter(
                redirect_url="http://localhost:4343/oauth/callback",
                expected_user_id="123",
                client_id="app",
                scopes=Scopes(["user:read:chat"]),
                host="127.0.0.1",
                port=4343,
            )
            adapter.client = client
            adapter._authorize = AsyncMock()
            result = await adapter.fetch_token(
                make_mocked_request("GET", "/oauth/callback?code=code%2B%2F%25")
            )
            assert result.status == 200
            assert result.payload.user_id == "123"
            adapter._authorize.assert_awaited_once()
            await client.add_token("access", "refresh+/%=")
            await client._http._refresh_token("123", "refresh+/%=")
            assert client.tokens["123"]["refresh"] == "rotated"
            await client._http.revoke_token("access+/%")
            assert posts[3]["token"] == "access+/%"
            await client._http.request_json(
                Route(
                    "POST",
                    "chat/messages",
                    token_for="123",
                    json={"message": "synthetic"},
                )
            )
            assert posts[0]["grant_type"] == "client_credentials"
            assert posts[1]["code"] == "code+/%"
            assert posts[1]["redirect_uri"] == adapter.redirect_url
            assert posts[2]["refresh_token"] == "refresh+/%="
            assert all(post["client_secret"] == "secret" for post in posts)
            assert all(not urlsplit(url).query for url in urls)
            assert validation_headers == ["OAuth access", "OAuth access"]
            redirect = True
            with pytest.raises(HTTPException):
                await client._http.client_credentials_token()
            assert not any("destination" in url for url in urls)
        finally:
            await client.close(save_tokens=False)
        assert not session.closed, "The caller still owns the borrowed session"
