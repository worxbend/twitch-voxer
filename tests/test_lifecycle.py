"""Offline token/subscription outcomes and responsive lightweight startup."""

import asyncio
import os
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from twitchio import HTTPException

from voxer import bootstrap
from voxer.bot import VoxBot
from voxer.config import RuntimeSettings, read_settings
from voxer.lifecycle import StartupStatus
from voxer.token_store import read_tokens, write_json_atomic


def http_error(status):
    return HTTPException(
        "synthetic provider failure", route=SimpleNamespace(), status=status, extra=None
    )


@pytest.fixture
async def bot(tmp_path):
    settings = RuntimeSettings.from_env(
        {
            "TWITCH_CLIENT_ID": "app",
            "TWITCH_CLIENT_SECRET": "secret",
            "TWITCH_BOT_USERNAME": "streamer",
            "VOXER_TOKEN_FILE": str(tmp_path / "tokens.json"),
        }
    )
    client = VoxBot(bot_id="123", message_queue=asyncio.Queue(), settings=settings)
    try:
        yield client
    finally:
        await client.close(save_tokens=False)


@pytest.mark.parametrize("error", [TimeoutError(), http_error(429), http_error(503)])
async def test_token_outage_retries_without_consent_or_discard(bot, monkeypatch, error):
    write_json_atomic(
        bot._runtime.TOKEN_FILE, {"123": {"token": "saved", "refresh": "recoverable"}}
    )
    bot.add_token = AsyncMock(side_effect=[error, error, error, error])
    monkeypatch.setattr("voxer.lifecycle.asyncio.sleep", AsyncMock())
    with pytest.raises(type(error)):
        await bot.load_tokens()
    assert bot.add_token.await_count == 4
    assert read_tokens(bot._runtime.TOKEN_FILE)["123"]["refresh"] == "recoverable"
    assert not bot.bot_authorized.is_set()


async def test_token_validation_recovers_and_saves_once(bot, monkeypatch):
    write_json_atomic(
        bot._runtime.TOKEN_FILE, {"123": {"token": "saved", "refresh": "recoverable"}}
    )
    bot.add_token = AsyncMock(
        side_effect=[TimeoutError(), SimpleNamespace(user_id="123")]
    )
    bot.save_tokens = AsyncMock()
    monkeypatch.setattr("voxer.lifecycle.asyncio.sleep", AsyncMock())
    await bot.load_tokens()
    assert bot.add_token.await_count == 2
    bot.save_tokens.assert_awaited_once()


async def test_rejected_token_is_not_retried(bot):
    write_json_atomic(
        bot._runtime.TOKEN_FILE, {"123": {"token": "saved", "refresh": "recoverable"}}
    )
    bot.add_token = AsyncMock(side_effect=ValueError("wrong scopes"))
    await bot.load_tokens()
    bot.add_token.assert_awaited_once()
    assert not bot.bot_authorized.is_set()


async def test_token_save_preserves_other_persisted_grants(bot):
    other = {"token": "other-access", "refresh": "other-refresh"}
    write_json_atomic(bot._runtime.TOKEN_FILE, {"456": other})
    bot._http._tokens["123"] = {
        "user_id": "123",
        "token": "current",
        "refresh": "rotated",
        "last_validated": "synthetic",
    }
    await bot.save_tokens()
    saved = read_tokens(bot._runtime.TOKEN_FILE)
    assert saved["456"] == other
    assert saved["123"]["refresh"] == "rotated"


async def test_oauth_save_failure_does_not_signal_or_subscribe(bot):
    bot.add_token = AsyncMock()
    bot.save_tokens = AsyncMock(side_effect=PermissionError("private"))
    bot.subscribe_for = AsyncMock()
    with pytest.raises(PermissionError):
        await bot.event_oauth_authorized(
            SimpleNamespace(
                user_id="123", access_token="access", refresh_token="refresh"
            )
        )
    assert not bot.bot_authorized.is_set()
    bot.subscribe_for.assert_not_awaited()


async def test_in_memory_admission_alone_cannot_unlock_startup(bot):
    # A callback can admit a token before the startup coordinator reaches
    # ensure_authorized. Failed persistence must not be bypassed by that race.
    bot._http._tokens["123"] = {
        "user_id": "123",
        "token": "unsaved",
        "refresh": "unsaved",
        "last_validated": "synthetic",
    }
    bot.save_tokens = AsyncMock(side_effect=PermissionError("private"))
    with pytest.raises(PermissionError):
        await bot.ensure_authorized()
    assert not bot.bot_authorized.is_set()


async def test_closing_bot_rejects_late_grants_and_refresh_writes(bot):
    bot._closing = True
    bot.add_token = AsyncMock()
    bot.save_tokens = AsyncMock()
    with pytest.raises(OSError):
        await bot.event_oauth_authorized(
            SimpleNamespace(user_id="123", access_token="late", refresh_token="late")
        )
    await bot.event_token_refreshed(object())
    bot.add_token.assert_not_awaited()
    bot.save_tokens.assert_not_awaited()


async def test_optional_registration_cannot_hold_mandatory_readiness(bot):
    from voxer.app import _authorize_and_subscribe
    from voxer.lifecycle import StartupStatus

    status = StartupStatus(model=True, stage="connecting_twitch")
    optional_started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def register(_payloads):
        nonlocal calls
        calls += 1
        if calls == 2:
            optional_started.set()
            await release.wait()
        return SimpleNamespace(errors=[])

    bot.multi_subscribe = AsyncMock(side_effect=register)
    bot.wait_until_ready = AsyncMock()
    bot.ensure_authorized = AsyncMock()

    def activate():
        status.chat = True
        status.stage = "ready"

    task = asyncio.create_task(
        _authorize_and_subscribe(bot, "123", status, on_chat_ready=activate)
    )
    try:
        await asyncio.wait_for(optional_started.wait(), 2)
        assert status.ready
        assert not task.done()
    finally:
        release.set()
        await task


async def test_oauth_success_does_not_register_twice(bot):
    bot.add_token = AsyncMock()
    bot.save_tokens = AsyncMock()
    bot.subscribe_for = AsyncMock()
    await bot.event_oauth_authorized(
        SimpleNamespace(user_id="123", access_token="access", refresh_token="refresh")
    )
    assert bot.bot_authorized.is_set()
    bot.save_tokens.assert_awaited_once()
    bot.subscribe_for.assert_not_awaited()


async def test_required_chat_failure_is_not_ready(bot, monkeypatch):
    bot.multi_subscribe = AsyncMock(
        return_value=SimpleNamespace(errors=[SimpleNamespace(error=http_error(503))])
    )
    monkeypatch.setattr("voxer.lifecycle.asyncio.sleep", AsyncMock())
    with pytest.raises(HTTPException):
        await bot.subscribe_for("123")
    assert bot.multi_subscribe.await_count == 4
    for call in bot.multi_subscribe.await_args_list:
        assert len(call.args[0]) == 1


async def test_optional_failures_do_not_disable_chat(bot):
    bot.multi_subscribe = AsyncMock(
        side_effect=[
            SimpleNamespace(errors=[]),
            SimpleNamespace(errors=[SimpleNamespace(error=http_error(403))]),
        ]
    )
    await bot.subscribe_for("123")
    assert bot.multi_subscribe.await_count == 2
    assert bot.optional_subscription_failures == 1


async def test_duplicate_chat_is_successful(bot):
    bot.multi_subscribe = AsyncMock(
        side_effect=[
            SimpleNamespace(errors=[SimpleNamespace(error=http_error(409))]),
            SimpleNamespace(errors=[]),
        ]
    )
    await bot.subscribe_for("123")
    assert bot.optional_subscription_failures == 0


def test_bootstrap_import_does_not_import_model():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import voxer.bootstrap; assert 'supertonic' not in sys.modules; assert 'voxer.app' not in sys.modules",
        ],
        env={**os.environ, "VOXER_SERVER_PORT": "invalid"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


async def test_fresh_bootstrap_is_live_and_retains_overlay(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("VOXER_OAUTH_PORT", str(port))
    monkeypatch.setenv("VOXER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("VOXER_TOKEN_FILE", str(tmp_path / "tokens.json"))
    task = asyncio.create_task(bootstrap.serve(runtime=AsyncMock()))
    async with aiohttp.ClientSession() as session:
        try:
            for _ in range(100):
                try:
                    response = await session.get(f"http://localhost:{port}/healthz")
                    break
                except aiohttp.ClientConnectionError:
                    await asyncio.sleep(0.01)
            else:
                pytest.fail("bootstrap listener never started")
            assert response.status == 200
            assert (await session.get(f"http://localhost:{port}/readyz")).status == 503
            saved = read_settings(tmp_path / "settings.json")
            assert len(saved["VOXER_OVERLAY_TOKEN"]) >= 32
            assert (tmp_path / "data/messages.json").read_text() == "[]\n"
            assert (tmp_path / "control.json").stat().st_mode & 0o777 == 0o600
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    # Re-running settings loading must retain the OBS credential.
    assert (
        read_settings(tmp_path / "settings.json")["VOXER_OVERLAY_TOKEN"]
        == saved["VOXER_OVERLAY_TOKEN"]
    )


def test_no_readiness_before_all_mandatory_stages():
    status = StartupStatus(stage="ready", model=True, authorized=True)
    assert status.ready is False
    status.chat = True
    assert status.ready is True
    assert not status.detail()["overlay_connected"]
