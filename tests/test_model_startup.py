"""Native preparation cannot block control HTTP or release ownership early."""

import asyncio
import socket
import threading
from types import SimpleNamespace

import aiohttp
import pytest

from voxer import app, bootstrap
from voxer.config import save_settings
from voxer.token_store import TokenFileBusyError, TokenFileLock, read_tokens


async def test_blocked_model_remains_responsive_and_owned_until_thread_finishes(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    for name in (
        "TWITCH_CLIENT_ID",
        "TWITCH_CLIENT_SECRET",
        "TWITCH_BOT_USERNAME",
        "VOXER_BOT_ID",
        "VOXER_OVERLAY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings_file, token_file = tmp_path / "settings.json", tmp_path / "tokens.json"
    monkeypatch.setenv("VOXER_SETTINGS_FILE", str(settings_file))
    monkeypatch.setenv("VOXER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("VOXER_OAUTH_PORT", str(port))
    save_settings(
        settings_file,
        {
            "TWITCH_CLIENT_ID": "app",
            "TWITCH_CLIENT_SECRET": "secret",
            "TWITCH_BOT_USERNAME": "streamer",
            "VOXER_BOT_ID": "123",
        },
    )
    started, release = threading.Event(), threading.Event()
    main_thread = threading.get_ident()

    def model(**_):
        assert threading.get_ident() != main_thread
        started.set()
        assert release.wait(10)
        return SimpleNamespace(voice_names=[])

    monkeypatch.setattr(app, "TTSService", model)
    task = asyncio.create_task(bootstrap.serve(runtime=app.run))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        async with aiohttp.ClientSession() as client:
            response = await client.get(f"http://localhost:{port}/healthz")
            assert response.status == 200
            assert (await client.get(f"http://localhost:{port}/readyz")).status == 503
            record = read_tokens(tmp_path / "control.json")
            response = await client.get(
                f"http://localhost:{port}/status",
                headers={"Authorization": "Bearer " + record["secret"]},
            )
            assert (await response.json())["stage"] == "preparing_model"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Cancelled native loading must retain its lease"
        async with aiohttp.ClientSession() as client:
            assert (await client.get(f"http://localhost:{port}/readyz")).status == 503
            response = await client.get(
                f"http://localhost:{port}/status",
                headers={"Authorization": "Bearer " + record["secret"]},
            )
            assert (await response.json())["stage"] == "stopping"
            assert (
                await client.post(f"http://localhost:{port}/credentials", data={})
            ).status == 503
        with pytest.raises(TokenFileBusyError):
            with TokenFileLock(token_file):
                pass
    finally:
        task.cancel() if not task.cancelling() else None
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with TokenFileLock(token_file):
        pass
