"""Chromium enrollment smoke against a local synthetic OAuth provider.

The redirect is rewritten before it reaches Chromium: no request can go to
Twitch. State, browser cookies, POST provenance and channel confirmation use
real application handlers and real local HTTP.
"""

import asyncio
import socket
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import AsyncMock

from aiohttp import web
from playwright.async_api import async_playwright
from twitchio import Scopes

from voxer.config import RuntimeSettings
from voxer.lifecycle import StartupStatus
from voxer.setup import Grant, SetupAdapter


class SyntheticAdapter(SetupAdapter):
    async def oauth_redirect(self, request):
        response = await super().oauth_redirect(request)
        if response.status == 302:
            query = urlsplit(response.headers["Location"]).query
            response.headers["Location"] = self.origin + "/synthetic-provider?" + query
        return response

    async def provider(self, request):
        state = parse_qs(request.query_string)["state"][0]
        return web.HTTPFound(
            self.redirect_url
            + "?"
            + urlencode({"state": state, "code": "synthetic-code"})
        )


async def main():
    with tempfile.TemporaryDirectory(prefix="voxer-setup-browser-") as directory:
        root = Path(directory)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = RuntimeSettings.from_env(
            {
                "VOXER_OAUTH_HOST": "127.0.0.1",
                "VOXER_OAUTH_PORT": str(port),
                "VOXER_SETTINGS_FILE": str(root / "settings.json"),
                "VOXER_TOKEN_FILE": str(root / "tokens.json"),
                "VOXER_OVERLAY_TOKEN": "o" * 43,
            }
        )
        adapter = SyntheticAdapter(
            settings, Scopes(["user:read:chat"]), StartupStatus()
        )
        adapter.router.add_get("/synthetic-provider", adapter.provider)
        adapter.exchange = AsyncMock(
            return_value=Grant(
                "123", "synthetic_channel", "not-real-access", "not-real-refresh"
            )
        )
        runner = web.AppRunner(adapter, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        errors.append(message.text) if message.type == "error" else None
                    ),
                )
                adapter._capability = ("synthetic-capability", time.monotonic() + 300)
                await page.goto(adapter.origin + "/setup?key=synthetic-capability")
                assert "key=" not in page.url
                await page.get_by_label("Client ID", exact=True).fill("syntheticapp")
                await page.get_by_label("Client Secret", exact=True).fill(
                    "syntheticsecret"
                )
                await page.get_by_role("button", name="Save and connect Twitch").click()
                await page.get_by_role("button", name="Confirm channel").wait_for(
                    timeout=5000
                )
                assert not adapter.enrolled.is_set()
                await page.get_by_role("button", name="Confirm channel").click()
                await page.get_by_role("link", name="Refresh status").wait_for()
                assert adapter.enrolled.is_set()
                adapter.status.model = adapter.status.chat = True
                adapter.status.stage = "ready"
                # Startup pages must advance automatically; the operator
                # should not have to discover a manual refresh after consent.
                await page.get_by_text("OBS Browser Source URL:").wait_for(timeout=5000)
                assert "not-real-access" not in await page.content()
                assert not errors, errors
                assert await page.evaluate("Object.keys(localStorage).length") == 0
                assert await page.evaluate("Object.keys(sessionStorage).length") == 0
                await browser.close()
                print("Chromium setup/local OAuth/channel confirmation/OBS URL: passed")
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
