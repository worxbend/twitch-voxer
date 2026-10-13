"""Lightweight entry point: private setup/status is available before TTS loads."""

import asyncio
import logging
import secrets
from dataclasses import replace
from pathlib import Path

from aiohttp import web

from .bot import OAUTH_SCOPES
from .config import (
    ConfigError,
    load_settings,
    read_settings,
    save_settings,
    validate_overlay_access,
    validate_oauth_listener,
    validate_trusted_proxies,
)
from .lifecycle import StartupStatus, with_signals
from .log import setup_logging
from .setup import SetupAdapter
from .token_store import TokenFileBusyError, TokenFileLock

LOGGER = logging.getLogger(__name__)


async def serve(runtime=None) -> None:
    settings = load_settings()
    setup_logging(settings.LOG_LEVEL)
    validate_oauth_listener(settings)
    validate_trusted_proxies(settings.TRUSTED_PROXIES)
    with TokenFileLock(settings.TOKEN_FILE) as owner:
        if not settings.OVERLAY_TOKEN and "VOXER_OVERLAY_TOKEN" not in settings.sources:
            values = read_settings(settings.SETTINGS_FILE)
            values["VOXER_OVERLAY_TOKEN"] = secrets.token_urlsafe(32)
            save_settings(settings.SETTINGS_FILE, values)
            settings = replace(settings, OVERLAY_TOKEN=values["VOXER_OVERLAY_TOKEN"])
        validate_overlay_access(settings.SERVER_HOST, settings.OVERLAY_TOKEN)
        messages = Path(settings.MESSAGES_PATH)
        messages.parent.mkdir(parents=True, exist_ok=True)
        try:
            with messages.open("x", encoding="utf-8") as handle:
                handle.write("[]\n")
        except FileExistsError:
            pass
        status = StartupStatus(
            stage="connecting_twitch" if settings.BOT_USERNAME else "setup_required"
        )
        adapter = SetupAdapter(
            settings, OAUTH_SCOPES, status, owner_task=asyncio.current_task()
        )
        runner = web.AppRunner(adapter, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, settings.OAUTH_HOST, settings.OAUTH_PORT).start()
            adapter._running = True
            adapter.write_control_file()
            LOGGER.info(
                "Control available. Run ./voxer.sh setup for a private browser session."
            )
            if (
                not settings.BOT_USERNAME
                or not settings.CLIENT_ID
                or not settings.CLIENT_SECRET
            ):
                await adapter.enrolled.wait()
            if runtime is None:
                # Deliberately lazy: importing bootstrap cannot import Supertonic.
                from .app import run

                runtime = run
            try:
                await runtime(
                    adapter.settings, owner=owner, adapter=adapter, status=status
                )
            except Exception as exc:
                component = status.stage
                status.stage = "error"
                status.chat = False
                if isinstance(exc, ConfigError):
                    status.reason = "Invalid runtime configuration. Check environment overrides; stop/start to retry."
                elif component == "preparing_model":
                    status.reason = "Speech model preparation failed. Check connectivity and writable cache; stop/start to retry."
                elif not status.authorized:
                    status.reason = "Twitch connection or token validation failed. Saved grants were retained; check connectivity and stop/start to retry."
                else:
                    status.reason = "Mandatory chat subscription or runtime component failed. Check Twitch availability and stop/start to retry."
                # Avoid printing provider exception groups containing tokens.
                LOGGER.error("Runtime startup failed; private status remains available")
                await asyncio.Event().wait()
        finally:
            status.stage = "stopping"
            await runner.cleanup()
            adapter._running = False


def main() -> None:
    try:
        asyncio.run(with_signals(serve))
    except (ConfigError, TokenFileBusyError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
